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
    # QUOTEBAR: the regular session's open, high and low so far (openPrice/highPrice/lowPrice; premarket
    # prints excluded). None before the first regular-hours trade, or when Questrade omits them.
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    # FIX-DAY1: when the app received this quote (the client's clock, right after the response). None for
    # quotes not fetched live (replay, older callers): the fill model then judges only the last trade's age.
    fetched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CandleRequest:
    symbol_id: int
    start: datetime
    end: datetime
    interval: Interval
