"""The screen (WS §4): three automatic disqualifiers, seven hard tests, three soft tests, the verdict ladder,
the ranking and the informational flags. A test whose input is missing is CAUTION with reason MISSING_DATA,
never PASS (WS §3.5); a FAIL that the known inputs prove still stands."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import ClassVar, Literal

from trader.option_strategies.wheel import calc, select
from trader.option_strategies.wheel.config import WheelParams
from trader.option_strategies.wheel.inputs import (
    MISSING_DATA,
    NOT_APPLICABLE,
    AccountInputs,
    OptionRow,
    OwnerInputs,
    TestStatus,
)
from trader.options.types import ExpiryInfo, UnderlyingFacts

Verdict = Literal["QUALIFIED", "QUALIFIED_WITH_CONDITION", "NEEDS_REVIEW", "NOT_NOW", "NOT_A_CANDIDATE"]
VERDICT_ORDER: tuple[Verdict, ...] = (
    "QUALIFIED",
    "QUALIFIED_WITH_CONDITION",
    "NEEDS_REVIEW",
    "NOT_NOW",
    "NOT_A_CANDIDATE",
)
TEST_NAMES: Mapping[int, str] = {
    1: "ownership",
    2: "profitability",
    3: "balance_sheet",
    4: "liquidity",
    5: "volatility",
    6: "earnings",
    7: "cash",
    8: "size",
    9: "trend",
    10: "premium",
}
HARD_TESTS = frozenset({1, 2, 3, 4, 5, 6, 7})
SOFT_TESTS = frozenset({8, 9, 10})
ETF_TYPES = frozenset({"BROAD_INDEX_ETF", "SECTOR_ETF"})
LEVERAGED = "LEVERAGED_OR_INVERSE_ETF"
_SEVERITY: Mapping[str, int] = {"PASS": 0, "CAUTION": 1, "FAIL": 2}


@dataclass(frozen=True, slots=True)
class TestResult:
    __test__: ClassVar[bool] = False  # not a pytest class

    number: int
    name: str
    status: TestStatus
    reason: str


@dataclass(frozen=True, slots=True)
class ScreenResult:
    ticker: str
    verdict: Verdict
    tests: tuple[TestResult, ...]  # the ten tests, in order
    flags: tuple[str, ...]  # informational (WS §4.6); they never change the verdict
    expiry: ExpiryInfo | None
    reference: OptionRow | None
    chosen: OptionRow | None
    target_delta: Decimal | None
    breakeven: Decimal | None  # chosen strike less its bid
    stale: bool  # the market is closed: option quotes need a live check (WS §3.5)
    disqualifiers: tuple[str, ...] = ()
    unacknowledged: tuple[str, ...] = ()  # hard tests at CAUTION the owner has not acknowledged

    def test(self, number: int) -> TestResult:
        return self.tests[number - 1]

    @property
    def passes(self) -> int:
        return sum(1 for t in self.tests if t.status == "PASS")

    @property
    def entry_allowed(self) -> bool:
        """The verdict precondition of WS §5 (the position and pause checks are `stops.can_open`)."""
        if self.chosen is None:
            return False
        if self.verdict == "NEEDS_REVIEW":
            return not self.unacknowledged
        return self.verdict in ("QUALIFIED", "QUALIFIED_WITH_CONDITION")


def _result(number: int, status: TestStatus, reason: str) -> TestResult:
    return TestResult(number, TEST_NAMES[number], status, reason)


def _is_etf(facts: UnderlyingFacts) -> bool:
    return facts.security_type in ETF_TYPES


def disqualifiers(facts: UnderlyingFacts) -> tuple[str, ...]:
    """WS §4.1."""
    found = []
    if facts.book_value_per_share is not None and facts.book_value_per_share <= 0:
        found.append("book value per share is not positive")
    if facts.security_type == LEVERAGED:
        found.append("leveraged or inverse ETF")
    if facts.security_type == "STOCK" and facts.eps_ttm is not None and facts.eps_ttm <= 0:
        found.append("earnings per share are not positive")
    return tuple(found)


def ownership(owner: OwnerInputs) -> TestResult:
    if owner.would_own is False:
        return _result(1, "FAIL", "the owner would not own it")
    if owner.would_own is None:
        return _result(1, "CAUTION", "ask the owner")
    if not owner.ownership_reason.strip():
        return _result(1, "CAUTION", "no ownership reason")
    return _result(1, "PASS", "the owner would own it")


def profitability(facts: UnderlyingFacts) -> TestResult:
    if _is_etf(facts):
        return _result(2, "PASS", NOT_APPLICABLE)
    if facts.eps_ttm is None:
        return _result(2, "CAUTION", MISSING_DATA)
    if facts.eps_ttm <= 0:
        return _result(2, "FAIL", "earnings per share are not positive")
    if facts.eps_growth_yoy is None:
        return _result(2, "CAUTION", MISSING_DATA)
    if facts.eps_growth_yoy < 0:
        return _result(2, "CAUTION", "earnings are shrinking")
    return _result(2, "PASS", "profitable and growing")


def balance_sheet(facts: UnderlyingFacts, profitability_status: TestStatus, cfg: WheelParams) -> TestResult:
    if _is_etf(facts):
        return _result(3, "PASS", NOT_APPLICABLE)
    if facts.book_value_per_share is not None and facts.book_value_per_share <= 0:
        return _result(3, "FAIL", "book value per share is not positive")
    sector = (facts.sector or "").lower()
    relaxed = any(word in sector for word in cfg.screen_relaxed_sectors)
    limit = cfg.screen_debt_to_equity_limit_relaxed if relaxed else cfg.screen_debt_to_equity_limit
    if facts.debt_to_equity is not None and facts.debt_to_equity >= limit:
        reason = f"debt to equity {facts.debt_to_equity} is at or over {limit}"
        return _result(3, "CAUTION" if profitability_status == "PASS" else "FAIL", reason)
    if facts.book_value_per_share is None or facts.debt_to_equity is None:
        return _result(3, "CAUTION", MISSING_DATA)
    return _result(3, "PASS", f"debt to equity {facts.debt_to_equity} is under {limit}")


def liquidity(row: OptionRow | None, cfg: WheelParams) -> TestResult:
    if row is None:
        return _result(4, "CAUTION", MISSING_DATA)
    status, reason = select.liquidity(row, cfg)
    return _result(4, status, reason)


def volatility(row: OptionRow | None, cfg: WheelParams) -> TestResult:
    if row is None or row.iv is None:
        return _result(5, "CAUTION", MISSING_DATA)
    if row.iv > cfg.screen_iv_fail_above:
        return _result(5, "FAIL", f"IV {row.iv:.0%} is too high")
    if cfg.screen_iv_pass_min <= row.iv <= cfg.screen_iv_pass_max:
        return _result(5, "PASS", f"IV {row.iv:.0%}")
    return _result(5, "CAUTION", f"IV {row.iv:.0%} is outside the pass range")


def earnings(next_earnings_date: date | None, expiry: date | None, today: date) -> TestResult:
    if expiry is None:
        return _result(6, "FAIL", select.NO_VALID_EXPIRY)
    upcoming = select.upcoming(next_earnings_date, today)
    if upcoming is None:
        return _result(6, "CAUTION", MISSING_DATA)
    if upcoming > expiry:
        return _result(6, "PASS", "earnings are after the expiry")
    return _result(6, "FAIL", "earnings are on or before the expiry")


def cash(strike: Decimal | None, account: AccountInputs, cfg: WheelParams) -> TestResult:
    if strike is None:
        return _result(7, "CAUTION", MISSING_DATA)
    need = calc.collateral(strike, cfg.entry_contracts_per_ticker)
    remaining = account.cash_usd - account.open_put_collateral_usd - need
    if remaining < 0:
        return _result(7, "FAIL", "the collateral does not fit the cash")
    limit = cfg.entry_per_ticker_limit_pct_of_wheel_cash
    if limit is not None and need > limit * account.wheel_cash_usd:
        return _result(7, "FAIL", "the collateral is over the per-ticker limit")
    if remaining < cfg.screen_cash_caution_remaining_pct * account.wheel_cash_usd:
        return _result(7, "CAUTION", "fits with nothing left over")
    return _result(7, "PASS", "the collateral fits")


def size(facts: UnderlyingFacts, cfg: WheelParams) -> TestResult:
    if _is_etf(facts):
        return _result(8, "PASS", NOT_APPLICABLE)
    if facts.market_cap_usd is None:
        return _result(8, "CAUTION", MISSING_DATA)
    if facts.market_cap_usd >= cfg.screen_market_cap_pass_usd:
        return _result(8, "PASS", "large company")
    if facts.market_cap_usd < cfg.screen_market_cap_caution_usd:
        return _result(8, "FAIL", "too small")
    return _result(8, "CAUTION", "mid-sized company")


def trend(facts: UnderlyingFacts, cfg: WheelParams) -> TestResult:
    since_low = facts.sessions_since_52w_low
    if since_low is not None and since_low <= cfg.screen_new_low_lookback_sessions:
        return _result(9, "FAIL", f"52-week low {since_low} sessions ago")
    below = None if facts.price is None or facts.sma50 is None else facts.price < facts.sma50
    falling = (
        None
        if facts.sma50 is None or facts.sma50_prior is None
        else facts.sma50 < facts.sma50_prior * (1 - cfg.screen_sma_flat_tolerance)
    )
    if below and falling:
        return _result(9, "FAIL", "below a falling moving average")
    if below is None or falling is None or since_low is None or facts.rsi14 is None:
        return _result(9, "CAUTION", MISSING_DATA)
    if below or falling:
        return _result(9, "CAUTION", "mixed trend")
    if facts.rsi14 >= cfg.screen_rsi_caution_at:
        return _result(9, "CAUTION", "wait for cooling")
    return _result(9, "PASS", "at or above a flat or rising moving average")


def premium(row: OptionRow | None, cfg: WheelParams) -> TestResult:
    if row is None or row.bid is None or row.dte <= 0:
        return _result(10, "CAUTION", MISSING_DATA)
    ror = calc.ror(row.bid, row.strike)
    if not cfg.screen_ror_dte_min <= row.dte <= cfg.screen_ror_dte_max:
        ror = ror * cfg.screen_ror_scale_to_days / row.dte
    if ror >= cfg.screen_ror_pass:
        return _result(10, "PASS", f"return on risk {ror:.2%}")
    if ror < cfg.screen_ror_fail_below:
        return _result(10, "FAIL", f"return on risk {ror:.2%}")
    return _result(10, "CAUTION", f"return on risk {ror:.2%}")


def verdict(disqualified: bool, tests: Iterable[TestResult]) -> Verdict:
    """WS §4.4."""
    hard: set[str] = set()
    soft: set[str] = set()
    for t in tests:
        (hard if t.number in HARD_TESTS else soft).add(t.status)
    if disqualified or "FAIL" in hard:
        return "NOT_A_CANDIDATE"
    if "FAIL" in soft:
        return "NOT_NOW"
    if "CAUTION" in hard:
        return "NEEDS_REVIEW"
    if "CAUTION" in soft:
        return "QUALIFIED_WITH_CONDITION"
    return "QUALIFIED"


def _flags(
    facts: UnderlyingFacts, reference: OptionRow | None, stale: bool, cfg: WheelParams
) -> tuple[str, ...]:
    flags = []
    if (
        facts.dividend_yield is not None
        and facts.dividend_yield > 0
        and facts.payout_ratio is not None
        and facts.payout_ratio > cfg.screen_flag_payout_ratio_above
    ):
        flags.append("DIVIDEND_CUT_RISK")
    if facts.short_float is not None and facts.short_float > cfg.screen_flag_short_float_above:
        flags.append("HEAVILY_SHORTED")
    if reference is not None and reference.iv is not None and reference.iv > cfg.screen_flag_iv_above:
        flags.append("HIGH_IV")
    if cfg.entry_per_ticker_limit_pct_of_wheel_cash is None:
        flags.append("PER_TICKER_LIMIT_NOT_SET")
    if stale:
        flags.append("VERIFY_LIVE")
    return tuple(flags)


def run(
    facts: UnderlyingFacts,
    owner: OwnerInputs,
    account: AccountInputs,
    expiries: Sequence[ExpiryInfo],
    puts_by_expiry: Mapping[date, Sequence[OptionRow]],
    today: date,
    market_open: bool,
    cfg: WheelParams,
) -> ScreenResult:
    """Score one underlying. The ten tests are always computed, a disqualified one included, so that the
    owner sees why."""
    t8, t9 = size(facts, cfg), trend(facts, cfg)
    picked_expiry = select.expiry(expiries, facts.next_earnings_date, today, cfg).expiry
    rows = puts_by_expiry.get(picked_expiry.expiry, ()) if picked_expiry is not None else ()
    picked = select.put(rows, "CAUTION" in (t8.status, t9.status), cfg)
    reference, chosen = picked.reference, picked.chosen

    t4 = liquidity(reference, cfg)
    if chosen is not None and chosen != reference:  # WS §5.2 step 4: the worse of the two strikes counts
        again = liquidity(chosen, cfg)
        if _SEVERITY[again.status] > _SEVERITY[t4.status]:
            t4 = _result(4, again.status, f"chosen strike {chosen.strike}: {again.reason}")
    t2 = profitability(facts)
    sized_on = chosen or reference
    strike = sized_on.strike if sized_on is not None else None
    tests = (
        ownership(owner),
        t2,
        balance_sheet(facts, t2.status, cfg),
        t4,
        volatility(reference, cfg),
        earnings(facts.next_earnings_date, picked_expiry.expiry if picked_expiry else None, today),
        cash(strike, account, cfg),
        t8,
        t9,
        premium(reference, cfg),
    )
    found = disqualifiers(facts)
    outcome = verdict(bool(found), tests)
    return ScreenResult(
        ticker=facts.ticker,
        verdict=outcome,
        tests=tests,
        flags=_flags(facts, reference, not market_open, cfg),
        expiry=picked_expiry,
        reference=reference,
        chosen=chosen,
        target_delta=picked.target_delta,
        breakeven=(
            calc.breakeven(chosen.strike, chosen.bid)
            if chosen is not None and chosen.bid is not None
            else None
        ),
        stale=not market_open,
        disqualifiers=found,
        unacknowledged=tuple(
            t.name
            for t in tests
            if t.number in HARD_TESTS and t.status == "CAUTION" and t.name not in owner.acknowledged
        ),
    )


def rank(results: Iterable[ScreenResult]) -> list[ScreenResult]:
    """WS §4.5: by verdict, then by the count of PASS results. Never by premium, RoR or IV."""
    return sorted(results, key=lambda r: (VERDICT_ORDER.index(r.verdict), -r.passes))
