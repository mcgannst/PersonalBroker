"""Nightly job (SPEC §9, 20:00 ET Sun–Thu): FinViz universe → symbol IDs → daily candles and ATR14 →
opening-bar history → cache. Prepares the NEXT session.

A run replaces the day: the universe snapshot and opening-bar stats of `session_date` end up holding
exactly this run's symbols (rows a previous run left for other symbols are deleted in the same
transaction as the upserts).

`open_bar_stats.avg_open_vol_14d` is the mean 09:30 five-minute volume over the lookback sessions. It
is NULL unless at least MIN_OPENING_BARS opening bars were found (or every lookback session, when
`open_bar.lookback_sessions` is below that), so a thin history never passes for a real average.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.parser import UniverseRow, to_questrade_ticker
from trader.adapters.finviz.scraper import FinvizError
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtSymbol
from trader.db.session import session_scope
from trader.events import log_event
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.market.indicators import atr, average_volume, opening_bar
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

DAILY_LOOKBACK = timedelta(days=30)
CHUNK_SIZE = 50  # symbols whose candles are fetched and reduced at a time (bounds memory)
MIN_OPENING_BARS = 10
MAX_ERROR_TICKERS = 50
# The last lookback session's daily bar is final only some time after its close.
SETTLE_AFTER_CLOSE = timedelta(minutes=15)


class UniverseSource(Protocol):
    def universe(self, filters: str) -> list[UniverseRow]: ...


class MarketData(Protocol):
    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]: ...

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]: ...


@dataclass
class NightlyDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    finviz: UniverseSource
    market: MarketData
    settings: RuntimeSettings


def target_session(calendar: SessionCalendar, clock: Clock) -> date:
    """The next session after today (ET): the one tonight's run prepares."""
    return calendar.next_session(et_date(clock.now()))


def earliest_run_time(calendar: SessionCalendar, session_date: date, lookback_sessions: int) -> datetime:
    """When a run for `session_date` may start without `--force`: 15 minutes after the close of the
    last lookback session, so a daytime run can't store that session's partial daily bar."""
    last = calendar.sessions_before(session_date, lookback_sessions)[-1]
    return calendar.session_close(last) + SETTLE_AFTER_CLOSE


def sessions_between(calendar: SessionCalendar, start: date, end: date) -> int:
    """How many sessions s satisfy start < s <= end (1 = `start` is the session right before `end`)."""
    n, d = 0, start
    while (d := calendar.next_session(d)) <= end:
        n += 1
    return n


@dataclass
class _Universe:
    rows: list[UniverseRow]
    source: str
    fallback: dict[str, Any] = field(default_factory=dict)  # detail keys, only for a fallback


async def _universe(deps: NightlyDeps, session_date: date) -> _Universe:
    # FinvizError covers every scraper failure: FinvizBlocked, FinvizHttpError, FinvizParseError and
    # FinvizFilterIgnored (P1-T5). The scraper never returns an empty or partial universe.
    try:
        rows = await asyncio.to_thread(deps.finviz.universe, deps.settings.universe_finviz_filters)
        return _Universe(rows, "finviz")
    except FinvizError as exc:
        with session_scope(deps.factory) as s:
            prev = repo.latest_universe_tickers(s, before=session_date)
            if prev is None:
                log_event(
                    s,
                    deps.clock,
                    "error",
                    "job.nightly",
                    "FinViz failed and no previous universe exists",
                    {"error": str(exc)},
                )
            else:
                # A fallback is an alert (Review Focus 3), and a fallback of a fallback reports the
                # date the universe really came from FinViz.
                origin = repo.universe_origin(s, prev[0])
                age = sessions_between(deps.calendar, origin, session_date)
                stale = age > deps.settings.universe_fallback_stale_after_sessions
                fallback = {
                    "fallback_from": origin.isoformat(),
                    "fallback_age_sessions": age,
                    "fallback_stale": stale,
                }
                log_event(
                    s,
                    deps.clock,
                    "error",
                    "job.nightly",
                    "FinViz failed; using previous universe",
                    {
                        "error": str(exc),
                        "from": origin.isoformat(),
                        "snapshot": prev[0].isoformat(),
                        "age_sessions": age,
                    },
                )
                if stale:
                    log_event(
                        s,
                        deps.clock,
                        "error",
                        "job.nightly",
                        "fallback universe too old",
                        {
                            "from": origin.isoformat(),
                            "age_sessions": age,
                            "stale_after_sessions": deps.settings.universe_fallback_stale_after_sessions,
                        },
                    )
        if prev is None:
            raise  # after the scope commits, so the error event is kept
        return _Universe([UniverseRow(t, "", "", "", None, None) for t in prev[1]], "fallback", fallback)


@dataclass
class _Fetched:
    """What the write phase needs for one symbol (candle responses are reduced right away)."""

    sym: QtSymbol
    row: UniverseRow | None
    daily: list[Candle]
    opening: list[Candle]
    errors: int  # failed candle requests (0, 1 or 2)


async def _fetch_chunk(
    deps: NightlyDeps, chunk: list[tuple[QtSymbol, UniverseRow | None]], lookback: list[date]
) -> list[_Fetched]:
    end_of_prev = datetime.combine(lookback[-1] + timedelta(days=1), time(0), tzinfo=ET)
    bars_from, bars_to = deps.calendar.session_open(lookback[0]), deps.calendar.session_close(lookback[-1])
    reqs = [
        (
            CandleRequest(sym.symbol_id, end_of_prev - DAILY_LOOKBACK, end_of_prev, "OneDay"),
            CandleRequest(sym.symbol_id, bars_from, bars_to, "FiveMinutes"),
        )
        for sym, _ in chunk
    ]
    results = await deps.market.candles_many([r for pair in reqs for r in pair])
    out: list[_Fetched] = []
    for (sym, row), (daily_req, bar_req) in zip(chunk, reqs, strict=True):
        daily, bars = results.pop(daily_req), results.pop(bar_req)
        # atr() requires strictly ascending, unique starts (P1-T8), and ON CONFLICT can't touch one
        # row twice: normalise so one bad symbol never aborts the whole job.
        daily_ok = (
            sorted({c.start: c for c in daily}.values(), key=lambda c: c.start)
            if isinstance(daily, list)
            else []
        )
        opening = (
            [b for d in lookback if (b := opening_bar(bars, deps.calendar, d))]
            if isinstance(bars, list)
            else []
        )
        errors = isinstance(daily, QuestradeApiError) + isinstance(bars, QuestradeApiError)
        out.append(_Fetched(sym, row, daily_ok, opening, errors))
    return out


async def run_nightly(deps: NightlyDeps, session_date: date) -> dict[str, Any]:
    universe = await _universe(deps, session_date)
    # Questrade form (BF-B -> BF.B). The scraper already does this; any other source might not.
    by_ticker = {to_questrade_ticker(r.ticker): r for r in universe.rows}
    wanted = list(dict.fromkeys([*by_ticker, *deps.settings.universe_extra_symbols]))
    resolved = await deps.market.symbols_by_names(wanted)
    unresolved = sorted(set(wanted) - set(resolved))

    # Two requested names can resolve to one Questrade symbol (an alias): keep one entry per symbol,
    # preferring a name that has a FinViz row (for its price).
    unique: dict[int, tuple[QtSymbol, UniverseRow | None]] = {}
    for name, sym in resolved.items():
        row = by_ticker.get(name)
        if sym.symbol_id not in unique or (unique[sym.symbol_id][1] is None and row is not None):
            unique[sym.symbol_id] = (sym, row)
    entries = list(unique.values())

    lookback = deps.calendar.sessions_before(session_date, deps.settings.open_bar_lookback_sessions)
    min_bars = min(MIN_OPENING_BARS, len(lookback))
    fetched: list[_Fetched] = []
    for i in range(0, len(entries), CHUNK_SIZE):
        fetched.extend(await _fetch_chunk(deps, entries[i : i + CHUNK_SIZE], lookback))
    failed = [f for f in fetched if f.errors]

    with session_scope(deps.factory) as s:
        snapshot: list[repo.UniverseSnapshotRow] = []
        stats: list[tuple[int, Decimal | None, Decimal | None]] = []
        for f in fetched:
            sid = repo.upsert_symbols(s, [f.sym], clock=deps.clock)[f.sym.symbol]
            repo.upsert_daily_candles(s, sid, f.daily)
            repo.upsert_intraday_candles(s, sid, "5m", f.opening)
            atr14 = atr(f.daily, 14)  # full ~20-session history so Wilder smoothing applies
            avg_vol = average_volume(f.daily[-14:])
            avg_open = average_volume(f.opening) if len(f.opening) >= min_bars else None
            stats.append((sid, avg_open, atr14))
            price = f.row.price if f.row is not None else None
            if price is None and f.daily:  # e.g. a fallback row: use the last daily close
                price = f.daily[-1].close
            snapshot.append(
                repo.UniverseSnapshotRow(
                    symbol_id=sid,
                    price=price,
                    avg_volume=int(avg_vol) if avg_vol is not None else None,
                    atr14=atr14,
                    source=universe.source,
                )
            )
        repo.delete_session_rows_except(s, session_date, [r.symbol_id for r in snapshot])
        repo.save_universe_snapshot(s, session_date, snapshot)
        repo.save_open_bar_stats(s, session_date, stats)
        detail: dict[str, Any] = {
            "session_date": session_date.isoformat(),
            "source": universe.source,
            "universe": len(snapshot),
            "unresolved": unresolved,
            "candle_errors": sum(f.errors for f in failed),
            "candle_error_tickers": sorted(f.sym.symbol for f in failed)[:MAX_ERROR_TICKERS],
            **universe.fallback,
        }
        log_event(s, deps.clock, "info", "job.nightly", f"universe ready for {session_date}", detail)
    return detail
