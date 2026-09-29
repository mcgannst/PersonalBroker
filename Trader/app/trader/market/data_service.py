"""Market data for strategies and jobs (master plan §7.1): the DB cache first, Questrade second.

Every symbol_id here is trader.symbols.id. Questrade IDs stay at the client boundary: quotes() rewrites
QtQuote.symbol_id to the database ID. Missing data is reported per symbol, never raised (Review Focus 4).
Only complete bars (end <= clock.now()) are ever written to the candle cache.
"""

import asyncio
import dataclasses
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import QuestradeApiError, missing_reason, reason_key
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.db import models as m
from trader.db.session import session_scope
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.market.indicators import opening_bar
from trader.market.types import (
    INTERVAL_CODES,
    Candle,
    Interval,
    OpenBarStats,
    OpeningBars,
    UniverseMember,
    UniverseStatus,
)

OPENING_BAR = timedelta(minutes=5)
OPENING_BAR_CODE = INTERVAL_CODES["FiveMinutes"]
# One batch of opening bars must finish well inside the 60 s budget for the 9:35 scan: ~550 symbols at
# 17 req/s (MARKET_RPS, FIX-PACING) take ~32 s. Symbols still outstanding at the deadline are reported
# as missing "timeout"; the bars that completed by then are kept (FIX-OPENBARS, Mon 2026-09-28).
FETCH_DEADLINE_S = 45.0
# Backstop past the deadline for a client that does not honour `deadline_s` (min of this and the deadline).
DEADLINE_GUARD_S = 5.0
STEP: dict[Interval, timedelta] = {
    "OneMinute": timedelta(minutes=1),
    "FiveMinutes": timedelta(minutes=5),
    "FifteenMinutes": timedelta(minutes=15),
    "OneHour": timedelta(hours=1),
    "OneDay": timedelta(days=1),
}

log = structlog.get_logger("market.data_service")


class QuoteClient(Protocol):
    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]: ...
    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]: ...
    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]: ...


@dataclass(frozen=True)
class OpeningScan:
    """FIX-401: the health of one `opening_bars` call. `reasons` counts the missing reasons without
    Questrade's code and message ("questrade_error: HTTP 401", "timeout", ...)."""

    session_date: date
    universe: int
    bars: int
    missing: int
    reasons: dict[str, int] = field(default_factory=dict)

    def detail(self) -> dict[str, Any]:
        """The job-detail keys: universe, bars, missing, missing_reasons."""
        return {
            "universe": self.universe,
            "bars": self.bars,
            "missing": self.missing,
            "missing_reasons": dict(self.reasons),
        }

    @property
    def top_reason(self) -> str | None:
        if not self.reasons:
            return None
        return min(self.reasons.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    @property
    def unhealthy(self) -> bool:
        """At least half of the universe has no opening bar (or there is no bar at all)."""
        return self.universe > 0 and (self.bars == 0 or 2 * self.missing >= self.universe)


def _from_row(row: m.IntradayCandle, step: timedelta) -> Candle:
    return Candle(row.ts, row.ts + step, row.open, row.high, row.low, row.close, row.volume, row.vwap)


class MarketDataService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        client: QuoteClient,
        *,
        fetch_deadline_s: float = FETCH_DEADLINE_S,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cal = calendar
        self._client = client
        self._fetch_deadline_s = fetch_deadline_s
        # symbols.id -> questrade_id for quotes(), kept for the process lifetime; an entry is dropped when
        # Questrade returns no quote for it, so a re-mapped symbol is re-read from the database.
        self._quote_qids: dict[int, int] = {}
        self._opening_scan: OpeningScan | None = None

    async def _db[T](self, step: Callable[..., T], *args: Any) -> T:
        """Runs one synchronous database step of `quotes()` or `candles()`. Inline here (the worker and the
        jobs); the API's subclass (`trader.api.services.OffLoopMarketData`) runs it in a worker thread, so the
        API's event loop never waits on the database (P4-REVIEW)."""
        return step(*args)

    # --- cache-only reads ---------------------------------------------------------------------------------
    async def universe(self, session_date: date) -> list[UniverseMember]:
        with self._factory() as s:
            rows = s.execute(
                select(m.UniverseSnapshot, m.Symbol)
                .join(m.Symbol, m.Symbol.id == m.UniverseSnapshot.symbol_id)
                .where(m.UniverseSnapshot.session_date == session_date)
                .order_by(m.Symbol.ticker)
            ).all()
        return [
            UniverseMember(sym.id, sym.ticker, sym.name, snap.price, snap.avg_volume, snap.atr14, snap.source)
            for snap, sym in rows
        ]

    async def universe_status(self, session_date: date) -> UniverseStatus:
        with self._factory() as s:
            detail = s.execute(
                select(m.JobRun.detail)
                .where(
                    m.JobRun.job == "nightly",
                    m.JobRun.session_date == session_date,
                    m.JobRun.status == "succeeded",
                )
                .order_by(m.JobRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            source = s.execute(
                select(m.UniverseSnapshot.source)
                .where(m.UniverseSnapshot.session_date == session_date)
                .limit(1)
            ).scalar_one_or_none()
        if source is None:
            return UniverseStatus(None, None, False, None)
        if not isinstance(detail, dict):
            # No successful nightly run vouches for this universe. A fallback one of unknown age is
            # treated as stale (conservative); a finviz or manual one is taken as it is.
            return UniverseStatus(source, None, source == "fallback", None)
        fallback_from = detail.get("fallback_from")
        age = detail.get("fallback_age_sessions")
        return UniverseStatus(
            source=str(detail.get("source", source)),
            fallback_from=date.fromisoformat(fallback_from) if fallback_from else None,
            stale=bool(detail.get("fallback_stale", False)),
            age_sessions=int(age) if age is not None else None,
        )

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        with self._factory() as s:
            rows = s.execute(
                select(m.OpenBarStat).where(m.OpenBarStat.session_date == session_date)
            ).scalars()
            return {r.symbol_id: OpenBarStats(r.symbol_id, r.avg_open_vol_14d, r.atr14) for r in rows}

    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Symbol.ticker, m.Symbol.id)
                .where(m.Symbol.ticker.in_(list(tickers)))
                .order_by(m.Symbol.id)
            ).all()
        out: dict[str, int] = {}
        for ticker, sid in rows:
            out.setdefault(ticker, sid)
        return out

    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]:
        prev = self._cal.previous_session(session_date)
        with self._factory() as s:
            rows = s.execute(
                select(m.DailyCandle.symbol_id, m.DailyCandle.close).where(
                    m.DailyCandle.date == prev, m.DailyCandle.symbol_id.in_(list(symbol_ids))
                )
            ).all()
        return {sid: close for sid, close in rows}

    def _questrade_ids(self, symbol_ids: Sequence[int]) -> dict[int, int]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Symbol.id, m.Symbol.questrade_id).where(
                    m.Symbol.id.in_(list(symbol_ids)), m.Symbol.questrade_id.is_not(None)
                )
            ).all()
        return {sid: int(qid) for sid, qid in rows}

    # --- live or fetched reads ----------------------------------------------------------------------------
    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        wanted = list(dict.fromkeys(symbol_ids))
        uncached = [sid for sid in wanted if sid not in self._quote_qids]
        if uncached:
            self._quote_qids.update(await self._db(self._questrade_ids, uncached))
        qids = {sid: self._quote_qids[sid] for sid in wanted if sid in self._quote_qids}
        if not qids:
            return {}
        back = {qid: sid for sid, qid in qids.items()}
        out: dict[int, QtQuote] = {}
        for q in await self._client.quotes(list(qids.values())):
            if q.symbol_id in back:
                sid = back[q.symbol_id]
                out[sid] = dataclasses.replace(q, symbol_id=sid)
        for sid in qids.keys() - out.keys():  # a miss: the mapping may be stale, re-read it next time
            self._quote_qids.pop(sid, None)
        return out

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        if symbol_ids is None:
            symbol_ids = [x.symbol_id for x in await self.universe(session_date)]
        ids = list(dict.fromkeys(symbol_ids))
        open_ = self._cal.session_open(session_date)
        with self._factory() as s:
            cached = s.execute(
                select(m.IntradayCandle).where(
                    m.IntradayCandle.interval == OPENING_BAR_CODE,
                    m.IntradayCandle.ts == open_,
                    m.IntradayCandle.symbol_id.in_(ids),
                )
            ).scalars()
            bars: dict[int, Candle] = {r.symbol_id: _from_row(r, OPENING_BAR) for r in cached}
        missing: dict[int, str] = {}
        need = [sid for sid in ids if sid not in bars]
        qids = self._questrade_ids(need)
        for sid in need:
            if sid not in qids:
                missing[sid] = "no_questrade_id"
        reqs = {
            sid: CandleRequest(qids[sid], open_, open_ + OPENING_BAR, "FiveMinutes")
            for sid in need
            if sid in qids
        }
        results: dict[CandleRequest, list[Candle] | QuestradeApiError] = {}
        if reqs:
            results = await self._fetch_opening_bars(session_date, list(reqs.values()))
        now = self._clock.now()
        fetched: dict[int, Candle] = {}
        for sid, req in reqs.items():
            result = results.get(req)
            if result is None:
                missing[sid] = "timeout"
            elif isinstance(result, QuestradeApiError):
                missing[sid] = missing_reason(result)
            else:
                found = opening_bar(result, self._cal, session_date)
                if found is None:
                    missing[sid] = "no_bar_at_open"
                elif found.end > now:
                    missing[sid] = "bar_not_complete"
                else:
                    fetched[sid] = found
        if fetched:
            with session_scope(self._factory) as s:
                for sid, found in fetched.items():
                    repo.upsert_intraday_candles(s, sid, OPENING_BAR_CODE, [found])
        bars.update(fetched)
        reasons = Counter(reason_key(r) for r in missing.values())
        self._opening_scan = OpeningScan(session_date, len(ids), len(bars), len(missing), dict(reasons))
        return OpeningBars(bars, missing)

    def pop_opening_scan(self) -> OpeningScan | None:
        """The counts of the last `opening_bars` call, once (the engine takes them after each event)."""
        scan, self._opening_scan = self._opening_scan, None
        return scan

    def _market_stats(self) -> dict[str, float] | None:
        """The client's market-category counters (requests, 429s, pause seconds...), when it keeps them."""
        stats = getattr(self._client, "stats", None)
        market = stats.get("market") if isinstance(stats, dict) else None
        if market is None or not dataclasses.is_dataclass(market) or isinstance(market, type):
            return None
        return {k: v for k, v in dataclasses.asdict(market).items() if isinstance(v, int | float)}

    async def _fetch_opening_bars(
        self, session_date: date, reqs: list[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        """One batch under the deadline. The client keeps every result completed by then and leaves the
        outstanding requests out (reported "timeout" by the caller). A hard guard a little past the
        deadline stops a client that ignores it; that loses the batch, so it is only a backstop."""
        deadline = self._fetch_deadline_s
        loop = asyncio.get_running_loop()
        before = self._market_stats() or {}
        started = loop.time()
        results: dict[CandleRequest, list[Candle] | QuestradeApiError] = {}
        try:
            async with asyncio.timeout(deadline + min(DEADLINE_GUARD_S, deadline)):
                results = await self._client.candles_many(reqs, deadline_s=deadline)
        except TimeoutError:
            results = {}
        elapsed = loop.time() - started
        after = self._market_stats()
        completed = sum(1 for r in reqs if r in results)
        fields: dict[str, Any] = {
            "session_date": session_date.isoformat(),
            "symbols": len(reqs),
            "completed": completed,
            "outstanding": len(reqs) - completed,
            "deadline_s": deadline,
            "elapsed_s": round(elapsed, 3),
        }
        if after is not None:  # this batch's share of the client's counters
            fields["client_stats"] = {k: round(v - before.get(k, 0), 3) for k, v in after.items()}
        if completed < len(reqs):
            log.warning("market.opening_bars_timeout", **fields)
        else:
            log.info("market.opening_bars_fetched", **fields)
        return results

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        """Bars with start in [start, end), from the cache when it holds them all, else from Questrade.

        `start` and `end` must be timezone-aware (ValueError otherwise). A bar still forming
        (end > clock.now()) is returned but never cached. On a Questrade error the cached bars (possibly
        none) are returned and a warning logged, so a decision point never crashes (Review Focus 4).
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("candles() needs timezone-aware start and end")
        step = STEP[interval]
        cached: list[Candle] = []
        if interval != "OneDay":
            expected = int((end - start) / step)
            cached = await self._db(self._cached_candles, symbol_id, start, end, interval)
            if expected > 0 and len(cached) >= expected:
                return cached
        qids = await self._db(self._questrade_ids, [symbol_id])
        if symbol_id not in qids:
            return cached
        try:
            fetched = await self._client.candles(qids[symbol_id], start, end, interval)
        except QuestradeApiError as exc:
            log.warning(
                "market.candles_fetch_failed",
                symbol_id=symbol_id,
                interval=interval,
                status=exc.status,
                served_from_cache=len(cached),
            )
            return cached
        if interval != "OneDay":
            now = self._clock.now()
            complete = [c for c in fetched if max(c.end, c.start + step) <= now]
            if complete:
                await self._db(self._store_candles, symbol_id, interval, complete)
        return fetched

    def _cached_candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        step = STEP[interval]
        with self._factory() as s:
            rows = s.execute(
                select(m.IntradayCandle)
                .where(
                    m.IntradayCandle.symbol_id == symbol_id,
                    m.IntradayCandle.interval == INTERVAL_CODES[interval],
                    m.IntradayCandle.ts >= start,
                    m.IntradayCandle.ts < end,
                )
                .order_by(m.IntradayCandle.ts)
            ).scalars()
            return [_from_row(r, step) for r in rows]

    def _store_candles(self, symbol_id: int, interval: Interval, candles: list[Candle]) -> None:
        with session_scope(self._factory) as s:
            repo.upsert_intraday_candles(s, symbol_id, INTERVAL_CODES[interval], candles)

    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None:
        prev = self._cal.previous_session(session_date)
        with self._factory() as s:
            close = s.execute(
                select(m.DailyCandle.close).where(
                    m.DailyCandle.symbol_id == symbol_id, m.DailyCandle.date == prev
                )
            ).scalar_one_or_none()
        if close is not None:
            return close
        qids = self._questrade_ids([symbol_id])
        if symbol_id not in qids:
            return None
        start = datetime.combine(prev, time(0), tzinfo=ET)
        end = datetime.combine(session_date, time(0), tzinfo=ET)
        try:
            daily = await self._client.candles(qids[symbol_id], start, end, "OneDay")
        except QuestradeApiError:
            return None
        bars = [c for c in daily if et_date(c.start) == prev]
        if not bars:
            return None
        with session_scope(self._factory) as s:
            repo.upsert_daily_candles(s, symbol_id, bars[-1:])
        return bars[-1].close
