"""GET /api/live -> LiveOut: the read-only live monitor (live dashboard design §5.1; plan S14, S15).

`range` selects the equity series (`today` or `run`); `expand` lists up to three open position ids whose
1-minute bars are included. The route never calls Questrade (D8): prices come from the worker's marks through
`trader.api.livedata` only. It does all its database work in one worker-thread hop, computing each part
inside its own guard: a part that raises is null in the response plus a `PartErrorOut` (the rest of the page
still renders), and only the live run or the session info failing is a real error (500). The response
carries `Server-Timing: app;dur=<ms>, <part>;dur=<ms>...` so a slow part is visible live.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated

import anyio.to_thread
import structlog
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select

from trader.api import views as api_views
from trader.api.deps import ApiServices, Services, _settings, current_user
from trader.api.livedata import activity, books, equity, periods, positions, risk
from trader.api.livedata.types import (
    HEARTBEAT_BADGE_SECONDS,
    MARK_STALE_SECONDS,
    PART_MESSAGE_CHARS,
    LivePositions,
)
from trader.api.routers.dashboard import DAY_JOBS, RunRow, build_timeline
from trader.api.schemas import (
    LiveOut,
    LiveRange,
    PartErrorOut,
    PeriodPnlOut,
    ProposalOut,
    SessionInfoOut,
    TimelineItemOut,
    TradingState,
    WorkerOut,
)
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.engine.scheduler import EVENT_JOB_PREFIX, DayPlan
from trader.logging_setup import redact_text
from trader.market.sessions import current_session, session_phase
from trader.notify.views import proposal_view

log = structlog.get_logger("api.live")

# Every route needs a signed-in session (SPEC §14).
router = APIRouter(tags=["live"], dependencies=[Depends(current_user)])

EXPAND_PATTERN = r"^[1-9][0-9]{0,18}(,[1-9][0-9]{0,18}){0,2}$"
FALLBACK_TRADING: TradingState = "blocked"  # when the state can't be read, never claim "running"


def expand_ids(expand: str | None) -> frozenset[int]:
    """The `expand` ids (the query pattern already allows at most three positive integers)."""
    return frozenset(int(x) for x in expand.split(",")) if expand else frozenset()


def part_message(exc: BaseException) -> str:
    """`<ExceptionType>: <masked text, 120 chars>` (plan S15)."""
    return f"{type(exc).__name__}: {redact_text(str(exc))[:PART_MESSAGE_CHARS]}"


@dataclass(slots=True)
class _Parts:
    """Runs each part in its own guard, recording its duration and, when it raised, a `PartErrorOut`."""

    errors: list[PartErrorOut] = field(default_factory=list)
    timings: list[tuple[str, float]] = field(default_factory=list)

    def run[T](self, part: str, fn: Callable[[], T]) -> T | None:
        start = time.perf_counter()
        try:
            return fn()
        except Exception as exc:
            log.warning("api.live_part_failed", part=part, error_type=type(exc).__name__)
            self.errors.append(PartErrorOut(part=part, message=part_message(exc)))
            return None
        finally:
            self.timings.append((part, (time.perf_counter() - start) * 1000))


def _session_info(services: ApiServices, now: datetime) -> SessionInfoOut:
    """As `/api/dashboard` builds it: the current session (today's, else the next) and its phase."""
    cal = services.core.calendar
    day = current_session(cal, now)
    phase = session_phase(cal, now)
    is_session = phase != "closed_day"
    return SessionInfoOut(
        date=day,
        phase=phase,
        is_session=is_session,
        open_at=cal.session_open(day) if is_session else None,
        close_at=cal.session_close(day) if is_session else None,
    )


def _or_empty[T](what: str, fn: Callable[[], T], default: T) -> T:
    """A plan or fired-keys lookup that fails leaves the timeline without it (as `/api/dashboard` does)."""
    try:
        return fn()
    except Exception as exc:
        log.warning("api.live_timeline_input_failed", part=what, error_type=type(exc).__name__)
        return default


def _timeline(services: ApiServices, session: SessionInfoOut, now: datetime) -> list[TimelineItemOut]:
    """Today's timeline exactly as `/api/dashboard` builds it (session days only)."""
    if not session.is_session:
        return []
    core, day = services.core, session.date
    plan = _or_empty("plan", lambda: services.plan(day), DayPlan(day, False, None, None, ()))
    fired = _or_empty("fired", lambda: services.fired(day), set[str]())
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
    return build_timeline(core.calendar, day, now, plan, fired, runs)


def _pending(services: ApiServices, run_id: int) -> list[ProposalOut]:
    with services.core.factory() as s:
        rows = s.execute(
            select(m.Proposal)
            .where(m.Proposal.run_id == run_id, m.Proposal.status == "pending")
            .order_by(m.Proposal.created_at, m.Proposal.id)
        ).scalars()
        return [ProposalOut.from_view(proposal_view(s, p), p) for p in rows]


def _closed_today(services: ApiServices, run_id: int, day: date) -> int:
    with services.core.factory() as s:
        return int(
            s.execute(
                select(func.count())
                .select_from(m.Trade)
                .where(m.Trade.run_id == run_id, m.Trade.session_date == day)
            ).scalar_one()
        )


def _no_positions() -> LivePositions:
    raise RuntimeError("the positions part failed")


def build_live(
    services: ApiServices, now: datetime, range_: LiveRange, expand: frozenset[int]
) -> tuple[LiveOut, list[tuple[str, float]]]:
    """The whole response, synchronously (a worker thread), with each part's duration in milliseconds."""
    core = services.core
    cal, factory = core.calendar, core.factory
    settings = _settings(services)
    run = get_live_run(factory, core.clock, settings)  # nothing else can be computed without it: 500
    session = _session_info(services, now)  # likewise
    run_id = run.id
    parts = _Parts()

    day = parts.run("session_day", lambda: periods.session_day(cal, now))
    session_day = day if day is not None else session.date
    worker = parts.run(
        "worker", lambda: api_views.worker_out(factory, now, settings.worker_heartbeat_stale_seconds)
    )
    worker = worker if worker is not None else WorkerOut(ok=False)
    trading = parts.run("trading", lambda: risk.trading_state(services.killswitches, run_id, session.date))

    live = parts.run("positions", lambda: positions.live_positions(factory, run_id, now, expand))
    open_values = live.open_values if live is not None else []

    def period_blocks() -> list[PeriodPnlOut]:
        windows = periods.period_windows(cal, now, run.started_at)
        blocks = periods.period_blocks(factory, run_id, windows, open_values)
        if live is None:  # without the positions no open P&L is known
            blocks = [b.model_copy(update={"unrealized_partial": True}) for b in blocks]
        return blocks

    period_list = parts.run("periods", period_blocks)
    claude = parts.run("claude_today", lambda: periods.claude_today(factory, now, settings))
    books_out = parts.run("books", lambda: books.books_check(factory, run_id))
    equity_now = live.equity_at_marks if live is not None else None
    equity_out = parts.run(
        "equity", lambda: equity.equity_series(factory, cal, run_id, now, range_, equity_now)
    )
    risk_out = parts.run(
        "risk",
        lambda: risk.risk_panel(
            services.killswitches,
            services.registry,
            factory,
            cal,
            settings,
            run_id,
            now,
            live if live is not None else _no_positions(),
        ),
    )
    feed = parts.run("activity", lambda: activity.activity_feed(factory, run_id, session_day))
    rejected = parts.run("rejections", lambda: activity.rejections(factory, run_id, session_day))
    timeline = parts.run("timeline", lambda: _timeline(services, session, now))
    pending = parts.run("pending", lambda: _pending(services, run_id))
    closed = parts.run("closed_today", lambda: _closed_today(services, run_id, session_day))

    out = LiveOut(
        server_time=now,
        run_id=run_id,
        run_started_at=run.started_at,
        session=session,
        session_day=session_day,
        approval_mode=settings.approval_mode,
        trading=trading if trading is not None else FALLBACK_TRADING,
        telegram_configured=services.telegram_configured,
        worker=worker,
        worker_stale=worker.age_seconds is None or worker.age_seconds > HEARTBEAT_BADGE_SECONDS,
        marks_stale_seconds=MARK_STALE_SECONDS,
        closed_today=closed if closed is not None else 0,
        periods=period_list,
        claude_today=claude,
        books=books_out,
        equity=equity_out,
        risk=risk_out,
        positions=live.positions if live is not None else None,
        activity=feed,
        rejections=rejected,
        timeline=timeline,
        pending=pending,
        part_errors=parts.errors,
    )
    return out, parts.timings


def server_timing(app_ms: float, timings: list[tuple[str, float]]) -> str:
    return ", ".join([f"app;dur={app_ms:.1f}", *(f"{part};dur={ms:.1f}" for part, ms in timings)])


@router.get("/live", response_model=LiveOut)
async def get_live(
    services: Services,
    response: Response,
    range_: Annotated[LiveRange, Query(alias="range")] = "today",
    expand: Annotated[str | None, Query(max_length=64, pattern=EXPAND_PATTERN)] = None,
) -> LiveOut:
    now = services.core.clock.now()
    start = time.perf_counter()
    out, timings = await anyio.to_thread.run_sync(build_live, services, now, range_, expand_ids(expand))
    response.headers["Server-Timing"] = server_timing((time.perf_counter() - start) * 1000, timings)
    return out
