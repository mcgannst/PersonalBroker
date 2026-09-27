"""P5-T6: replay setup (settings snapshot, pinned registry, engine build). Real DB; no network.

Acceptance test 8: `SnapshotSettings.set` raises; `PinnedRegistry.update` and `ensure_defaults` raise; a
plug-in whose params fail to build is reported with the replay's `run_id` (no `run_id`-less event).
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_replay import FakeReplayMarket, seed_replay_world
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.orchestrator import Engine
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.replay.setup import PinnedRegistry, SnapshotSettings, build_replay_engine, pinned_views
from trader.replay.types import PinnedStrategy, ReplayProgress, ReplayRun
from trader.settings_store import RuntimeSettings
from trader.strategies.registry import StrategyRegistry

T0 = datetime(2026, 11, 28, 17, 0, tzinfo=UTC)


def _replay_run_row(factory: sessionmaker[Session], clock: FixedClock) -> int:
    with session_scope(factory) as s:
        run = m.Run(
            mode="replay", started_at=clock.now(), params={"kind": "replay"}, status="queued", label="t"
        )
        s.add(run)
        s.flush()
        return run.id


def _run(run_id: int, pins: tuple[PinnedStrategy, ...], settings: RuntimeSettings | None = None) -> ReplayRun:
    return ReplayRun(
        id=run_id,
        label="t",
        status="queued",
        date_from=datetime(2026, 11, 23).date(),
        date_to=datetime(2026, 11, 24).date(),
        data_mode="offline",
        catalyst_mode="stored",
        half_spread_bps=Decimal("5"),
        settings=settings or RuntimeSettings(approval_mode="auto"),
        strategies=pins,
        overrides={},
        progress=ReplayProgress(),
        created_at=T0,
        finished_at=None,
        error=None,
        cancel_requested=False,
    )


def _pins(factory: sessionmaker[Session], clock: FixedClock) -> tuple[PinnedStrategy, ...]:
    reg = StrategyRegistry(factory, clock)
    out = []
    for key in reg.keys():
        v = reg.current(key)
        out.append(PinnedStrategy(key, v.id, v.revision, v.version, "live", v.enabled, v.params))
    return tuple(out)


def test_snapshot_settings_is_frozen() -> None:
    snap = RuntimeSettings(approval_mode="auto", risk_pct=Decimal("0.01"))
    store = SnapshotSettings(snap)
    assert store.load() is snap
    with pytest.raises(RuntimeError, match="read-only"):
        store.set("approval_mode", "manual", "test")
    assert store.load().approval_mode == "auto"


def test_pinned_registry_serves_pins_and_refuses_changes(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(T0)
    seed_replay_world(db_factory, clock=clock)
    run_id = _replay_run_row(db_factory, clock)
    pins = _pins(db_factory, clock)
    # the replay disables spy_overlay: the pinned view says so, the live row doesn't change
    pins = tuple(replace(p, enabled=False) if p.key == "spy_overlay" else p for p in pins)
    views = pinned_views(db_factory, _run(run_id, pins))
    reg = PinnedRegistry(db_factory, clock, views, run_id)
    assert reg.keys() == ["orb_sip", "spy_overlay"]
    assert reg.current("orb_sip") == views["orb_sip"]
    assert [cfg.strategy_key for _, cfg in reg.enabled()] == ["orb_sip"]
    strategy, cfg = reg.instance("orb_sip")
    assert strategy.key == "orb_sip" and cfg.id == views["orb_sip"].id
    with pytest.raises(KeyError):
        reg.current("nope")
    with pytest.raises(RuntimeError):
        reg.update("orb_sip", params={"top_n": 3}, actor="x")
    with pytest.raises(RuntimeError):
        reg.ensure_defaults()
    assert StrategyRegistry(db_factory, clock).current("spy_overlay").enabled is True
    with db_factory() as s:
        assert s.execute(select(func.count(m.StrategyConfig.id))).scalar_one() == 2
        assert s.execute(select(func.count(m.AuditLog.id))).scalar_one() == 0


def test_broken_plugin_is_reported_with_the_replay_run_id(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(T0)
    seed_replay_world(db_factory, clock=clock)
    run_id = _replay_run_row(db_factory, clock)
    pins = tuple(
        replace(p, params={**p.params, "top_n": 0}) if p.key == "orb_sip" else p
        for p in _pins(db_factory, clock)
    )
    reg = PinnedRegistry(db_factory, clock, pinned_views(db_factory, _run(run_id, pins)), run_id)
    assert [cfg.strategy_key for _, cfg in reg.enabled()] == ["spy_overlay"]
    with db_factory() as s:
        events = s.execute(select(m.EventLog)).scalars().all()
    assert [(e.level, e.run_id) for e in events] == [("error", run_id)]
    assert "orb_sip" in events[0].message
    assert all(e.run_id is not None for e in events)


def test_build_replay_engine_wires_the_snapshot(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(T0)
    seed_replay_world(db_factory, clock=clock)
    run_id = _replay_run_row(db_factory, clock)
    run = _run(
        run_id, _pins(db_factory, clock), RuntimeSettings(approval_mode="auto", risk_pct=Decimal("0.01"))
    )
    reg = PinnedRegistry(db_factory, clock, pinned_views(db_factory, run), run_id)
    market = FakeReplayMarket(clock)
    engine = build_replay_engine(
        db_factory,
        clock,  # type: ignore[arg-type]  # any monotonic Clock will do for the wiring
        SessionCalendar(),
        run,
        market,
        market,  # type: ignore[arg-type]  # not called here
        reg,
    )
    assert isinstance(engine, Engine)
    assert engine.run_id == run_id and engine.broker.run_id == run_id
    assert engine.registry is reg
    assert engine.proposals._audit_auto is False
    assert engine.proposals._entry_blocked is not None
    settings = engine._settings
    assert isinstance(settings, SnapshotSettings)
    assert settings.load().approval_mode == "auto" and settings.load().risk_pct == Decimal("0.01")
    assert engine.broker.read_settings() is settings.load()
