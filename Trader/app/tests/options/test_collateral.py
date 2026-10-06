"""The collateral engine (OPTSIM T4): one row per case of the cover table (feature plan §3.3) and one per
rejection. Everything is a value: no database, no clock, no network.

Contracts on F (multiplier 100): puts 14.50, 13 and 12 and calls 15 and 16 for November; calls 14, 15 and
16 for December; a call 15 for October. Every quote is 0.45 bid, 0.50 ask unless a row says otherwise. The
fee is 0.99 per contract and leg, and the cap is half of an account value of 5,000."""

import ast
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import trader.options.collateral as collateral_module
from tests.options.factories import EXPIRY, SESSION, T0, make_contract, make_leg, make_quote, make_request
from trader.adapters.questrade.models import QtQuote
from trader.options.collateral import DefaultCollateralEngine
from trader.options.protocols import CollateralBook
from trader.options.settings import OptionSettings
from trader.options.types import (
    SOURCE_MANUAL,
    ZERO,
    CollateralDecision,
    Effect,
    OptionAccountState,
    OptOrderView,
    OptPositionView,
    OrderIntent,
    OrderLeg,
    OrderRequest,
    Side,
    StructureKind,
    StructureView,
)

OCT, NOV, DEC = date(2026, 10, 16), EXPIRY, date(2026, 12, 18)
P145, P13, C15, C16, C15_DEC, C16_DEC, C14_DEC, C15_OCT, P_EXPIRED, P_OTHER, P12 = range(1, 12)
CONTRACTS = {
    c.id: c
    for c in (
        make_contract(P145, strike="14.50", right="put"),
        make_contract(P13, strike="13", right="put"),
        make_contract(C15, strike="15", right="call"),
        make_contract(C16, strike="16", right="call"),
        make_contract(C15_DEC, strike="15", right="call", expiry=DEC),
        make_contract(C16_DEC, strike="16", right="call", expiry=DEC),
        make_contract(C14_DEC, strike="14", right="call", expiry=DEC),
        make_contract(C15_OCT, strike="15", right="call", expiry=OCT),
        make_contract(P_EXPIRED, strike="14.50", right="put", expiry=date(2026, 10, 2)),
        make_contract(P_OTHER, underlying="T", strike="20", right="put"),
        make_contract(P12, strike="12", right="put"),
    )
}
SHARE_QUOTE = QtQuote(
    symbol_id=1,
    symbol="F",
    bid=Decimal("14.95"),
    ask=Decimal("15.00"),
    last=Decimal("14.97"),
    last_regular=None,
    volume=0,
    last_trade_time=None,
    delay=0,
    is_halted=False,
    vwap=None,
)
ENGINE = DefaultCollateralEngine()


def D(value: str | int) -> Decimal:
    return Decimal(str(value))


# --- builders -----------------------------------------------------------------------------------------------


def sell(contract_id: int, leg_no: int = 1, effect: Effect = "open", ratio: int = 1) -> OrderLeg:
    return make_leg(leg_no, side="sell", effect=effect, ratio=ratio, contract_id=contract_id)


def buy(contract_id: int, leg_no: int = 1, effect: Effect = "open", ratio: int = 1) -> OrderLeg:
    return make_leg(leg_no, side="buy", effect=effect, ratio=ratio, contract_id=contract_id)


def shares(side: Side, leg_no: int = 1, effect: Effect = "open", ratio: int = 100) -> OrderLeg:
    return make_leg(leg_no, instrument="shares", side=side, effect=effect, ratio=ratio)


def order(legs: Sequence[OrderLeg], net: str | None = "0.45", **kwargs: Any) -> OrderRequest:
    """A limit order at `net`; `net=None` makes it a market order."""
    if net is None:
        return make_request(legs, order_type="market", **kwargs)
    return make_request(legs, net_limit=D(net), **kwargs)


def structure(
    id: int,
    kind: StructureKind,
    positions: Sequence[tuple[int | None, int, str]],  # (contract id or None for shares, qty, avg price)
    *,
    source: str = SOURCE_MANUAL,
    underlying: str = "F",
    reserved: str = "0",
    frozen: bool = False,
    cover: int | None = None,
) -> StructureView:
    return StructureView(
        id=id,
        source=source,
        strategy_config_id=None,
        kind=kind,
        underlying=underlying,
        state="open",
        close_reason=None,
        frozen=frozen,
        qty=1,
        entry_net=ZERO,
        reserved_cash=D(reserved),
        take_profit_net=None,
        cover_structure_id=cover,
        parent_structure_id=None,
        realized_pnl=ZERO,
        fees_total=ZERO,
        opened_at=T0,
        closed_at=None,
        positions=tuple(
            OptPositionView(
                id=id * 10 + n,
                structure_id=id,
                instrument="shares" if contract_id is None else "option",
                contract=None if contract_id is None else CONTRACTS[contract_id],
                underlying=underlying,
                qty=qty,
                avg_price=D(avg),
                realized_pnl=ZERO,
            )
            for n, (contract_id, qty, avg) in enumerate(positions)
        ),
        meta={},
    )


def working(
    legs: Sequence[OrderLeg],
    *,
    intent: OrderIntent = "open",
    structure_id: int | None = None,
    reserved: str = "0",
    source: str = SOURCE_MANUAL,
    underlying: str = "F",
) -> OptOrderView:
    return OptOrderView(
        id=900,
        source=source,
        strategy_config_id=None,
        intent=intent,
        structure_id=structure_id,
        underlying=underlying,
        legs=tuple(legs),
        qty=1,
        order_type="limit",
        net_limit=D("0.45"),
        tif="day",
        walk=False,
        take_profit_pct=None,
        reason="test",
        evidence={},
        submitted_by="test",
        status="working",
        reject_reason=None,
        reject_detail=None,
        reserved_cash=D(reserved),
        submitted_at=T0,
        closed_at=None,
        fill_net=None,
        fees=None,
    )


def book(
    *,
    cash: str = "5000",
    equity: str = "5000",
    structures: Sequence[StructureView] = (),
    orders: Sequence[OptOrderView] = (),
    quotes: Mapping[int, tuple[str | None, str | None]] | None = None,
    **settings: Any,
) -> CollateralBook:
    """A snapshot with `cash`; `quotes` overrides (bid, ask) per contract; `settings` by field name."""
    all_quotes = {cid: make_quote(cid) for cid in CONTRACTS}
    for cid, (bid, ask) in (quotes or {}).items():
        all_quotes[cid] = make_quote(cid, bid, ask)
    return CollateralBook(
        account=OptionAccountState(
            cash=D(cash),
            reserved=ZERO,
            free_cash=D(cash),
            positions_value=ZERO,
            equity=D(equity),
            premium_collected=ZERO,
            as_of=T0,
            marks_complete=True,
        ),
        structures=tuple(structures),
        working_orders=tuple(orders),
        contracts=CONTRACTS,
        quotes=all_quotes,
        share_quotes={"F": SHARE_QUOTE},
        settings=OptionSettings(**settings),
        today=SESSION,
    )


def covers(decision: CollateralDecision) -> list[tuple[int, str, int | None]]:
    return sorted((p.short_leg_no, p.cover, p.cover_ref) for p in decision.pairs)


SHARES_100 = structure(1, "shares", [(None, 100, "15")])
CALL_ON_SHARES = structure(2, "covered_call", [(C16, -1, "0.40")], cover=1)
SHORT_PUT = structure(3, "csp", [(P145, -1, "0.45")], reserved="1450")
CALL_CREDIT_SPREAD = structure(4, "credit_spread", [(C15, -1, "0.60"), (C16, 1, "0.20")], reserved="100")
PUT_CREDIT_SPREAD = structure(5, "credit_spread", [(P145, -1, "0.60"), (P13, 1, "0.20")], reserved="150")
BUY_WRITE = structure(6, "covered_call", [(None, 100, "15"), (C16, -1, "0.40")])


# --- the cover table ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legs", "net", "structures", "kind", "reserve", "pairs"),
    [
        ([buy(C15)], "-0.50", [], "long_call", "0", []),
        ([sell(P145)], "0.45", [], "csp", "1450", [(1, "cash", None)]),
        ([sell(C15)], "0.45", [SHARES_100], "covered_call", "0", [(1, "shares", 1)]),
        ([buy(C15, 1), sell(C16, 2)], "-0.40", [], "debit_spread", "0", [(2, "long_leg", 1)]),
        ([sell(P145, 1), buy(P13, 2)], "0.30", [], "credit_spread", "150", [(1, "long_leg", 2)]),
        (
            [sell(P13, 1), buy(P12, 2), sell(C15, 3), buy(C16, 4)],
            "0.40",
            [],
            "iron_condor",
            "100",
            [(1, "long_leg", 2), (3, "long_leg", 4)],
        ),
        ([buy(C16_DEC, 1), sell(C15, 2)], "-0.10", [], "diagonal", "100", [(2, "long_leg", 1)]),
    ],
    ids=["long", "cash_secured_put", "covered_call", "debit_spread", "credit_spread", "condor", "diagonal"],
)
def test_fp_table_rows(
    legs: list[OrderLeg],
    net: str,
    structures: list[StructureView],
    kind: str,
    reserve: str,
    pairs: list[tuple[int, str, int | None]],
) -> None:
    decision = ENGINE.evaluate(order(legs, net), book(structures=structures))

    assert decision.accepted, decision.detail
    assert (decision.kind, decision.reserve_cash) == (kind, D(reserve))
    assert covers(decision) == pairs
    assert decision.cover_structure_id == (1 if kind == "covered_call" else None)


@pytest.mark.parametrize(
    ("legs", "structures", "orders"),
    [
        ([sell(C15)], [], []),
        ([sell(C15)], [SHARES_100, CALL_ON_SHARES], []),
        ([sell(C15)], [SHARES_100], [working([sell(C16)])]),
        ([sell(C15)], [structure(1, "shares", [(None, 100, "15")], source="wheel")], []),
        ([sell(C15)], [structure(1, "shares", [(None, 99, "15")])], []),
        ([sell(C15, 1), buy(P145, 2)], [], []),
        ([sell(C15, 1), buy(C15_OCT, 2)], [], []),
        ([sell(C15, 1, ratio=2), buy(C16, 2)], [], []),
        ([shares("sell")], [], []),
    ],
    ids=[
        "no_shares",
        "shares_cover_another_call",
        "shares_cover_a_working_call",
        "shares_of_another_source",
        "too_few_shares",
        "long_leg_of_the_other_right",
        "long_expires_first",
        "more_short_than_long",
        "short_shares",
    ],
)
def test_naked_rejections(
    legs: list[OrderLeg], structures: list[StructureView], orders: list[OptOrderView]
) -> None:
    decision = ENGINE.evaluate(order(legs), book(structures=structures, orders=orders))

    assert (decision.accepted, decision.reject_reason) == (False, "naked_short")
    assert decision.detail


def test_one_order_takes_its_share_cover_from_one_structure_and_no_need_is_dropped() -> None:
    two_calls = order([sell(C15, 1), sell(C16, 2)], "0.80")
    one_lot = ENGINE.evaluate(two_calls, book(structures=[structure(1, "shares", [(None, 200, "15")])]))

    assert one_lot.accepted, one_lot.detail
    assert (covers(one_lot), one_lot.cover_structure_id) == ([(1, "shares", 1), (2, "shares", 1)], 1)

    # calls that need more than their linked lot holds: the rest is counted against the source's other lot
    lots = [SHARES_100, structure(9, "shares", [(None, 100, "15")])]
    calls = structure(2, "custom", [(C15, -1, "0.45"), (C16, -1, "0.40")], cover=1)
    sold = ENGINE.evaluate(
        close([shares("sell", effect="close")], 9, "14.90"), book(structures=[*lots, calls])
    )
    again = ENGINE.evaluate(order([sell(C15_DEC)]), book(structures=[*lots, calls]))

    assert (sold.reject_reason, again.reject_reason) == ("shares_committed", "naked_short")


# --- cash and the cap ---------------------------------------------------------------------------------------

OTHER_SOURCE_PUT = structure(
    7, "csp", [(P_OTHER, -1, "0.50")], source="wheel", underlying="T", reserved="2000"
)


@pytest.mark.parametrize(
    ("legs", "net", "cash", "extra", "free_after"),
    [
        # a put: 1,450 reserved, 45 received, 0.99 fees
        ([sell(P145)], "0.45", "1405.99", {}, "0"),
        ([sell(P145)], "0.45", "1405.98", {}, "-0.01"),
        # a put credit spread: 150 reserved, 30 received, 1.98 fees
        ([sell(P145, 1), buy(P13, 2)], "0.30", "121.98", {}, "0"),
        ([sell(P145, 1), buy(P13, 2)], "0.30", "121.97", {}, "-0.01"),
        # what a working order and another source's structure have reserved is not there to use
        ([sell(P145)], "0.45", "1415.99", {"orders": [working([buy(C15)], reserved="10")]}, "0"),
        ([sell(P145)], "0.45", "1415.98", {"orders": [working([buy(C15)], reserved="10")]}, "-0.01"),
        ([sell(P145)], "0.45", "3405.99", {"structures": [OTHER_SOURCE_PUT]}, "0"),
        ([sell(P145)], "0.45", "3405.98", {"structures": [OTHER_SOURCE_PUT]}, "-0.01"),
        # a market order pays the ask (0.50), not the bid
        ([buy(C15)], None, "50.99", {}, "0"),
        ([buy(C15)], None, "50.98", {}, "-0.01"),
    ],
)
def test_cash_rules(
    legs: list[OrderLeg], net: str | None, cash: str, extra: dict[str, Any], free_after: str
) -> None:
    decision = ENGINE.evaluate(order(legs, net), book(cash=cash, equity="50000", **extra))

    assert decision.free_cash_after == D(free_after)
    assert decision.accepted is (D(free_after) >= 0)
    assert decision.reject_reason == (None if decision.accepted else "insufficient_cash")
    if net is None:
        assert decision.net_at_market == D("-0.50")
        assert decision.cash_after == D(cash) - D("50.99")


@pytest.mark.parametrize(
    ("req", "structures", "accepted", "exposure_after"),
    [
        # the cap is 2,500; a new put on 14.50 adds 1,450
        (order([sell(P145)]), [structure(3, "csp", [(P13, -1, "0.30")], reserved="1050")], True, "2500"),
        (
            order([sell(P145)]),
            [structure(3, "csp", [(P13, -1, "0.30")], reserved="1050.01")],
            False,
            "2500.01",
        ),
        # another underlying's exposure does not count
        (order([sell(P145)]), [OTHER_SOURCE_PUT], True, "1450"),
        # shares that came from an assignment count at what they cost
        (order([sell(P13)]), [structure(1, "shares", [(None, 100, "14.50")])], False, "2750"),
        # a long option counts at its debit
        (
            order([buy(C15)], "-0.50"),
            [structure(3, "csp", [(P13, -1, "0.30")], reserved="2450")],
            True,
            "2500",
        ),
        (
            order([buy(C15)], "-0.51"),
            [structure(3, "csp", [(P13, -1, "0.30")], reserved="2450")],
            False,
            "2501",
        ),
        # an order that does not raise the exposure passes over the cap: a close, and a covered call
        (
            order([buy(P145, effect="close")], "-0.50", intent="close", structure_id=3),
            [SHORT_PUT, structure(8, "csp", [(P13, -2, "0.30")], reserved="2600")],
            True,
            "2600",
        ),
        (order([sell(C15, ratio=2)]), [structure(1, "shares", [(None, 200, "14.50")])], True, "2900"),
    ],
    ids=[
        "at_cap",
        "cent_over",
        "other_underlying",
        "assigned_shares",
        "debit_at",
        "debit_over",
        "close",
        "call",
    ],
)
def test_cap(req: OrderRequest, structures: list[StructureView], accepted: bool, exposure_after: str) -> None:
    decision = ENGINE.evaluate(req, book(cash="50000", structures=structures))

    assert decision.cap_limit == D("2500")
    assert decision.exposure_after == D(exposure_after)
    assert (decision.accepted, decision.reject_reason) == (accepted, None if accepted else "position_cap")


def test_working_opening_orders_count_toward_the_cap() -> None:
    pending = working([sell(P13)], reserved="1300")

    decision = ENGINE.evaluate(order([sell(P145)]), book(cash="50000", orders=[pending]))

    assert (decision.reject_reason, decision.exposure_after) == ("position_cap", D("2750"))


# --- closes and rolls ---------------------------------------------------------------------------------------


def close(legs: Sequence[OrderLeg], structure_id: int, net: str = "-0.10", **kwargs: Any) -> OrderRequest:
    return order(legs, net, intent="close", structure_id=structure_id, **kwargs)


@pytest.mark.parametrize(
    ("req", "structures", "orders", "reason"),
    [
        (close([buy(P145, effect="close")], 3, qty=2), [SHORT_PUT], [], "nothing_to_close"),
        (close([sell(P145, effect="close")], 3), [SHORT_PUT], [], "nothing_to_close"),
        (close([buy(P13, effect="close")], 3), [SHORT_PUT], [], "nothing_to_close"),
        (
            close([buy(P145, effect="close")], 3),
            [SHORT_PUT],
            [working([buy(P145, effect="close")], intent="close", structure_id=3)],
            "nothing_to_close",
        ),
        (close([sell(C16, effect="close")], 4), [CALL_CREDIT_SPREAD], [], "not_covered_after_close"),
        (
            close([shares("sell", effect="close")], 1, "14.90"),
            [SHARES_100, CALL_ON_SHARES],
            [],
            "shares_committed",
        ),
        (
            close([shares("sell", effect="close")], 1, "14.90"),
            [SHARES_100],
            [working([sell(C16)])],
            "shares_committed",
        ),
        (close([shares("sell", effect="close")], 6, "14.90"), [BUY_WRITE], [], "shares_committed"),
        (
            close([buy(P145, effect="close")], 3),
            [structure(3, "csp", [(P145, -1, "0.45")], reserved="1450", frozen=True)],
            [],
            "structure_frozen",
        ),
        (
            close([buy(P145, effect="close")], 3),
            [structure(3, "csp", [(P145, -1, "0.45")], reserved="1450", source="wheel")],
            [],
            "invalid_order",
        ),
        (close([buy(P145, effect="close")], 99), [SHORT_PUT], [], "invalid_order"),
    ],
    ids=[
        "more_than_held",
        "wrong_side",
        "contract_not_held",
        "a_working_close_already_has_it",
        "long_leg_alone_leaves_the_call_naked",
        "shares_cover_a_call",
        "shares_cover_a_working_call",
        "shares_of_a_buy_write",
        "frozen",
        "another_source",
        "no_such_structure",
    ],
)
def test_close_rules(
    req: OrderRequest, structures: list[StructureView], orders: list[OptOrderView], reason: str
) -> None:
    decision = ENGINE.evaluate(req, book(structures=structures, orders=orders))

    assert (decision.accepted, decision.reject_reason) == (False, reason)


@pytest.mark.parametrize(
    ("req", "structures", "reserve", "free_after"),
    [
        # the whole spread: the 100 reserved comes back, 10 and 1.98 fees are paid
        (
            close([buy(C15, 1, "close"), sell(C16, 2, "close")], 4),
            [CALL_CREDIT_SPREAD],
            "0",
            "4988.02",
        ),
        # the short leg alone: what remains is a long call
        (close([buy(C15, effect="close")], 4), [CALL_CREDIT_SPREAD], "0", "4989.01"),
        # the long leg of a put spread: what remains is a put, now secured by cash
        (close([sell(P13, effect="close")], 5, "0.10"), [PUT_CREDIT_SPREAD], "1450", "3559.01"),
        # the call first, then the shares are free to sell
        (close([buy(C16, effect="close")], 6), [BUY_WRITE], "0", "4989.01"),
        (close([shares("sell", 1, "close"), buy(C16, 2, "close")], 6, "14.50"), [BUY_WRITE], "0", "6449.01"),
        (close([shares("sell", effect="close")], 1, "14.90"), [SHARES_100], "0", "6490"),
    ],
    ids=["whole_spread", "short_leg", "long_put_leg", "call_of_buy_write", "whole_buy_write", "free_shares"],
)
def test_closes_that_leave_everything_covered(
    req: OrderRequest, structures: list[StructureView], reserve: str, free_after: str
) -> None:
    decision = ENGINE.evaluate(req, book(structures=structures))

    assert decision.accepted, decision.detail
    assert decision.reserve_cash == D(reserve)
    assert decision.free_cash_after == D(free_after)
    assert decision.free_cash_after == decision.cash_after - D(reserve)


def test_partial_close_that_needs_cash_it_does_not_have() -> None:
    req = close([sell(P13, effect="close")], 5, "0.10")

    decision = ENGINE.evaluate(req, book(cash="1000", structures=[PUT_CREDIT_SPREAD]))

    assert (decision.reject_reason, decision.reserve_cash) == ("insufficient_cash", D("1450"))


@pytest.mark.parametrize(
    ("req", "structures", "accepted", "reserve", "cover_structure_id"),
    [
        # a covered call rolled out: the same shares cover the new call
        (
            order([buy(C16, 1, "close"), sell(C16_DEC, 2)], "0.20", intent="roll", structure_id=2),
            [SHARES_100, CALL_ON_SHARES],
            True,
            "0",
            1,
        ),
        # a put rolled down: the reserve follows the new strike
        (
            order([buy(P145, 1, "close"), sell(P13, 2)], "-0.20", intent="roll", structure_id=3),
            [SHORT_PUT],
            True,
            "1300",
            None,
        ),
        # the short call of a spread rolled past its long leg
        (
            order([buy(C15, 1, "close"), sell(C15_DEC, 2)], "0.20", intent="roll", structure_id=4),
            [CALL_CREDIT_SPREAD],
            False,
            "0",
            None,
        ),
    ],
    ids=["covered_call", "put", "past_the_long_leg"],
)
def test_rolls(
    req: OrderRequest,
    structures: list[StructureView],
    accepted: bool,
    reserve: str,
    cover_structure_id: int | None,
) -> None:
    decision = ENGINE.evaluate(req, book(structures=structures))

    assert (decision.accepted, decision.reject_reason) == (accepted, None if accepted else "naked_short")
    assert (decision.reserve_cash, decision.cover_structure_id) == (D(reserve), cover_structure_id)


# --- malformed orders and the other rejections --------------------------------------------------------------


@pytest.mark.parametrize(
    ("req", "settings"),
    [
        (order([]), {}),
        (order([buy(C15, 1), buy(C16, 2), buy(C15_DEC, 3)]), {"max_legs": 2}),
        (order([sell(P145)], qty=0), {}),
        (order([sell(P145)], qty=11), {}),
        (order([sell(P145)], qty=3), {"max_contracts_per_order": 2}),
        (order([make_leg(1, underlying="T", contract_id=P_OTHER)]), {}),
        (order([sell(P_OTHER)]), {}),
        (order([buy(C15, 1), buy(C15, 2)]), {}),
        (order([buy(C15, 1), buy(C16, 1)]), {}),
        (make_request([sell(P145)], net_limit=None), {}),
        (order([sell(P145)], None), {"allow_market_orders": False}),
        (order([sell(P145, ratio=0)]), {}),
        (order([make_leg(1, instrument="option", contract_id=None)]), {}),
        (order([buy(P145, effect="close")]), {}),
        (order([sell(P145)], intent="close", structure_id=3), {}),
        (order([sell(P145)], intent="roll", structure_id=3), {}),
        (order([buy(P145, effect="close")], intent="close"), {}),
    ],
    ids=[
        "no_legs",
        "too_many_legs",
        "zero_quantity",
        "quantity_over_the_default_limit",
        "quantity_over_the_set_limit",
        "leg_on_another_underlying",
        "contract_of_another_underlying",
        "repeated_leg",
        "repeated_leg_number",
        "limit_without_a_price_or_walk",
        "market_orders_off",
        "zero_ratio",
        "option_leg_without_a_contract",
        "closing_leg_in_an_opening_order",
        "opening_leg_in_a_closing_order",
        "roll_without_a_closing_leg",
        "close_without_a_structure",
    ],
)
def test_invalid_orders(req: OrderRequest, settings: dict[str, Any]) -> None:
    decision = ENGINE.evaluate(req, book(structures=[SHORT_PUT], **settings))

    assert (decision.accepted, decision.reject_reason) == (False, "invalid_order")
    assert decision.detail
    assert (decision.cash_after, decision.free_cash_after) == (D("5000"), D("3550"))


def test_a_walk_order_without_a_limit_is_priced_at_the_market() -> None:
    decision = ENGINE.evaluate(make_request([sell(P145)], net_limit=None, walk=True), book(cash="1405.99"))

    assert (decision.accepted, decision.net_at_market, decision.free_cash_after) == (True, D("0.45"), ZERO)


def test_roll_that_opens_against_a_held_position_is_invalid() -> None:
    req = order([buy(C15, 1, "close"), sell(C16, 2)], "0.10", intent="roll", structure_id=4)

    decision = ENGINE.evaluate(req, book(structures=[CALL_CREDIT_SPREAD]))

    assert decision.reject_reason == "invalid_order"


@pytest.mark.parametrize(
    ("req", "extra", "reason"),
    [
        (order([sell(P145)], source="wheel"), {"strategies_paused": True}, "strategies_paused"),
        (order([sell(404)]), {}, "unknown_contract"),
        (order([sell(P_EXPIRED)]), {}, "expired_contract"),
        (order([buy(C15)], None), {"quotes": {C15: ("0.45", None)}}, "no_quote"),
        (order([shares("buy", 1), sell(C15, 2)], None), {"quotes": {C15: (None, "0.50")}}, "no_quote"),
    ],
    ids=["paused", "unknown_contract", "expired_contract", "no_ask", "no_bid"],
)
def test_other_rejections(req: OrderRequest, extra: dict[str, Any], reason: str) -> None:
    decision = ENGINE.evaluate(req, book(**extra))

    assert (decision.accepted, decision.reject_reason) == (False, reason)


def test_manual_orders_pass_while_strategies_are_paused_and_limits_need_no_quote() -> None:
    quiet = book(strategies_paused=True, quotes={P145: (None, None)})

    decision = ENGINE.evaluate(order([sell(P145)]), quiet)

    assert (decision.accepted, decision.net_at_market) == (True, None)


# --- the numbers the ticket shows ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legs", "net", "max_loss", "max_profit", "breakeven", "fees"),
    [
        ([sell(P145)], "0.45", "1405", "45", "14.05", "0.99"),
        ([buy(C15)], "-0.50", "50", None, "15.50", "0.99"),
        ([buy(P145)], "-0.50", "50", "1400", "14.00", "0.99"),
        ([buy(C15, 1), sell(C16, 2)], "-0.40", "40", "60", "15.40", "1.98"),
        ([sell(C15, 1), buy(C16, 2)], "0.30", "70", "30", "15.30", "1.98"),
        ([buy(P145, 1), sell(P13, 2)], "-0.40", "40", "110", "14.10", "1.98"),
        ([sell(P145, 1), buy(P13, 2)], "0.30", "120", "30", "14.20", "1.98"),
        ([shares("buy", 1), sell(C16, 2)], "-14.60", "1460", "140", "14.60", "0.99"),
    ],
    ids=["csp", "long_call", "long_put", "call_debit", "call_credit", "put_debit", "put_credit", "buy_write"],
)
def test_preview_numbers(
    legs: list[OrderLeg], net: str, max_loss: str, max_profit: str | None, breakeven: str, fees: str
) -> None:
    decision = ENGINE.evaluate(order(legs, net), book())

    assert decision.accepted, decision.detail
    assert decision.max_loss == D(max_loss)
    assert decision.max_profit == (None if max_profit is None else D(max_profit))
    assert decision.breakevens == (D(breakeven),)
    assert decision.fees == D(fees)


def test_preview_numbers_scale_with_quantity_and_use_the_cost_of_covering_shares() -> None:
    puts = ENGINE.evaluate(order([sell(P13)], "0.30", qty=2), book(cash="50000", equity="50000"))
    call = ENGINE.evaluate(order([sell(C16)], "0.40"), book(structures=[SHARES_100]))
    condor = ENGINE.evaluate(order([sell(P13, 1), buy(P12, 2), sell(C15, 3), buy(C16, 4)]), book())

    assert (puts.max_loss, puts.max_profit, puts.breakevens) == (D("2540"), D("60"), (D("12.70"),))
    assert (puts.reserve_cash, puts.fees) == (D("2600"), D("1.98"))
    assert (call.max_loss, call.max_profit, call.breakevens) == (D("1460"), D("140"), (D("14.60"),))
    assert (condor.max_loss, condor.max_profit, condor.breakevens) == (None, None, ())


@pytest.mark.parametrize(
    ("legs", "qty", "kind", "reserve"),
    [
        # the wider side of a condor: puts 14.50 over 12 (2.50 wide), calls 15 under 16 (1 wide)
        ([sell(P145, 1), buy(P12, 2), sell(C15, 3), buy(C16, 4)], 1, "iron_condor", "250"),
        ([sell(P145, 1), buy(P12, 2), sell(C15, 3), buy(C16, 4)], 2, "iron_condor", "500"),
        ([buy(C15_DEC, 1), sell(C15, 2)], 1, "calendar", "0"),
        ([buy(C14_DEC, 1), sell(C15, 2)], 1, "diagonal", "0"),
        ([buy(C16_DEC, 1), sell(C15, 2)], 3, "diagonal", "300"),
        # two expiries are added up: the November put spread's long leg is gone before December
        ([sell(P145, 1), buy(P13, 2), sell(C15_DEC, 3), buy(C16_DEC, 4)], 1, "custom", "250"),
    ],
)
def test_iron_condor_and_diagonal_reserve(legs: list[OrderLeg], qty: int, kind: str, reserve: str) -> None:
    decision = ENGINE.evaluate(order(legs, "0.10", qty=qty), book())

    assert decision.accepted, decision.detail
    assert (decision.kind, decision.reserve_cash) == (kind, D(reserve))


@pytest.mark.parametrize(
    ("legs", "net", "kind"),
    [
        ([buy(C15)], "-0.50", "long_call"),
        ([buy(P145)], "-0.50", "long_put"),
        ([sell(P145)], "0.45", "csp"),
        ([shares("buy")], "-15.00", "shares"),
        ([shares("buy", 1), sell(C15, 2)], "-14.60", "covered_call"),
        ([buy(C15, 1), sell(C16, 2)], "-0.40", "debit_spread"),
        ([sell(C15, 1), buy(C16, 2)], "0.30", "credit_spread"),
        ([buy(P145, 1), sell(P13, 2)], "-0.40", "debit_spread"),
        ([sell(P145, 1), buy(P13, 2)], "0.30", "credit_spread"),
        ([sell(P13, 1), buy(P12, 2), sell(C15, 3), buy(C16, 4)], "0.40", "iron_condor"),
        ([buy(C15_DEC, 1), sell(C15, 2)], "-0.20", "calendar"),
        ([buy(C14_DEC, 1), sell(C15, 2)], "-0.90", "diagonal"),
        ([buy(C15, 1), buy(P145, 2)], "-1.00", "custom"),
        ([buy(C15, 1, ratio=2), sell(C16, 2)], "-0.50", "custom"),
        ([shares("buy", 1), buy(P145, 2)], "-15.50", "custom"),
    ],
)
def test_kind_classification(legs: list[OrderLeg], net: str, kind: str) -> None:
    decision = ENGINE.evaluate(order(legs, net), book())

    assert decision.accepted, decision.detail
    assert decision.kind == kind


def test_a_buy_write_is_covered_by_its_own_shares_and_a_close_keeps_the_structure_kind() -> None:
    buy_write = ENGINE.evaluate(order([shares("buy", 1), sell(C15, 2)], "-14.60"), book())
    closing = ENGINE.evaluate(close([buy(P145, effect="close")], 3), book(structures=[SHORT_PUT]))

    assert (covers(buy_write), buy_write.reserve_cash, buy_write.cover_structure_id) == (
        [(2, "shares", None)],
        ZERO,
        None,
    )
    assert (closing.kind, closing.pairs, closing.max_loss) == ("csp", (), None)


# --- purity -------------------------------------------------------------------------------------------------


def test_same_input_same_output_and_no_io() -> None:
    snapshot = book(
        equity="50000", structures=[SHARES_100, CALL_ON_SHARES, SHORT_PUT], orders=[working([buy(C15)])]
    )
    requests = [order([sell(P13)]), order([sell(C15)]), close([buy(P145, effect="close")], 3)]

    first = [ENGINE.evaluate(req, snapshot) for req in requests]
    second = [DefaultCollateralEngine().evaluate(req, snapshot) for req in requests]

    assert first == second
    assert [d.reject_reason for d in first] == [None, "naked_short", None]

    tree = ast.parse(Path(collateral_module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = ("trader.db", "trader.adapters", "httpx", "sqlalchemy")
    assert not [name for name in imported if name.startswith(banned)]
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"open", "datetime", "Clock", "time"}
