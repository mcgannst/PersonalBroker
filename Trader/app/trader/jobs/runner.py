"""Runs a job once per (job, session_date) and records it in job_runs (SPEC §9).

Single flight: a PostgreSQL session-level advisory lock keyed on (job, session_date) is held on a
dedicated connection for the whole run, so a second process starting the same run at the same time
gets `skipped` ({"reason": "already running"}) instead of running the body twice. Any `running` row
found once the lock is held belongs to a process that died, and is marked `failed` ("abandoned").
"""

import hashlib
from collections.abc import Callable
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
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


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
    outcome is still returned (or the original error re-raised)."""
    key = lock_key(job, session_date)
    with _engine(factory).connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        # None until the server's answer is known: an interrupt can land after the server granted the
        # lock but before we see it, and that case must be unlocked too.
        acquired: bool | None = None
        try:
            acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar_one())
            if not acquired:
                return JobOutcome("skipped", {"reason": "already running"})
            return _run_locked(factory, clock, job, session_date, fn, force)
        finally:
            if acquired is not False and not conn.invalidated:
                _unlock(conn, key)


def _run_locked(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    fn: Callable[[], dict[str, Any]],
    force: bool,
) -> JobOutcome:
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
        run_id = run.id
    try:
        detail = fn()
    except BaseException as exc:
        error = _describe(exc)
        _record_failure(factory, clock, job, session_date, run_id, error)
        if not isinstance(exc, Exception):
            raise
        return JobOutcome("failed", error=error)
    with session_scope(factory) as s:
        row = s.get(JobRun, run_id)
        if row is None:
            raise JobRunMissing(f"job_runs row {run_id} disappeared")
        row.status, row.finished_at, row.detail = "succeeded", clock.now(), detail
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
