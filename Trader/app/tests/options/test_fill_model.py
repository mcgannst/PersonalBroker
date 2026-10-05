"""OPTSIM T5: the option fill model. Buy at the ask, sell at the bid, all legs or none; the nine reasons for
no fill, in their order; a limit fills at the market net when that is at or better than the limit; fees per
contract and per leg."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.options import factories as f
from trader.options.fill_model import QuoteFillModel
from trader.options.settings import OptionSettings
from trader.options.types import FillDecision, LegQuote, NoFill, OptOrderType, OptOrderView, OrderLeg

D = Decimal
OPEN = f.T0 - timedelta(minutes=30)  # 09:30 ET
CLOSE = f.T0 + timedelta(hours=6)  # 16:00 ET
SETTINGS = OptionSettings()
MODEL = QuoteFillModel()


def order(
    legs: Sequence[OrderLeg], order_type: OptOrderType = "market", limit: str | None = None, qty: int = 1
) -> OptOrderView:
    return OptOrderView(
        id=1,
        source="manual",
        strategy_config_id=None,
        intent="open",
        structure_id=None,
        underlying="F",
        legs=tuple(legs),
        qty=qty,
        order_type=order_type,
        net_limit=None if limit is None else D(limit),
        tif="day",
        walk=False,
        take_profit_pct=None,
        reason="test",
        evidence={},
        submitted_by="test",
        status="working",
        reject_reason=None,
        reject_detail=None,
        reserved_cash=D(0),
        submitted_at=f.T0,
        closed_at=None,
        fill_net=None,
        fees=None,
    )


def lq(leg_no: int, bid: str | None, ask: str | None, **over: Any) -> LegQuote:
    values: dict[str, Any] = {"last": None, "fetched_at": f.T0, "delay": 0, "is_halted": False, "raw": {}}
    return LegQuote(
        leg_no=leg_no,
        bid=None if bid is None else D(bid),
        ask=None if ask is None else D(ask),
        **{**values, **over},
    )


def assess(o: OptOrderView, quotes: Sequence[LegQuote], now: datetime = f.T0) -> FillDecision | NoFill:
    return MODEL.assess(o, {q.leg_no: q for q in quotes}, now, OPEN, CLOSE, SETTINGS)


SELL = f.make_leg(1, side="sell")
BUY = f.make_leg(1, side="buy")
SELL_LIMIT = order([SELL], "limit", "0.45")


@pytest.mark.parametrize(
    ("reason", "o", "quotes", "now"),
    [
        ("outside_hours", SELL_LIMIT, [lq(1, "0.45", "0.50")], CLOSE),
        ("quote_missing", SELL_LIMIT, [], f.T0),
        ("quote_delayed", SELL_LIMIT, [lq(1, "0.45", "0.50", delay=None)], f.T0),
        ("halted", SELL_LIMIT, [lq(1, "0.45", "0.50", is_halted=True)], f.T0),
        ("quote_stale", SELL_LIMIT, [lq(1, "0.45", "0.50", fetched_at=f.T0 - timedelta(seconds=16))], f.T0),
        ("one_sided", SELL_LIMIT, [lq(1, "0.45", None)], f.T0),
        ("crossed", SELL_LIMIT, [lq(1, "0.50", "0.45")], f.T0),
        ("zero_bid", order([SELL]), [lq(1, "0", "0.05")], f.T0),
        ("limit_not_reached", order([SELL], "limit", "0.46"), [lq(1, "0.45", "0.50")], f.T0),
    ],
)
def test_no_fill_matrix(reason: str, o: OptOrderView, quotes: list[LegQuote], now: datetime) -> None:
    result = assess(o, quotes, now)
    assert isinstance(result, NoFill) and result.reason == reason


def test_the_edges_that_still_fill() -> None:
    edge = lq(1, "0.45", "0.45", fetched_at=f.T0 - timedelta(seconds=15))  # locked, and exactly at the age
    assert isinstance(assess(SELL_LIMIT, [edge], OPEN), FillDecision)  # the open itself is inside
    assert isinstance(assess(order([BUY]), [lq(1, "0", "0.05")]), FillDecision)  # a buy ignores a zero bid


PUT_SOLD = f.make_leg(1, side="sell", contract_id=1)
PUT_BOUGHT = f.make_leg(2, side="buy", contract_id=2)
CALL_BOUGHT = f.make_leg(1, side="buy", contract_id=3)
CALL_SOLD = f.make_leg(2, side="sell", contract_id=4)
SHARES = f.make_leg(1, instrument="shares", side="buy", ratio=100)
CREDIT_QUOTES = [lq(1, "0.45", "0.50"), lq(2, "0.15", "0.20")]


@pytest.mark.parametrize(
    ("o", "quotes", "net", "prices"),
    [
        (order([BUY]), [lq(1, "1.10", "1.20")], "-1.20", ["1.20"]),
        (order([SELL]), [lq(1, "0.45", "0.50")], "0.45", ["0.45"]),
        (order([PUT_SOLD, PUT_BOUGHT]), CREDIT_QUOTES, "0.25", ["0.45", "0.20"]),
        (
            order([CALL_BOUGHT, CALL_SOLD]),
            [lq(1, "1.10", "1.20"), lq(2, "0.50", "0.55")],
            "-0.70",
            ["1.20", "0.50"],
        ),
        (  # a buy-write: 100 shares at the ask, one call at the bid
            order([SHARES, CALL_SOLD]),
            [lq(1, "14.79", "14.80"), lq(2, "0.30", "0.35")],
            "-14.50",
            ["14.80", "0.30"],
        ),
        (order([PUT_SOLD, PUT_BOUGHT], "limit", "0.25"), CREDIT_QUOTES, "0.25", ["0.45", "0.20"]),
        (order([PUT_SOLD, PUT_BOUGHT], "limit", "0.20"), CREDIT_QUOTES, "0.25", ["0.45", "0.20"]),
        (order([PUT_SOLD, PUT_BOUGHT], "limit", "0.26"), CREDIT_QUOTES, None, []),  # one cent better: no
    ],
)
def test_fill_prices(o: OptOrderView, quotes: list[LegQuote], net: str | None, prices: list[str]) -> None:
    result = assess(o, quotes)
    if net is None:
        assert isinstance(result, NoFill) and result.reason == "limit_not_reached"
        return
    assert isinstance(result, FillDecision)
    assert result.net_price == D(net)  # the market net, never the limit
    assert [price for _, price in result.leg_prices] == [D(p) for p in prices]
    assert result.trigger == o.order_type


def test_fees_per_contract_per_leg() -> None:
    settings = OptionSettings(share_commission=D("1.00"))
    legs = [
        f.make_leg(1, side="sell", ratio=1, contract_id=1),
        f.make_leg(2, side="buy", ratio=2, contract_id=2),
        f.make_leg(3, instrument="shares", side="buy", ratio=100),
    ]
    quotes = {n: lq(n, "0.45", "0.50") for n in (1, 2, 3)}
    result = MODEL.assess(order(legs, qty=3), quotes, f.T0, OPEN, CLOSE, settings)
    assert isinstance(result, FillDecision)
    assert result.fees == D("0.99") * (3 + 6) + D("1.00")  # 9 contracts, and one commission for the shares


def test_the_contract_multiplier_comes_from_the_leg_quote() -> None:
    mini = lq(1, "0.45", "0.50", raw={"multiplier": 10})
    result = assess(order([SELL]), [mini])
    assert isinstance(result, FillDecision) and result.net_price == D("0.045")
