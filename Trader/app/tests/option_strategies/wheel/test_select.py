"""Expiry and strike of a new put (WS §5.1, §5.2) and the covered call (WS §9.3)."""

from datetime import timedelta
from typing import Any

import pytest

from trader.option_strategies.wheel import select
from trader.option_strategies.wheel.inputs import OptionRow

from . import CFG, TODAY, D, chain, expiry_info, facts, row, run_screen

EXPIRIES: list[tuple[str, list[tuple[int, bool]], int | None, dict[str, Any], int | None]] = [
    ("step1_inside_the_window", [(29, True), (38, True), (46, True)], None, {}, 38),
    ("step1_weekly_is_skipped", [(38, False)], None, {}, None),
    ("step1_weekly_allowed_by_config", [(38, False)], None, {"entry_monthly_expiries_only": False}, 38),
    ("step2_drop_after_earnings", [(31, True), (44, True)], 40, {}, 31),
    ("step2_drop_on_the_earnings_day", [(31, True), (44, True)], 44, {}, 31),
    ("step3_closest_to_45", [(31, True), (44, True)], None, {}, 44),
    ("step3_earnings_after_both", [(31, True), (44, True)], 50, {}, 44),
    ("step4_none_left", [(38, True)], 20, {}, None),
]


@pytest.mark.parametrize(
    ("days", "earnings_in", "config", "expected"), [c[1:] for c in EXPIRIES], ids=[c[0] for c in EXPIRIES]
)
def test_expiry_selection(
    days: list[tuple[int, bool]], earnings_in: int | None, config: dict[str, Any], expected: int | None
) -> None:
    expiries = [expiry_info(TODAY + timedelta(days=d), monthly=m) for d, m in days]
    earnings = None if earnings_in is None else TODAY + timedelta(days=earnings_in)
    picked = select.expiry(expiries, earnings, TODAY, CFG.model_copy(update=config))
    if expected is None:
        assert (picked.expiry, picked.reason) == (None, select.NO_VALID_EXPIRY)
    else:
        assert picked.expiry is not None and picked.expiry.dte == expected


WIDE = [row(52, "0.34", "1.90"), row(50, "0.29", "1.50"), row(47, "0.22", "0.95")]
STRIKES: list[tuple[str, list[OptionRow], bool, tuple[Any, Any, Any]]] = [
    # (reference strike, target delta, chosen strike)
    ("step1_reference_closest_to_030", WIDE, False, (50, "0.30", 50)),
    ("step2_normal_target_is_delta_max", chain(), False, (50, "0.30", 50)),
    ("step2_size_or_trend_caution", chain(), True, (50, "0.20", 46)),
    ("step2_high_reference_iv", chain(iv="0.41"), False, (50, "0.20", 46)),
    ("step3_closest_inside_the_range", WIDE, True, (50, "0.20", 47)),
    (
        "step3_nothing_inside_the_range",
        [row(52, "0.35", "1.90"), row(44, "0.15", "0.50")],
        False,
        (52, "0.30", None),
    ),
    ("no_deltas", [row(50, None, "1.50")], False, (None, None, None)),
]


@pytest.mark.parametrize(
    ("rows", "conservative", "expected"), [c[1:] for c in STRIKES], ids=[c[0] for c in STRIKES]
)
def test_strike_selection(rows: list[OptionRow], conservative: bool, expected: tuple[Any, Any, Any]) -> None:
    picked = select.put(rows, conservative, CFG)
    reference, target, chosen = expected
    assert (picked.reference.strike if picked.reference else None) == (
        None if reference is None else D(reference)
    )
    assert picked.target_delta == (None if target is None else D(target))
    assert (picked.chosen.strike if picked.chosen else None) == (None if chosen is None else D(chosen))


def test_strike_selection_step4_reruns_liquidity_on_the_chosen_strike() -> None:
    thin = [row(50, "0.30", "1.50"), row(48, "0.25", "1.10"), row(46, "0.20", "0.85", oi=50)]
    conservative = run_screen(f=facts(rsi14=74), rows=thin)
    assert conservative.chosen is not None and conservative.chosen.strike == D(46)
    assert conservative.test(4).status == "FAIL" and "46" in conservative.test(4).reason
    assert conservative.breakeven == D("45.15")
    assert run_screen(rows=thin).test(4).status == "PASS"  # the 0.30 put is both reference and chosen


def _call(strike: Any, delta: str, bid: str, **more: Any) -> OptionRow:
    return row(strike, delta, bid, right="call", **more)


CALLS: list[tuple[str, list[OptionRow], dict[str, Any], tuple[str, Any, str | None]]] = [
    (
        "standard_closest_to_delta_max",
        [_call(52, "0.22", "0.56"), _call(50, "0.27", "0.60")],
        {},
        ("SELL_CALL", 50, None),
    ),
    (
        "never_below_net_cost",
        [_call(47, "0.30", "1.50"), _call(52, "0.22", "0.56")],
        {},
        ("SELL_CALL", 52, None),
    ),
    (
        "low_delta_takes_the_highest_bid",
        [
            _call(50, "0.27", "0.40"),
            _call(54, "0.12", "0.30", ask="0.32"),
            _call(55, "0.10", "0.28", ask="0.30"),
        ],
        {},
        ("SELL_CALL", 54, "low-delta call"),
    ),
    ("low_delta_not_worthwhile", [_call(54, "0.12", "0.20", ask="0.21")], {}, ("HOLD_UNCOVERED", None, None)),
    ("no_calls", [], {}, ("HOLD_UNCOVERED", None, None)),
    (
        "earnings_before_the_expiry",
        [_call(50, "0.27", "0.60")],
        {"earnings_in": 30},
        ("SKIP_CALL_THIS_CYCLE", None, "earnings"),
    ),
    (
        "no_expiry_in_the_window",
        [_call(50, "0.27", "0.60")],
        {"expiry_in": 20},
        ("SKIP_CALL_THIS_CYCLE", None, select.NO_VALID_EXPIRY),
    ),
    (
        "liquidity_fail_removes_the_contract",
        [_call(50, "0.27", "0.60", oi=50), _call(52, "0.22", "0.56")],
        {},
        ("SELL_CALL", 52, None),
    ),
    (
        "liquidity_fail_leaves_nothing",
        [_call(50, "0.27", "0.60", ask="0.75")],
        {},
        ("HOLD_UNCOVERED", None, None),
    ),
]


@pytest.mark.parametrize(("calls", "when", "expected"), [c[1:] for c in CALLS], ids=[c[0] for c in CALLS])
def test_call_selection_branches(
    calls: list[OptionRow], when: dict[str, Any], expected: tuple[str, Any, str | None]
) -> None:
    earnings = TODAY + timedelta(days=when["earnings_in"]) if "earnings_in" in when else None
    expiries = (
        [expiry_info(TODAY + timedelta(days=when["expiry_in"]))] if "expiry_in" in when else [expiry_info()]
    )
    picked = select.call(D("47.80"), calls, expiries, earnings, TODAY, CFG)
    action, strike, reason = expected
    assert picked.action == action
    assert (picked.row.strike if picked.row else None) == (None if strike is None else D(strike))
    if reason is not None:
        assert picked.reason == reason
