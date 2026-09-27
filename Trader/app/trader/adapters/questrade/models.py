from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trader.market.types import Interval


@dataclass(frozen=True, slots=True)
class QtSymbol:
    symbol_id: int
    symbol: str
    listing_exchange: str
    currency: str
    description: str
    is_tradable: bool
    is_quotable: bool


@dataclass(frozen=True, slots=True)
class QtQuote:
    symbol_id: int
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    last_regular: Decimal | None
    volume: int
    last_trade_time: datetime | None
    delay: int | None  # None when Questrade omits it: unknown, never assume real-time
    is_halted: bool
    vwap: Decimal | None


@dataclass(frozen=True, slots=True)
class CandleRequest:
    symbol_id: int
    start: datetime
    end: datetime
    interval: Interval
