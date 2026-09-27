"""Strategy plug-in framework (SPEC §5.1, BR-10).

Strategies return intents. They never size positions, place orders or touch the database: ranked candidates
and decision notes go into the context, and the engine (P2-T13) persists them. on_event and on_fill are async
because market data (Questrade) is async.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import AccountState, Fill, OrderView, PositionView, dec_str
from trader.events import Level
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import Candle, Interval, OpenBarStats, OpeningBars, UniverseMember, UniverseStatus

_OFFSET = re.compile(r"(open|close)(?:([+-])(\d{1,3})m(?:(\d{1,2})s)?)?")
MAX_OFFSET_SECONDS = 12 * 3600


@dataclass(frozen=True, slots=True)
class SessionOffset:
    """A time relative to a session's open or close, so early closes work automatically (SPEC §5.1)."""

    anchor: Literal["open", "close"]
    seconds: int

    @classmethod
    def parse(cls, text: str) -> Self:
        m = _OFFSET.fullmatch(text)  # fullmatch: `$` would accept a trailing newline
        if m is None:
            raise ValueError(f"not a session offset: {text!r} (e.g. 'open+5m', 'close-30m', 'open+5m5s')")
        name, sign, minutes, secs = m.groups()
        if secs is not None and int(secs) >= 60:
            raise ValueError(f"seconds must be below 60 in {text!r}")
        total = int(minutes or 0) * 60 + int(secs or 0)
        if total > MAX_OFFSET_SECONDS:
            raise ValueError(f"offset {text!r} is longer than a session")
        anchor: Literal["open", "close"] = "open" if name == "open" else "close"
        return cls(anchor, -total if sign == "-" else total)

    def resolve(self, cal: SessionCalendar, session: date) -> datetime:
        base = cal.session_open(session) if self.anchor == "open" else cal.session_close(session)
        return base + timedelta(seconds=self.seconds)

    def __str__(self) -> str:
        if self.seconds == 0:
            return self.anchor
        minutes, secs = divmod(abs(self.seconds), 60)
        return f"{self.anchor}{'-' if self.seconds < 0 else '+'}{minutes}m" + (f"{secs}s" if secs else "")


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    key: str
    at: SessionOffset


def _require_decimals(intent: object, names: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    """Global Constraints: never float for money. Intents are where a plug-in's prices enter the engine."""
    for name in names:
        value = getattr(intent, name)
        if value is None and name in optional:
            continue
        if not isinstance(value, Decimal):
            raise TypeError(
                f"{type(intent).__name__}.{name} must be a Decimal, not {type(value).__name__} ({value!r})"
            )


@dataclass(frozen=True)
class EnterLong:
    symbol_id: int
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    stop: Decimal | None
    limit: Decimal | None
    stop_loss: Decimal
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_decimals(self, ("stop", "limit", "stop_loss"), optional=("stop", "limit"))


@dataclass(frozen=True)
class Exit:
    position_id: int
    order_type: Literal["market", "stop"]
    stop: Decimal | None
    reason: str

    def __post_init__(self) -> None:
        _require_decimals(self, ("stop",), optional=("stop",))


@dataclass(frozen=True)
class Cancel:
    order_id: int
    reason: str


Intent = EnterLong | Exit | Cancel


def intent_to_json(intent: Intent) -> dict[str, Any]:
    """The intent's order fields as JSON (Decimals as strings).

    `EnterLong.evidence` is deliberately left out: the engine stores it separately (with the signal), so
    this dict stays the order-shaped part that is compared and replayed.
    """
    if isinstance(intent, EnterLong):
        return {
            "type": "enter_long",
            "symbol_id": intent.symbol_id,
            "order_type": intent.order_type,
            "stop": dec_str(intent.stop),
            "limit": dec_str(intent.limit),
            "stop_loss": dec_str(intent.stop_loss),
            "reason": intent.reason,
        }
    if isinstance(intent, Exit):
        return {
            "type": "exit",
            "position_id": intent.position_id,
            "order_type": intent.order_type,
            "stop": dec_str(intent.stop),
            "reason": intent.reason,
        }
    return {"type": "cancel", "order_id": intent.order_id, "reason": intent.reason}


@dataclass
class CandidateRecord:
    """One ranked name and why it was rejected (SPEC §5.2 step 7). Persisted to `candidates` by the engine."""

    symbol_id: int
    rvol: Decimal | None
    rank: int | None
    candle: dict[str, Any] | None
    passed: bool = False
    reject_reason: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecisionNote:
    message: str
    level: Level = "info"
    data: dict[str, Any] = field(default_factory=dict)


class MarketDataView(Protocol):
    async def universe(self, session_date: date) -> list[UniverseMember]: ...
    async def universe_status(self, session_date: date) -> UniverseStatus: ...
    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]: ...
    async def opening_bars(
        self, session_date: date, symbol_ids: Sequence[int] | None = None
    ) -> OpeningBars: ...
    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]: ...
    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]: ...
    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None: ...
    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]: ...


class CatalystInfo(Protocol):
    @property
    def catalyst_type(self) -> str: ...
    @property
    def direction(self) -> str: ...
    @property
    def quality(self) -> int | None: ...
    @property
    def classified(self) -> bool: ...


class CatalystSource(Protocol):
    async def get(self, symbol_ids: Sequence[int], session_date: date) -> Mapping[int, CatalystInfo]: ...


@dataclass
class StrategyContext:
    clock: Clock
    calendar: SessionCalendar
    session_date: date
    data: MarketDataView
    catalysts: CatalystSource
    params: BaseModel
    strategy_config_id: int
    positions: list[PositionView]
    working_orders: list[OrderView]
    account: AccountState
    entries_today: int = 0
    candidates: list[CandidateRecord] = field(default_factory=list)
    notes: list[DecisionNote] = field(default_factory=list)

    def note(self, message: str, level: Level = "info", **data: Any) -> None:
        self.notes.append(DecisionNote(message, level, data))


@runtime_checkable
class Strategy(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def kind(self) -> Literal["entry", "overlay"]: ...
    @property
    def params_model(self) -> type[BaseModel]: ...
    @property
    def params(self) -> BaseModel: ...

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]: ...
    async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]: ...
    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]: ...
