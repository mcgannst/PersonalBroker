"""Running the decision recorder while the app runs (P6-T11).

`DecisionsLoop` is the worker's fourth background task (beside the relay, the heartbeat and the bot): on a
session day, from 07:50 ET until the close + 30 min, every `reports.decisions_refresh_seconds` it runs one
`record_day(final=False)` pass for the worker's live run. It never contends with the 9:35 scan: no pass
starts between 09:34:00 and 09:38:00 ET (the scan and its 09:36 cron backup), and a pass is skipped while
any `event:*` job of today is `running`. Every database step it takes (the settings read, the `running`
check, the pass itself, its warning/info events) runs in a worker thread (`asyncio.to_thread`), so the
worker's event loop (the step loop, quote polling, the bot) is never blocked by it. It never raises: a
failed pass writes ONE `warning` event per failure streak (source `decisions`, never relayed) and one
`info` event when a pass next succeeds, through the `event` writer the composition root passes (this package
itself writes only `decision_log`).

`final_pass` is the post-close's step: a `record_day(final=True)` that freezes the day, then `prune`, then
the day's summary read back from its `day` row (the daily Telegram summary's "Decisions" line).

Logging only (D2): nothing here is imported by decision-path code, and it writes `decision_log` (through
the recorder) only.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Protocol

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.decisions.prune import PruneResult, prune
from trader.decisions.recorder import record_day
from trader.decisions.summary import summary_from_json
from trader.decisions.types import DaySummary, RecorderDeps, RecordResult
from trader.logging_setup import redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, et_date
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("decisions.loop")

WINDOW_START = time(7, 50)  # ET: just before the 08:00 pre-market scan, the day's first source
AFTER_CLOSE = timedelta(minutes=30)  # the loop keeps recording until the close + 30 min
QUIET_FROM = time(9, 34)  # ET: no pass STARTS in [09:34:00, 09:38:00) (the 9:35 scan and its 09:36 backup)
QUIET_TO = time(9, 38)
EVENT_JOB_PREFIX = "event:"
MAX_ERROR_CHARS = 300
DEFAULT_INTERVAL = float(RuntimeSettings().reports_decisions_refresh_seconds)

LoopSkip = Literal[
    "disabled", "not_session", "outside_window", "scan_quiet", "no_run", "event_running", "error"
]


class Recorder(Protocol):
    """`record_day`'s signature (tests pass a fake)."""

    def __call__(
        self,
        deps: RecorderDeps,
        run_id: int,
        session_date: date,
        *,
        final: bool = False,
        rebuild: bool = False,
    ) -> Awaitable[RecordResult]: ...


@dataclass(frozen=True, slots=True)
class LoopStep:
    """One iteration: when it started, why it did not record (None: a pass ran and succeeded), the pass's
    result, and how long to wait before the next one (seconds)."""

    at: datetime
    skipped: LoopSkip | None
    result: RecordResult | None
    interval: float


@dataclass(frozen=True, slots=True)
class _Gate:
    interval: float
    skipped: LoopSkip | None
    run_id: int | None = None
    day: date | None = None


def describe(exc: BaseException) -> str:
    """An exception as one masked, capped line (it goes into event_log, which the web app shows)."""
    flat = " ".join(redact_text(f"{type(exc).__name__}: {exc}").split())
    return flat if len(flat) <= MAX_ERROR_CHARS else flat[: MAX_ERROR_CHARS - 1] + "…"


def in_quiet_window(now: datetime) -> bool:
    """True in [09:34:00, 09:38:00) ET, whatever the season (EDT or EST): no pass starts then."""
    local = now.astimezone(ET).time()
    return QUIET_FROM <= local < QUIET_TO


def recording_window(calendar: SessionCalendar, day: date) -> tuple[datetime, datetime]:
    """[07:50 ET, the session's close + 30 min) of session `day` (13:00 closes on early-close days)."""
    return datetime.combine(day, WINDOW_START, tzinfo=ET), calendar.session_close(day) + AFTER_CLOSE


def event_running(factory: sessionmaker[Session], day: date) -> bool:
    """Whether any `event:*` job of `day` has a `running` row (the 9:35 scan, a flatten, ...)."""
    with factory() as s:
        found = s.execute(
            select(m.JobRun.id)
            .where(
                m.JobRun.job.startswith(EVENT_JOB_PREFIX),
                m.JobRun.session_date == day,
                m.JobRun.status == "running",
            )
            .limit(1)
        ).first()
    return found is not None


EventWriter = Callable[[str, str, dict[str, Any], int | None], None]
"""(level, message, data, run_id) -> one `event_log` row, source `decisions`; never raises. The composition
root passes it (`trader.runtime`): this package itself writes only `decision_log`."""


def _log_only(level: str, message: str, data: dict[str, Any], run_id: int | None) -> None:
    log.info("decisions.event", level=level, message=message)


class DecisionsLoop:
    """See the module docstring. `run_id` is the worker's live run (called in a worker thread each pass)."""

    def __init__(
        self,
        deps: RecorderDeps,
        run_id: Callable[[], int | None],
        *,
        record: Recorder = record_day,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        event: EventWriter | None = None,
    ) -> None:
        self.deps = deps
        self.run_id = run_id
        self._event = event or _log_only
        self._record = record
        self._sleep = sleep
        self._failures = 0
        self._interval = DEFAULT_INTERVAL

    async def run(self, stop: asyncio.Event) -> None:
        """Iterate until `stop` is set. Never raises (each iteration is guarded)."""
        while not stop.is_set():
            step = await self.run_once()
            if stop.is_set():
                return
            await self._sleep_or_stop(step.interval, stop)

    async def run_once(self) -> LoopStep:
        """One iteration: the gate (settings, the window, the 9:35 quiet window, the live run, a running
        event) and, when it opens, one pass. Never raises."""
        now = self.deps.clock.now()
        try:
            gate = await asyncio.to_thread(self._gate, now)
        except Exception as exc:
            await self._failed(exc, None, et_date(now))
            return LoopStep(now, "error", None, self._interval)
        self._interval = gate.interval
        if gate.skipped is not None or gate.run_id is None or gate.day is None:
            return LoopStep(now, gate.skipped or "no_run", None, gate.interval)
        try:
            result = await self._record(self.deps, gate.run_id, gate.day, final=False)
        except Exception as exc:
            await self._failed(exc, gate.run_id, gate.day)
            return LoopStep(now, "error", None, gate.interval)
        # the pass's duration on the process clock (the only clock the app reads: see trader.market.clock)
        seconds = round((self.deps.clock.now() - now).total_seconds(), 3)
        if result.skipped is None:
            log.info(
                "decisions.recorded",
                run_id=gate.run_id,
                session_date=gate.day.isoformat(),
                rows=sum(result.stages.values()),
                seconds=seconds,
            )
        await self._ok(gate.run_id)
        return LoopStep(now, None, result, gate.interval)

    def _gate(self, now: datetime) -> _Gate:
        """The worker-thread part of an iteration that decides whether to record (database reads)."""
        settings = self.deps.settings()
        interval = float(settings.reports_decisions_refresh_seconds)
        if not settings.reports_decisions_enabled:
            return _Gate(interval, "disabled")
        day = et_date(now)
        calendar = self.deps.calendar
        if not calendar.is_session(day):
            return _Gate(interval, "not_session")
        start, end = recording_window(calendar, day)
        if not start <= now < end:
            return _Gate(interval, "outside_window")
        if in_quiet_window(now):
            return _Gate(interval, "scan_quiet")
        run_id = self.run_id()
        if run_id is None:
            return _Gate(interval, "no_run")
        if event_running(self.deps.factory, day):
            return _Gate(interval, "event_running", run_id, day)
        return _Gate(interval, None, run_id, day)

    async def _failed(self, exc: Exception, run_id: int | None, day: date) -> None:
        self._failures += 1
        text = describe(exc)
        if self._failures > 1:
            log.debug("decisions.pass_still_failing", failures=self._failures, error=text)
            return
        log.warning("decisions.pass_failed", session_date=day.isoformat(), error=text)
        await asyncio.to_thread(
            self._event,
            "warning",
            f"decision log pass for {day.isoformat()} failed: {text}",
            {"session_date": day.isoformat(), "error_type": type(exc).__name__},
            run_id,
        )

    async def _ok(self, run_id: int | None) -> None:
        failures, self._failures = self._failures, 0
        if not failures:
            return
        log.info("decisions.pass_recovered", failures=failures)
        await asyncio.to_thread(
            self._event,
            "info",
            f"decision log recovered after {failures} failed passes",
            {"failures": failures},
            run_id,
        )

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


# --- the post-close's final pass ----------------------------------------------------------------------------
@dataclass(frozen=True)
class FinalPass:
    """The post-close's final pass: the recorder's result, the day's summary as stored on its `day` row
    (None when there is no day row, e.g. the log is disabled), the day's row count, and what `prune` deleted
    (None, with `prune_error` the exception type, when it failed)."""

    result: RecordResult
    summary: DaySummary | None
    rows: int
    pruned: PruneResult | None
    prune_error: str | None = None


def day_state(
    factory: sessionmaker[Session], run_id: int, session_date: date
) -> tuple[DaySummary | None, int]:
    """The summary stored on run `run_id`'s `day` row for `session_date` (None when there is none or it can't
    be read) and the number of rows of that day."""
    d = m.DecisionLog
    with factory() as s:
        rows = int(
            s.execute(
                select(func.count()).where(d.run_id == run_id, d.session_date == session_date)
            ).scalar_one()
        )
        data = s.execute(
            select(d.data)
            .where(d.run_id == run_id, d.session_date == session_date, d.stage == "day")
            .order_by(d.seq.desc())
            .limit(1)
        ).scalar_one_or_none()
    if not isinstance(data, dict):
        return None, rows
    try:
        return summary_from_json(data), rows
    except Exception as exc:  # an unreadable day row: the summary goes out without the line
        log.warning("decisions.summary_unreadable", error_type=type(exc).__name__)
        return None, rows


async def final_pass(
    deps: RecorderDeps, run_id: int, session_date: date, *, record: Recorder = record_day
) -> FinalPass:
    """`record_day(final=True)` (the day is frozen), then `prune` (a failure is logged and reported, the pass
    still returns), then the day's summary. Raises when the recording or the read-back fails (the post-close
    isolates it). Every database step runs in a worker thread."""
    result = await record(deps, run_id, session_date, final=True)
    pruned: PruneResult | None = None
    prune_error: str | None = None
    try:
        pruned = await asyncio.to_thread(lambda: prune(deps.factory, deps.clock, deps.settings()))
    except Exception as exc:
        prune_error = type(exc).__name__
        log.warning("decisions.prune_failed", error=describe(exc))
    summary, rows = await asyncio.to_thread(day_state, deps.factory, run_id, session_date)
    return FinalPass(result, summary, rows, pruned, prune_error)
