"""The calculations of WS §11, one function per line. Money and ratios are Decimal; nothing is rounded."""

from decimal import Decimal

from trader.options.types import SHARES_PER_CONTRACT


def collateral(strike: Decimal, contracts: int) -> Decimal:
    return strike * SHARES_PER_CONTRACT * contracts


def breakeven(strike: Decimal, premium: Decimal) -> Decimal:
    return strike - premium


def cushion(price: Decimal, strike: Decimal) -> Decimal:
    return (price - strike) / price


def ror(bid: Decimal, strike: Decimal) -> Decimal:
    """Return on risk: the option bid over the strike."""
    return bid / strike


def ror_annualized(ror_value: Decimal, dte: int) -> Decimal:
    return ror_value * 365 / dte


def spread_pct(bid: Decimal, ask: Decimal) -> Decimal:
    return (ask - bid) / bid


def put_pl_per_share(premium: Decimal, current_ask: Decimal) -> Decimal:
    return premium - current_ask


def net_cost(
    assignment_strike: Decimal,
    total_put_premium: Decimal,
    total_call_premium: Decimal = Decimal(0),
    *,
    include_call_premium: bool = False,
) -> Decimal:
    """WS §9.1: the assignment strike less all put premium per share (roll credits included). Call premium
    lowers it only when `cc_include_call_premium_in_net_cost` is set."""
    cost = assignment_strike - total_put_premium
    return cost - total_call_premium if include_call_premium else cost


def drawdown_from_cost(net_cost_value: Decimal, price: Decimal) -> Decimal:
    return (net_cost_value - price) / net_cost_value


def full_cycle_result(
    *,
    total_put_premium: Decimal,
    total_call_premium: Decimal,
    dividends: Decimal,
    sale_price: Decimal,
    assignment_strike: Decimal,
    contracts: int,
    fees: Decimal,
) -> Decimal:
    per_share = total_put_premium + total_call_premium + dividends + sale_price - assignment_strike
    return per_share * SHARES_PER_CONTRACT * contracts - fees


def account_value(cash: Decimal, shares_at_market: Decimal, cost_to_close_shorts: Decimal) -> Decimal:
    """The headline number: unrealized losses included (short options are valued at the ask)."""
    return cash + shares_at_market - cost_to_close_shorts
