"""Replay setup (SPEC §8; P5-T6): the frozen settings snapshot, the registry of pinned strategy configs, and
the engine built over them. Nothing here writes a live setting or a live strategy revision.

- `SnapshotSettings` is the run's settings store: `load()` is the snapshot taken at creation (overrides
  applied, `approval_mode = "auto"`), `set()` refuses, so a replay can never change a global setting.
- `PinnedRegistry` serves the strategy config rows pinned at creation (the live rows, or the replay's own
  `replay`-scoped override rows), so `enabled()` / `instance()` never read a live row; `update` and
  `ensure_defaults` refuse; a plug-in that fails to build is reported with the replay's `run_id`.
- `build_replay_engine` is the unchanged Phase 2 engine over them: a `SimBroker` with the candle fill model,
  a `ProposalService` that writes no automatic audit rows, the replay's own kill switches and ledger.
"""

from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.fill_model import FillParams
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches
from trader.engine.orchestrator import Engine
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.replay.candle_fill_model import CandleFillModel
from trader.replay.clock import ReplayClock
from trader.replay.types import ReplayMarket, ReplayRun
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import CatalystSource
from trader.strategies.registry import SOURCE as REGISTRY_SOURCE
from trader.strategies.registry import StrategyConfigView, StrategyRegistry

log = structlog.get_logger("replay.setup")

READ_ONLY_SETTINGS = "replay settings are read-only"
READ_ONLY_REGISTRY = "a replay's strategy configs are pinned: they can't be changed"


class SnapshotSettings(SettingsStore):
    """A `SettingsStore` whose `load()` is the run's frozen snapshot; `set()` raises RuntimeError."""

    def __init__(self, settings: RuntimeSettings) -> None:  # no database: the snapshot is the store
        self._snapshot = settings

    def load(self) -> RuntimeSettings:
        return self._snapshot

    def set(self, key: str, value: Any, actor: str) -> RuntimeSettings:
        raise RuntimeError(READ_ONLY_SETTINGS)


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
        return sorted(self.pinned)

    def current(self, key: str) -> StrategyConfigView:
        if key not in self.pinned:
            raise KeyError(f"strategy {key!r} is not pinned in replay {self.run_id}")
        return self.pinned[key]

    def update(
        self,
        key: str,
        *,
        params: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        actor: str,
    ) -> StrategyConfigView:
        raise RuntimeError(READ_ONLY_REGISTRY)

    def ensure_defaults(self, actor: str = "system") -> None:
        raise RuntimeError(READ_ONLY_REGISTRY)

    def _report(self, key: str, stage: str, exc: BaseException) -> None:
        """As the live registry's, but the event carries the replay's run id (so it is never relayed and
        never shows on the live Dashboard or System page)."""
        error = f"{type(exc).__name__}: {exc}"[:500]
        log.error("strategy.plugin_failed", plugin=key, stage=stage, error=error, run_id=self.run_id)
        try:
            with session_scope(self._factory) as s:
                log_event(
                    s,
                    self._clock,
                    "error",
                    REGISTRY_SOURCE,
                    f"strategy {key} skipped ({stage})",
                    {"strategy": key, "stage": stage, "error": error},
                    run_id=self.run_id,
                )
        except Exception as db_exc:  # the database may be the reason the plug-in failed
            log.error("strategy.plugin_failed_unrecorded", plugin=key, error=str(db_exc)[:300])


def pinned_views(factory: sessionmaker[Session], run: ReplayRun) -> dict[str, StrategyConfigView]:
    """The `StrategyConfigView` of each pinned strategy, from its config row (the pinned `params` and
    `enabled` win, so the run always uses exactly what it pinned at creation)."""
    ids = [p.config_id for p in run.strategies]
    with factory() as s:
        rows = {
            r.id: r for r in s.execute(select(m.StrategyConfig).where(m.StrategyConfig.id.in_(ids))).scalars()
        }
    out: dict[str, StrategyConfigView] = {}
    for p in sorted(run.strategies, key=lambda x: x.key):
        row = rows.get(p.config_id)
        if row is None:
            raise LookupError(f"replay {run.id}: pinned config {p.config_id} of {p.key} no longer exists")
        out[p.key] = StrategyConfigView(
            id=row.id,
            strategy_key=p.key,
            version=p.version,
            revision=p.revision,
            params=dict(p.params),
            enabled=p.enabled,
            created_at=row.created_at,
            scope=p.scope,
        )
    return out


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
    settings = SnapshotSettings(run.settings)
    snapshot = settings.load()
    broker = SimBroker(
        factory,
        clock,
        Ledger(calendar),
        CandleFillModel(FillParams.from_settings(snapshot), run.half_spread_bps),
        run.id,
        currency=snapshot.account_currency,
        calendar=calendar,
        settings=settings.load,
    )
    killswitches = KillSwitches(factory, clock)
    return Engine(
        factory=factory,
        clock=clock,
        calendar=calendar,
        settings=settings,
        registry=registry,
        data=market,
        catalysts=catalysts,
        broker=broker,
        proposals=ProposalService(
            factory,
            clock,
            settings,
            broker,
            run.id,
            entry_blocked=killswitches.entry_guard(),
            audit_auto=False,  # approvals are automatic (the snapshot) and write no audit rows
        ),
        risk=RiskManager(calendar),
        killswitches=killswitches,
        run_id=run.id,
    )
