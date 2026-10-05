"""Option value types shared by the market view, the collateral engine, the book, the broker, the strategy
framework, the API and the plug-ins (OPTSIM task plan §3.1). Frozen, slotted dataclasses; no I/O.

Price and quantity conventions (every task uses these):

- One UNIT of an order is one set of its legs; `qty` is the number of units. An option leg's `ratio` is
  contracts per unit; a shares leg's `ratio` is shares per unit (100 for a buy-write).
- NET PRICE is per share, credit positive: `net = (sum of sell legs - sum of buy legs) / 100`, each leg
  `price x ratio x m`, where `m` is the contract multiplier for an option leg and 1 for a shares leg. A
  `net_limit` of 0.45 means "receive at least 0.45"; -1.20 means "pay at most 1.20".
- Cash moved by a fill = `net x 100 x qty` (positive = received) minus fees.
- `entry_net` and `take_profit_net` of a structure use the same sign.
- Money, greeks, IV and ratios are `Decimal`, never `float`: an intent or request given a float raises
  `TypeError`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Literal

Right = Literal["call", "put"]
Side = Literal["buy", "sell"]
Effect = Literal["open", "close"]
Instrument = Literal["option", "shares"]
OptOrderType = Literal["market", "limit"]
Tif = Literal["day", "gtc"]
OrderIntent = Literal["open", "close", "roll"]
OrderStatus = Literal["working", "filled", "cancelled", "expired", "rejected"]
StructureKind = Literal[
    "long_call",
    "long_put",
    "csp",
    "covered_call",
    "debit_spread",
    "credit_spread",
    "iron_condor",
    "calendar",
    "diagonal",
    "shares",
    "custom",
]
StructureState = Literal["open", "closed"]
CloseReason = Literal["closed", "expired", "assigned", "exercised", "called_away", "rolled", "sold"]
LifecycleKind = Literal["expired", "exercised", "assigned", "called_away", "early_assignment", "frozen"]
RejectReason = Literal[
    "naked_short",
    "insufficient_cash",
    "position_cap",
    "shares_committed",
    "not_covered_after_close",
    "nothing_to_close",
    "unknown_contract",
    "expired_contract",
    "invalid_order",
    "no_quote",
    "structure_frozen",
    "strategies_paused",
]
NoFillReason = Literal[
    "outside_hours",
    "quote_missing",
    "quote_delayed",
    "quote_stale",
    "halted",
    "one_sided",
    "crossed",
    "zero_bid",
    "limit_not_reached",
]
PromptStatus = Literal["pending", "answered", "expired", "cancelled"]
CoverKind = Literal["cash", "shares", "long_leg"]
FillTrigger = Literal["market", "limit"]
AnsweredVia = Literal["telegram", "web"]

SOURCE_MANUAL = "manual"  # any other order or structure source is a strategy key
PROMPT_CHOICE_CODES = "aryncobskhw"  # every letter a PromptChoice.code may be (task plan T10)
MAX_REASON = 100  # opt_orders.reason is varchar(100)
MAX_DEDUPE_KEY = 150  # owner_prompts.dedupe_key is varchar(150)
SHARES_PER_CONTRACT = 100
HUNDRED = Decimal(100)
ZERO = Decimal(0)


def require_decimals(obj: object, names: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    """Global rule 3: never float for money. Raises TypeError naming the field that is not a Decimal."""
    for name in names:
        value = getattr(obj, name)
        if value is None and name in optional:
            continue
        if not isinstance(value, Decimal):
            raise TypeError(
                f"{type(obj).__name__}.{name} must be a Decimal, not {type(value).__name__} ({value!r})"
            )


# --- contracts and quotes -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContractKey:
    """How a strategy names a contract; the host resolves it with `OptionMarketView.find_contract`."""

    underlying: str
    expiry: date
    strike: Decimal
    right: Right

    def __post_init__(self) -> None:
        require_decimals(self, ("strike",))


@dataclass(frozen=True, slots=True)
class OptionContract:
    id: int  # option_contracts.id
    underlying: str
    underlying_symbol_id: int
    qt_symbol_id: int
    root: str
    expiry: date
    strike: Decimal
    right: Right
    multiplier: int
    is_monthly: bool
    adjusted: bool


@dataclass(frozen=True, slots=True)
class OptionQuote:
    contract_id: int
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    bid_size: int | None
    ask_size: int | None
    volume: int | None
    open_interest: int | None
    iv: Decimal | None  # a decimal fraction: 0.35 = 35%
    delta: Decimal | None  # signed: negative for puts
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    last_trade_time: datetime | None
    delay: int | None  # None: unknown, never assume real-time
    is_halted: bool
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class ExpiryInfo:
    expiry: date
    dte: int  # calendar days from the session date
    is_monthly: bool
    strikes: int


@dataclass(frozen=True, slots=True)
class ChainStrike:
    strike: Decimal
    call_id: int | None  # option_contracts.id
    put_id: int | None


# --- orders -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegSpec:
    """One leg as a strategy asks for it (the host resolves `contract` and sets the effect)."""

    instrument: Instrument
    side: Side
    ratio: int
    underlying: str
    contract: ContractKey | None = None  # None for a shares leg


@dataclass(frozen=True, slots=True)
class OrderLeg:
    """One resolved leg of an order."""

    leg_no: int
    instrument: Instrument
    side: Side
    effect: Effect
    ratio: int
    underlying: str
    contract_id: int | None  # None for a shares leg


@dataclass(frozen=True, slots=True)
class OrderRequest:
    source: str  # SOURCE_MANUAL or a strategy key
    strategy_config_id: int | None
    intent: OrderIntent
    structure_id: int | None  # the structure a close or roll acts on
    underlying: str
    legs: tuple[OrderLeg, ...]
    qty: int
    order_type: OptOrderType
    net_limit: Decimal | None
    tif: Tif
    walk: bool
    take_profit_pct: Decimal | None
    reason: str
    evidence: Mapping[str, Any]
    submitted_by: str

    def __post_init__(self) -> None:
        require_decimals(self, ("net_limit", "take_profit_pct"), optional=("net_limit", "take_profit_pct"))
        if len(self.reason) > MAX_REASON:
            raise ValueError(f"reason is {len(self.reason)} characters, over the {MAX_REASON} allowed")


@dataclass(frozen=True, slots=True)
class OptOrderView:
    id: int
    source: str
    strategy_config_id: int | None
    intent: OrderIntent
    structure_id: int | None
    underlying: str
    legs: tuple[OrderLeg, ...]
    qty: int
    order_type: OptOrderType
    net_limit: Decimal | None
    tif: Tif
    walk: bool
    take_profit_pct: Decimal | None
    reason: str
    evidence: Mapping[str, Any]
    submitted_by: str
    status: OrderStatus
    reject_reason: RejectReason | None
    reject_detail: str | None
    reserved_cash: Decimal
    submitted_at: datetime
    closed_at: datetime | None
    fill_net: Decimal | None
    fees: Decimal | None


@dataclass(frozen=True, slots=True)
class LegFill:
    leg_no: int
    instrument: Instrument
    contract_id: int | None
    side: Side
    effect: Effect
    qty: int  # contracts or shares
    price: Decimal
    fee: Decimal
    quote: Mapping[str, Any]  # the quote snapshot stored on the fill


@dataclass(frozen=True, slots=True)
class OptionFillEvent:
    order_id: int
    structure_id: int
    source: str
    strategy_config_id: int | None
    intent: OrderIntent
    ts: datetime
    qty: int
    net_price: Decimal
    fees: Decimal
    legs: tuple[LegFill, ...]
    realized_pnl: Decimal | None


# --- positions, structures, the account ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptPositionView:
    id: int
    structure_id: int
    instrument: Instrument
    contract: OptionContract | None  # None for shares
    underlying: str
    qty: int  # signed: short < 0
    avg_price: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class StructureView:
    id: int
    source: str
    strategy_config_id: int | None
    kind: StructureKind
    underlying: str
    state: StructureState
    close_reason: CloseReason | None
    frozen: bool
    qty: int
    entry_net: Decimal
    reserved_cash: Decimal
    take_profit_net: Decimal | None
    cover_structure_id: int | None
    parent_structure_id: int | None
    realized_pnl: Decimal
    fees_total: Decimal
    opened_at: datetime
    closed_at: datetime | None
    positions: tuple[OptPositionView, ...]
    meta: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OptionAccountState:
    cash: Decimal
    reserved: Decimal
    free_cash: Decimal  # cash - reserved
    positions_value: Decimal
    equity: Decimal  # the account value
    premium_collected: Decimal
    as_of: datetime
    marks_complete: bool


# --- collateral and fills -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverPair:
    short_leg_no: int
    cover: CoverKind
    cover_ref: int | None  # a leg_no (long_leg) or a shares structure id (shares); None for cash


@dataclass(frozen=True, slots=True)
class CollateralDecision:
    accepted: bool
    reject_reason: RejectReason | None
    detail: str
    kind: StructureKind
    net_at_market: Decimal | None
    reserve_cash: Decimal
    max_loss: Decimal | None
    max_profit: Decimal | None
    breakevens: tuple[Decimal, ...]
    fees: Decimal
    cash_after: Decimal
    free_cash_after: Decimal
    exposure_after: Decimal
    cap_limit: Decimal
    pairs: tuple[CoverPair, ...]
    cover_structure_id: int | None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    order: OptOrderView  # a rejected order is stored with status `rejected`
    decision: CollateralDecision


@dataclass(frozen=True, slots=True)
class LegQuote:
    leg_no: int
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    fetched_at: datetime
    delay: int | None
    is_halted: bool
    raw: Mapping[str, Any]  # the snapshot stored on the fill


@dataclass(frozen=True, slots=True)
class FillDecision:
    net_price: Decimal
    leg_prices: tuple[tuple[int, Decimal], ...]  # (leg_no, price)
    fees: Decimal
    trigger: FillTrigger


@dataclass(frozen=True, slots=True)
class NoFill:
    reason: NoFillReason
    detail: str = ""


# --- lifecycle and facts ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    id: int
    structure_id: int
    source: str
    strategy_config_id: int | None
    kind: LifecycleKind
    session_date: date
    ts: datetime
    contract: OptionContract | None
    qty: int
    strike: Decimal | None
    underlying_close: Decimal | None
    shares_delta: int
    cash_delta: Decimal
    new_structure_id: int | None


@dataclass(frozen=True, slots=True)
class UnderlyingFacts:
    """The wheel rules spec §3.1 fields of one underlying on one day (table `underlying_facts`). Every fact
    is None when unknown; `sources` maps a field name to (source, fetched at)."""

    symbol_id: int
    as_of: date
    ticker: str
    security_type: str | None = None  # STOCK | BROAD_INDEX_ETF | SECTOR_ETF | LEVERAGED_OR_INVERSE_ETF
    sector: str | None = None
    price: Decimal | None = None
    eps_ttm: Decimal | None = None
    eps_growth_yoy: Decimal | None = None
    debt_to_equity: Decimal | None = None
    book_value_per_share: Decimal | None = None
    market_cap_usd: Decimal | None = None
    sma50: Decimal | None = None
    sma50_prior: Decimal | None = None
    low_52w: Decimal | None = None
    sessions_since_52w_low: int | None = None
    rsi14: Decimal | None = None
    next_earnings_date: date | None = None
    next_ex_dividend_date: date | None = None
    dividend_per_share: Decimal | None = None
    dividend_yield: Decimal | None = None
    payout_ratio: Decimal | None = None
    short_float: Decimal | None = None
    has_options: bool | None = None
    sources: Mapping[str, tuple[str, datetime]] = field(default_factory=dict)


# --- owner prompts ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptChoice:
    code: str  # one letter of PROMPT_CHOICE_CODES
    label: str


@dataclass(frozen=True, slots=True)
class OwnerPromptRequest:
    kind: str
    scope_key: str
    dedupe_key: str  # at most MAX_DEDUPE_KEY characters
    title: str
    body: str
    choices: tuple[PromptChoice, ...]
    needs_text: bool = False
    default_choice: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PromptView:
    id: int
    source: str
    kind: str
    scope_key: str
    dedupe_key: str
    title: str
    body: str
    choices: tuple[PromptChoice, ...]
    needs_text: bool
    default_choice: str | None
    data: Mapping[str, Any]
    status: PromptStatus
    asked_at: datetime
    last_sent_at: datetime | None
    send_count: int
    answered_at: datetime | None
    answer: str | None
    answer_text: str | None
    answered_via: AnsweredVia | None
    delivered_at: datetime | None


# --- helpers ------------------------------------------------------------------------------------------------


def net_price(
    legs: Sequence[OrderLeg], prices: Mapping[int, Decimal], multipliers: Mapping[int, int]
) -> Decimal:
    """The net price per share of one unit, credit positive. `prices` and `multipliers` are keyed by
    `leg_no`; a shares leg always counts 1 per share, an option leg its contract multiplier (100 when
    `multipliers` does not name the leg). Not rounded. Raises KeyError for a leg without a price."""
    total = ZERO
    for leg in legs:
        m = 1 if leg.instrument == "shares" else multipliers.get(leg.leg_no, SHARES_PER_CONTRACT)
        amount = prices[leg.leg_no] * leg.ratio * m
        total += amount if leg.side == "sell" else -amount
    return total / HUNDRED


def round_tick(value: Decimal, tick: Decimal, favour: Literal["up", "down"]) -> Decimal:
    """`value` on the tick grid: `up` is the next multiple at or above it, `down` at or below. A net price is
    credit positive, so `up` favours the order's owner for a credit and a debit alike."""
    if tick <= 0:
        raise ValueError("tick must be positive")
    steps = (value / tick).to_integral_value(ROUND_CEILING if favour == "up" else ROUND_FLOOR)
    return steps * tick


def dte(expiry: date, today: date) -> int:
    """Calendar days from `today` (the ET session date) to the expiry; negative once it has passed."""
    return (expiry - today).days


def _strike_text(strike: Decimal) -> str:
    exponent = strike.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        return format(strike.normalize(), "f")
    return format(strike.quantize(Decimal("0.01")), "f")


def contract_label(contract: OptionContract) -> str:
    """For example `F 2026-10-30 P 14.50`."""
    right = "C" if contract.right == "call" else "P"
    return f"{contract.underlying} {contract.expiry.isoformat()} {right} {_strike_text(contract.strike)}"
