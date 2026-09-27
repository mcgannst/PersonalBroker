"""GET /api/dashboard -> DashboardOut: the day at a glance (session, timeline, pending proposals, positions,
P&L, kill switches, events, token, worker, top candidates). SPEC §11, §12 (Dashboard); BR-50, BR-33.

The numbers are the ones Telegram shows: positions from `trader.notify.views.position_lines` (`/positions`),
the P&L from `trader.notify.views.pnl_view` (what `/pnl` shows), pending proposals through
`notify.views.proposal_view`, kill switches, token and worker through `trader.api.views`.

The dashboard is live-only: it takes `run=live` (SPEC §11 lists the parameter; it is also the default) and
answers 422 for any other value, since a replay run has no "today".

The timeline (session days only) lists the crontab's day-level jobs (`DAY_JOBS`, checked against
`docker/crontab` by a test) and the day plan's events, each with its status from `job_runs`; the first
upcoming item at or after now is `next`.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal

import anyio.to_thread
import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from trader.api import views as api_views
from trader.api.deps import ApiServices, Services, _settings, current_user, live_run_id
from trader.api.routers.trading import candidate_out, open_positions
from trader.api.schemas import (
    CandidateOut,
    DashboardOut,
    EventOut,
    KillSwitchOut,
    PnlOut,
    ProposalOut,
    SessionInfoOut,
    TimelineItemOut,
    TimelineStatus,
    TokenOut,
    WorkerOut,
)
from trader.db import models as m
from trader.engine.scheduler import EVENT_JOB_PREFIX, MISSED_PREFIX, DayPlan, event_job
from trader.logging_setup import redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET
from trader.market.sessions import current_session, session_phase
from trader.notify import views

log = structlog.get_logger("api.dashboard")

# Every route needs a signed-in session (SPEC §14).
router = APIRouter(tags=["dashboard"], dependencies=[Depends(current_user)])

EVENT_LEVELS = ("info", "warning", "error", "critical")  # the dashboard's event list: info and above
EVENTS_SHOWN = 20
CANDIDATES_SHOWN = 5
DETAIL_CHARS = 200


@dataclass(frozen=True, slots=True)
class DayJob:
    """A day-level cron job on the timeline: `job_name` is its `job_runs.job`, `et_time` its crontab time."""

    key: str
    label: str
    job_name: str
    et_time: time

    @property
    def command(self) -> str:
        """The crontab command after `trader` (`checkin@11:30` runs as `checkin --at 11:30`)."""
        name, _, at = self.job_name.partition("@")
        return f"{name} --at {at}" if at else name


NIGHTLY = "nightly"  # runs at 20:00 ET the evening before the session it prepares
CHECKIN_1330 = "checkin_1330"  # left out when the session closes at or before 13:30 ET

DAY_JOBS: tuple[DayJob, ...] = (
    DayJob(NIGHTLY, "Nightly universe", "nightly", time(20, 0)),
    DayJob("premarket", "Pre-market scan", "premarket", time(8, 0)),
    DayJob("preopen", "Pre-open check", "preopen", time(9, 20)),
    DayJob("checkin_1130", "Check-in 11:30", "checkin@11:30", time(11, 30)),
    DayJob(CHECKIN_1330, "Check-in 13:30", "checkin@13:30", time(13, 30)),
    DayJob("postclose", "Post-close summary", "postclose", time(16, 15)),
)

EVENT_LABELS = {
    "orb_open": "ORB entry (orb_open)",
    "entry_cancel": "Cancel unfilled entries",
    "overlay_decision": "SPY overlay decision",
    "flatten": "Flatten",
}


@dataclass(frozen=True, slots=True)
class RunRow:
    """One `job_runs` row as the timeline needs it (rows are given oldest first)."""

    job: str
    status: str  # running | succeeded | failed
    error: str | None


def _et(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


def _detail(error: str | None) -> str | None:
    return redact_text(error)[:DETAIL_CHARS] if error else None


def _job_status(rows: Sequence[RunRow]) -> tuple[TimelineStatus, str | None]:
    """The newest run decides: succeeded -> done, failed -> failed, running -> running, none -> upcoming."""
    if not rows:
        return "upcoming", None
    last = rows[-1]
    if last.status == "succeeded":
        return "done", None
    if last.status == "running":
        return "running", None
    return "failed", _detail(last.error)


def _event_status(rows: Sequence[RunRow], fired: bool) -> tuple[TimelineStatus, str | None]:
    """done once it succeeded (or the scheduler counts it fired), `missed` when a run failed with `missed:`,
    running while a run is running, failed for other failures, else upcoming."""
    if any(r.status == "succeeded" for r in rows):
        return "done", None
    missed = [r for r in rows if r.status == "failed" and (r.error or "").startswith(MISSED_PREFIX)]
    if missed:
        return "missed", _detail(missed[-1].error)
    if rows and rows[-1].status == "running":
        return "running", None
    failed = [r for r in rows if r.status == "failed"]
    if failed:
        return "failed", _detail(failed[-1].error)
    if fired:
        return "done", None
    return "upcoming", None


def build_timeline(
    calendar: SessionCalendar,
    session_date: date,
    now: datetime,
    plan: DayPlan,
    fired: set[str],
    runs: Iterable[RunRow],
) -> list[TimelineItemOut]:
    """The session's day jobs and planned events in time order (an event before a job at the same minute),
    each with its status; the first upcoming item at or after `now` becomes `next`. Empty on a day that is
    not a session. Pure: reads nothing."""
    if not calendar.is_session(session_date):
        return []
    by_job: dict[str, list[RunRow]] = {}
    for r in runs:
        by_job.setdefault(r.job, []).append(r)
    close = calendar.session_close(session_date)
    items: list[tuple[datetime, int, TimelineItemOut]] = []
    for job in DAY_JOBS:
        if job.key == NIGHTLY:
            at = _et(session_date - timedelta(days=1), job.et_time)
        else:
            at = _et(session_date, job.et_time)
        if job.key == CHECKIN_1330 and close <= at:
            continue
        status, detail = _job_status(by_job.get(job.job_name, []))
        items.append(
            (
                at,
                1,
                TimelineItemOut(
                    key=job.key, label=job.label, kind="job", at=at, status=status, detail=detail
                ),
            )
        )
    for ev in plan.events if plan.is_session else ():
        status, detail = _event_status(by_job.get(event_job(ev.key), []), ev.key in fired)
        label = EVENT_LABELS.get(ev.key, ev.key)
        items.append(
            (
                ev.at,
                0,
                TimelineItemOut(
                    key=ev.key, label=label, kind="event", at=ev.at, status=status, detail=detail
                ),
            )
        )
    items.sort(key=lambda t: (t[0], t[1], t[2].key))
    out = [item for _, _, item in items]
    for i, item in enumerate(out):
        if item.status == "upcoming" and item.at >= now:
            out[i] = item.model_copy(update={"status": "next"})
            break
    return out


# --- the synchronous part (a worker thread) -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stored:
    run_id: int
    session: SessionInfoOut
    approval_mode: Any
    timeline: list[TimelineItemOut]
    pending: list[ProposalOut]
    killswitches: list[KillSwitchOut]
    events: list[EventOut]
    token: TokenOut
    worker: WorkerOut
    candidates_top: list[CandidateOut]
    candidates_count: int


def _safe[T](what: str, fn: Callable[[], T], default: T) -> T:
    """A plan or fired-keys lookup that fails leaves the timeline without it, never fails the page."""
    try:
        return fn()
    except Exception as exc:
        log.warning("api.dashboard_part_failed", part=what, error_type=type(exc).__name__)
        return default


def _stored(services: ApiServices, now: datetime) -> _Stored:
    core = services.core
    cal = core.calendar
    settings = _settings(services)
    run_id = live_run_id(services)
    day = current_session(cal, now)
    phase = session_phase(cal, now)
    is_session = phase != "closed_day"
    session = SessionInfoOut(
        date=day,
        phase=phase,
        is_session=is_session,
        open_at=cal.session_open(day) if is_session else None,
        close_at=cal.session_close(day) if is_session else None,
    )
    timeline: list[TimelineItemOut] = []
    if is_session:
        empty = DayPlan(day, False, None, None, ())
        plan = _safe("plan", lambda: services.plan(day), empty)
        fired = _safe("fired", lambda: services.fired(day), set[str]())
        names = [j.job_name for j in DAY_JOBS]
        with core.factory() as s:
            runs = [
                RunRow(job, status, error)
                for job, status, error in s.execute(
                    select(m.JobRun.job, m.JobRun.status, m.JobRun.error)
                    .where(
                        m.JobRun.session_date == day,
                        m.JobRun.job.in_(names) | m.JobRun.job.startswith(EVENT_JOB_PREFIX, autoescape=True),
                    )
                    .order_by(m.JobRun.started_at, m.JobRun.id)
                ).tuples()
            ]
        timeline = build_timeline(cal, day, now, plan, fired, runs)

    with core.factory() as s:
        pending_rows = s.execute(
            select(m.Proposal)
            .where(m.Proposal.run_id == run_id, m.Proposal.status == "pending")
            .order_by(m.Proposal.created_at, m.Proposal.id)
        ).scalars()
        pending = [ProposalOut.from_view(views.proposal_view(s, p), p) for p in pending_rows]
        events = [
            api_views.event_out(e)
            for e in s.execute(
                select(m.EventLog)
                .where(m.EventLog.level.in_(EVENT_LEVELS))
                .order_by(m.EventLog.ts.desc(), m.EventLog.id.desc())
                .limit(EVENTS_SHOWN)
            ).scalars()
        ]
        top = s.execute(
            select(m.Candidate, m.Symbol.ticker)
            .outerjoin(m.Symbol, m.Symbol.id == m.Candidate.symbol_id)
            .where(
                m.Candidate.run_id == run_id, m.Candidate.session_date == day, m.Candidate.rank.is_not(None)
            )
            .order_by(m.Candidate.rank, m.Candidate.id)
            .limit(CANDIDATES_SHOWN)
        ).all()
        count = s.execute(
            select(func.count())
            .select_from(m.Candidate)
            .where(m.Candidate.run_id == run_id, m.Candidate.session_date == day)
        ).scalar_one()
    return _Stored(
        run_id=run_id,
        session=session,
        approval_mode=settings.approval_mode,
        timeline=timeline,
        pending=pending,
        killswitches=api_views.killswitch_states(services.killswitches, core.factory, run_id, day),
        events=events,
        token=api_views.token_out(services.credentials.health, now),
        worker=api_views.worker_out(core.factory, now, settings.worker_heartbeat_stale_seconds),
        candidates_top=[candidate_out(c, ticker) for c, ticker in top],
        candidates_count=int(count),
    )


@router.get("/dashboard", response_model=DashboardOut)
async def get_dashboard(
    services: Services, run: Annotated[Literal["live"], Query(description="Only `live`")] = "live"
) -> DashboardOut:
    core = services.core
    now = core.clock.now()
    st = await anyio.to_thread.run_sync(_stored, services, now)
    live = await open_positions(services, st.run_id)
    pnl = await anyio.to_thread.run_sync(
        views.pnl_view, core.factory, core.calendar, now, st.run_id, live.lines
    )
    return DashboardOut(
        server_time=now,
        run_id=st.run_id,
        session=st.session,
        approval_mode=st.approval_mode,
        telegram_configured=services.telegram_configured,
        timeline=st.timeline,
        pending=st.pending,
        positions=list(live.positions),
        pnl=PnlOut(
            session_date=pnl.session_date,
            realized_today=pnl.realized_today,
            unrealized=pnl.unrealized,
            unrealized_partial=any(ln.unrealized_pnl is None for ln in live.lines),
            week_to_date=pnl.week_to_date,
            equity=pnl.equity,
            peak_equity=pnl.peak_equity,
            drawdown_pct=pnl.drawdown_pct,
        ),
        killswitches=st.killswitches,
        events=st.events,
        token=st.token,
        worker=st.worker,
        candidates_top=st.candidates_top,
        candidates_count=st.candidates_count,
    )
