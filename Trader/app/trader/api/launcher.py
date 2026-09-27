"""Manual job runs from the System page: `trader <args> [--date YYYY-MM-DD] [--force]` as a child process with
the API's environment, not awaited (a done-callback reaps it); a non-zero exit writes one `warning` event.

Stub (P4-T1): T9 implements `SubprocessJobLauncher`; `CLI_ARGS` is the plan's table.
"""

import asyncio
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.api.schemas import JobLaunchOut, ManualJob
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock

CLI_ARGS: Mapping[ManualJob, tuple[str, ...]] = {
    "nightly": ("nightly",),
    "premarket": ("premarket",),
    "preopen": ("preopen",),
    "postclose": ("postclose",),
    "token-refresh": ("token-refresh",),  # takes no --date or --force on trunk
}


class SubprocessJobLauncher:
    """Implements `trader.api.deps.JobLauncher`."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        *,
        executable: str = "trader",
        spawn: Callable[..., Any] = asyncio.create_subprocess_exec,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._calendar = calendar
        self._executable = executable
        self._spawn = spawn

    async def launch(
        self, job: ManualJob, session_date: date | None, force: bool, actor: str
    ) -> JobLaunchOut:
        raise NotImplementedError("P4-T9")

    def running(self, job: str) -> bool:
        raise NotImplementedError("P4-T9")
