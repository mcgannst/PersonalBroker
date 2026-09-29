"""`QuoteTap`: a transparent wrapper around the worker's Questrade client that remembers the quotes it
returned (live dashboard plan S1a, S10). DB-T1 stub with the final signatures; DB-T2 implements it.

Rules (S1a): each proxied method is a plain `async def` whose only `await` is the inner call with the same
arguments; it returns the inner result itself and re-raises the inner exception unchanged; its bookkeeping is
synchronous, bounded and does no I/O.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.market.clock import Clock
from trader.market.data_service import QuoteClient
from trader.market.types import Candle, Interval
from trader.marks.types import ObservedQuote


class QuoteTap:
    """Satisfies `QuoteClient` (and `LatestQuotes` through `drain`)."""

    def __init__(self, inner: QuoteClient, clock: Clock) -> None:
        self._inner = inner
        self._clock = clock

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        raise NotImplementedError("DB-T2")

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        raise NotImplementedError("DB-T2")

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        raise NotImplementedError("DB-T2")

    @property
    def stats(self) -> Any:
        """The inner client's `stats` attribute on every access (None when it has none)."""
        raise NotImplementedError("DB-T2")

    def drain(self) -> list[ObservedQuote]:
        """The undrained observations in observation order; the tap starts empty again."""
        raise NotImplementedError("DB-T2")

    def health_detail(self) -> dict[str, Any]:
        """The heartbeat's `questrade` and `candle_batches` keys (S10)."""
        raise NotImplementedError("DB-T2")
