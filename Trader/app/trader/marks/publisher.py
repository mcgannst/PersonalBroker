"""`MarkPublisher`: the worker task that writes `quote_marks` and `mark_bars` from the quotes the tap saw
(live dashboard plan S1, S10). DB-T1 stub with the final signatures; DB-T2 implements it.

Every database step runs in the publisher's own single-thread executor (never the default one); a pass with
no new observations touches no database; a failing pass logs one masked warning per streak and never raises.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.market.clock import Clock
from trader.marks.types import PUBLISH_INTERVAL_S, EventWriter, LatestQuotes, PublishStep


@dataclass(frozen=True)
class MarkPublisherDeps:
    factory: sessionmaker[Session]
    clock: Clock
    tap: LatestQuotes
    run_id: Callable[[], int | None]
    event: EventWriter | None = None


class MarkPublisher:
    def __init__(
        self,
        deps: MarkPublisherDeps,
        *,
        interval_s: float = PUBLISH_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.deps = deps
        self._interval_s = interval_s
        self._sleep = sleep

    async def run(self, stop: asyncio.Event) -> None:
        """`run_once` then sleep `interval_s` (waking early on `stop`) until `stop` is set; never raises."""
        raise NotImplementedError("DB-T2")

    async def run_once(self) -> PublishStep:
        raise NotImplementedError("DB-T2")

    def health_detail(self) -> dict[str, Any]:
        """The heartbeat's `marks` key (S10)."""
        raise NotImplementedError("DB-T2")

    def close(self) -> None:
        """Shuts the executor down without waiting; idempotent."""
        raise NotImplementedError("DB-T2")
