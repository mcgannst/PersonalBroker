"""`QuoteTap`: a transparent wrapper around the worker's Questrade client that remembers the quotes it
returned (live dashboard plan S1a, S10).

Rules (S1a; soak safety, pinned by tests/marks/test_tap.py):
- Each proxied method is a plain `async def` whose only `await` is the inner call with the same arguments: no
  task, lock, timeout, sleep, thread, context manager or retry, so the tap adds no event-loop iteration and a
  cancellation reaches the inner call exactly as before.
- It returns the inner result itself (never a copy) and re-raises the inner exception unchanged (a bare
  `raise`, nothing returned or suppressed in a `finally`). Its own bookkeeping runs in `try/except Exception`
  blocks that only log (at `warning`, once per failure streak) and never replace the caller's result or
  exception.
- `candles_many` forwards `reqs` (not touched before the inner call returns) and `deadline_s` as given; around
  the call it reads only the loop's clock and a snapshot of the client's market counters, and afterwards keeps
  only counts (`CandleBatch`), never a reference to the requests, the result or a candle.
- Memory is bounded (at most MAX_OBSERVATIONS_PER_SYMBOL × MAX_TAP_SYMBOLS quote references, and
  MAX_CANDLE_BATCHES count records). No database, file or network access; times come from the Clock.
"""

import asyncio
import dataclasses
import math
from collections import deque
from collections.abc import Sequence
from datetime import UTC, date, datetime
from itertools import count
from typing import Any

import structlog

from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.market.clock import Clock, et_date
from trader.market.data_service import QuoteClient
from trader.market.types import Candle, Interval
from trader.marks.types import (
    MAX_CANDLE_BATCHES,
    MAX_OBSERVATIONS_PER_SYMBOL,
    MAX_TAP_SYMBOLS,
    CandleBatch,
    ObservedQuote,
)

log = structlog.get_logger("marks.tap")

CATEGORIES = ("market", "account")
COUNTERS = ("requests", "http_429", "pause_s", "http_5xx", "transport_errors")
FLOAT_COUNTERS = frozenset({"pause_s"})

Counters = dict[str, dict[str, float]]


def _finite(value: Any) -> float:
    """A number as a finite float (0.0 for NaN, infinity or a non-number): the heartbeat refuses NaN."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _category(stats: Any, name: str) -> dict[str, float]:
    """One category's counters from a client's `stats` (a dict of `CallStats`); zeros when absent."""
    item = stats.get(name) if isinstance(stats, dict) else None
    if item is None or not dataclasses.is_dataclass(item) or isinstance(item, type):
        return dict.fromkeys(COUNTERS, 0.0)
    values = dataclasses.asdict(item)
    return {k: _finite(values.get(k, 0)) for k in COUNTERS}


def _snapshot(stats: Any) -> Counters:
    return {name: _category(stats, name) for name in CATEGORIES}


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def _counts_out(values: dict[str, float]) -> dict[str, int | float]:
    return {k: round(v, 3) if k in FLOAT_COUNTERS else int(v) for k, v in values.items()}


class QuoteTap:
    """Satisfies `QuoteClient` (and `LatestQuotes` through `drain`)."""

    def __init__(self, inner: QuoteClient, clock: Clock) -> None:
        self._inner = inner
        self._clock = clock
        # Undrained observations: Questrade id -> the newest (sequence, observation) pairs, least recently
        # observed id first (an id moves to the end when observed again).
        self._pending: dict[int, deque[tuple[int, ObservedQuote]]] = {}
        self._seq = count()
        self._batches: deque[CandleBatch] = deque(maxlen=MAX_CANDLE_BATCHES)
        # S10 baseline: zero until the first ET date change, then the counters before the day's first call.
        self._base: Counters = _snapshot(None)
        self._base_day: date | None = None
        self._since: datetime | None = None
        self._failing = False
        self._health_failing = False
        try:
            built = clock.now()
            self._base_day, self._since = et_date(built), built
        except Exception:  # a broken clock: the first bookkeeping that can read it sets the day (or logs)
            self._base_day = self._since = None

    # --- the proxied QuoteClient methods --------------------------------------------------------------------

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        try:
            self._roll_day()
        except Exception as exc:
            self._failed("roll_day", exc)
        result = await self._inner.quotes(ids)
        try:
            self._observe(result)
        except Exception as exc:
            self._failed("observe", exc)
        return result

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        return await self._inner.candles(symbol_id, start, end, interval)

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        before: tuple[datetime, float, dict[str, float]] | None = None
        try:  # its own guard: a failing day roll never drops this batch's record
            self._roll_day()
        except Exception as exc:
            self._failed("roll_day", exc)
        try:
            before = (self._clock.now(), asyncio.get_running_loop().time(), self._market())
        except Exception as exc:
            self._failed("batch_start", exc)
        try:
            result = await self._inner.candles_many(reqs, deadline_s=deadline_s)
        except BaseException as exc:
            self._end_batch(before, reqs, None, deadline_s, type(exc).__name__)
            raise
        self._end_batch(before, reqs, result, deadline_s, None)
        return result

    @property
    def stats(self) -> Any:
        """The inner client's `stats` attribute on every access (None when it has none)."""
        return getattr(self._inner, "stats", None)

    # --- bookkeeping (synchronous, bounded, no I/O) ---------------------------------------------------------

    def _observe(self, result: list[QtQuote]) -> None:
        now = self._clock.now()
        pending = self._pending
        for q in result:
            qid = q.symbol_id
            kept = pending.pop(qid, None)
            if kept is None:
                kept = deque(maxlen=MAX_OBSERVATIONS_PER_SYMBOL)
            kept.append((next(self._seq), ObservedQuote(qid, q, now)))
            pending[qid] = kept
            if len(pending) > MAX_TAP_SYMBOLS:
                del pending[next(iter(pending))]
        self._ok()

    def _market(self) -> dict[str, float]:
        return _category(self.stats, "market")

    def _roll_day(self) -> None:
        """On the first call or health read of a new ET date, the baseline becomes the counters now (S10)."""
        now = self._clock.now()
        today = et_date(now)
        if self._base_day is None:
            self._base_day, self._since = today, now
        elif today != self._base_day:
            self._base = _snapshot(self.stats)
            self._base_day, self._since = today, now

    def _end_batch(
        self,
        before: tuple[datetime, float, dict[str, float]] | None,
        reqs: Sequence[CandleRequest],
        result: dict[CandleRequest, list[Candle] | QuestradeApiError] | None,
        deadline_s: float | None,
        raised: str | None,
    ) -> None:
        """Count one finished batch; never raises (a failure is logged once per streak)."""
        try:
            if before is None:
                return
            started_at, started, market = before
            elapsed = asyncio.get_running_loop().time() - started
            after = self._market()
            distinct = set(reqs)
            completed = errors = 0
            if result is not None:
                for value in result.values():
                    if isinstance(value, QuestradeApiError):
                        errors += 1
                    elif isinstance(value, list):
                        completed += 1
            outstanding = len(distinct) if result is None else sum(1 for r in distinct if r not in result)
            # a deadline that is not a finite number would not be JSON-safe: reported as none
            finite = isinstance(deadline_s, int | float) and math.isfinite(deadline_s)
            deadline = float(deadline_s) if finite and deadline_s is not None else None
            self._batches.append(
                CandleBatch(
                    started_at=started_at,
                    symbols=len(distinct),
                    completed=completed,
                    errors=errors,
                    outstanding=outstanding,
                    elapsed_s=round(_finite(elapsed), 3),
                    deadline_s=deadline,
                    http_429=int(after["http_429"] - market["http_429"]),
                    pause_s=round(after["pause_s"] - market["pause_s"], 3),
                    raised=raised,
                )
            )
            self._ok()
        except Exception as exc:
            self._failed("batch_end", exc)

    def _failed(self, where: str, exc: Exception) -> None:
        """Logs once per failure streak; never raises (it runs inside `except` blocks, so a raising logger
        would otherwise replace the caller's result or the inner exception)."""
        if self._failing:
            return
        self._failing = True
        try:
            log.warning("marks.tap_bookkeeping_failed", where=where, error_type=type(exc).__name__)
        except Exception:  # noqa: S110 - a broken log sink must never reach the trading caller
            pass

    def _ok(self) -> None:
        self._failing = False

    # --- the publisher's and the heartbeat's side -----------------------------------------------------------

    def drain(self) -> list[ObservedQuote]:
        """The undrained observations in observation order; the tap starts empty again."""
        pending, self._pending = self._pending, {}
        pairs = [pair for kept in pending.values() for pair in kept]
        pairs.sort(key=lambda pair: pair[0])
        return [obs for _, obs in pairs]

    def health_detail(self) -> dict[str, Any]:
        """The heartbeat's `questrade` and `candle_batches` keys (S10); `{}` when it fails (logged once per
        streak). `questrade` is absent while the inner client has no `stats`."""
        try:
            self._roll_day()
            today = self._base_day
            detail: dict[str, Any] = {}
            stats = self.stats
            if stats is not None and self._since is not None and today is not None:
                current = _snapshot(stats)
                detail["questrade"] = {
                    "day": today.isoformat(),
                    "since": _iso(self._since),
                    **{
                        name: _counts_out({k: current[name][k] - self._base[name][k] for k in COUNTERS})
                        for name in CATEGORIES
                    },
                }
            detail["candle_batches"] = [
                {
                    "started_at": _iso(b.started_at),
                    "symbols": b.symbols,
                    "completed": b.completed,
                    "errors": b.errors,
                    "outstanding": b.outstanding,
                    "elapsed_s": b.elapsed_s,
                    "deadline_s": b.deadline_s,
                    "http_429": b.http_429,
                    "pause_s": b.pause_s,
                    "raised": b.raised,
                }
                for b in self._batches
                if et_date(b.started_at) == today
            ]
        except Exception as exc:
            if not self._health_failing:
                self._health_failing = True
                try:  # never raises, even when the logger does
                    log.warning("marks.tap_health_failed", error_type=type(exc).__name__)
                except Exception:  # noqa: S110
                    pass
            return {}
        self._health_failing = False
        return detail
