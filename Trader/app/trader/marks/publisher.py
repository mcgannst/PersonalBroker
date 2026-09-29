"""`MarkPublisher`: the worker task that writes `quote_marks` and `mark_bars` from the quotes the tap saw
(live dashboard plan S1, S10).

Every `PUBLISH_INTERVAL_S` it drains the tap on the event loop (a dict swap, no I/O). A pass with no new
observations touches no database. Otherwise the whole pass runs in the publisher's OWN single-thread executor
(threads named `marks*`), never on the event loop and never in the default executor that the Questrade
client's token fetch uses, so a stuck publisher can never hold a thread the trading path needs. The pass is
one short transaction on one pooled connection with `statement_timeout` and `lock_timeout` set, and reads the
trading tables with plain SELECTs only (no `FOR UPDATE`, no advisory lock). The only row locks it takes
outside its own tables are the `KEY SHARE` locks its inserts' foreign-key checks need (the live `runs` row
and the `symbols` rows), and it takes those first, in id order, with `NOWAIT`: when a trading transaction
holds a conflicting lock (nightly's `upsert_symbols` locks symbols rows `FOR UPDATE`) the pass is skipped at
once and retried at the next cadence, so the publisher never waits while holding a key lock and can never
make a trading transaction the victim of a deadlock. It writes only `quote_marks` (the latest observation per
symbol) and `mark_bars` (1-minute bars of observed prices), for symbols the live run holds or has working
orders in.

A failing pass logs one masked warning and writes one `warning` event (through `deps.event`, in the executor)
per failure streak, and one `info` event on the next success; it never raises. It never calls Questrade, and
nothing on the decision path reads its tables (bars built from quotes are never candles).
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import delete, func, select, text, union
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.logging_setup import redact_text
from trader.market.clock import Clock, et_date
from trader.marks.types import (
    MARK_BARS_KEEP_DAYS,
    MAX_OBSERVATIONS_PER_SYMBOL,
    MAX_TAP_SYMBOLS,
    PUBLISH_INTERVAL_S,
    PUBLISH_STATEMENT_TIMEOUT_MS,
    EventWriter,
    LatestQuotes,
    ObservedQuote,
    PublishStep,
)

log = structlog.get_logger("marks.publisher")

MAX_ERROR_CHARS = 300
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)  # a step's time when even the clock failed
THREAD_PREFIX = "marks"
# One statement sets both limits for this transaction only (SET LOCAL semantics: is_local = true).
SET_LIMITS = "SELECT set_config('statement_timeout', :ms, true), set_config('lock_timeout', :ms, true)"
MARK_COLUMNS = ("bid", "ask", "last", "quote_time", "observed_at", "written_at", "is_halted")
# The rows the inserts' foreign-key checks lock (KEY SHARE), taken first, in one order and WITHOUT waiting: a
# pass that meets a conflicting lock (nightly's upsert_symbols takes FOR UPDATE on symbols rows) gives up at
# once instead of holding some key locks while it waits for others, so it can never close a deadlock cycle
# with a trading transaction (DB-T2 gauntlet F2).
LOCK_RUN = "SELECT id FROM trader.runs WHERE id = :run_id FOR KEY SHARE NOWAIT"
LOCK_SYMBOLS = "SELECT id FROM trader.symbols WHERE id = ANY(:sids) ORDER BY id FOR KEY SHARE NOWAIT"
LOCK_NOT_AVAILABLE = "55P03"  # SQLSTATE lock_not_available (NOWAIT)
# A carried-over busy pass keeps at most this many observations (the tap's own bound).
MAX_CARRY = MAX_OBSERVATIONS_PER_SYMBOL * MAX_TAP_SYMBOLS


class _Busy(Exception):
    """A referenced row is locked by another transaction right now: this pass is skipped, not failed."""


@dataclass(frozen=True)
class MarkPublisherDeps:
    factory: sessionmaker[Session]
    clock: Clock
    tap: LatestQuotes
    run_id: Callable[[], int | None]
    event: EventWriter | None = None


def describe(exc: BaseException) -> str:
    """An exception as one masked, capped line (it goes into event_log, which the web app shows)."""
    flat = " ".join(redact_text(f"{type(exc).__name__}: {exc}").split())
    return flat if len(flat) <= MAX_ERROR_CHARS else flat[: MAX_ERROR_CHARS - 1] + "…"


def _price(q: QtQuote) -> Decimal | None:
    """The quote's price: the last trade, else the last regular-session trade."""
    return q.last if q.last is not None else q.last_regular


def _minute(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(second=0, microsecond=0)


def _log_only(level: str, message: str, data: dict[str, Any], run_id: int | None) -> None:
    log.info("marks.event", level=level, message=message)


class MarkPublisher:
    def __init__(
        self,
        deps: MarkPublisherDeps,
        *,
        interval_s: float = PUBLISH_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.deps = deps
        self._interval_s = interval_s
        self._sleep = sleep
        # Its own single thread: never the loop's default executor (the Questrade token fetch's).
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=THREAD_PREFIX)
        # The state below is written only in the executor's thread (the health read only reads it).
        self._symbol_ids: dict[int, int] = {}  # Questrade id -> symbols.id
        self._failures = 0
        self.busy_passes = 0  # passes skipped because a referenced row was locked (never a failure)
        self._carry: list[ObservedQuote] = []  # a busy pass's observations, retried with the next pass
        self._pruned_day: date | None = None
        self._written_at: datetime | None = None
        self._symbols = 0
        self._loop_failing = False  # a failure before the executor (drain, a closed executor): logged once

    # --- the loop -------------------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """`run_once` then sleep `interval_s` (waking early on `stop`) until `stop` is set; never raises."""
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # run_once never raises; a last guard so the task never dies
                log.warning("marks.run_once_raised", error_type=type(exc).__name__)
            if stop.is_set():
                return
            await self._sleep_or_stop(self._interval_s, stop)

    async def run_once(self) -> PublishStep:
        """Drain the tap on the loop; with observations, one pass in the executor. Never raises."""
        at = EPOCH
        try:
            at = self.deps.clock.now()
            observed = self.deps.tap.drain()
        except Exception as exc:
            self._loop_failed("drain", exc)
            return PublishStep(at, "error", 0, 0)
        if not observed:
            return PublishStep(at, "no_new_quotes", 0, 0)
        try:
            loop = asyncio.get_running_loop()
            step = await loop.run_in_executor(self._executor, self._pass, observed)
        except Exception as exc:  # the executor itself failed (e.g. closed): nothing reached the database
            self._loop_failed("executor", exc)
            return PublishStep(at, "error", 0, 0)
        self._loop_failing = False
        return step

    def health_detail(self) -> dict[str, Any]:
        """The heartbeat's `marks` key (S10)."""
        written = self._written_at
        return {
            "written_at": written.astimezone(UTC).isoformat() if written is not None else None,
            "symbols": int(self._symbols),
            "failing": self._failures > 0 or self._loop_failing,
        }

    def close(self) -> None:
        """Shuts the executor down without waiting; idempotent."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    # --- one pass (in the executor's thread) ----------------------------------------------------------------

    def _pass(self, observed: Sequence[ObservedQuote]) -> PublishStep:
        now = EPOCH
        run_id: int | None = None
        carried, self._carry = self._carry, []
        if carried:  # a busy pass's observations first: observation order is kept
            observed = [*carried, *observed]
        try:
            now = self.deps.clock.now()
            run_id = self.deps.run_id()
            if run_id is None:  # no live run: the observations are dropped
                return PublishStep(now, "no_run", 0, 0)
            marks, bars = self._write(observed, run_id, now)
        except _Busy:
            # Not a failure (no warning, no event, the streak is untouched): the rows are locked by another
            # transaction for now. The transaction was rolled back; the next cadence retries these
            # observations.
            self.busy_passes += 1
            self._carry = list(observed[-MAX_CARRY:])
            try:
                log.debug("marks.publish_busy", busy_passes=self.busy_passes, carried=len(self._carry))
            except Exception:  # noqa: S110 - a skipped pass never raises
                pass
            return PublishStep(now, None, 0, 0)
        except Exception as exc:
            self._failed(exc, run_id)
            return PublishStep(now, "error", 0, 0)
        self._succeeded(run_id, now, marks)
        return PublishStep(now, None, marks, bars)

    def _write(self, observed: Sequence[ObservedQuote], run_id: int, now: datetime) -> tuple[int, int]:
        """One transaction with statement and lock timeouts: the marks and bars, then (once per ET day) the
        run's old bars."""
        today = et_date(now)
        prune = self._pruned_day != today
        with self.deps.factory() as s, s.begin():
            s.execute(text(SET_LIMITS), {"ms": str(PUBLISH_STATEMENT_TIMEOUT_MS)})
            counts = self._apply(s, observed, run_id, now)
            if prune:
                s.execute(
                    delete(m.MarkBar).where(
                        m.MarkBar.run_id == run_id,
                        m.MarkBar.minute_start < now - timedelta(days=MARK_BARS_KEEP_DAYS),
                    )
                )
        if prune:
            self._pruned_day = today
        return counts

    def _wanted(self, s: Session, observed: Sequence[ObservedQuote], run_id: int) -> dict[int, int]:
        """Questrade id -> symbols.id for the observed symbols the run holds or has working orders in."""
        qids = {o.qt_id for o in observed}
        unknown = [qid for qid in qids if qid not in self._symbol_ids]
        if unknown:
            rows = s.execute(
                select(m.Symbol.id, m.Symbol.questrade_id).where(m.Symbol.questrade_id.in_(unknown))
            ).all()
            for sid, qid in rows:
                if qid is not None:
                    self._symbol_ids[int(qid)] = int(sid)
        mapped = {qid: self._symbol_ids[qid] for qid in qids if qid in self._symbol_ids}
        if not mapped:
            return {}
        sids = sorted(set(mapped.values()))
        held = select(m.Position.symbol_id).where(
            m.Position.run_id == run_id, m.Position.closed_at.is_(None), m.Position.symbol_id.in_(sids)
        )
        working = select(m.Order.symbol_id).where(
            m.Order.run_id == run_id, m.Order.status == "working", m.Order.symbol_id.in_(sids)
        )
        wanted = {int(sid) for sid in s.execute(union(held, working)).scalars()}
        return {qid: sid for qid, sid in mapped.items() if sid in wanted}

    @staticmethod
    def _lock_keys(s: Session, run_id: int, sids: list[int]) -> None:
        """KEY SHARE on the run row and the symbols rows the inserts reference, in id order, NOWAIT. KEY SHARE
        conflicts only with FOR UPDATE (a delete or a key-column update), never with the trading path's
        FOR NO KEY UPDATE / plain UPDATE of a row; a conflict raises `_Busy` at once (the pass is skipped)."""
        try:
            s.execute(text(LOCK_RUN), {"run_id": run_id})
            s.execute(text(LOCK_SYMBOLS), {"sids": sids})
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
                raise _Busy from exc
            raise

    def _apply(
        self, s: Session, observed: Sequence[ObservedQuote], run_id: int, now: datetime
    ) -> tuple[int, int]:
        """Upsert the latest mark per wanted symbol and its 1-minute bars; (marks, bars) written."""
        wanted = self._wanted(s, observed, run_id)
        if not wanted:
            return (0, 0)
        self._lock_keys(s, run_id, sorted(set(wanted.values())))
        latest: dict[int, ObservedQuote] = {}
        # (symbol_id, minute) -> [open, high, low, close, samples], in observation order
        bars: dict[tuple[int, datetime], list[Any]] = {}
        for o in observed:
            sid = wanted.get(o.qt_id)
            if sid is None:
                continue
            seen = latest.get(sid)
            if seen is None or o.observed_at >= seen.observed_at:
                latest[sid] = o
            price = _price(o.quote)
            if price is None or price <= 0:
                continue
            key = (sid, _minute(o.observed_at))
            bar = bars.get(key)
            if bar is None:
                bars[key] = [price, price, price, price, 1]
            else:
                bar[1], bar[2], bar[3], bar[4] = max(bar[1], price), min(bar[2], price), price, bar[4] + 1
        mark_rows = [
            {
                "run_id": run_id,
                "symbol_id": sid,
                "bid": o.quote.bid,
                "ask": o.quote.ask,
                "last": _price(o.quote),
                "quote_time": o.quote.last_trade_time,
                "observed_at": o.observed_at,
                "written_at": now,
                "is_halted": bool(o.quote.is_halted),
            }
            for sid, o in sorted(latest.items())
        ]
        marks = insert(m.QuoteMark).values(mark_rows)
        s.execute(
            marks.on_conflict_do_update(
                index_elements=[m.QuoteMark.run_id, m.QuoteMark.symbol_id],
                set_={c: marks.excluded[c] for c in MARK_COLUMNS},
                where=m.QuoteMark.observed_at <= marks.excluded.observed_at,  # never older over newer
            )
        )
        if not bars:
            return (len(mark_rows), 0)
        bar_rows = [
            {
                "run_id": run_id,
                "symbol_id": sid,
                "minute_start": minute,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "samples": n,
                "updated_at": now,
            }
            for (sid, minute), (o, h, lo, c, n) in sorted(bars.items())
        ]
        upsert = insert(m.MarkBar).values(bar_rows)
        s.execute(
            upsert.on_conflict_do_update(
                index_elements=[m.MarkBar.run_id, m.MarkBar.symbol_id, m.MarkBar.minute_start],
                set_={
                    "high": func.greatest(m.MarkBar.high, upsert.excluded.high),
                    "low": func.least(m.MarkBar.low, upsert.excluded.low),
                    "close": upsert.excluded.close,
                    "samples": m.MarkBar.samples + upsert.excluded.samples,
                    "updated_at": upsert.excluded.updated_at,
                },
            )
        )
        return (len(mark_rows), len(bar_rows))

    # --- the failure streak (in the executor's thread) ------------------------------------------------------

    def _failed(self, exc: Exception, run_id: int | None) -> None:
        self._failures += 1
        message = describe(exc)
        if self._failures > 1:
            log.debug("marks.publish_still_failing", failures=self._failures, error=message)
            return
        log.warning("marks.publish_failed", error=message)
        self._emit(
            "warning",
            f"mark publisher pass failed: {message}",
            {"error_type": type(exc).__name__},
            run_id,
        )

    def _succeeded(self, run_id: int, now: datetime, marks: int) -> None:
        if marks:
            self._written_at, self._symbols = now, marks
        failures, self._failures = self._failures, 0
        if not failures:
            return
        log.info("marks.publish_recovered", failures=failures)
        self._emit("info", f"marks recovered after {failures} failures", {"failures": failures}, run_id)

    def _emit(self, level: str, message: str, data: dict[str, Any], run_id: int | None) -> None:
        try:
            (self.deps.event or _log_only)(level, message, data, run_id)
        except Exception as exc:  # the writer should never raise; a failing one never stops the publisher
            log.warning("marks.event_failed", error_type=type(exc).__name__)

    def _loop_failed(self, where: str, exc: Exception) -> None:
        if self._loop_failing:
            return
        self._loop_failing = True
        log.warning("marks.publish_skipped", where=where, error=describe(exc))

    async def _sleep_or_stop(self, seconds: float, stop: asyncio.Event) -> None:
        if stop.is_set():
            return
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waiter):
                task.cancel()
            await asyncio.gather(sleeper, waiter, return_exceptions=True)
