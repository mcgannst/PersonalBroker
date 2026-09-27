from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.types import FillDecision, FillModel, NoFill, OrderSpec
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
MODEL = QuoteFillModel(FillParams())


def q(
    bid: str | None = "9.99",
    ask: str | None = "10.00",
    last: str | None = "10.00",
    age: float | None = 1.0,
    *,
    symbol_id: int = 1,
    halted: bool = False,
    delay: int | None = 0,
) -> QtQuote:
    return QtQuote(
        symbol_id=symbol_id,
        symbol="AAA",
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        last=Decimal(last) if last is not None else None,
        last_regular=None,
        volume=100_000,
        last_trade_time=NOW - timedelta(seconds=age) if age is not None else None,
        delay=delay,
        is_halted=halted,
        vwap=None,
    )


def buy(
    order_type: str = "market", stop: str | None = None, limit: str | None = None, qty: int = 100
) -> OrderSpec:
    return OrderSpec(
        1,
        "buy",
        order_type,  # type: ignore[arg-type]
        qty,
        stop=Decimal(stop) if stop else None,
        limit=Decimal(limit) if limit else None,
    )


def sell(
    order_type: str = "market", stop: str | None = None, limit: str | None = None, qty: int = 100
) -> OrderSpec:
    return OrderSpec(
        1,
        "sell",
        order_type,  # type: ignore[arg-type]
        qty,
        stop=Decimal(stop) if stop else None,
        limit=Decimal(limit) if limit else None,
        purpose="stop" if order_type == "stop" else "exit",
        position_id=7,
    )


def filled(order: OrderSpec, quote: QtQuote) -> FillDecision:
    out = MODEL.evaluate(order, quote, NOW)
    assert isinstance(out, FillDecision), MODEL.assess(order, quote, NOW)
    return out


def test_buy_market_fills_at_ask_plus_min_slippage() -> None:
    d = filled(buy(), q(ask="10.00"))
    assert (d.price, d.slippage, d.trigger, d.qty) == (Decimal("10.0100"), Decimal("0.0100"), "market", 100)


def test_sell_market_fills_at_bid_minus_slippage() -> None:
    assert filled(sell(), q(bid="9.99")).price == Decimal("9.9800")


def test_bps_slippage_dominates_on_higher_prices() -> None:
    d = filled(buy(), q(ask="50.00", last="50.00"))
    assert d.slippage == Decimal("0.0250") and d.price == Decimal("50.0250")


def test_buy_stop_waits_for_the_trigger() -> None:
    out = MODEL.assess(buy("stop", stop="10.10"), q(ask="10.05", last="10.00"), NOW)
    assert out == NoFill("not_triggered")


def test_buy_stop_triggered_by_last() -> None:
    assert filled(buy("stop", stop="10.00"), q(ask="9.99", last="10.00")).price == Decimal("10.0100")


def test_buy_stop_gap_fills_at_the_ask() -> None:
    assert filled(buy("stop", stop="10.00"), q(ask="10.20", last="10.18")).price == Decimal("10.2100")


def test_sell_stop_triggered_by_bid_fills_below_it() -> None:
    assert filled(sell("stop", stop="10.00"), q(bid="9.80", ask="9.81", last="9.85")).price == Decimal(
        "9.7900"
    )


def test_sell_stop_triggered_by_last() -> None:
    assert filled(sell("stop", stop="10.00"), q(bid="10.01", ask="10.02", last="9.99")).price == Decimal(
        "9.9900"
    )


def test_buy_stop_limit_fills_inside_the_limit() -> None:
    d = filled(buy("stop_limit", stop="10.00", limit="10.05"), q(ask="10.03", last="10.02"))
    assert d.price == Decimal("10.0400") and d.trigger == "stop_limit"


def test_buy_stop_limit_refused_above_the_limit() -> None:
    out = MODEL.assess(buy("stop_limit", stop="10.00", limit="10.05"), q(ask="10.05", last="10.04"), NOW)
    assert isinstance(out, NoFill) and out.reason == "above_limit"


def test_buy_stop_limit_not_triggered() -> None:
    out = MODEL.assess(
        buy("stop_limit", stop="10.00", limit="10.05"), q(bid="9.94", ask="9.95", last="9.94"), NOW
    )
    assert out == NoFill("not_triggered")


def test_sell_stop_limit_refused_below_the_limit() -> None:
    out = MODEL.assess(sell("stop_limit", stop="10.00", limit="9.95"), q(bid="9.94", last="9.94"), NOW)
    assert isinstance(out, NoFill) and out.reason == "below_limit"


def test_limit_orders_fill_at_the_limit_without_slippage() -> None:
    d = filled(buy("limit", limit="10.00"), q(bid="9.97", ask="9.98"))
    assert (d.price, d.slippage) == (Decimal("10.00"), Decimal("0"))
    assert MODEL.evaluate(buy("limit", limit="10.00"), q(ask="10.01"), NOW) is None
    assert filled(sell("limit", limit="10.00"), q(bid="10.02", ask="10.03")).price == Decimal("10.00")
    assert MODEL.evaluate(sell("limit", limit="10.00"), q(bid="9.99"), NOW) is None


def test_stale_quote_never_fills() -> None:
    """Review Focus 1: a quote older than stale_quote_seconds never fills, whatever the order."""
    for order in (buy(), sell(), buy("stop", stop="9.00"), sell("stop", stop="11.00")):
        out = MODEL.assess(order, q(age=10.5), NOW)
        assert isinstance(out, NoFill) and out.reason == "stale_quote"
        assert MODEL.evaluate(order, q(age=10.5), NOW) is None
    assert MODEL.evaluate(buy(), q(age=10.0), NOW) is not None  # exactly at the limit is still fresh
    assert MODEL.assess(buy(), q(age=None), NOW) == NoFill("stale_quote", "the quote has no time")


@pytest.mark.parametrize(
    ("order", "quote", "reason"),
    [
        (buy(), q(halted=True), "halted"),
        (buy(), q(delay=15), "delayed_quote"),
        (buy(), q(delay=None), "delayed_quote"),
        (buy(), q(ask=None), "no_ask"),
        (buy(), q(ask="0"), "no_ask"),
        (sell(), q(bid=None), "no_bid"),
    ],
)
def test_unusable_quotes_never_fill(order: OrderSpec, quote: QtQuote, reason: str) -> None:
    """Review Focus 1: halted, delayed or one-sided quotes never fill."""
    out = MODEL.assess(order, quote, NOW)
    assert isinstance(out, NoFill) and out.reason == reason


def test_sec_fee_on_sells_only() -> None:
    d = filled(sell(), q(bid="10.00"))  # 100 x 9.99 = 999.00 x 0.0000206 = 0.0205794
    assert d.fees.sec == Decimal("0.0206") and d.fees.total == Decimal("0.0206")
    assert filled(buy(), q()).fees.total == 0


def test_ecn_only_with_direct_route_and_commission_per_fill() -> None:
    m = QuoteFillModel(FillParams(direct_route=True, commission=Decimal("1")))
    d = m.evaluate(buy(), q(), NOW)
    assert d is not None
    assert (d.fees.commission, d.fees.ecn, d.fees.total) == (
        Decimal("1"),
        Decimal("0.3500"),
        Decimal("1.3500"),
    )


def test_quote_snapshot_is_returned() -> None:
    d = filled(buy(), q(bid="9.99", ask="10.00", last="10.00"))
    assert d.quote_snapshot["bid"] == "9.99" and d.quote_snapshot["ask"] == "10.00"
    assert d.quote_snapshot["time"] == (NOW - timedelta(seconds=1)).isoformat()
    assert d.quote_snapshot["evaluated_at"] == NOW.isoformat()


def test_wrong_symbol_raises() -> None:
    with pytest.raises(ValueError, match="symbol"):
        MODEL.assess(buy(), q(symbol_id=2), NOW)


def test_candles_are_not_this_models_job() -> None:
    c = Candle(NOW, NOW + timedelta(minutes=1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), 1, None)
    with pytest.raises(TypeError):
        MODEL.evaluate(buy(), c, NOW)
    with pytest.raises(TypeError):
        MODEL.assess(buy(), c, NOW)


def test_quote_model_satisfies_the_fill_model_protocol() -> None:
    model: FillModel = MODEL  # mypy checks the protocol; the broker is typed to FillModel, not QuoteFillModel
    assert model.evaluate(buy(), q(), NOW) is not None


def test_params_from_settings() -> None:
    s = RuntimeSettings(slippage_min=Decimal("0.02"), stale_quote_seconds=5.0, fees_direct_route=True)
    p = FillParams.from_settings(s)
    assert (p.slippage_min, p.stale_quote_seconds, p.direct_route) == (Decimal("0.02"), 5.0, True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": "buy", "order_type": "market", "qty": 0},
        {"side": "buy", "order_type": "stop", "qty": 1},
        {"side": "buy", "order_type": "limit", "qty": 1},
        {"side": "buy", "order_type": "market", "qty": 1, "purpose": "exit"},
        {"side": "sell", "order_type": "market", "qty": 1, "purpose": "exit"},
        {"side": "sell", "order_type": "market", "qty": 1, "purpose": "entry", "position_id": 1},
    ],
)
def test_order_spec_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        OrderSpec(symbol_id=1, **kwargs)  # type: ignore[arg-type]


def test_order_spec_json_round_trip() -> None:
    spec = OrderSpec(
        1, "buy", "stop", 10, stop=Decimal("10.01"), stop_loss=Decimal("9.91"), strategy_config_id=3
    )
    assert OrderSpec.from_json(spec.to_json()) == spec
    assert spec.to_json()["stop"] == "10.01"
