"""The stop rules (WS §10). They override everything else; the daily evaluation calls the same predicates."""

from datetime import date
from decimal import Decimal

from trader.option_strategies.wheel.config import WheelParams
from trader.option_strategies.wheel.inputs import AccountInputs

POSITION_OPEN = "POSITION_OPEN"
NEW_POSITIONS_PAUSED = "NEW_POSITIONS_PAUSED"
INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
PER_TICKER_LIMIT = "PER_TICKER_LIMIT"


def can_open(
    ticker_has_position: bool, account: AccountInputs, collateral: Decimal, cfg: WheelParams
) -> str | None:
    """Why a new put is blocked, or None when it may be opened (WS §5 preconditions and §10)."""
    if ticker_has_position:
        return POSITION_OPEN
    if account.new_positions_paused:
        return NEW_POSITIONS_PAUSED
    if collateral + account.open_put_collateral_usd > account.cash_usd:
        return INSUFFICIENT_CASH
    limit = cfg.entry_per_ticker_limit_pct_of_wheel_cash
    if limit is not None and collateral > limit * account.wheel_cash_usd:
        return PER_TICKER_LIMIT
    return None


def can_roll(roll_count: int, cfg: WheelParams) -> bool:
    return roll_count < cfg.manage_max_rolls_per_put


def benchmark_review(benchmark_return: Decimal, account_return: Decimal, cfg: WheelParams) -> bool:
    """True = pause new positions: the benchmark's quarter was flat or falling and the wheel still trailed
    it. Trailing in a rising market does not trigger this."""
    return benchmark_return <= cfg.stops_flat_market_quarter_return_max and account_return < benchmark_return


def fresh_cash_due(answer_date: date | None, today: date, cfg: WheelParams) -> bool:
    """No recorded answer, or the last one is `stops_fresh_cash_retest_days` old."""
    return answer_date is None or (today - answer_date).days >= cfg.stops_fresh_cash_retest_days


def at_review_level(price: Decimal, net_cost: Decimal, cfg: WheelParams) -> bool:
    return price <= net_cost * (1 - cfg.stops_review_drawdown_from_net_cost)


def drawdown_review_due(
    price: Decimal, net_cost: Decimal, last_review_date: date | None, today: date, cfg: WheelParams
) -> bool:
    """Shares at or beyond the review level with no written review inside the review period."""
    if not at_review_level(price, net_cost, cfg):
        return False
    return last_review_date is None or (today - last_review_date).days >= cfg.stops_drawdown_review_days
