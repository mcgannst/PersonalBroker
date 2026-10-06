"""The sixteen acceptance cases of the wheel rules spec §13, with the default config."""

from collections.abc import Callable
from datetime import timedelta

import pytest

from trader.option_strategies.wheel import calc, select, stops

from . import (
    CFG,
    ROLL_CFG,
    TODAY,
    D,
    account,
    chain,
    eval_call,
    eval_put,
    eval_shares,
    expiry_info,
    facts,
    row,
    run_screen,
)


def _01_negative_eps_is_disqualified() -> None:
    result = run_screen(f=facts(eps_ttm="-0.40"))
    assert result.verdict == "NOT_A_CANDIDATE"
    assert result.disqualifiers


def _02_thin_premium_is_a_condition() -> None:
    result = run_screen(rows=chain(bid="0.90"))  # 0.90 / 50 = 1.8%
    assert [t.status for t in result.tests[:9]] == ["PASS"] * 9
    assert result.test(10).status == "CAUTION"
    assert result.verdict == "QUALIFIED_WITH_CONDITION"
    assert result.target_delta == D("0.30")


def _03_hot_rsi_selects_the_conservative_delta() -> None:
    result = run_screen(f=facts(rsi14=74))
    assert [t.number for t in result.tests if t.status != "PASS"] == [9]
    assert result.verdict == "QUALIFIED_WITH_CONDITION"
    assert result.target_delta == D("0.20")
    assert result.chosen is not None and result.chosen.strike == D(46)


def _04_high_iv_fails() -> None:
    result = run_screen(rows=chain(iv="0.64"))
    assert result.test(5).status == "FAIL"
    assert result.verdict == "NOT_A_CANDIDATE"


def _05_earnings_before_the_only_expiry() -> None:
    expiry = TODAY + timedelta(days=38)
    result = run_screen(
        f=facts(next_earnings_date=TODAY + timedelta(days=20)),
        rows=[row(50, "0.30", "1.50", expiry=expiry)],
        expiries=[expiry_info(expiry)],
    )
    assert result.expiry is None
    assert (result.test(6).status, result.test(6).reason) == ("FAIL", select.NO_VALID_EXPIRY)


def _06_profit_target() -> None:
    assert eval_put(premium="1.20", ask="0.58", dte=30).kind == "CLOSE_PUT_PROFIT"


def _07_paper_loss_with_time_left_is_hold() -> None:
    assert eval_put(premium="1.20", ask="2.90", dte=33, price=47).kind == "HOLD"


def _08_time_exit_out_of_the_money() -> None:
    assert eval_put(premium="1.20", ask="0.80", dte=19, price=53).kind == "CLOSE_PUT_TIME"


def _09_time_exit_in_the_money_assign() -> None:
    assert eval_put(dte=20, price=48, ask="2.60").kind == "TAKE_ASSIGNMENT"


def _10_second_roll_is_not_allowed() -> None:
    next_puts = [row(50, "0.45", "3.40", expiry=TODAY + timedelta(days=48))]
    action = eval_put(dte=20, price=48, ask="2.60", roll_count=1, next_puts=next_puts, cfg=ROLL_CFG)
    assert action.kind == "TAKE_ASSIGNMENT"


def _11_eps_turns_negative() -> None:
    assert eval_put(eps_ttm="-0.10").kind == "CLOSE_PUT_NOW"


def _12_sell_the_call_at_50() -> None:
    action = eval_shares(net_cost="47.80", price="48.50", calls=[row(50, "0.27", "0.60", right="call")])
    assert action.kind == "SELL_CALL"
    assert action.candidate is not None and action.candidate.strike == D(50)


def _13_no_call_worth_selling_then_drawdown_review() -> None:
    calls = [row(40, "0.35", "1.00", right="call"), row(50, "0.05", "0.05", right="call")]
    assert calc.drawdown_from_cost(D("47.80"), D(38)).quantize(D("0.001")) == D("0.205")
    assert eval_shares(net_cost="47.80", price=38, calls=calls).kind == "HOLD_UNCOVERED"
    assert calc.drawdown_from_cost(D("47.80"), D(35)).quantize(D("0.001")) == D("0.268")
    assert eval_shares(net_cost="47.80", price=35, calls=calls).kind == "DRAWDOWN_REVIEW"


def _14_fresh_cash_no_sells_the_shares() -> None:
    assert eval_shares(answer=False).kind == "SELL_SHARES"


def _15_call_in_the_money_at_the_time_exit() -> None:
    assert eval_call(dte=18, price=52, ask="2.40").kind == "HOLD_FOR_CALL_AWAY"


def _16_new_put_on_a_held_ticker_is_blocked() -> None:
    assert stops.can_open(True, account(), D(5000), CFG) == stops.POSITION_OPEN
    assert stops.can_open(False, account(), D(5000), CFG) is None


CASES: list[Callable[[], None]] = [
    _01_negative_eps_is_disqualified,
    _02_thin_premium_is_a_condition,
    _03_hot_rsi_selects_the_conservative_delta,
    _04_high_iv_fails,
    _05_earnings_before_the_only_expiry,
    _06_profit_target,
    _07_paper_loss_with_time_left_is_hold,
    _08_time_exit_out_of_the_money,
    _09_time_exit_in_the_money_assign,
    _10_second_roll_is_not_allowed,
    _11_eps_turns_negative,
    _12_sell_the_call_at_50,
    _13_no_call_worth_selling_then_drawdown_review,
    _14_fresh_cash_no_sells_the_shares,
    _15_call_in_the_money_at_the_time_exit,
    _16_new_put_on_a_held_ticker_is_blocked,
]


@pytest.mark.parametrize("case", CASES, ids=[f"ws13_{c.__name__[1:3]}" for c in CASES])
def test_ws13_cases(case: Callable[[], None]) -> None:
    case()
