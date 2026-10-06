"""The inputs of the wheel rules (WS §3) that are not already a core type. Frozen value objects; the plug-in
(T11) builds them from the market view, the facts and its own tables. Premiums and prices are per share."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from trader.options.types import ContractKey

PositionState = Literal["NONE", "PUT_OPEN", "SHARES_HELD", "CALL_OPEN"]
TestStatus = Literal["PASS", "CAUTION", "FAIL"]

MISSING_DATA = "MISSING_DATA"
NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class OwnerInputs:
    """WS §3.3. `acknowledged` holds the names of the tests whose caution the owner has acknowledged
    (`TestResult.name`, for example `liquidity`)."""

    would_own: bool | None = None
    ownership_reason: str = ""
    thesis_broken: bool = False
    acknowledged: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class OptionRow:
    """One option contract with its quote (WS §3.2). `delta` is signed as quoted; the rules use its
    absolute value. `dte` is calendar days from the session date."""

    contract: ContractKey
    bid: Decimal | None
    ask: Decimal | None
    delta: Decimal | None
    iv: Decimal | None
    open_interest: int | None
    is_monthly: bool
    dte: int

    @property
    def strike(self) -> Decimal:
        return self.contract.strike

    @property
    def expiry(self) -> date:
        return self.contract.expiry

    @property
    def abs_delta(self) -> Decimal | None:
        return None if self.delta is None else abs(self.delta)


@dataclass(frozen=True, slots=True)
class AccountInputs:
    """WS §3.4."""

    cash_usd: Decimal
    wheel_cash_usd: Decimal
    open_put_collateral_usd: Decimal
    new_positions_paused: bool = False


@dataclass(frozen=True, slots=True)
class PutPosition:
    """State PUT_OPEN. `premium` is the price the open put was sold for (the profit target and the paper
    result use it); `total_put_premium` is everything collected on this position, roll credits included."""

    ticker: str
    strike: Decimal
    expiry: date
    contracts: int
    premium: Decimal
    roll_count: int = 0
    total_put_premium: Decimal | None = None  # None: the same as `premium` (never rolled)


@dataclass(frozen=True, slots=True)
class SharesPosition:
    """State SHARES_HELD. `fresh_cash_answer` and `fresh_cash_date` are the owner's last answer to the
    fresh-cash question (WS §9.2); `last_review_date` is the last written drawdown review."""

    ticker: str
    contracts: int
    assignment_strike: Decimal
    net_cost: Decimal
    fresh_cash_answer: bool | None = None
    fresh_cash_date: date | None = None
    last_review_date: date | None = None


@dataclass(frozen=True, slots=True)
class CallPosition:
    """State CALL_OPEN. `premium` is the price the open call was sold for."""

    ticker: str
    strike: Decimal
    expiry: date
    contracts: int
    premium: Decimal
    net_cost: Decimal
