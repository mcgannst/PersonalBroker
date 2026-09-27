"""Session event scheduler: today's event times from every enabled strategy, which are due, and firing
each (event, session) exactly once across the worker and the cron backups (SPEC §5.1, §9).

Every firing goes through `run_job_async` with the job name `event:<key>`, so the advisory lock and the
"already succeeded" rule make the worker and a cron backup safe to race.

Late policy (SPEC ambiguity resolved, P3 plan T3): an event may fire up to
`scheduler.late_grace_seconds` after its time. Later than that it is recorded as missed (a failed job
run with an error starting `missed:`, and its error event, which the relay turns into an alert), except
the keys in `scheduler.always_fire_late` (exits and cancels), which fire however late until the close.
After the close every event is missed: there is nothing left to do. `force` bypasses the time rules.

Settled keys (`fired_keys`) are never due again, so a missed or persistently failing event is recorded
and alerted once (or MAX_EVENT_ATTEMPTS times), not on every worker step.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import JobRun
from trader.db.session import session_scope
from trader.jobs.runner import JobFailure, JobOutcome, run_job_async
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings
from trader.strategies.base import Strategy

log = structlog.get_logger("scheduler")

EVENT_JOB_PREFIX = "event:"
MAX_EVENT_ATTEMPTS = 3
# job_runs.job and event_log.source are varchar(50): `event:<key>` and `job.event:<key>` must fit.
MAX_EVENT_KEY_CHARS = 39
MISSED_PREFIX = "missed:"


def event_job(key: str) -> str:
    """The job_runs.job name of a session event: `event:<key>`."""
    return f"{EVENT_JOB_PREFIX}{key}"


class MissedEvent(JobFailure):
    """Raised inside the job body of an event that is too late to fire, so it is recorded as a failure
    whose error starts with `missed:`."""


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    key: str
    at: datetime  # UTC
    strategies: tuple[str, ...]  # strategy keys that scheduled this key
    always_fire_late: bool


@dataclass(frozen=True, slots=True)
class DayPlan:
    session_date: date
    is_session: bool
    open: datetime | None
    close: datetime | None
    events: tuple[PlannedEvent, ...]  # sorted by time, then key


def day_plan(
    strategies: Sequence[Strategy], cal: SessionCalendar, session_date: date, settings: RuntimeSettings
) -> DayPlan:
    """Resolve every strategy's schedule against the calendar for `session_date` (so early closes
    follow automatically). A key scheduled by several strategies runs once, at the earliest of their
    times; differing times and over-long keys are logged at error level (source `scheduler`)."""
    if not cal.is_session(session_date):
        return DayPlan(session_date, False, None, None, ())
    times: dict[str, list[tuple[datetime, str]]] = {}
    for strategy in strategies:
        for ev in strategy.schedule(cal):
            if len(ev.key) > MAX_EVENT_KEY_CHARS:
                log.error(
                    "scheduler.key_too_long",
                    source="scheduler",
                    key=ev.key,
                    strategy=strategy.key,
                    session_date=session_date.isoformat(),
                    max_chars=MAX_EVENT_KEY_CHARS,
                )
                continue
            times.setdefault(ev.key, []).append((ev.at.resolve(cal, session_date), strategy.key))
    always = set(settings.scheduler_always_fire_late)
    events: list[PlannedEvent] = []
    for key, scheduled in times.items():
        at = min(t for t, _ in scheduled)
        if len({t for t, _ in scheduled}) > 1:
            log.error(
                "scheduler.key_time_conflict",
                source="scheduler",
                key=key,
                session_date=session_date.isoformat(),
                chosen=at.isoformat(),
                times={s: t.isoformat() for t, s in scheduled},
            )
        owners = tuple(dict.fromkeys(s for _, s in scheduled))
        events.append(PlannedEvent(key, at, owners, key in always))
    events.sort(key=lambda e: (e.at, e.key))
    return DayPlan(
        session_date, True, cal.session_open(session_date), cal.session_close(session_date), tuple(events)
    )


def fired_keys(factory: sessionmaker[Session], session_date: date) -> set[str]:
    """The settled keys: succeeded, missed, or failed MAX_EVENT_ATTEMPTS times."""
    with session_scope(factory) as s:
        rows = s.execute(
            select(JobRun.job, JobRun.status, JobRun.error).where(
                JobRun.session_date == session_date, JobRun.job.startswith(EVENT_JOB_PREFIX, autoescape=True)
            )
        ).all()
    settled: set[str] = set()
    failures: dict[str, int] = {}
    for job, status, error in rows:
        key = job.removeprefix(EVENT_JOB_PREFIX)
        if status == "succeeded" or (status == "failed" and (error or "").startswith(MISSED_PREFIX)):
            settled.add(key)
        elif status == "failed":
            failures[key] = failures.get(key, 0) + 1
    settled.update(k for k, n in failures.items() if n >= MAX_EVENT_ATTEMPTS)
    return settled


def due_events(plan: DayPlan, now: datetime, fired: set[str]) -> list[PlannedEvent]:
    """Unfired events with `at <= now`, in time order."""
    return [e for e in plan.events if e.at <= now and e.key not in fired]


class EventRunner(Protocol):
    """What fire_event runs an event on. P2's Engine satisfies it (same parameter names)."""

    async def run_event(self, event_key: str, session_date: date) -> Any: ...


@dataclass(frozen=True, slots=True)
class FireDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    plan: Callable[[date], DayPlan]
    runner: Callable[[], Awaitable[EventRunner]]  # built lazily, only when an event actually runs


FireStatus = Literal["fired", "skipped", "missed", "too_early", "not_scheduled", "not_session", "failed"]


@dataclass(frozen=True, slots=True)
class FireResult:
    key: str
    session_date: date
    status: FireStatus
    detail: dict[str, Any] = field(default_factory=dict)


async def fire_event(deps: FireDeps, key: str, session_date: date, *, force: bool = False) -> FireResult:
    """Fire `key` for `session_date` at most once (see the module docstring for the late policy).

    Returns `not_session` / `not_scheduled` / `too_early` without writing anything; `missed` after
    recording a failed `event:<key>` run (unless it already succeeded: `skipped`); otherwise the
    `run_job_async` outcome as `fired` / `skipped` / `failed`."""
    if not deps.calendar.is_session(session_date):
        return FireResult(key, session_date, "not_session")
    plan = deps.plan(session_date)
    event = next((e for e in plan.events if e.key == key), None)
    if event is None:
        return FireResult(key, session_date, "not_scheduled")
    now = deps.clock.now()
    if not force:
        if now < event.at:
            early = (event.at - now).total_seconds()
            return FireResult(
                key, session_date, "too_early", {"at": event.at.isoformat(), "early_seconds": early}
            )
        missed = _missed_reason(event, now, plan.close, deps.settings())
        if missed is not None:
            return await _record_missed(deps, event, session_date, now, missed)

    async def body() -> dict[str, Any]:
        runner = await deps.runner()
        result = await runner.run_event(key, session_date)
        return {
            "strategies": list(getattr(result, "strategies", [])),
            "outcomes": len(getattr(result, "outcomes", [])),
        }

    outcome = await run_job_async(deps.factory, deps.clock, event_job(key), session_date, body, force=force)
    return _from_outcome(key, session_date, outcome)


def _late_seconds(event: PlannedEvent, now: datetime) -> int:
    return int((now - event.at).total_seconds())


def _missed_reason(
    event: PlannedEvent, now: datetime, close: datetime | None, settings: RuntimeSettings
) -> str | None:
    """The `missed:` error text if the event is too late to fire, else None."""
    late = _late_seconds(event, now)
    if close is not None and now >= close:
        return f"{MISSED_PREFIX} {late}s late (after the close)"
    if not event.always_fire_late and late > settings.scheduler_late_grace_seconds:
        return f"{MISSED_PREFIX} {late}s late"
    return None


async def _record_missed(
    deps: FireDeps, event: PlannedEvent, session_date: date, now: datetime, reason: str
) -> FireResult:
    async def body() -> dict[str, Any]:
        raise MissedEvent(reason)

    outcome = await run_job_async(deps.factory, deps.clock, event_job(event.key), session_date, body)
    if outcome.status == "skipped":
        return FireResult(event.key, session_date, "skipped", dict(outcome.detail))
    log.warning("scheduler.event_missed", key=event.key, session_date=session_date.isoformat(), reason=reason)
    return FireResult(
        event.key,
        session_date,
        "missed",
        {"late_seconds": _late_seconds(event, now), "error": outcome.error or reason},
    )


def _from_outcome(key: str, session_date: date, outcome: JobOutcome) -> FireResult:
    if outcome.status == "succeeded":
        return FireResult(key, session_date, "fired", dict(outcome.detail))
    if outcome.status == "skipped":
        return FireResult(key, session_date, "skipped", dict(outcome.detail))
    return FireResult(key, session_date, "failed", {"error": outcome.error})
