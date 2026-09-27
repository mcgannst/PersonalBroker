"""P4-T5 acceptance test 10: `CachedQuotes`, the API's short-lived quote cache (`web.quote_cache_seconds`)."""

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.adapters.questrade.models import QtQuote
from trader.api.quotes import CachedQuotes
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)


class CountingQuotes:
    """A `Quotes` callable that counts fetches; `gate` (when set) holds every fetch until it is set;
    `fail_next` makes the next fetch raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, ...]] = []
        self.gate: asyncio.Event | None = None
        self.fail_next = False

    async def __call__(self, symbol_ids: Sequence[int]) -> Mapping[int, QtQuote]:
        self.calls.append(tuple(symbol_ids))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("questrade down")
        return {
            sid: QtQuote(sid, f"S{sid}", None, None, Decimal(sid), None, 0, None, 0, False, None)
            for sid in symbol_ids
        }


def _cache(fetch: CountingQuotes, clock: FixedClock, ttl: float = 5.0) -> CachedQuotes:
    return CachedQuotes(fetch, clock, lambda: ttl)


async def test_two_calls_within_the_ttl_fetch_once() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    cache = _cache(fetch, clock)
    first = await cache([1, 2])
    clock.advance(timedelta(seconds=4))
    second = await cache([2, 1, 2])  # the same distinct set, in another order
    assert len(fetch.calls) == 1
    assert first == second and first[1].last == Decimal(1)


async def test_after_the_ttl_it_fetches_again() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    cache = _cache(fetch, clock)
    await cache([1])
    clock.advance(timedelta(seconds=5, milliseconds=1))
    await cache([1])
    assert len(fetch.calls) == 2


async def test_the_ttl_is_read_on_every_call() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    ttl = [5.0]
    cache = CachedQuotes(fetch, clock, lambda: ttl[0])
    await cache([1])
    clock.advance(timedelta(seconds=2))
    ttl[0] = 1.0  # the setting changed
    await cache([1])
    assert len(fetch.calls) == 2


async def test_a_different_symbol_set_is_a_different_entry() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    cache = _cache(fetch, clock)
    await cache([1])
    await cache([1, 2])
    await cache([1])
    assert fetch.calls == [(1,), (1, 2)]


async def test_concurrent_calls_share_one_fetch() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    fetch.gate = asyncio.Event()
    cache = _cache(fetch, clock)
    tasks = [asyncio.create_task(cache([3, 4])) for _ in range(3)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    fetch.gate.set()
    results = await asyncio.gather(*tasks)
    assert len(fetch.calls) == 1
    assert all(r == results[0] for r in results) and set(results[0]) == {3, 4}


async def test_a_failed_fetch_is_not_cached() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    cache = _cache(fetch, clock)
    fetch.fail_next = True
    with pytest.raises(RuntimeError):
        await cache([1])
    got = await cache([1])  # retried at once, not served from a cached failure
    assert len(fetch.calls) == 2 and got[1].last == Decimal(1)


async def test_concurrent_callers_all_see_a_failure_then_the_next_call_retries() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    fetch.gate = asyncio.Event()
    fetch.fail_next = True
    cache = _cache(fetch, clock)
    tasks = [asyncio.create_task(cache([1])) for _ in range(2)]
    await asyncio.sleep(0)
    fetch.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results) and len(fetch.calls) == 1
    await cache([1])
    assert len(fetch.calls) == 2


async def test_a_cancelled_caller_does_not_cancel_the_shared_fetch() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    fetch.gate = asyncio.Event()
    cache = _cache(fetch, clock)
    first = asyncio.create_task(cache([1]))
    second = asyncio.create_task(cache([1]))
    await asyncio.sleep(0)
    first.cancel()
    fetch.gate.set()
    got = await second
    assert got[1].last == Decimal(1) and len(fetch.calls) == 1


async def test_no_symbols_fetches_nothing() -> None:
    fetch, clock = CountingQuotes(), FixedClock(NOW)
    assert await _cache(fetch, clock)([]) == {}
    assert fetch.calls == []
