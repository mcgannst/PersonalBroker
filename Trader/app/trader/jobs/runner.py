"""Runs a job once per (job, session_date) and records it in job_runs (SPEC §9).

Single flight: a PostgreSQL session-level advisory lock keyed on (job, session_date) is held on a
dedicated connection for the whole run, so a second process starting the same run at the same time
gets `skipped` ({"reason": "already running"}) instead of running the body twice. Any `running` row
found once the lock is held belongs to a process that died, and is marked `failed` ("abandoned").

`run_job` takes a sync body and `run_job_async` an async one; both share every step around the body.
The database calls themselves are synchronous (short, on the calling thread), so a worker's event loop
only yields inside the body.

If the body succeeded but that can't be recorded (a DB error, or the row deleted underneath), the run
ends cleanly with a `failed` outcome instead of raising (P1-REVIEW should-fix 4), so a CLI prints one
line and exits 1 rather than a traceback. A best-effort `critical` event (source `job.<job>`, on a fresh
session) is written too, so the relay alerts Stephen (P3 fix round 1).

`rerun_abandoned=False` (P3 fix round 1, used by the scheduler for entry-type events): a leftover
`running` row means an earlier run's outcome is unknown (it may have placed orders). Instead of running
the body again, that row is settled as failed (OUTCOME_UNKNOWN) with one `critical` event, and the body
is not run. `force` still runs it.

`failure_level` (P3-REVIEW): the level of the event a failed body writes, `error` by default (the relay
alerts it). The scheduler passes `warning` for a safety event that keeps retrying past its alerted
attempts, so a persistently failing event is not one phone alert every two minutes until the close.

`retry` (P5-T15, SPEC §9 as amended): a `RetryPolicy(attempts=n, first_delay_s=d, backoff=b)` with n > 1
re-runs a failed body in-process, for the day-level cron jobs only (events keep the scheduler's own
retries). The single-flight lock is held across every attempt, so a second process starting the same run
while this one waits gets `skipped` ("already running"). Each attempt goes through `_start` (so "already
succeeded" still skips) and inserts its own job_runs row. A failed attempt k < n is recorded `failed` with
a `warning` event ("... (attempt k of n), retrying in S s"), then the runner sleeps d * b^(k-1) seconds
through the injected `sleep`; only the last failed attempt writes the alerting event ("... after n
attempts", at the caller's `failure_level`). A success after failed attempts adds `"attempts": k` to the
detail. Never retried: a `JobFailure` (deliberate), a non-Exception (interrupt, cancellation: recorded and
re-raised as above) and a success that could not be recorded. An interrupt during a wait writes one
`error` event (at `failure_level`) saying the retries stopped, and is re-raised with the lock released.
A (job, session) therefore can have `failed` rows followed by a `succeeded` one: its result is its latest
row. `None` or one attempt behaves exactly as above.

A policy's `deadline` (fix round 1) bounds the waits: a wait that would end after it is never started, and
that failure is the final, alerting one ("... (retries stopped: deadline)", data `stopped: deadline`).
Callers that must finish by a fixed time (preopen by 09:28 ET, premarket before preopen) pass one.
Log lines that duplicate a critical event written here carry `event_logged=True`, so the log mirror
(P5-T14) does not add a second row for the same incident.
"""

import asyncio
import hashlib
import math
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import Connection, Engine, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import JobRun
from trader.db.session import session_scope
from trader.events import log_event
from trader.logging_setup import redact_text
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("jobs.runner")

MAX_ERROR_CHARS = 2000
OUTCOME_UNKNOWN = "outcome unknown: not re-run automatically"


@dataclass(frozen=True)
class RetryPolicy:
    """In-process retries of a day-level job (SPEC §9 as amended in Phase 5): up to `attempts` runs in all,
    waiting `first_delay_s`, then `first_delay_s * backoff`, and so on.

    `deadline` (an aware datetime, fix round 1): a wait that would end after it is never started. The
    failure that would have been retried is then the final one and alerts ("retries stopped: deadline"),
    so a pre-open job never retries into the open."""

    attempts: int = 1
    first_delay_s: float = 120.0
    backoff: float = 2.0
    deadline: datetime | None = None

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("RetryPolicy.attempts must be at least 1")
        if not math.isfinite(self.first_delay_s) or self.first_delay_s < 0:
            raise ValueError("RetryPolicy.first_delay_s must be a finite number of seconds, at least 0")
        if not math.isfinite(self.backoff) or self.backoff < 1:
            raise ValueError("RetryPolicy.backoff must be finite and at least 1")
        if self.deadline is not None and self.deadline.utcoffset() is None:
            raise ValueError("RetryPolicy.deadline must be timezone-aware")

    @classmethod
    def from_settings(cls, s: RuntimeSettings, *, deadline: datetime | None = None) -> "RetryPolicy":
        return cls(
            attempts=s.jobs_retry_attempts,
            first_delay_s=float(s.jobs_retry_delay_seconds),
            deadline=deadline,
        )

    def delay(self, attempt: int) -> float:
        """The wait after failed attempt `attempt` (1-based)."""
        return self.first_delay_s * self.backoff ** (attempt - 1)


def _attempts(retry: RetryPolicy | None) -> int:
    return retry.attempts if retry is not None and retry.attempts > 1 else 1


_LEVEL_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}


def _at_most(level: str, cap: str) -> str:
    """`level`, lowered to `cap` when the caller asked for a lower failure level (never raised)."""
    return level if _LEVEL_RANK.get(level, 0) <= _LEVEL_RANK.get(cap, 3) else cap


def _retryable(exc: BaseException) -> bool:
    """A transient failure worth another attempt: any Exception except a deliberate JobFailure."""
    return isinstance(exc, Exception) and not isinstance(exc, JobFailure)


class JobRunMissing(RuntimeError):
    """The job_runs row this run created is gone (deleted underneath us)."""


class JobFailure(Exception):
    """A deliberate failure raised by a job body: its message is recorded as the error text as is,
    without the exception type in front (e.g. the scheduler's `missed: 295s late`)."""


@dataclass(frozen=True)
class JobOutcome:
    status: Literal["succeeded", "failed", "skipped"]
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def lock_key(job: str, session_date: date) -> int:
    """A stable signed 64-bit advisory-lock key for (job, session_date), the same in every process."""
    digest = hashlib.blake2b(f"trader.job:{job}:{session_date.isoformat()}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _describe(exc: BaseException) -> str:
    """The stored error text: masked (job_runs.error and the event are shown in the web app and relayed;
    an exception's text can quote a token URL), then capped."""
    text_ = str(exc) if isinstance(exc, JobFailure) else f"{type(exc).__name__}: {exc}"
    return redact_text(text_)[:MAX_ERROR_CHARS]


def _engine(factory: sessionmaker[Session]) -> Engine:
    bind = factory.kw.get("bind")
    if not isinstance(bind, Engine):
        raise TypeError("run_job needs a sessionmaker bound to an Engine")
    return bind


def _unlock(conn: Connection, key: int) -> None:
    """Release the lock. If that fails in any way (a DB error, or an interrupt mid-call), discard the
    connection: the lock lives as long as the session, so it must never go back to the pool locked.
    A non-Exception (KeyboardInterrupt, CancelledError) is re-raised once the connection is gone."""
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    except BaseException as exc:
        log.exception("job.unlock_failed", key=key)
        conn.invalidate()
        if not isinstance(exc, Exception):
            raise


@contextmanager
def _single_flight(factory: sessionmaker[Session], job: str, session_date: date) -> Iterator[bool]:
    """Try the (job, session_date) advisory lock on a dedicated connection; yields whether it was taken.
    The lock is released on the way out, however the block ends."""
    key = lock_key(job, session_date)
    with _engine(factory).connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        # None until the server's answer is known: an interrupt can land after the server granted the
        # lock but before we see it, and that case must be unlocked too.
        acquired: bool | None = None
        try:
            acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar_one())
            yield acquired
        finally:
            if acquired is not False and not conn.invalidated:
                _unlock(conn, key)


def run_job(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    fn: Callable[[], dict[str, Any]],
    force: bool = False,
    *,
    rerun_abandoned: bool = True,
    failure_level: str = "error",
    retry: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> JobOutcome:
    """Run `fn` for (job, session_date) unless it already succeeded (then `skipped`, unless `force`)
    or another process is running it right now (`skipped`, reason "already running").

    A failure of `fn` is recorded (status `failed`, error truncated to 2000 characters, an error event)
    and returned. KeyboardInterrupt, CancelledError and other non-Exception errors are recorded the
    same way and then re-raised. If recording a failure itself fails, that is logged and the original
    outcome is still returned (or the original error re-raised). If recording the success fails, the
    outcome is `failed` ("succeeded but could not be recorded: <type>"), a best-effort critical event
    is written, and nothing is raised. With `rerun_abandoned=False` a leftover `running` row is settled
    as failed (OUTCOME_UNKNOWN) and the body is not run."""
    tries = _Tries(factory, clock, job, session_date, failure_level, retry)
    with _single_flight(factory, job, session_date) as acquired:
        if not acquired:
            return JobOutcome("skipped", {"reason": "already running"})
        attempt = 1
        while True:
            started = _start(factory, clock, job, session_date, force, rerun_abandoned)
            if isinstance(started, JobOutcome):
                return started
            try:
                detail = fn()
            except BaseException as exc:
                if not tries.again(exc, attempt):
                    return tries.failed(started, exc, attempt)
                error, wait = tries.retrying(started, exc, attempt)
                try:
                    sleep(wait)
                except BaseException as stop:
                    return tries.interrupted(error, stop, attempt)
                attempt += 1
                continue
            return _record_success(factory, clock, job, session_date, started, tries.detail(detail, attempt))


async def run_job_async(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    fn: Callable[[], Awaitable[dict[str, Any]]],
    force: bool = False,
    *,
    rerun_abandoned: bool = True,
    failure_level: str = "error",
    retry: RetryPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> JobOutcome:
    """run_job for an async body, with exactly run_job's semantics. A cancellation of the awaiting task
    (CancelledError) is recorded as a failure and re-raised, and the lock is released."""
    tries = _Tries(factory, clock, job, session_date, failure_level, retry)
    with _single_flight(factory, job, session_date) as acquired:
        if not acquired:
            return JobOutcome("skipped", {"reason": "already running"})
        attempt = 1
        while True:
            started = _start(factory, clock, job, session_date, force, rerun_abandoned)
            if isinstance(started, JobOutcome):
                return started
            try:
                detail = await fn()
            except BaseException as exc:
                if not tries.again(exc, attempt):
                    return tries.failed(started, exc, attempt)
                error, wait = tries.retrying(started, exc, attempt)
                try:
                    await sleep(wait)
                except BaseException as stop:
                    return tries.interrupted(error, stop, attempt)
                attempt += 1
                continue
            return _record_success(factory, clock, job, session_date, started, tries.detail(detail, attempt))


def _start(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    force: bool,
    rerun_abandoned: bool = True,
) -> int | JobOutcome:
    """With the lock held: `skipped` if the run already succeeded (and not `force`), else mark any
    leftover `running` row abandoned and insert this run's `running` row, returning its id. With
    `rerun_abandoned=False` (and not `force`) a leftover `running` row is settled as OUTCOME_UNKNOWN
    with one critical event instead, and a `failed` outcome is returned without a new row."""
    same_run = (JobRun.job == job, JobRun.session_date == session_date)
    with session_scope(factory) as s:
        done = s.execute(
            select(JobRun.id).where(*same_run, JobRun.status == "succeeded").limit(1)
        ).scalar_one_or_none()
        if done is not None and not force:
            return JobOutcome("skipped", {"reason": "already succeeded"})
        # We hold the lock, so nobody else is running this: a `running` row is left over from a crash
        # (or from a success that could not be recorded).
        if not rerun_abandoned and not force:
            leftover = list(
                s.execute(
                    select(JobRun.id).where(*same_run, JobRun.status == "running").order_by(JobRun.id)
                ).scalars()
            )
            if leftover:
                s.execute(
                    update(JobRun)
                    .where(JobRun.id.in_(leftover))
                    .values(status="failed", finished_at=clock.now(), error=OUTCOME_UNKNOWN)
                )
                log_event(
                    s,
                    clock,
                    "critical",
                    f"job.{job}",
                    f"{job} for {session_date}: an earlier run's outcome is unknown, so it is not re-run "
                    "automatically. Check the orders, then run it by hand with --force if needed.",
                    {"job_run_ids": leftover, "session_date": session_date.isoformat()},
                )
                # the critical event above is the record: the log mirror must not add a second row
                log.critical(
                    "job.outcome_unknown", job=job, session_date=session_date.isoformat(), event_logged=True
                )
                return JobOutcome("failed", {"reason": "outcome unknown"}, error=OUTCOME_UNKNOWN)
        s.execute(
            update(JobRun)
            .where(*same_run, JobRun.status == "running")
            .values(status="failed", finished_at=clock.now(), error="abandoned")
        )
        run = JobRun(job=job, session_date=session_date, started_at=clock.now(), status="running")
        s.add(run)
        s.flush()
        return run.id


@dataclass(frozen=True)
class _Tries:
    """The attempts of one run_job / run_job_async call: what each failure records, and whether to go on."""

    factory: sessionmaker[Session]
    clock: Clock
    job: str
    session_date: date
    failure_level: str
    retry: RetryPolicy | None

    @property
    def attempts(self) -> int:
        return _attempts(self.retry)

    def _past_deadline(self, attempt: int) -> bool:
        """Whether the wait after failed attempt `attempt` would end after the policy's deadline."""
        if self.retry is None or self.retry.deadline is None:
            return False
        wait = timedelta(seconds=self.retry.delay(attempt))
        return self.clock.now() + wait > self.retry.deadline

    def again(self, exc: BaseException, attempt: int) -> bool:
        return attempt < self.attempts and _retryable(exc) and not self._past_deadline(attempt)

    def _data(self, error: str, attempt: int) -> dict[str, Any]:
        if self.attempts == 1:
            return {"error": error}  # exactly P3's event
        return {"error": error, "attempt": attempt, "attempts": self.attempts}

    def failed(self, run_id: int, exc: BaseException, attempt: int) -> JobOutcome:
        """The run's final failure: the alerting event at the caller's level. Re-raises a non-Exception
        (interrupt, cancellation) once recorded."""
        error = _describe(exc)
        message = f"{self.job} failed for {self.session_date}"
        data = self._data(error, attempt)
        if self.attempts > 1 and _retryable(exc):
            if attempt == self.attempts:
                message += f" after {self.attempts} attempts"
            else:  # retries were left, but the next wait would end after the deadline
                message += f" after attempt {attempt} of {self.attempts} (retries stopped: deadline)"
                data["stopped"] = "deadline"
        _record_failure(
            self.factory,
            self.clock,
            self.job,
            self.session_date,
            run_id,
            error,
            self.failure_level,
            message=message,
            data=data,
        )
        if not isinstance(exc, Exception):
            raise exc
        return JobOutcome("failed", error=error)

    def retrying(self, run_id: int, exc: BaseException, attempt: int) -> tuple[str, float]:
        """Record a failed attempt that will be retried (a `warning`, never an alert); returns the error
        text and the wait before the next attempt."""
        assert self.retry is not None  # attempts > 1
        error = _describe(exc)
        wait = self.retry.delay(attempt)
        _record_failure(
            self.factory,
            self.clock,
            self.job,
            self.session_date,
            run_id,
            error,
            _at_most("warning", self.failure_level),
            message=(
                f"{self.job} failed for {self.session_date} (attempt {attempt} of {self.attempts}), "
                f"retrying in {wait:g} s"
            ),
            data=self._data(error, attempt),
        )
        log.warning(
            "job.retrying",
            job=self.job,
            session_date=self.session_date.isoformat(),
            attempt=attempt,
            attempts=self.attempts,
            wait_s=wait,
        )
        return error, wait

    def interrupted(self, error: str, stop: BaseException, attempt: int) -> JobOutcome:
        """The wait between attempts was interrupted: the retries end here, so write the alerting event
        (best effort, on a fresh session), then re-raise a non-Exception or return the failure."""
        name = type(stop).__name__
        try:
            with session_scope(self.factory) as s:
                log_event(
                    s,
                    self.clock,
                    self.failure_level,
                    f"job.{self.job}",
                    f"{self.job} failed for {self.session_date}: retries stopped after attempt {attempt} "
                    f"of {self.attempts} ({name})",
                    {**self._data(error, attempt), "interrupted": name},
                )
        except Exception:
            log.exception(
                "job.record_interrupt_failed", job=self.job, session_date=self.session_date.isoformat()
            )
        if not isinstance(stop, Exception):
            raise stop
        return JobOutcome("failed", error=error)

    @staticmethod
    def detail(detail: dict[str, Any], attempt: int) -> dict[str, Any]:
        """The body's detail, plus the attempts it took when more than one. A body's own `attempts` key
        is never overwritten: the count then goes in `retry_attempts`."""
        if attempt <= 1:
            return detail
        key = "retry_attempts" if "attempts" in detail else "attempts"
        return {**detail, key: attempt}


def _record_success(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    run_id: int,
    detail: dict[str, Any],
) -> JobOutcome:
    try:
        with session_scope(factory) as s:
            row = s.get(JobRun, run_id)
            if row is None:
                raise JobRunMissing(f"job_runs row {run_id} disappeared")
            row.status, row.finished_at, row.detail = "succeeded", clock.now(), detail
    except Exception as exc:
        # the critical event written next is the record (if that fails too, its own log line is mirrored)
        log.critical(
            "job.record_success_failed",
            job=job,
            session_date=session_date.isoformat(),
            run_id=run_id,
            error_type=type(exc).__name__,
            event_logged=True,
        )
        _alert_unrecorded_success(factory, clock, job, session_date, run_id, type(exc).__name__)
        return JobOutcome(
            "failed", detail, error=f"succeeded but could not be recorded: {type(exc).__name__}"
        )
    return JobOutcome("succeeded", detail)


def _alert_unrecorded_success(
    factory: sessionmaker[Session], clock: Clock, job: str, session_date: date, run_id: int, error_type: str
) -> None:
    """Best effort, on a fresh session: a critical event, so the relay tells Stephen the job ran but its
    success is not in job_runs. Never raises."""
    try:
        with session_scope(factory) as s:
            log_event(
                s,
                clock,
                "critical",
                f"job.{job}",
                f"{job} for {session_date} succeeded but could not be recorded ({error_type})",
                {"job_run_id": run_id, "session_date": session_date.isoformat(), "error_type": error_type},
            )
    except Exception as exc:
        log.critical(
            "job.alert_unrecorded_success_failed",
            job=job,
            session_date=session_date.isoformat(),
            run_id=run_id,
            error_type=type(exc).__name__,
        )


def _record_failure(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    run_id: int,
    error: str,
    level: str = "error",
    *,
    message: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    try:
        with session_scope(factory) as s:
            row = s.get(JobRun, run_id)
            if row is None:
                raise JobRunMissing(f"job_runs row {run_id} disappeared")
            row.status, row.finished_at, row.error = "failed", clock.now(), error
            log_event(
                s,
                clock,
                level,
                f"job.{job}",
                message or f"{job} failed for {session_date}",
                data if data is not None else {"error": error},
            )
    except Exception:
        log.exception(
            "job.record_failure_failed", job=job, session_date=session_date.isoformat(), run_id=run_id
        )
