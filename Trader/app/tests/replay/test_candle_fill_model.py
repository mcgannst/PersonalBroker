"""P5-T3 tests 2-10: the SPEC §7.4 candle fill model never flatters the strategy.

Defaults (`FillParams()`): slippage max(0.01, 5 bps x price); half spread 5 bps (`replay.half_spread_bps`).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.types import FillDecision, FillModel, NoFill, OrderSpec
from trader.market.types import Candle
from trader.replay.candle_fill_model import CANDLE_SNAPSHOT_SOURCE, CandleFillModel

START = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)  # 10:00 ET
NOW = START + timedelta(minutes=1)
HS = Decimal("5")
MODEL = CandleFillModel(FillParams(), HS)


def bar(o: str, h: str, low: str, c: str, v: int = 1000) -> Candle:
    return Candle(
        start=START,
        end=START + timedelta(minutes=1),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=v,
        vwap=None,
    )


def buy(order_type: str = "market", stop: str | None = None, limit: str | None = None) -> OrderSpec:
    return OrderSpec(
        1,
        "buy",
        order_type,  # type: ignore[arg-type]
        100,
        stop=Decimal(stop) if stop else None,
        limit=Decimal(limit) if limit else None,
    )


def sell(order_type: str = "market", stop: str | None = None, limit: str | None = None) -> OrderSpec:
    return OrderSpec(
        1,
        "sell",
        order_type,  # type: ignore[arg-type]
        100,
        stop=Decimal(stop) if stop else None,
        limit=Decimal(limit) if limit else None,
        purpose="stop" if order_type in ("stop", "stop_limit") else "exit",
        position_id=7,
    )


def filled(order: OrderSpec, candle: Candle, model: CandleFillModel = MODEL) -> FillDecision:
    out = model.assess(order, candle, NOW)
    assert isinstance(out, FillDecision), out
    return out


def refused(order: OrderSpec, candle: Candle) -> NoFill:
    out = MODEL.assess(order, candle, NOW)
    assert isinstance(out, NoFill), out
    assert MODEL.evaluate(order, candle, NOW) is None
    return out


# --- protocol and determinism (test 2) ----------------------------------------------------------------------


def test_satisfies_fill_model_protocol() -> None:
    model: FillModel = CandleFillModel(FillParams(), HS)  # typed assignment, checked by mypy
    assert model.evaluate(buy(), bar("10.00", "10.10", "9.90", "10.05"), NOW) is not None


def _without_time(d: FillDecision) -> tuple[Any, ...]:
    snap = dict(d.quote_snapshot)
    snap.pop("evaluated_at")
    return (d.price, d.qty, d.slippage, d.fees, d.trigger, snap)


@pytest.mark.parametrize(
    ("order", "candle"),
    [
        (buy(), bar("10.00", "10.10", "9.90", "10.05")),
        (sell(), bar("10.00", "10.10", "9.90", "10.05")),
        (buy("stop", stop="10.20"), bar("10.00", "10.35", "9.95", "10.30")),
        (sell("stop", stop="9.80"), bar("9.60", "9.65", "9.50", "9.55")),
        (buy("limit", limit="10.00"), bar("9.90", "10.02", "9.88", "10.00")),
        (buy("stop_limit", stop="10.20", limit="10.25"), bar("10.00", "10.35", "9.95", "10.30")),
    ],
)
def test_two_models_decide_identically_and_only_evaluated_at_depends_on_now(
    order: OrderSpec, candle: Candle
) -> None:
    a = CandleFillModel(FillParams(), Decimal("5"))
    b = CandleFillModel(FillParams(), Decimal("5"))
    da = filled(order, candle, a)
    assert da == filled(order, candle, b)
    later = b.assess(order, candle, NOW + timedelta(hours=3))
    assert isinstance(later, FillDecision)
    assert _without_time(later) == _without_time(da)
    assert later.quote_snapshot["evaluated_at"] == (NOW + timedelta(hours=3)).isoformat()
    assert da.quote_snapshot["evaluated_at"] == NOW.isoformat()


def test_refusals_do_not_depend_on_now() -> None:
    order, candle = buy("stop", stop="10.20"), bar("10.00", "10.19", "9.95", "10.10")
    assert MODEL.assess(order, candle, NOW) == MODEL.assess(order, candle, NOW + timedelta(days=30))


# --- stops (tests 3-6) --------------------------------------------------------------------------------------


def test_buy_stop_fills_at_stop_plus_slippage_plus_half_spread() -> None:
    candle = bar("10.00", "10.35", "9.95", "10.30", v=4321)
    d = filled(buy("stop", stop="10.20"), candle)
    assert d.price == Decimal("10.2151")  # 10.20 + 0.01 + 0.0051
    assert d.trigger == "stop"
    assert d.slippage == Decimal("0.0100")
    assert d.qty == 100
    assert d.quote_snapshot == {
        "source": CANDLE_SNAPSHOT_SOURCE,
        "start": START.isoformat(),
        "end": (START + timedelta(minutes=1)).isoformat(),
        "open": "10.00",
        "high": "10.35",
        "low": "9.95",
        "close": "10.30",
        "volume": 4321,
        "half_spread": "0.0051",
        "evaluated_at": NOW.isoformat(),
    }
    assert CANDLE_SNAPSHOT_SOURCE == "candle_1m"
    assert MODEL.evaluate(buy("stop", stop="10.20"), candle, NOW) == d


def test_buy_stop_touched_exactly_at_the_high_triggers() -> None:
    d = filled(buy("stop", stop="10.20"), bar("10.00", "10.20", "9.95", "10.10"))
    assert d.price == Decimal("10.2151")


def test_buy_stop_gap_up_fills_at_the_open() -> None:
    d = filled(buy("stop", stop="10.20"), bar("10.40", "10.50", "10.38", "10.45"))
    assert d.price == Decimal("10.4152")  # 10.40 + 0.01 + 0.0052, worse than the stop
    assert d.trigger == "stop_gap"
    assert d.slippage == Decimal("0.0100")
    assert d.quote_snapshot["half_spread"] == "0.0052"


def test_sell_stop_fills_below_the_stop() -> None:
    d = filled(sell("stop", stop="9.80"), bar("9.90", "9.95", "9.70", "9.75"))
    assert d.price == Decimal("9.7851")  # 9.80 - 0.01 - 0.0049
    assert d.trigger == "stop"
    assert d.slippage == Decimal("0.0100")
    assert d.quote_snapshot["half_spread"] == "0.0049"


def test_sell_stop_gap_through_fills_at_the_open_not_the_stop() -> None:
    d = filled(sell("stop", stop="9.80"), bar("9.60", "9.65", "9.50", "9.55"))
    assert d.price == Decimal("9.5852")  # 9.60 - 0.01 - 0.0048, worse than the stop
    assert d.trigger == "stop_gap"
    assert d.quote_snapshot["half_spread"] == "0.0048"


def test_same_bar_worst_case_bar_reopened_at_the_entry_fill_stops_out_at_the_stop() -> None:
    """T4's same-bar pass re-applies the entry bar reopened at the entry fill price, its range widened to
    include that price (fix round 1: the fill can lie above the bar's high after slippage and half spread)."""

    def reopen(b: Candle, p: Decimal) -> Candle:
        return replace(b, open=p, high=max(b.high, p), low=min(b.low, p))

    entry_bar = bar("10.00", "10.35", "9.70", "9.75")
    entry = filled(buy("stop", stop="10.20"), entry_bar)
    stop = filled(sell("stop", stop="9.80"), reopen(entry_bar, entry.price))
    assert stop.price == Decimal("9.7851")  # min(stop, reopened open) = stop
    assert stop.trigger == "stop"
    gap_bar = bar("10.40", "10.41", "9.70", "9.75")
    gap_entry = filled(buy("stop", stop="10.20"), gap_bar)
    assert gap_entry.price == Decimal("10.4152") > gap_bar.high
    assert filled(sell("stop", stop="9.80"), reopen(gap_bar, gap_entry.price)).price == Decimal("9.7851")


def test_an_open_outside_the_bars_range_is_a_bad_bar() -> None:
    """Fix round 1: a corrupt open (above the high or below the low) never fills; zero volume still reads
    `no_volume` first."""
    for o in ("10.50", "9.60"):
        corrupt = bar(o, "10.35", "9.70", "9.75")
        for order in (buy("stop", stop="10.20"), sell("stop", stop="9.80"), buy("market"), sell("market")):
            assert refused(order, corrupt).reason == "bad_bar"
    assert refused(buy("market"), replace(bar("10.50", "10.35", "9.70", "9.75"), volume=0)).reason == (
        "no_volume"
    )


def test_stops_not_reached_do_not_fill() -> None:
    assert (
        refused(buy("stop", stop="10.20"), bar("10.00", "10.19", "9.95", "10.10")).reason == "not_triggered"
    )
    assert refused(sell("stop", stop="9.80"), bar("9.90", "9.95", "9.81", "9.85")).reason == "not_triggered"


# --- market orders (test 7) ---------------------------------------------------------------------------------


def test_market_orders_use_the_open_plus_or_minus_slip_and_half_spread() -> None:
    candle = bar("10.00", "10.10", "9.90", "10.05")
    b = filled(buy(), candle)
    assert b.price == Decimal("10.0150")  # 10.00 + 0.01 + 0.005
    assert (b.trigger, b.slippage, b.quote_snapshot["half_spread"]) == ("market", Decimal("0.0100"), "0.0050")
    s = filled(sell(), candle)
    assert s.price == Decimal("9.9850")
    assert (s.trigger, s.slippage) == ("market", Decimal("0.0100"))


def test_slippage_uses_bps_above_the_minimum() -> None:
    assert MODEL.slip(Decimal("250.00")) == Decimal("0.1250")
    assert MODEL.slip(Decimal("10.20")) == Decimal("0.0100")
    assert MODEL.half_spread(Decimal("250.00")) == Decimal("0.1250")
    assert MODEL.half_spread(Decimal("10.20")) == Decimal("0.0051")
    assert MODEL.half_spread(Decimal("10.01")) == Decimal("0.0050")  # 0.005005 -> 4 dp half-up
    assert MODEL.half_spread(Decimal("10.09")) == Decimal("0.0050")  # 0.005045
    assert MODEL.half_spread(Decimal("10.10")) == Decimal("0.0051")  # 0.00505 half-up
    assert CandleFillModel(FillParams(), Decimal("0")).half_spread(Decimal("10")) == Decimal("0.0000")
    d = filled(buy(), bar("250.00", "251.00", "249.00", "250.50"))
    assert d.price == Decimal("250.2500")
    assert d.slippage == Decimal("0.1250")


def test_market_sell_that_would_price_at_zero_does_not_fill() -> None:
    assert refused(sell(), bar("0.01", "0.01", "0.01", "0.01")).reason == "no_bid"


# --- stop-limit and limit orders (test 8) -------------------------------------------------------------------


def test_buy_stop_limit_fills_only_within_the_limit() -> None:
    candle = bar("10.00", "10.35", "9.95", "10.30")
    assert refused(buy("stop_limit", stop="10.20", limit="10.21"), candle).reason == "above_limit"
    d = filled(buy("stop_limit", stop="10.20", limit="10.25"), candle)
    assert d.price == Decimal("10.2151")
    assert d.trigger == "stop_limit"
    assert d.slippage == Decimal("0.0100")
    gap = bar("10.40", "10.50", "10.38", "10.45")
    assert refused(buy("stop_limit", stop="10.20", limit="10.25"), gap).reason == "above_limit"
    assert refused(
        buy("stop_limit", stop="10.20", limit="10.25"), bar("10.00", "10.19", "9.9", "10")
    ).reason == ("not_triggered")


def test_sell_stop_limit_mirrors() -> None:
    candle = bar("9.90", "9.95", "9.70", "9.75")
    assert refused(sell("stop_limit", stop="9.80", limit="9.79"), candle).reason == "below_limit"
    d = filled(sell("stop_limit", stop="9.80", limit="9.78"), candle)
    assert (d.price, d.trigger) == (Decimal("9.7851"), "stop_limit")


def test_buy_limit_fills_at_exactly_the_limit_never_better() -> None:
    d = filled(buy("limit", limit="10.00"), bar("10.05", "10.10", "9.99", "10.02"))
    assert (d.price, d.slippage, d.trigger) == (Decimal("10.00"), Decimal("0"), "limit")
    assert d.quote_snapshot["half_spread"] == "0.0050"  # hs of the limit price
    opened_below = filled(buy("limit", limit="10.00"), bar("9.90", "10.02", "9.88", "10.00"))
    assert (opened_below.price, opened_below.slippage) == (Decimal("10.00"), Decimal("0"))
    touched = bar("10.05", "10.10", "9.995", "10.02")  # low + hs = 10.000 <= 10.00
    assert filled(buy("limit", limit="10.00"), touched).price == Decimal("10.00")
    assert (
        refused(buy("limit", limit="10.00"), bar("10.05", "10.10", "9.999", "10.02")).reason
        == "not_triggered"
    )


def test_sell_limit_mirrors() -> None:
    d = filled(sell("limit", limit="10.00"), bar("9.95", "10.01", "9.90", "9.98"))
    assert (d.price, d.slippage, d.trigger) == (Decimal("10.00"), Decimal("0"), "limit")
    opened_above = filled(sell("limit", limit="10.00"), bar("10.10", "10.12", "9.98", "10.00"))
    assert opened_above.price == Decimal("10.00")
    assert (
        refused(sell("limit", limit="10.00"), bar("9.95", "10.001", "9.90", "9.98")).reason == "not_triggered"
    )


# --- bad data (test 9) --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candle",
    [
        bar("10.00", "9.90", "10.10", "10.00"),  # low > high
        bar("0", "10.10", "9.90", "10.00"),
        bar("10.00", "10.10", "-1", "10.00"),
        bar("10.00", "0", "0", "10.00"),
        bar("10.00", "10.10", "9.90", "0"),
        bar("10.00", "10.10", "9.90", "10.00", v=-5),
    ],
)
def test_bad_bars_never_fill(candle: Candle) -> None:
    for order in (
        buy(),
        sell(),
        buy("stop", stop="0.01"),
        sell("stop", stop="100"),
        buy("limit", limit="100"),
    ):
        assert refused(order, candle).reason == "bad_bar"


def test_zero_volume_never_fills() -> None:
    candle = bar("10.00", "10.10", "9.90", "10.00", v=0)
    for order in (buy(), sell(), buy("stop", stop="10.05"), sell("limit", limit="9.95")):
        assert refused(order, candle).reason == "no_volume"


def test_a_quote_is_a_type_error() -> None:
    quote = QtQuote(
        symbol_id=1,
        symbol="AAA",
        bid=Decimal("9.99"),
        ask=Decimal("10.00"),
        last=Decimal("10.00"),
        last_regular=None,
        volume=1000,
        last_trade_time=NOW,
        delay=0,
        is_halted=False,
        vwap=None,
    )
    with pytest.raises(TypeError):
        MODEL.assess(buy(), quote, NOW)
    with pytest.raises(TypeError):
        MODEL.evaluate(buy(), quote, NOW)


# --- fees (test 10) -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        FillParams(),
        FillParams(commission=Decimal("4.95"), direct_route=True, ecn_per_share=Decimal("0.0035")),
        FillParams(sec_fee_rate=Decimal("0.0000278"), commission=Decimal("0.123456")),
    ],
)
def test_fees_are_identical_to_the_quote_model(params: FillParams) -> None:
    candle_model, quote_model = CandleFillModel(params, HS), QuoteFillModel(params)
    for side in ("buy", "sell"):
        for qty, price in ((100, Decimal("9.7851")), (37, Decimal("250.25")), (1, Decimal("0.5"))):
            assert candle_model.fees(side, qty, price) == quote_model.fees(side, qty, price)


def test_sec_fee_on_sells_only_rounded_to_4dp() -> None:
    assert MODEL.fees("buy", 100, Decimal("9.7851")).sec == Decimal("0")
    assert MODEL.fees("sell", 100, Decimal("9.7851")).sec == Decimal("0.0202")  # 0.020157...
    d = filled(sell("stop", stop="9.80"), bar("9.90", "9.95", "9.70", "9.75"))
    assert d.fees == MODEL.fees("sell", 100, Decimal("9.7851"))
    assert filled(buy(), bar("10.00", "10.10", "9.90", "10.05")).fees.sec == Decimal("0")
