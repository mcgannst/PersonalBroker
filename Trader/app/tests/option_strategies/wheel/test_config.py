"""The params model: defaults as WS §2, every assumption labelled, every numeric threshold wired to a rule,
and the rule modules free of I/O."""

import ast
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import trader.option_strategies.wheel as wheel_package
from trader.option_strategies.wheel import screen, select, stops
from trader.option_strategies.wheel.config import WheelParams

from . import CFG, TODAY, D, account, chain, eval_put, eval_shares, expiry_info, facts, row, run_screen

# WS §2, by `<section>_<key>`. The two planned differences are marked.
SPEC_DEFAULTS: dict[str, Any] = {
    "screen_market_cap_pass_usd": 10_000_000_000,
    "screen_market_cap_caution_usd": 2_000_000_000,
    "screen_debt_to_equity_limit": "1.0",
    "screen_debt_to_equity_limit_relaxed": "2.0",
    "screen_relaxed_sectors": ("utilities", "telecom", "pipelines"),
    "screen_spread_pct_of_bid_pass": "0.10",
    "screen_spread_pct_of_bid_fail": "0.15",
    "screen_open_interest_pass": 500,
    "screen_open_interest_fail_below": 100,
    "screen_iv_pass_min": "0.20",
    "screen_iv_pass_max": "0.50",
    "screen_iv_fail_above": "0.60",
    "screen_ror_pass": "0.025",
    "screen_ror_fail_below": "0.005",
    "screen_ror_reference_delta": "0.30",
    "screen_ror_scale_to_days": 45,
    "screen_rsi_caution_at": 70,
    "screen_sma_period": 50,
    "screen_sma_slope_lookback_sessions": 20,
    "screen_sma_flat_tolerance": "0.01",
    "screen_new_low_lookback_sessions": 20,
    "entry_dte_min": 30,
    "entry_dte_max": 45,
    "entry_monthly_expiries_only": True,
    "entry_delta_min": "0.20",
    "entry_delta_max": "0.30",
    "entry_delta_conservative": "0.20",
    "entry_iv_conservative_above": "0.40",
    "entry_contracts_per_ticker": 1,
    "entry_per_ticker_limit_pct_of_wheel_cash": "0.50",  # WS: null. Planned default P1
    "entry_allow_margin": False,
    "manage_profit_target_pct_of_premium": "0.50",
    "manage_time_exit_dte": 21,
    "manage_max_rolls_per_put": 1,
    "manage_pin_risk_band_pct": "0.01",
    "manage_itm_at_time_exit_preference": "ASSIGN",
    "cc_dte_min": 30,
    "cc_dte_max": 45,
    "cc_delta_min": "0.20",
    "cc_delta_max": "0.30",
    "cc_low_delta_min": "0.10",
    "cc_low_delta_max": "0.15",
    "cc_min_ror": "0.01",
    "cc_low_delta_min_ror": "0.005",
    "cc_strike_floor": "NET_COST",
    "cc_include_call_premium_in_net_cost": False,
    "stops_review_drawdown_from_net_cost": "0.25",
    "stops_fresh_cash_retest_days": 30,
    "stops_benchmark_ticker": None,  # None = the setting options.benchmark_ticker (P3)
    "stops_benchmark_review": "QUARTERLY",
    "stops_flat_market_quarter_return_max": "0.02",
}
EXTRA_DEFAULTS: dict[str, Any] = {
    "screen_cash_caution_remaining_pct": "0.05",
    "screen_ror_dte_min": 30,
    "screen_ror_dte_max": 45,
    "screen_flag_payout_ratio_above": "1.0",
    "screen_flag_short_float_above": "0.20",
    "screen_flag_iv_above": "0.40",
    "entry_target_dte": 45,
    "stops_drawdown_review_days": 30,
    "daily_event_offset": "open+60m",
    "walk_orders": True,
    "market_screen_max_candidates": 10,
}
ASSUMPTIONS = {
    "screen_spread_pct_of_bid_fail",
    "screen_open_interest_fail_below",
    "screen_iv_pass_min",
    "screen_sma_slope_lookback_sessions",
    "screen_sma_flat_tolerance",
    "screen_new_low_lookback_sessions",
    "screen_cash_caution_remaining_pct",
    "cc_low_delta_min_ror",
    "cc_include_call_premium_in_net_cost",
    "stops_flat_market_quarter_return_max",
}


def test_defaults_match_the_spec() -> None:
    expected = {**SPEC_DEFAULTS, **EXTRA_DEFAULTS}
    assert set(WheelParams.model_fields) == set(expected) | {"market_screen_filters"}
    for name, value in expected.items():
        actual = getattr(CFG, name)
        assert actual == (Decimal(value) if isinstance(actual, Decimal) else value), name
    assert CFG.market_screen_filters and all(f == f.strip().lower() for f in CFG.market_screen_filters)


def test_params_are_strict_and_frozen() -> None:
    assert WheelParams.model_validate({"manage_time_exit_dte": 14}).manage_time_exit_dte == 14
    assert WheelParams.model_validate_json(CFG.model_dump_json()) == CFG
    assert WheelParams.model_json_schema()["properties"]["screen_ror_pass"]["description"]
    for bad in (
        {"no_such_field": 1},
        {"manage_itm_at_time_exit_preference": "ROLL"},
        {"daily_event_offset": "noon"},
    ):
        with pytest.raises(ValidationError):
            WheelParams.model_validate(bad)
    with pytest.raises(ValidationError):
        CFG.manage_time_exit_dte = 14  # type: ignore[misc]


def test_assumptions_are_labelled() -> None:
    labelled = {
        n for n, f in WheelParams.model_fields.items() if (f.description or "").startswith("ASSUMPTION:")
    }
    assert labelled == ASSUMPTIONS
    assert all(f.description for f in WheelParams.model_fields.values())


def _later(strike: Any, bid: Any) -> Any:
    return row(strike, "0.45", bid, expiry=TODAY + timedelta(days=48))


def _roll(cfg: WheelParams) -> str:
    rolling = cfg.model_copy(update={"manage_itm_at_time_exit_preference": "ROLL_ONCE"})
    return eval_put(
        dte=20, price=48, ask="2.60", roll_count=1, next_puts=[_later(50, "3.40")], cfg=rolling
    ).kind


def _expiry(days: list[int]) -> Callable[[WheelParams], Any]:
    expiries = [expiry_info(TODAY + timedelta(days=d)) for d in days]
    return lambda c: select.expiry(expiries, None, TODAY, c).expiry


def _put(conservative: bool, part: str) -> Callable[[WheelParams], Any]:
    return lambda c: getattr(select.put(chain(), conservative, c), part)


def _call(delta: str, bid: str, ask: str) -> Callable[[WheelParams], Any]:
    calls = [row(50, delta, bid, ask=ask, right="call")]
    return lambda c: select.call(D("47.80"), calls, [expiry_info()], None, TODAY, c).action


STANDARD_CALL = _call("0.27", "0.60", "0.62")
LOW_CALL = _call("0.12", "0.30", "0.31")
UTILITY = facts(sector="Utilities", debt_to_equity="1.5")

# (field, another value, a scenario at the boundary): the scenario's result differs between the two values.
THRESHOLDS: list[tuple[str, Any, Callable[[WheelParams], Any]]] = [
    ("screen_market_cap_pass_usd", D(60_000_000_000), lambda c: screen.size(facts(), c).status),
    (
        "screen_market_cap_caution_usd",
        D(4_000_000_000),
        lambda c: screen.size(facts(market_cap_usd=3e9), c).status,
    ),
    ("screen_debt_to_equity_limit", D("0.4"), lambda c: screen.balance_sheet(facts(), "PASS", c).status),
    (
        "screen_debt_to_equity_limit_relaxed",
        D("1.2"),
        lambda c: screen.balance_sheet(UTILITY, "PASS", c).status,
    ),
    ("screen_spread_pct_of_bid_pass", D("0.02"), lambda c: select.liquidity(row(50, "0.30", "1.50"), c)[0]),
    (
        "screen_spread_pct_of_bid_fail",
        D("0.20"),
        lambda c: select.liquidity(row(50, "0.30", "1.00", ask="1.18"), c)[0],
    ),
    ("screen_open_interest_pass", 2000, lambda c: select.liquidity(row(50, "0.30", "1.50"), c)[0]),
    (
        "screen_open_interest_fail_below",
        300,
        lambda c: select.liquidity(row(50, "0.30", "1.50", oi=200), c)[0],
    ),
    ("screen_iv_pass_min", D("0.35"), lambda c: screen.volatility(row(50, "0.30", "1.50"), c).status),
    ("screen_iv_pass_max", D("0.25"), lambda c: screen.volatility(row(50, "0.30", "1.50"), c).status),
    (
        "screen_iv_fail_above",
        D("0.70"),
        lambda c: screen.volatility(row(50, "0.30", "1.50", iv="0.64"), c).status,
    ),
    ("screen_ror_pass", D("0.035"), lambda c: screen.premium(row(50, "0.30", "1.50"), c).status),
    ("screen_ror_fail_below", D("0.02"), lambda c: screen.premium(row(50, "0.30", "0.90"), c).status),
    ("screen_ror_reference_delta", D("0.20"), _put(False, "reference")),
    ("screen_ror_scale_to_days", 90, lambda c: screen.premium(row(50, "0.30", "1.00", dte=60), c).status),
    ("screen_ror_dte_min", 40, lambda c: screen.premium(row(50, "0.30", "1.00", dte=35), c).status),
    ("screen_ror_dte_max", 50, lambda c: screen.premium(row(50, "0.30", "1.30", dte=48), c).status),
    ("screen_rsi_caution_at", D(80), lambda c: screen.trend(facts(rsi14=74), c).status),
    (
        "screen_sma_flat_tolerance",
        D("0.05"),
        lambda c: screen.trend(facts(sma50=50, sma50_prior=52), c).status,
    ),
    (
        "screen_new_low_lookback_sessions",
        5,
        lambda c: screen.trend(facts(sessions_since_52w_low=10), c).status,
    ),
    ("screen_cash_caution_remaining_pct", D("0.9"), lambda c: screen.cash(D(50), account(), c).status),
    (
        "screen_flag_payout_ratio_above",
        D(2),
        lambda c: run_screen(f=facts(dividend_yield="0.03", payout_ratio="1.5"), cfg=c).flags,
    ),
    (
        "screen_flag_short_float_above",
        D("0.5"),
        lambda c: run_screen(f=facts(short_float="0.3"), cfg=c).flags,
    ),
    ("screen_flag_iv_above", D("0.20"), lambda c: run_screen(cfg=c).flags),
    ("entry_dte_min", 46, _expiry([45])),
    ("entry_dte_max", 40, _expiry([45])),
    ("entry_target_dte", 30, _expiry([31, 44])),
    ("entry_delta_min", D("0.27"), _put(True, "chosen")),
    ("entry_delta_max", D("0.25"), _put(False, "chosen")),
    ("entry_delta_conservative", D("0.25"), _put(True, "chosen")),
    ("entry_iv_conservative_above", D("0.25"), _put(False, "target_delta")),
    ("entry_contracts_per_ticker", 3, lambda c: screen.cash(D(50), account(), c).status),
    ("entry_per_ticker_limit_pct_of_wheel_cash", D("0.2"), lambda c: screen.cash(D(50), account(), c).status),
    ("manage_profit_target_pct_of_premium", D("0.6"), lambda c: eval_put(ask="0.58", cfg=c).kind),
    ("manage_time_exit_dte", 15, lambda c: eval_put(dte=19, price=53, cfg=c).kind),
    ("manage_max_rolls_per_put", 2, _roll),
    ("manage_pin_risk_band_pct", D("0.05"), lambda c: eval_put(dte=0, price="51.50", cfg=c).kind),
    ("cc_dte_min", 46, STANDARD_CALL),
    ("cc_dte_max", 40, STANDARD_CALL),
    ("cc_delta_min", D("0.28"), STANDARD_CALL),
    ("cc_delta_max", D("0.25"), STANDARD_CALL),
    ("cc_min_ror", D("0.02"), STANDARD_CALL),
    ("cc_low_delta_min", D("0.13"), LOW_CALL),
    ("cc_low_delta_max", D("0.11"), LOW_CALL),
    ("cc_low_delta_min_ror", D("0.01"), LOW_CALL),
    ("stops_review_drawdown_from_net_cost", D("0.15"), lambda c: eval_shares(price=38, calls=[], cfg=c).kind),
    (
        "stops_drawdown_review_days",
        10,
        lambda c: eval_shares(price=35, reviewed_days_ago=15, calls=[], cfg=c).kind,
    ),
    ("stops_fresh_cash_retest_days", 10, lambda c: eval_shares(answered_days_ago=15, cfg=c).kind),
    (
        "stops_flat_market_quarter_return_max",
        D("0.05"),
        lambda c: stops.benchmark_review(D("0.03"), D("0.01"), c),
    ),
]
# Numeric fields the pure rules do not read: the plug-in (T11) uses them to gather facts and candidates.
PLUGIN_ONLY = {"screen_sma_period", "screen_sma_slope_lookback_sessions", "market_screen_max_candidates"}


@pytest.mark.parametrize(("name", "other", "scenario"), THRESHOLDS, ids=[t[0] for t in THRESHOLDS])
def test_every_threshold_moves_a_result(
    name: str, other: Any, scenario: Callable[[WheelParams], Any]
) -> None:
    assert type(other) is type(getattr(CFG, name))
    changed = WheelParams.model_validate({**CFG.model_dump(), name: other})
    assert scenario(CFG) != scenario(changed)


def test_every_numeric_threshold_has_a_row() -> None:
    numeric = {
        name
        for name, value in CFG.model_dump().items()
        if isinstance(value, int | Decimal) and not isinstance(value, bool)
    }
    assert numeric == {t[0] for t in THRESHOLDS} | PLUGIN_ONLY
    assert len(THRESHOLDS) == len({t[0] for t in THRESHOLDS})


RULE_MODULES = ("config", "inputs", "screen", "select", "evaluate", "stops", "calc", "alerts")
PACKAGE = "trader.option_strategies.wheel"
ALLOWED_IMPORTS = {
    "collections.abc",
    "dataclasses",
    "datetime",
    "decimal",
    "typing",
    "re",
    "pydantic",
    "trader.options.types",
}
FORBIDDEN_CALLS = {"now", "today", "utcnow", "open", "float", "print"}  # no clock, no I/O, no float


def test_rules_are_pure() -> None:
    folder = Path(wheel_package.__file__).parent
    for module in RULE_MODULES:
        tree = ast.parse((folder / f"{module}.py").read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, module
                if node.module == PACKAGE:
                    imported |= {f"{PACKAGE}.{alias.name}" for alias in node.names}
                else:
                    imported.add(node.module or "")
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                assert name not in FORBIDDEN_CALLS, f"{module} calls {name}"
        allowed = ALLOWED_IMPORTS | {f"{PACKAGE}.{m}" for m in RULE_MODULES}
        assert imported <= allowed, f"{module} imports {sorted(imported - allowed)}"
