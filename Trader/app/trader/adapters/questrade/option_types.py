"""Questrade option market data as the client returns it (OPTSIM task plan §3.2). Ids here are Questrade
symbol ids, never `option_contracts.id`; `iv_pct` is the percentage Questrade sends (35.2 = 35.2%)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from trader.adapters.questrade.models import QtQuote, QtSymbol
from trader.market.types import Candle, Interval
from trader.options.types import Right


@dataclass(frozen=True, slots=True)
class QtChainStrike:
    strike: Decimal
    call_id: int
    put_id: int


@dataclass(frozen=True, slots=True)
class QtChainRoot:
    root: str
    multiplier: int
    strikes: tuple[QtChainStrike, ...]


@dataclass(frozen=True, slots=True)
class QtChainExpiry:
    expiry: date
    roots: tuple[QtChainRoot, ...]


@dataclass(frozen=True, slots=True)
class QtOptionQuote:
    symbol_id: int
    symbol: str
    underlying: str
    underlying_id: int
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    bid_size: int | None
    ask_size: int | None
    volume: int | None
    open_interest: int | None
    iv_pct: Decimal | None  # as Questrade sends it
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    rho: Decimal | None
    last_trade_time: datetime | None
    delay: int | None  # None when Questrade omits it: unknown, never assume real-time
    is_halted: bool
    vwap: Decimal | None
    fetched_at: datetime  # when the app received this quote (the client's clock)
    requested_at: datetime | None  # when the request that answered it was sent


@dataclass(frozen=True, slots=True)
class QtSymbolDetails:
    symbol_id: int
    symbol: str
    description: str
    security_type: str
    listing_exchange: str
    currency: str
    has_options: bool
    eps: Decimal | None
    pe: Decimal | None
    market_cap: Decimal | None
    dividend: Decimal | None
    ex_date: date | None
    yield_pct: Decimal | None
    industry_sector: str | None
    industry_group: str | None


class OptionQuoteClient(Protocol):
    """The Questrade calls the options side uses: the four option calls (T2 adds them to `QuestradeClient`)
    plus the existing share quotes, candles and symbol lookup."""

    async def option_chain(self, symbol_id: int) -> list[QtChainExpiry]: ...

    async def option_quotes(self, ids: Sequence[int]) -> list[QtOptionQuote]: ...

    async def option_quotes_filter(
        self,
        underlying_id: int,
        expiry: date,
        right: Right,
        min_strike: Decimal | None = None,
        max_strike: Decimal | None = None,
    ) -> list[QtOptionQuote]: ...

    async def symbol_details(self, ids: Sequence[int]) -> dict[int, QtSymbolDetails]: ...

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]: ...

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]: ...

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]: ...
