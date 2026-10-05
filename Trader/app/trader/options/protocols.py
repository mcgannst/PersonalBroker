"""The core protocols of the options simulation (OPTSIM task plan §3.3). Every service is built against
these, so each task can be written and tested against the fakes in `tests/options/fakes.py`.

Methods are async unless the protocol says sync. Money is `Decimal`; times come from the injected `Clock`.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from trader.adapters.questrade.models import QtQuote
from trader.jobs.runner import JobOutcome
from trader.market.types import Candle
from trader.notify.types import Buttons, OutboundMessage
from trader.option_strategies.base import (
    OptionEvent,
    OptionStrategy,
    OptionStrategyConfigView,
    PanelActionRequest,
    PanelActionResult,
    StrategyPanel,
)
from trader.options.settings import OptionSettings, OptionSettingsStore
from trader.options.types import (
    AnsweredVia,
    ChainStrike,
    CloseReason,
    CollateralDecision,
    ContractKey,
    ExpiryInfo,
    FillDecision,
    Instrument,
    LegQuote,
    LifecycleEvent,
    LifecycleKind,
    NoFill,
    OptionAccountState,
    OptionContract,
    OptionFillEvent,
    OptionQuote,
    OptOrderView,
    OptPositionView,
    OrderRequest,
    OrderStatus,
    OwnerPromptRequest,
    PromptView,
    Right,
    StructureKind,
    StructureView,
    SubmitResult,
    UnderlyingFacts,
)

CashKind = Literal["buy", "sell", "fee"]
AnswerStatus = Literal["ok", "already", "unknown", "invalid_choice", "text_required"]


class UnknownUnderlying(LookupError):
    """The ticker is not a known symbol, has no Questrade id, or has no options. Raised by every
    `OptionMarketView` method that takes an underlying (the API answers 404)."""


class UnknownContract(LookupError):
    """`OptionMarketView.contract` was given an id that is not in the contract master."""


class OptionMarketView(Protocol):
    """Contracts, chains, quotes, facts and the calendar maths for options (T3 `OptionMarketService`).
    Contract ids are `option_contracts.id`; `underlying` is a ticker."""

    async def expiries(self, underlying: str) -> list[ExpiryInfo]: ...

    async def strikes(self, underlying: str, expiry: date) -> list[ChainStrike]: ...

    async def contract(self, contract_id: int) -> OptionContract: ...

    async def find_contract(self, key: ContractKey) -> OptionContract | None: ...

    async def quotes(self, contract_ids: Sequence[int]) -> dict[int, OptionQuote]: ...

    async def quotes_for_expiry(
        self,
        underlying: str,
        expiry: date,
        right: Right,
        min_strike: Decimal | None = None,
        max_strike: Decimal | None = None,
    ) -> list[tuple[OptionContract, OptionQuote]]: ...

    async def underlying_quote(self, underlying: str) -> QtQuote | None: ...

    async def facts(self, underlying: str) -> UnderlyingFacts | None: ...

    async def daily_bars(self, underlying: str, start: date, end: date) -> list[Candle]: ...

    def is_open(self, now: datetime) -> bool: ...

    def is_monthly(self, expiry: date) -> bool: ...


class OptionBook(Protocol):
    """The option run's positions, structures and cash inside ONE transaction (sync; T5 `DbBook`). The
    caller holds the book lock. `tests/options/contract_book.py` is the behaviour every book must pass.

    - `apply` adds a signed quantity to the structure's position in that instrument and contract (a new
      position when there is none): adding re-averages the price (kept to 4 decimals); reducing realizes
      `(price - avg) x closed qty x multiplier` for a long and the reverse for a short (multiplier 1 for
      shares), on the position and its structure; a position at zero gets `closed_at`; crossing through
      zero raises ValueError. It never moves cash.
    - A `StructureView` lists every position the structure ever had, in the order they were opened; a
      closed position stays in it with `qty` 0.
    - `move_cash`: `amount` is signed as the cash ledger wants it: `sell` > 0, `buy` < 0, `fee` < 0 (else
      ValueError). A `fee` given a `structure_id` is added to that structure's `fees_total`.
    - `reserved()` is the reserved cash of the open structures plus the reservations of working orders.
    - `close_structure` also releases the structure's reserve.
    - `record_lifecycle` returns the new event's id, or None when an event of that position, kind and
      session date already exists (then nothing was written).
    """

    def cash(self) -> Decimal: ...

    def reserved(self) -> Decimal: ...

    def add_structure(
        self,
        *,
        kind: StructureKind,
        source: str,
        strategy_config_id: int | None,
        underlying: str,
        qty: int,
        entry_net: Decimal,
        reserved_cash: Decimal,
        cover_structure_id: int | None,
        parent_structure_id: int | None,
        take_profit_net: Decimal | None,
        meta: Mapping[str, Any],
        ts: datetime,
    ) -> int: ...

    def apply(
        self,
        structure_id: int,
        instrument: Instrument,
        contract_id: int | None,
        qty_delta: int,
        price: Decimal,
        ts: datetime,
    ) -> OptPositionView: ...

    def move_cash(
        self, amount: Decimal, kind: CashKind, ref: str, ts: datetime, *, structure_id: int | None = None
    ) -> None: ...

    def set_reserved(self, structure_id: int, amount: Decimal) -> None: ...

    def close_structure(self, structure_id: int, close_reason: CloseReason, ts: datetime) -> None: ...

    def freeze(self, structure_id: int, detail: str) -> None: ...

    def structure(self, structure_id: int) -> StructureView: ...

    def structures(
        self, *, open_only: bool = True, source: str | None = None, underlying: str | None = None
    ) -> list[StructureView]: ...

    def record_lifecycle(
        self,
        *,
        structure_id: int,
        position_id: int,
        contract_id: int | None,
        kind: LifecycleKind,
        session_date: date,
        ts: datetime,
        underlying_close: Decimal | None,
        strike: Decimal | None,
        qty: int,
        shares_delta: int,
        cash_delta: Decimal,
        detail: Mapping[str, Any],
    ) -> int | None: ...

    def link_lifecycle(self, event_id: int, new_structure_id: int) -> None: ...


class OptionBroker(Protocol):
    """Orders from submit to fill for the active options run (T5 `SimOptionBroker`)."""

    async def preview(self, req: OrderRequest) -> CollateralDecision: ...

    async def submit(self, req: OrderRequest) -> SubmitResult: ...

    async def cancel(self, order_id: int, reason: str, actor: str) -> bool: ...

    async def reprice(self, order_id: int, net_limit: Decimal, actor: str) -> bool: ...

    async def poll(self, now: datetime) -> list[OptionFillEvent]: ...

    async def walk(self, now: datetime) -> int: ...

    async def take_profits(self, now: datetime) -> list[int]: ...

    async def expire_day_orders(self, session_date: date, now: datetime) -> int: ...

    async def account(self) -> OptionAccountState: ...

    async def structures(
        self, *, source: str | None = None, open_only: bool = True
    ) -> list[StructureView]: ...

    async def orders(
        self, *, status: OrderStatus | None = None, source: str | None = None, limit: int = 200
    ) -> list[OptOrderView]: ...

    async def order(self, order_id: int) -> OptOrderView | None: ...


@dataclass(frozen=True, slots=True)
class CollateralBook:
    """Everything the collateral engine may look at: a snapshot, so the engine does no I/O."""

    account: OptionAccountState
    structures: Sequence[StructureView]  # every open structure of the run
    working_orders: Sequence[OptOrderView]
    contracts: Mapping[int, OptionContract]
    quotes: Mapping[int, OptionQuote]  # by contract id
    share_quotes: Mapping[str, QtQuote]  # by underlying ticker
    settings: OptionSettings
    today: date


class CollateralEngine(Protocol):
    """Sync and pure (T4 `DefaultCollateralEngine`)."""

    def evaluate(self, req: OrderRequest, book: CollateralBook) -> CollateralDecision: ...


class OptionFillModel(Protocol):
    """Sync and pure (T5 `QuoteFillModel`). `quotes` is keyed by `leg_no`."""

    def assess(
        self,
        order: OptOrderView,
        quotes: Mapping[int, LegQuote],
        now: datetime,
        session_open: datetime,
        session_close: datetime,
        settings: OptionSettings,
    ) -> FillDecision | NoFill: ...


class FactsProvider(Protocol):
    """T8 `FactsService`. `refresh` maps each ticker to its facts, or to the error text when every source
    failed for it."""

    async def get(self, underlying: str) -> UnderlyingFacts | None: ...

    async def refresh(self, underlyings: Sequence[str]) -> dict[str, UnderlyingFacts | str]: ...


@dataclass(frozen=True, slots=True)
class AnswerResult:
    status: AnswerStatus
    prompt: PromptView | None


class PromptStore(Protocol):
    """The owner's questions (sync; T10 `DbPromptStore`, table `owner_prompts`)."""

    def ensure(self, run_id: int, source: str, req: OwnerPromptRequest) -> PromptView: ...

    def get(self, prompt_id: int) -> PromptView | None: ...

    def by_key(self, dedupe_key: str) -> PromptView | None: ...

    def pending(self, source: str | None = None) -> list[PromptView]: ...

    def answer(
        self, prompt_id: int, choice: str, *, text: str | None = None, via: AnsweredVia, actor: str
    ) -> AnswerResult: ...

    def undelivered(self, source: str) -> list[PromptView]: ...

    def mark_delivered(self, prompt_id: int) -> None: ...

    def due_for_send(self, now: datetime) -> list[PromptView]: ...

    def mark_sent(self, prompt_id: int, now: datetime) -> None: ...

    def cancel(self, dedupe_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class OptionSummaryView:
    session_date: date
    account: OptionAccountState
    day_change: Decimal | None
    fills: int
    lifecycle: tuple[LifecycleEvent, ...]
    open_structures: int
    pending_prompts: int
    by_source: Mapping[str, Decimal]  # realized today


class OptionRenderer(Protocol):
    """Every option message (sync and pure; T10 `OptionMessages`)."""

    def fill(self, fill: OptionFillEvent, structure: StructureView) -> OutboundMessage: ...

    def lifecycle(self, event: LifecycleEvent) -> OutboundMessage: ...

    def alert(self, source: str, kind: str, message: str, dedupe_key: str) -> OutboundMessage: ...

    def prompt(self, prompt: PromptView, buttons: Buttons) -> OutboundMessage: ...

    def summary(self, view: OptionSummaryView) -> OutboundMessage: ...


class StrategyHost(Protocol):
    """Runs the plug-ins' hooks (T7 `DefaultStrategyHost`)."""

    async def ensure_defaults(self) -> None: ...

    async def due_events(self, session_date: date, now: datetime) -> list[tuple[str, OptionEvent]]: ...

    async def fire(self, strategy_key: str, event: OptionEvent, *, force: bool = False) -> JobOutcome: ...

    async def deliver_fill(self, fill: OptionFillEvent) -> None: ...

    async def deliver_lifecycle(self, event: LifecycleEvent) -> None: ...

    async def deliver_answers(self) -> int: ...

    async def sync_prompts(self) -> int: ...

    async def watch_underlyings(self) -> set[str]: ...

    async def panel(self, strategy_key: str) -> StrategyPanel: ...

    async def action(self, strategy_key: str, req: PanelActionRequest, actor: str) -> PanelActionResult: ...


class OptionStrategyRegistryView(Protocol):
    """The registry as the API reads it (sync; T7 `OptionStrategyRegistry`). `plugin_class`, `json_schema`
    and `current` raise KeyError for an unknown key; `update` raises pydantic's ValidationError."""

    def keys(self) -> list[str]: ...

    def plugin_class(self, key: str) -> type[OptionStrategy]: ...

    def json_schema(self, key: str) -> dict[str, Any]: ...

    def current(self, key: str) -> OptionStrategyConfigView: ...

    def update(
        self, key: str, *, params: dict[str, Any] | None = None, enabled: bool | None = None, actor: str
    ) -> OptionStrategyConfigView: ...


@dataclass(frozen=True, slots=True)
class OptionApiServices:
    """What the options routes use (`ApiServices.options`; T16 builds it, tests build it from the fakes)."""

    run_id: Callable[[], int | None]  # the active options run, read now
    broker: OptionBroker
    market: OptionMarketView
    prompts: PromptStore
    settings: OptionSettingsStore
    registry: OptionStrategyRegistryView
    host: StrategyHost
