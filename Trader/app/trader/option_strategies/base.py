"""The option strategy plug-in framework (OPTSIM task plan §3.4, §3.5).

A plug-in is a class found through the entry-point group `trader.option_strategies`, constructed as
`cls(params)`. Its hooks return intents; the host (T7) turns them into orders. A plug-in never sizes
collateral, places an order or touches core tables: it reads the context, keeps its own tables and state,
asks the owner through prompts and shows itself through a generic panel.

`ScheduledEvent` and `SessionOffset` are the stock framework's, reused unchanged.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from trader.events import Level
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.options.settings import OptionSettings
from trader.options.types import (
    MAX_REASON,
    LegSpec,
    LifecycleEvent,
    OptionAccountState,
    OptionFillEvent,
    OptOrderType,
    OptOrderView,
    OwnerPromptRequest,
    PromptView,
    StructureView,
    Tif,
    require_decimals,
)
from trader.strategies.base import DecisionNote, ScheduledEvent, SessionOffset

if TYPE_CHECKING:
    from trader.options.protocols import OptionMarketView

__all__ = [
    "ENTRY_POINT_GROUP",
    "KEY_PATTERN",
    "POSTCLOSE_EVENT",
    "CancelOrder",
    "CloseStructure",
    "DecisionNote",
    "KeyValue",
    "OpenStructure",
    "OptionEvent",
    "OptionIntent",
    "OptionStrategy",
    "OptionStrategyConfigView",
    "OptionStrategyContext",
    "PanelAction",
    "PanelActionRequest",
    "PanelActionResult",
    "PanelColumn",
    "PanelRow",
    "PanelTable",
    "Reprice",
    "RollStructure",
    "ScheduledEvent",
    "SellShares",
    "SessionOffset",
    "StrategyAlert",
    "StrategyPanel",
    "StrategyState",
]

ENTRY_POINT_GROUP = "trader.option_strategies"
# A strategy key (equal to its entry-point name) and an event key. At most 20 characters, so the job name
# `opt_event:<strategy>:<key>` fits job_runs.job (varchar(50)).
KEY_PATTERN = r"^[a-z][a-z0-9_]{0,19}$"
POSTCLOSE_EVENT = "postclose"  # the built-in event the post-close job fires after expiry handling


@dataclass(frozen=True, slots=True)
class OptionEvent:
    key: str
    session_date: date
    scheduled: bool = True  # False for a manual or cron-fired event


# --- intents ------------------------------------------------------------------------------------------------


def _check_reason(intent: object, reason: str) -> None:
    if len(reason) > MAX_REASON:
        raise ValueError(
            f"{type(intent).__name__}.reason is {len(reason)} characters, over the {MAX_REASON} allowed"
        )


@dataclass(frozen=True)
class OpenStructure:
    legs: tuple[LegSpec, ...]
    qty: int
    order_type: OptOrderType
    net_limit: Decimal | None
    tif: Tif
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    walk: bool = False
    take_profit_pct: Decimal | None = None

    def __post_init__(self) -> None:
        require_decimals(self, ("net_limit", "take_profit_pct"), optional=("net_limit", "take_profit_pct"))
        _check_reason(self, self.reason)


@dataclass(frozen=True)
class CloseStructure:
    structure_id: int
    order_type: OptOrderType
    net_limit: Decimal | None
    reason: str
    tif: Tif = "day"
    walk: bool = False
    qty: int | None = None  # None = all

    def __post_init__(self) -> None:
        require_decimals(self, ("net_limit",), optional=("net_limit",))
        _check_reason(self, self.reason)


@dataclass(frozen=True)
class RollStructure:
    structure_id: int
    open_legs: tuple[LegSpec, ...]
    order_type: OptOrderType
    net_limit: Decimal | None
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    tif: Tif = "day"
    walk: bool = False
    take_profit_pct: Decimal | None = None

    def __post_init__(self) -> None:
        require_decimals(self, ("net_limit", "take_profit_pct"), optional=("net_limit", "take_profit_pct"))
        _check_reason(self, self.reason)


@dataclass(frozen=True)
class Reprice:
    order_id: int
    net_limit: Decimal

    def __post_init__(self) -> None:
        require_decimals(self, ("net_limit",))


@dataclass(frozen=True)
class CancelOrder:
    order_id: int
    reason: str

    def __post_init__(self) -> None:
        _check_reason(self, self.reason)


@dataclass(frozen=True)
class SellShares:
    """Sell a `shares` structure with a market order (at the bid)."""

    structure_id: int
    reason: str

    def __post_init__(self) -> None:
        _check_reason(self, self.reason)


OptionIntent = OpenStructure | CloseStructure | RollStructure | Reprice | CancelOrder | SellShares


# --- the strategy panel (§3.5): drawn generically by the web ------------------------------------------------

Tone = Literal["ok", "warn", "bad"]
PanelColumnKind = Literal["text", "money", "number", "date", "badge", "bool"]
PanelActionKind = Literal["button", "toggle", "text", "choice"]


@dataclass(frozen=True, slots=True)
class KeyValue:
    label: str
    value: str
    tone: Tone | None = None


@dataclass(frozen=True, slots=True)
class PanelColumn:
    key: str
    label: str
    kind: PanelColumnKind = "text"


@dataclass(frozen=True, slots=True)
class PanelRow:
    id: str
    cells: Mapping[str, Any]  # column key -> a JSON value
    actions: tuple[str, ...] = ()  # the action keys offered on this row
    detail: tuple[KeyValue, ...] = ()  # shown when the row is expanded


@dataclass(frozen=True, slots=True)
class PanelTable:
    key: str
    title: str
    columns: tuple[PanelColumn, ...]
    rows: tuple[PanelRow, ...]
    empty_text: str = ""


@dataclass(frozen=True, slots=True)
class PanelAction:
    key: str
    label: str
    kind: PanelActionKind = "button"
    confirm: bool = False
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StrategyPanel:
    summary: tuple[KeyValue, ...] = ()
    tables: tuple[PanelTable, ...] = ()
    actions: tuple[PanelAction, ...] = ()


@dataclass(frozen=True, slots=True)
class PanelActionRequest:
    action: str
    row_id: str | None = None
    value: str | bool | None = None


@dataclass(frozen=True, slots=True)
class PanelActionResult:
    ok: bool
    message: str


# --- state, config, context ---------------------------------------------------------------------------------


class StrategyState(Protocol):
    """A plug-in's own key-value state (table `option_strategy_state`, scoped to its strategy key)."""

    def get(self, scope_key: str) -> dict[str, Any] | None: ...

    def put(self, scope_key: str, value: dict[str, Any]) -> None: ...

    def delete(self, scope_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class OptionStrategyConfigView:
    id: int
    strategy_key: str
    version: str
    revision: int
    params: dict[str, Any]
    enabled: bool
    created_at: datetime
    created_by: str


@dataclass(frozen=True, slots=True)
class StrategyAlert:
    kind: str
    message: str
    dedupe_key: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class OptionStrategyContext:
    """What a hook sees. The host builds one per call; `prompt` and `equity_on` are the host's lookups."""

    clock: Clock
    calendar: SessionCalendar
    session_date: date
    run_id: int
    strategy_key: str
    config_id: int
    params: BaseModel
    settings: OptionSettings
    market: "OptionMarketView"
    account: OptionAccountState
    structures: list[StructureView]  # this strategy's, open
    orders: list[OptOrderView]  # this strategy's, working
    state: StrategyState
    factory: sessionmaker[Session]  # for the plug-in's own tables only
    usd_cad_rate: Decimal
    prompt: Callable[[str], PromptView | None]  # this strategy's prompt by dedupe key
    equity_on: Callable[[date], Decimal | None]  # the account value at that session's close
    notes: list[DecisionNote] = field(default_factory=list)
    alerts: list[StrategyAlert] = field(default_factory=list)

    def note(self, message: str, level: Level = "info", **data: Any) -> None:
        self.notes.append(DecisionNote(message, level, data))

    def alert(self, kind: str, message: str, dedupe_key: str, **data: Any) -> None:
        self.alerts.append(StrategyAlert(kind, message, dedupe_key, data))


@runtime_checkable
class OptionStrategy(Protocol):
    """An option strategy plug-in. `key` matches KEY_PATTERN and equals the entry-point name."""

    @property
    def key(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def params_model(self) -> type[BaseModel]: ...
    @property
    def manual_events(self) -> tuple[str, ...]: ...

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]: ...

    async def watch_underlyings(self, ctx: OptionStrategyContext) -> set[str]: ...

    async def on_event(self, ctx: OptionStrategyContext, event: OptionEvent) -> list[OptionIntent]: ...

    async def on_fill(self, ctx: OptionStrategyContext, fill: OptionFillEvent) -> list[OptionIntent]: ...

    async def on_lifecycle(self, ctx: OptionStrategyContext, event: LifecycleEvent) -> list[OptionIntent]: ...

    async def on_answer(self, ctx: OptionStrategyContext, prompt: PromptView) -> list[OptionIntent]: ...

    async def prompts(self, ctx: OptionStrategyContext) -> list[OwnerPromptRequest]: ...

    async def panel(self, ctx: OptionStrategyContext) -> StrategyPanel: ...

    async def on_action(self, ctx: OptionStrategyContext, req: PanelActionRequest) -> PanelActionResult: ...
