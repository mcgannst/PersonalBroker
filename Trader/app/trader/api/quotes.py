"""A short-lived quote cache for the API's position prices (`web.quote_cache_seconds`): one Questrade fetch
per distinct symbol set per TTL, concurrent callers share one in-flight fetch, a failed fetch is not cached.

Stub (P4-T1): T5 implements it.
"""

from collections.abc import Callable, Mapping, Sequence

from trader.adapters.questrade.models import QtQuote
from trader.market.clock import Clock
from trader.notify.views import Quotes


class CachedQuotes:
    """Satisfies `trader.notify.views.Quotes`."""

    def __init__(self, fetch: Quotes, clock: Clock, ttl_seconds: Callable[[], float]) -> None:
        self._fetch = fetch
        self._clock = clock
        self._ttl_seconds = ttl_seconds

    async def __call__(self, symbol_ids: Sequence[int]) -> Mapping[int, QtQuote]:
        raise NotImplementedError("P4-T5")
