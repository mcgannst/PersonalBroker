"""The change feed behind SSE: while a browser is connected, one watermark query every
`web.sse_poll_seconds`; changed topics become `invalidate` messages and new `event_log` rows `events`
messages (decision "SSE by polling, not LISTEN/NOTIFY").

Stub (P4-T1): T11 implements it.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.api.deps import FeedMessage
from trader.api.schemas import Topic
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings

# The topics the watermark query covers (every Topic).
WATERMARK_TOPICS: tuple[Topic, ...] = (
    "proposals",
    "orders",
    "fills",
    "positions",
    "trades",
    "candidates",
    "killswitch",
    "events",
    "journal",
    "jobs",
    "settings",
    "strategies",
    "system",
)


def watermarks(s: Session) -> dict[Topic, tuple[Any, ...]]:
    """Every topic's watermark, read with one SQL statement."""
    raise NotImplementedError("P4-T11")


class PollingChangeFeed:
    """Implements `trader.api.deps.ChangeFeed`."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        settings: Callable[[], RuntimeSettings],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        queue_size: int = 100,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._settings = settings
        self._sleep = sleep
        self._queue_size = queue_size

    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[FeedMessage]]:
        raise NotImplementedError("P4-T11")

    async def run(self, stop: asyncio.Event) -> None:
        raise NotImplementedError("P4-T11")

    def subscriber_count(self) -> int:
        raise NotImplementedError("P4-T11")
