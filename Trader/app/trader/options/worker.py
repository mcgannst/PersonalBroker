"""The options worker (`python -m trader.options.worker [--once]`; OPTSIM task plan T12): the always-on
process of the options simulation.

One loop, one step at a time. In the session a step runs every `options.quote_poll_seconds`: it fires the
strategies' due events, polls the working orders (each fill: its message, then the strategy's `on_fill`),
walks the walking limits every `options.reprice_seconds`, and every `options.mark_seconds` records the
marks of the open contracts, submits the due take-profits, raises the strike-touched alerts and writes an
equity snapshot when `options.snapshot_seconds` have passed. In and out of the session every step delivers
the owner's answers, syncs and sends the owner prompts and writes the heartbeat. The first step after the
close expires the day orders. Out of the session there is one step every 30 s.

It holds no strategy logic and names no plug-in: everything a strategy does goes through the host.

Nothing needed after a restart is kept in memory. Timers restart from "due now"; a fill is one database
transaction in the broker; messages are deduplicated by their key; a strategy event runs once per session
through its job row; the snapshot spacing is read from the last stored row. What IS in memory only saves
repeats: the failure streaks, the back-off of a failing strategy event and the strike alerts already sent.

Every part of a step is guarded: a failure is one `error` event when its streak starts, one `critical`
event at ten failures in a row and one `info` event when it recovers, and the other parts still run.

Exit codes: 1 setup failed, 2 another options worker holds the lock (after sleeping 30 s, so supervisord
does not spin), 3 the lock was lost to another worker, 4 the active options run changed (a restart builds
everything on the new run).
"""

import argparse
import asyncio
import functools
import hashlib
import importlib
import os
import signal
import socket
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

import sqlalchemy
import structlog
from sqlalchemy import Connection, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader import logging_setup
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.jobs.runner import JobOutcome
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.notify.types import Notifier
from trader.option_strategies.base import OptionEvent
from trader.options.account import active_options_run_id
from trader.options.protocols import OptionBroker, OptionRenderer, StrategyHost
from trader.options.settings import OptionSettings
from trader.options.types import OptionFillEvent, StructureView

log = structlog.get_logger("options.worker")

LOCK_NAME = "trader.options_worker"
HEARTBEAT_PROCESS = "options-worker"  # the API's /options/account reads this exact name
EVENT_SOURCE = "options.worker"
RUNTIME_MODULE = "trader.options.runtime"  # T16: `build_worker_deps(core, stack)`
EXIT_SETUP_FAILED = 1
EXIT_LOCK_HELD = 2
EXIT_LOCK_LOST = 3
EXIT_RUN_CHANGED = 4
LOCK_HELD_SLEEP_SECONDS = 30.0  # a refused second worker waits this long before it exits
IDLE_STEP_SECONDS = 30.0
FAILED_STEPS_CRITICAL = 10  # consecutive failures of one part that raise one critical event
EVENT_RETRY_SECONDS = 60.0  # a failed strategy event is offered again after this long, doubling each time
EVENT_RETRY_MAX_SECONDS = 900.0
STRIKE_TOUCHED = "STRIKE_TOUCHED"
MAX_ERROR_CHARS = 500
Q4 = Decimal("0.0001")
ZERO = Decimal(0)


class PromptSending(Protocol):
    """What the worker needs of `trader.options.prompts.PromptSender`."""

    async def send_due(self, now: datetime) -> int: ...


@dataclass(frozen=True)
class OptionsWorkerDeps:
    """`engine` holds the single-instance lock (one dedicated connection). `run_id` is the active options
    run the broker and host were built for (None: there was none); the worker stops with exit 4 when the
    active run is no longer that one. `record_marks(contract_ids)` is the market service's."""

    factory: sessionmaker[Session]
    engine: sqlalchemy.Engine
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], OptionSettings]
    run_id: int | None
    broker: OptionBroker
    record_marks: Callable[[Sequence[int]], Awaitable[int]]
    host: StrategyHost
    prompt_sender: PromptSending
    notifier: Notifier
    renderer: OptionRenderer
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True, slots=True)
class StepReport:
    """One step. `parts` names the parts that ran, in order (a failed part is named too); `events` is
    (strategy, event key, outcome status) for each event offered to the host."""

    now: datetime
    phase: str  # "session" | "idle"
    parts: tuple[str, ...]
    events: tuple[tuple[str, str, str], ...] = ()
    fills: int = 0


# --- the single-instance lock -------------------------------------------------------------------------------


def _lock_key() -> int:
    digest = hashlib.blake2b(LOCK_NAME.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def acquire_single_instance(engine: sqlalchemy.Engine) -> Connection | None:
    """Take the options worker's session advisory lock on a dedicated connection held for the process
    lifetime; None when another options worker holds it."""
    conn = engine.connect()
    try:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        granted = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_key()}).scalar_one())
    except BaseException:
        conn.invalidate()  # the lock may have been granted before the error: never pool it
        conn.close()
        raise
    if not granted:
        conn.close()
        return None
    return conn


def release_single_instance(conn: Connection) -> None:
    """Release the lock and close its connection (discarded if the unlock fails)."""
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key()})
    except Exception as exc:
        log.warning("options_worker.unlock_failed", error=_describe(exc))
        conn.invalidate()
    finally:
        conn.close()


def still_holds_lock(conn: Connection) -> bool:
    """Whether `conn` still holds the lock (a re-entrant try-lock, then an unlock of the extra level).
    Raises when the connection is dead."""
    granted = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_key()}).scalar_one())
    if granted:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key()})
    return granted


def _describe(exc: BaseException) -> str:
    """An exception as one masked, capped text (it goes into event_log, which the web app shows)."""
    return logging_setup.redact_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS]


def _install_signal_handlers(stop: asyncio.Event) -> list[Callable[[], object]]:
    """SIGTERM and SIGINT set `stop`. Returns the removers (none where the loop can't take handlers)."""
    loop = asyncio.get_running_loop()
    removers: list[Callable[[], object]] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        removers.append(functools.partial(loop.remove_signal_handler, sig))
    return removers


def touched(structure: StructureView, price: Decimal, contract_id: int) -> bool:
    """Whether `price` (the underlying's last trade) is at or through the strike of the structure's short
    position in that contract: at or below a short put's strike, at or above a short call's."""
    for p in structure.positions:
        if p.contract is None or p.contract.id != contract_id or p.qty >= 0:
            continue
        return price <= p.contract.strike if p.contract.right == "put" else price >= p.contract.strike
    return False


class OptionsWorker:
    def __init__(self, deps: OptionsWorkerDeps) -> None:
        self.deps = deps
        self._lock: Connection | None = None
        self._lock_lost = False
        self._exit_code: int | None = None
        self._stop: asyncio.Event | None = None
        self._started_at: datetime | None = None
        self._defaults_done = False
        self._next: dict[str, datetime] = {}  # timers; a missing one is due now
        self._expired_for: date | None = None
        self._streaks: dict[str, int] = {}  # consecutive failures per part
        self._event_retry: dict[tuple[str, str, date], tuple[int, datetime]] = {}  # failures, not before
        self._touched: set[tuple[int, date]] = set()  # strike alerts raised (structure, session)
        self._good_settings: OptionSettings | None = None
        self._settings_failing = False
        self._beat_failing = False
        self._phase = "idle"
        self._session: date | None = None
        self._fills_today = 0
        self._fills_day: date | None = None
        self._last_event: str | None = None
        self._ran: list[str] = []

    # --- one iteration --------------------------------------------------------------------------------------

    async def step(self) -> StepReport:
        d = self.deps
        now = d.clock.now()
        day = et_date(now)
        self._ran = []
        self._phase, self._session = "idle", None
        if self._lock is not None and not self._check_lock():
            return StepReport(now, "idle", ())
        if not self._defaults_done:
            self._defaults_done = await self._part("ensure_defaults", self._ensure_defaults, False)
        if not self._run_is_ours():
            self._beat(self._phase)
            return StepReport(now, "idle", tuple(self._ran))
        settings = self._settings()
        hours = self._hours(day)
        events: list[tuple[str, str, str]] = []
        fills = 0
        if hours is not None:
            self._session = day
        if hours is not None and hours[0] <= now < hours[1]:
            self._phase = "session"
            events = await self._fire_due(day, now)
            fills = await self._poll(day, now)
            if self._due("walk", now, settings.reprice_seconds):
                await self._part("walk", lambda: d.broker.walk(now), 0)
            if self._due("marks", now, settings.mark_seconds):
                await self._mark_pass(day, now, settings)
        elif hours is not None and now >= hours[1] and self._expired_for != day:
            # Kept in memory only: after a restart it runs once more, and expiring twice changes nothing.
            if await self._part("expire_day_orders", lambda: self._expire(day, now), False):
                self._expired_for = day
        await self._part("deliver_answers", d.host.deliver_answers, 0)
        await self._part("sync_prompts", d.host.sync_prompts, 0)
        await self._part("send_prompts", lambda: d.prompt_sender.send_due(now), 0)
        self._beat(self._phase)
        return StepReport(now, self._phase, tuple(self._ran), tuple(events), fills)

    async def _part[T](
        self, name: str, fn: Callable[[], Awaitable[T]], default: T, *, streak: str | None = None, **f: Any
    ) -> T:
        """Run one part of the step on its own: a failure is recorded and `default` returned. Once the
        worker is stopping the remaining parts are skipped, so a stop never waits for a whole step."""
        if self._stop is not None and self._stop.is_set():
            return default
        self._ran.append(name)
        try:
            result = await fn()
        except Exception as exc:
            self._failed(name, exc, streak=streak, **f)
            return default
        self._ok(name, streak=streak, **f)
        return result

    async def _ensure_defaults(self) -> bool:
        await self.deps.host.ensure_defaults()
        return True

    def _run_is_ours(self) -> bool:
        """True when there is an active options run and it is the one this worker was built for. No run:
        idle. Another run (or one where there was none): stop with exit 4 so a restart builds on it."""
        try:
            active = active_options_run_id(self.deps.factory)
        except Exception as exc:
            self._failed("options_run", exc)
            return False
        self._ok("options_run")
        if active == self.deps.run_id:
            return active is not None
        log.warning("options_worker.run_changed", built_for=self.deps.run_id, active=active)
        self._event(
            "warning",
            "the active options run changed; the options worker restarts",
            {"built_for": self.deps.run_id, "active": active},
        )
        self._exit_code = EXIT_RUN_CHANGED
        if self._stop is not None:
            self._stop.set()
        return False

    def _hours(self, day: date) -> tuple[datetime, datetime] | None:
        """The open and close of that ET date, or None when it is not a session."""
        cal = self.deps.calendar
        try:
            if not cal.is_session(day):
                return None
            return cal.session_open(day), cal.session_close(day)
        except ValueError:  # outside the calendar's range
            return None

    def _due(self, timer: str, now: datetime, every_seconds: float) -> bool:
        at = self._next.get(timer)
        if at is not None and now < at:
            return False
        self._next[timer] = now + timedelta(seconds=every_seconds)
        return True

    # --- strategy events ------------------------------------------------------------------------------------

    async def _fire_due(self, day: date, now: datetime) -> list[tuple[str, str, str]]:
        """Offer every due event to the host, in its order. An event that failed (or raised) is not offered
        again until its back-off has passed: the host records an error for every failed run."""
        host = self.deps.host
        nothing: list[tuple[str, OptionEvent]] = []
        due = await self._part("due_events", lambda: host.due_events(day, now), nothing)
        results: list[tuple[str, str, str]] = []
        for key, event in due:
            ref = (key, event.key, event.session_date)
            failures, not_before = self._event_retry.get(ref, (0, now))
            if now < not_before:
                continue
            fire: Callable[[], Awaitable[JobOutcome | None]] = functools.partial(host.fire, key, event)
            outcome = await self._part(
                "fire", fire, None, streak=f"fire:{key}:{event.key}", strategy=key, key=event.key
            )
            status = outcome.status if outcome is not None else "failed"
            results.append((key, event.key, status))
            if status == "succeeded":
                self._event_retry.pop(ref, None)
                self._last_event = f"{key}:{event.key}"
                continue
            wait = min(EVENT_RETRY_SECONDS * 2**failures, EVENT_RETRY_MAX_SECONDS)
            self._event_retry[ref] = (failures + 1, now + timedelta(seconds=wait))
        return results

    # --- orders ---------------------------------------------------------------------------------------------

    async def _poll(self, day: date, now: datetime) -> int:
        d = self.deps
        none: list[OptionFillEvent] = []
        fills = await self._part("poll", lambda: d.broker.poll(now), none)
        if self._fills_day != day:
            self._fills_day, self._fills_today = day, 0
        self._fills_today += len(fills)
        for fill in fills:
            await self._part("fill_message", functools.partial(self._fill_message, fill), None)
            await self._part("deliver_fill", functools.partial(d.host.deliver_fill, fill), None)
        return len(fills)

    async def _fill_message(self, fill: OptionFillEvent) -> None:
        """The fill's message (dedupe key `opt:fill:<order id>`), with the structure as it is now."""
        d = self.deps
        structures = await d.broker.structures(source=fill.source, open_only=False)
        structure = next((s for s in structures if s.id == fill.structure_id), None)
        if structure is None:
            raise LookupError(f"structure {fill.structure_id} of filled order {fill.order_id} was not found")
        await d.notifier.send(d.renderer.fill(fill, structure))

    async def _expire(self, day: date, now: datetime) -> bool:
        expired = await self.deps.broker.expire_day_orders(day, now)
        if expired:
            self._event("info", f"{expired} day order(s) expired at the close", {"expired": expired})
        return True

    # --- marks, take-profits, alerts, snapshots -------------------------------------------------------------

    async def _mark_pass(self, day: date, now: datetime, settings: OptionSettings) -> None:
        d = self.deps
        no_structures: list[StructureView] = []
        no_orders: list[int] = []
        structures = await self._part("structures", d.broker.structures, no_structures)
        shorts: dict[int, list[StructureView]] = {}
        ids: set[int] = set()
        for structure in structures:
            for p in structure.positions:
                if p.contract is None or p.qty == 0:
                    continue
                ids.add(p.contract.id)
                if p.qty < 0:
                    shorts.setdefault(p.contract.id, []).append(structure)
        if ids:
            await self._part("record_marks", lambda: d.record_marks(sorted(ids)), 0)
        await self._part("take_profits", lambda: d.broker.take_profits(now), no_orders)
        if settings.strike_touch_alerts and shorts:
            await self._part("strike_touch", lambda: self._strike_touch(day, now, settings, shorts), None)
        await self._part("snapshot", lambda: self._snapshot(now, settings), False)

    async def _strike_touch(
        self, day: date, now: datetime, settings: OptionSettings, shorts: dict[int, list[StructureView]]
    ) -> None:
        """One alert per structure per session when the underlying's last trade (stored with the contract's
        mark by `record_marks`) is at or through a short option's strike. A mark older than one mark
        interval is not used."""
        d = self.deps
        fresh = now - timedelta(seconds=settings.mark_seconds)
        mark = m.OptionQuoteMark
        with d.factory() as s:
            prices = {
                int(cid): price
                for cid, price in s.execute(
                    select(mark.contract_id, mark.underlying_price).where(
                        mark.contract_id.in_(sorted(shorts)),
                        mark.underlying_price.is_not(None),
                        mark.fetched_at >= fresh,
                    )
                )
            }
        for contract_id, price in sorted(prices.items()):
            for structure in shorts[contract_id]:
                ref = (structure.id, day)
                if ref in self._touched or not touched(structure, price, contract_id):
                    continue
                contract = next(
                    p.contract for p in structure.positions if p.contract and p.contract.id == contract_id
                )
                message = (
                    f"{structure.underlying} traded at {price.normalize():f}: the short {contract.right} "
                    f"{contract.strike.normalize():f} expiring {contract.expiry.isoformat()} "
                    f"(structure {structure.id}) is at or through its strike"
                )
                msg = d.renderer.alert(
                    structure.source, STRIKE_TOUCHED, message, f"strike_touched:{structure.id}:{day}"
                )
                if not self._already_sent(msg.dedupe_key):  # a restart: said earlier this session
                    self._event(
                        "warning",
                        message,
                        {
                            "kind": STRIKE_TOUCHED,
                            "underlying": structure.underlying,
                            "structure_id": structure.id,
                            "strategy": structure.source,
                        },
                    )
                    await d.notifier.send(msg)
                self._touched.add(ref)

    def _already_sent(self, dedupe_key: str | None) -> bool:
        if dedupe_key is None:
            return False
        with self.deps.factory() as s:
            found = s.execute(
                select(m.Notification.id).where(m.Notification.dedupe_key == dedupe_key).limit(1)
            ).scalar_one_or_none()
        return found is not None

    async def _snapshot(self, now: datetime, settings: OptionSettings) -> bool:
        """One equity snapshot when the run's last one is at least `options.snapshot_seconds` old (read
        from the table, so a restart keeps the spacing). False when it was not yet time."""
        d = self.deps
        run_id = d.run_id
        assert run_id is not None  # the step returned earlier without a run
        snap = m.EquitySnapshot
        with d.factory() as s:
            last, peak_before = s.execute(
                select(func.max(snap.ts), func.max(snap.peak_equity)).where(snap.run_id == run_id)
            ).one()
        if last is not None and (now - last).total_seconds() < settings.snapshot_seconds:
            return False
        account = await d.broker.account()
        peak = account.equity if peak_before is None else max(account.equity, peak_before)
        drawdown = ((peak - account.equity) / peak).quantize(Q4, ROUND_HALF_UP) if peak > 0 else ZERO
        stmt = insert(snap).values(
            run_id=run_id,
            ts=now,
            equity=account.equity,
            cash=account.cash,
            settled_cash=account.cash,
            peak_equity=peak,
            drawdown_pct=drawdown,
        )
        with session_scope(d.factory) as s:
            s.execute(stmt.on_conflict_do_nothing(index_elements=[snap.run_id, snap.ts]))
        return True

    # --- errors: one event per failure streak ---------------------------------------------------------------

    def _failed(self, part: str, exc: Exception, *, streak: str | None = None, **fields: Any) -> None:
        """Count a failure of `part`. The first of a streak writes one `error` event and logs the
        traceback; the FAILED_STEPS_CRITICAL-th writes one `critical` event; the rest only log a line."""
        key = streak or part
        n = self._streaks.get(key, 0) + 1
        self._streaks[key] = n
        detail = _describe(exc)
        if n == 1:
            log.error("options_worker.part_failed", part=part, exc_info=exc, **fields)
            self._event("error", f"options worker {part} failed: {detail}", {"part": part, **fields})
        else:
            log.warning("options_worker.part_still_failing", part=part, failures=n, error=detail, **fields)
        if n == FAILED_STEPS_CRITICAL:
            log.critical("options_worker.failing", part=part, failures=n, **fields)
            self._event(
                "critical",
                f"options worker {part} failed {n} times in a row: {detail}",
                {"part": part, "failures": n, **fields},
            )

    def _ok(self, part: str, *, streak: str | None = None, **fields: Any) -> None:
        """A success of `part`: ends its failure streak with one `info` event."""
        n = self._streaks.pop(streak or part, 0)
        if n:
            log.info("options_worker.part_recovered", part=part, failures=n, **fields)
            self._event(
                "info",
                f"options worker {part} recovered after {n} failures",
                {"part": part, "failures": n, **fields},
            )

    def _event(self, level: str, message: str, data: dict[str, Any]) -> None:
        d = self.deps
        try:
            with session_scope(d.factory) as s:
                log_event(s, d.clock, level, EVENT_SOURCE, message, data, d.run_id)
        except Exception as exc:
            log.warning("options_worker.event_failed", level=level, error=_describe(exc))

    def _settings(self) -> OptionSettings:
        """The option settings; on a failed read the last good ones (the defaults if there never was one).
        A failure streak logs one warning."""
        try:
            settings = self.deps.settings()
        except Exception as exc:
            if not self._settings_failing:
                self._settings_failing = True
                log.warning("options_worker.settings_unavailable", error=_describe(exc))
            return self._good_settings if self._good_settings is not None else OptionSettings()
        if self._settings_failing:
            self._settings_failing = False
            log.info("options_worker.settings_recovered")
        self._good_settings = settings
        return settings

    # --- heartbeat and lock ---------------------------------------------------------------------------------

    def _beat(self, phase: str) -> None:
        if self._lock_lost:
            return  # another worker owns the row now
        d = self.deps
        now = d.clock.now()
        values: dict[str, Any] = {
            "process": HEARTBEAT_PROCESS,
            "pid": os.getpid(),
            "host": socket.gethostname()[:100],
            "started_at": self._started_at or now,
            "beat_at": now,
            "session_date": self._session,
            "phase": phase,
            "detail": {"run_id": d.run_id, "fills_today": self._fills_today, "last_event": self._last_event},
        }
        stmt = insert(m.WorkerHeartbeat).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.WorkerHeartbeat.process],
            set_={k: stmt.excluded[k] for k in values if k != "process"},
        )
        try:
            with session_scope(d.factory) as s:
                s.execute(stmt)
        except Exception as exc:
            if not self._beat_failing:  # once per failure streak, not on every retry
                self._beat_failing = True
                log.error("options_worker.heartbeat_failed", phase=phase, error=_describe(exc))
            return
        if self._beat_failing:
            self._beat_failing = False
            log.info("options_worker.heartbeat_recovered", phase=phase)

    def _check_lock(self) -> bool:
        """True while this worker holds the single-instance lock. A dead lock connection is replaced by a
        new one that re-takes the lock; when another worker holds it: one critical event, stop, exit 3.
        When the database can't be reached at all, try again next time."""
        if self._lock is not None:
            try:
                if still_holds_lock(self._lock):
                    return True
            except Exception as exc:
                log.warning("options_worker.lock_connection_lost", error=_describe(exc))
            else:
                return self._lost_lock()  # the connection lives but another session holds the lock
            try:
                self._lock.invalidate()
                self._lock.close()
            except Exception as exc:
                log.debug("options_worker.lock_close_failed", error=_describe(exc))
            self._lock = None
        try:
            conn = acquire_single_instance(self.deps.engine)
        except Exception as exc:
            log.warning("options_worker.lock_retake_failed", error=_describe(exc))
            return False  # nobody can take it while the database is unreachable
        if conn is None:
            return self._lost_lock()
        self._lock = conn
        log.warning("options_worker.lock_retaken")
        self._event("warning", "options worker lock connection was lost; the lock was taken again", {})
        return True

    def _lost_lock(self) -> bool:
        self._lock_lost = True
        self._exit_code = EXIT_LOCK_LOST
        log.critical("options_worker.lock_lost")
        self._event(
            "critical", "options worker lost its single-instance lock to another worker; stopping", {}
        )
        if self._stop is not None:
            self._stop.set()
        return False

    # --- the loop -------------------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event, *, once: bool = False) -> None:
        """Run until `stop` is set (SIGTERM and SIGINT set it), or one step with `once`. A second options
        worker sleeps 30 s and exits with SystemExit(2) having done nothing else; one that loses its lock
        stops with SystemExit(3); one whose options run changed with SystemExit(4)."""
        d = self.deps
        lock = acquire_single_instance(d.engine)
        if lock is None:
            log.critical("options_worker.already_running")
            await d.sleep(LOCK_HELD_SLEEP_SECONDS)
            raise SystemExit(EXIT_LOCK_HELD)
        self._lock = lock
        self._stop = stop
        removers: list[Callable[[], object]] = []
        try:
            self._started_at = d.clock.now()
            self._beat("starting")
            if once:
                await self.step()
                return
            removers = _install_signal_handlers(stop)
            while not stop.is_set():
                await self.step()
                await self._pause(self._interval(), stop)
        finally:
            stop.set()
            try:
                self._beat("stopped")
            finally:
                try:
                    if self._lock is not None:
                        release_single_instance(self._lock)
                        self._lock = None
                finally:
                    for remove in removers:
                        try:
                            remove()
                        except Exception as exc:
                            log.warning("options_worker.signal_restore_failed", error=_describe(exc))
        if self._exit_code is not None:
            raise SystemExit(self._exit_code)

    def _interval(self) -> float:
        """Seconds until the next step: the quote poll in the session, else 30 s, or less to be on time
        for the open."""
        if self._phase == "session":
            return float(self._settings().quote_poll_seconds)
        now = self.deps.clock.now()
        hours = self._hours(et_date(now))
        if hours is not None and now < hours[0]:
            return min(IDLE_STEP_SECONDS, max((hours[0] - now).total_seconds(), 1.0))
        return IDLE_STEP_SECONDS

    async def _pause(self, seconds: float, stop: asyncio.Event) -> None:
        """Sleep until the next step, waking every `options.heartbeat_seconds` to re-check the lock and
        beat, and at once when `stop` is set."""
        remaining = seconds
        while remaining > 0 and not stop.is_set():
            chunk = min(remaining, float(self._settings().heartbeat_seconds))
            await self._sleep_or_stop(chunk, stop)
            remaining -= chunk
            if remaining > 0 and not stop.is_set() and self._check_lock():
                self._beat(self._phase)

    async def _sleep_or_stop(self, seconds: float, stop: asyncio.Event) -> None:
        sleeper = asyncio.ensure_future(self.deps.sleep(seconds))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waiter):
                task.cancel()
            await asyncio.gather(sleeper, waiter, return_exceptions=True)


# --- the command line ---------------------------------------------------------------------------------------


async def run_event_cli(
    host: StrategyHost,
    *,
    strategy: str | None,
    key: str | None,
    session_date: date,
    force: bool,
    due: bool,
    now: datetime,
) -> int:
    """`trader options-event`: fire every due and unfired event (`due`), or the one named. Prints one line
    per event. Exit code 0, 1 when an event failed, 2 for an unknown strategy or a bad event key."""
    if due:
        events = await host.due_events(session_date, now)
    elif strategy is not None and key is not None:
        events = [(strategy, OptionEvent(key, session_date, scheduled=False))]
    else:
        print("options-event: give STRATEGY and KEY, or --due")
        return 2
    if not events:
        print(f"options-event: nothing is due for {session_date.isoformat()}")
        return 0
    code = 0
    for strategy_key, event in events:
        name = f"{strategy_key} {event.key} {event.session_date.isoformat()}"
        try:
            outcome = await host.fire(strategy_key, event, force=force and not due)
        except KeyError:
            print(f"options-event: {name}: unknown strategy")
            return 2
        except ValueError as exc:
            print(f"options-event: {name}: {exc}")
            return 2
        if outcome.status == "failed":
            code = 1
            print(f"options-event: {name}: failed: {outcome.error or ''}".rstrip())
        elif outcome.status == "skipped":
            print(f"options-event: {name}: skipped: {outcome.detail.get('reason', '')}".rstrip())
        else:
            print(f"options-event: {name}: succeeded {outcome.detail}")
    return code


async def _run(once: bool) -> int:
    """Build the worker's dependencies with `trader.options.runtime.build_worker_deps(core, stack)` and
    run it. The stack is closed after the worker stopped."""
    from trader import bootstrap  # late: `--help` and the tests never build a core

    runtime = importlib.import_module(RUNTIME_MODULE)
    core = bootstrap.build_core()
    async with AsyncExitStack() as stack:
        deps: OptionsWorkerDeps = await runtime.build_worker_deps(core, stack)
        await OptionsWorker(deps).run(asyncio.Event(), once=once)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m trader.options.worker [--once]`. Returns the exit code (see the module text)."""
    logging_setup.configure_logging(HEARTBEAT_PROCESS)  # first, before anything can log
    parser = argparse.ArgumentParser(prog="python -m trader.options.worker", description="Options worker.")
    parser.add_argument("--once", action="store_true", help="run one step and exit (smoke check)")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.once))
    except SystemExit as exc:  # 2: a second worker; 3: the lock was lost; 4: the options run changed
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else EXIT_SETUP_FAILED
    except Exception as exc:  # setup failed (configuration, database, wiring): one line, no traceback
        log.error(
            "options_worker.setup_failed",
            error_type=type(exc).__name__,
            error=logging_setup.redact_text(str(exc))[:MAX_ERROR_CHARS],
        )
        return EXIT_SETUP_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
