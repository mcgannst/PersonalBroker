import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import EntryPoint
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.strategies.demo_plugin import DemoParams, DemoStrategy
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.strategies import registry as reg
from trader.strategies.base import Cancel, EnterLong, Exit, SessionOffset, intent_to_json
from trader.strategies.registry import PluginError, StrategyRegistry

CAL = SessionCalendar()
CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    ("text", "anchor", "seconds"),
    [
        ("open+5m", "open", 300),
        ("close-30m", "close", -1800),
        ("open+5m5s", "open", 305),
        ("open", "open", 0),
        ("close-10m", "close", -600),
        ("open+120m", "open", 7200),
    ],
)
def test_session_offset_parses_and_prints(text: str, anchor: str, seconds: int) -> None:
    off = SessionOffset.parse(text)
    assert (off.anchor, off.seconds) == (anchor, seconds)
    assert str(off) == text


@pytest.mark.parametrize(
    "text", ["noon+5m", "open+5", "open+5m60s", "close*2m", "", "open +5m", "open+5000m"]
)
def test_session_offset_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        SessionOffset.parse(text)


def test_offsets_resolve_on_a_normal_day() -> None:
    day = date(2026, 10, 6)  # EDT: open 13:30Z, close 20:00Z
    assert SessionOffset.parse("open+5m5s").resolve(CAL, day) == datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)
    assert SessionOffset.parse("close-30m").resolve(CAL, day) == datetime(2026, 10, 6, 19, 30, tzinfo=UTC)
    assert SessionOffset.parse("close-10m").resolve(CAL, day) == datetime(2026, 10, 6, 19, 50, tzinfo=UTC)


def test_offsets_follow_an_early_close() -> None:
    day = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close = 18:00Z (EST)
    assert SessionOffset.parse("close-10m").resolve(CAL, day) == datetime(2026, 11, 27, 17, 50, tzinfo=UTC)
    assert SessionOffset.parse("close-30m").resolve(CAL, day) == datetime(2026, 11, 27, 17, 30, tzinfo=UTC)
    assert SessionOffset.parse("open+5m5s").resolve(CAL, day) == datetime(2026, 11, 27, 14, 35, 5, tzinfo=UTC)


def test_offsets_refuse_a_holiday() -> None:
    with pytest.raises(ValueError):
        SessionOffset.parse("open+5m").resolve(CAL, date(2026, 11, 26))  # Thanksgiving


def test_intents_are_frozen_and_serialise() -> None:
    e = EnterLong(7, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {"rvol": "3.2"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.symbol_id = 8  # type: ignore[misc]
    assert intent_to_json(e) == {
        "type": "enter_long",
        "symbol_id": 7,
        "order_type": "stop",
        "stop": "20.01",
        "limit": None,
        "stop_loss": "19.91",
        "reason": "orb_breakout",
    }
    assert intent_to_json(Exit(3, "market", None, "flatten_close"))["type"] == "exit"
    assert intent_to_json(Cancel(9, "entry_cancel_at")) == {
        "type": "cancel",
        "order_id": 9,
        "reason": "entry_cancel_at",
    }


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, *pairs: tuple[str, str]) -> None:
    eps = [EntryPoint(name=n, value=v, group=reg.ENTRY_POINT_GROUP) for n, v in pairs]
    monkeypatch.setattr(reg, "entry_points", lambda group: [e for e in eps if e.group == group])


def test_plugins_are_found_through_the_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, ("demo", "tests.strategies.demo_plugin:DemoStrategy"))
    assert list(reg.available()) == ["demo"]
    assert reg.load_all() == {"demo": DemoStrategy}


def test_a_plugin_keyed_differently_from_its_entry_point_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, ("demo", "tests.strategies.demo_plugin:Mislabeled"))
    with pytest.raises(PluginError, match="not_demo"):
        reg.load_plugin("demo")
    with pytest.raises(PluginError, match="no strategy plug-in"):
        reg.load_plugin("missing")


def test_the_real_plugins_are_declared() -> None:
    assert {"orb_sip", "spy_overlay"} <= set(reg.available())


@pytest.fixture
def registry(db_factory: sessionmaker[Session]) -> StrategyRegistry:
    r = StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoStrategy})
    r.ensure_defaults()
    return r


def _revisions(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return int(s.execute(select(func.count()).select_from(m.StrategyConfig)).scalar_one())


@pytest.mark.db
def test_defaults_are_revision_one_and_created_once(
    db_factory: sessionmaker[Session], registry: StrategyRegistry
) -> None:
    registry.ensure_defaults()
    cfg = registry.current("demo")
    assert (cfg.revision, cfg.version, cfg.enabled) == (1, "0.1.0", True)
    assert cfg.params == {"threshold": 5, "at": "open+15m"}
    assert _revisions(db_factory) == 1


@pytest.mark.db
def test_each_settings_change_is_a_new_audited_revision(
    db_factory: sessionmaker[Session], registry: StrategyRegistry
) -> None:
    v2 = registry.update("demo", params={"threshold": 7}, actor="stephen")
    assert v2.revision == 2 and v2.params == {"threshold": 7, "at": "open+15m"}
    same = registry.update("demo", params={"threshold": 7}, actor="stephen")
    assert same.id == v2.id  # no change, no new revision
    off = registry.update("demo", enabled=False, actor="stephen")
    assert off.revision == 3 and off.enabled is False
    assert registry.enabled() == []
    assert v2.id in registry.config_ids("demo") and off.id in registry.config_ids("demo")
    assert len(registry.config_ids("demo")) == 3
    assert registry.config_key(v2.id) == "demo"
    with db_factory() as s:
        audits = s.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars().all()
    assert [a.action for a in audits] == ["strategy.update:demo", "strategy.update:demo"]
    assert audits[0].before["params"]["threshold"] == 5 and audits[0].after["params"]["threshold"] == 7


@pytest.mark.db
@pytest.mark.parametrize("bad", [{"threshold": 11}, {"threshold": "lots"}, {"unknown": 1}])
def test_invalid_params_are_rejected(
    db_factory: sessionmaker[Session], registry: StrategyRegistry, bad: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        registry.update("demo", params=bad, actor="stephen")
    assert _revisions(db_factory) == 1


@pytest.mark.db
def test_instance_carries_validated_params(registry: StrategyRegistry) -> None:
    registry.update("demo", params={"at": "close-30m"}, actor="stephen")
    strategy, cfg = registry.instance("demo")
    assert isinstance(strategy.params, DemoParams) and strategy.params.at == "close-30m"
    assert strategy.schedule(CAL)[0].at == SessionOffset.parse("close-30m")
    assert cfg.revision == 2 and registry.json_schema("demo")["properties"]["threshold"]["maximum"] == 10


@pytest.mark.db
def test_current_before_defaults_is_a_key_error(db_factory: sessionmaker[Session]) -> None:
    with pytest.raises(KeyError):
        StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoStrategy}).current("demo")
