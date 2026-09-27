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
line and exits 1 rather than a traceback.
"""

import hashlib
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import structlog
from sqlalchemy import Connection, Engine, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import JobRun
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock

log = structlog.get_logger("jobs.runner")

MAX_ERROR_CHARS = 2000


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
    text_ = str(exc) if isinstance(exc, JobFailure) else f"{type(exc).__name__}: {exc}"
    return text_[:MAX_ERROR_CHARS]


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
) -> JobOutcome:
    """Run `fn` for (job, session_date) unless it already succeeded (then `skipped`, unless `force`)
    or another process is running it right now (`skipped`, reason "already running").

    A failure of `fn` is recorded (status `failed`, error truncated to 2000 characters, an error event)
    and returned. KeyboardInterrupt, CancelledError and other non-Exception errors are recorded the
    same way and then re-raised. If recording a failure itself fails, that is logged and the original
    outcome is still returned (or the original error re-raised). If recording the success fails, the
    outcome is `failed` ("succeeded but could not be recorded: <type>") and nothing is raised."""
    with _single_flight(factory, job, session_date) as acquired:
        if not acquired:
            return JobOutcome("skipped", {"reason": "already running"})
        started = _start(factory, clock, job, session_date, force)
        if isinstance(started, JobOutcome):
            return started
        try:
            detail = fn()
        except BaseException as exc:
            return _failed(factory, clock, job, session_date, started, exc)
        return _record_success(factory, clock, job, session_date, started, detail)


async def run_job_async(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    fn: Callable[[], Awaitable[dict[str, Any]]],
    force: bool = False,
) -> JobOutcome:
    """run_job for an async body, with exactly run_job's semantics. A cancellation of the awaiting task
    (CancelledError) is recorded as a failure and re-raised, and the lock is released."""
    with _single_flight(factory, job, session_date) as acquired:
        if not acquired:
            return JobOutcome("skipped", {"reason": "already running"})
        started = _start(factory, clock, job, session_date, force)
        if isinstance(started, JobOutcome):
            return started
        try:
            detail = await fn()
        except BaseException as exc:
            return _failed(factory, clock, job, session_date, started, exc)
        return _record_success(factory, clock, job, session_date, started, detail)


def _start(
    factory: sessionmaker[Session], clock: Clock, job: str, session_date: date, force: bool
) -> int | JobOutcome:
    """With the lock held: `skipped` if the run already succeeded (and not `force`), else mark any
    leftover `running` row abandoned and insert this run's `running` row, returning its id."""
    same_run = (JobRun.job == job, JobRun.session_date == session_date)
    with session_scope(factory) as s:
        done = s.execute(
            select(JobRun.id).where(*same_run, JobRun.status == "succeeded").limit(1)
        ).scalar_one_or_none()
        if done is not None and not force:
            return JobOutcome("skipped", {"reason": "already succeeded"})
        # We hold the lock, so nobody else is running this: a `running` row is left over from a crash.
        s.execute(
            update(JobRun)
            .where(*same_run, JobRun.status == "running")
            .values(status="failed", finished_at=clock.now(), error="abandoned")
        )
        run = JobRun(job=job, session_date=session_date, started_at=clock.now(), status="running")
        s.add(run)
        s.flush()
        return run.id


def _failed(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    run_id: int,
    exc: BaseException,
) -> JobOutcome:
    """Record the body's failure; re-raise a non-Exception (interrupt, cancellation) once recorded."""
    error = _describe(exc)
    _record_failure(factory, clock, job, session_date, run_id, error)
    if not isinstance(exc, Exception):
        raise exc
    return JobOutcome("failed", error=error)


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
        log.critical(
            "job.record_success_failed",
            job=job,
            session_date=session_date.isoformat(),
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        return JobOutcome(
            "failed", detail, error=f"succeeded but could not be recorded: {type(exc).__name__}"
        )
    return JobOutcome("succeeded", detail)


def _record_failure(
    factory: sessionmaker[Session], clock: Clock, job: str, session_date: date, run_id: int, error: str
) -> None:
    try:
        with session_scope(factory) as s:
            row = s.get(JobRun, run_id)
            if row is None:
                raise JobRunMissing(f"job_runs row {run_id} disappeared")
            row.status, row.finished_at, row.error = "failed", clock.now(), error
            log_event(s, clock, "error", f"job.{job}", f"{job} failed for {session_date}", {"error": error})
    except Exception:
        log.exception(
            "job.record_failure_failed", job=job, session_date=session_date.isoformat(), run_id=run_id
        )
