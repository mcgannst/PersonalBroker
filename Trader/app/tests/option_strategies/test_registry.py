"""OPTSIM T7: the option strategy registry. Plug-ins are found by entry point, a broken one is skipped and
logged, and every settings change is a new, audited revision."""

from importlib.metadata import EntryPoint

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.options.factories import T0
from tests.options.toy_plugin import ToyCallBuyer, ToyParams
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.option_strategies import registry as reg
from trader.option_strategies.registry import OptionStrategyRegistry, PluginError

CLOCK = FixedClock(T0)
TOY = "tests.options.toy_plugin:ToyCallBuyer"
HERE = "tests.option_strategies.test_registry"


class NoHooks:
    """Has the attributes but none of the hooks."""

    key = "no_hooks"
    version = "0.1.0"
    params_model = ToyParams
    manual_events: tuple[str, ...] = ()


class ToyV2(ToyCallBuyer):
    version = "0.2.0"


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, *pairs: tuple[str, str]) -> None:
    eps = [EntryPoint(name=n, value=v, group=reg.ENTRY_POINT_GROUP) for n, v in pairs]
    monkeypatch.setattr(reg, "entry_points", lambda group: [e for e in eps if e.group == group])


def test_plugins_found_through_the_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, ("toy_call", TOY))
    assert reg.ENTRY_POINT_GROUP == "trader.option_strategies"
    assert list(reg.available()) == ["toy_call"]
    assert reg.load_all() == {"toy_call": ToyCallBuyer}
    assert OptionStrategyRegistry(None, CLOCK).keys() == ["toy_call"]  # type: ignore[arg-type]


def test_broken_or_mislabeled_plugin_is_skipped_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(
        monkeypatch,
        ("toy_call", TOY),
        ("gone", "tests.options.no_such_module:Nope"),
        ("renamed", TOY),  # the class is keyed toy_call
        ("no_hooks", f"{HERE}:NoHooks"),
    )
    with capture_logs() as logs:
        assert reg.load_all() == {"toy_call": ToyCallBuyer}
    failed = sorted(e["plugin"] for e in logs if e["event"] == "option_strategy.plugin_failed")
    assert failed == ["gone", "no_hooks", "renamed"]
    with pytest.raises(PluginError, match="keyed 'toy_call'"):
        reg.load_plugin("renamed")
    with pytest.raises(PluginError, match="on_event"):
        reg.load_plugin("no_hooks")
    with pytest.raises(PluginError, match="no option strategy plug-in"):
        reg.load_plugin("missing")


def _revisions(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return int(s.execute(select(func.count()).select_from(m.OptionStrategyConfig)).scalar_one())


def _audits(factory: sessionmaker[Session]) -> list[m.AuditLog]:
    with factory() as s:
        return list(s.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars())


@pytest.mark.db
def test_ensure_defaults_revision_1_and_version_bump(db_factory: sessionmaker[Session]) -> None:
    registry = OptionStrategyRegistry(db_factory, CLOCK, plugins={"toy_call": ToyCallBuyer})
    with pytest.raises(KeyError):
        registry.current("toy_call")
    registry.ensure_defaults()
    registry.ensure_defaults()
    cfg = registry.current("toy_call")
    assert (cfg.revision, cfg.version, cfg.enabled, cfg.created_by) == (1, "0.1.0", True, "system")
    assert cfg.params == {"underlying": "F", "min_dte": 30, "at": "open+5m"}
    assert _revisions(db_factory) == 1 and _audits(db_factory) == []

    registry.update("toy_call", params={"min_dte": 45}, enabled=False, actor="web:stephen")
    bumped = OptionStrategyRegistry(db_factory, CLOCK, plugins={"toy_call": ToyV2})
    bumped.ensure_defaults()
    v3 = bumped.current("toy_call")
    assert (v3.revision, v3.version, v3.enabled, v3.params["min_dte"]) == (3, "0.2.0", False, 45)
    assert [a.action for a in _audits(db_factory)] == ["option_strategy.update:toy_call"] * 2
    assert _audits(db_factory)[1].after["version"] == "0.2.0"


@pytest.mark.db
def test_update_validates_audits_and_is_a_no_op_when_equal(db_factory: sessionmaker[Session]) -> None:
    registry = OptionStrategyRegistry(db_factory, CLOCK, plugins={"toy_call": ToyCallBuyer})
    registry.ensure_defaults()
    v2 = registry.update("toy_call", params={"min_dte": 45}, actor="web:stephen")
    assert (v2.revision, v2.params["min_dte"], v2.created_by) == (2, 45, "web:stephen")
    assert registry.update("toy_call", params={"min_dte": 45}, actor="web:stephen").id == v2.id
    for bad in ({"min_dte": 0}, {"underlying": "f"}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            registry.update("toy_call", params=bad, actor="web:stephen")
    assert _revisions(db_factory) == 2
    audit = _audits(db_factory)
    assert [a.action for a in audit] == ["option_strategy.update:toy_call"]
    assert (audit[0].actor, audit[0].before["params"]["min_dte"], audit[0].after["params"]["min_dte"]) == (
        "web:stephen",
        30,
        45,
    )

    strategy, cfg = registry.instance("toy_call")
    assert isinstance(strategy, ToyCallBuyer) and strategy.params.min_dte == 45 and cfg.id == v2.id
    assert [c.id for _, c in registry.enabled()] == [v2.id]
    off = registry.update("toy_call", enabled=False, actor="web:stephen")
    assert off.revision == 3 and registry.enabled() == []
    assert registry.config_ids("toy_call") == {v2.id - 1, v2.id, off.id}
    assert registry.config_key(off.id) == "toy_call" and registry.config_key(999) is None
    assert registry.json_schema("toy_call")["properties"]["min_dte"]["maximum"] == 365
    with pytest.raises(KeyError):
        registry.update("nope", actor="web:stephen")
