"""P5-T4: replay-scoped strategy configs. A replay's override row never becomes a live setting."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.strategies.demo_plugin import DemoStrategy
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.strategies.registry import StrategyConfigView, StrategyRegistry

pytestmark = pytest.mark.db
CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))


@pytest.fixture
def registry(db_factory: sessionmaker[Session]) -> StrategyRegistry:
    r = StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoStrategy})
    r.ensure_defaults()
    return r


def _replay(r: StrategyRegistry, base: StrategyConfigView, **params: object) -> StrategyConfigView:
    return r.create_replay_config("demo", base=base, params=params, enabled=True, created_by="replay:7")


def _audits(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.execute(select(func.count()).select_from(m.AuditLog)).scalar_one()


def test_replay_row_is_written_and_never_seen_as_live(
    registry: StrategyRegistry, db_factory: sessionmaker[Session]
) -> None:
    live = registry.update("demo", params={"threshold": 6}, enabled=False, actor="stephen")  # revision 2
    audits = _audits(db_factory)
    rep = _replay(registry, live, threshold=9)
    assert (rep.scope, rep.revision, rep.version, rep.enabled) == ("replay", 2, live.version, True)
    assert rep.params == {"threshold": 9, "at": "open+15m"} and rep.id != live.id
    with db_factory() as s:
        row = s.get(m.StrategyConfig, rep.id)
    assert row is not None and row.created_by == "replay:7" and row.scope == "replay"
    assert _audits(db_factory) == audits  # no audit row: the replay's replay.start row describes it
    # current, enabled, instance only ever see the live row
    assert registry.current("demo") == live
    assert registry.enabled() == []  # the live row is disabled; the enabled replay row doesn't count
    strategy, cfg = registry.instance("demo")
    assert cfg == live and strategy.params.threshold == 6  # type: ignore[attr-defined]
    # update builds on the live row: revision 3, no conflict with the replay row's revision 2
    v3 = registry.update("demo", params={"at": "open+20m"}, actor="stephen")
    assert (v3.scope, v3.revision, v3.params) == ("live", 3, {"threshold": 6, "at": "open+20m"})


def test_replay_row_with_the_same_revision_as_a_later_live_row_is_allowed(registry: StrategyRegistry) -> None:
    base = registry.current("demo")  # revision 1
    first = _replay(registry, base, threshold=2)
    second = _replay(registry, base, threshold=3)  # two replays of one base: same (key, revision)
    assert first.revision == second.revision == 1 and first.id != second.id


def test_invalid_override_params_raise_validation_error(registry: StrategyRegistry) -> None:
    base = registry.current("demo")
    with pytest.raises(ValidationError):
        _replay(registry, base, threshold=99)
    with pytest.raises(ValidationError):
        _replay(registry, base, unknown=1)


def test_a_replay_base_is_refused(registry: StrategyRegistry) -> None:
    rep = _replay(registry, registry.current("demo"), threshold=4)
    with pytest.raises(ValueError, match="not a live"):
        _replay(registry, rep, threshold=5)


def test_a_base_of_another_key_or_unknown_id_is_refused(registry: StrategyRegistry) -> None:
    base = registry.current("demo")
    with pytest.raises(ValueError, match="not a live"):
        _replay(registry, StrategyConfigView(base.id + 1000, "demo", "0.1.0", 1, {}, True, base.created_at))
    other = StrategyConfigView(base.id, "other", base.version, 1, {}, True, base.created_at)
    with pytest.raises(KeyError):  # unknown plug-in key
        registry.create_replay_config("other", base=other, params={}, enabled=True, created_by="replay:7")


def test_ensure_defaults_after_a_replay_row_behaves_as_before(
    registry: StrategyRegistry, db_factory: sessionmaker[Session]
) -> None:
    _replay(registry, registry.current("demo"), threshold=4)
    registry.ensure_defaults()  # same version: nothing new
    with db_factory() as s:
        live = s.execute(
            select(func.count()).select_from(m.StrategyConfig).where(m.StrategyConfig.scope == "live")
        ).scalar_one()
    assert live == 1 and registry.current("demo").revision == 1


def test_config_ids_and_config_key_include_replay_rows(registry: StrategyRegistry) -> None:
    base = registry.current("demo")
    rep = _replay(registry, base, threshold=4)
    assert registry.config_ids("demo") == {base.id, rep.id}
    assert registry.config_key(rep.id) == "demo"


def test_a_version_bump_after_a_replay_row_builds_on_the_live_row(db_factory: sessionmaker[Session]) -> None:
    r1 = StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoStrategy})
    r1.ensure_defaults()
    r1.update("demo", params={"threshold": 6}, actor="stephen")  # live revision 2
    _replay(r1, r1.current("demo"), threshold=9)

    class DemoV2(DemoStrategy):
        version = "0.2.0"

    r2 = StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoV2})
    r2.ensure_defaults()
    cur = r2.current("demo")
    assert (cur.scope, cur.revision, cur.version, cur.params["threshold"]) == ("live", 3, "0.2.0", 6)
