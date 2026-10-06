"""The daily evaluation of an open position (WS §7 to §9): one recommended action per position and day, the
first rule that applies. There is no price-based stop on a put."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from trader.option_strategies.wheel import calc, screen, select, stops
from trader.option_strategies.wheel.config import WheelParams
from trader.option_strategies.wheel.inputs import (
    MISSING_DATA,
    CallPosition,
    OptionRow,
    OwnerInputs,
    PutPosition,
    SharesPosition,
)
from trader.options.types import ExpiryInfo, UnderlyingFacts, dte

ActionKind = Literal[
    "HOLD",
    # PUT_OPEN (WS §8)
    "CLOSE_PUT_NOW",
    "CLOSE_PUT_PROFIT",
    "CLOSE_PUT_BEFORE_EARNINGS",
    "REVIEW_BEFORE_EARNINGS",
    "PIN_RISK",
    "CLOSE_PUT_TIME",
    "ROLL_PUT",
    "TAKE_ASSIGNMENT",
    # SHARES_HELD (WS §9.2 to §9.4)
    "SELL_SHARES",
    "RUN_FRESH_CASH_TEST",
    "DRAWDOWN_REVIEW",
    "SELL_CALL",
    "SKIP_CALL_THIS_CYCLE",
    "HOLD_UNCOVERED",
    # CALL_OPEN (WS §9.5)
    "CLOSE_CALL_AND_SELL_SHARES",
    "CLOSE_CALL_PROFIT",
    "EARLY_ASSIGNMENT_RISK",
    "HOLD_FOR_CALL_AWAY",
    "CLOSE_OR_EXPIRE_CALL",
]


@dataclass(frozen=True, slots=True)
class BusinessCheck:
    passes: bool
    failed: tuple[str, ...]  # what failed: `thesis_broken`, a test name, or a disqualifier


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    reason: str
    candidate: OptionRow | None = None  # the put to roll into, or the call to sell
    detail: Mapping[str, Any] = field(default_factory=dict)  # the numbers behind the action


def business_check(facts: UnderlyingFacts, owner: OwnerInputs, cfg: WheelParams) -> BusinessCheck:
    """WS §7: narrower than the screen on purpose. Liquidity, volatility, earnings and cash are not part of
    it; missing data is a caution, not a failure."""
    failed = ["thesis_broken"] if owner.thesis_broken else []
    t2 = screen.profitability(facts)
    tests = (screen.ownership(owner), t2, screen.balance_sheet(facts, t2.status, cfg))
    failed += [t.name for t in tests if t.status == "FAIL"]
    failed += list(screen.disqualifiers(facts))
    return BusinessCheck(not failed, tuple(failed))


def _earnings_inside(facts: UnderlyingFacts, expiry: date, today: date) -> date | None:
    upcoming = select.upcoming(facts.next_earnings_date, today)
    return upcoming if upcoming is not None and upcoming <= expiry else None


def _roll_candidate(
    pos: PutPosition,
    facts: UnderlyingFacts,
    quote: OptionRow,
    next_monthly_puts: Sequence[OptionRow],
    today: date,
    cfg: WheelParams,
) -> Action | None:
    """WS §8.1: the highest strike at or below the current one, in the next monthly expiry, that rolls for
    a net credit and expires before earnings."""
    if cfg.manage_itm_at_time_exit_preference != "ROLL_ONCE" or not stops.can_roll(pos.roll_count, cfg):
        return None
    if quote.ask is None:
        return None
    later = [
        r
        for r in next_monthly_puts
        if r.contract.right == "put" and r.is_monthly and r.expiry > pos.expiry and r.bid is not None
    ]
    if not later:
        return None
    next_expiry = min(r.expiry for r in later)
    earnings = select.upcoming(facts.next_earnings_date, today)
    if earnings is not None and next_expiry >= earnings:
        return None
    for row in sorted((r for r in later if r.expiry == next_expiry), key=lambda r: -r.strike):
        if row.strike <= pos.strike and row.bid is not None and row.bid - quote.ask > 0:
            credit = row.bid - quote.ask
            return Action("ROLL_PUT", "in the money at the time exit", row, {"net_credit": credit})
    return None


def put(
    pos: PutPosition,
    facts: UnderlyingFacts,
    quote: OptionRow,
    next_monthly_puts: Sequence[OptionRow],
    biz: BusinessCheck,
    today: date,
    cfg: WheelParams,
) -> Action:
    """WS §8. `quote` is the open put's own row; `next_monthly_puts` the puts of the next monthly expiry."""
    if not biz.passes:
        return Action("CLOSE_PUT_NOW", "business check failed: " + ", ".join(biz.failed))
    target = pos.premium * (1 - cfg.manage_profit_target_pct_of_premium)
    if quote.ask is not None and quote.ask <= target:
        return Action(
            "CLOSE_PUT_PROFIT", "profit target reached", detail={"ask": quote.ask, "target": target}
        )
    earnings = _earnings_inside(facts, pos.expiry, today)
    if earnings is not None:
        detail = {"next_earnings_date": earnings}
        if quote.ask is not None and calc.put_pl_per_share(pos.premium, quote.ask) > 0:
            return Action("CLOSE_PUT_BEFORE_EARNINGS", "earnings before the expiry, in profit", detail=detail)
        return Action("REVIEW_BEFORE_EARNINGS", "earnings before the expiry, not in profit", detail=detail)
    days = dte(pos.expiry, today)
    if days > cfg.manage_time_exit_dte:
        return Action("HOLD", "no rule applies", detail={"dte": days})
    if facts.price is None:
        return Action("HOLD", MISSING_DATA, detail={"dte": days})
    if days == 0 and abs(facts.price - pos.strike) / pos.strike <= cfg.manage_pin_risk_band_pct:
        return Action("PIN_RISK", "expiry day with the price at the strike", detail={"price": facts.price})
    if facts.price > pos.strike:
        return Action("CLOSE_PUT_TIME", "time exit, out of the money", detail={"dte": days})
    roll = _roll_candidate(pos, facts, quote, next_monthly_puts, today, cfg)
    return roll or Action("TAKE_ASSIGNMENT", "in the money at the time exit", detail={"dte": days})


def shares(
    pos: SharesPosition,
    facts: UnderlyingFacts,
    calls: Sequence[OptionRow],
    expiries: Sequence[ExpiryInfo],
    biz: BusinessCheck,
    today: date,
    cfg: WheelParams,
) -> Action:
    """WS §9.2 to §9.4 for shares with no call open."""
    if not biz.passes:
        return Action("SELL_SHARES", "business check failed: " + ", ".join(biz.failed))
    if pos.fresh_cash_answer is False:
        return Action("SELL_SHARES", "fresh-cash test failed")
    if pos.fresh_cash_answer is None or stops.fresh_cash_due(pos.fresh_cash_date, today, cfg):
        return Action("RUN_FRESH_CASH_TEST", "fresh-cash test is due")
    if facts.price is not None and stops.drawdown_review_due(
        facts.price, pos.net_cost, pos.last_review_date, today, cfg
    ):
        drawdown = calc.drawdown_from_cost(pos.net_cost, facts.price)
        return Action("DRAWDOWN_REVIEW", "shares are at the review level", detail={"drawdown": drawdown})
    picked = select.call(pos.net_cost, calls, expiries, facts.next_earnings_date, today, cfg)
    return Action(picked.action, picked.reason, picked.row)


def call(
    pos: CallPosition,
    facts: UnderlyingFacts,
    quote: OptionRow,
    biz: BusinessCheck,
    today: date,
    cfg: WheelParams,
) -> Action:
    """WS §9.5. `quote` is the open call's own row."""
    if not biz.passes:
        return Action("CLOSE_CALL_AND_SELL_SHARES", "business check failed: " + ", ".join(biz.failed))
    target = pos.premium * (1 - cfg.manage_profit_target_pct_of_premium)
    if quote.ask is not None and quote.ask <= target:
        return Action(
            "CLOSE_CALL_PROFIT", "profit target reached", detail={"ask": quote.ask, "target": target}
        )
    days = dte(pos.expiry, today)
    if facts.price is None:
        return Action("HOLD", MISSING_DATA, detail={"dte": days})
    in_the_money = facts.price > pos.strike
    ex_date = facts.next_ex_dividend_date
    if in_the_money and ex_date is not None and today <= ex_date <= pos.expiry:
        return Action(
            "EARLY_ASSIGNMENT_RISK",
            "in the money with an ex-dividend date ahead",
            detail={"ex_date": ex_date},
        )
    if days > cfg.manage_time_exit_dte:
        return Action("HOLD", "no rule applies", detail={"dte": days})
    if in_the_money:
        return Action("HOLD_FOR_CALL_AWAY", "time exit, in the money", detail={"dte": days})
    return Action("CLOSE_OR_EXPIRE_CALL", "time exit, out of the money", detail={"dte": days})
