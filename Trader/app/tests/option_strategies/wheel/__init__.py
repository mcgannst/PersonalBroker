"""Builders for the wheel rule tests. The defaults describe a ticker that passes all ten tests: a large,
profitable stock in an uptrend with a liquid 45-day monthly chain, and an account with room for it."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from trader.option_strategies.wheel import evaluate, screen
from trader.option_strategies.wheel.config import WheelParams
from trader.option_strategies.wheel.inputs import (
    AccountInputs,
    CallPosition,
    OptionRow,
    OwnerInputs,
    PutPosition,
    SharesPosition,
)
from trader.options.types import ContractKey, ExpiryInfo, Right, UnderlyingFacts

TODAY = date(2026, 10, 6)
EXPIRY = date(2026, 11, 20)  # a monthly, 45 days out
CFG = WheelParams()
ROLL_CFG = CFG.model_copy(update={"manage_itm_at_time_exit_preference": "ROLL_ONCE"})


def D(value: Any) -> Decimal:
    return Decimal(str(value))


def _decimals(values: dict[str, Any]) -> dict[str, Any]:
    return {k: D(v) if isinstance(v, int | float | str) and k not in _TEXT else v for k, v in values.items()}


_TEXT = {"ticker", "security_type", "sector", "sessions_since_52w_low", "symbol_id", "has_options"}


def facts(**changes: Any) -> UnderlyingFacts:
    values: dict[str, Any] = {
        "symbol_id": 1,
        "as_of": TODAY,
        "ticker": "F",
        "security_type": "STOCK",
        "sector": "Technology",
        "price": 55,
        "eps_ttm": 3,
        "eps_growth_yoy": "0.10",
        "debt_to_equity": "0.5",
        "book_value_per_share": 20,
        "market_cap_usd": 50_000_000_000,
        "sma50": 52,
        "sma50_prior": 51,
        "low_52w": 40,
        "sessions_since_52w_low": 150,
        "rsi14": 55,
        "next_earnings_date": date(2026, 12, 10),
    }
    values.update(changes)
    return UnderlyingFacts(**_decimals(values))


def owner(**changes: Any) -> OwnerInputs:
    values: dict[str, Any] = {"would_own": True, "ownership_reason": "a business I want to hold"}
    values.update(changes)
    return OwnerInputs(**values)


def account(
    cash: Any = 20_000, *, collateral: Any = 0, wheel_cash: Any = None, paused: bool = False
) -> AccountInputs:
    return AccountInputs(D(cash), D(cash if wheel_cash is None else wheel_cash), D(collateral), paused)


def row(
    strike: Any,
    delta: Any,
    bid: Any,
    *,
    ask: Any = "auto",
    iv: Any = "0.30",
    oi: int | None = 1000,
    right: Right = "put",
    expiry: date = EXPIRY,
    monthly: bool = True,
    dte: int | None = None,
) -> OptionRow:
    """`delta` is given unsigned; a put's is stored negative. `ask` defaults to five cents over the bid."""
    if ask == "auto":
        ask = None if bid is None else D(bid) + D("0.05")
    signed = None if delta is None else (-D(delta) if right == "put" else D(delta))
    return OptionRow(
        contract=ContractKey("F", expiry, D(strike), right),
        bid=None if bid is None else D(bid),
        ask=None if ask is None else D(ask),
        delta=signed,
        iv=None if iv is None else D(iv),
        open_interest=oi,
        is_monthly=monthly,
        dte=(expiry - TODAY).days if dte is None else dte,
    )


def expiry_info(expiry: date = EXPIRY, *, monthly: bool = True, today: date = TODAY) -> ExpiryInfo:
    return ExpiryInfo(expiry, (expiry - today).days, monthly, 20)


def chain(**reference: Any) -> list[OptionRow]:
    """Three puts at deltas 0.30, 0.25 and 0.20. `reference` overrides the 0.30 put's quote."""
    return [row(50, "0.30", **{"bid": "1.50", **reference}), row(48, "0.25", "1.10"), row(46, "0.20", "0.85")]


def run_screen(
    *,
    f: UnderlyingFacts | None = None,
    o: OwnerInputs | None = None,
    a: AccountInputs | None = None,
    rows: Sequence[OptionRow] | None = None,
    expiries: Sequence[ExpiryInfo] | None = None,
    market_open: bool = True,
    cfg: WheelParams = CFG,
) -> screen.ScreenResult:
    rows = chain() if rows is None else rows
    by_expiry: dict[date, list[OptionRow]] = {}
    for r in rows:
        by_expiry.setdefault(r.expiry, []).append(r)
    return screen.run(
        f or facts(),
        o or owner(),
        a or account(),
        [expiry_info()] if expiries is None else expiries,
        by_expiry,
        TODAY,
        market_open,
        cfg,
    )


def eval_put(
    *,
    ask: Any = "1.00",
    dte: int = 30,
    price: Any = 55,
    premium: Any = "1.20",
    strike: Any = 50,
    roll_count: int = 0,
    next_puts: Sequence[OptionRow] = (),
    o: OwnerInputs | None = None,
    cfg: WheelParams = CFG,
    **fact_changes: Any,
) -> evaluate.Action:
    expiry = TODAY + timedelta(days=dte)
    f = facts(price=price, **fact_changes)
    pos = PutPosition("F", D(strike), expiry, 1, D(premium), roll_count)
    quote = row(strike, "0.30", None, ask=ask, expiry=expiry)
    biz = evaluate.business_check(f, o or owner(), cfg)
    return evaluate.put(pos, f, quote, next_puts, biz, TODAY, cfg)


def eval_shares(
    *,
    price: Any = "48.50",
    net_cost: Any = "47.80",
    calls: Sequence[OptionRow] | None = None,
    answer: bool | None = True,
    answered_days_ago: int | None = 0,
    reviewed_days_ago: int | None = None,
    o: OwnerInputs | None = None,
    cfg: WheelParams = CFG,
    **fact_changes: Any,
) -> evaluate.Action:
    f = facts(price=price, **fact_changes)
    pos = SharesPosition(
        "F",
        1,
        D(50),
        D(net_cost),
        answer,
        None if answered_days_ago is None else TODAY - timedelta(days=answered_days_ago),
        None if reviewed_days_ago is None else TODAY - timedelta(days=reviewed_days_ago),
    )
    calls = [row(50, "0.27", "0.60", right="call")] if calls is None else calls
    biz = evaluate.business_check(f, o or owner(), cfg)
    return evaluate.shares(pos, f, calls, [expiry_info()], biz, TODAY, cfg)


def eval_call(
    *,
    ask: Any = "0.50",
    dte: int = 30,
    price: Any = 49,
    premium: Any = "0.60",
    strike: Any = 50,
    o: OwnerInputs | None = None,
    cfg: WheelParams = CFG,
    **fact_changes: Any,
) -> evaluate.Action:
    expiry = TODAY + timedelta(days=dte)
    f = facts(price=price, **fact_changes)
    pos = CallPosition("F", D(strike), expiry, 1, D(premium), D("47.80"))
    quote = row(strike, "0.27", None, ask=ask, right="call", expiry=expiry)
    biz = evaluate.business_check(f, o or owner(), cfg)
    return evaluate.call(pos, f, quote, biz, TODAY, cfg)
