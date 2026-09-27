"""Manual job runs from the System page: `trader <args> [--date YYYY-MM-DD] [--force]` as a child process with
the API's environment, not awaited (a reaper task waits for it); a non-zero exit writes one `warning` event.

- Only the `ManualJob` names run (`CLI_ARGS`), else 404. A `date` must be a trading session, else 422.
  `token-refresh` takes no `--date` or `--force` on trunk, so either one for it is a 422.
- One launched child per job at a time: a second launch while it runs is a 409 "already running". The job's
  own `run_job` lock and skip rules still apply inside the child.
- The child's output is inherited (it reaches `docker logs`); none of it is stored. When it exits non-zero
  the launcher writes one `warning` event (source `jobs.manual`, data: job, date, exit code), so a refusal
  that happens before the job records a `job_runs` row still shows on the System page.
- Every launch writes an `audit_log` row `job.run_manual` (after: job, date, force) before the child starts.
"""

import asyncio
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import anyio
import structlog
from sqlalchemy.orm import Session, sessionmaker

from trader.api.errors import ApiError
from trader.api.schemas import JobLaunchOut, ManualJob
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date

log = structlog.get_logger("api.launcher")

CLI_ARGS: Mapping[ManualJob, tuple[str, ...]] = {
    "nightly": ("nightly",),
    "premarket": ("premarket",),
    "preopen": ("preopen",),
    "postclose": ("postclose",),
    "token-refresh": ("token-refresh",),  # takes no --date or --force on trunk
}
NO_OPTIONS: frozenset[str] = frozenset({"token-refresh"})
EXIT_EVENT_SOURCE = "jobs.manual"
AUDIT_ACTION = "job.run_manual"


def _invalid(msg: str, loc: str) -> ApiError:
    return ApiError(422, "validation", msg, [{"loc": ["body", loc], "msg": msg}])


def check_request(calendar: SessionCalendar, job: str, session_date: date | None, force: bool) -> ManualJob:
    """The job name as a `ManualJob` when the request may run: 404 for a name that is not a manual job, 422
    for options `token-refresh` doesn't take or a date that is not a trading session."""
    if job not in CLI_ARGS:
        raise ApiError(404, "not_found", "Unknown job")
    if job in NO_OPTIONS:
        if session_date is not None:
            raise _invalid(f"{job} takes no date", "date")
        if force:
            raise _invalid(f"{job} takes no force option", "force")
    if session_date is not None:
        try:
            is_session = calendar.is_session(session_date)
        except ValueError:  # outside the calendar's range
            is_session = False
        if not is_session:
            raise _invalid("The date is not a trading session", "date")
    return job  # type: ignore[return-value]  # checked against CLI_ARGS above


def command(executable: str, job: ManualJob, session_date: date | None, force: bool) -> tuple[str, ...]:
    """The argv of a manual run."""
    argv = (executable, *CLI_ARGS[job])
    if session_date is not None:
        argv += ("--date", session_date.isoformat())
    if force:
        argv += ("--force",)
    return argv


def already_running(job: str) -> ApiError:
    return ApiError(409, "conflict", f"{job} is already running")


class SubprocessJobLauncher:
    """Implements `trader.api.deps.JobLauncher` with child processes of this (single) API process."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        *,
        executable: str = "trader",
        spawn: Callable[..., Any] = asyncio.create_subprocess_exec,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._calendar = calendar
        self._executable = executable
        self._spawn = spawn
        self._children: dict[str, Any] = {}  # job -> its running child process
        self._starting: set[str] = set()  # jobs between the checks and the spawn
        self._reapers: set[asyncio.Task[None]] = set()  # strong references until each finishes

    async def launch(
        self, job: ManualJob, session_date: date | None, force: bool, actor: str
    ) -> JobLaunchOut:
        job = check_request(self._calendar, job, session_date, force)
        if self.running(job):
            raise already_running(job)
        self._starting.add(job)
        try:
            await anyio.to_thread.run_sync(self._audit, actor, job, session_date, force)
            shown = session_date or self._default_date(job)
            argv = command(self._executable, job, session_date, force)
            try:
                proc = await self._spawn(*argv, stdin=asyncio.subprocess.DEVNULL)
            except Exception as exc:
                log.error("api.job_launch_failed", job=job, error_type=type(exc).__name__)
                return JobLaunchOut(
                    job=job,
                    session_date=shown,
                    launched=False,
                    message=f"Could not start {job} ({type(exc).__name__})",
                )
            self._children[job] = proc
        finally:
            self._starting.discard(job)
        log.info("api.job_launched", job=job, session_date=session_date, force=force, pid=proc.pid)
        task = asyncio.create_task(self._reap(job, session_date, proc), name=f"reap-{job}")
        self._reapers.add(task)
        task.add_done_callback(self._reapers.discard)
        suffix = f" for {shown.isoformat()}" if shown is not None else ""
        return JobLaunchOut(
            job=job,
            session_date=shown,
            launched=True,
            message=f"{job}{suffix} started. Its run appears in the job list when it records one.",
        )

    def running(self, job: str) -> bool:
        return job in self._children or job in self._starting

    def children(self) -> dict[str, int]:
        """The running children: job -> pid (for tests and diagnostics)."""
        return {job: proc.pid for job, proc in self._children.items()}

    # --- internals ------------------------------------------------------------------------------------------

    def _default_date(self, job: ManualJob) -> date | None:
        """The session the command picks without `--date` (shown only; the argv carries no date): the next
        session for `nightly`, today's ET date for the session jobs, none for `token-refresh`."""
        if job in NO_OPTIONS:
            return None
        today = et_date(self._clock.now())
        if job == "nightly":
            try:
                return self._calendar.next_session(today)
            except ValueError:
                return None
        return today

    async def _reap(self, job: str, session_date: date | None, proc: Any) -> None:
        code: int | None = None
        try:
            code = await proc.wait()
        except Exception as exc:
            log.error("api.job_wait_failed", job=job, error_type=type(exc).__name__)
        finally:
            if self._children.get(job) is proc:
                del self._children[job]
        log.info("api.job_exited", job=job, exit_code=code)
        if code:
            try:
                await anyio.to_thread.run_sync(self._record_exit, job, session_date, code)
            except Exception as exc:
                log.error("api.job_exit_event_failed", job=job, error_type=type(exc).__name__)

    def _audit(self, actor: str, job: str, session_date: date | None, force: bool) -> None:
        after = {"job": job, "date": session_date.isoformat() if session_date else None, "force": force}
        with session_scope(self._factory) as s:
            s.add(
                m.AuditLog(ts=self._clock.now(), actor=actor, action=AUDIT_ACTION, before=None, after=after)
            )

    def _record_exit(self, job: str, session_date: date | None, code: int) -> None:
        with session_scope(self._factory) as s:
            log_event(
                s,
                self._clock,
                "warning",
                EXIT_EVENT_SOURCE,
                f"Manual {job} run exited with code {code}",
                {"job": job, "date": session_date.isoformat() if session_date else None, "exit_code": code},
            )
