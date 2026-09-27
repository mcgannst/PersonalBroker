"""Replay setup (SPEC §8; P5-T6): the frozen settings snapshot, the registry of pinned strategy configs, and
the engine built over them. Nothing here writes a live setting or a live strategy revision."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.engine.orchestrator import Engine
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.replay.clock import ReplayClock
from trader.replay.types import ReplayMarket, ReplayRun
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import CatalystSource
from trader.strategies.registry import StrategyConfigView, StrategyRegistry


class SnapshotSettings(SettingsStore):
    """A `SettingsStore` whose `load()` is the run's frozen snapshot; `set()` raises RuntimeError."""

    def __init__(self, settings: RuntimeSettings) -> None:  # no database: the snapshot is the store
        self._snapshot = settings

    def load(self) -> RuntimeSettings:
        raise NotImplementedError("P5-T6")

    def set(self, key: str, value: Any, actor: str) -> RuntimeSettings:
        raise NotImplementedError("P5-T6")


class PinnedRegistry(StrategyRegistry):
    """A `StrategyRegistry` over the replay's pinned configs: `keys()` are the pinned keys and `current(key)`
    the pinned view, so `enabled()` / `instance()` never read a live row; `update` / `ensure_defaults` raise
    RuntimeError; a plug-in failure is reported with the replay's `run_id`."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        pinned: Mapping[str, StrategyConfigView],
        run_id: int,
        plugins: Mapping[str, type[Any]] | None = None,
    ) -> None:
        super().__init__(factory, clock, plugins)
        self.pinned = dict(pinned)
        self.run_id = run_id

    def keys(self) -> list[str]:
        raise NotImplementedError("P5-T6")

    def current(self, key: str) -> StrategyConfigView:
        raise NotImplementedError("P5-T6")

    def update(
        self,
        key: str,
        *,
        params: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        actor: str,
    ) -> StrategyConfigView:
        raise NotImplementedError("P5-T6")

    def ensure_defaults(self, actor: str = "system") -> None:
        raise NotImplementedError("P5-T6")


def build_replay_engine(
    factory: sessionmaker[Session],
    clock: ReplayClock,
    calendar: SessionCalendar,
    run: ReplayRun,
    market: ReplayMarket,
    catalysts: CatalystSource,
    registry: PinnedRegistry,
) -> Engine:
    """The real Phase 2 engine for one replay run: `SimBroker` with a `CandleFillModel`, `ProposalService(
    audit_auto=False)` over `SnapshotSettings`, the kill switches and ledger of the replay's run id."""
    raise NotImplementedError("P5-T6")
