"""The long-running worker (`python -m trader.worker [--once]`): fires session events on time, polls quotes
for working orders, expires proposals, relays notifications, runs the Telegram bot, ends the session,
writes a heartbeat, and refuses to run twice (SPEC §1, §6, §7.2, §9; BR-31, BR-42).

Nothing is held in memory that the database doesn't have: which events are settled comes from `fired`
(job_runs), the relay resumes from its cursors, the engine re-reads working orders and pending proposals.
So a worker restarted mid-session carries on without firing or sending anything twice. Every part of a
step is guarded: an exception is logged and the step goes on; the loop itself never dies on the engine,
the relay, the settings or the database.

Four asyncio tasks run side by side, so none can delay another (Telegram never blocks trading):
- the step loop (events, quotes, ticks, the session end) at `quote_poll_seconds` in the session;
- the relay loop (`relay()`, which talks to Telegram) at the same cadence, with a last pump on stop;
- the heartbeat loop, every `worker.heartbeat_seconds`, which also re-checks the single-instance lock;
- the bot (long polling), restarted 30 s after it dies.
- (P6-T11) the decision log loop (`trader.decisions.loop.DecisionsLoop`), when given: logging only, its
  database work in worker threads, cancelled on stop without a grace period.
- (DB-T2) the mark publisher (`trader.marks.publisher.MarkPublisher`), when given: logging only (the live
  dashboard's `quote_marks`/`mark_bars`), its database work in its own thread, cancelled on stop without a
  grace period.

Failure alerts (fix round 1): a part that fails writes ONE `error` event when its failure streak starts,
one `critical` event at FAILED_STEPS_CRITICAL consecutive failures, and one `info` "recovered after N
failures" event when it next succeeds; nothing per step in between (the relay turns every error event
into a phone alert).
"""

import argparse
import asyncio
import functools
import hashlib
import json
import os
import signal
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy
import structlog
from sqlalchemy import Connection, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader import logging_setup
from trader.db.models import WorkerHeartbeat
from trader.db.session import session_scope
from trader.decisions.loop import DecisionsLoop
from trader.engine.scheduler import DayPlan, FireResult, due_events
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.market.sessions import SessionPhase, session_phase
from trader.marks.publisher import MarkPublisher
from trader.notify.notifier import settle_interrupted_sends
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("worker")

LOCK_NAME = "trader.worker"
PRE_OPEN_LEAD = timedelta(seconds=60)  # the session loop starts this long before the open
BOT_RESTART_SECONDS = 30.0  # a dead bot (or relay/heartbeat loop) is restarted after this long
FAILED_STEPS_CRITICAL = 10  # consecutive failures of one part that raise one critical event
BOT_STOP_GRACE_SECONDS = 5.0  # on top of one Telegram poll timeout
RELAY_STOP_SECONDS = 15.0  # the relay's last pump on stop may take this long (real time) at most
MAX_ERROR_CHARS = 500
EXIT_LOCK_LOST = 3  # `run` exits with this code when another worker took the lock
EXIT_SETUP_FAILED = 1  # `main`: building the worker failed (one log line, no traceback)
LIVE_RUN_CHECK_SECONDS = 60.0  # idle steps re-read the live run this often (clock seconds), FIX-401 (g)


class WorkerEngine(Protocol):
    """What the worker drives. P2's Engine satisfies it (it returns EventResult, list[FillEvent] and
    list[PositionView]; Sequence[Any] keeps this protocol free of those types)."""

    async def run_event(self, event_key: str, session_date: date) -> Any: ...

    async def poll_quotes(self) -> Sequence[Any]: ...

    async def tick(self, now: datetime) -> None: ...

    async def end_of_session(self, session_date: date) -> Sequence[Any]: ...


@dataclass(frozen=True)
class WorkerDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    engine_for: Callable[[date], Awaitable[WorkerEngine]]  # one engine per session
    plan: Callable[[date], DayPlan]
    fire: Callable[[str, date], Awaitable[FireResult]]
    fired: Callable[[date], set[str]]
    relay: Callable[[], Awaitable[Any]] | None
    bot: Callable[[asyncio.Event], Awaitable[None]] | None
    # (session_date, body) -> outcome; T12 passes run_job_async with job `session_end`.
    end_session: Callable[[date, Callable[[], Awaitable[dict[str, Any]]]], Awaitable[Any]]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    process: str = "worker"
    host: str = ""
    # P4-T18: extra keys merged into the heartbeat `detail` (the Questrade rate-limit numbers the System page
    # shows). The P3 keys win on a clash; a failing or unserialisable result is skipped (logged per streak).
    heartbeat_extra: Callable[[], Mapping[str, Any]] | None = None
    # P6-T11: the decision log's refresh loop, run beside the relay. Its database work runs off the event
    # loop and it never raises. None: the worker records no decisions.
    decisions: DecisionsLoop | None = None
    # DB-T2 (live dashboard): the mark publisher, run beside the decisions loop. Its database work runs in its
    # own thread and it never raises. None: no marks are written.
    marks: MarkPublisher | None = None


@dataclass(frozen=True, slots=True)
class StepReport:
    """One step. `relayed` is kept for the contract but is always False since fix round 1: the relay
    runs as its own task beside the step loop (a `--once` run pumps it once after its step)."""

    now: datetime
    phase: SessionPhase
    fired: list[FireResult]
    fills: int
    relayed: bool
    ended: bool


def _lock_key() -> int:
    digest = hashlib.blake2b(LOCK_NAME.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def acquire_single_instance(engine: sqlalchemy.Engine) -> Connection | None:
    """Take the worker's session advisory lock on a dedicated connection held for the process lifetime;
    None when another worker holds it."""
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
    """Release the lock and close its connection. If the unlock fails the connection is discarded, so it
    never goes back to the pool still holding the lock."""
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key()})
    except Exception as exc:
        log.warning("worker.unlock_failed", error=_describe(exc))
        conn.invalidate()
    finally:
        conn.close()


def still_holds_lock(conn: Connection) -> bool:
    """Whether `conn` still holds the worker lock: a re-entrant pg_try_advisory_lock on the same
    connection (granted when this session holds it), then an unlock of the extra level. Raises when the
    connection is dead."""
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


class Worker:
    def __init__(self, deps: WorkerDeps, *, live_run_check: Callable[[], bool] | None = None) -> None:
        """`live_run_check` (FIX-401 g) re-reads the active live run: False when it changed (it then sets
        the stop event itself and the worker exits 4). Idle steps call it every LIVE_RUN_CHECK_SECONDS by
        the clock; in the session the runtime's `fired` and `engine_for` carry the check. None: no idle
        check. A constructor argument, so `marks` stays the last WorkerDeps field (DB-T2 contract)."""
        self.deps = deps
        self._live_run_check = live_run_check
        self._engine: WorkerEngine | None = None
        self._engine_session: date | None = None
        self._plan: DayPlan | None = None
        self._ended: date | None = None
        self._fills_today = 0
        self._last_event: str | None = None
        self._streaks: dict[str, int] = {}  # consecutive failures per part
        self._good_settings: RuntimeSettings | None = None
        self._settings_failing = False
        self._beat_failing = False
        self._extra_failing = False
        self._started_at: datetime | None = None
        self._hb_phase = "idle"
        self._hb_session: date | None = None
        self._lock: Connection | None = None
        self._lock_lost = False
        self._exit_code: int | None = None
        self._live_run_checked_at: datetime | None = None

    # --- one iteration --------------------------------------------------------------------------------

    async def step(self) -> StepReport:
        now = self.deps.clock.now()
        cal = self.deps.calendar
        phase = session_phase(cal, now)
        day = et_date(now)
        fired: list[FireResult] = []
        fills = 0
        ended = False
        if phase == "open" or (phase == "pre_market" and cal.session_open(day) - now <= PRE_OPEN_LEAD):
            self._hb_phase, self._hb_session = "session", day
            engine = await self._guarded_engine(day)
            fired = await self._fire_due(day, now)
            if engine is not None:
                fills = await self._poll(engine)
                await self._tick(engine, now)
        elif phase == "after_close" and self._ended != day:
            self._hb_phase, self._hb_session = "idle", day
            fired = await self._fire_due(day, now)  # due safety events first; `fire` decides
            await self._end_session(day)
            ended = True
        else:
            self._hb_phase = "idle"
            self._hb_session = day if phase != "closed_day" else None
            self._idle_live_run_check(now)
        return StepReport(now, phase, fired, fills, False, ended)

    def _idle_live_run_check(self, now: datetime) -> None:
        """FIX-401 (g): a live-run switch made while the worker idles (overnight, a weekend) is noticed
        within LIVE_RUN_CHECK_SECONDS, not at the next session. The check sets the stop event itself."""
        check = self._live_run_check
        if check is None:
            return
        last = self._live_run_checked_at
        if last is not None and (now - last).total_seconds() < LIVE_RUN_CHECK_SECONDS:
            return
        self._live_run_checked_at = now
        try:
            check()
        except Exception as exc:
            self._failed("live_run_check", exc)
        else:
            self._ok("live_run_check")

    async def _open_engine(self, day: date) -> WorkerEngine:
        """Today's engine, built once per session (rebuilt each session so settings changes apply)."""
        if self._engine is None or self._engine_session != day:
            self._engine = await self.deps.engine_for(day)
            self._engine_session = day
            self._fills_today = 0
        return self._engine

    async def _guarded_engine(self, day: date) -> WorkerEngine | None:
        try:
            engine = await self._open_engine(day)
        except Exception as exc:
            self._failed("engine", exc)
            return None
        self._ok("engine")
        return engine

    def _plan_for(self, day: date) -> DayPlan:
        if self._plan is None or self._plan.session_date != day:
            self._plan = self.deps.plan(day)
        return self._plan

    async def _fire_due(self, day: date, now: datetime) -> list[FireResult]:
        """Offer every due, unsettled event to `fire`, in time order. `fire` owns idempotency and
        lateness; `fired` reads the settled keys from the database."""
        try:
            due = due_events(self._plan_for(day), now, self.deps.fired(day))
        except Exception as exc:
            self._failed("plan", exc)
            return []
        self._ok("plan")
        results: list[FireResult] = []
        for event in due:
            streak = f"fire:{event.key}"
            try:
                result = await self.deps.fire(event.key, day)
            except Exception as exc:
                self._failed("fire", exc, streak=streak, key=event.key)
                continue
            self._ok("fire", streak=streak, key=event.key)
            results.append(result)
            if result.status == "fired":
                self._last_event = event.key
        return results

    async def _poll(self, engine: WorkerEngine) -> int:
        try:
            fills = len(await engine.poll_quotes())
        except Exception as exc:
            self._failed("poll_quotes", exc)
            return 0
        self._ok("poll_quotes")
        self._fills_today += fills
        return fills

    async def _tick(self, engine: WorkerEngine, now: datetime) -> None:
        try:
            await engine.tick(now)
        except Exception as exc:
            self._failed("tick", exc)
        else:
            self._ok("tick")

    async def _end_session(self, day: date) -> None:
        """Once per session day, whatever the outcome: `end_session` (run_job_async in T12) records it,
        and the 16:15 post-close job repeats the end-of-day cancels as the safety net."""

        async def body() -> dict[str, Any]:
            engine = await self._open_engine(day)  # lazily: a restart after the close has none yet
            still_open = await engine.end_of_session(day)
            return {"open_positions": len(still_open)}

        self._ended = day
        try:
            await self.deps.end_session(day, body)
        except Exception as exc:
            self._failed("end_session", exc)
        else:
            self._ok("end_session")

    async def _relay(self) -> bool:
        if self.deps.relay is None:
            return False
        try:
            await self.deps.relay()
        except Exception as exc:
            self._failed("relay", exc)
            return False
        self._ok("relay")
        return True

    # --- errors: one event per failure streak -----------------------------------------------------------

    def _failed(self, part: str, exc: Exception, *, streak: str | None = None, **fields: Any) -> None:
        """Count a failure of `part`. The first of a streak writes one `error` event and logs the
        traceback; the FAILED_STEPS_CRITICAL-th writes one `critical` event; the rest only log a line."""
        key = streak or part
        n = self._streaks.get(key, 0) + 1
        self._streaks[key] = n
        if part == "bot":
            # Telegram's client puts the token in URLs: log the type only, never the text or a traceback.
            detail = type(exc).__name__
        else:
            detail = _describe(exc)
        if n == 1:
            if part == "bot":
                log.error("worker.part_failed", part=part, error_type=detail, **fields)
            else:
                log.error("worker.part_failed", part=part, exc_info=exc, **fields)
            self._event("error", f"worker {part} failed: {detail}", {"part": part, **fields})
        else:
            log.warning("worker.part_still_failing", part=part, failures=n, error=detail, **fields)
        if n == FAILED_STEPS_CRITICAL:
            log.critical("worker.failing", part=part, failures=n, **fields)
            self._event(
                "critical",
                f"worker {part} failed {n} times in a row: {detail}",
                {"part": part, "failures": n, **fields},
            )

    def _ok(self, part: str, *, streak: str | None = None, **fields: Any) -> None:
        """A success of `part`: ends its failure streak with one `info` event."""
        n = self._streaks.pop(streak or part, 0)
        if n:
            log.info("worker.part_recovered", part=part, failures=n, **fields)
            self._event(
                "info", f"worker {part} recovered after {n} failures", {"part": part, "failures": n, **fields}
            )

    def _event(self, level: str, message: str, data: dict[str, Any]) -> None:
        try:
            with session_scope(self.deps.factory) as s:
                log_event(s, self.deps.clock, level, "worker", message, data)
        except Exception as exc:
            log.warning("worker.event_failed", level=level, error=_describe(exc))

    # --- settings ---------------------------------------------------------------------------------------

    def _settings(self) -> RuntimeSettings:
        """The runtime settings; on a failed read the last good ones (defaults if there never was a good
        read). A failure streak logs one warning."""
        try:
            settings = self.deps.settings()
        except Exception as exc:
            if not self._settings_failing:
                self._settings_failing = True
                log.warning(
                    "worker.settings_unavailable",
                    error=_describe(exc),
                    fallback="last_good" if self._good_settings is not None else "defaults",
                )
            return self._good_settings if self._good_settings is not None else RuntimeSettings()
        if self._settings_failing:
            self._settings_failing = False
            log.info("worker.settings_recovered")
        self._good_settings = settings
        return settings

    # --- heartbeat and lock -----------------------------------------------------------------------------

    def _beat(self, phase: str) -> None:
        now = self.deps.clock.now()
        values: dict[str, Any] = {
            "process": self.deps.process,
            "pid": os.getpid(),
            "host": (self.deps.host or socket.gethostname())[:100],
            "started_at": self._started_at or now,
            "beat_at": now,
            "session_date": self._hb_session,
            "phase": phase,
            "detail": {
                **self._heartbeat_extra(),
                "fills_today": self._fills_today,
                "last_event": self._last_event,
            },
        }
        stmt = insert(WorkerHeartbeat).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.process],
            set_={k: stmt.excluded[k] for k in values if k != "process"},
        )
        try:
            with session_scope(self.deps.factory) as s:
                s.execute(stmt)
        except Exception as exc:
            if not self._beat_failing:  # once per failure streak, not on every retry
                self._beat_failing = True
                log.error("worker.heartbeat_failed", phase=phase, error=_describe(exc))
            return
        if self._beat_failing:
            self._beat_failing = False
            log.info("worker.heartbeat_recovered", phase=phase)

    def _heartbeat_extra(self) -> dict[str, Any]:
        """`deps.heartbeat_extra()` as a JSON-safe dict; {} when there is none or it fails (one log line per
        failure streak, never the heartbeat itself)."""
        extra_fn = self.deps.heartbeat_extra
        if extra_fn is None:
            return {}
        try:
            extra = dict(extra_fn())
            # the detail column is jsonb: an unserialisable value, or a NaN/Infinity (which json.dumps
            # accepts by default and PostgreSQL refuses), would lose the beat
            json.dumps(extra, allow_nan=False)
        except Exception as exc:
            if not self._extra_failing:
                self._extra_failing = True
                log.warning("worker.heartbeat_extra_failed", error=_describe(exc))
            return {}
        if self._extra_failing:
            self._extra_failing = False
            log.info("worker.heartbeat_extra_recovered")
        return extra

    def _check_lock(self, stop: asyncio.Event) -> bool:
        """True while this worker holds the single-instance lock. A dead lock connection is replaced by a
        new one that re-takes the lock; when another worker holds it, write a critical event, set `stop`
        and remember the exit code. When the database can't be reached at all, try again next time."""
        if self._lock is not None:
            try:
                if still_holds_lock(self._lock):
                    return True
            except Exception as exc:
                log.warning("worker.lock_connection_lost", error=_describe(exc))
            else:
                return self._lost_lock(stop)  # the connection lives but another session holds the lock
            try:
                self._lock.invalidate()
                self._lock.close()
            except Exception as exc:
                log.debug("worker.lock_close_failed", error=_describe(exc))
            self._lock = None
        bind = self.deps.factory.kw.get("bind")
        if not isinstance(bind, sqlalchemy.Engine):
            return self._lost_lock(stop)
        try:
            conn = acquire_single_instance(bind)
        except Exception as exc:
            log.warning("worker.lock_retake_failed", error=_describe(exc))
            return False  # nobody can take it while the database is unreachable; retry next heartbeat
        if conn is None:
            return self._lost_lock(stop)
        self._lock = conn
        log.warning("worker.lock_retaken")
        self._event("warning", "worker lock connection was lost; the lock was taken again", {})
        return True

    def _lost_lock(self, stop: asyncio.Event) -> bool:
        self._lock_lost = True
        self._exit_code = EXIT_LOCK_LOST
        log.critical("worker.lock_lost", process=self.deps.process)
        self._event("critical", "worker lost its single-instance lock to another worker; stopping", {})
        stop.set()
        return False

    def recover_after_lock(self) -> None:
        """Once the single-instance lock is held (P5-GO fix round 1): notifications a dead process left
        `sending` (claimed, then killed mid-send) are settled `unknown` so the System page lists them as
        undelivered; they are never re-sent. Best effort: a failure is logged and the worker starts."""
        try:
            settled = settle_interrupted_sends(self.deps.factory, self.deps.clock)
        except Exception as exc:
            log.error("worker.settle_sends_failed", error=_describe(exc))
            return
        if settled:
            log.warning("worker.interrupted_sends_settled", count=settled)

    # --- the loop ---------------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event, *, once: bool = False) -> None:
        """Run until `stop` is set (SIGTERM and SIGINT set it), or one step (plus one relay pump) with
        `once`, which never starts the bot. A second worker exits at once with SystemExit(2), before doing
        anything else; a worker that loses its lock to another stops cleanly with SystemExit(3)."""
        bind = self.deps.factory.kw.get("bind")
        if not isinstance(bind, sqlalchemy.Engine):
            raise TypeError("the worker needs a sessionmaker bound to an Engine")
        lock = acquire_single_instance(bind)
        if lock is None:
            log.critical("worker.already_running", process=self.deps.process)
            raise SystemExit(2)
        self._lock = lock
        tasks: dict[str, asyncio.Task[None]] = {}
        removers: list[Callable[[], object]] = []
        try:
            self._started_at = self.deps.clock.now()
            self.recover_after_lock()
            self._beat("starting")
            if once:
                await self.step()
                await self._relay()
                return
            removers = _install_signal_handlers(stop)
            tasks["heartbeat"] = asyncio.create_task(
                self._supervise("heartbeat_loop", self._heartbeat_loop, stop)
            )
            if self.deps.relay is not None:
                tasks["relay"] = asyncio.create_task(self._supervise("relay_loop", self._relay_loop, stop))
            if self.deps.bot is not None:
                tasks["bot"] = asyncio.create_task(self._supervise("bot", self.deps.bot, stop))
            if self.deps.decisions is not None:
                tasks["decisions"] = asyncio.create_task(
                    self._supervise("decisions_loop", self.deps.decisions.run, stop)
                )
            if self.deps.marks is not None:
                tasks["marks"] = asyncio.create_task(
                    self._supervise("marks_publisher", self.deps.marks.run, stop)
                )
            while not stop.is_set():
                await self.step()
                if stop.is_set():
                    break
                await self._sleep_or_stop(self._interval(), stop)
        finally:
            stop.set()
            await self._shutdown(tasks, removers)
        if self._exit_code is not None:
            raise SystemExit(self._exit_code)

    async def _shutdown(
        self, tasks: dict[str, asyncio.Task[None]], removers: list[Callable[[], object]]
    ) -> None:
        """Each step runs even if an earlier one fails: heartbeat `stopping`, the relay's last pump and
        the bot (bounded), heartbeat `stopped` (not when another worker owns the row now), the lock, the
        signal handlers."""
        try:
            if not self._lock_lost:
                self._beat("stopping")
            timeout = self._settings().telegram_poll_timeout_seconds + BOT_STOP_GRACE_SECONDS
            await asyncio.gather(
                self._finish(tasks.get("relay"), RELAY_STOP_SECONDS),
                self._finish(tasks.get("bot"), timeout),
                self._finish(tasks.get("heartbeat"), 0),
                self._finish(tasks.get("decisions"), 0),  # no grace period: a pass is idempotent
                self._finish(tasks.get("marks"), 0),  # idem; a pass stuck in its thread ends at its timeout
            )
        finally:
            try:
                if not self._lock_lost:
                    self._beat("stopped")
            finally:
                try:
                    if self._lock is not None:
                        release_single_instance(self._lock)
                        self._lock = None
                except Exception as exc:
                    log.warning("worker.release_failed", error=_describe(exc))
                finally:
                    for remove in removers:
                        try:
                            remove()
                        except Exception as exc:
                            log.warning("worker.signal_restore_failed", error=_describe(exc))

    @staticmethod
    async def _finish(task: asyncio.Task[None] | None, timeout: float) -> None:
        """Wait for a task to see `stop` (up to `timeout` real seconds), then cancel it."""
        if task is None:
            return
        if timeout > 0:
            await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _interval(self) -> float:
        settings = self._settings()
        if self._hb_phase == "session":
            return float(settings.quote_poll_seconds)
        idle = float(settings.worker_idle_poll_seconds)
        if self._live_run_check is not None:  # wake for the idle live-run check (FIX-401)
            idle = min(idle, LIVE_RUN_CHECK_SECONDS)
        now = self.deps.clock.now()
        cal = self.deps.calendar
        day = et_date(now)
        try:
            pre_market = session_phase(cal, now) == "pre_market"
            until = (cal.session_open(day) - PRE_OPEN_LEAD - now).total_seconds() if pre_market else idle
        except Exception as exc:
            log.warning("worker.interval_failed", error=_describe(exc))
            return idle
        # wake in time for the session loop, however long the idle poll is
        return min(idle, max(until, float(settings.quote_poll_seconds)))

    async def _sleep_or_stop(self, seconds: float, stop: asyncio.Event) -> None:
        if stop.is_set():
            return
        sleeper = asyncio.ensure_future(self.deps.sleep(seconds))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waiter):
                task.cancel()
            await asyncio.gather(sleeper, waiter, return_exceptions=True)

    async def _supervise(
        self, part: str, body: Callable[[asyncio.Event], Awaitable[None]], stop: asyncio.Event
    ) -> None:
        """Run `body(stop)` beside the step loop; one that raises is logged and restarted after 30 s,
        one that returns before `stop` is restarted the same way."""
        while not stop.is_set():
            try:
                await body(stop)
            except Exception as exc:
                self._failed(part, exc)
            else:
                if stop.is_set():
                    return
                log.warning("worker.task_returned", part=part)
            await self._sleep_or_stop(BOT_RESTART_SECONDS, stop)

    async def _relay_loop(self, stop: asyncio.Event) -> None:
        """Pump the relay on the step cadence, then once more when `stop` is set (so alerts recorded by
        the last step go out), unless another worker has taken over."""
        while True:
            await self._relay()
            if stop.is_set():
                return
            await self._sleep_or_stop(self._interval(), stop)
            if self._lock_lost:
                return

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        """Beat every `worker.heartbeat_seconds`, whatever the step loop is doing, after checking the
        single-instance lock is still ours."""
        while not stop.is_set():
            await self._sleep_or_stop(float(self._settings().worker_heartbeat_seconds), stop)
            if stop.is_set():
                return
            if self._check_lock(stop):
                self._beat(self._hb_phase)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m trader.worker [--once]`: calls trader.runtime.run_worker(once=...) and returns its code."""
    logging_setup.configure_logging("worker")  # first, before anything can log (P3-T12)
    parser = argparse.ArgumentParser(prog="python -m trader.worker", description="The Trader worker.")
    parser.add_argument("--once", action="store_true", help="run one step and exit (smoke check)")
    args = parser.parse_args(argv)
    from trader import runtime  # late: the composition root builds this module's Worker

    try:
        return asyncio.run(runtime.run_worker(once=args.once))
    except SystemExit as exc:  # a refused second worker exits 2, one that lost its lock 3
        return runtime.exit_code(exc)
    except Exception as exc:  # setup failed (configuration, database): one line, no traceback
        log.error(
            "worker.setup_failed",
            error_type=type(exc).__name__,
            error=logging_setup.redact_text(str(exc))[:MAX_ERROR_CHARS],
        )
        return EXIT_SETUP_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
