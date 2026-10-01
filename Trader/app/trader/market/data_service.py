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
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import QuestradeApiError, missing_reason, reason_key
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.db import models as m
from trader.db.session import session_scope
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.market.indicators import opening_bar, regular_hours
from trader.market.quote_bars import (
    CAPTURE_BAR,
    CAPTURE_OPEN,
    NO_OPEN_SNAPSHOT,
    NO_QUOTE,
    QUOTE_BAR_MAX_LAG,
    QUOTE_LATE_AFTER,
    VOLUME_BASIS_DELTA,
    OpeningBarSource,
    VolumeScale,
    median_factor,
    open_snapshot_volume,
    opening_delta,
    quote_bar,
    regular_factor,
    scale_volume,
    usable_factor,
)
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
# QUOTEBAR: the live engine's source of the 9:35 opening bar (trader.engine.orchestrator.build_engine). The
# service's default stays "candles" (replay, the archive, the API and every older test use candles).
LIVE_OPENING_BAR_SOURCE: OpeningBarSource = "quotes"
QUOTE_IDS_PER_CALL = 100  # Questrade's limit per quotes request
# The quotes pass takes ~6 paced requests (< 1 s); past this the rest fall back to candles, whose own
# FETCH_DEADLINE_S then still ends the scan within ~60 s.
QUOTES_DEADLINE_S = 15.0
MEASURE_DEADLINE_S = 180.0  # the post-close volume-factor measurement (a whole day of 5-minute candles each)
# FIX-DAY1: a timed capture (the open snapshot, the 09:35:00 bar) is ~6 paced requests (< 1 s); its budget is
# small, so a slow capture is cut short rather than read late.
CAPTURE_DEADLINE_S = 4.0
# The post-close measurement also reads the day's pre-market candles, from this long before the open
# (04:00 ET).
PREMARKET_SPAN = timedelta(hours=5, minutes=30)
# The 9:35 scan uses the factors of the latest earlier session that has any, at most this far back.
FACTOR_LOOKBACK = timedelta(days=10)
STALE_QUOTE = "stale_quote"  # measurement: the quote's last trade is not from the measured session
INCOMPLETE_DAY = "incomplete_day"  # measurement: the session's last 5-minute candle is not there (yet)
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
    # QUOTEBAR (quotes mode only, else None): where the bars came from ("quotes", "quotes+candles" when some
    # fell back to candles, "candles" outside the quote window), how many bars used each volume-factor source
    # (symbol / median / default), and how long after 09:35:00 the quotes were read.
    source: str | None = None
    factor_sources: dict[str, int] | None = None
    quote_lag_s: float | None = None
    # FIX-DAY1 (quotes mode): how many quote bars got their volume from the open-capture delta and how many
    # fell back to candles for want of a usable volume at the open; the stored 09:35:00 capture used
    # (started_at, ended_at, symbols), None when the scan read its own quotes.
    volume_basis: dict[str, int] | None = None
    capture: dict[str, Any] | None = None

    def detail(self) -> dict[str, Any]:
        """The job-detail keys: universe, bars, missing, missing_reasons (and, in quotes mode, source,
        volume_factors, quote_lag_s, volume_basis and capture)."""
        out: dict[str, Any] = {
            "universe": self.universe,
            "bars": self.bars,
            "missing": self.missing,
            "missing_reasons": dict(self.reasons),
        }
        if self.source is not None:
            out["source"] = self.source
        if self.factor_sources is not None:
            out["volume_factors"] = dict(self.factor_sources)
        if self.quote_lag_s is not None:
            out["quote_lag_s"] = self.quote_lag_s
        if self.volume_basis is not None:
            out["volume_basis"] = dict(self.volume_basis)
        if self.capture is not None:
            out["capture"] = dict(self.capture)
        return out

    @property
    def top_reason(self) -> str | None:
        if not self.reasons:
            return None
        return min(self.reasons.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    @property
    def unhealthy(self) -> bool:
        """At least half of the universe has no opening bar (or there is no bar at all)."""
        return self.universe > 0 and (self.bars == 0 or 2 * self.missing >= self.universe)


@dataclass(frozen=True)
class _QuoteBars:
    """What `_quote_opening_bars` found: the bars, the symbols to fall back to candles for, the volume-factor
    sources and volume bases counted, and the stored 09:35:00 capture used (None: the scan's own pass)."""

    bars: dict[int, Candle]
    fallback: list[int]
    factors: Counter[str]
    basis: Counter[str]
    capture: dict[str, Any] | None


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
        opening_bar_source: OpeningBarSource = "candles",
    ) -> None:
        """`opening_bar_source="quotes"` (QUOTEBAR, the live engine): `opening_bars` called within
        QUOTE_BAR_MAX_LAG after the bar's end builds the bars from one batched quotes pass (see
        trader.market.quote_bars); outside that window, and for symbols whose quotes failed, candles as
        before."""
        self._factory = factory
        self._clock = clock
        self._cal = calendar
        self._client = client
        self._fetch_deadline_s = fetch_deadline_s
        self._opening_bar_source: OpeningBarSource = opening_bar_source
        # symbols.id -> questrade_id for quotes(), kept for the process lifetime; an entry is dropped when
        # Questrade returns no quote for it, so a re-mapped symbol is re-read from the database.
        self._quote_qids: dict[int, int] = {}
        self._opening_scan: OpeningScan | None = None

    @property
    def opening_bar_source(self) -> OpeningBarSource:
        return self._opening_bar_source

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
        end = open_ + OPENING_BAR
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
        quotes_mode = self._opening_bar_source == "quotes"
        sources: dict[int, str] = dict.fromkeys(bars, "candles") if quotes_mode else {}
        started = self._clock.now()
        use_quotes = quotes_mode and end <= started < end + QUOTE_BAR_MAX_LAG
        candle_ids = [sid for sid in need if sid in qids]
        factor_counts: Counter[str] | None = None
        basis_counts: Counter[str] | None = None
        capture: dict[str, Any] | None = None
        lag: float | None = None
        fell_back = False
        if use_quotes:
            lag = round((started - end).total_seconds(), 3)
            factor_counts, basis_counts = Counter(), Counter()
            if candle_ids:
                got = await self._quote_opening_bars(
                    session_date, {sid: qids[sid] for sid in candle_ids}, open_, end, started, missing
                )
                bars.update(got.bars)
                sources.update(dict.fromkeys(got.bars, "quotes"))
                candle_ids, factor_counts, basis_counts = got.fallback, got.factors, got.basis
                capture = got.capture
                if capture is not None:  # the bars were read at the capture, not now
                    lag = capture["quote_lag_s"]
                fell_back = bool(candle_ids)
        reqs = {sid: CandleRequest(qids[sid], open_, end, "FiveMinutes") for sid in candle_ids}
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
        source: str | None = None
        if quotes_mode:
            sources.update(dict.fromkeys(fetched, "candles"))
            source = ("quotes+candles" if fell_back else "quotes") if use_quotes else "candles"
        reasons = Counter(reason_key(r) for r in missing.values())
        self._opening_scan = OpeningScan(
            session_date,
            len(ids),
            len(bars),
            len(missing),
            dict(reasons),
            source=source,
            factor_sources=dict(factor_counts) if factor_counts is not None else None,
            quote_lag_s=lag,
            volume_basis=dict(basis_counts) if basis_counts is not None else None,
            capture=capture,
        )
        return OpeningBars(bars, missing, sources)

    async def _quote_pass(
        self, items: Sequence[tuple[int, int]], deadline_s: float
    ) -> tuple[dict[int, QtQuote], dict[int, str]]:
        """ONE batched quotes pass over (symbols.id, questrade_id) pairs, at most QUOTE_IDS_PER_CALL ids per
        request, through the client (its pacing, retries and 401 token refresh). Returns the quotes by
        symbols.id and, for the symbols of a failed request, the missing reason. FIX-401 fail fast: after a
        request answered HTTP 401 (already after the client's forced refresh), or once the deadline passed,
        the remaining requests are not sent; their symbols get that reason too."""
        loop = asyncio.get_running_loop()
        until = loop.time() + deadline_s
        out: dict[int, QtQuote] = {}
        errors: dict[int, str] = {}
        fatal: str | None = None
        requests = 0
        for i in range(0, len(items), QUOTE_IDS_PER_CALL):
            chunk = items[i : i + QUOTE_IDS_PER_CALL]
            chunk_ids = [sid for sid, _ in chunk]
            remaining = until - loop.time()
            if fatal is None and remaining <= 0:
                fatal = "timeout"
            if fatal is not None:
                errors.update(dict.fromkeys(chunk_ids, fatal))
                continue
            requests += 1
            try:
                async with asyncio.timeout(remaining):
                    got = await self._client.quotes([qid for _, qid in chunk])
            except TimeoutError:
                fatal = "timeout"
                errors.update(dict.fromkeys(chunk_ids, fatal))
                continue
            except QuestradeApiError as exc:
                reason = missing_reason(exc)
                errors.update(dict.fromkeys(chunk_ids, reason))
                if exc.status == 401:
                    fatal = reason
                continue
            except Exception as exc:  # noqa: BLE001 - one failed request never ends the scan
                reason = missing_reason(QuestradeApiError(0, type(exc).__name__))
                errors.update(dict.fromkeys(chunk_ids, reason))
                continue
            back = {qid: sid for sid, qid in chunk}
            for q in got:
                sid = back.get(q.symbol_id)
                if sid is not None:
                    out[sid] = dataclasses.replace(q, symbol_id=sid)
        if fatal is not None:
            log.error(
                "market.quotes_fail_fast",
                symbols=len(items),
                requests=requests,
                failed=len(errors),
                reason=reason_key(fatal),
            )
        return out, errors

    async def _quote_opening_bars(
        self,
        session_date: date,
        qids: dict[int, int],
        open_: datetime,
        end: datetime,
        now: datetime,
        missing: dict[int, str],
    ) -> "_QuoteBars":
        """QUOTEBAR + FIX-DAY1: the opening bars from the stored 09:35:00 capture (`bar`), and from one quotes
        pass for the symbols it lacks; volume = (quote volume - the open capture's volume) x the candle-scale
        factor. Symbols whose quote request failed, or without a usable volume at the open, fall back to
        candles (never the raw day volume: it includes pre-market trades). Symbols with a quote but no usable
        bar (including `quote_late`) get their reason in `missing`. The bars are stored in
        `opening_bar_quotes` (never as candles)."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        stored, cap_start, cap_end = self._stored_captures(session_date, CAPTURE_BAR, list(qids))
        capture: dict[str, Any] | None = None
        if stored and cap_start is not None and end <= cap_start < end + QUOTE_BAR_MAX_LAG:
            capture = {
                "started_at": cap_start.isoformat(),
                "ended_at": cap_end.isoformat() if cap_end is not None else None,
                "symbols": len(stored),
                "quote_lag_s": round((cap_start - end).total_seconds(), 3),
            }
        else:
            stored, cap_start, cap_end = {}, None, None
        rest = [(sid, qid) for sid, qid in qids.items() if sid not in stored]
        quotes: dict[int, QtQuote] = dict(stored)
        errors: dict[int, str] = {}
        if rest:
            fresh, errors = await self._quote_pass(rest, min(QUOTES_DEADLINE_S, self._fetch_deadline_s))
            quotes.update(fresh)
        opens, _, _ = self._stored_captures(session_date, CAPTURE_OPEN, list(qids))
        scale = self._volume_scale(session_date)
        bars: dict[int, Candle] = {}
        counts: Counter[str] = Counter()
        basis: Counter[str] = Counter()
        fallback: list[int] = []
        rows: list[dict[str, Any]] = []
        for sid in qids:
            if sid in errors:
                fallback.append(sid)
                continue
            q = quotes.get(sid)
            if q is None:
                missing[sid] = NO_QUOTE
                continue
            built = quote_bar(q, open_, end)
            if isinstance(built, str):
                missing[sid] = built
                continue
            open_volume = open_snapshot_volume(opens.get(sid), open_, session_date)
            delta = opening_delta(built.volume, open_volume)
            if delta is None:
                basis[NO_OPEN_SNAPSHOT] += 1
                fallback.append(sid)
                continue
            factor, factor_source = scale.factor_for(sid)
            volume = scale_volume(delta, factor)
            bars[sid] = dataclasses.replace(built, volume=volume, vwap=None)
            counts[factor_source] += 1
            basis[VOLUME_BASIS_DELTA] += 1
            from_capture = sid in stored
            rows.append(
                {
                    "session_date": session_date,
                    "symbol_id": sid,
                    "captured_at": cap_start if from_capture else now,
                    "quote_time": q.last_trade_time,
                    "open": built.open,
                    "high": built.high,
                    "low": built.low,
                    "close": built.close,
                    "quote_volume": built.volume,
                    "volume": volume,
                    "vol_factor": factor,
                    "factor_source": factor_source,
                    "open_volume": open_volume,
                    "volume_basis": VOLUME_BASIS_DELTA,
                    "capture_started_at": cap_start if from_capture else None,
                    "capture_ended_at": cap_end if from_capture else None,
                }
            )
        if rows:
            try:
                self._store_quote_bars(rows)
            except Exception as exc:  # bookkeeping: the scan goes on with the bars it has
                log.error("market.quote_bars_not_stored", error=type(exc).__name__, rows=len(rows))
        log.info(
            "market.opening_bars_from_quotes",
            session_date=session_date.isoformat(),
            symbols=len(qids),
            bars=len(bars),
            failed=len(errors),
            from_capture=len(stored),
            volume_factors=dict(counts),
            volume_basis=dict(basis),
            factor_session=scale.measured_on.isoformat() if scale.measured_on else None,
            elapsed_s=round(loop.time() - started, 3),
        )
        return _QuoteBars(bars, fallback, counts, basis, capture)

    def _stored_captures(
        self, session_date: date, kind: str, symbol_ids: Sequence[int]
    ) -> tuple[dict[int, QtQuote], datetime | None, datetime | None]:
        """FIX-DAY1: the stored quotes of one timed capture by symbols.id, with the capture's start and end
        (the earliest start and latest end over the rows); empty when there is none or the read fails."""
        try:
            with self._factory() as s:
                rows = (
                    s.execute(
                        select(m.OpeningQuoteCapture).where(
                            m.OpeningQuoteCapture.session_date == session_date,
                            m.OpeningQuoteCapture.kind == kind,
                            m.OpeningQuoteCapture.symbol_id.in_(list(symbol_ids)),
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception as exc:  # the scan reads its own quotes instead
            log.error("market.captures_unreadable", kind=kind, error=type(exc).__name__)
            return {}, None, None
        if not rows:
            return {}, None, None
        out = {
            r.symbol_id: QtQuote(
                symbol_id=r.symbol_id,
                symbol="",
                bid=None,
                ask=None,
                last=r.last,
                last_regular=r.last_regular,
                volume=int(r.volume),
                last_trade_time=r.quote_time,
                delay=r.delay,
                is_halted=False,
                vwap=None,
                open=r.open,
                high=r.high,
                low=r.low,
                fetched_at=r.fetched_at,
            )
            for r in rows
        }
        return out, min(r.capture_started_at for r in rows), max(r.capture_ended_at for r in rows)

    async def capture_quotes(
        self, session_date: date, kind: str, symbol_ids: Sequence[int] | None = None
    ) -> dict[str, Any]:
        """FIX-DAY1: one timed quotes capture of the universe (default) under a small budget, stored in
        `opening_quote_captures` (kind `open`: the volume at the open, read just before 09:30:00; kind `bar`:
        the 09:30-09:35 bar, read from 09:35:00.0). Records the capture's start and end and, per symbol, the
        quote's last-trade time and when it arrived. A re-run replaces the rows. Returns the detail (never
        raises for missing data; a storage failure is logged)."""
        if kind not in (CAPTURE_OPEN, CAPTURE_BAR):
            raise ValueError(f"unknown capture kind {kind!r}")
        if symbol_ids is None:
            symbol_ids = [x.symbol_id for x in await self.universe(session_date)]
        ids = list(dict.fromkeys(symbol_ids))
        uncached = [sid for sid in ids if sid not in self._quote_qids]
        if uncached:  # the open capture warms this cache, so the 09:35:00 one starts with no database read
            self._quote_qids.update(await self._db(self._questrade_ids, uncached))
        items = [(sid, self._quote_qids[sid]) for sid in ids if sid in self._quote_qids]
        started = self._clock.now()
        quotes, errors = await self._quote_pass(items, CAPTURE_DEADLINE_S)
        ended = self._clock.now()
        rows = [
            {
                "session_date": session_date,
                "symbol_id": sid,
                "kind": kind,
                "capture_started_at": started,
                "capture_ended_at": ended,
                "fetched_at": q.fetched_at or ended,
                "quote_time": q.last_trade_time,
                "open": q.open,
                "high": q.high,
                "low": q.low,
                "last": q.last,
                "last_regular": q.last_regular,
                "volume": max(int(q.volume), 0),
                "delay": q.delay,
            }
            for sid, q in quotes.items()
        ]
        open_ = self._cal.session_open(session_date)
        target = open_ + OPENING_BAR if kind == CAPTURE_BAR else open_
        detail: dict[str, Any] = {
            "session_date": session_date.isoformat(),
            "kind": kind,
            "symbols": len(ids),
            "quoted": len(rows),
            "failed": len(errors),
            "missing_reasons": dict(Counter(reason_key(r) for r in errors.values())),
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "elapsed_s": round((ended - started).total_seconds(), 3),
            "offset_s": round((started - target).total_seconds(), 3),  # start relative to 09:35:00 / 09:30:00
        }
        if kind == CAPTURE_BAR:
            late_at = target + QUOTE_LATE_AFTER
            detail["late"] = sum(
                1 for q in quotes.values() if q.last_trade_time is not None and q.last_trade_time > late_at
            )
        else:
            detail["after_open"] = sum(
                1 for q in quotes.values() if q.last_trade_time is not None and q.last_trade_time >= open_
            )
        if rows:
            try:
                await self._db(self._store_captures, rows)
            except Exception as exc:
                log.error("market.capture_not_stored", kind=kind, error=type(exc).__name__, rows=len(rows))
                detail["stored"] = False
        log.info("market.quotes_captured", **detail)
        return detail

    def _store_captures(self, rows: list[dict[str, Any]]) -> None:
        stmt = pg_insert(m.OpeningQuoteCapture).values(rows)
        keep = ("session_date", "symbol_id", "kind")
        set_ = {k: stmt.excluded[k] for k in rows[0] if k not in keep}
        with session_scope(self._factory) as s:
            s.execute(stmt.on_conflict_do_update(index_elements=list(keep), set_=set_))

    def _store_quote_bars(self, rows: list[dict[str, Any]]) -> None:
        """Upsert by (session_date, symbol_id). A re-read replaces the bar and clears an earlier shadow
        check."""
        stmt = pg_insert(m.OpeningBarQuote).values(rows)
        keep = ("session_date", "symbol_id")
        set_: dict[str, Any] = {k: stmt.excluded[k] for k in rows[0] if k not in keep}
        for col in (
            "checked_at",
            "check_status",
            "official_open",
            "official_high",
            "official_low",
            "official_close",
            "official_volume",
            "decision_differs",
        ):
            set_[col] = None
        with session_scope(self._factory) as s:
            s.execute(stmt.on_conflict_do_update(index_elements=list(keep), set_=set_))

    def _volume_scale(self, session_date: date) -> VolumeScale:
        """The volume factors of the latest session before `session_date` (within FACTOR_LOOKBACK) that has
        any measured factor; empty (the default factor) when there is none or the read fails."""
        try:
            with self._factory() as s:
                latest = s.execute(
                    select(func.max(m.QuoteVolumeScale.session_date)).where(
                        m.QuoteVolumeScale.session_date < session_date,
                        m.QuoteVolumeScale.session_date >= session_date - FACTOR_LOOKBACK,
                        m.QuoteVolumeScale.factor.is_not(None),
                    )
                ).scalar_one_or_none()
                if latest is None:
                    return VolumeScale({}, None)
                rows = s.execute(
                    select(m.QuoteVolumeScale.symbol_id, m.QuoteVolumeScale.factor).where(
                        m.QuoteVolumeScale.session_date == latest, m.QuoteVolumeScale.factor.is_not(None)
                    )
                ).all()
        except Exception as exc:  # the default factor is better than no scan
            log.error("market.volume_scale_unreadable", error=type(exc).__name__)
            return VolumeScale({}, None)
        return VolumeScale({int(sid): f for sid, f in rows if f is not None}, latest)

    async def measure_volume_scale(
        self, session_date: date, symbol_ids: Sequence[int] | None = None
    ) -> dict[str, Any]:
        """QUOTEBAR (after the close): for each symbol (default: the session's universe), the day's quote
        volume (one batched quotes pass) and the sum of its regular-session 5-minute candles; their ratio on
        regular-session volume (FIX-DAY1, `regular_factor`: the pre-market volume from the open capture, else
        from the day's pre-market candles, is taken out) is stored in `quote_volume_scale`, for the next
        session's 9:35 bars. A quote whose
        last trade is not from `session_date` is not measured (stale_quote), nor a day whose last 5-minute
        candle is not there yet (incomplete_day). Returns the job-detail counts; missing data is counted,
        never raised."""
        if symbol_ids is None:
            symbol_ids = [x.symbol_id for x in await self.universe(session_date)]
        ids = list(dict.fromkeys(symbol_ids))
        qids = self._questrade_ids(ids)
        reasons: Counter[str] = Counter()
        reasons["no_questrade_id"] += sum(1 for sid in ids if sid not in qids)
        quotes, errors = await self._quote_pass(list(qids.items()), MEASURE_DEADLINE_S)
        reasons.update(reason_key(r) for r in errors.values())
        todays: dict[int, int] = {}
        for sid in qids:
            if sid in errors:
                continue
            q = quotes.get(sid)
            if q is None:
                reasons[NO_QUOTE] += 1
            elif q.last_trade_time is None or et_date(q.last_trade_time) != session_date or q.volume <= 0:
                reasons[STALE_QUOTE] += 1
            else:
                todays[sid] = q.volume
        open_, close = self._cal.session_open(session_date), self._cal.session_close(session_date)
        # FIX-DAY1: the quote's day volume includes pre-market trades; take them out of the ratio, from the
        # open capture (quote scale) when there is one, else from that day's pre-market candles.
        opens, _, _ = self._stored_captures(session_date, CAPTURE_OPEN, list(todays))
        reqs = {sid: CandleRequest(qids[sid], open_ - PREMARKET_SPAN, close, "FiveMinutes") for sid in todays}
        results: dict[CandleRequest, list[Candle] | QuestradeApiError] = {}
        if reqs:
            results = await self._client.candles_many(list(reqs.values()), deadline_s=MEASURE_DEADLINE_S)
        now = self._clock.now()
        last_bar = close - OPENING_BAR
        rows: list[dict[str, Any]] = []
        factors: list[Decimal] = []
        for sid, req in reqs.items():
            result = results.get(req)
            candle_volume: int | None = None
            factor: Decimal | None = None
            premarket: int | None = None
            premarket_source: str | None = None
            if result is None:
                reasons["timeout"] += 1
            elif isinstance(result, QuestradeApiError):
                reasons[reason_key(missing_reason(result))] += 1
            else:
                rth = regular_hours(result, self._cal, session_date)
                if not any(c.start == last_bar for c in rth):
                    reasons[INCOMPLETE_DAY] += 1
                else:
                    candle_volume = sum(c.volume for c in rth)
                    snapshot = open_snapshot_volume(opens.get(sid), open_, session_date)
                    if snapshot is not None:
                        premarket, premarket_source = snapshot, "snapshot"
                        factor = regular_factor(candle_volume, todays[sid], premarket_quote=snapshot)
                    else:
                        premarket = sum(c.volume for c in result if open_ - PREMARKET_SPAN <= c.start < open_)
                        premarket_source = "candles"
                        factor = regular_factor(candle_volume, todays[sid], premarket_candle=premarket)
                    if factor is None:
                        reasons["no_volume"] += 1
                    elif not usable_factor(factor):
                        # Out of range (e.g. a near-zero quote volume): not trusted and not stored, so one
                        # absurd value can't overflow NUMERIC(10,6) and lose the whole session's factors.
                        reasons["factor_out_of_range"] += 1
                        factor = None
                    else:
                        factors.append(factor)
            rows.append(
                {
                    "session_date": session_date,
                    "symbol_id": sid,
                    "quote_volume": todays[sid],
                    "candle_volume": candle_volume,
                    "factor": factor,
                    "recorded_at": now,
                    "premarket_volume": premarket,
                    "premarket_source": premarket_source,
                }
            )
        if rows:
            stmt = pg_insert(m.QuoteVolumeScale).values(rows)
            updated = (
                "quote_volume",
                "candle_volume",
                "factor",
                "recorded_at",
                "premarket_volume",
                "premarket_source",
            )
            with session_scope(self._factory) as s:
                s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["session_date", "symbol_id"],
                        set_={k: stmt.excluded[k] for k in updated},
                    )
                )
        median = median_factor(f for f in factors if usable_factor(f))
        detail: dict[str, Any] = {
            "session_date": session_date.isoformat(),
            "symbols": len(ids),
            "quoted": len(todays),
            "measured": len(factors),
            "median": str(median) if median is not None else None,
            "missing_reasons": dict(sorted((k, v) for k, v in reasons.items() if v)),
        }
        log.info("market.volume_scale_measured", **detail)
        return detail

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
