"""Account valuation for the options run (OPTSIM task plan T5; wheel rules spec §11 `account_value`). Pure.

Liquidation marks: a long option is worth its bid, a short option costs its ask, shares are worth the last
trade. A position without a usable live quote (for a short, also an ask of 0 or less) falls back to the last
recorded mark of its contract (from `option_quote_marks`), and without one to its cost; either way
`marks_complete` is False.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from math import gcd

from trader.adapters.questrade.models import QtQuote
from trader.options.types import (
    HUNDRED,
    ZERO,
    OptionAccountState,
    OptionQuote,
    OptPositionView,
    StructureView,
)


def liquidation_price(position: OptPositionView, quote: OptionQuote | None) -> Decimal | None:
    """What one contract of an option position is worth if closed now: the bid for a long, the ask for a
    short. None without that side of the quote; an ask of 0 or less is no offer, so a short has no mark
    then (a long at a bid of 0 is simply worth nothing)."""
    if quote is None:
        return None
    if position.qty > 0:
        return quote.bid
    return quote.ask if quote.ask is not None and quote.ask > 0 else None


def structure_units(structure: StructureView) -> int:
    """How many units of the structure are still open: the greatest common divisor of its open positions'
    quantities (0 when nothing is open)."""
    units = 0
    for p in structure.positions:
        units = gcd(units, abs(p.qty))
    return units


def close_net(structure: StructureView, quotes: Mapping[int, OptionQuote]) -> Decimal | None:
    """The net per share of closing one unit of the structure at the market, credit positive (so a short put
    quoted 0.20 at the ask gives -0.20). `quotes` is keyed by contract id. None when nothing is open, when
    the structure holds shares, or when an open position has no positive price on the side that closes it
    (the ask to buy a short back, the bid to sell a long): a close could not fill there."""
    units = structure_units(structure)
    if units == 0:
        return None
    total = ZERO
    for p in structure.positions:
        if p.qty == 0:
            continue
        if p.contract is None:
            return None
        price = liquidation_price(p, quotes.get(p.contract.id))
        if price is None or price <= 0:
            return None
        total += price * p.qty * p.contract.multiplier  # a short's qty is negative: closing it costs
    return total / (HUNDRED * units)


def account_state(
    cash: Decimal,
    reserved: Decimal,
    structures: Sequence[StructureView],
    quotes: Mapping[int, OptionQuote],
    share_quotes: Mapping[str, QtQuote],
    marks: Mapping[int, OptionQuote],
    now: datetime,
    *,
    premium_collected: Decimal = ZERO,
) -> OptionAccountState:
    """The account at `now`. `structures` are the open ones; `quotes` are live option quotes and `marks` the
    last recorded ones, both by contract id; `share_quotes` is by ticker."""
    value, complete = ZERO, True
    for s in structures:
        for p in s.positions:
            if p.qty == 0:
                continue
            if p.contract is None:
                share = share_quotes.get(s.underlying)
                mark, multiplier = (None if share is None else share.last), 1
            else:
                mark = liquidation_price(p, quotes.get(p.contract.id))
                multiplier = p.contract.multiplier
                if mark is None:
                    complete = False
                    mark = liquidation_price(p, marks.get(p.contract.id))
            if mark is None:
                complete = False
                mark = p.avg_price
            value += mark * p.qty * multiplier
    return OptionAccountState(
        cash=cash,
        reserved=reserved,
        free_cash=cash - reserved,
        positions_value=value,
        equity=cash + value,
        premium_collected=premium_collected,
        as_of=now,
        marks_complete=complete,
    )
