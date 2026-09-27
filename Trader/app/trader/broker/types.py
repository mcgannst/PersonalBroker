"""Broker value types shared by the fill model, the simulated broker, strategies and the engine."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, Self

from trader.adapters.questrade.models import QtQuote
from trader.market.types import Candle

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit"]
Purpose = Literal["entry", "stop", "exit"]
TimeInForce = Literal["day", "gtc"]
Q4 = Decimal("0.0001")
ZERO = Decimal("0")
MAX_REASON = 100  # orders.reason and trades.exit_reason are varchar(100)


def dec_str(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def str_dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


@dataclass(frozen=True, slots=True)
class OrderSpec:
    symbol_id: int
    side: Side
    order_type: OrderType
    qty: int
    stop: Decimal | None = None
    limit: Decimal | None = None
    tif: TimeInForce = "day"
    purpose: Purpose = "entry"
    position_id: int | None = None
    proposal_id: int | None = None
    strategy_config_id: int | None = None
    stop_loss: Decimal | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.order_type in ("stop", "stop_limit") and (self.stop is None or self.stop <= 0):
            raise ValueError(f"a {self.order_type} order needs a positive stop price")
        if self.order_type in ("limit", "stop_limit") and (self.limit is None or self.limit <= 0):
            raise ValueError(f"a {self.order_type} order needs a positive limit price")
        if (self.side == "buy") != (self.purpose == "entry"):
            raise ValueError("long only: entries are buys; stops and exits are sells")
        if self.side == "sell" and self.position_id is None:
            raise ValueError("a sell must name the position it closes")
        if self.side == "buy" and self.position_id is not None:
            raise ValueError("a buy opens a new position, so it can't name a position_id")
        if len(self.reason) > MAX_REASON:
            raise ValueError(f"reason is {len(self.reason)} characters, over the {MAX_REASON} allowed")

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "side": self.side,
            "order_type": self.order_type,
            "qty": self.qty,
            "stop": dec_str(self.stop),
            "limit": dec_str(self.limit),
            "tif": self.tif,
            "purpose": self.purpose,
            "position_id": self.position_id,
            "proposal_id": self.proposal_id,
            "strategy_config_id": self.strategy_config_id,
            "stop_loss": dec_str(self.stop_loss),
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Self:
        return cls(
            symbol_id=int(d["symbol_id"]),
            side=d["side"],
            order_type=d["order_type"],
            qty=int(d["qty"]),
            stop=str_dec(d.get("stop")),
            limit=str_dec(d.get("limit")),
            tif=d.get("tif", "day"),
            purpose=d.get("purpose", "entry"),
            position_id=d.get("position_id"),
            proposal_id=d.get("proposal_id"),
            strategy_config_id=d.get("strategy_config_id"),
            stop_loss=str_dec(d.get("stop_loss")),
            reason=d.get("reason", ""),
        )


@dataclass(frozen=True, slots=True)
class Fees:
    commission: Decimal = ZERO
    ecn: Decimal = ZERO
    sec: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.commission + self.ecn + self.sec

    def to_json(self) -> dict[str, str]:
        return {
            "commission": str(self.commission),
            "ecn": str(self.ecn),
            "sec": str(self.sec),
            "total": str(self.total),
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Self:
        return cls(Decimal(str(d["commission"])), Decimal(str(d["ecn"])), Decimal(str(d["sec"])))


@dataclass(frozen=True, slots=True)
class FillDecision:
    price: Decimal
    qty: int
    slippage: Decimal  # per share, always >= 0 (a cost)
    fees: Fees
    quote_snapshot: dict[str, Any]
    trigger: str


@dataclass(frozen=True, slots=True)
class NoFill:
    reason: str
    detail: str = ""


class FillModel(Protocol):
    """Decides whether a working order fills against one piece of market data (master plan §7.1).

    QuoteFillModel (P2-T4) fills from quotes; CandleFillModel (P5-T2) will fill from candles for replay.
    An implementation raises TypeError for a market type it doesn't handle.
    """

    def evaluate(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | None: ...

    def assess(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | NoFill: ...


@dataclass(frozen=True, slots=True)
class FillEvent:
    fill_id: int
    order_id: int
    run_id: int
    symbol_id: int
    side: Side
    purpose: Purpose
    qty: int
    price: Decimal
    ts: datetime
    position_id: int
    strategy_config_id: int | None
    stop_loss: Decimal | None
    proposal_id: int | None
    trade_id: int | None = None
    pnl: Decimal | None = None


Fill = FillEvent  # the name SPEC §5.1 uses in Strategy.on_fill


@dataclass(frozen=True, slots=True)
class PositionView:
    id: int
    symbol_id: int
    strategy_config_id: int | None
    qty: int
    avg_price: Decimal
    stop_loss: Decimal | None
    opened_at: datetime
    session_date: date
    stop_order_id: int | None
    unprotected_since: datetime | None
    unprotected_seconds: int


@dataclass(frozen=True, slots=True)
class OrderView:
    id: int
    symbol_id: int
    side: Side
    order_type: OrderType
    purpose: Purpose
    qty: int
    stop: Decimal | None
    limit: Decimal | None
    status: str
    position_id: int | None
    strategy_config_id: int | None
    proposal_id: int | None
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class AccountState:
    total_cash: Decimal
    settled_cash: Decimal
    buying_power: Decimal
    positions_value: Decimal
    equity: Decimal
