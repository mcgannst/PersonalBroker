"""The replay data source (SPEC §8; P5-T5): everything a strategy reads during a replay, from the candle
archive and caches first and Questrade second (only within `replay.questrade_window_days` of the run's
creation date, never offline), with no lookahead (no bar ending after the replay clock) and no writes to any
table.

Implements `trader.replay.types.ReplayMarket`. Fetched Questrade data is held in memory for the run only:
each session's opening 5-minute bar, daily bars, and the current session's 1-minute bars.

Sources, in order:
- opening 5-minute bars: `candle_archive` (5m) -> `intraday_candles` (5m) -> Questrade `FiveMinutes`, per
  symbol and chunk of about `CHUNK_SESSIONS` sessions (fix round 1: never the whole range at once), split
  into windows of at most `MAX_CANDLES_PER_REQUEST` intervals; only each session's opening bar is kept.
- 1-minute bars (fills, synthetic quotes): `candle_archive` (1m) -> `intraday_candles` (1m) -> one Questrade
  `OneMinute` request per symbol and session; held for the current session only.
- daily bars (prior close, ATR): `daily_candles` -> Questrade `OneDay`, per symbol and chunk of about
  `CHUNK_DAYS` calendar days.
- universe: that session's `universe_snapshots`; with none, the names of the newest stored universe on or
  before the run's creation date, every member `source = "biased"` (and the day in `biased_days`), with
  `price`, `avg_volume` and `atr14` recomputed from the daily bars before that session with the nightly
  job's formulas (fix round 1; the snapshot's own numbers are from a later date).
- opening-bar stats: that session's `open_bar_stats`; a member without a row gets them computed in memory
  with the nightly job's formulas.

Memory: bars are loaded a chunk at a time, opening and daily bars older than the current day's look-back
are dropped in `prepare_day`, and the previous day's 1-minute bars are released, so a 130-session replay of
the whole universe stays small (`held_counts`). A Questrade error is a counted "missing" and one `warning`
event per day and kind with the replay's `run_id`, never an exception to the strategy. There is no wall-clock
fetch deadline: a replay waits for every response, so its result never depends on timing. Fetches are
assembled by symbol id (sorted), never in completion order.

Cron quiet windows (fix round 1): given `quiet_sleep` (the runner passes it in `full` mode), a fetch never
starts within `QUIET_MARGIN` of a `QUIET_TIMES` line (the container's crontab, kept in sync by a test); it
waits for the window to pass instead, so a replay never competes with a scheduled job for Questrade.
"""

from bisect import bisect_right
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import MAX_CANDLES_PER_REQUEST, QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.broker.fill_model import FillParams
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.market.data_service import STEP, MarketDataService, QuoteClient
from trader.market.indicators import atr, average_volume, regular_hours
from trader.market.types import (
    INTERVAL_CODES,
    Candle,
    Interval,
    OpenBarStats,
    OpeningBars,
    UniverseMember,
    UniverseStatus,
)
from trader.replay.candle_fill_model import CandleFillModel
from trader.settings_store import OVERLAY_SYMBOL

BIASED_SOURCE = "biased"
EVENT_SOURCE = "replay.data"
ONE_MINUTE = timedelta(minutes=1)
FIVE_MINUTES = timedelta(minutes=5)
ONE_DAY = timedelta(days=1)
# The nightly job's formulas (`trader.jobs.nightly`, which is not imported: it pulls in the FinViz adapter).
# A test keeps them equal to the nightly job's.
DAILY_LOOKBACK = timedelta(days=30)
MIN_OPENING_BARS = 10
ATR_PERIOD = 14
AVG_VOLUME_DAYS = 14
# Daily bars kept before the current day: the nightly ATR window (30 calendar days before the previous
# session) plus a long holiday weekend.
DAILY_MARGIN = DAILY_LOOKBACK + timedelta(days=10)
# Loading granularity (fix round 1): opening bars ~20 sessions at a time, daily bars ~20 sessions' worth of
# calendar days, per symbol.
CHUNK_SESSIONS = 20
CHUNK_DAYS = timedelta(days=28)
MINUTE_CODE = INTERVAL_CODES["OneMinute"]
FIVE_CODE = INTERVAL_CODES["FiveMinutes"]
NO_ARCHIVED_BAR = "no_archived_bar"

# The container's cron lines (`Trader/docker/crontab`, America/New_York), as (ET time, Python weekdays with
# Monday = 0). `tests/replay/test_data.py` checks every crontab line is listed here. The Saturday 09:00 line
# is the weekly report (P5-T17).
_WEEKDAYS = frozenset({0, 1, 2, 3, 4})
QUIET_TIMES: tuple[tuple[time, frozenset[int]], ...] = (
    (time(2, 0), frozenset(range(7))),  # token-refresh
    (time(20, 0), frozenset({6, 0, 1, 2, 3})),  # nightly, Sunday to Thursday
    (time(8, 0), _WEEKDAYS),  # premarket
    (time(9, 20), _WEEKDAYS),  # preopen
    (time(9, 36), _WEEKDAYS),  # event orb_open
    (time(11, 30), _WEEKDAYS),  # checkin
    (time(12, 32), _WEEKDAYS),  # event --due
    (time(12, 55), _WEEKDAYS),  # event flatten
    (time(12, 58), _WEEKDAYS),  # event flatten
    (time(13, 30), _WEEKDAYS),  # checkin
    (time(15, 32), _WEEKDAYS),  # event --due
    (time(15, 55), _WEEKDAYS),  # event flatten
    (time(15, 58), _WEEKDAYS),  # event flatten
    (time(16, 15), _WEEKDAYS),  # postclose
    (time(9, 0), frozenset({5})),  # weekly report, Saturday
)
QUIET_MARGIN = timedelta(minutes=10)


def quiet_until(at: datetime) -> datetime | None:
    """The end of the cron quiet window `at` falls in (`QUIET_MARGIN` either side of a `QUIET_TIMES` line,
    start included, end excluded), or None outside every window."""
    local = at.astimezone(ET)
    end: datetime | None = None
    for offset in (-1, 0, 1):
        day = local.date() + timedelta(days=offset)
        for t, weekdays in QUIET_TIMES:
            if day.weekday() not in weekdays:
                continue
            line = datetime.combine(day, t, tzinfo=ET)
            if line - QUIET_MARGIN <= local < line + QUIET_MARGIN:
                stop = line + QUIET_MARGIN
                end = stop if end is None else max(end, stop)
    return end


class _NoQuestrade:
    """The client given to the read-only `MarketDataService` helper: its cache reads never fetch."""

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        raise RuntimeError("replay data never fetches through MarketDataService")

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        raise RuntimeError("replay data never fetches through MarketDataService")

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        raise RuntimeError("replay data never fetches through MarketDataService")


def _row_candle(start: datetime, row: Any, step: timedelta) -> Candle:
    return Candle(start, start + step, row.open, row.high, row.low, row.close, row.volume, row.vwap)


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=ET)


def _windows(start: datetime, end: datetime, step: timedelta) -> list[tuple[datetime, datetime]]:
    """[start, end) split into consecutive windows of at most MAX_CANDLES_PER_REQUEST intervals."""
    width = step * MAX_CANDLES_PER_REQUEST
    out: list[tuple[datetime, datetime]] = []
    t = start
    while t < end:
        out.append((t, min(t + width, end)))
        t += width
    return out


def _error_reason(result: list[Candle] | QuestradeApiError) -> str | None:
    return f"questrade_error: HTTP {result.status}" if isinstance(result, QuestradeApiError) else None


def _group(ranges: Mapping[int, tuple[Any, Any]]) -> dict[tuple[Any, Any], list[int]]:
    """Symbols by the range they need, so each distinct range is one set of database reads."""
    out: dict[tuple[Any, Any], list[int]] = {}
    for sid in sorted(ranges):
        out.setdefault(ranges[sid], []).append(sid)
    return out


class ReplayData:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        wall: Clock,
        calendar: SessionCalendar,
        client: QuoteClient | None,
        *,
        run_id: int,
        date_from: date,
        date_to: date,
        half_spread_bps: Decimal,
        questrade_window_days: int,
        lookback_sessions: int,
        created_at: datetime | None = None,
        quiet_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._wall = wall
        self._calendar = calendar
        self._client = client
        self._quiet_sleep = quiet_sleep
        self.run_id = run_id
        self.date_from = date_from
        self.date_to = date_to
        self.half_spread_bps = half_spread_bps
        self.questrade_window_days = questrade_window_days
        self.lookback_sessions = lookback_sessions
        # The one definition of the half spread (the candle fill model's); FillParams are not used by it.
        self._spread = CandleFillModel(FillParams(), half_spread_bps)
        # Cache-only reads shared with the live service (universe, its status, stored stats, symbol ids).
        self._db = MarketDataService(factory, clock, calendar, _NoQuestrade())

        # Measured from the run's creation (fix round 1), else the wall clock now; decided once, so a run
        # that crosses midnight, or starts long after it was queued, still reads the same data.
        self._wall_date = et_date(created_at if created_at is not None else wall.now())
        self._questrade_from = self._wall_date - timedelta(days=questrade_window_days)
        # Every session whose opening bar the run can need: the look-back of the first day, then the range.
        span = calendar.sessions_before(date_from, lookback_sessions) if lookback_sessions > 0 else []
        d = date_from if calendar.is_session(date_from) else calendar.next_session(date_from)
        while d <= date_to:
            span.append(d)
            d = calendar.next_session(d)
        self._span: list[date] = span
        self._span_index: dict[date, int] = {s: i for i, s in enumerate(span)}
        self._opens: dict[datetime, date] = {calendar.session_open(s): s for s in span}
        self._daily_from = date_from - DAILY_MARGIN

        self._counts: dict[str, int] = {
            "missing_opening_bars": 0,
            "missing_minute_bars": 0,
            "questrade_requests": 0,
        }
        self._biased: set[date] = set()
        self._biased_names: list[UniverseMember] | None = None
        self._warned: set[tuple[date, str]] = set()
        self._tickers: dict[int, str] = {}

        # The prepared day (universe and stats are cached for it only).
        self._day: date | None = None
        self._day_universe: list[UniverseMember] = []
        self._day_stats: dict[int, OpenBarStats] | None = None
        # symbol -> session -> opening bar; reasons for the missing ones that are not "no_archived_bar";
        # symbol -> the last span index loaded.
        self._opening: dict[int, dict[date, Candle]] = {}
        self._opening_reason: dict[int, dict[date, str]] = {}
        self._opening_to: dict[int, int] = {}
        # symbol -> date -> daily bar; symbol -> the last date loaded.
        self._daily: dict[int, dict[date, Candle]] = {}
        self._daily_to: dict[int, date] = {}
        # (symbol, session) -> ATR of the daily bars before that session, for the prepared day only.
        self._atr: dict[tuple[int, date], Decimal | None] = {}
        # 1-minute bars of one session only.
        self._minute_day: date | None = None
        self._minute: dict[int, list[Candle]] = {}

    # --- helpers --------------------------------------------------------------------------------------------
    def _questrade_allowed(self, session: date) -> bool:
        return self._client is not None and session >= self._questrade_from

    def _current_day(self) -> date:
        return self._day if self._day is not None else self.date_from

    def _questrade_ids(self, symbol_ids: Sequence[int]) -> dict[int, int]:
        if not symbol_ids:
            return {}
        with self._factory() as s:
            rows = s.execute(
                select(m.Symbol.id, m.Symbol.questrade_id).where(
                    m.Symbol.id.in_(list(symbol_ids)), m.Symbol.questrade_id.is_not(None)
                )
            ).all()
        return {sid: int(qid) for sid, qid in rows}

    def _ticker_names(self, symbol_ids: Sequence[int]) -> dict[int, str]:
        need = [sid for sid in symbol_ids if sid not in self._tickers]
        if need:
            with self._factory() as s:
                rows = s.execute(select(m.Symbol.id, m.Symbol.ticker).where(m.Symbol.id.in_(need))).all()
            self._tickers.update({sid: ticker for sid, ticker in rows})
        return {sid: self._tickers[sid] for sid in symbol_ids if sid in self._tickers}

    async def _wait_out_cron(self) -> None:
        """With `quiet_sleep`, wait until the wall clock is outside every cron quiet window."""
        if self._quiet_sleep is None:
            return
        while (until := quiet_until(self._wall.now())) is not None:
            await self._quiet_sleep(max((until - self._wall.now()).total_seconds(), 1.0))

    async def _fetch(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        """One batch through the client; any failure becomes that request's error, never an exception."""
        assert self._client is not None
        await self._wait_out_cron()
        self._counts["questrade_requests"] += len(reqs)
        try:
            return await self._client.candles_many(list(reqs))
        except QuestradeApiError as exc:
            return {r: exc for r in reqs}
        except Exception as exc:  # a transport or parse failure of the whole batch
            err = QuestradeApiError(0, type(exc).__name__)
            return {r: err for r in reqs}

    def _warn(self, day: date, kind: str, errors: Mapping[int, str]) -> None:
        """At most one `warning` event per day and kind, stamped with the replay clock and run id."""
        if not errors or (day, kind) in self._warned:
            return
        self._warned.add((day, kind))
        with session_scope(self._factory) as s:
            log_event(
                s,
                self._clock,
                "warning",
                EVENT_SOURCE,
                f"replay: Questrade {kind} failed for {len(errors)} symbols",
                {
                    "session_date": day.isoformat(),
                    "kind": kind,
                    "errors": {str(sid): errors[sid] for sid in sorted(errors)},
                },
                run_id=self.run_id,
            )

    # --- opening bars ---------------------------------------------------------------------------------------
    async def _ensure_opening(self, symbol_ids: Sequence[int], session_date: date) -> None:
        """Load the opening bars `session_date` needs (its look-back and itself) and those of the next
        CHUNK_SESSIONS - 1 sessions, per symbol, so a symbol is fetched about once per chunk."""
        idx = self._span_index.get(session_date)
        if idx is None:  # not a session of the span: nothing to load (reported as no_archived_bar)
            return
        low = max(0, idx - self.lookback_sessions)
        last = len(self._span) - 1
        ranges: dict[int, tuple[int, int]] = {}
        for sid in sorted(set(symbol_ids)):
            loaded = self._opening_to.get(sid)
            if loaded is not None and loaded >= idx:
                continue
            lo = low if loaded is None else max(low, loaded + 1)
            ranges[sid] = (lo, min(last, idx + CHUNK_SESSIONS - 1))
        errors: dict[int, str] = {}
        for (lo, hi), ids in _group(ranges).items():
            await self._load_opening(ids, self._span[lo : hi + 1], errors)
            for sid in ids:
                self._opening_to[sid] = hi
        self._warn(self._current_day(), "opening_bars", errors)

    async def _load_opening(self, ids: list[int], sessions: list[date], errors: dict[int, str]) -> None:
        opens = [self._calendar.session_open(d) for d in sessions]
        found: dict[int, dict[date, Candle]] = {sid: {} for sid in ids}
        with self._factory() as s:
            for arch in s.execute(
                select(m.CandleArchive).where(
                    m.CandleArchive.symbol_id.in_(ids),
                    m.CandleArchive.interval == FIVE_CODE,
                    m.CandleArchive.start_ts.in_(opens),
                )
            ).scalars():
                found[arch.symbol_id][self._opens[arch.start_ts]] = _row_candle(
                    arch.start_ts, arch, FIVE_MINUTES
                )
            for row in s.execute(
                select(m.IntradayCandle).where(
                    m.IntradayCandle.symbol_id.in_(ids),
                    m.IntradayCandle.interval == FIVE_CODE,
                    m.IntradayCandle.ts.in_(opens),
                )
            ).scalars():
                found[row.symbol_id].setdefault(self._opens[row.ts], _row_candle(row.ts, row, FIVE_MINUTES))
        reasons: dict[int, dict[date, str]] = {}
        wanted = {
            sid: [d for d in sessions if d not in found[sid] and self._questrade_allowed(d)] for sid in ids
        }
        qids = self._questrade_ids([sid for sid in ids if wanted[sid]])
        for sid in ids:
            need = wanted[sid]
            if not need:
                continue
            if sid not in qids:
                reasons[sid] = {d: "no_questrade_id" for d in need}
                continue
            start, end = self._calendar.session_open(need[0]), self._calendar.session_close(need[-1])
            reqs = [
                CandleRequest(qids[sid], a, b, "FiveMinutes") for a, b in _windows(start, end, FIVE_MINUTES)
            ]
            results = await self._fetch(reqs)
            need_set = set(need)
            for req in reqs:
                result = results.get(req, QuestradeApiError(0, "no result"))
                why = _error_reason(result)
                if why is not None:
                    errors[sid] = why
                    for d in need:
                        if req.start <= self._calendar.session_open(d) < req.end:
                            reasons.setdefault(sid, {})[d] = why
                    continue
                assert isinstance(result, list)
                for bar in result:  # keep only each session's opening bar; the rest is dropped at once
                    session = self._opens.get(bar.start)
                    if session is not None and session in need_set:
                        found[sid].setdefault(session, bar)
            for d in need:
                if d not in found[sid]:
                    reasons.setdefault(sid, {}).setdefault(d, "no_bar_at_open")
        for sid in ids:
            self._opening.setdefault(sid, {}).update(found[sid])
            if sid in reasons:
                self._opening_reason.setdefault(sid, {}).update(reasons[sid])

    # --- daily bars -----------------------------------------------------------------------------------------
    async def _ensure_daily(self, symbol_ids: Sequence[int], session_date: date) -> None:
        """Load the daily bars `session_date` needs (DAILY_MARGIN before it, and its own day) and the next
        CHUNK_DAYS, per symbol, never after `date_to`."""
        through = min(session_date, self.date_to)
        low = max(self._daily_from, session_date - DAILY_MARGIN)
        ranges: dict[int, tuple[date, date]] = {}
        for sid in sorted(set(symbol_ids)):
            loaded = self._daily_to.get(sid)
            if loaded is not None and loaded >= through:
                continue
            lo = low if loaded is None else max(low, loaded + ONE_DAY)
            ranges[sid] = (lo, min(self.date_to, through + CHUNK_DAYS))
        errors: dict[int, str] = {}
        for (lo, hi), ids in _group(ranges).items():
            await self._load_daily(ids, lo, hi, errors)
            for sid in ids:
                self._daily_to[sid] = hi
        self._warn(self._current_day(), "daily_bars", errors)

    async def _load_daily(self, ids: list[int], lo: date, hi: date, errors: dict[int, str]) -> None:
        found: dict[int, dict[date, Candle]] = {sid: {} for sid in ids}
        with self._factory() as s:
            for row in s.execute(
                select(m.DailyCandle).where(
                    m.DailyCandle.symbol_id.in_(ids), m.DailyCandle.date >= lo, m.DailyCandle.date <= hi
                )
            ).scalars():
                found[row.symbol_id][row.date] = _row_candle(_day_start(row.date), row, ONE_DAY)
        sessions: list[date] = []
        d = lo
        while d <= hi:
            if self._calendar.is_session(d) and self._questrade_allowed(d):
                sessions.append(d)
            d += ONE_DAY
        wanted = {sid: [x for x in sessions if x not in found[sid]] for sid in ids}
        qids = self._questrade_ids([sid for sid in ids if wanted[sid]])
        reqs: dict[int, CandleRequest] = {
            sid: CandleRequest(
                qids[sid], _day_start(wanted[sid][0]), _day_start(wanted[sid][-1] + ONE_DAY), "OneDay"
            )
            for sid in ids
            if wanted[sid] and sid in qids
        }
        if reqs:
            results = await self._fetch([reqs[sid] for sid in sorted(reqs)])
            for sid in sorted(reqs):
                result = results.get(reqs[sid], QuestradeApiError(0, "no result"))
                why = _error_reason(result)
                if why is not None:
                    errors[sid] = why
                    continue
                assert isinstance(result, list)
                need = set(wanted[sid])
                for bar in sorted(result, key=lambda c: c.start):
                    day = et_date(bar.start)
                    if day in need:
                        found[sid].setdefault(day, bar)
        for sid in ids:
            self._daily.setdefault(sid, {}).update(found[sid])

    def _daily_before(self, sid: int, session_date: date) -> list[Candle]:
        """The nightly job's ATR input: daily bars from 30 days before the previous session's end."""
        prev = self._calendar.previous_session(session_date)
        end = _day_start(prev + ONE_DAY)
        start = end - DAILY_LOOKBACK
        bars = self._daily.get(sid, {})
        return [bars[d] for d in sorted(bars) if start <= bars[d].start < end]

    def _atr_before(self, sid: int, session_date: date) -> Decimal | None:
        key = (sid, session_date)
        if key not in self._atr:
            self._atr[key] = atr(self._daily_before(sid, session_date), ATR_PERIOD)
        return self._atr[key]

    # --- 1-minute bars --------------------------------------------------------------------------------------
    def _switch_minute_day(self, session_date: date) -> None:
        if self._minute_day != session_date:
            self._minute = {}
            self._minute_day = session_date

    def _minutes_until(self, symbol_id: int, at: datetime) -> list[Candle]:
        """The loaded 1-minute bars of `symbol_id` that ended by `at` (the day of `at`'s last minute)."""
        if self._minute_day is None or et_date(at - ONE_MINUTE) != self._minute_day:
            return []
        bars = self._minute.get(symbol_id, [])
        return bars[: bisect_right([b.end for b in bars], at)]

    # --- MarketDataView (strategy-facing; never past the replay clock) --------------------------------------
    async def _universe_for(self, session_date: date) -> list[UniverseMember]:
        stored = await self._db.universe(session_date)
        if stored:
            return stored
        if self._biased_names is None:
            with self._factory() as s:
                newest = s.execute(
                    select(m.UniverseSnapshot.session_date)
                    .where(m.UniverseSnapshot.session_date <= self._wall_date)
                    .order_by(m.UniverseSnapshot.session_date.desc())
                    .limit(1)
                ).scalar_one_or_none()
            self._biased_names = await self._db.universe(newest) if newest is not None else []
        names = self._biased_names
        if not names:
            return []
        if self.date_from <= session_date <= self.date_to:
            self._biased.add(session_date)
        # Only the names come from the later snapshot; the numbers are recomputed from the daily bars
        # before this session with the nightly formulas (no lookahead).
        await self._ensure_daily([u.symbol_id for u in names], session_date)
        out: list[UniverseMember] = []
        for u in names:
            daily = self._daily_before(u.symbol_id, session_date)
            avg = average_volume(daily[-AVG_VOLUME_DAYS:])
            out.append(
                UniverseMember(
                    u.symbol_id,
                    u.ticker,
                    u.name,
                    daily[-1].close if daily else None,
                    int(avg) if avg is not None else None,
                    self._atr_before(u.symbol_id, session_date),
                    BIASED_SOURCE,
                )
            )
        return out

    async def universe(self, session_date: date) -> list[UniverseMember]:
        if session_date == self._day:
            return list(self._day_universe)
        return await self._universe_for(session_date)

    async def universe_status(self, session_date: date) -> UniverseStatus:
        members = await self.universe(session_date)
        if members and members[0].source == BIASED_SOURCE:
            return UniverseStatus(source=BIASED_SOURCE, fallback_from=None, stale=False, age_sessions=None)
        return await self._db.universe_status(session_date)

    async def _stats_for(
        self, session_date: date, members: Sequence[UniverseMember]
    ) -> dict[int, OpenBarStats]:
        stats = dict(await self._db.open_bar_stats(session_date))
        need = sorted({u.symbol_id for u in members} - stats.keys())
        if need:
            await self._ensure_opening(need, session_date)
            await self._ensure_daily(need, session_date)
            lookback = self._calendar.sessions_before(session_date, self.lookback_sessions)
            min_bars = min(MIN_OPENING_BARS, len(lookback))
            for sid in need:
                bars = self._opening.get(sid, {})
                opening = [bars[d] for d in lookback if d in bars]
                avg_open = average_volume(opening) if len(opening) >= min_bars else None
                stats[sid] = OpenBarStats(sid, avg_open, self._atr_before(sid, session_date))
        return dict(sorted(stats.items()))

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        if session_date == self._day and self._day_stats is not None:
            return dict(self._day_stats)
        return await self._stats_for(session_date, await self.universe(session_date))

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        if symbol_ids is None:
            symbol_ids = [u.symbol_id for u in await self.universe(session_date)]
        ids = sorted(set(symbol_ids))
        await self._ensure_opening(ids, session_date)
        now = self._clock.now()
        bars: dict[int, Candle] = {}
        missing: dict[int, str] = {}
        for sid in ids:
            bar = self._opening.get(sid, {}).get(session_date)
            if bar is None:
                missing[sid] = self._opening_reason.get(sid, {}).get(session_date, NO_ARCHIVED_BAR)
            elif bar.end > now:
                missing[sid] = "bar_not_complete"
            else:
                bars[sid] = bar
        return OpeningBars(bars, missing)

    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        """Synthetic quotes from the last complete 1-minute bar (last = close, bid/ask = close -/+ hs)."""
        now = self._clock.now()
        day = et_date(now)
        if not self._calendar.is_session(day):
            return {}
        ids = sorted(set(symbol_ids))
        self._switch_minute_day(day)
        need = [sid for sid in ids if sid not in self._minute]
        if need:
            await self.load_minute_bars(need, day)
        names = self._ticker_names(ids)
        out: dict[int, QtQuote] = {}
        for sid in ids:
            done = self._minutes_until(sid, now)
            if not done:
                continue
            bar = done[-1]
            hs = self._spread.half_spread(bar.close)
            out[sid] = QtQuote(
                symbol_id=sid,
                symbol=names.get(sid, ""),
                bid=bar.close - hs,
                ask=bar.close + hs,
                last=bar.close,
                last_regular=bar.close,
                volume=bar.volume,
                last_trade_time=bar.end,
                delay=0,
                is_halted=False,
                vwap=None,
            )
        return out

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        """Bars with start in [start, end) that ended by the replay clock. 1-minute bars of the current day
        come from the loaded day; other bars from the archive and the cache (daily: `daily_candles` and
        fetched daily bars, as far back as the current day's look-back), never fetched here."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("candles() needs timezone-aware start and end")
        now = self._clock.now()
        step = STEP[interval]
        found: dict[datetime, Candle] = {}
        if interval == "OneDay":
            await self._ensure_daily([symbol_id], self._current_day())
            for bar in self._daily.get(symbol_id, {}).values():
                found[bar.start] = bar
        else:
            day = et_date(start)
            if interval == "OneMinute" and self._calendar.is_session(day) and day == self._minute_day:
                if symbol_id not in self._minute:
                    await self.load_minute_bars([symbol_id], day)
                for bar in self._minute.get(symbol_id, []):
                    found[bar.start] = bar
            code = INTERVAL_CODES[interval]
            with self._factory() as s:
                for arch in s.execute(
                    select(m.CandleArchive).where(
                        m.CandleArchive.symbol_id == symbol_id,
                        m.CandleArchive.interval == code,
                        m.CandleArchive.start_ts >= start,
                        m.CandleArchive.start_ts < end,
                    )
                ).scalars():
                    found.setdefault(arch.start_ts, _row_candle(arch.start_ts, arch, step))
                for row in s.execute(
                    select(m.IntradayCandle).where(
                        m.IntradayCandle.symbol_id == symbol_id,
                        m.IntradayCandle.interval == code,
                        m.IntradayCandle.ts >= start,
                        m.IntradayCandle.ts < end,
                    )
                ).scalars():
                    found.setdefault(row.ts, _row_candle(row.ts, row, step))
            if interval == "FiveMinutes":
                for bar in self._opening.get(symbol_id, {}).values():
                    found.setdefault(bar.start, bar)
        return [found[t] for t in sorted(found) if start <= t < end and found[t].end <= now]

    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None:
        return (await self.prior_closes([symbol_id], session_date)).get(symbol_id)

    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]:
        prev = self._calendar.previous_session(session_date)
        if self._calendar.session_close(prev) > self._clock.now():
            return {}  # that session had not closed yet at the replay clock
        ids = sorted(set(symbol_ids))
        await self._ensure_daily(ids, session_date)
        out: dict[int, Decimal] = {}
        for sid in ids:
            bar = self._daily.get(sid, {}).get(prev)
            if bar is not None:
                out[sid] = bar.close
        return out

    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]:
        return await self._db.symbol_ids(tickers)

    # --- runner-only helpers --------------------------------------------------------------------------------
    def _prune(self, session_date: date) -> None:
        """Drop opening and daily bars that no later day can need."""
        lookback = self._calendar.sessions_before(session_date, self.lookback_sessions)
        oldest = lookback[0] if lookback else session_date
        for sid, bars in self._opening.items():
            self._opening[sid] = {d: bar for d, bar in bars.items() if d >= oldest}
        for sid, reasons in self._opening_reason.items():
            self._opening_reason[sid] = {d: why for d, why in reasons.items() if d >= oldest}
        daily_oldest = session_date - DAILY_MARGIN
        for sid, by_date in self._daily.items():
            self._daily[sid] = {d: v for d, v in by_date.items() if d >= daily_oldest}
        self._atr = {}

    async def prepare_day(self, session_date: date) -> None:
        """Load the day's universe, opening bars, stats and SPY's 1-minute bars. Drop the previous day's."""
        self._switch_minute_day(session_date)
        self._prune(session_date)
        self._day = session_date
        self._day_stats = None
        self._day_universe = await self._universe_for(session_date)
        ids = sorted({u.symbol_id for u in self._day_universe})
        await self._ensure_opening(ids, session_date)
        self._counts["missing_opening_bars"] += sum(
            1 for sid in ids if session_date not in self._opening.get(sid, {})
        )
        self._day_stats = await self._stats_for(session_date, self._day_universe)
        spy = (await self._db.symbol_ids([OVERLAY_SYMBOL])).get(OVERLAY_SYMBOL)
        if spy is not None:
            await self.load_minute_bars([spy], session_date)

    async def load_minute_bars(self, symbol_ids: Sequence[int], session_date: date) -> None:
        self._switch_minute_day(session_date)
        need = sorted(set(symbol_ids) - self._minute.keys())
        if not need:
            return
        open_, close = self._calendar.session_open(session_date), self._calendar.session_close(session_date)
        found: dict[int, dict[datetime, Candle]] = {sid: {} for sid in need}
        with self._factory() as s:
            for arch in s.execute(
                select(m.CandleArchive).where(
                    m.CandleArchive.symbol_id.in_(need),
                    m.CandleArchive.interval == MINUTE_CODE,
                    m.CandleArchive.start_ts >= open_,
                    m.CandleArchive.start_ts < close,
                )
            ).scalars():
                found[arch.symbol_id][arch.start_ts] = _row_candle(arch.start_ts, arch, ONE_MINUTE)
            cache_ids = [sid for sid in need if not found[sid]]
            if cache_ids:
                for row in s.execute(
                    select(m.IntradayCandle).where(
                        m.IntradayCandle.symbol_id.in_(cache_ids),
                        m.IntradayCandle.interval == MINUTE_CODE,
                        m.IntradayCandle.ts >= open_,
                        m.IntradayCandle.ts < close,
                    )
                ).scalars():
                    found[row.symbol_id][row.ts] = _row_candle(row.ts, row, ONE_MINUTE)
        errors: dict[int, str] = {}
        fetch_ids = [sid for sid in need if not found[sid]] if self._questrade_allowed(session_date) else []
        qids = self._questrade_ids(fetch_ids)
        reqs = {sid: CandleRequest(qids[sid], open_, close, "OneMinute") for sid in fetch_ids if sid in qids}
        if reqs:
            results = await self._fetch([reqs[sid] for sid in sorted(reqs)])
            for sid in sorted(reqs):
                result = results.get(reqs[sid], QuestradeApiError(0, "no result"))
                why = _error_reason(result)
                if why is not None:
                    errors[sid] = why
                    continue
                assert isinstance(result, list)
                for bar in regular_hours(result, self._calendar, session_date):
                    found[sid].setdefault(bar.start, bar)
        for sid in need:
            bars = [found[sid][t] for t in sorted(found[sid])]
            self._minute[sid] = bars
            if not bars:
                self._counts["missing_minute_bars"] += 1
        self._warn(session_date, "minute_bars", errors)

    def bar_ending_at(self, symbol_id: int, at: datetime) -> Candle | None:
        done = self._minutes_until(symbol_id, at)
        return done[-1] if done and done[-1].end == at else None

    def last_close(self, symbol_id: int, at: datetime) -> Decimal | None:
        done = self._minutes_until(symbol_id, at)
        return done[-1].close if done else None

    def progress_counts(self) -> Mapping[str, int]:
        """`missing_opening_bars`, `missing_minute_bars`, `questrade_requests`."""
        return dict(self._counts)

    def held_counts(self) -> Mapping[str, int]:
        """How many bars are held in memory (tests and diagnostics)."""
        return {
            "opening_bars": sum(len(v) for v in self._opening.values()),
            "daily_bars": sum(len(v) for v in self._daily.values()),
            "minute_bars": sum(len(v) for v in self._minute.values()),
        }

    @property
    def biased_days(self) -> frozenset[date]:
        return frozenset(self._biased)
