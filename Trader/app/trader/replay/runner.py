"""The replay runner (SPEC §8, §16; P5-T6): create a replay run from a request (validated, settings snapshot
with automatic approvals, strategies pinned), run it session by session through the real engine with a
`ReplayClock`, record progress, honour cancellation, and settle abandoned runs.

One replay at a time: the session-level advisory lock `REPLAY_LOCK` is held by the running process. A replay
never uses `fire_event`, `run_job` or `job_runs`.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from trader.bootstrap import Core
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.replay.clock import ReplayClock
from trader.replay.types import DataMode, ReplayEngine, ReplayMarket, ReplayRequest, ReplayRun
from trader.settings_store import SettingsStore
from trader.strategies.base import CatalystSource
from trader.strategies.registry import StrategyConfigView, StrategyRegistry

REPLAY_LOCK = "trader.replay"


def create_replay(
    factory: sessionmaker[Session],
    wall: Clock,
    calendar: SessionCalendar,
    settings: SettingsStore,
    registry: StrategyRegistry,
    request: ReplayRequest,
    actor: str,
    *,
    config_writer: Callable[..., StrategyConfigView] | None = None,
    app_version: str = "dev",
) -> int:
    """Validate and store a `queued` replay run (with its `replay.start` audit row); returns its id. Raises
    `ReplayInvalid` (every problem, by field path) or `ReplayBusy`."""
    raise NotImplementedError("P5-T6")


@dataclass(frozen=True)
class ReplayDeps:
    factory: sessionmaker[Session]
    wall: Clock
    calendar: SessionCalendar
    market_factory: Callable[[ReplayRun, ReplayClock], ReplayMarket]
    catalysts_factory: Callable[[ReplayRun], CatalystSource]
    engine_factory: Callable[[ReplayRun, ReplayClock, ReplayMarket, CatalystSource], ReplayEngine]


async def run_replay(deps: ReplayDeps, run_id: int) -> ReplayRun:
    """Run a `queued` replay to its end under `REPLAY_LOCK`; returns the final state (`completed`,
    `cancelled` or `failed`). `ReplayBusy` when another replay holds the lock."""
    raise NotImplementedError("P5-T6")


def request_cancel(factory: sessionmaker[Session], wall: Clock, run_id: int, actor: str) -> bool:
    """Ask a `queued` or `running` replay to stop (audit `replay.cancel`); False when it isn't either."""
    raise NotImplementedError("P5-T6")


def reconcile_abandoned(factory: sessionmaker[Session], wall: Clock) -> list[int]:
    """When `REPLAY_LOCK` is free, settle every `running` replay and every `queued` one older than 2 minutes
    as `failed` ("abandoned"); returns their ids."""
    raise NotImplementedError("P5-T6")


@asynccontextmanager
async def open_replay_deps(core: Core, *, data_mode: DataMode) -> AsyncIterator[ReplayDeps]:
    """The real composition: `ReplayData` (a rate-limited `QuestradeClient` in `full` mode, none offline),
    `ReplayCatalysts`, and `build_replay_engine` over a `PinnedRegistry`."""
    raise NotImplementedError("P5-T6")
    yield  # pragma: no cover  (makes this an async generator)
