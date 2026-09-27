"""A short-lived quote cache for the API's position prices (`web.quote_cache_seconds`): one Questrade fetch
per distinct symbol set per TTL, concurrent callers share one in-flight fetch, a failed fetch is not cached.

The dashboard, the positions list and a position's detail page all ask for the same few open positions, and
a phone plus a laptop refetch on every live-update hint; the cache keeps that to one market-data request per
TTL (Questrade allows 20 a second, shared with the worker).
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

import structlog

from trader.adapters.questrade.models import QtQuote
from trader.market.clock import Clock
from trader.notify.views import Quotes

log = structlog.get_logger("api.quotes")

Key = tuple[int, ...]


def _retrieve(task: "asyncio.Task[Mapping[int, QtQuote]]") -> None:
    """Mark a failed fetch's exception as seen even when every caller went away (no asyncio warning)."""
    if not task.cancelled():
        task.exception()


class CachedQuotes:
    """Satisfies `trader.notify.views.Quotes`. Entries are keyed by the sorted distinct symbol ids; the TTL
    is read on every call (a settings change applies at once). Only the last result per key is kept, and
    keys whose entry expired are dropped on the next call, so the cache stays as small as the set of pages
    in use."""

    def __init__(self, fetch: Quotes, clock: Clock, ttl_seconds: Callable[[], float]) -> None:
        self._fetch = fetch
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._cached: dict[Key, tuple[datetime, Mapping[int, QtQuote]]] = {}
        self._inflight: dict[Key, asyncio.Task[Mapping[int, QtQuote]]] = {}

    async def __call__(self, symbol_ids: Sequence[int]) -> Mapping[int, QtQuote]:
        key: Key = tuple(sorted(set(symbol_ids)))
        if not key:
            return {}
        now = self._clock.now()
        ttl = self._ttl_seconds()
        for k, (at, _) in list(self._cached.items()):
            if (now - at).total_seconds() > ttl:
                del self._cached[k]
        hit = self._cached.get(key)
        if hit is not None:
            return hit[1]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._load(key))
            self._inflight[key] = task
            task.add_done_callback(_retrieve)
        # shield: a caller that goes away (a closed request) never cancels the fetch the others wait for
        return await asyncio.shield(task)

    async def _load(self, key: Key) -> Mapping[int, QtQuote]:
        try:
            got = await self._fetch(list(key))
        except BaseException:
            log.warning("api.quotes_fetch_failed", symbols=len(key))
            raise  # not cached: the next call fetches again
        else:
            self._cached[key] = (self._clock.now(), got)
            return got
        finally:
            self._inflight.pop(key, None)
