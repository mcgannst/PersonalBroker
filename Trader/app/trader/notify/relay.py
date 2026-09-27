"""The notification relay: new pending proposals, fills, error events and the overlay decision recorded
in the database go to Telegram, exactly once per row, surviving restarts (BR-32; SPEC §4.4, §6.2).

P3-T1 stub: the contracts are final, P3-T8 implements them.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import ProposalMessenger
from trader.market.clock import Clock
from trader.notify.types import MessageKind, Notifier, Renderer
from trader.settings_store import RuntimeSettings

STREAMS = ("proposals", "fills", "events")


@dataclass(frozen=True, slots=True)
class RelayReport:
    proposals: int
    fills: int
    events: int
    closed: int
    skipped: int


def alert_kind(source: str, level: str, message: str) -> MessageKind:
    """Which alert message an event_log row becomes (pure)."""
    raise NotImplementedError("P3-T8")


class NotificationRelay:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        notifier: Notifier,
        render: Renderer,
        messenger: ProposalMessenger,
        run_id: int,
        *,
        settings: Callable[[], RuntimeSettings],
    ) -> None:
        self.factory = factory
        self.clock = clock
        self.notifier = notifier
        self.render = render
        self.messenger = messenger
        self.run_id = run_id
        self.settings = settings

    async def pump(self) -> RelayReport:
        raise NotImplementedError("P3-T8")
