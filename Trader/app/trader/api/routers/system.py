"""GET /api/system, GET /api/events, POST /api/system/telegram-test (BR-55, SPEC §11, §12 System).

- **System:** the newest `job_runs` row per job name over the last 7 days (newest first), the token chain,
  the worker's heartbeat with the Questrade rate-limit numbers it reports (`detail["rate_limit"]`), the last
  50 `error`/`critical` events, the last 20 notifications not delivered (`failed`: Telegram refused it and the
  relay may still re-send it; `unknown`: it may or may not have arrived) without their text, the Alembic
  revision, the pip tzdata release in use and the manual jobs the page can start.
- **Events:** newest first (`before` pages backwards); with `since`, the ids after it oldest first (catch-up);
  `level` keeps that level and above; every message and data value is masked (`views.event_out`).
- Rows of a replay run are never listed, in the events or the errors (P5-T7: `feed.live_or_unscoped`); a
  replay's events are on its own page (`GET /api/replays/{id}`).
- **Telegram test:** 409 when Telegram is not configured; else one `reply` message through the notifier
  (the `trader telegram-test` text, saying it came from the web app). `Notifier.send` never raises.

Registered under `/api` by `trader.api.routers.ROUTERS`.
"""

import dataclasses
from datetime import datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import anyio
import structlog
from fastapi import APIRouter, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trader.api import views
from trader.api.deps import CsrfUser, CurrentUser, Services, actor
from trader.api.errors import ApiError
from trader.api.feed import live_or_unscoped
from trader.api.launcher import CLI_ARGS
from trader.api.routers.jobs import job_run_out
from trader.api.routers.meta import tz_iana_version
from trader.api.schemas import EventOut, Items, JobRunOut, NotificationOut, SystemOut, TelegramTestOut
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import Level
from trader.logging_setup import redact_text
from trader.notify.messages import fmt_time
from trader.notify.types import OutboundMessage
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("api.system")

router = APIRouter(tags=["system"])

LAST_RUNS_DAYS = 7
MAX_ERRORS = 50
MAX_NOTIFICATIONS = 20
MAX_EVENTS = 500
MAX_ID = 2**63 - 1  # a Postgres bigint: a larger id is a 422, never a database error
ERROR_LEVELS = ("error", "critical")
LEVEL_ORDER: tuple[str, ...] = ("debug", "info", "warning", "error", "critical")
# Not delivered and not in flight (`sending`): the outcomes of a failed send (trader.notify.notifier).
UNDELIVERED = ("failed", "unknown")
CLI_WORDING = "Sent by <code>trader telegram-test</code>"
WEB_WORDING = "Sent from the web app (System page)"


def _settings(services: Services) -> RuntimeSettings:
    try:
        return services.core.settings.load()
    except Exception as exc:
        log.warning("api.system_settings_unusable", error_type=type(exc).__name__)
        return RuntimeSettings()


def _last_runs(s: Session, now: datetime) -> list[JobRunOut]:
    newest = (
        select(m.JobRun)
        .where(m.JobRun.started_at >= now - timedelta(days=LAST_RUNS_DAYS))
        .order_by(m.JobRun.job, m.JobRun.started_at.desc(), m.JobRun.id.desc())
        .distinct(m.JobRun.job)
        .subquery()
    )
    rows = s.scalars(
        select(m.JobRun)
        .join(newest, newest.c.id == m.JobRun.id)
        .order_by(m.JobRun.started_at.desc(), m.JobRun.id.desc())
    )
    return [job_run_out(r) for r in rows]


def _errors(s: Session) -> list[EventOut]:
    rows = s.scalars(
        select(m.EventLog)
        .where(m.EventLog.level.in_(ERROR_LEVELS), live_or_unscoped(m.EventLog.run_id))
        .order_by(m.EventLog.id.desc())
        .limit(MAX_ERRORS)
    )
    return [views.event_out(r) for r in rows]


def _undelivered(s: Session) -> list[NotificationOut]:
    rows = s.execute(
        select(
            m.Notification.id,
            m.Notification.kind,
            m.Notification.status,
            m.Notification.created_at,
            m.Notification.sent_at,
            m.Notification.attempts,
            m.Notification.error,
        )  # never the message text
        .where(m.Notification.status.in_(UNDELIVERED))
        .order_by(m.Notification.id.desc())
        .limit(MAX_NOTIFICATIONS)
    )
    return [
        NotificationOut(
            id=r.id,
            kind=r.kind,
            status=r.status,
            created_at=r.created_at,
            sent_at=r.sent_at,
            attempts=r.attempts,
            error=redact_text(r.error) if r.error is not None else None,
        )
        for r in rows
    ]


def _alembic_revision(s: Session) -> str | None:
    try:
        return s.execute(text("SELECT max(version_num) FROM trader.alembic_version")).scalar_one_or_none()
    except Exception as exc:
        log.warning("api.alembic_revision_failed", error_type=type(exc).__name__)
        s.rollback()
        return None


def _rate_limit(detail: dict[str, Any] | None) -> dict[str, int] | None:
    """`detail["rate_limit"]` when it is a mapping of integer counts (anything else is ignored)."""
    value = detail.get("rate_limit") if detail else None
    if not isinstance(value, dict):
        return None
    counts = {str(k): v for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool)}
    return counts or None


@router.get("/system")
def get_system(_user: CurrentUser, services: Services) -> SystemOut:
    core = services.core
    now = core.clock.now()
    token = views.token_out(services.credentials.health, now)
    worker = views.worker_out(core.factory, now, _settings(services).worker_heartbeat_stale_seconds)
    with core.factory() as s:
        last_runs = _last_runs(s, now)
        errors = _errors(s)
        undelivered = _undelivered(s)
        revision = _alembic_revision(s)
    return SystemOut(
        server_time=now,
        version=core.env.app_version,
        app_env=core.env.app_env,
        alembic_revision=revision,
        tz_iana_version=tz_iana_version(core.env.tz_display),
        telegram_configured=services.telegram_configured,
        token=token,
        worker=worker,
        rate_limit=_rate_limit(worker.detail),
        last_runs=last_runs,
        errors=errors,
        notifications_failed=undelivered,
        manual_jobs=list(CLI_ARGS),
    )


@router.get("/events")
def list_events(
    _user: CurrentUser,
    services: Services,
    since: Annotated[int | None, Query(ge=0, le=MAX_ID)] = None,
    before: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    level: Level | None = None,
    source: Annotated[str | None, Query(max_length=50)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS)] = 100,
) -> Items[EventOut]:
    stmt = select(m.EventLog).where(live_or_unscoped(m.EventLog.run_id))  # a replay's events: its own page
    if since is not None:
        stmt = stmt.where(m.EventLog.id > since).order_by(m.EventLog.id.asc())
    else:
        stmt = stmt.order_by(m.EventLog.id.desc())
    if before is not None:
        stmt = stmt.where(m.EventLog.id < before)
    if level is not None:
        stmt = stmt.where(m.EventLog.level.in_(LEVEL_ORDER[LEVEL_ORDER.index(level) :]))
    if source:
        stmt = stmt.where(m.EventLog.source == source)
    with services.core.factory() as s:
        return Items[EventOut](items=[views.event_out(r) for r in s.scalars(stmt.limit(limit))])


def _audit_test(services: Services, who: str) -> None:
    core = services.core
    with session_scope(core.factory) as s:
        s.add(m.AuditLog(ts=core.clock.now(), actor=who, action="telegram.test", before=None, after=None))


@router.post("/system/telegram-test")
async def telegram_test(user: CsrfUser, services: Services) -> TelegramTestOut:
    if not services.telegram_configured:
        raise ApiError(409, "conflict", "Telegram is not configured")
    from trader.runtime import telegram_test_message  # the composition root; imported late, never at load

    core = services.core
    now_label = fmt_time(core.clock.now(), ZoneInfo(core.env.tz_display))
    msg: OutboundMessage = telegram_test_message(now_label, buttons=False)
    msg = dataclasses.replace(msg, text=msg.text.replace(CLI_WORDING, WEB_WORDING))
    await anyio.to_thread.run_sync(_audit_test, services, actor(user))
    await services.notifier.send(msg)
    return TelegramTestOut(sent=True, message="Test message sent: check your Telegram.")
