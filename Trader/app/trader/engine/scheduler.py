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

Fix round 1 (P3 gauntlet):
- A failed attempt is retried only after a backoff (RETRY_BACKOFF_SECONDS: 30 s, 60 s, then 120 s);
  until then `fire_event` returns `skipped` ({"reason": "retry backoff"}). Safety keys
  (`scheduler.always_fire_late`: flatten, entry_cancel, overlay_decision) are idempotent, so they are
  never settled by failures: they keep retrying with the 120 s backoff until the close.
- A leftover `running` row (a crash, or a success that could not be recorded) of an entry-type event is
  never re-run automatically: `fire_event` settles it as failed ("outcome unknown: not re-run
  automatically") with one critical event. Safety events are re-run. `force` always runs.
- `day_plan` stays pure and records its problems (a shared key at different times, a key over 39
  characters) on `DayPlan.problems`; `report_plan_problems` writes each as ONE `error` event (source
  `scheduler`) per session. `fire_event` calls it, and the worker should call it each step.
- The late-grace comparison is exact (120.9 s late with a 120 s grace is missed).

P3-REVIEW: a failed attempt's event is `error` (relayed) only for the first MAX_EVENT_ATTEMPTS attempts; a
safety event's later retries are recorded at `warning`, so a flatten or entry_cancel that keeps failing
alerts three times and once more when the close records it missed, not every 120 s until the close.
"""

import math
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import EventLog, JobRun, Order, Position
from trader.db.session import session_scope
from trader.events import log_event
from trader.jobs.runner import OUTCOME_UNKNOWN, JobFailure, JobOutcome, lock_key, run_job_async
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import Strategy

if TYPE_CHECKING:
    from trader.strategies.registry import StrategyRegistry

log = structlog.get_logger("scheduler")

EVENT_JOB_PREFIX = "event:"
MAX_EVENT_ATTEMPTS = 3
# The wait after the n-th failed attempt before the next one (the last value repeats for safety keys).
RETRY_BACKOFF_SECONDS = (30, 60, 120)
# job_runs.job and event_log.source are varchar(50): `event:<key>` and `job.event:<key>` must fit.
MAX_EVENT_KEY_CHARS = 39
MISSED_PREFIX = "missed:"
PLAN_PROBLEM_SOURCE = "scheduler"


def _settled_failure(error: str | None) -> bool:
    """A failed run that settles its key at once: missed, or an entry event whose outcome is unknown."""
    e = error or ""
    return e.startswith(MISSED_PREFIX) or e == OUTCOME_UNKNOWN


def retry_backoff(failures: int) -> timedelta:
    """The wait before the next attempt after `failures` failed attempts (>= 1)."""
    return timedelta(seconds=RETRY_BACKOFF_SECONDS[min(failures, len(RETRY_BACKOFF_SECONDS)) - 1])


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
class PlanProblem:
    """Something wrong with a day plan, reported once per session as an `error` event."""

    kind: Literal["key_time_conflict", "key_too_long"]
    key: str
    message: str
    strategies: tuple[str, ...]

    @property
    def problem_id(self) -> str:
        return f"{self.kind}:{self.key}"


@dataclass(frozen=True, slots=True)
class DayPlan:
    session_date: date
    is_session: bool
    open: datetime | None
    close: datetime | None
    events: tuple[PlannedEvent, ...]  # sorted by time, then key
    problems: tuple[PlanProblem, ...] = ()  # fix round 1: reported by report_plan_problems


def day_plan(
    strategies: Sequence[Strategy],
    cal: SessionCalendar,
    session_date: date,
    settings: RuntimeSettings,
    *,
    exits_only: Sequence[Strategy] = (),
) -> DayPlan:
    """Resolve every strategy's schedule against the calendar for `session_date` (so early closes
    follow automatically). A key scheduled by several strategies runs once, at the earliest of their
    times. Differing times and over-long keys (left out) are logged at error level (source `scheduler`)
    and recorded on `DayPlan.problems`; `report_plan_problems` turns them into event_log rows. Pure
    apart from the structlog lines: no database access.

    `exits_only` (P2-REVIEW, BR-42): disabled strategies that still own an open position or a working
    order (see `exits_only_strategies`). Only their safety events (`scheduler.always_fire_late`:
    flatten, entry_cancel, overlay_decision) are scheduled, which the engine runs in exits-only mode."""
    if not cal.is_session(session_date):
        return DayPlan(session_date, False, None, None, ())
    times: dict[str, list[tuple[datetime, str]]] = {}
    problems: list[PlanProblem] = []
    too_long: dict[str, list[str]] = {}
    always = set(settings.scheduler_always_fire_late)
    running = {s.key for s in strategies}
    sources: list[tuple[Strategy, bool]] = [(s, False) for s in strategies]
    sources += [(s, True) for s in exits_only if s.key not in running]
    for strategy, winding_down in sources:
        for ev in strategy.schedule(cal):
            if winding_down and ev.key not in always:
                continue
            if len(ev.key) > MAX_EVENT_KEY_CHARS:
                log.error(
                    "scheduler.key_too_long",
                    source="scheduler",
                    key=ev.key,
                    strategy=strategy.key,
                    session_date=session_date.isoformat(),
                    max_chars=MAX_EVENT_KEY_CHARS,
                )
                too_long.setdefault(ev.key, []).append(strategy.key)
                continue
            times.setdefault(ev.key, []).append((ev.at.resolve(cal, session_date), strategy.key))
    for key, owners_ in too_long.items():
        problems.append(
            PlanProblem(
                "key_too_long",
                key,
                f"event key {key!r} of {', '.join(owners_)} is over {MAX_EVENT_KEY_CHARS} characters: "
                "it will not run",
                tuple(dict.fromkeys(owners_)),
            )
        )
    events: list[PlannedEvent] = []
    for key, scheduled in times.items():
        at = min(t for t, _ in scheduled)
        owners = tuple(dict.fromkeys(s for _, s in scheduled))
        if len({t for t, _ in scheduled}) > 1:
            log.error(
                "scheduler.key_time_conflict",
                source="scheduler",
                key=key,
                session_date=session_date.isoformat(),
                chosen=at.isoformat(),
                times={s: t.isoformat() for t, s in scheduled},
            )
            listed = ", ".join(f"{s} at {t.isoformat()}" for t, s in scheduled)
            problems.append(
                PlanProblem(
                    "key_time_conflict",
                    key,
                    f"event {key!r} is scheduled at different times ({listed}); it runs once, at "
                    f"{at.isoformat()}",
                    owners,
                )
            )
        events.append(PlannedEvent(key, at, owners, key in always))
    events.sort(key=lambda e: (e.at, e.key))
    return DayPlan(
        session_date,
        True,
        cal.session_open(session_date),
        cal.session_close(session_date),
        tuple(events),
        tuple(problems),
    )


def report_plan_problems(factory: sessionmaker[Session], clock: Clock, plan: DayPlan) -> int:
    """Write each of the plan's problems as ONE `error` event (source `scheduler`) per session, however
    often the plan is built or this is called, and from however many processes (a transaction-level
    advisory lock per problem, then an existence check). Returns the number of rows written. Never
    raises: a failure is logged and retried on the next call."""
    written = 0
    for problem in plan.problems:
        day = plan.session_date.isoformat()
        try:
            with session_scope(factory) as s:
                s.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": lock_key(f"plan_problem:{problem.problem_id}", plan.session_date)},
                )
                exists = s.execute(
                    select(EventLog.id)
                    .where(
                        EventLog.source == PLAN_PROBLEM_SOURCE,
                        EventLog.data["problem"].astext == problem.problem_id,
                        EventLog.data["session_date"].astext == day,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if exists is not None:
                    continue
                log_event(
                    s,
                    clock,
                    "error",
                    PLAN_PROBLEM_SOURCE,
                    problem.message,
                    {
                        "problem": problem.problem_id,
                        "kind": problem.kind,
                        "key": problem.key,
                        "strategies": list(problem.strategies),
                        "session_date": day,
                    },
                )
                written += 1
        except Exception as exc:
            log.error(
                "scheduler.plan_problem_not_recorded",
                problem=problem.problem_id,
                session_date=day,
                error_type=type(exc).__name__,
            )
    return written


def exits_only_strategies(
    factory: sessionmaker[Session], registry: "StrategyRegistry", run_id: int
) -> list[Strategy]:
    """The disabled strategies that still own an open position or a working order in run `run_id`
    (P2-REVIEW, BR-42): the engine runs their events exits-only, so the plan must schedule their safety
    events. A plug-in that can't start is logged and left out (the engine alerts on it when it runs)."""
    with session_scope(factory) as s:
        owner_ids = set(
            s.execute(
                select(Position.strategy_config_id).where(
                    Position.run_id == run_id, Position.closed_at.is_(None)
                )
            ).scalars()
        ) | set(
            s.execute(
                select(Order.strategy_config_id).where(Order.run_id == run_id, Order.status == "working")
            ).scalars()
        )
    enabled = {strategy.key for strategy, _ in registry.enabled()}
    out: list[Strategy] = []
    seen: set[str] = set()
    for config_id in sorted(i for i in owner_ids if i is not None):
        key = registry.config_key(config_id)
        if key is None or key in enabled or key in seen:
            continue
        seen.add(key)
        try:
            out.append(registry.instance(key)[0])
        except Exception as exc:
            log.error(
                "scheduler.exits_only_strategy_failed",
                source="scheduler",
                strategy=key,
                strategy_config_id=config_id,
                error_type=type(exc).__name__,
            )
    return out


def live_day_plan(
    factory: sessionmaker[Session],
    registry: "StrategyRegistry",
    run_id: int,
    cal: SessionCalendar,
    session_date: date,
    settings: RuntimeSettings,
) -> DayPlan:
    """The worker's plan: every enabled strategy, plus the safety events of disabled strategies that
    still own positions or working orders in the live run."""
    enabled = [strategy for strategy, _ in registry.enabled()]
    return day_plan(
        enabled, cal, session_date, settings, exits_only=exits_only_strategies(factory, registry, run_id)
    )


def _no_writes() -> datetime:
    raise RuntimeError("fired_keys only reads settings")


def _load_always_fire_late(factory: sessionmaker[Session]) -> set[str]:
    """The stored safety keys; the defaults if the stored settings can't be read (never settle a flatten
    because of a bad settings row)."""
    try:
        return set(SettingsStore(factory, _no_writes).load().scheduler_always_fire_late)
    except Exception as exc:
        log.error("scheduler.settings_unreadable", error_type=type(exc).__name__)
        return set(RuntimeSettings().scheduler_always_fire_late)


def fired_keys(
    factory: sessionmaker[Session],
    session_date: date,
    *,
    always_fire_late: Collection[str] | None = None,
) -> set[str]:
    """The settled keys: succeeded, missed, settled as "outcome unknown", or (except the safety keys in
    `always_fire_late`, which retry until the close) failed MAX_EVENT_ATTEMPTS times. `always_fire_late`
    defaults to the stored `scheduler.always_fire_late` setting."""
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
        if status == "succeeded" or (status == "failed" and _settled_failure(error)):
            settled.add(key)
        elif status == "failed":
            failures[key] = failures.get(key, 0) + 1
    exhausted = {k for k, n in failures.items() if n >= MAX_EVENT_ATTEMPTS and k not in settled}
    if exhausted:
        safety = set(always_fire_late) if always_fire_late is not None else _load_always_fire_late(factory)
        exhausted -= safety
    settled.update(exhausted)
    return settled


def _failure_history(
    factory: sessionmaker[Session], key: str, session_date: date
) -> tuple[int, datetime | None]:
    """(failed attempts that count towards the retry limit, when the last one finished)."""
    with session_scope(factory) as s:
        rows = s.execute(
            select(JobRun.error, JobRun.finished_at).where(
                JobRun.job == event_job(key), JobRun.session_date == session_date, JobRun.status == "failed"
            )
        ).all()
    counted = [finished for error, finished in rows if not _settled_failure(error)]
    last = max((f for f in counted if f is not None), default=None)
    return len(counted), last


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
    report_plan_problems(deps.factory, deps.clock, plan)
    event = next((e for e in plan.events if e.key == key), None)
    if event is None:
        return FireResult(key, session_date, "not_scheduled")
    now = deps.clock.now()
    failures = 0
    if not force:
        if now < event.at:
            early = (event.at - now).total_seconds()
            return FireResult(
                key, session_date, "too_early", {"at": event.at.isoformat(), "early_seconds": early}
            )
        missed = _missed_reason(event, now, plan.close, deps.settings())
        if missed is not None:
            return await _record_missed(deps, event, session_date, now, missed)
        failures, last_failed = _failure_history(deps.factory, key, session_date)
        if failures and last_failed is not None:
            retry_at = last_failed + retry_backoff(failures)
            if now < retry_at:
                return FireResult(
                    key,
                    session_date,
                    "skipped",
                    {"reason": "retry backoff", "attempts": failures, "retry_at": retry_at.isoformat()},
                )

    async def body() -> dict[str, Any]:
        runner = await deps.runner()
        result = await runner.run_event(key, session_date)
        return {
            "strategies": list(getattr(result, "strategies", [])),
            "outcomes": len(getattr(result, "outcomes", [])),
        }

    outcome = await run_job_async(
        deps.factory,
        deps.clock,
        event_job(key),
        session_date,
        body,
        force=force,
        rerun_abandoned=force or event.always_fire_late,
        failure_level=_failure_level(failures),
    )
    return _from_outcome(key, session_date, outcome)


def _failure_level(failures: int) -> str:
    """The level of the event a failed attempt writes: `error` (relayed as an alert) for the first
    MAX_EVENT_ATTEMPTS attempts, `warning` for a safety event's later retries (P3-REVIEW: one that keeps
    failing retries every 120 s until the close, and would otherwise alert on each; the close then records
    it `missed`, which alerts once more)."""
    return "error" if failures < MAX_EVENT_ATTEMPTS else "warning"


def _late_seconds(event: PlannedEvent, now: datetime) -> int:
    """Whole seconds late, rounded up (so 120.9 s shows as 121 s, matching the exact grace test)."""
    return max(0, math.ceil((now - event.at).total_seconds()))


def _missed_reason(
    event: PlannedEvent, now: datetime, close: datetime | None, settings: RuntimeSettings
) -> str | None:
    """The `missed:` error text if the event is too late to fire, else None. The grace comparison is
    exact (timedelta, no truncation)."""
    late = _late_seconds(event, now)
    if close is not None and now >= close:
        return f"{MISSED_PREFIX} {late}s late (after the close)"
    grace = timedelta(seconds=settings.scheduler_late_grace_seconds)
    if not event.always_fire_late and now - event.at > grace:
        return f"{MISSED_PREFIX} {late}s late"
    return None


async def _record_missed(
    deps: FireDeps, event: PlannedEvent, session_date: date, now: datetime, reason: str
) -> FireResult:
    async def body() -> dict[str, Any]:
        raise MissedEvent(reason)

    outcome = await run_job_async(
        deps.factory,
        deps.clock,
        event_job(event.key),
        session_date,
        body,
        rerun_abandoned=event.always_fire_late,
    )
    if outcome.error == OUTCOME_UNKNOWN:
        return FireResult(event.key, session_date, "failed", {"error": outcome.error})
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
