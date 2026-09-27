"""The long-running worker (`python -m trader.worker [--once]`): fires session events on time, polls quotes
for working orders, expires proposals, relays notifications, runs the Telegram bot, ends the session,
writes a heartbeat, and refuses to run twice (SPEC §1, §6, §7.2, §9; BR-31, BR-42).

P3-T1 stub: the contracts are final, P3-T9 implements them.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import sqlalchemy
from sqlalchemy import Connection
from sqlalchemy.orm import Session, sessionmaker

from trader.engine.scheduler import DayPlan, FireResult
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.sessions import SessionPhase
from trader.settings_store import RuntimeSettings


class WorkerEngine(Protocol):
    """What the worker drives. P2's Engine satisfies it (it returns EventResult, list[FillEvent] and
    list[PositionView]; Sequence[Any] keeps this protocol free of those types)."""

    async def run_event(self, event_key: str, session_date: date) -> Any: ...

    async def poll_quotes(self) -> Sequence[Any]: ...

    async def tick(self, now: datetime) -> None: ...

    async def end_of_session(self, session_date: date) -> Sequence[Any]: ...


@dataclass(frozen=True)
class WorkerDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    engine_for: Callable[[date], Awaitable[WorkerEngine]]  # one engine per session
    plan: Callable[[date], DayPlan]
    fire: Callable[[str, date], Awaitable[FireResult]]
    fired: Callable[[date], set[str]]
    relay: Callable[[], Awaitable[Any]] | None
    bot: Callable[[asyncio.Event], Awaitable[None]] | None
    # (session_date, body) -> outcome; T12 passes run_job_async with job `session_end`.
    end_session: Callable[[date, Callable[[], Awaitable[dict[str, Any]]]], Awaitable[Any]]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    process: str = "worker"
    host: str = ""


@dataclass(frozen=True, slots=True)
class StepReport:
    now: datetime
    phase: SessionPhase
    fired: list[FireResult]
    fills: int
    relayed: bool
    ended: bool


def acquire_single_instance(engine: sqlalchemy.Engine) -> Connection | None:
    """Take the worker's session advisory lock on a dedicated connection held for the process lifetime;
    None when another worker holds it."""
    raise NotImplementedError("P3-T9")


class Worker:
    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps

    async def step(self) -> StepReport:
        raise NotImplementedError("P3-T9")

    async def run(self, stop: asyncio.Event) -> None:
        raise NotImplementedError("P3-T9")


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m trader.worker [--once]`: calls trader.runtime.run_worker(once=...) and returns its code."""
    raise NotImplementedError("P3-T9")


if __name__ == "__main__":
    raise SystemExit(main())
