"""The Control page's engine, strategies, schedule and error-log parts (live dashboard design §4, plan DB-T6).

- `engine_card`: approval mode, trading state (`risk.trading_state`), when the manual pause began, the live
  run, the version and the Alembic revision.
- `strategy_cards`: each plug-in with a current live config: kind, enabled, revision, who changed it, whether
  it owns an open position or working order (`routers.strategies._owns_open_positions`), `max_positions`.
- `schedule`: `dashboard.build_timeline` for `current_session` (today on a session day, else the next
  session), each item enriched from that session's `job_runs`: the latest row's times and duration, the
  number of attempts, a one-line summary (`job_summary`) and the manual job that re-runs it (day jobs only).
- `error_log`: the newest warning-and-above `event_log` rows (live or unscoped, `log.*` included), masked.

All read-only (the kill-switch helpers used are `KillSwitches.active`); none calls Questrade.
"""

import math
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, cast, get_args

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.api import views
from trader.api.deps import ApiServices, _settings
from trader.api.feed import live_or_unscoped
from trader.api.livedata import health, risk
from trader.api.livedata.types import ERRORS_SHOWN
from trader.api.routers.dashboard import DAY_JOBS, RunRow, build_timeline
from trader.api.routers.strategies import _owns_open_positions
from trader.api.routers.system import _alembic_revision
from trader.api.schemas import (
    EngineOut,
    EventOut,
    ManualJob,
    OpeningBarsOut,
    ScheduleItemOut,
    StrategyCardOut,
)
from trader.db import models as m
from trader.engine.scheduler import EVENT_JOB_PREFIX, DayPlan, event_job
from trader.logging_setup import redact_text
from trader.market.clock import et_date
from trader.market.sessions import current_session

log = structlog.get_logger("api.livedata.control")

ERROR_LEVELS = ("warning", "error", "critical")
SETTINGS_LINK = "/settings#strategies"
ORB_OPEN_JOB = event_job("orb_open")
SUMMARY_ERROR_CHARS = 80
SUMMARY_STRING_MAX = 40
SUMMARY_ENTRIES = 3
# The day jobs a manual run can repeat (the check-ins and events have no manual job).
RERUN_JOBS: frozenset[str] = frozenset(j for j in get_args(ManualJob) if j in {d.key for d in DAY_JOBS})


# --- engine -------------------------------------------------------------------------------------------------


def engine_card(services: ApiServices, run_id: int, now: datetime) -> EngineOut:
    core = services.core
    day = current_session(core.calendar, now)
    trading = risk.trading_state(services.killswitches, run_id, day)
    pause = next((a for a in services.killswitches.active(run_id, day) if a.switch == "manual_pause"), None)
    with core.factory() as s:
        started_at = s.execute(select(m.Run.started_at).where(m.Run.id == run_id)).scalar_one()
        revision = _alembic_revision(s)
    return EngineOut(
        approval_mode=_settings(services).approval_mode,
        trading=trading,
        paused_at=pause.tripped_at if pause is not None else None,
        run_id=run_id,
        run_started_at=started_at,
        run_start_date=et_date(started_at),
        version=core.env.app_version,
        app_env=core.env.app_env,
        alembic_revision=revision,
    )


# --- strategies ---------------------------------------------------------------------------------------------


def strategy_cards(services: ApiServices, run_id: int) -> list[StrategyCardOut]:
    registry = services.registry
    cards: list[StrategyCardOut] = []
    for key in registry.keys():
        try:
            cfg = registry.current(key)
        except KeyError:  # no settings row yet: the worker's ensure_defaults() writes revision 1
            continue
        with services.core.factory() as s:
            created_by = s.execute(
                select(m.StrategyConfig.created_by).where(m.StrategyConfig.id == cfg.id)
            ).scalar_one_or_none()
        max_positions = cfg.params.get("max_positions")
        cards.append(
            StrategyCardOut(
                key=key,
                kind=registry.plugin_class(key).kind,
                enabled=cfg.enabled,
                revision=cfg.revision,
                version=cfg.version,
                updated_at=cfg.created_at,
                updated_by=created_by,
                owns_open_positions=_owns_open_positions(services, key, run_id),
                max_positions=max_positions
                if isinstance(max_positions, int) and not isinstance(max_positions, bool)
                else None,
                settings_link=SETTINGS_LINK,
            )
        )
    return cards


# --- schedule -----------------------------------------------------------------------------------------------


def _scalar_text(value: Any) -> str | None:
    """A scalar detail value as the summary shows it, or None when it isn't shown."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else None
    if isinstance(value, str) and len(value) <= SUMMARY_STRING_MAX:
        return redact_text(value)
    return None


def _seconds(value: float) -> str:
    return f"{round(value, 1):g}"


def job_summary(job: str, detail: Any, batch: OpeningBarsOut | None) -> str | None:
    """A job's key detail in a few words (e.g. "543 bars in 31 s"), or None."""
    if job == ORB_OPEN_JOB and batch is not None:
        text = f"{batch.completed} of {batch.symbols} bars in {_seconds(batch.elapsed_s)} s"
        return text + (f", {batch.http_429} x 429" if batch.http_429 else "")
    if not isinstance(detail, Mapping) or not detail:
        return None
    skipped = detail.get("skipped")
    if isinstance(skipped, str):
        return f"skipped: {redact_text(skipped)}"
    error = detail.get("error")
    if isinstance(error, str):
        return redact_text(error)[:SUMMARY_ERROR_CHARS]
    parts: list[str] = []
    for key, value in detail.items():
        shown = _scalar_text(value)
        if shown is not None:
            parts.append(f"{key} {shown}")
        if len(parts) == SUMMARY_ENTRIES:
            break
    return " · ".join(parts) if parts else None


def _safe[T](what: str, fn: Callable[[], T], default: T) -> T:
    """The plan or the fired keys failing leaves the schedule without them (as the dashboard does)."""
    try:
        return fn()
    except Exception as exc:
        log.warning("api.control_schedule_part_failed", part=what, error_type=type(exc).__name__)
        return default


def schedule(
    services: ApiServices, now: datetime, heartbeat_detail: Mapping[str, Any] | None
) -> list[ScheduleItemOut]:
    core = services.core
    cal = core.calendar
    day = current_session(cal, now)
    plan = _safe("plan", lambda: services.plan(day), DayPlan(day, False, None, None, ()))
    fired = _safe("fired", lambda: services.fired(day), set[str]())
    names = [j.job_name for j in DAY_JOBS]
    with core.factory() as s:
        rows = list(
            s.execute(
                select(m.JobRun)
                .where(
                    m.JobRun.session_date == day,
                    m.JobRun.job.in_(names) | m.JobRun.job.startswith(EVENT_JOB_PREFIX, autoescape=True),
                )
                .order_by(m.JobRun.started_at, m.JobRun.id)
            ).scalars()
        )
    by_job: dict[str, list[m.JobRun]] = {}
    for r in rows:
        by_job.setdefault(r.job, []).append(r)
    timeline = build_timeline(cal, day, now, plan, fired, [RunRow(r.job, r.status, r.error) for r in rows])
    batch = health.opening_bars(heartbeat_detail, cal, now)
    if batch is not None and batch.session_date != day:
        batch = None
    job_names = {j.key: j.job_name for j in DAY_JOBS}
    items: list[ScheduleItemOut] = []
    for item in timeline:
        job = job_names[item.key] if item.kind == "job" else event_job(item.key)
        mine = by_job.get(job, [])
        latest = mine[-1] if mine else None
        finished = latest.finished_at if latest is not None else None
        items.append(
            ScheduleItemOut(
                key=item.key,
                label=item.label,
                kind=item.kind,
                at=item.at,
                status=item.status,
                detail=item.detail,
                started_at=latest.started_at if latest is not None else None,
                finished_at=finished,
                duration_seconds=(
                    (finished - latest.started_at).total_seconds()
                    if latest is not None and finished is not None
                    else None
                ),
                attempts=len(mine),
                summary=job_summary(job, latest.detail if latest is not None else None, batch),
                rerun=_rerun(item.key) if item.kind == "job" else None,
            )
        )
    return items


def _rerun(key: str) -> ManualJob | None:
    return cast(ManualJob, key) if key in RERUN_JOBS else None


# --- errors -------------------------------------------------------------------------------------------------


def error_log(factory: sessionmaker[Session], limit: int = ERRORS_SHOWN) -> list[EventOut]:
    """The newest warning-and-above `event_log` rows (live or unscoped), masked."""
    with factory() as s:
        rows = s.execute(
            select(m.EventLog)
            .where(m.EventLog.level.in_(ERROR_LEVELS), live_or_unscoped(m.EventLog.run_id))
            .order_by(m.EventLog.id.desc())
            .limit(limit)
        ).scalars()
        return [views.event_out(r) for r in rows]
