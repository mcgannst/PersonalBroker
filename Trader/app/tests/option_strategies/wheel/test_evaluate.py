"""The daily evaluation (WS §7 to §9): one row per rule-table row, plus the first-match order."""

from datetime import timedelta
from typing import Any

import pytest

from trader.option_strategies.wheel import evaluate

from . import CFG, ROLL_CFG, TODAY, D, eval_call, eval_put, eval_shares, facts, owner, row

BROKEN = owner(thesis_broken=True)
IN = TODAY + timedelta(days=10)  # an event date inside a 30-day option

BUSINESS: list[tuple[str, dict[str, Any], tuple[str, ...]]] = [
    ("passes", {}, ()),
    ("missing_data_is_not_a_failure", {"f": facts(eps_growth_yoy=None, debt_to_equity=None)}, ()),
    ("thesis_broken", {"o": BROKEN}, ("thesis_broken",)),
    ("test_1", {"o": owner(would_own=False)}, ("ownership",)),
    (
        "test_2_and_disqualifier",
        {"f": facts(eps_ttm=-1)},
        ("profitability", "earnings per share are not positive"),
    ),
    ("test_3", {"f": facts(debt_to_equity="1.5", eps_growth_yoy="-0.1")}, ("balance_sheet",)),
    ("disqualifier", {"f": facts(security_type="LEVERAGED_OR_INVERSE_ETF")}, ("leveraged or inverse ETF",)),
]


@pytest.mark.parametrize(("given", "failed"), [c[1:] for c in BUSINESS], ids=[c[0] for c in BUSINESS])
def test_business_check(given: dict[str, Any], failed: tuple[str, ...]) -> None:
    check = evaluate.business_check(given.get("f", facts()), given.get("o", owner()), CFG)
    assert (check.passes, check.failed) == (not failed, failed)


PUT_ROWS: list[tuple[str, dict[str, Any], str]] = [
    ("1_business_check_fails", {"o": BROKEN}, "CLOSE_PUT_NOW"),
    ("1_before_the_profit_target", {"o": BROKEN, "ask": "0.50"}, "CLOSE_PUT_NOW"),
    ("2_profit_target_exactly", {"ask": "0.60"}, "CLOSE_PUT_PROFIT"),
    ("2_just_short_of_the_target", {"ask": "0.61"}, "HOLD"),
    ("2_before_earnings_rules", {"ask": "0.58", "next_earnings_date": IN}, "CLOSE_PUT_PROFIT"),
    ("3_earnings_in_profit", {"ask": "1.00", "next_earnings_date": IN}, "CLOSE_PUT_BEFORE_EARNINGS"),
    (
        "3_earnings_on_the_expiry_day",
        {"ask": "1.00", "next_earnings_date": TODAY + timedelta(days=30)},
        "CLOSE_PUT_BEFORE_EARNINGS",
    ),
    ("4_earnings_at_break_even", {"ask": "1.20", "next_earnings_date": IN}, "REVIEW_BEFORE_EARNINGS"),
    (
        "4_before_the_time_exit",
        {"ask": "2.60", "next_earnings_date": IN, "dte": 19, "price": 48},
        "REVIEW_BEFORE_EARNINGS",
    ),
    ("5_pin_risk_above", {"dte": 0, "price": "50.40"}, "PIN_RISK"),
    ("5_pin_risk_below", {"dte": 0, "price": "49.50"}, "PIN_RISK"),
    ("5_expiry_day_clear_of_the_strike", {"dte": 0, "price": 51}, "CLOSE_PUT_TIME"),
    ("6_time_exit_above_the_strike", {"dte": 21, "price": 53}, "CLOSE_PUT_TIME"),
    ("7_time_exit_at_the_strike", {"dte": 21, "price": 50, "ask": "1.80"}, "TAKE_ASSIGNMENT"),
    ("8_before_the_time_exit", {"dte": 22, "price": 53}, "HOLD"),
    ("8_paper_loss_is_not_a_stop", {"dte": 40, "price": 40, "ask": "9.80"}, "HOLD"),
    ("8_no_price_at_the_time_exit", {"dte": 10, "price": None}, "HOLD"),
]


@pytest.mark.parametrize(("given", "kind"), [c[1:] for c in PUT_ROWS], ids=[c[0] for c in PUT_ROWS])
def test_put_evaluation_rows(given: dict[str, Any], kind: str) -> None:
    assert eval_put(**given).kind == kind


def _later(strike: Any, bid: Any, **more: Any) -> Any:
    return row(strike, "0.45", bid, expiry=TODAY + timedelta(days=48), **more)


ITM = {"dte": 20, "price": 48, "ask": "2.60", "cfg": ROLL_CFG}
ROLLS: list[tuple[str, list[Any], dict[str, Any], tuple[str, Any]]] = [
    ("same_strike_for_a_credit", [_later(50, "3.40")], {}, ("ROLL_PUT", 50)),
    (
        "highest_strike_at_or_below",
        [_later(49, "2.90"), _later(50, "3.40"), _later(51, "4.00")],
        {},
        ("ROLL_PUT", 50),
    ),
    ("next_strike_down_when_no_credit", [_later(50, "2.60"), _later(49, "2.70")], {}, ("ROLL_PUT", 49)),
    ("no_net_credit", [_later(50, "2.60"), _later(49, "2.20")], {}, ("TAKE_ASSIGNMENT", None)),
    ("no_later_puts", [], {}, ("TAKE_ASSIGNMENT", None)),
    ("weekly_is_not_a_candidate", [_later(50, "3.40", monthly=False)], {}, ("TAKE_ASSIGNMENT", None)),
    (
        "new_expiry_after_earnings",
        [_later(50, "3.40")],
        {"next_earnings_date": TODAY + timedelta(days=40)},
        ("TAKE_ASSIGNMENT", None),
    ),
    ("preference_assign", [_later(50, "3.40")], {"cfg": CFG}, ("TAKE_ASSIGNMENT", None)),
    ("already_rolled_once", [_later(50, "3.40")], {"roll_count": 1}, ("TAKE_ASSIGNMENT", None)),
]


@pytest.mark.parametrize(
    ("next_puts", "given", "expected"), [c[1:] for c in ROLLS], ids=[c[0] for c in ROLLS]
)
def test_roll_branches(next_puts: list[Any], given: dict[str, Any], expected: tuple[str, Any]) -> None:
    action = eval_put(**{**ITM, **given}, next_puts=next_puts)
    kind, strike = expected
    assert action.kind == kind
    assert (action.candidate.strike if action.candidate else None) == (None if strike is None else D(strike))
    if kind == "ROLL_PUT":
        assert action.candidate is not None and action.candidate.bid is not None
        assert action.detail["net_credit"] == action.candidate.bid - D("2.60") > 0


SHARES_ROWS: list[tuple[str, dict[str, Any], str]] = [
    ("1_business_check_fails", {"o": BROKEN, "answer": None}, "SELL_SHARES"),
    ("2_never_answered", {"answer": None, "answered_days_ago": None}, "RUN_FRESH_CASH_TEST"),
    ("2_retest_after_30_days", {"answered_days_ago": 30}, "RUN_FRESH_CASH_TEST"),
    ("2_before_the_drawdown_review", {"answered_days_ago": 30, "price": 35}, "RUN_FRESH_CASH_TEST"),
    ("2_answer_no", {"answer": False}, "SELL_SHARES"),
    ("3_at_the_review_level", {"price": "35.85"}, "DRAWDOWN_REVIEW"),
    ("3_just_above_the_review_level", {"price": "35.86", "calls": []}, "HOLD_UNCOVERED"),
    ("3_reviewed_10_days_ago", {"price": 35, "reviewed_days_ago": 10, "calls": []}, "HOLD_UNCOVERED"),
    ("3_reviewed_30_days_ago", {"price": 35, "reviewed_days_ago": 30}, "DRAWDOWN_REVIEW"),
    ("4_answer_inside_its_window", {"answered_days_ago": 29}, "SELL_CALL"),
    (
        "4_earnings_inside_the_call",
        {"next_earnings_date": TODAY + timedelta(days=30)},
        "SKIP_CALL_THIS_CYCLE",
    ),
    ("4_no_call", {"calls": []}, "HOLD_UNCOVERED"),
]


@pytest.mark.parametrize(("given", "kind"), [c[1:] for c in SHARES_ROWS], ids=[c[0] for c in SHARES_ROWS])
def test_shares_rows(given: dict[str, Any], kind: str) -> None:
    assert eval_shares(**given).kind == kind


CALL_ROWS: list[tuple[str, dict[str, Any], str]] = [
    ("1_business_check_fails", {"o": BROKEN, "ask": "0.10"}, "CLOSE_CALL_AND_SELL_SHARES"),
    ("2_profit_target_exactly", {"ask": "0.30"}, "CLOSE_CALL_PROFIT"),
    ("2_just_short_of_the_target", {"ask": "0.31"}, "HOLD"),
    (
        "3_in_the_money_before_ex_dividend",
        {"price": 52, "ask": "2.40", "next_ex_dividend_date": IN},
        "EARLY_ASSIGNMENT_RISK",
    ),
    (
        "3_before_the_time_exit_rule",
        {"price": 52, "ask": "2.40", "dte": 18, "next_ex_dividend_date": TODAY},
        "EARLY_ASSIGNMENT_RISK",
    ),
    ("3_out_of_the_money", {"price": 49, "next_ex_dividend_date": IN}, "HOLD"),
    (
        "3_ex_dividend_after_the_expiry",
        {"price": 52, "ask": "2.40", "next_ex_dividend_date": TODAY + timedelta(days=31)},
        "HOLD",
    ),
    ("4_time_exit_above_the_strike", {"price": 52, "ask": "2.40", "dte": 21}, "HOLD_FOR_CALL_AWAY"),
    ("5_time_exit_at_the_strike", {"price": 50, "dte": 21}, "CLOSE_OR_EXPIRE_CALL"),
    ("6_before_the_time_exit", {"price": 52, "ask": "2.40", "dte": 22}, "HOLD"),
]


@pytest.mark.parametrize(("given", "kind"), [c[1:] for c in CALL_ROWS], ids=[c[0] for c in CALL_ROWS])
def test_call_rows(given: dict[str, Any], kind: str) -> None:
    assert eval_call(**given).kind == kind
