"""Runs a job once per (job, session_date) and records it in job_runs (SPEC §9)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import JobRun
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock


@dataclass(frozen=True)
class JobOutcome:
    status: Literal["succeeded", "failed", "skipped"]
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def run_job(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    fn: Callable[[], dict[str, Any]],
    force: bool = False,
) -> JobOutcome:
    with session_scope(factory) as s:
        done = s.execute(
            select(JobRun.id)
            .where(JobRun.job == job, JobRun.session_date == session_date, JobRun.status == "succeeded")
            .limit(1)
        ).scalar_one_or_none()
        if done is not None and not force:
            return JobOutcome("skipped")
        run = JobRun(job=job, session_date=session_date, started_at=clock.now(), status="running")
        s.add(run)
        s.flush()
        run_id = run.id
    try:
        detail = fn()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with session_scope(factory) as s:
            row = s.get(JobRun, run_id)
            assert row is not None
            row.status, row.finished_at, row.error = "failed", clock.now(), error
            log_event(s, clock, "error", f"job.{job}", f"{job} failed for {session_date}", {"error": error})
        return JobOutcome("failed", error=error)
    with session_scope(factory) as s:
        row = s.get(JobRun, run_id)
        assert row is not None
        row.status, row.finished_at, row.detail = "succeeded", clock.now(), detail
    return JobOutcome("succeeded", detail)
