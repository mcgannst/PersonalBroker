"""Choosing the expiry and strike of a new put (WS §5.1, §5.2) and the covered call (WS §9.3), plus the
liquidity test of one contract (WS test 4), which both the screen and the call selection apply."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from trader.option_strategies.wheel import calc
from trader.option_strategies.wheel.config import WheelParams
from trader.option_strategies.wheel.inputs import MISSING_DATA, OptionRow, TestStatus
from trader.options.types import ExpiryInfo, Right

NO_VALID_EXPIRY = "NO_VALID_EXPIRY"
CallAction = Literal["SELL_CALL", "SKIP_CALL_THIS_CYCLE", "HOLD_UNCOVERED"]


@dataclass(frozen=True, slots=True)
class ExpirySelection:
    expiry: ExpiryInfo | None
    reason: str | None = None  # NO_VALID_EXPIRY when `expiry` is None


@dataclass(frozen=True, slots=True)
class PutSelection:
    reference: OptionRow | None  # tests 4, 5 and 10 read this strike
    chosen: OptionRow | None  # the put to sell; None when no put is inside the delta range
    target_delta: Decimal | None


@dataclass(frozen=True, slots=True)
class CallSelection:
    action: CallAction
    row: OptionRow | None
    reason: str


def upcoming(next_earnings_date: date | None, today: date) -> date | None:
    """The next earnings date, or None when it is unknown. A date already past is stale, so unknown."""
    if next_earnings_date is None or next_earnings_date < today:
        return None
    return next_earnings_date


def liquidity(row: OptionRow, cfg: WheelParams) -> tuple[TestStatus, str]:
    """WS test 4 on one contract. A FAIL that the known fields prove stands even when another is missing."""
    if row.bid is not None and row.bid <= 0:
        return "FAIL", "no bid"
    spread = None if row.bid is None or row.ask is None else calc.spread_pct(row.bid, row.ask)
    if spread is not None and spread > cfg.screen_spread_pct_of_bid_fail:
        return "FAIL", f"spread {spread:.1%} of the bid"
    if row.open_interest is not None and row.open_interest < cfg.screen_open_interest_fail_below:
        return "FAIL", f"open interest {row.open_interest}"
    if spread is None or row.open_interest is None:
        return "CAUTION", MISSING_DATA
    if spread <= cfg.screen_spread_pct_of_bid_pass and row.open_interest >= cfg.screen_open_interest_pass:
        return "PASS", f"spread {spread:.1%}, open interest {row.open_interest}"
    return "CAUTION", f"spread {spread:.1%}, open interest {row.open_interest}"


def _valid_expiries(
    expiries: Iterable[ExpiryInfo],
    dte_min: int,
    dte_max: int,
    earnings: date | None,
    cfg: WheelParams,
) -> list[ExpiryInfo]:
    return [
        e
        for e in expiries
        if dte_min <= e.dte <= dte_max
        and (e.is_monthly or not cfg.entry_monthly_expiries_only)
        and (earnings is None or e.expiry < earnings)
    ]


def expiry(
    expiries: Sequence[ExpiryInfo], next_earnings_date: date | None, today: date, cfg: WheelParams
) -> ExpirySelection:
    """WS §5.1: a monthly expiry inside the DTE window and before earnings, the one closest to the target."""
    valid = _valid_expiries(
        expiries, cfg.entry_dte_min, cfg.entry_dte_max, upcoming(next_earnings_date, today), cfg
    )
    if not valid:
        return ExpirySelection(None, NO_VALID_EXPIRY)
    return ExpirySelection(min(valid, key=lambda e: (abs(e.dte - cfg.entry_target_dte), -e.dte)))


def _closest(rows: Iterable[OptionRow], right: Right, target: Decimal) -> OptionRow | None:
    """The row whose absolute delta is closest to `target`; a tie goes to the lower delta."""
    best: tuple[Decimal, Decimal, OptionRow] | None = None
    for row in rows:
        delta = row.abs_delta
        if delta is None or row.contract.right != right:
            continue
        if best is None or (abs(delta - target), delta) < best[:2]:
            best = (abs(delta - target), delta, row)
    return None if best is None else best[2]


def put(rows: Sequence[OptionRow], conservative: bool, cfg: WheelParams) -> PutSelection:
    """WS §5.2 steps 1 to 3 over the puts of the chosen expiry. `conservative` is true when test 8 or test 9
    is CAUTION; a reference IV above `entry_iv_conservative_above` selects the conservative delta too."""
    reference = _closest(rows, "put", cfg.screen_ror_reference_delta)
    if reference is None:
        return PutSelection(None, None, None)
    high_iv = reference.iv is not None and reference.iv > cfg.entry_iv_conservative_above
    target = cfg.entry_delta_conservative if conservative or high_iv else cfg.entry_delta_max
    in_range = [
        r
        for r in rows
        if r.abs_delta is not None and cfg.entry_delta_min <= r.abs_delta <= cfg.entry_delta_max
    ]
    return PutSelection(reference, _closest(in_range, "put", target), target)


def call(
    net_cost: Decimal,
    calls: Sequence[OptionRow],
    expiries: Sequence[ExpiryInfo],
    next_earnings_date: date | None,
    today: date,
    cfg: WheelParams,
) -> CallSelection:
    """WS §9.3. The strike is never below the net cost. A call that fails the liquidity test is left out."""
    earnings = upcoming(next_earnings_date, today)
    valid = {e.expiry for e in _valid_expiries(expiries, cfg.cc_dte_min, cfg.cc_dte_max, earnings, cfg)}
    if not valid:
        blocked = bool(_valid_expiries(expiries, cfg.cc_dte_min, cfg.cc_dte_max, None, cfg))
        return CallSelection("SKIP_CALL_THIS_CYCLE", None, "earnings" if blocked else NO_VALID_EXPIRY)

    def candidates(delta_min: Decimal, delta_max: Decimal, min_ror: Decimal) -> list[OptionRow]:
        return [
            c
            for c in calls
            if c.contract.right == "call"
            and c.expiry in valid
            and c.strike >= net_cost
            and c.abs_delta is not None
            and delta_min <= c.abs_delta <= delta_max
            and c.bid is not None
            and calc.ror(c.bid, c.strike) >= min_ror
            and liquidity(c, cfg)[0] != "FAIL"
        ]

    standard = candidates(cfg.cc_delta_min, cfg.cc_delta_max, cfg.cc_min_ror)
    chosen = _closest(standard, "call", cfg.cc_delta_max)
    if chosen is not None:
        return CallSelection("SELL_CALL", chosen, "standard call")
    low = candidates(cfg.cc_low_delta_min, cfg.cc_low_delta_max, cfg.cc_low_delta_min_ror)
    if low:
        best = max(low, key=lambda c: (c.bid or Decimal(0), -c.strike))
        return CallSelection("SELL_CALL", best, "low-delta call")
    return CallSelection("HOLD_UNCOVERED", None, "no call at or above the net cost is worth selling")
