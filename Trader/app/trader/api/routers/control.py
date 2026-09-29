"""GET /api/control -> ControlOut: the Control page's read model (live dashboard design §4, §5.3; plan S15).

Read-only: every action keeps using the existing mutation routes (approval mode, kill switches, strategies,
jobs, Telegram test, watchlist). Never calls Questrade (D8): Questrade numbers come from the worker's
heartbeat. The live run is looked up without writing (`readonly.live_run`: it is created only when none
exists yet, as every process does). The database work runs in one worker thread; each part (`engine`,
`killswitches`, `strategies`, `schedule`, `health`, `soak`, `errors`) is computed on its own: one that raises
becomes null plus a `PartErrorOut` (the exception type and a masked, 120-character text), and the page still
answers 200. The live run or the session info failing is a real error (500), since nothing else can be
computed. A failed part logs at `warning` (fix round 1): the API's log mirror copies every `error` line into
`event_log`, which would write a row per page load.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import anyio.to_thread
import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import select

from trader.api.deps import ApiServices, Services, _settings, current_user
from trader.api.launcher import CLI_ARGS
from trader.api.livedata import control, health, positions, readonly, risk
from trader.api.livedata.types import PART_MESSAGE_CHARS
from trader.api.schemas import (
    ControlOut,
    KillSwitchEventOut,
    KillSwitchLightOut,
    PartErrorOut,
    SessionInfoOut,
)
from trader.db import models as m
from trader.logging_setup import redact_text
from trader.market.sessions import current_session, session_phase

log = structlog.get_logger("api.control")

# Every route needs a signed-in session (SPEC §14).
router = APIRouter(tags=["control"], dependencies=[Depends(current_user)])

KILLSWITCH_HISTORY = 20


def _live_run(services: ApiServices) -> int:
    """The active live run's id, read without writing; created (as every process does) only when none
    exists yet."""
    return readonly.live_run(services).id


def _session(services: ApiServices, now: datetime) -> SessionInfoOut:
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


def _history(services: ApiServices, run_id: int) -> list[KillSwitchEventOut]:
    """The live run's last KILLSWITCH_HISTORY trips, newest first (the fields /api/killswitch returns)."""
    with services.core.factory() as s:
        rows = s.execute(
            select(m.KillSwitchEvent)
            .where(m.KillSwitchEvent.run_id == run_id)
            .order_by(m.KillSwitchEvent.id.desc())
            .limit(KILLSWITCH_HISTORY)
        ).scalars()
        return [
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


def _killswitches(
    services: ApiServices, run_id: int, now: datetime
) -> tuple[list[KillSwitchLightOut], list[KillSwitchEventOut]]:
    core = services.core
    equity = positions.live_positions(core.factory, run_id, now, ()).equity_at_marks
    lights = risk.killswitch_lights(
        services.killswitches, core.factory, core.calendar, _settings(services), run_id, now, equity
    )
    return lights, _history(services, run_id)


def _part_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {redact_text(str(exc))[:PART_MESSAGE_CHARS]}"


def _build(services: ApiServices, now: datetime) -> ControlOut:
    run_id = _live_run(services)
    session = _session(services, now)
    errors: list[PartErrorOut] = []

    def part[T](name: str, fn: Callable[[], T]) -> T | None:
        try:
            return fn()
        except Exception as exc:
            log.warning("api.control_part_failed", part=name, error_type=type(exc).__name__)
            errors.append(PartErrorOut(part=name, message=_part_message(exc)))
            return None

    engine = part("engine", lambda: control.engine_card(services, run_id, now))
    switches = part("killswitches", lambda: _killswitches(services, run_id, now))
    strategies = part("strategies", lambda: control.strategy_cards(services, run_id))
    health_out = part("health", lambda: health.health_panel(services, now))
    detail: dict[str, Any] | None = health_out.worker.detail if health_out is not None else None
    schedule = part("schedule", lambda: control.schedule(services, now, detail))
    soak = part("soak", lambda: health.soak_summary(services, now))
    error_log = part("errors", lambda: control.error_log(services.core.factory))
    order = ("engine", "killswitches", "strategies", "schedule", "health", "soak", "errors")
    errors.sort(key=lambda e: order.index(e.part))
    return ControlOut(
        server_time=now,
        session=session,
        manual_jobs=list(CLI_ARGS),
        engine=engine,
        killswitches=switches[0] if switches is not None else None,
        killswitch_history=switches[1] if switches is not None else None,
        strategies=strategies,
        schedule=schedule,
        health=health_out,
        soak=soak,
        errors=error_log,
        part_errors=errors,
    )


@router.get("/control", response_model=ControlOut)
async def get_control(services: Services) -> ControlOut:
    now = services.core.clock.now()
    return await anyio.to_thread.run_sync(_build, services, now)
