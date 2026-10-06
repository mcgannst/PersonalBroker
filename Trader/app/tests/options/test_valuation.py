"""OPTSIM T5: account valuation. Liquidation marks (a long option at the bid, a short at the ask, shares at
the last trade); a position without a live quote falls back to its last mark and the account says so."""

from decimal import Decimal

from tests.options import factories as f
from trader.adapters.questrade.models import QtQuote
from trader.options import valuation
from trader.options.types import OptionContract, OptPositionView, StructureKind, StructureView

D = Decimal
PUT = f.make_contract(1, strike="14.50", right="put")
CALL = f.make_contract(2, strike="15.00", right="call")
LOW_PUT = f.make_contract(3, strike="13.50", right="put")


def structure(
    sid: int, kind: StructureKind, *positions: tuple[OptionContract | None, int, str]
) -> StructureView:
    return StructureView(
        id=sid,
        source="manual",
        strategy_config_id=None,
        kind=kind,
        underlying="F",
        state="open",
        close_reason=None,
        frozen=False,
        qty=1,
        entry_net=D("0.45"),
        reserved_cash=D(0),
        take_profit_net=None,
        cover_structure_id=None,
        parent_structure_id=None,
        realized_pnl=D(0),
        fees_total=D(0),
        opened_at=f.T0,
        closed_at=None,
        positions=tuple(
            OptPositionView(
                id=sid * 10 + n,
                structure_id=sid,
                instrument="shares" if contract is None else "option",
                contract=contract,
                underlying="F",
                qty=qty,
                avg_price=D(avg),
                realized_pnl=D(0),
            )
            for n, (contract, qty, avg) in enumerate(positions)
        ),
        meta={},
    )


def share_quote(last: str) -> QtQuote:
    return QtQuote(
        1, "F", D(last) - D("0.01"), D(last) + D("0.01"), D(last), D(last), 0, f.T0, 0, False, None
    )


BOOK = [
    structure(1, "long_call", (CALL, 2, "1.20")),
    structure(2, "csp", (PUT, -1, "0.45")),
    structure(3, "shares", (None, 100, "14.50")),
    structure(4, "csp", (LOW_PUT, 0, "0.10")),  # a closed position counts for nothing
]


def test_account_value_uses_liquidation_marks() -> None:
    quotes = {1: f.make_quote(1, "0.40", "0.50"), 2: f.make_quote(2, "1.00", "1.10")}
    state = valuation.account_state(
        D("1000"), D("300"), BOOK, quotes, {"F": share_quote("15.00")}, {}, f.T0, premium_collected=D("45")
    )
    # two long calls at the 1.00 bid, one short put at the 0.50 ask, 100 shares at the 15.00 last
    assert state.positions_value == D("200") - D("50") + D("1500")
    assert (state.cash, state.reserved, state.free_cash) == (D("1000"), D("300"), D("700"))
    assert (state.equity, state.premium_collected) == (D("2650"), D("45"))
    assert (state.as_of, state.marks_complete) == (f.T0, True)


def test_missing_quote_uses_the_last_mark() -> None:
    live = {2: f.make_quote(2, "1.00", "1.10")}
    marks = {1: f.make_quote(1, "0.30", "0.35")}
    shares = {"F": share_quote("15.00")}
    state = valuation.account_state(D("1000"), D("0"), BOOK, live, shares, marks, f.T0)
    assert state.positions_value == D("200") - D("35") + D("1500")  # the put at its last mark's ask
    assert state.marks_complete is False
    # No mark either, and no share quote: each is carried at its cost, and the account is still incomplete.
    bare = valuation.account_state(D("1000"), D("0"), BOOK, live, {}, {}, f.T0)
    assert bare.positions_value == D("200") - D("45") + D("1450")
    assert bare.marks_complete is False


def test_close_net_is_per_share_of_one_unit_and_credit_positive() -> None:
    quotes = {1: f.make_quote(1, "0.15", "0.20"), 3: f.make_quote(3, "0.05", "0.10")}
    assert valuation.close_net(structure(1, "csp", (PUT, -2, "0.45")), quotes) == D("-0.20")
    spread = structure(2, "credit_spread", (PUT, -2, "0.45"), (LOW_PUT, 2, "0.20"))
    assert valuation.close_net(spread, quotes) == D("-0.15")  # buy at 0.20, sell at 0.05
    assert valuation.structure_units(spread) == 2
    assert valuation.close_net(spread, {1: quotes[1]}) is None  # a leg without a quote
    assert valuation.close_net(structure(3, "shares", (None, 100, "14.50")), quotes) is None
    assert valuation.close_net(structure(4, "csp", (PUT, 0, "0.45")), quotes) is None


def test_a_zero_quote_is_no_mark_for_a_short_and_no_close_net() -> None:
    dead = {1: f.make_quote(1, "0", "0"), 3: f.make_quote(3, "0", "0.05")}
    short_put = structure(1, "csp", (PUT, -1, "0.45"))
    assert valuation.close_net(short_put, dead) is None  # nothing to buy it back from
    # The short is carried at its last mark (here its cost), never at 0, and the account says so.
    state = valuation.account_state(D("1000"), D("0"), [short_put], dead, {}, {}, f.T0)
    assert (state.positions_value, state.marks_complete) == (D("-45"), False)
    # A long with a bid of 0 is worth nothing (a complete mark), but a close would have nobody to sell to.
    long_put = structure(2, "long_put", (LOW_PUT, 1, "0.10"))
    worthless = valuation.account_state(D("1000"), D("0"), [long_put], dead, {}, {}, f.T0)
    assert (worthless.positions_value, worthless.marks_complete) == (D("0"), True)
    assert valuation.close_net(long_put, dead) is None
