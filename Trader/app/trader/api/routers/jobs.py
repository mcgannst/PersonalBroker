"""GET /api/jobs, POST /api/jobs/{job}/run (202) (BR-55, SPEC §11 `/jobs`, §12 System).

- `GET /api/jobs?job=&limit=100`: `job_runs` rows newest first (by start), optionally of one job name.
- `POST /api/jobs/{job}/run` (body `JobRunIn`, optional): a manual run of one `ManualJob` through
  `services.jobs` (`trader.api.launcher.SubprocessJobLauncher`): 404 for any other name, 422 for a date that
  is not a session or for options `token-refresh` doesn't take, 409 while a launched run of that job is still
  going. The launcher writes the audit row `job.run_manual`; the job records its own `job_runs` row.

Registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from trader.api.deps import CsrfUser, CurrentUser, Services, actor
from trader.api.launcher import already_running, check_request
from trader.api.schemas import Items, JobLaunchOut, JobRunIn, JobRunOut
from trader.api.views import redacted_json
from trader.db import models as m
from trader.logging_setup import redact_text

router = APIRouter(tags=["jobs"])

MAX_LIMIT = 500


def job_run_out(row: m.JobRun) -> JobRunOut:
    """One `job_runs` row; its error and detail masked, its duration in seconds once it finished."""
    duration = (row.finished_at - row.started_at).total_seconds() if row.finished_at is not None else None
    return JobRunOut(
        id=row.id,
        job=row.job,
        session_date=row.session_date,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        error=redact_text(row.error) if row.error is not None else None,
        detail=redacted_json(row.detail) if isinstance(row.detail, dict) else None,
        duration_seconds=duration,
    )


@router.get("/jobs")
def list_jobs(
    _user: CurrentUser,
    services: Services,
    job: Annotated[str | None, Query(max_length=50)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
) -> Items[JobRunOut]:
    stmt = select(m.JobRun).order_by(m.JobRun.started_at.desc(), m.JobRun.id.desc()).limit(limit)
    if job:
        stmt = stmt.where(m.JobRun.job == job)
    with services.core.factory() as s:
        return Items[JobRunOut](items=[job_run_out(r) for r in s.scalars(stmt)])


@router.post("/jobs/{job}/run", status_code=202)
async def run_job(job: str, user: CsrfUser, services: Services, body: JobRunIn | None = None) -> JobLaunchOut:
    request = body or JobRunIn()
    manual = check_request(services.core.calendar, job, request.date, request.force)
    launcher = services.jobs
    if launcher.running(manual):  # in memory: the launcher's own table of children
        raise already_running(manual)
    return await launcher.launch(manual, request.date, request.force, actor(user))
