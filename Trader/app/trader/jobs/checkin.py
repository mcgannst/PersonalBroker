"""The 11:30 and 13:30 ET check-ins: a status push, plus a backup firing of every due session event
(SPEC §9).

P3-T1 stub: the contracts are final, P3-T10 implements them.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote
from trader.engine.scheduler import DayPlan, FireResult
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.notify.types import Notifier, Renderer
from trader.settings_store import RuntimeSettings


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


async def run_checkin(deps: CheckinDeps, session_date: date, at_label: str) -> dict[str, Any]:
    raise NotImplementedError("P3-T10")
