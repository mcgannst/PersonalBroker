"""Market data for strategies and jobs (master plan §7.1): the DB cache first, Questrade second.

Every symbol_id here is trader.symbols.id. Questrade IDs stay at the client boundary: quotes() rewrites
QtQuote.symbol_id to the database ID. Missing data is reported per symbol, never raised (Review Focus 4).
"""

import dataclasses
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import QuestradeApiError
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
STEP: dict[Interval, timedelta] = {
    "OneMinute": timedelta(minutes=1),
    "FiveMinutes": timedelta(minutes=5),
    "FifteenMinutes": timedelta(minutes=15),
    "OneHour": timedelta(hours=1),
    "OneDay": timedelta(days=1),
}


class QuoteClient(Protocol):
    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]: ...
    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]: ...
    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]: ...


def _from_row(row: m.IntradayCandle, step: timedelta) -> Candle:
    return Candle(row.ts, row.ts + step, row.open, row.high, row.low, row.close, row.volume, row.vwap)


class MarketDataService:
    def __init__(
        self, factory: sessionmaker[Session], clock: Clock, calendar: SessionCalendar, client: QuoteClient
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cal = calendar
        self._client = client

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
            return UniverseStatus(source, None, False, None)
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
        qids = self._questrade_ids(symbol_ids)
        if not qids:
            return {}
        back = {qid: sid for sid, qid in qids.items()}
        out: dict[int, QtQuote] = {}
        for q in await self._client.quotes(list(qids.values())):
            if q.symbol_id in back:
                sid = back[q.symbol_id]
                out[sid] = dataclasses.replace(q, symbol_id=sid)
        return out

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        if symbol_ids is None:
            symbol_ids = [x.symbol_id for x in await self.universe(session_date)]
        ids = list(dict.fromkeys(symbol_ids))
        open_ = self._cal.session_open(session_date)
        with self._factory() as s:
            cached = s.execute(
                select(m.IntradayCandle).where(
                    m.IntradayCandle.interval == "5m",
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
        results = await self._client.candles_many(list(reqs.values())) if reqs else {}
        now = self._clock.now()
        fetched: dict[int, Candle] = {}
        for sid, req in reqs.items():
            result = results[req]
            if isinstance(result, QuestradeApiError):
                missing[sid] = f"questrade_error: HTTP {result.status}"
                continue
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
                    repo.upsert_intraday_candles(s, sid, "5m", [found])
        bars.update(fetched)
        return OpeningBars(bars, missing)

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        qids = self._questrade_ids([symbol_id])
        step = STEP[interval]
        if interval != "OneDay":
            code = INTERVAL_CODES[interval]
            expected = int((end - start) / step)
            with self._factory() as s:
                rows = s.execute(
                    select(m.IntradayCandle)
                    .where(
                        m.IntradayCandle.symbol_id == symbol_id,
                        m.IntradayCandle.interval == code,
                        m.IntradayCandle.ts >= start,
                        m.IntradayCandle.ts < end,
                    )
                    .order_by(m.IntradayCandle.ts)
                ).scalars()
                cached = [_from_row(r, step) for r in rows]
            if expected > 0 and len(cached) >= expected:
                return cached
        if symbol_id not in qids:
            return []
        fetched = await self._client.candles(qids[symbol_id], start, end, interval)
        if interval != "OneDay" and fetched:
            with session_scope(self._factory) as s:
                repo.upsert_intraday_candles(s, symbol_id, INTERVAL_CODES[interval], fetched)
        return fetched

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
