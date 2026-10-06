"""The eight alerts of WS §12, found by comparing yesterday's snapshot of a ticker with today's. An alert
fires on the day its condition becomes true, not on every day it stays true."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from trader.option_strategies.wheel import stops
from trader.option_strategies.wheel.config import WheelParams
from trader.option_strategies.wheel.inputs import PositionState, TestStatus

AlertKind = Literal[
    "STRIKE_TOUCHED",
    "TIME_EXIT_DUE",
    "EARNINGS_MOVED",
    "EX_DIVIDEND_AHEAD",
    "FRESH_CASH_DUE",
    "DRAWDOWN_REVIEW_DUE",
    "BENCHMARK_REVIEW_DUE",
    "THESIS_FLAG",
]


@dataclass(frozen=True, slots=True)
class Alert:
    kind: AlertKind
    message: str


@dataclass(frozen=True, slots=True)
class AlertSnapshot:
    """What the alerts read about one ticker on one day. `strike` and `expiry` are the open option's (state
    PUT_OPEN or CALL_OPEN). `quarter_end` is true on the last session of a quarter (the caller knows the
    calendar). For BENCHMARK_REVIEW_DUE alone, pass a snapshot with state NONE and an empty ticker."""

    ticker: str
    today: date
    state: PositionState
    price: Decimal | None = None
    strike: Decimal | None = None
    expiry: date | None = None
    next_earnings_date: date | None = None
    next_ex_dividend_date: date | None = None
    net_cost: Decimal | None = None
    fresh_cash_date: date | None = None
    last_review_date: date | None = None
    profitability: TestStatus | None = None  # test 2
    balance_sheet: TestStatus | None = None  # test 3
    quarter_end: bool = False


def _has_option(s: AlertSnapshot) -> bool:
    return s.state in ("PUT_OPEN", "CALL_OPEN") and s.strike is not None and s.expiry is not None


def _touched(s: AlertSnapshot) -> bool:
    if not _has_option(s) or s.price is None or s.strike is None:
        return False
    return s.price <= s.strike if s.state == "PUT_OPEN" else s.price >= s.strike


def _time_exit(s: AlertSnapshot, cfg: WheelParams) -> bool:
    return _has_option(s) and s.expiry is not None and (s.expiry - s.today).days <= cfg.manage_time_exit_dte


def _earnings_inside(s: AlertSnapshot) -> bool:
    if not _has_option(s) or s.expiry is None or s.next_earnings_date is None:
        return False
    return s.today <= s.next_earnings_date <= s.expiry


def _ex_dividend(s: AlertSnapshot) -> bool:
    if s.state != "CALL_OPEN" or s.price is None or s.strike is None or s.price <= s.strike:
        return False
    ex_date = s.next_ex_dividend_date
    return ex_date is not None and s.expiry is not None and s.today <= ex_date <= s.expiry


def _fresh_cash(s: AlertSnapshot, cfg: WheelParams) -> bool:
    return s.state == "SHARES_HELD" and stops.fresh_cash_due(s.fresh_cash_date, s.today, cfg)


def _drawdown(s: AlertSnapshot, cfg: WheelParams) -> bool:
    if s.state not in ("SHARES_HELD", "CALL_OPEN") or s.price is None or s.net_cost is None:
        return False
    return stops.drawdown_review_due(s.price, s.net_cost, s.last_review_date, s.today, cfg)


def _thesis(s: AlertSnapshot) -> bool:
    return s.state != "NONE" and "FAIL" in (s.profitability, s.balance_sheet)


def detect(previous: AlertSnapshot | None, current: AlertSnapshot, cfg: WheelParams) -> list[Alert]:
    """The alerts that start today. `previous` is the same ticker's last snapshot, or None on the first day
    (then every condition that holds fires, except the two that need a change: EARNINGS_MOVED and
    THESIS_FLAG)."""
    t = current.ticker
    found: list[Alert] = []

    def started(kind: AlertKind, now: bool, before: bool, message: str) -> None:
        if now and not before:
            found.append(Alert(kind, message))

    p = previous
    started(
        "STRIKE_TOUCHED", _touched(current), p is not None and _touched(p), f"{t} reached {current.strike}"
    )
    started(
        "TIME_EXIT_DUE",
        _time_exit(current, cfg),
        p is not None and _time_exit(p, cfg),
        f"{t} option is at the {cfg.manage_time_exit_dte}-day time exit",
    )
    started(
        "EARNINGS_MOVED",
        p is not None and _earnings_inside(current) and p.next_earnings_date != current.next_earnings_date,
        False,
        f"{t} earnings moved to {current.next_earnings_date}, on or before the expiry",
    )
    started(
        "EX_DIVIDEND_AHEAD",
        _ex_dividend(current),
        p is not None and _ex_dividend(p),
        f"{t} call is in the money and goes ex-dividend {current.next_ex_dividend_date}",
    )
    started(
        "FRESH_CASH_DUE",
        _fresh_cash(current, cfg),
        p is not None and _fresh_cash(p, cfg),
        f"{t} fresh-cash test is due",
    )
    started(
        "DRAWDOWN_REVIEW_DUE",
        _drawdown(current, cfg),
        p is not None and _drawdown(p, cfg),
        f"{t} shares are at the review level with no review",
    )
    started(
        "BENCHMARK_REVIEW_DUE",
        current.quarter_end,
        False,
        "quarter end: compare the account with the benchmark",
    )
    started(
        "THESIS_FLAG",
        p is not None and _thesis(current) and not _thesis(p),
        False,
        f"{t} profitability or balance sheet now fails",
    )
    return found
