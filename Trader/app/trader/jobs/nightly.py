"""Nightly job (SPEC §9, 20:00 ET Sun–Thu): FinViz universe → symbol IDs → daily candles and ATR14 →
opening-bar history → cache. Prepares the NEXT session."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
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


async def _universe(deps: NightlyDeps, session_date: date) -> tuple[list[UniverseRow], str, str | None]:
    # FinvizError covers every scraper failure: FinvizBlocked, FinvizHttpError, FinvizParseError and
    # FinvizFilterIgnored (P1-T5). The scraper never returns an empty or partial universe.
    try:
        rows = await asyncio.to_thread(deps.finviz.universe, deps.settings.universe_finviz_filters)
        return rows, "finviz", None
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
                log_event(
                    s,
                    deps.clock,
                    "warning",
                    "job.nightly",
                    "FinViz failed; using previous universe",
                    {"error": str(exc), "from": prev[0].isoformat()},
                )
        if prev is None:
            raise  # after the scope commits, so the error event is kept
        rows = [UniverseRow(t, "", "", "", None, None) for t in prev[1]]
        return rows, "fallback", prev[0].isoformat()


async def run_nightly(deps: NightlyDeps, session_date: date) -> dict[str, Any]:
    rows, source, fallback_from = await _universe(deps, session_date)
    # Questrade form (BF-B -> BF.B). The scraper already does this; any other source might not.
    by_ticker = {to_questrade_ticker(r.ticker): r for r in rows}
    wanted = list(dict.fromkeys([*by_ticker, *deps.settings.universe_extra_symbols]))
    resolved = await deps.market.symbols_by_names(wanted)
    unresolved = sorted(set(wanted) - set(resolved))

    lookback = deps.calendar.sessions_before(session_date, deps.settings.open_bar_lookback_sessions)
    end_of_prev = datetime.combine(lookback[-1] + timedelta(days=1), time(0), tzinfo=ET)
    daily_reqs = {
        s.symbol_id: CandleRequest(s.symbol_id, end_of_prev - DAILY_LOOKBACK, end_of_prev, "OneDay")
        for s in resolved.values()
    }
    bar_reqs = {
        s.symbol_id: CandleRequest(
            s.symbol_id,
            deps.calendar.session_open(lookback[0]),
            deps.calendar.session_close(lookback[-1]),
            "FiveMinutes",
        )
        for s in resolved.values()
    }
    results = await deps.market.candles_many([*daily_reqs.values(), *bar_reqs.values()])
    errors = sum(isinstance(v, QuestradeApiError) for v in results.values())

    with session_scope(deps.factory) as s:
        ids = repo.upsert_symbols(s, resolved.values())
        snapshot: list[repo.UniverseSnapshotRow] = []
        stats: list[tuple[int, Decimal | None, Decimal | None]] = []
        for ticker, sym in resolved.items():
            sid = ids[ticker]
            daily = results[daily_reqs[sym.symbol_id]]
            bars = results[bar_reqs[sym.symbol_id]]
            # atr() requires strictly ascending, unique starts (P1-T8), and ON CONFLICT can't
            # touch one row twice: normalise so one bad symbol never aborts the whole job.
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
            repo.upsert_daily_candles(s, sid, daily_ok)
            repo.upsert_intraday_candles(s, sid, "5m", opening)
            atr14 = atr(daily_ok, 14)  # full ~20-session history so Wilder smoothing applies
            avg_vol = average_volume(daily_ok[-14:])
            stats.append((sid, average_volume(opening), atr14))
            row = by_ticker.get(ticker)
            snapshot.append(
                repo.UniverseSnapshotRow(
                    symbol_id=sid,
                    price=row.price if row else (daily_ok[-1].close if daily_ok else None),
                    avg_volume=int(avg_vol) if avg_vol is not None else None,
                    atr14=atr14,
                    source=source,
                )
            )
        repo.save_universe_snapshot(s, session_date, snapshot)
        repo.save_open_bar_stats(s, session_date, stats)
        detail: dict[str, Any] = {
            "session_date": session_date.isoformat(),
            "source": source,
            "universe": len(snapshot),
            "unresolved": unresolved,
            "candle_errors": errors,
        }
        if fallback_from:
            detail["fallback_from"] = fallback_from
        log_event(s, deps.clock, "info", "job.nightly", f"universe ready for {session_date}", detail)
    return detail
