"""The 09:20 ET pre-open check: token, universe, opening-bar stats, pre-market run, kill switches and the
worker heartbeat, sent directly from the cron process (SPEC §9).

P3-T1 stub: the contracts are final, P3-T10 implements them.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.engine.killswitch import KillSwitches
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import UniverseStatus
from trader.notify.types import Notifier, Renderer
from trader.settings_store import RuntimeSettings


@dataclass(frozen=True)
class PreopenDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    token_check: Callable[[], Awaitable[None]]  # raises when the Questrade token can't be refreshed
    universe_status: Callable[[date], Awaitable[UniverseStatus]]
    killswitches: KillSwitches
    run_id: int
    notifier: Notifier
    render: Renderer


async def run_preopen(deps: PreopenDeps, session_date: date) -> dict[str, Any]:
    """The job detail: every Check as a dict, and `ok`."""
    raise NotImplementedError("P3-T10")
