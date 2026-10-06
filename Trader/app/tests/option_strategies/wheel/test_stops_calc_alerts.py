"""The stop rules (WS §10), the calculations (WS §11) and the alerts (WS §12)."""

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trader.option_strategies.wheel import alerts, calc, stops

from . import CFG, TODAY, D, account, eval_call, eval_put, eval_shares, owner

BROKEN = owner(thesis_broken=True)
NO_LIMIT = CFG.model_copy(update={"entry_per_ticker_limit_pct_of_wheel_cash": None})
REVIEWED = TODAY - timedelta(days=29)

STOPS: list[tuple[str, Callable[[], Any], Any]] = [
    ("business_check_exits_a_put", lambda: eval_put(o=BROKEN).kind, "CLOSE_PUT_NOW"),
    ("business_check_exits_shares", lambda: eval_shares(o=BROKEN).kind, "SELL_SHARES"),
    ("business_check_exits_a_call", lambda: eval_call(o=BROKEN).kind, "CLOSE_CALL_AND_SELL_SHARES"),
    ("fresh_cash_fails", lambda: eval_shares(answer=False).kind, "SELL_SHARES"),
    (
        "drawdown_at_the_level",
        lambda: stops.drawdown_review_due(D("35.85"), D("47.80"), None, TODAY, CFG),
        True,
    ),
    (
        "drawdown_above_the_level",
        lambda: stops.drawdown_review_due(D("35.86"), D("47.80"), None, TODAY, CFG),
        False,
    ),
    (
        "drawdown_reviewed_recently",
        lambda: stops.drawdown_review_due(D(30), D("47.80"), REVIEWED, TODAY, CFG),
        False,
    ),
    (
        "drawdown_review_every_30_days",
        lambda: stops.drawdown_review_due(D(30), D("47.80"), TODAY - timedelta(days=30), TODAY, CFG),
        True,
    ),
    ("open_position_blocks", lambda: stops.can_open(True, account(), D(5000), CFG), stops.POSITION_OPEN),
    ("first_roll_allowed", lambda: stops.can_roll(0, CFG), True),
    ("second_roll_blocked", lambda: stops.can_roll(1, CFG), False),
    (
        "collateral_fits_exactly",
        lambda: stops.can_open(False, account(10_000, collateral=5_000), D(5000), CFG),
        None,
    ),
    (
        "collateral_over_the_cash",
        lambda: stops.can_open(False, account(10_000, collateral=5_000), D(5001), CFG),
        stops.INSUFFICIENT_CASH,
    ),
    ("per_ticker_limit", lambda: stops.can_open(False, account(), D(10_001), CFG), stops.PER_TICKER_LIMIT),
    ("no_per_ticker_limit_set", lambda: stops.can_open(False, account(), D(10_001), NO_LIMIT), None),
    ("paused", lambda: stops.can_open(False, account(paused=True), D(5000), CFG), stops.NEW_POSITIONS_PAUSED),
    ("benchmark_flat_and_trailing", lambda: stops.benchmark_review(D("0.02"), D("0.01"), CFG), True),
    ("benchmark_falling_and_trailing", lambda: stops.benchmark_review(D("-0.05"), D("-0.08"), CFG), True),
    ("benchmark_flat_and_matched", lambda: stops.benchmark_review(D("0.02"), D("0.02"), CFG), False),
    ("benchmark_falling_but_ahead", lambda: stops.benchmark_review(D("-0.05"), D("-0.02"), CFG), False),
    ("trailing_a_rising_market", lambda: stops.benchmark_review(D("0.06"), D("0.01"), CFG), False),
]


@pytest.mark.parametrize(("rule", "expected"), [c[1:] for c in STOPS], ids=[c[0] for c in STOPS])
def test_stop_rules(rule: Callable[[], Any], expected: Any) -> None:
    assert rule() == expected


CYCLE = {
    "total_put_premium": D("2.20"),
    "total_call_premium": D("0.60"),
    "dividends": D("0.30"),
    "sale_price": D(50),
    "assignment_strike": D(50),
    "contracts": 2,
    "fees": D(4),
}
CALCS: list[tuple[str, Decimal, str]] = [
    ("collateral", calc.collateral(D(50), 2), "10000"),
    ("breakeven", calc.breakeven(D(50), D("1.20")), "48.80"),
    ("cushion", calc.cushion(D(55), D("49.50")), "0.1"),
    ("ror", calc.ror(D("1.50"), D(50)), "0.03"),
    ("ror_annualized", calc.ror_annualized(D("0.03"), 73), "0.15"),
    ("spread_pct", calc.spread_pct(D("1.00"), D("1.12")), "0.12"),
    ("put_pl_per_share", calc.put_pl_per_share(D("1.20"), D("0.58")), "0.62"),
    ("net_cost", calc.net_cost(D(50), D("2.20"), D("0.60")), "47.80"),
    (
        "net_cost_with_call_premium",
        calc.net_cost(D(50), D("2.20"), D("0.60"), include_call_premium=True),
        "47.20",
    ),
    ("drawdown_from_cost", calc.drawdown_from_cost(D(48), D(36)), "0.25"),
    ("full_cycle_result", calc.full_cycle_result(**CYCLE), "616"),
    ("account_value", calc.account_value(D(10_000), D(4_800), D(250)), "14550"),
]


@pytest.mark.parametrize(("value", "expected"), [c[1:] for c in CALCS], ids=[c[0] for c in CALCS])
def test_calculations(value: Decimal, expected: str) -> None:
    assert isinstance(value, Decimal)
    assert value == Decimal(expected)


EXPIRY = TODAY + timedelta(days=30)
YESTERDAY = TODAY - timedelta(days=1)


def _snap(state: str, today: date = TODAY, **more: Any) -> alerts.AlertSnapshot:
    values: dict[str, Any] = {"price": 53, "fresh_cash_date": TODAY - timedelta(days=5)}
    if state in ("PUT_OPEN", "CALL_OPEN"):
        values.update(strike=50, expiry=EXPIRY)
    if state != "NONE":
        values.update(net_cost="47.80")
    values.update(more)
    for key in ("price", "strike", "net_cost"):
        if values.get(key) is not None:
            values[key] = D(values[key])
    return alerts.AlertSnapshot("F", today, state, **values)  # type: ignore[arg-type]


def _pair(
    state: str, before: dict[str, Any], now: dict[str, Any]
) -> tuple[alerts.AlertSnapshot, alerts.AlertSnapshot]:
    return _snap(state, YESTERDAY, **before), _snap(state, **now)


IN = TODAY + timedelta(days=10)
AFTER = EXPIRY + timedelta(days=5)
AT_EXIT = {"expiry": TODAY + timedelta(days=21)}
OLD_ANSWER = {"fresh_cash_date": TODAY - timedelta(days=30)}
ALERTS: list[tuple[str, tuple[alerts.AlertSnapshot | None, alerts.AlertSnapshot], list[str]]] = [
    ("quiet_day", _pair("PUT_OPEN", {}, {}), []),
    ("put_strike_touched", _pair("PUT_OPEN", {"price": 51}, {"price": 50}), ["STRIKE_TOUCHED"]),
    ("put_still_below_the_strike", _pair("PUT_OPEN", {"price": 49}, {"price": 48}), []),
    ("call_strike_touched", _pair("CALL_OPEN", {"price": 49}, {"price": 50}), ["STRIKE_TOUCHED"]),
    ("time_exit_due", _pair("PUT_OPEN", AT_EXIT, AT_EXIT), ["TIME_EXIT_DUE"]),
    ("time_exit_already_passed", _pair("PUT_OPEN", {"expiry": IN}, {"expiry": IN}), []),
    (
        "earnings_moved_inside",
        _pair("PUT_OPEN", {"next_earnings_date": AFTER}, {"next_earnings_date": IN}),
        ["EARNINGS_MOVED"],
    ),
    ("earnings_unchanged", _pair("PUT_OPEN", {"next_earnings_date": IN}, {"next_earnings_date": IN}), []),
    ("earnings_moved_but_after_expiry", _pair("PUT_OPEN", {}, {"next_earnings_date": AFTER}), []),
    (
        "ex_dividend_ahead",
        _pair("CALL_OPEN", {"price": 52}, {"price": 52, "next_ex_dividend_date": IN}),
        ["EX_DIVIDEND_AHEAD"],
    ),
    (
        "ex_dividend_out_of_the_money",
        _pair("CALL_OPEN", {"price": 49}, {"price": 49, "next_ex_dividend_date": IN}),
        [],
    ),
    ("fresh_cash_due", _pair("SHARES_HELD", OLD_ANSWER, OLD_ANSWER), ["FRESH_CASH_DUE"]),
    ("drawdown_review_due", _pair("SHARES_HELD", {"price": 38}, {"price": 35}), ["DRAWDOWN_REVIEW_DUE"]),
    (
        "drawdown_reviewed",
        _pair("SHARES_HELD", {"price": 38}, {"price": 35, "last_review_date": TODAY - timedelta(days=3)}),
        [],
    ),
    ("benchmark_review_due", (None, _snap("NONE", quarter_end=True)), ["BENCHMARK_REVIEW_DUE"]),
    (
        "thesis_flag",
        _pair("PUT_OPEN", {"profitability": "PASS", "balance_sheet": "PASS"}, {"profitability": "FAIL"}),
        ["THESIS_FLAG"],
    ),
    ("thesis_flag_needs_a_position", _pair("NONE", {"profitability": "PASS"}, {"profitability": "FAIL"}), []),
    (
        "first_day_reports_what_holds",
        (None, _snap("PUT_OPEN", price=49, expiry=IN, next_earnings_date=IN, profitability="FAIL")),
        ["STRIKE_TOUCHED", "TIME_EXIT_DUE"],
    ),
]


@pytest.mark.parametrize(("snaps", "expected"), [c[1:] for c in ALERTS], ids=[c[0] for c in ALERTS])
def test_alerts(snaps: tuple[alerts.AlertSnapshot | None, alerts.AlertSnapshot], expected: list[str]) -> None:
    found = alerts.detect(snaps[0], snaps[1], CFG)
    assert [a.kind for a in found] == expected
    assert all(a.message for a in found)


def test_alerts_cover_the_eight_of_the_spec() -> None:
    fired = {kind for case in ALERTS for kind in case[2]}
    assert fired == set(alerts.AlertKind.__args__)  # type: ignore[attr-defined]
    assert len(fired) == 8
