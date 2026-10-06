"""OPTSIM T7: a plug-in's key-value state, scoped to its strategy key."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.options.factories import T0
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.option_strategies.state import DbStrategyState

pytestmark = pytest.mark.db


def test_state_store_get_put_delete_and_isolation_by_key(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(T0)
    wheel = DbStrategyState(db_factory, clock, "wheel")
    toy = DbStrategyState(db_factory, clock, "toy_call")
    assert wheel.get("account") is None

    wheel.put("account", {"paused": False, "tickers": ["F"]})
    toy.put("account", {"note": "mine"})
    wheel.put("account", {"paused": True})  # replaces, never merges
    assert wheel.get("account") == {"paused": True}
    assert toy.get("account") == {"note": "mine"}
    with db_factory() as s:
        rows = s.execute(select(m.OptionStrategyState).order_by(m.OptionStrategyState.strategy_key)).scalars()
        assert [(r.strategy_key, r.scope_key, r.version, r.updated_at) for r in rows] == [
            ("toy_call", "account", 1, T0),
            ("wheel", "account", 2, T0),
        ]

    wheel.delete("account")
    wheel.delete("never_there")
    assert wheel.get("account") is None
    assert toy.get("account") == {"note": "mine"}
