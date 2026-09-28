"""An in-memory Questrade client for tests. IDs here are Questrade symbol IDs, as at the real boundary."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtQuote, QtSymbol
from trader.market.types import Candle, Interval


class FakeQuestrade:
    def __init__(self) -> None:
        self.symbols: dict[str, QtSymbol] = {}
        self.quote_map: dict[int, QtQuote] = {}
        self.bars: dict[tuple[int, str], list[Candle]] = {}
        self.errors: dict[int, int] = {}
        self.calls: list[tuple[str, int]] = []

    def add_symbol(
        self, ticker: str, qt_id: int, *, currency: str = "USD", exchange: str = "NASDAQ"
    ) -> QtSymbol:
        sym = QtSymbol(qt_id, ticker, exchange, currency, f"{ticker} Inc", True, True)
        self.symbols[ticker] = sym
        return sym

    def set_quote(self, qt_id: int, bid: str, ask: str, last: str, at: datetime, *, age: float = 1.0) -> None:
        ticker = next((t for t, s in self.symbols.items() if s.symbol_id == qt_id), f"Q{qt_id}")
        self.quote_map[qt_id] = QtQuote(
            symbol_id=qt_id,
            symbol=ticker,
            bid=Decimal(bid),
            ask=Decimal(ask),
            last=Decimal(last),
            last_regular=Decimal(last),
            volume=100_000,
            last_trade_time=at - timedelta(seconds=age),
            delay=0,
            is_halted=False,
            vwap=None,
        )

    def add_bars(self, qt_id: int, interval: Interval, candles: Sequence[Candle]) -> None:
        self.bars.setdefault((qt_id, interval), []).extend(candles)

    def _candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]:
        if symbol_id in self.errors:
            raise QuestradeApiError(self.errors[symbol_id], "fake error")
        found = [c for c in self.bars.get((symbol_id, interval), []) if start <= c.start < end]
        return sorted(found, key=lambda c: c.start)

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        self.calls.append(("symbols_by_names", len(names)))
        return {n: self.symbols[n] for n in names if n in self.symbols}

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        self.calls.append(("quotes", len(ids)))
        return [self.quote_map[i] for i in ids if i in self.quote_map]

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        self.calls.append(("candles", 1))
        return self._candles(symbol_id, start, end, interval)

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.calls.append(("candles_many", len(reqs)))
        out: dict[CandleRequest, list[Candle] | QuestradeApiError] = {}
        for r in reqs:
            try:
                out[r] = self._candles(r.symbol_id, r.start, r.end, r.interval)
            except QuestradeApiError as exc:
                out[r] = exc
        return out
