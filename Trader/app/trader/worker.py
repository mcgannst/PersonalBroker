"""The long-running worker (`python -m trader.worker [--once]`): fires session events on time, polls quotes
for working orders, expires proposals, relays notifications, runs the Telegram bot, ends the session,
writes a heartbeat, and refuses to run twice (SPEC §1, §6, §7.2, §9; BR-31, BR-42).

Nothing is held in memory that the database doesn't have: which events are settled comes from `fired`
(job_runs), the relay resumes from its cursors, the engine re-reads working orders and pending proposals.
So a worker restarted mid-session carries on without firing or sending anything twice. Every part of a
step is guarded: an exception is logged (structlog plus one `error` event, source `worker`) and the step
goes on; the loop itself never dies on the engine, the relay or the database.
"""

import argparse
import asyncio
import functools
import hashlib
import os
import signal
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy
import structlog
from sqlalchemy import Connection, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import WorkerHeartbeat
from trader.db.session import session_scope
from trader.engine.scheduler import DayPlan, FireResult
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.market.sessions import SessionPhase, session_phase
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("worker")

LOCK_NAME = "trader.worker"
PRE_OPEN_LEAD = timedelta(seconds=60)  # the session loop starts this long before the open
BOT_RESTART_SECONDS = 30.0
FAILED_STEPS_CRITICAL = 10
BOT_STOP_GRACE_SECONDS = 5.0  # on top of one Telegram poll timeout
MAX_ERROR_CHARS = 500
_BEAT_SLACK_SECONDS = 0.05  # a real sleep may wake a hair early by the wall clock


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


@dataclass(frozen=True, slots=True)
class StepReport:
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
    except Exception:
        log.exception("worker.unlock_failed")
        conn.invalidate()
    finally:
        conn.close()


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


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
    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps
        self._engine: WorkerEngine | None = None
        self._engine_session: date | None = None
        self._plan: DayPlan | None = None
        self._ended: date | None = None
        self._fills_today = 0
        self._last_event: str | None = None
        self._step_errors = 0
        self._failed_steps = 0
        self._started_at: datetime | None = None
        self._last_beat: datetime | None = None
        self._hb_phase = "idle"
        self._hb_session: date | None = None

    # --- one iteration --------------------------------------------------------------------------------

    async def step(self) -> StepReport:
        now = self.deps.clock.now()
        cal = self.deps.calendar
        phase = session_phase(cal, now)
        day = et_date(now)
        self._step_errors = 0
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
        relayed = await self._relay()
        self._count_step()
        return StepReport(now, phase, fired, fills, relayed, ended)

    async def _open_engine(self, day: date) -> WorkerEngine:
        """Today's engine, built once per session (rebuilt each session so settings changes apply)."""
        if self._engine is None or self._engine_session != day:
            self._engine = await self.deps.engine_for(day)
            self._engine_session = day
            self._fills_today = 0
        return self._engine

    async def _guarded_engine(self, day: date) -> WorkerEngine | None:
        try:
            return await self._open_engine(day)
        except Exception as exc:
            self._failed("engine", exc)
            return None

    def _plan_for(self, day: date) -> DayPlan:
        if self._plan is None or self._plan.session_date != day:
            self._plan = self.deps.plan(day)
        return self._plan

    async def _fire_due(self, day: date, now: datetime) -> list[FireResult]:
        """Offer every due, unsettled event to `fire`, in time order (the plan is sorted). `fire` owns
        idempotency and lateness; `fired` reads the settled keys from the database."""
        try:
            plan = self._plan_for(day)
            settled = self.deps.fired(day)
        except Exception as exc:
            self._failed("plan", exc)
            return []
        results: list[FireResult] = []
        for event in plan.events:
            if event.at > now or event.key in settled:
                continue
            try:
                result = await self.deps.fire(event.key, day)
            except Exception as exc:
                self._failed("fire", exc, key=event.key)
                continue
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
        self._fills_today += fills
        return fills

    async def _tick(self, engine: WorkerEngine, now: datetime) -> None:
        try:
            await engine.tick(now)
        except Exception as exc:
            self._failed("tick", exc)

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

    async def _relay(self) -> bool:
        if self.deps.relay is None:
            return False
        try:
            await self.deps.relay()
        except Exception as exc:
            self._failed("relay", exc)
            return False
        return True

    # --- errors -----------------------------------------------------------------------------------------

    def _failed(self, part: str, exc: Exception, *, count: bool = True, **fields: Any) -> None:
        if count:
            self._step_errors += 1
        if part == "bot":
            # Telegram's client puts the token in URLs: log the type only, never the text or a traceback.
            log.error("worker.part_failed", part=part, error_type=type(exc).__name__, **fields)
            message = f"worker {part} failed: {type(exc).__name__}"
        else:
            log.error("worker.part_failed", part=part, exc_info=exc, **fields)
            message = f"worker {part} failed: {_describe(exc)}"
        self._event("error", message, {"part": part, **fields})

    def _count_step(self) -> None:
        if not self._step_errors:
            self._failed_steps = 0
            return
        self._failed_steps += 1
        if self._failed_steps == FAILED_STEPS_CRITICAL:
            log.critical("worker.failing", steps=self._failed_steps)
            self._event("critical", f"worker: {FAILED_STEPS_CRITICAL} consecutive steps failed", {})

    def _event(self, level: str, message: str, data: dict[str, Any]) -> None:
        try:
            with session_scope(self.deps.factory) as s:
                log_event(s, self.deps.clock, level, "worker", message, data)
        except Exception:
            log.exception("worker.event_failed", level=level)

    # --- heartbeat --------------------------------------------------------------------------------------

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
            "detail": {"fills_today": self._fills_today, "last_event": self._last_event},
        }
        stmt = insert(WorkerHeartbeat).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.process],
            set_={k: stmt.excluded[k] for k in values if k != "process"},
        )
        try:
            with session_scope(self.deps.factory) as s:
                s.execute(stmt)
        except Exception:
            log.exception("worker.heartbeat_failed", phase=phase)
            return
        self._last_beat = now

    def _seconds_to_beat(self) -> float:
        every = float(self.deps.settings().worker_heartbeat_seconds)
        if self._last_beat is None:
            return 0.0
        return every - (self.deps.clock.now() - self._last_beat).total_seconds()

    def _beat_if_due(self) -> None:
        if self._seconds_to_beat() <= _BEAT_SLACK_SECONDS:
            self._beat(self._hb_phase)

    # --- the loop ---------------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event, *, once: bool = False) -> None:
        """Run until `stop` is set (SIGTERM and SIGINT set it), or one step with `once`, which never starts
        the bot. A second worker exits at once with SystemExit(2), before doing anything else."""
        bind = self.deps.factory.kw.get("bind")
        if not isinstance(bind, sqlalchemy.Engine):
            raise TypeError("the worker needs a sessionmaker bound to an Engine")
        lock = acquire_single_instance(bind)
        if lock is None:
            log.critical("worker.already_running", process=self.deps.process)
            raise SystemExit(2)
        bot_task: asyncio.Task[None] | None = None
        removers: list[Callable[[], object]] = []
        try:
            self._started_at = self.deps.clock.now()
            self._beat("starting")
            if once:
                await self.step()
                return
            removers = _install_signal_handlers(stop)
            if self.deps.bot is not None:
                bot_task = asyncio.create_task(self._supervise_bot(stop))
            while not stop.is_set():
                await self.step()
                self._beat_if_due()
                await self._pause(self._interval(), stop)
        finally:
            stop.set()
            if bot_task is not None:
                await self._finish_bot(bot_task)
            self._beat("stopped")
            release_single_instance(lock)
            for remove in removers:
                remove()

    def _interval(self) -> float:
        settings = self.deps.settings()
        if self._hb_phase == "session":
            return float(settings.quote_poll_seconds)
        idle = float(settings.worker_idle_poll_seconds)
        now = self.deps.clock.now()
        cal = self.deps.calendar
        day = et_date(now)
        if session_phase(cal, now) == "pre_market":
            # wake in time for the session loop, however long the idle poll is
            until = (cal.session_open(day) - PRE_OPEN_LEAD - now).total_seconds()
            idle = min(idle, max(until, float(settings.quote_poll_seconds)))
        return idle

    async def _pause(self, seconds: float, stop: asyncio.Event) -> None:
        """Sleep `seconds` (stop cuts it short), beating the heartbeat on time in between."""
        remaining = seconds
        while remaining > 0 and not stop.is_set():
            chunk = min(remaining, max(self._seconds_to_beat(), 0.5))
            await self._sleep_or_stop(chunk, stop)
            remaining -= chunk
            if not stop.is_set():
                self._beat_if_due()

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

    async def _supervise_bot(self, stop: asyncio.Event) -> None:
        """The bot runs beside the quote loop, so long polling never delays it; a bot that dies is logged
        and restarted after 30 s."""
        bot = self.deps.bot
        assert bot is not None
        while not stop.is_set():
            try:
                await bot(stop)
            except Exception as exc:
                self._failed("bot", exc, count=False)
            else:
                if stop.is_set():
                    return
                log.warning("worker.bot_returned")
            await self._sleep_or_stop(BOT_RESTART_SECONDS, stop)

    async def _finish_bot(self, task: asyncio.Task[None]) -> None:
        """Wait for the bot to see `stop` (up to one poll timeout), then cancel it."""
        timeout = self.deps.settings().telegram_poll_timeout_seconds + BOT_STOP_GRACE_SECONDS
        _, pending = await asyncio.wait({task}, timeout=timeout)
        for t in pending:
            t.cancel()
        await asyncio.gather(task, return_exceptions=True)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m trader.worker [--once]`: calls trader.runtime.run_worker(once=...) and returns its code."""
    parser = argparse.ArgumentParser(prog="python -m trader.worker", description="The Trader worker.")
    parser.add_argument("--once", action="store_true", help="run one step and exit (smoke check)")
    args = parser.parse_args(argv)
    from trader import runtime  # late: the composition root builds this module's Worker

    try:
        return asyncio.run(runtime.run_worker(once=args.once))
    except SystemExit as exc:  # a refused second worker exits 2
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
