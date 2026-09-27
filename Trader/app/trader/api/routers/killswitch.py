"""GET /api/killswitch, POST /api/killswitch/{switch}/reset, POST /api/killswitch/pause and /resume
(SPEC §6.3, §11; BR-34, BR-41).

Everything goes through `KillSwitches` (audited there, serialised per run on its advisory lock), for the
live run read on every request and the current session. `reset` is for the automatic switches only and
needs a typed reason (3-500 characters after stripping); a manual pause is lifted with `resume`, which
never touches an automatic switch. Every write answers with the new state, so the page redraws at once.
"""

from typing import NoReturn

from fastapi import APIRouter
from sqlalchemy import select

from trader.api import views
from trader.api.deps import ApiServices, CsrfUser, CurrentUser, Services, actor, live_run_id
from trader.api.errors import ApiError
from trader.api.schemas import KillSwitchesOut, KillSwitchEventOut, ResetIn
from trader.db import models as m
from trader.engine.killswitch import SWITCHES
from trader.market.sessions import current_session

router = APIRouter(tags=["killswitch"])

HISTORY_LIMIT = 50


def _state(services: ApiServices, run_id: int) -> KillSwitchesOut:
    core = services.core
    session_date = current_session(core.calendar, core.clock.now())
    switches = views.killswitch_states(services.killswitches, core.factory, run_id, session_date)
    with core.factory() as s:
        rows = s.execute(
            select(m.KillSwitchEvent)
            .where(m.KillSwitchEvent.run_id == run_id)
            .order_by(m.KillSwitchEvent.id.desc())
            .limit(HISTORY_LIMIT)
        ).scalars()
        history = [
            KillSwitchEventOut(
                id=r.id,
                switch=r.switch,
                session_date=r.session_date,
                tripped_at=r.tripped_at,
                value=r.value,
                threshold=r.threshold,
                reset_at=r.reset_at,
                reset_reason=r.reset_reason,
                reset_by=r.reset_by,
            )
            for r in rows
        ]
    return KillSwitchesOut(switches=switches, history=history)


def _not_tripped(switch: str) -> NoReturn:
    raise ApiError(409, "conflict", f"Kill switch {switch} is not tripped.")


@router.get("/killswitch")
def get_killswitches(services: Services, user: CurrentUser) -> KillSwitchesOut:
    """The four switches (tripped or not, and how each clears) and the last 50 trips, newest first."""
    return _state(services, live_run_id(services))


@router.post("/killswitch/pause")
def pause(services: Services, user: CsrfUser) -> KillSwitchesOut:
    """Block new entries (BR-34); exits and stops keep working."""
    core = services.core
    run_id = live_run_id(services)
    if not services.killswitches.pause(run_id, current_session(core.calendar, core.clock.now()), actor(user)):
        raise ApiError(409, "conflict", "Already paused.")
    return _state(services, run_id)


@router.post("/killswitch/resume")
def resume(services: Services, user: CsrfUser) -> KillSwitchesOut:
    """Lift a manual pause. Never resets an automatic switch (BR-34)."""
    run_id = live_run_id(services)
    if not services.killswitches.resume(run_id, actor(user)):
        raise ApiError(409, "conflict", "Not paused.")
    return _state(services, run_id)


@router.post("/killswitch/{switch}/reset")
def reset(services: Services, user: CsrfUser, switch: str, body: ResetIn) -> KillSwitchesOut:
    """Re-enable a tripped automatic switch with a typed reason (BR-41)."""
    if switch not in SWITCHES:
        raise ApiError(404, "not_found", "Unknown kill switch")
    if switch == "manual_pause":
        raise ApiError(400, "bad_request", "A manual pause is lifted with Resume, not reset: use Resume.")
    core = services.core
    run_id = live_run_id(services)
    active = services.killswitches.active(run_id, current_session(core.calendar, core.clock.now()))
    if not any(a.switch == switch for a in active):  # as the page shows it (daily loss: this session's)
        _not_tripped(switch)
    try:
        services.killswitches.reset(run_id, switch, body.reason, actor(user))
    except ValueError as exc:  # another request reset it first
        if "not tripped" in str(exc):
            _not_tripped(switch)
        raise
    return _state(services, run_id)
