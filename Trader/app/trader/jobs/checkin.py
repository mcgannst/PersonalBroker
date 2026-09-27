"""The 11:30 and 13:30 ET check-ins: a status push, plus a backup firing of every due session event
(SPEC §9, "entry-cancel event at 11:30").

The status is the same view as `/status` (the shared builder in `trader.notify.views`, P3-T12). A failure
to build or send it is logged as an `error` event (source `job.checkin`, which the relay alerts) and never
stops the backup firing, which is the check-in's safety half. After the session close (13:30 on an
early-close day) the check-in does nothing.

Every step is isolated. A failing `plan` read is recorded (the status still goes out, with no next event,
and nothing is fired). A failing `fired` read is recorded and the backup passes every due event to the
idempotent `fire`, which skips the settled ones. A `fire` that raises is logged (exception type only),
recorded as `{"key", "status": "failed", "error": <type>}` and as an `error` event, and the loop goes on,
so one bad event never costs the flatten.
"""

import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches
from trader.engine.scheduler import DayPlan, FireResult, due_events
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.notify import views
from trader.notify.types import Notifier, Renderer, StatusView
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("jobs.checkin")

NOT_A_SESSION = {"skipped": "not a session"}
AFTER_CLOSE = {"skipped": "after close"}
SOURCE = "job.checkin"


@dataclass(frozen=True)
class CheckinDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    run_id: int
    notifier: Notifier
    render: Renderer
    plan: Callable[[date], DayPlan]
    fired: Callable[[date], set[str]]
    fire: Callable[[str, date], Awaitable[FireResult]]
    quotes: Callable[[Sequence[int]], Awaitable[Mapping[int, QtQuote]]] | None
    # P3-T12 (matching CommandDeps): the token health and kill switches of the status view. None reads the
    # token chain's state from the database and builds KillSwitches on `factory`.
    token_health: Callable[[], TokenHealth] | None = None
    killswitches: KillSwitches | None = None


# --- the status view --------------------------------------------------------------------------------------
async def _status(
    deps: CheckinDeps, session_date: date, now: datetime, plan: DayPlan, fired: set[str]
) -> StatusView:
    """The shared status view (trader.notify.views, as /status), for the plan and fired keys read above."""
    factory = deps.factory
    return await views.status_view(
        factory=factory,
        clock=deps.clock,
        calendar=deps.calendar,
        settings=deps.settings(),
        killswitches=deps.killswitches or KillSwitches(factory, deps.clock),
        run_id=deps.run_id,
        plan=lambda _d: plan,
        fired=lambda _d: fired,
        token_health=deps.token_health or (lambda: views.db_token_health(factory)),
        quotes=deps.quotes,
        session_date=session_date,
    )


# --- the job ----------------------------------------------------------------------------------------------
def _record_error(
    deps: CheckinDeps, session_date: date, at_label: str, what: str, data: dict[str, Any]
) -> None:
    """An `error` event (source job.checkin, which the relay alerts). A database failure here is only logged:
    the database may be the reason, and the caller's log line remains."""
    try:
        with session_scope(deps.factory) as s:
            log_event(
                s,
                deps.clock,
                "error",
                SOURCE,
                f"check-in {at_label}: {what}",
                {"session_date": session_date.isoformat(), "at": at_label, **data},
                deps.run_id,
            )
    except Exception as db_exc:
        log.error("checkin.event_log_failed", error=type(db_exc).__name__)


async def run_checkin(deps: CheckinDeps, session_date: date, at_label: str) -> dict[str, Any]:
    if not deps.calendar.is_session(session_date):
        return dict(NOT_A_SESSION)
    now = deps.clock.now()
    if now >= deps.calendar.session_close(session_date):
        return dict(AFTER_CLOSE)
    detail: dict[str, Any] = {"session_date": session_date.isoformat(), "at": at_label}
    plan: DayPlan | None = None
    try:
        plan = deps.plan(session_date)
    except Exception as exc:  # no plan: nothing can be backed up, but the status still goes out
        error = type(exc).__name__
        log.error("checkin.plan_failed", error=error)
        detail["plan_error"] = error
        _record_error(deps, session_date, at_label, f"day plan not built ({error})", {"error": error})
    fired: set[str] = set()
    try:
        fired = deps.fired(session_date)
    except Exception as exc:  # settled keys unknown: the idempotent `fire` skips the settled ones itself
        error = type(exc).__name__
        log.error("checkin.fired_failed", error=error)
        detail["fired_error"] = error
        _record_error(deps, session_date, at_label, f"fired events not read ({error})", {"error": error})
    try:
        view_plan = plan if plan is not None else DayPlan(session_date, True, None, None, ())
        view = await _status(deps, session_date, now, view_plan, fired)
        msg = deps.render.checkin(view, at_label)
        dedupe = f"checkin:{session_date.isoformat()}:{at_label}"
        await deps.notifier.send(dataclasses.replace(msg, dedupe_key=dedupe))
        detail["sent"] = True
    except Exception as exc:  # the status is informative; the backup firing below must still happen
        error = type(exc).__name__
        log.error("checkin.status_failed", error=error)
        detail["sent"] = False
        detail["error"] = error
        _record_error(deps, session_date, at_label, f"status not sent ({error})", {"error": error})
    results: list[dict[str, Any]] = []
    if plan is not None:
        for event in due_events(plan, now, fired):
            results.append(await _backup_fire(deps, session_date, at_label, event.key))
    detail["fired"] = results
    return detail


async def _backup_fire(deps: CheckinDeps, session_date: date, at_label: str, key: str) -> dict[str, Any]:
    """One backup firing. An exception becomes a `failed` result and an error event, never an abort, so one
    bad event does not cost the later ones (the flatten)."""
    try:
        result = await deps.fire(key, session_date)
    except Exception as exc:
        error = type(exc).__name__
        log.error("checkin.fire_failed", key=key, error=error)
        _record_error(
            deps, session_date, at_label, f"backup of {key} failed ({error})", {"key": key, "error": error}
        )
        return {"key": key, "status": "failed", "error": error}
    return {"key": result.key, "status": result.status}
