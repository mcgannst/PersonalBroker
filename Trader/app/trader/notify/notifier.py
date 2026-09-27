"""The Notifier: sends OutboundMessages through the Telegram API, deduplicated through `notifications`,
never raising, logging Telegram's error text and never the token (SPEC §4.4, §14).

P3-T1 stub: the contracts are final, P3-T5 implements them.
"""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import TelegramApi
from trader.market.clock import Clock
from trader.notify.types import OutboundMessage


def split_text(text: str, limit: int = 4096) -> list[str]:
    raise NotImplementedError("P3-T5")


class TelegramNotifier:
    """Notifier over a TelegramApi, sending to one chat."""

    def __init__(
        self,
        api: TelegramApi,
        chat_id: int,
        factory: sessionmaker[Session],
        clock: Clock,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api = api
        self.chat_id = chat_id
        self.factory = factory
        self.clock = clock
        self.sleep = sleep

    async def send(self, msg: OutboundMessage) -> None:
        raise NotImplementedError("P3-T5")


class NullNotifier:
    """Notifier used when Telegram is not configured: logs once per process, records nothing."""

    async def send(self, msg: OutboundMessage) -> None:
        raise NotImplementedError("P3-T5")
