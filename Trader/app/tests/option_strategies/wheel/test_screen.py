"""The screen (WS §4): one row per cell of the two test tables, the disqualifiers, the verdict ladder, the
missing-data rule, the ranking and the stale flag."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from trader.option_strategies.wheel import screen
from trader.option_strategies.wheel.inputs import MISSING_DATA, NOT_APPLICABLE

from . import CFG, TODAY, account, chain, facts, owner, row, run_screen

ETF = {"security_type": "SECTOR_ETF", "eps_ttm": None, "eps_growth_yoy": None, "debt_to_equity": None}
SOON = TODAY + timedelta(days=20)

# (id, test number, how the passing ticker is changed, status, reason or None for "any")
HARD: list[tuple[str, int, dict[str, Any], str, str | None]] = [
    ("1_pass", 1, {}, "PASS", None),
    ("1_caution_unset", 1, {"o": owner(would_own=None)}, "CAUTION", None),
    ("1_caution_no_reason", 1, {"o": owner(ownership_reason=" ")}, "CAUTION", None),
    ("1_fail", 1, {"o": owner(would_own=False)}, "FAIL", None),
    ("2_pass", 2, {}, "PASS", None),
    ("2_caution_shrinking", 2, {"f": facts(eps_growth_yoy="-0.05")}, "CAUTION", None),
    ("2_fail", 2, {"f": facts(eps_ttm=-1)}, "FAIL", None),
    ("2_etf", 2, {"f": facts(**ETF)}, "PASS", NOT_APPLICABLE),
    ("3_pass", 3, {}, "PASS", None),
    ("3_caution_debt_with_test_2_pass", 3, {"f": facts(debt_to_equity="1.5")}, "CAUTION", None),
    (
        "3_fail_debt_without_test_2_pass",
        3,
        {"f": facts(debt_to_equity="1.5", eps_growth_yoy="-0.05")},
        "FAIL",
        None,
    ),
    ("3_fail_book_value", 3, {"f": facts(book_value_per_share=-1)}, "FAIL", None),
    ("3_relaxed_sector", 3, {"f": facts(debt_to_equity="1.5", sector="Utilities")}, "PASS", None),
    ("3_relaxed_sector_limit", 3, {"f": facts(debt_to_equity="2.0", sector="Utilities")}, "CAUTION", None),
    ("3_etf", 3, {"f": facts(**ETF)}, "PASS", NOT_APPLICABLE),
    ("4_pass", 4, {}, "PASS", None),
    ("4_caution_open_interest", 4, {"rows": chain(oi=300)}, "CAUTION", None),
    ("4_caution_spread", 4, {"rows": chain(bid="1.00", ask="1.12")}, "CAUTION", None),
    ("4_fail_no_bid", 4, {"rows": chain(bid="0", ask="0.05")}, "FAIL", None),
    ("4_fail_spread", 4, {"rows": chain(bid="1.00", ask="1.20")}, "FAIL", None),
    ("4_fail_open_interest", 4, {"rows": chain(oi=50)}, "FAIL", None),
    ("5_pass", 5, {}, "PASS", None),
    ("5_caution_low", 5, {"rows": chain(iv="0.15")}, "CAUTION", None),
    ("5_caution_high", 5, {"rows": chain(iv="0.55")}, "CAUTION", None),
    ("5_fail", 5, {"rows": chain(iv="0.61")}, "FAIL", None),
    ("6_pass", 6, {}, "PASS", None),
    ("6_caution_unknown", 6, {"f": facts(next_earnings_date=None)}, "CAUTION", MISSING_DATA),
    (
        "6_caution_stale_date",
        6,
        {"f": facts(next_earnings_date=TODAY - timedelta(days=3))},
        "CAUTION",
        MISSING_DATA,
    ),
    ("6_fail", 6, {"f": facts(next_earnings_date=SOON)}, "FAIL", "NO_VALID_EXPIRY"),
    ("7_pass", 7, {}, "PASS", None),
    ("7_caution_nothing_left", 7, {"a": account(10_200, collateral=5_000)}, "CAUTION", None),
    ("7_fail_does_not_fit", 7, {"a": account(10_200, collateral=5_300)}, "FAIL", None),
    ("7_fail_per_ticker_limit", 7, {"a": account(8_000)}, "FAIL", None),
]

SOFT: list[tuple[str, int, dict[str, Any], str, str | None]] = [
    ("8_pass", 8, {}, "PASS", None),
    ("8_caution", 8, {"f": facts(market_cap_usd=5_000_000_000)}, "CAUTION", None),
    ("8_fail", 8, {"f": facts(market_cap_usd=1_000_000_000)}, "FAIL", None),
    ("8_etf", 8, {"f": facts(market_cap_usd=None, **ETF)}, "PASS", NOT_APPLICABLE),
    ("9_pass", 9, {}, "PASS", None),
    ("9_pass_flat_average", 9, {"f": facts(sma50="50.6", sma50_prior=51)}, "PASS", None),
    ("9_caution_rsi", 9, {"f": facts(rsi14=70)}, "CAUTION", "wait for cooling"),
    ("9_caution_mixed", 9, {"f": facts(price=50)}, "CAUTION", "mixed trend"),
    ("9_fail_below_falling_average", 9, {"f": facts(price=50, sma50_prior=54)}, "FAIL", None),
    ("9_fail_new_low", 9, {"f": facts(sessions_since_52w_low=20)}, "FAIL", None),
    ("10_pass", 10, {}, "PASS", None),
    ("10_caution", 10, {"rows": chain(bid="0.90")}, "CAUTION", None),
    ("10_fail", 10, {"rows": chain(bid="0.20")}, "FAIL", None),
]


def _check(number: int, changes: dict[str, Any], status: str, reason: str | None) -> None:
    result = run_screen(**changes).test(number)
    assert (result.number, result.name) == (number, screen.TEST_NAMES[number])
    assert result.status == status
    if reason is not None:
        assert result.reason == reason


@pytest.mark.parametrize(
    ("number", "changes", "status", "reason"), [c[1:] for c in HARD], ids=[c[0] for c in HARD]
)
def test_hard_tests(number: int, changes: dict[str, Any], status: str, reason: str | None) -> None:
    _check(number, changes, status, reason)


@pytest.mark.parametrize(
    ("number", "changes", "status", "reason"), [c[1:] for c in SOFT], ids=[c[0] for c in SOFT]
)
def test_soft_tests(number: int, changes: dict[str, Any], status: str, reason: str | None) -> None:
    _check(number, changes, status, reason)


@pytest.mark.parametrize(
    ("dte", "bid", "status"),
    [
        (45, "1.00", "CAUTION"),  # 2.0%, inside 30 to 45: not scaled
        (60, "1.50", "CAUTION"),  # 3.0% x 45 / 60 = 2.25%
        (20, "0.60", "PASS"),  # 1.2% x 45 / 20 = 2.7%
    ],
)
def test_soft_tests_ror_is_scaled_outside_the_dte_band(dte: int, bid: str, status: str) -> None:
    assert screen.premium(row(50, "0.30", bid, dte=dte), CFG).status == status


DISQUALIFIED = ("NOT_A_CANDIDATE", True)
LADDER: list[tuple[str, dict[str, Any], tuple[str, bool]]] = [
    ("disqualifier_book_value", {"f": facts(book_value_per_share=0)}, DISQUALIFIED),
    ("disqualifier_leveraged_etf", {"f": facts(security_type="LEVERAGED_OR_INVERSE_ETF")}, DISQUALIFIED),
    ("disqualifier_stock_without_earnings", {"f": facts(eps_ttm=0)}, DISQUALIFIED),
    (
        "etf_without_earnings_is_not_disqualified",
        {"f": facts(**{**ETF, "eps_ttm": -1})},
        ("QUALIFIED", False),
    ),
    ("hard_fail", {"o": owner(would_own=False), "f": facts(rsi14=74)}, ("NOT_A_CANDIDATE", False)),
    (
        "soft_fail_beats_hard_caution",
        {"o": owner(would_own=None), "rows": chain(bid="0.20", ask="0.21")},
        ("NOT_NOW", False),
    ),
    ("hard_caution", {"o": owner(would_own=None), "f": facts(rsi14=74)}, ("NEEDS_REVIEW", False)),
    ("soft_caution", {"f": facts(rsi14=74)}, ("QUALIFIED_WITH_CONDITION", False)),
    ("all_pass", {}, ("QUALIFIED", False)),
]


@pytest.mark.parametrize(("changes", "expected"), [c[1:] for c in LADDER], ids=[c[0] for c in LADDER])
def test_disqualifiers_and_verdict_ladder(changes: dict[str, Any], expected: tuple[str, bool]) -> None:
    result = run_screen(**changes)
    assert (result.verdict, bool(result.disqualifiers)) == expected
    assert len(result.tests) == 10


def test_needs_review_allows_entry_only_when_every_caution_is_acknowledged() -> None:
    changes = {"f": facts(next_earnings_date=None), "rows": chain(oi=300)}
    blocked = run_screen(**changes, o=owner(acknowledged=frozenset({"liquidity"})))
    assert (blocked.verdict, blocked.unacknowledged, blocked.entry_allowed) == (
        "NEEDS_REVIEW",
        ("earnings",),
        False,
    )
    allowed = run_screen(**changes, o=owner(acknowledged=frozenset({"liquidity", "earnings"})))
    assert (allowed.verdict, allowed.unacknowledged, allowed.entry_allowed) == ("NEEDS_REVIEW", (), True)
    assert run_screen().entry_allowed
    assert not run_screen(o=owner(would_own=False)).entry_allowed


NO_DELTAS = [row(50, None, "1.50"), row(48, None, "1.10")]
MISSING: list[tuple[str, dict[str, Any], list[int]]] = [
    ("eps_ttm", {"f": facts(eps_ttm=None)}, [2]),
    ("eps_growth_yoy", {"f": facts(eps_growth_yoy=None)}, [2]),
    ("debt_to_equity", {"f": facts(debt_to_equity=None)}, [3]),
    ("book_value_per_share", {"f": facts(book_value_per_share=None)}, [3]),
    ("market_cap_usd", {"f": facts(market_cap_usd=None)}, [8]),
    ("price", {"f": facts(price=None)}, [9]),
    ("sma50", {"f": facts(sma50=None)}, [9]),
    ("sma50_prior", {"f": facts(sma50_prior=None)}, [9]),
    ("sessions_since_52w_low", {"f": facts(sessions_since_52w_low=None)}, [9]),
    ("rsi14", {"f": facts(rsi14=None)}, [9]),
    ("next_earnings_date", {"f": facts(next_earnings_date=None)}, [6]),
    ("bid", {"rows": chain(bid=None)}, [4, 10]),
    ("ask", {"rows": chain(ask=None)}, [4]),
    ("open_interest", {"rows": chain(oi=None)}, [4]),
    ("iv", {"rows": chain(iv=None)}, [5]),
    ("delta", {"rows": NO_DELTAS}, [4, 5, 7, 10]),
    ("no_chain", {"rows": []}, [4, 5, 7, 10]),
]


@pytest.mark.parametrize(("changes", "numbers"), [c[1:] for c in MISSING], ids=[c[0] for c in MISSING])
def test_missing_data_is_caution_never_pass(changes: dict[str, Any], numbers: list[int]) -> None:
    result = run_screen(**changes)
    for number in numbers:
        assert (result.test(number).status, result.test(number).reason) == ("CAUTION", MISSING_DATA)
    assert result.verdict != "QUALIFIED"


def test_a_proven_fail_stands_when_other_data_is_missing() -> None:
    assert run_screen(f=facts(rsi14=None, sessions_since_52w_low=3)).test(9).status == "FAIL"
    assert run_screen(rows=chain(oi=None, bid="1.00", ask="1.30")).test(4).status == "FAIL"


def test_ranking_never_uses_premium() -> None:
    plain: Callable[..., screen.ScreenResult] = run_screen
    thin = plain(f=facts(ticker="THIN"), rows=chain(bid="1.30"))  # QUALIFIED at 2.6%
    rich = plain(f=facts(ticker="RICH", rsi14=74), rows=chain(bid="3.00"))  # a condition, at 6%
    richest = plain(f=facts(ticker="RICHEST", rsi14=74, market_cap_usd=5_000_000_000), rows=chain(bid="4.00"))
    not_now = plain(f=facts(ticker="LOW", sessions_since_52w_low=2), rows=chain(bid="4.50"))
    ranked = screen.rank([not_now, richest, rich, thin])
    assert [r.ticker for r in ranked] == ["THIN", "RICH", "RICHEST", "LOW"]  # verdict, then PASS count
    assert [r.verdict for r in ranked][1:3] == ["QUALIFIED_WITH_CONDITION"] * 2
    assert (rich.passes, richest.passes) == (9, 8)


def test_stale_flag_when_market_closed() -> None:
    closed = run_screen(market_open=False)
    assert closed.stale and "VERIFY_LIVE" in closed.flags
    assert closed.verdict == "QUALIFIED"  # stale quotes never block
    live = run_screen()
    assert not live.stale and live.flags == ()


def test_informational_flags_do_not_change_the_verdict() -> None:
    result = run_screen(
        f=facts(dividend_yield="0.04", payout_ratio="1.3", short_float="0.25"),
        rows=chain(iv="0.45"),
        cfg=CFG.model_copy(update={"entry_per_ticker_limit_pct_of_wheel_cash": None}),
    )
    assert result.flags == ("DIVIDEND_CUT_RISK", "HEAVILY_SHORTED", "HIGH_IV", "PER_TICKER_LIMIT_NOT_SET")
    assert result.verdict == "QUALIFIED"
