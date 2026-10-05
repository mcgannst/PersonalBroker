"""The option fill model (OPTSIM task plan T5, feature plan §3.4, decision D11). Sync and pure: no I/O, no
clock; the broker hands it the order, one quote per leg and the session's hours.

Conservative prices: a buy fills at the ask, a sell at the bid, shares legs the same from the share quote.
All legs fill together or none do. A market order fills on the first usable set of quotes; a limit order
fills when the market net is at or better than its limit, at the market net (never better than the market
gave).

No fill, in this order (the first that applies is the reason): `outside_hours`; then, each over every leg,
`quote_missing`, `quote_delayed` (a `delay` of None counts as delayed), `halted`, `quote_stale`, `one_sided`,
`crossed`, `zero_bid` (a sell leg's bid is 0); last `limit_not_reached`.

An option leg's contract multiplier is read from `LegQuote.raw["multiplier"]` (100 when absent): the order
view carries contract ids only, and the broker that builds the leg quotes knows the contracts.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from trader.options.settings import OptionSettings
from trader.options.types import (
    SHARES_PER_CONTRACT,
    ZERO,
    FillDecision,
    LegQuote,
    NoFill,
    NoFillReason,
    OptOrderView,
    OrderLeg,
    net_price,
)

MULTIPLIER_KEY = "multiplier"  # in LegQuote.raw, for an option leg


def leg_fee(leg: OrderLeg, qty: int, settings: OptionSettings) -> Decimal:
    """`options.fee_per_contract` per contract for an option leg; `options.share_commission` for a shares
    leg, whatever its size."""
    if leg.instrument == "shares":
        return settings.share_commission
    return settings.fee_per_contract * leg.ratio * qty


def order_fees(legs: Sequence[OrderLeg], qty: int, settings: OptionSettings) -> Decimal:
    return sum((leg_fee(leg, qty, settings) for leg in legs), ZERO)


def leg_multipliers(legs: Sequence[OrderLeg], quotes: Mapping[int, LegQuote]) -> dict[int, int]:
    """Multiplier by `leg_no` for the option legs, from each leg quote's snapshot."""
    out: dict[int, int] = {}
    for leg in legs:
        quote = quotes.get(leg.leg_no)
        if leg.instrument == "option" and quote is not None:
            out[leg.leg_no] = int(quote.raw.get(MULTIPLIER_KEY, SHARES_PER_CONTRACT))
    return out


def market_prices(legs: Sequence[OrderLeg], quotes: Mapping[int, LegQuote]) -> dict[int, Decimal] | None:
    """Buy at the ask, sell at the bid, by `leg_no`; None when a leg has no such price."""
    prices: dict[int, Decimal] = {}
    for leg in legs:
        quote = quotes.get(leg.leg_no)
        price = None if quote is None else (quote.ask if leg.side == "buy" else quote.bid)
        if price is None:
            return None
        prices[leg.leg_no] = price
    return prices


def market_net(legs: Sequence[OrderLeg], quotes: Mapping[int, LegQuote]) -> Decimal | None:
    """The net per share of one unit at the market (credit positive); None without a full set of prices."""
    prices = market_prices(legs, quotes)
    return None if prices is None else net_price(legs, prices, leg_multipliers(legs, quotes))


def mid_net(legs: Sequence[OrderLeg], quotes: Mapping[int, LegQuote]) -> Decimal | None:
    """The net per share of one unit at each leg's midpoint; None when a leg lacks a bid or an ask."""
    prices: dict[int, Decimal] = {}
    for leg in legs:
        quote = quotes.get(leg.leg_no)
        if quote is None or quote.bid is None or quote.ask is None:
            return None
        prices[leg.leg_no] = (quote.bid + quote.ask) / 2
    return net_price(legs, prices, leg_multipliers(legs, quotes))


Check = Callable[[OrderLeg, LegQuote | None], bool]


class QuoteFillModel:
    """The `OptionFillModel` of the simulation."""

    def assess(
        self,
        order: OptOrderView,
        quotes: Mapping[int, LegQuote],
        now: datetime,
        session_open: datetime,
        session_close: datetime,
        settings: OptionSettings,
    ) -> FillDecision | NoFill:
        if not session_open <= now < session_close:
            return NoFill("outside_hours", f"{now.isoformat()} is outside the session")
        max_age = timedelta(seconds=settings.stale_quote_seconds)
        # Each check sees a quote that passed every check before it, so `q` is only None in the first.
        checks: tuple[tuple[NoFillReason, Check], ...] = (
            ("quote_missing", lambda leg, q: q is None),
            ("quote_delayed", lambda leg, q: q is not None and q.delay != 0),
            ("halted", lambda leg, q: q is not None and q.is_halted),
            ("quote_stale", lambda leg, q: q is not None and now - q.fetched_at > max_age),
            ("one_sided", lambda leg, q: q is not None and (q.bid is None or q.ask is None)),
            (
                "crossed",
                lambda leg, q: q is not None and q.bid is not None and q.ask is not None and q.bid > q.ask,
            ),
            ("zero_bid", lambda leg, q: q is not None and leg.side == "sell" and q.bid == 0),
        )
        legs = sorted(order.legs, key=lambda leg: leg.leg_no)
        for reason, failed in checks:
            for leg in legs:
                if failed(leg, quotes.get(leg.leg_no)):
                    return NoFill(reason, f"leg {leg.leg_no}")
        prices = market_prices(legs, quotes)
        if prices is None:  # unreachable after the checks; kept so a price is never invented
            return NoFill("one_sided", "a leg has no price on its side")
        net = net_price(legs, prices, leg_multipliers(legs, quotes))
        if order.order_type == "limit" and (order.net_limit is None or net < order.net_limit):
            return NoFill("limit_not_reached", f"market net {net} against limit {order.net_limit}")
        return FillDecision(
            net_price=net,
            leg_prices=tuple((leg.leg_no, prices[leg.leg_no]) for leg in legs),
            fees=order_fees(legs, order.qty, settings),
            trigger="market" if order.order_type == "market" else "limit",
        )
