"""Remote Telegram commands (SPEC §4.4): /status, /positions, /pnl, /pending, /pause (confirmed), /resume,
/help. BR-34, BR-30, BR-33.

P3-T1 stub: the contracts are final, P3-T7 implements them. The view builders are async (a refinement of
the plan's signatures) because the last prices come from the async `quotes` callable.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.adapters.telegram.types import CallbackIssuer, ProposalMessenger
from trader.engine.killswitch import KillSwitches
from trader.engine.scheduler import DayPlan
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.notify.types import OutboundMessage, PnlView, PositionLine, Renderer, StatusView
from trader.settings_store import RuntimeSettings


@dataclass(frozen=True)
class CommandDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    killswitches: KillSwitches
    run_id: int
    chat_id: int
    plan: Callable[[date], DayPlan]
    fired: Callable[[date], set[str]]
    token_health: Callable[[], TokenHealth]
    quotes: Callable[[Sequence[int]], Awaitable[Mapping[int, QtQuote]]] | None
    messenger: ProposalMessenger
    issuer: CallbackIssuer
    render: Renderer


async def status_view(deps: CommandDeps) -> StatusView:
    raise NotImplementedError("P3-T7")


async def position_lines(deps: CommandDeps) -> tuple[PositionLine, ...]:
    raise NotImplementedError("P3-T7")


async def pnl_view(deps: CommandDeps) -> PnlView:
    raise NotImplementedError("P3-T7")


class Commands:
    """Implements trader.adapters.telegram.types.CommandHandler."""

    def __init__(self, deps: CommandDeps) -> None:
        self.deps = deps

    async def handle(self, text: str) -> list[OutboundMessage]:
        raise NotImplementedError("P3-T7")

    async def confirm_pause(self, action: str) -> str:
        raise NotImplementedError("P3-T7")
