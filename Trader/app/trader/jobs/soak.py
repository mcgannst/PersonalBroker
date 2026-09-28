"""The soak report (P6-T2, SPEC §15.1 criterion 2): the clean-day verdict of every recent session, the run of
consecutive clean days, the earliest finish, operator marks, and the daily Telegram line.

A session day D is **clean** (Phase 6 plan, decision D1) when every job expected for D finally succeeded
before its deadline, the 9:35 scan fired within its late grace, and D has no `outage` mark:

- a job's check is `succeeded` when ANY of its rows succeeded on time (`finished_at` ≤ deadline; events by
  `started_at`, because the scheduler judges an event's lateness by when it fired), so a failed attempt
  followed by an on-time retry, or an on-time success followed by a failed forced re-run, is clean;
- after the deadline: `late` (a success only after it), `missed` (an event recorded `missed:`), `failed`
  (the latest row failed, or a row was still running) or `missing` (no row at all); before it, anything
  but an on-time success is `pending`;
- `token-refresh` writes no row: it fails when a `questrade.token` error event was written in D's token
  window (after the previous session's close, by D 18:00 ET) and no Questrade-dependent job of D
  (`premarket`, `event:orb_open`, `postclose`) that started after the last such event and by D 18:00 ET
  succeeded.

The report is **read-only**: it never goes through `run_job` (no `job_runs` row, so it never expects
itself) and builds its day plans without `runtime.plan_builder`, which would create a live run, ensure the
strategy defaults and write relayed plan-problem events for past days. Its only writes are the one
Telegram message's `notifications` row and, for `trader soak-mark`, one `info` event (source `soak`),
which the relay never sends.

Deadlines are ET wall-clock times on the exchange calendar (early closes included), stored and compared
in UTC; the Telegram line shows Mountain Time.
"""

import asyncio
import dataclasses
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.scheduler import (
    EVENT_JOB_PREFIX,
    MISSED_PREFIX,
    DayPlan,
    day_plan,
    event_job,
    live_day_plan,
)
from trader.events import log_event
from trader.logging_setup import redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.notify.notifier import NullNotifier
from trader.notify.types import Notifier, Renderer, SoakLineView
from trader.runtime import SESSION_END_JOB, TOKEN_SOURCE, active_live_run_id, checkin_job_name
from trader.settings_store import RuntimeSettings
from trader.strategies.base import SessionOffset
from trader.strategies.orb_sip import ORB_AT, ORB_EVENT, OrbSip
from trader.strategies.registry import StrategyRegistry

log = structlog.get_logger("soak")

SoakVerdict = Literal["clean", "not_clean", "pending"]
CheckStatus = Literal["succeeded", "late", "failed", "missing", "missed", "pending"]
# SoakDay.orb_open: a CheckStatus, or "off" when the 9:35 scan is not expected (orb_sip disabled, no row).
OrbStatus = Literal["succeeded", "late", "failed", "missing", "missed", "pending", "off"]
MarkKind = Literal["reset", "outage", "clear"]
MARK_KINDS: tuple[MarkKind, ...] = ("reset", "outage", "clear")

SOAK_SOURCE = "soak"
DEFAULT_TARGET = 10
DEFAULT_SESSIONS = 20
OUTAGE_MINUTES = 5  # an outage (worker or /api/health down) longer than this, 09:30-close, spoils the day
ERROR_CHARS = 200
MAX_REASON = 200
DEDUPE_PREFIX = "soak:"

CHECKIN_LABELS = ("11:30", "13:30")
TOKEN_JOB = "token-refresh"  # noqa: S105 (a job name, not a secret)
WEEKLY_JOB = "weekly"
ORB_JOB = event_job(ORB_EVENT)
# The jobs that need a working Questrade token: a success started after a token failure proves the chain
# recovered (D1).
TOKEN_USERS = ("premarket", ORB_JOB, "postclose")
STILL_RUNNING = "still running at the deadline"

# The D1 table: the ET wall-clock deadline on session D of each fixed job. Session events are judged by
# their planned time + `scheduler.late_grace_seconds` (the safety keys of `scheduler.always_fire_late` by
# D's close), and `weekly` (only on the week's last session) by the following Saturday at WEEKLY_DEADLINE.
DEADLINES: Mapping[str, time] = {
    "nightly": time(8, 0),  # keyed to its target session D, run the evening before
    "premarket": time(9, 30),
    "preopen": time(9, 30),
    checkin_job_name("11:30"): time(12, 30),
    checkin_job_name("13:30"): time(14, 30),
    SESSION_END_JOB: time(18, 0),
    "postclose": time(18, 0),
    TOKEN_JOB: time(18, 0),
}
WEEKLY_DEADLINE = time(12, 0)  # the Saturday after the week's last session

# The day-level jobs with in-process retries (P5-T15). Before its deadline, such a job whose last
# `retry_attempts` rows all failed (retries exhausted, nothing running, no success) is PROVISIONALLY failed
# (fix round 1): the day shows not clean ("catch-up possible until <deadline>"), is not final and is not
# counted; a successful catch-up before the deadline turns it clean on the next report.
RETRIED_JOBS: frozenset[str] = frozenset({"nightly", "premarket", "preopen", "postclose", WEEKLY_JOB})
DEFAULT_RETRY_ATTEMPTS: int = RuntimeSettings.model_fields["jobs_retry_attempts"].default


@dataclass(frozen=True)
class ExpectedJob:
    job: str
    deadline: datetime  # UTC


@dataclass(frozen=True)
class JobRunRow:
    """A plain copy of the `job_runs` columns the report needs."""

    job: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None


@dataclass(frozen=True)
class JobCheck:
    job: str
    status: CheckStatus
    deadline: datetime
    attempts: int
    finished_at: datetime | None
    error: str | None  # masked, at most ERROR_CHARS characters
    # `failed` before its deadline with the retries exhausted: a catch-up may still turn it `succeeded`.
    provisional: bool = False


@dataclass(frozen=True)
class SoakMark:
    session_date: date
    kind: MarkKind
    reason: str
    at: datetime


@dataclass(frozen=True)
class SoakDay:
    session_date: date
    verdict: SoakVerdict
    checks: tuple[JobCheck, ...]
    failed: tuple[str, ...]  # "<job> <status>" of every failed-for-good check
    orb_open: OrbStatus
    orb_open_seconds: float | None
    reset: bool
    outage: str | None
    # not clean only through provisional failures: not final yet (not counted, may still turn clean)
    provisional: bool = False


@dataclass(frozen=True)
class SoakReport:
    through: date
    target: int
    days: tuple[SoakDay, ...]
    consecutive_clean: int
    total_clean: int
    last_final: date | None
    earliest_finish: date | None
    generated_at: datetime
    env: Literal["dev", "prod"]
    # Earlier days of the window that became final with another verdict than the previous Telegram line's
    # evaluation gave them (re-derived by load_report; empty without a previous line).
    changed: tuple[tuple[date, str], ...] = ()


@dataclass(frozen=True)
class SoakDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    plan: Callable[[date], DayPlan]
    orb_enabled: Callable[[], bool]
    notifier: Notifier
    render: Renderer
    env: Literal["dev", "prod"]


# --- pure: expected jobs, checks, the day's verdict ---------------------------------------------------------


def _et_at(d: date, at: time) -> datetime:
    return datetime.combine(d, at, tzinfo=ET).astimezone(UTC)


def _week_last_session(cal: SessionCalendar, d: date) -> date | None:
    monday = d - timedelta(days=d.weekday())
    sessions = [x for x in (monday + timedelta(days=i) for i in range(5)) if cal.is_session(x)]
    return sessions[-1] if sessions else None


def expected_jobs(
    cal: SessionCalendar,
    session_date: date,
    plan: DayPlan,
    settings: RuntimeSettings,
    row_jobs: Collection[str],
    *,
    orb_required: bool = True,
) -> tuple[ExpectedJob, ...]:
    """The D1 table for session D: the fixed jobs, `event:<key>` for every key of the plan and every event
    key that has a row, `event:orb_open` while orb_sip is enabled, `weekly` on the week's last session.
    Pure. Not a session → ()."""
    if not cal.is_session(session_date):
        return ()
    close = cal.session_close(session_date)
    grace = timedelta(seconds=settings.scheduler_late_grace_seconds)
    always = set(settings.scheduler_always_fire_late)
    out = [ExpectedJob(job, _et_at(session_date, at)) for job, at in DEADLINES.items()]
    events: dict[str, datetime] = {}
    for ev in plan.events:
        events[ev.key] = close if ev.key in always else ev.at + grace
    if orb_required and ORB_EVENT not in events:
        # A missing ORB event is a missed 9:35 even when the plan lacks it.
        events[ORB_EVENT] = SessionOffset.parse(ORB_AT).resolve(cal, session_date) + grace
    for job in row_jobs:
        if job.startswith(EVENT_JOB_PREFIX):
            key = job.removeprefix(EVENT_JOB_PREFIX)
            # A key with a row but not in today's plan (a since-disabled strategy): its planned time is
            # unknown, so it is judged by the close (the orb scan by its fixed time).
            if key not in events:
                events[key] = (
                    SessionOffset.parse(ORB_AT).resolve(cal, session_date) + grace
                    if key == ORB_EVENT
                    else close
                )
    out += [
        ExpectedJob(event_job(key), at) for key, at in sorted(events.items(), key=lambda kv: (kv[1], kv[0]))
    ]
    if _week_last_session(cal, session_date) == session_date:
        saturday = session_date + timedelta(days=5 - session_date.weekday())
        out.append(ExpectedJob(WEEKLY_JOB, _et_at(saturday, WEEKLY_DEADLINE)))
    return tuple(out)


def _mask(text: str | None) -> str | None:
    if text is None:
        return None
    return " ".join(redact_text(text).split())[:ERROR_CHARS]


def _is_event(job: str) -> bool:
    return job.startswith(EVENT_JOB_PREFIX)


def _judged_at(job: str, r: JobRunRow) -> datetime:
    """When a success counts: an event when it started (the scheduler's lateness rule), a job when it
    finished."""
    if _is_event(job):
        return r.started_at
    return r.finished_at or r.started_at


def _retries_exhausted(job: str, mine: Sequence[JobRunRow], retry_attempts: int) -> bool:
    """A retried day-level job whose last `retry_attempts` rows (in start order) all failed: the in-process
    retries are over and nothing is running."""
    if job not in RETRIED_JOBS or not mine or retry_attempts < 1:
        return False
    if any(r.status == "running" for r in mine):
        return False
    tail = mine[-retry_attempts:]
    return len(tail) == retry_attempts and all(r.status == "failed" for r in tail)


def _job_check(
    exp: ExpectedJob,
    rows: Sequence[JobRunRow],
    now: datetime,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> JobCheck:
    mine = sorted(
        (r for r in rows if r.job == exp.job), key=lambda r: (r.started_at, r.finished_at or r.started_at)
    )
    attempts = len(mine)
    successes = [r for r in mine if r.status == "succeeded"]
    on_time = [r for r in successes if _judged_at(exp.job, r) <= exp.deadline]
    if on_time:
        first = on_time[0]
        return JobCheck(exp.job, "succeeded", exp.deadline, attempts, first.finished_at, None)
    missed = [r for r in mine if r.status == "failed" and (r.error or "").startswith(MISSED_PREFIX)]
    if _is_event(exp.job) and missed:
        r = missed[-1]
        return JobCheck(exp.job, "missed", exp.deadline, attempts, r.finished_at, _mask(r.error))
    if now < exp.deadline:
        last = mine[-1] if mine else None
        if last is not None and _retries_exhausted(exp.job, mine, retry_attempts):
            return JobCheck(
                exp.job,
                "failed",
                exp.deadline,
                attempts,
                last.finished_at,
                _mask(last.error) or "failed",
                provisional=True,
            )
        return JobCheck(
            exp.job,
            "pending",
            exp.deadline,
            attempts,
            last.finished_at if last else None,
            _mask(last.error) if last else None,
        )
    if successes:
        r = successes[0]
        return JobCheck(exp.job, "late", exp.deadline, attempts, r.finished_at, None)
    if not mine:
        return JobCheck(exp.job, "missing", exp.deadline, 0, None, None)
    last = mine[-1]
    if any(r.status == "running" for r in mine):
        return JobCheck(exp.job, "failed", exp.deadline, attempts, None, STILL_RUNNING)
    return JobCheck(
        exp.job, "failed", exp.deadline, attempts, last.finished_at, _mask(last.error) or "failed"
    )


def _token_check(
    exp: ExpectedJob, rows: Sequence[JobRunRow], now: datetime, token_failures: Sequence[datetime]
) -> JobCheck:
    """D1's token check: failure events in D's window, recovered only by a later successful Questrade job.
    Before the deadline it is pending (a failure could still come); after it, it never depends on `now`."""
    failures = sorted(t for t in token_failures if t <= exp.deadline)
    if now < exp.deadline:
        return JobCheck(exp.job, "pending", exp.deadline, len(failures), None, None)
    if not failures:
        return JobCheck(exp.job, "succeeded", exp.deadline, 0, None, None)
    last = failures[-1]
    # Only a run that started by the deadline counts: a forced re-run keyed to D days later must not turn a
    # final day clean after the fact (a past verdict never flips).
    recovered = any(
        r.job in TOKEN_USERS and r.status == "succeeded" and last < r.started_at <= exp.deadline for r in rows
    )
    if recovered:
        return JobCheck(exp.job, "succeeded", exp.deadline, len(failures), None, None)
    return JobCheck(
        exp.job,
        "failed",
        exp.deadline,
        len(failures),
        None,
        f"Questrade token failure at {last.isoformat()} not recovered by a later premarket, "
        "9:35 scan or postclose",
    )


FINAL_BAD: frozenset[str] = frozenset({"late", "failed", "missing", "missed"})


def evaluate_day(
    session_date: date,
    expected: Sequence[ExpectedJob],
    rows: Sequence[JobRunRow],
    now: datetime,
    *,
    token_failures: Sequence[datetime] = (),
    outage: str | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> SoakDay:
    """The D1 verdict of one session from its rows (pure). `token_failures` are the `questrade.token` error
    event times of D's token window; `outage` the reason of an outage mark; `retry_attempts` the
    in-process attempts of the retried day-level jobs (`jobs.retry_attempts`). A day that is not clean only
    through provisional failures is `not_clean` with `provisional` set (not final)."""
    checks: list[JobCheck] = []
    for exp in expected:
        if exp.job == TOKEN_JOB:
            checks.append(_token_check(exp, rows, now, token_failures))
        else:
            checks.append(_job_check(exp, rows, now, retry_attempts))
    failed = tuple(f"{c.job} {c.status}" for c in checks if c.status in FINAL_BAD)
    final_bad = any(c.status in FINAL_BAD and not c.provisional for c in checks)
    provisional = bool(failed) and outage is None and not final_bad
    if outage is not None or failed:
        verdict: SoakVerdict = "not_clean"
    elif any(c.status == "pending" for c in checks):
        verdict = "pending"
    else:
        verdict = "clean"
    orb = next((c for c in checks if c.job == ORB_JOB), None)
    orb_status: OrbStatus = orb.status if orb is not None else "off"
    seconds: float | None = None
    if orb is not None and orb.status == "succeeded":
        on_time = [
            r
            for r in rows
            if r.job == ORB_JOB and r.status == "succeeded" and r.started_at <= orb.deadline and r.finished_at
        ]
        if on_time:
            first = min(on_time, key=lambda r: r.started_at)
            assert first.finished_at is not None
            seconds = round((first.finished_at - first.started_at).total_seconds(), 1)
    return SoakDay(
        session_date,
        verdict,
        tuple(checks),
        failed,
        orb_status,
        seconds,
        False,
        _mask(outage) if outage is not None else None,
        provisional,
    )


# --- pure: marks, the window, the count ---------------------------------------------------------------------


def effective_marks(marks: Sequence[SoakMark]) -> dict[date, SoakMark]:
    """The mark in force per session: the latest one wins; `clear` removes it."""
    out: dict[date, SoakMark] = {}
    for mark in sorted(marks, key=lambda x: x.at):
        if mark.kind == "clear":
            out.pop(mark.session_date, None)
        else:
            out[mark.session_date] = mark
    return out


def session_window(cal: SessionCalendar, through: date, sessions: int) -> tuple[date, ...]:
    """The last `sessions` sessions up to `through` (itself a session: a non-session snaps to the previous
    one), oldest first."""
    last = through if cal.is_session(through) else cal.previous_session(through)
    if sessions <= 0:
        return ()
    return (*cal.sessions_before(last, sessions - 1), last)


def default_through(cal: SessionCalendar, now: datetime) -> date:
    """The latest session whose ET date is on or before today."""
    today = et_date(now)
    return today if cal.is_session(today) else cal.previous_session(today)


def build_report(
    days: Sequence[SoakDay],
    *,
    cal: SessionCalendar,
    through: date,
    target: int,
    now: datetime,
    env: str,
) -> SoakReport:
    """The consecutive count over the final days in order (clean +1, not clean → 0, a reset mark → 0
    before its day), stopping at the first pending or provisional day; the earliest finish is the session on
    which the count would reach `target` if every later day is clean (None once reached). Pure."""
    ordered = sorted(days, key=lambda d: d.session_date)
    count = 0
    last_final: date | None = None
    for day in ordered:
        if day.reset:
            count = 0
        if day.verdict == "pending" or day.provisional:  # not final yet: never counted
            break
        count = count + 1 if day.verdict == "clean" else 0
        last_final = day.session_date
    total = sum(1 for d in ordered if d.verdict == "clean")
    finish: date | None = None
    if count < target:
        if last_final is not None:
            base = last_final
        elif ordered:
            base = cal.previous_session(ordered[0].session_date)
        else:
            base = cal.previous_session(through) if cal.is_session(through) else through
        finish = base
        for _ in range(target - count):
            finish = cal.next_session(finish)
    return SoakReport(
        through=through,
        target=target,
        days=tuple(ordered),
        consecutive_clean=count,
        total_clean=total,
        last_final=last_final,
        earliest_finish=finish,
        generated_at=now,
        env="prod" if env == "prod" else "dev",
    )


def _verdict_words(day: SoakDay) -> str:
    if day.verdict == "clean":
        return "clean"
    if day.verdict == "pending":
        return "pending"
    reasons = list(day.failed)
    if day.outage is not None:
        reasons.append(f"outage ({day.outage})")
    return "not clean: " + ", ".join(reasons) if reasons else "not clean"


def _short(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def line_view(report: SoakReport, *, final: bool) -> SoakLineView:
    """The Telegram line's view of the report's `through` session. For a not-clean day `failed` names each
    failed check with a short masked error; for a pending day it names the checks still open."""
    day = next((d for d in report.days if d.session_date == report.through), None)
    if day is None:
        raise ValueError(f"{report.through} is not in the report")
    failed: list[str] = []
    if day.verdict == "pending":
        failed = [c.job for c in day.checks if c.status == "pending"]
    else:
        for c in day.checks:
            if c.status in FINAL_BAD:
                text = f"{c.job} {c.status}"
                if c.error:
                    text += f" ({_short(c.error)})"
                failed.append(text)
        if day.outage is not None:
            failed.append(f"outage ({_short(day.outage)})")
    return SoakLineView(
        session_date=day.session_date,
        verdict=day.verdict,
        failed=tuple(failed),
        orb_open=day.orb_open,
        orb_open_seconds=day.orb_open_seconds,
        consecutive_clean=report.consecutive_clean,
        target=report.target,
        earliest_finish=report.earliest_finish,
        changed=report.changed,
        final=final,
        env=report.env,
        catch_up_until=(max(c.deadline for c in day.checks if c.provisional) if day.provisional else None),
    )


# --- read-only database access ------------------------------------------------------------------------------


class _QuietRegistry(StrategyRegistry):
    """A StrategyRegistry that never writes: a plug-in that can't start (or has no config yet) is left out
    with a debug line instead of an `error` event (which the relay would send; the worker reports it). Debug,
    so `soak-report --json` prints nothing but the JSON (log lines go to stdout at INFO and above)."""

    def _report(self, key: str, stage: str, exc: BaseException) -> None:
        log.debug("soak.plugin_skipped", plugin=key, stage=stage, error_type=type(exc).__name__)


def readonly_plan(
    factory: sessionmaker[Session],
    clock: Clock,
    calendar: SessionCalendar,
    settings: Callable[[], RuntimeSettings],
    *,
    registry: StrategyRegistry | None = None,
) -> Callable[[date], DayPlan]:
    """`SoakDeps.plan`: `scheduler.live_day_plan` on the active live run (never created), or without one the
    `day_plan` of the enabled strategies (no exits-only ones). Plan problems are never reported, no strategy
    default is ensured. Today's strategy configs stand in for past days too."""
    reg = registry if registry is not None else _QuietRegistry(factory, clock)

    def plan(session_date: date) -> DayPlan:
        loaded = settings()
        run_id = active_live_run_id(factory)
        if run_id is not None:
            return live_day_plan(factory, reg, run_id, calendar, session_date, loaded)
        return day_plan([s for s, _ in reg.enabled()], calendar, session_date, loaded)

    return plan


def orb_enabled_reader(
    factory: sessionmaker[Session], clock: Clock, *, registry: StrategyRegistry | None = None
) -> Callable[[], bool]:
    """Whether orb_sip is enabled (its latest live config); no config yet counts as enabled."""
    reg = registry if registry is not None else _QuietRegistry(factory, clock)

    def enabled() -> bool:
        try:
            return reg.current(OrbSip.key).enabled
        except KeyError:
            return True

    return enabled


def _rows(factory: sessionmaker[Session], days: Sequence[date]) -> dict[date, list[JobRunRow]]:
    out: dict[date, list[JobRunRow]] = {d: [] for d in days}
    if not days:
        return out
    with factory() as s:
        found = s.execute(
            select(
                m.JobRun.session_date,
                m.JobRun.job,
                m.JobRun.status,
                m.JobRun.started_at,
                m.JobRun.finished_at,
                m.JobRun.error,
            ).where(m.JobRun.session_date.in_(list(days)))
        ).all()
    for session_date, job, status, started, finished, error in found:
        out[session_date].append(JobRunRow(job, status, started, finished, error))
    return out


def _token_events(factory: sessionmaker[Session], start: datetime, end: datetime) -> list[datetime]:
    with factory() as s:
        return list(
            s.execute(
                select(m.EventLog.ts).where(
                    m.EventLog.source == TOKEN_SOURCE,
                    m.EventLog.level == "error",
                    m.EventLog.ts > start,
                    m.EventLog.ts <= end,
                )
            ).scalars()
        )


def _marks(factory: sessionmaker[Session]) -> list[SoakMark]:
    with factory() as s:
        found = s.execute(
            select(m.EventLog.ts, m.EventLog.message, m.EventLog.data).where(m.EventLog.source == SOAK_SOURCE)
        ).all()
    out: list[SoakMark] = []
    for ts, message, data in found:
        data = data if isinstance(data, dict) else {}
        kind, day = data.get("mark"), data.get("session_date")
        if kind not in MARK_KINDS or not isinstance(day, str):
            continue
        try:
            out.append(SoakMark(date.fromisoformat(day), kind, message, ts))
        except ValueError:
            continue
    return out


def _last_line_at(factory: sessionmaker[Session]) -> datetime | None:
    """When the previous soak line went out (the latest `soak:` notification)."""
    with factory() as s:
        row = s.execute(
            select(m.Notification.sent_at, m.Notification.created_at)
            .where(m.Notification.dedupe_key.startswith(DEDUPE_PREFIX, autoescape=True))
            .order_by(m.Notification.created_at.desc(), m.Notification.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    sent_at: datetime | None = row.sent_at
    created_at: datetime = row.created_at
    return sent_at or created_at


def _as_of(rows: Sequence[JobRunRow], at: datetime) -> list[JobRunRow]:
    """The rows as they stood at `at`: later rows dropped, rows finished later seen still running."""
    out = []
    for r in rows:
        if r.started_at > at:
            continue
        if r.finished_at is not None and r.finished_at > at:
            r = dataclasses.replace(r, status="running", finished_at=None, error=None)
        out.append(r)
    return out


def _evaluate_window(
    cal: SessionCalendar,
    window: Sequence[date],
    plans: Mapping[date, DayPlan],
    settings: RuntimeSettings,
    orb_required: bool,
    rows: Mapping[date, list[JobRunRow]],
    token_events: Sequence[datetime],
    marks: Sequence[SoakMark],
    now: datetime,
) -> list[SoakDay]:
    in_force = effective_marks([mk for mk in marks if mk.at <= now])
    days: list[SoakDay] = []
    for d in window:
        day_rows = _as_of(rows.get(d, []), now)
        expected = expected_jobs(
            cal, d, plans[d], settings, {r.job for r in day_rows}, orb_required=orb_required
        )
        start = cal.session_close(cal.previous_session(d))
        end = _et_at(d, DEADLINES[TOKEN_JOB])
        failures = [t for t in token_events if start < t <= end and t <= now]
        mark = in_force.get(d)
        outage = mark.reason if mark is not None and mark.kind == "outage" else None
        day = evaluate_day(
            d,
            expected,
            day_rows,
            now,
            token_failures=failures,
            outage=outage,
            retry_attempts=settings.jobs_retry_attempts,
        )
        if mark is not None and mark.kind == "reset":
            day = dataclasses.replace(day, reset=True)
        days.append(day)
    return days


def load_report(
    deps: SoakDeps,
    *,
    through: date | None = None,
    sessions: int = DEFAULT_SESSIONS,
    target: int = DEFAULT_TARGET,
) -> SoakReport:
    """The report over the last `sessions` sessions up to `through` (default: the latest session on or
    before today's ET date). Read-only. `changed` compares the verdicts with the window evaluated as of the
    previous soak line. With `sessions` < `target` the count and the earliest finish are taken over the last
    `target` sessions (a shorter window would cap the count below the target), and only the last `sessions`
    days are reported."""
    cal = deps.calendar
    now = deps.clock.now()
    last = through if through is not None else default_through(cal, now)
    shown = max(sessions, 0)
    window = session_window(cal, last, max(sessions, target))
    settings = deps.settings()
    orb_required = deps.orb_enabled()
    plans = {d: deps.plan(d) for d in window}
    rows = _rows(deps.factory, window)
    token_events: list[datetime] = []
    if window:
        token_events = _token_events(
            deps.factory,
            cal.session_close(cal.previous_session(window[0])),
            _et_at(window[-1], DEADLINES[TOKEN_JOB]),
        )
    marks = _marks(deps.factory)
    days = _evaluate_window(cal, window, plans, settings, orb_required, rows, token_events, marks, now)
    report = build_report(
        days, cal=cal, through=window[-1] if window else last, target=target, now=now, env=deps.env
    )
    first_shown = window[-shown] if 0 < shown <= len(window) else None
    if shown < len(window):
        kept = tuple(d for d in report.days if first_shown is not None and d.session_date >= first_shown)
        report = dataclasses.replace(
            report, days=kept, total_clean=sum(1 for d in kept if d.verdict == "clean")
        )
    previous_at = _last_line_at(deps.factory)
    if previous_at is None or previous_at >= now:
        return report
    before = _evaluate_window(
        cal, window, plans, settings, orb_required, rows, token_events, marks, previous_at
    )
    shown_through = et_date(previous_at)
    changed: list[tuple[date, str]] = []
    for old, new in zip(before, days, strict=True):
        if new.session_date >= report.through or new.session_date > shown_through:
            continue
        if first_shown is None or new.session_date < first_shown:
            continue
        if new.verdict != "pending" and not new.provisional and new.verdict != old.verdict:
            changed.append((new.session_date, _verdict_words(new)))
    return dataclasses.replace(report, changed=tuple(changed))


# --- the Telegram line and marks ----------------------------------------------------------------------------


def dedupe_key(session_date: date, *, final: bool) -> str:
    return f"{DEDUPE_PREFIX}{session_date.isoformat()}" + (":final" if final else "")


def _line_status(factory: sessionmaker[Session], key: str) -> str | None:
    """The `notifications` status of the line's dedupe key (None: no row)."""
    with factory() as s:
        status: str | None = s.execute(
            select(m.Notification.status).where(m.Notification.dedupe_key == key)
        ).scalar_one_or_none()
    return status


def _already_sent(factory: sessionmaker[Session], key: str) -> bool:
    status = _line_status(factory, key)
    return status is not None and status != "failed"


# notify_report's outcome: "sent", "duplicate" (the key was already taken, nothing sent), "failed" (the
# notifier recorded the send as failed, unknown or still sending), "off" (no Telegram notifier).
SendOutcome = Literal["sent", "duplicate", "failed", "off"]


async def notify_report(deps: SoakDeps, report: SoakReport, *, final: bool) -> SendOutcome:
    """Send the line for the report's `through` session: at most one message per session (and one `final`
    one), deduplicated by the notifier's `notifications` table. The notifier never raises, so the outcome
    is read back from its row: `sent` only when the row says `sent` (a notifier that keeps no row, as in
    tests, counts as sent). The database reads run in a worker thread."""
    if isinstance(deps.notifier, NullNotifier):
        return "off"
    msg = deps.render.soak_line(line_view(report, final=final))
    key = msg.dedupe_key or dedupe_key(report.through, final=final)
    if await asyncio.to_thread(_already_sent, deps.factory, key):
        return "duplicate"
    await deps.notifier.send(dataclasses.replace(msg, dedupe_key=key))
    status = await asyncio.to_thread(_line_status, deps.factory, key)
    return "sent" if status in (None, "sent") else "failed"


def record_mark(
    factory: sessionmaker[Session],
    clock: Clock,
    run_id: int | None,
    kind: str,
    session_date: date,
    reason: str,
) -> None:
    """One `info` event (source `soak`) marking `session_date`: the relay never sends it (it relays errors
    only) and the log mirror ignores it. The reason is masked."""
    if kind not in MARK_KINDS:
        raise ValueError(f"mark must be one of {', '.join(MARK_KINDS)}")
    text = " ".join(redact_text(reason).split())[:MAX_REASON]
    with session_scope(factory) as s:
        log_event(
            s,
            clock,
            "info",
            SOAK_SOURCE,
            text,
            {"mark": kind, "session_date": session_date.isoformat()},
            run_id=run_id,
        )


# --- JSON and the table -------------------------------------------------------------------------------------


def _z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def report_json(report: SoakReport) -> dict[str, Any]:
    """The `--json` shape (the contract for P6-T3 and P6-T5)."""
    return {
        "env": report.env,
        "generated_at": _z(report.generated_at),
        "through": report.through.isoformat(),
        "target": report.target,
        "consecutive_clean": report.consecutive_clean,
        "total_clean": report.total_clean,
        "last_final": report.last_final.isoformat() if report.last_final else None,
        "earliest_finish": report.earliest_finish.isoformat() if report.earliest_finish else None,
        "days": [
            {
                "session_date": d.session_date.isoformat(),
                "verdict": d.verdict,
                "failed": list(d.failed),
                "orb_open": d.orb_open,
                "orb_open_seconds": d.orb_open_seconds,
                "reset": d.reset,
                "outage": d.outage,
                "provisional": d.provisional,
                "checks": [
                    {
                        "job": c.job,
                        "status": c.status,
                        "attempts": c.attempts,
                        "deadline": _z(c.deadline),
                        "finished_at": _z(c.finished_at),
                        "error": c.error,
                    }
                    for c in d.checks
                ],
            }
            for d in report.days
        ],
    }


def report_lines(report: SoakReport) -> list[str]:
    """The table: one line per session (date, verdict, failed checks with their errors, 9:35 seconds, marks)
    and a summary line."""
    lines = []
    for d in report.days:
        scan = f"{d.orb_open_seconds:.1f}s" if d.orb_open_seconds is not None else d.orb_open
        bad = [
            f"{c.job} {c.status}" + (f" ({c.error})" if c.error else "")
            for c in d.checks
            if c.status in FINAL_BAD
        ]
        marks = []
        if d.reset:
            marks.append("reset")
        if d.outage is not None:
            marks.append(f"outage ({d.outage})")
        if d.provisional:
            marks.append("provisional (catch-up possible)")
        lines.append(
            f"{d.session_date.isoformat()} {d.session_date:%a}  {d.verdict:<9}  9:35 {scan:<9}  "
            f"failed: {'; '.join(bad) or '-'}  marks: {', '.join(marks) or '-'}"
        )
    finish = report.earliest_finish.isoformat() if report.earliest_finish else "reached"
    last = report.last_final.isoformat() if report.last_final else "none"
    lines.append(
        f"{report.env}: {report.consecutive_clean}/{report.target} clean in a row, "
        f"{report.total_clean} clean in the window, last final {last}, earliest finish {finish}"
    )
    return lines
