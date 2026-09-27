"""Starts a queued replay in its own process (SPEC §8; P5-T7): `trader replay --run <id>` as a child of the
API process, with the API's environment and inherited output, not awaited (reaped by a done-callback task, as
`SubprocessJobLauncher` does). A non-zero exit writes one `warning` event (source `replay.launcher`) with the
replay's run id. Implements `trader.api.deps.ReplayLauncher`."""

import asyncio
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.market.clock import Clock


class SubprocessReplayLauncher:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        *,
        executable: str = "trader",
        spawn: Callable[..., Any] = asyncio.create_subprocess_exec,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._executable = executable
        self._spawn = spawn

    async def launch(self, run_id: int) -> None:
        raise NotImplementedError("P5-T7")

    def running(self) -> bool:
        raise NotImplementedError("P5-T7")
