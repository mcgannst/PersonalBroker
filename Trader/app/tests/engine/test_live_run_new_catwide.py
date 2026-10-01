"""CATWIDE: the new orb_sip parameters go through `trader live-run new --strategy-param` (values JSON-parsed,
validated against OrbSipParams over the live settings) and are stored on the new revision."""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
from tests.engine.test_live_run_new import NIGHT, _old_run, _runs
from tests.fakes_api import test_core
from trader.bootstrap import Core
from trader.cli import app
from trader.market.clock import FixedClock
from trader.strategies.orb_sip import OrbSipParams
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
runner = CliRunner()

CATWIDE_FLAGS = [
    "--strategy-param",
    "orb_sip.require_catalyst=false",
    "--strategy-param",
    "orb_sip.reject_bearish_catalyst=true",
    "--strategy-param",
    "orb_sip.extend_past_top_n=true",
    "--strategy-param",
    "orb_sip.max_rank=100",
    "--strategy-param",
    "orb_sip.doji_body_pct_max=0",
]


@pytest.fixture
def core(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[Core]:
    c = test_core(db_factory, FixedClock(NIGHT))
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: c)
    yield c


def test_cli_enables_all_three_changes(core: Core) -> None:
    old = _old_run(core.factory)
    registry = StrategyRegistry(core.factory, core.clock)
    registry.ensure_defaults()
    result = runner.invoke(app, ["live-run", "new", "--confirm", *CATWIDE_FLAGS])
    assert result.exit_code == 0, result.output
    assert _runs(core.factory)[0] == (old, "completed")
    cfg = registry.current("orb_sip")
    assert cfg.version == "1.0.0" and cfg.revision == 2
    p = OrbSipParams.model_validate(cfg.params)
    assert (p.require_catalyst, p.reject_bearish_catalyst, p.extend_past_top_n) == (False, True, True)
    assert (p.max_rank, p.doji_body_pct_max, p.top_n) == (100, Decimal("0"), 20)
    strategy, _ = registry.instance("orb_sip")
    assert strategy.params == p


@pytest.mark.parametrize(
    "bad",
    [
        ["--strategy-param", "orb_sip.max_rank=5", "--strategy-param", "orb_sip.extend_past_top_n=true"],
        ["--strategy-param", "orb_sip.max_rank=0"],
        ["--strategy-param", "orb_sip.reject_bearish_catalyst=maybe"],
    ],
)
def test_cli_rejects_bad_catwide_values(core: Core, bad: list[str]) -> None:
    old = _old_run(core.factory)
    registry = StrategyRegistry(core.factory, core.clock)
    registry.ensure_defaults()
    result = runner.invoke(app, ["live-run", "new", "--confirm", *bad])
    assert result.exit_code == 1
    assert _runs(core.factory) == [(old, "active")]
    assert registry.current("orb_sip").revision == 1
