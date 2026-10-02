"""FILLFIX (Fri 2026-10-02, run 274): stop orders trigger on the last trade, as Questrade's do, and a
triggered entry is held while its spread is wider than half its stop distance.

Friday's opening fills came from the ask/bid triggers on wide spreads: BXDC's one quote (bid 17.41, ask 17.83,
last 17.72) filled the 17.73 entry stop at the ask and, 3.6 s later on the same quote, the 17.6678 protective
stop at the bid (-2.6R) although no trade ever printed at the entry stop. VSTS and TPG the same.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.broker.test_sim_broker import Env, entry, env, quote  # noqa: F401  (env is a fixture)
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import SPREAD_WIDE, FillParams, QuoteFillModel
from trader.broker.types import FillDecision, NoFill, OrderSpec
from trader.db import models as m
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 2, 13, 35, 50, tzinfo=UTC)
MODEL = QuoteFillModel(FillParams())


def qq(bid: str, ask: str, last: str, *, age: float = 1.0) -> QtQuote:
    return QtQuote(
        symbol_id=1,
        symbol="BXDC",
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(last),
        last_regular=None,
        volume=1000,
        last_trade_time=NOW - timedelta(seconds=age),
        delay=0,
        is_halted=False,
        vwap=None,
        fetched_at=NOW - timedelta(seconds=0.2),
    )


def buy_stop(stop: str, stop_loss: str | None) -> OrderSpec:
    return OrderSpec(
        1, "buy", "stop", 5, stop=Decimal(stop), stop_loss=Decimal(stop_loss) if stop_loss else None
    )


def sell_stop(stop: str) -> OrderSpec:
    return OrderSpec(1, "sell", "stop", 5, stop=Decimal(stop), purpose="stop", position_id=9)


# --- the last trade triggers --------------------------------------------------------------------------------
def test_bxdc_regression_the_ask_alone_never_fills_the_entry() -> None:
    assert MODEL.assess(buy_stop("17.73", "17.6678"), qq("17.41", "17.83", "17.72"), NOW) == NoFill(
        "not_triggered"
    )


def test_bxdc_regression_the_bid_alone_never_fills_the_protective_stop() -> None:
    assert MODEL.assess(sell_stop("17.6678"), qq("17.41", "17.83", "17.72"), NOW) == NoFill("not_triggered")


def test_one_quote_can_never_fill_an_entry_and_its_stop() -> None:
    """The protective stop is always below the entry stop, so no last trade triggers both."""
    entry_spec, stop_spec = buy_stop("10.00", None), sell_stop("9.90")  # no stop_loss: no spread guard
    for last in ("9.85", "9.90", "9.95", "10.00", "10.05"):
        quote = qq("9.80", "10.10", last)  # a 30-cent spread around both stops
        entered = isinstance(MODEL.assess(entry_spec, quote, NOW), FillDecision)
        stopped = isinstance(MODEL.assess(stop_spec, quote, NOW), FillDecision)
        assert not (entered and stopped), last


def test_a_buy_stop_fills_at_the_ask_once_a_trade_prints_at_the_stop() -> None:
    out = MODEL.assess(buy_stop("10.00", None), qq("9.99", "10.02", "10.00"), NOW)
    assert isinstance(out, FillDecision) and out.price == Decimal("10.0300") and out.trigger == "stop"


def test_a_sell_stop_fills_at_the_bid_once_a_trade_prints_at_the_stop() -> None:
    out = MODEL.assess(sell_stop("9.90"), qq("9.88", "9.91", "9.90"), NOW)
    assert isinstance(out, FillDecision) and out.price == Decimal("9.8700") and out.trigger == "stop"


def test_market_and_limit_orders_are_unchanged() -> None:
    market = OrderSpec(1, "sell", "market", 5, purpose="exit", position_id=9)
    out = MODEL.assess(market, qq("9.88", "9.91", "9.95"), NOW)
    assert isinstance(out, FillDecision) and out.price == Decimal("9.8700")
    limit = OrderSpec(1, "buy", "limit", 5, limit=Decimal("10.00"))
    assert isinstance(MODEL.assess(limit, qq("9.98", "9.99", "10.20"), NOW), FillDecision)


# --- the spread guard ---------------------------------------------------------------------------------------
def test_a_triggered_entry_is_held_while_the_spread_is_over_half_the_stop_distance() -> None:
    # stop distance 17.73 - 17.6678 = 0.0622, allowed 0.0311; the spread is 0.42
    out = MODEL.assess(buy_stop("17.73", "17.6678"), qq("17.41", "17.83", "17.75"), NOW)
    assert isinstance(out, NoFill) and out.reason == SPREAD_WIDE and "0.42" in out.detail


def test_the_boundary_is_inclusive() -> None:
    # distance 0.10, allowed 0.05: a 0.05 spread fills, 0.06 is held
    ok = MODEL.assess(buy_stop("10.00", "9.90"), qq("9.97", "10.02", "10.01"), NOW)
    assert isinstance(ok, FillDecision)
    held = MODEL.assess(buy_stop("10.00", "9.90"), qq("9.96", "10.02", "10.01"), NOW)
    assert isinstance(held, NoFill) and held.reason == SPREAD_WIDE


def test_the_fraction_is_a_setting() -> None:
    loose = QuoteFillModel(
        FillParams.from_settings(RuntimeSettings(**{"fill.max_spread_stop_fraction": "2"}))
    )
    assert isinstance(
        loose.assess(buy_stop("10.00", "9.90"), qq("9.85", "10.02", "10.01"), NOW), FillDecision
    )
    assert RuntimeSettings().fill_max_spread_stop_fraction == Decimal("0.5")


def test_protective_stops_are_never_held_for_spread() -> None:
    out = MODEL.assess(sell_stop("17.6678"), qq("17.41", "17.83", "17.60"), NOW)
    assert isinstance(out, FillDecision) and out.price == Decimal("17.4000")


def test_an_entry_without_a_stop_loss_is_not_guarded() -> None:
    assert isinstance(MODEL.assess(buy_stop("17.73", None), qq("17.41", "17.83", "17.75"), NOW), FillDecision)


def test_an_untriggered_entry_reports_not_triggered_not_spread() -> None:
    out = MODEL.assess(buy_stop("17.73", "17.6678"), qq("17.41", "17.83", "17.72"), NOW)
    assert out == NoFill("not_triggered")


# --- the broker: held entries stay working, logged once -----------------------------------------------------
@pytest.mark.db
def test_a_held_entry_stays_working_logs_once_and_fills_when_the_spread_narrows(env: Env) -> None:  # noqa: F811
    oid = entry(env)  # stop 10.00, stop_loss 9.90: allowed spread 0.05
    wide = quote(env, "9.90", "10.05", "10.01")
    assert env.broker.on_quotes([wide], env.clock.now()) == []
    assert env.broker.on_quotes([wide], env.clock.now()) == []
    assert [o.id for o in env.broker.working_orders()] == [oid]
    with env.factory() as s:
        held = (
            s.execute(select(m.EventLog.level).where(m.EventLog.message.contains("entry held")))
            .scalars()
            .all()
        )
    assert held == ["warning"]
    (ev,) = env.broker.on_quotes([quote(env, "10.01", "10.03", "10.02")], env.clock.now())
    assert ev.order_id == oid
