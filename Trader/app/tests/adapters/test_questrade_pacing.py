"""FIX-PACING: market data is paced under Questrade's 20 req/s limit and a 429 pause follows
X-RateLimit-Reset (2026-09-28 9:35 ET opening-bar slowdown). Never touches the network (respx + fakes)."""

import asyncio
import heapq
import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import (
    MARKET_RPS,
    MAX_429_PAUSE,
    QuestradeApiError,
    QuestradeClient,
)
from trader.adapters.questrade.models import CandleRequest
from trader.market.clock import FixedClock

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 9, 28, 13, 35, 5, tzinfo=UTC)
START = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
FETCH_DEADLINE_S = 45.0


class FakeTokens:
    def access(self) -> AccessToken:
        return AccessToken("tok-1", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self) -> AccessToken:
        return self.access()


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def reset_in(seconds: float) -> dict[str, str]:
    return {"X-RateLimit-Reset": str(NOW.timestamp() + seconds), "X-RateLimit-Remaining": "0"}


def pauses(c: QuestradeClient) -> float:
    return c.stats["market"].pause_s


# --- pacing -------------------------------------------------------------------------------------------


def test_default_market_rate_is_below_the_20_per_second_limit() -> None:
    assert MARKET_RPS == 17.0


@respx.mock
async def test_market_calls_are_paced_at_17_per_second_on_a_fake_monotonic_clock() -> None:
    t = {"now": 100.0}
    dispatched: list[float] = []

    async def fake_sleep(s: float) -> None:
        t["now"] += s

    def handler(request: httpx.Request) -> httpx.Response:
        dispatched.append(t["now"])
        return httpx.Response(200, json={"quotes": []})

    respx.get(BASE + "markets/quotes").mock(side_effect=handler)
    async with QuestradeClient(
        FakeTokens(), FixedClock(NOW), sleep=fake_sleep, monotonic=lambda: t["now"]
    ) as c:
        for _ in range(35):
            await c.quotes([1])
    gaps = [b - a for a, b in itertools.pairwise(dispatched)]
    assert gaps == pytest.approx([1 / 17] * 34)
    assert dispatched[-1] - dispatched[0] == pytest.approx(2.0)


# --- 429 pause ----------------------------------------------------------------------------------------


@respx.mock
async def test_429_pause_follows_a_near_reset_plus_a_small_margin() -> None:
    """Reset says the per-second window clears in 0.2 s: pause ~0.25 s, not the old 0.5 s floor."""
    sleeps = Sleeps()
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(429, headers=reset_in(0.2)),
            httpx.Response(200, json={"quotes": []}),
        ]
    )
    async with QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=sleeps) as c:
        assert await c.quotes([1]) == []
        assert pauses(c) == pytest.approx(0.25, abs=0.01)
        assert c.stats["market"].http_429 == 1
    assert max(sleeps.calls) == pytest.approx(0.25, abs=0.01)


@respx.mock
async def test_429_pause_does_not_grow_with_the_attempt_when_reset_is_near() -> None:
    sleeps = Sleeps()
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[httpx.Response(429, headers=reset_in(0.1)) for _ in range(3)]
        + [httpx.Response(200, json={"quotes": []})]
    )
    async with QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=sleeps) as c:
        assert await c.quotes([1]) == []
        assert pauses(c) == pytest.approx(3 * 0.15, abs=0.01)


@respx.mock
async def test_429_with_a_reset_now_or_just_passed_pauses_the_minimum() -> None:
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(429, headers=reset_in(0.0)),
            httpx.Response(429, headers=reset_in(-0.3)),
            httpx.Response(200, json={"quotes": []}),
        ]
    )
    async with QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=Sleeps()) as c:
        assert await c.quotes([1]) == []
        assert pauses(c) == pytest.approx(0.05 + 0.05, abs=0.01)


@respx.mock
async def test_429_with_a_reset_under_2s_is_capped_at_2s() -> None:
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(429, headers=reset_in(1.99)),
            httpx.Response(200, json={"quotes": []}),
        ]
    )
    async with QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=Sleeps()) as c:
        assert await c.quotes([1]) == []
        assert pauses(c) == pytest.approx(2.0, abs=0.01)


@pytest.mark.parametrize("headers", [{}, {"X-RateLimit-Reset": "junk"}], ids=["missing", "unparseable"])
@respx.mock
async def test_429_without_a_usable_reset_backs_off_exponentially_capped_at_2s(
    headers: dict[str, str],
) -> None:
    sleeps = Sleeps()
    respx.get(BASE + "markets/quotes").mock(return_value=httpx.Response(429, headers=headers))
    async with QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=sleeps) as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.quotes([1])
        assert err.value.status == 429
        assert pauses(c) == pytest.approx(0.5 + 1.0 + 2.0 + 2.0)
        assert c.stats["market"].http_429 == 5
    # A sleep after a pause also carries up to one 1/17 s slot of spacing.
    assert [s for s in sleeps.calls if s >= 0.4] == pytest.approx([0.5, 1.0, 2.0, 2.0], abs=0.07)


@pytest.mark.parametrize(("ahead", "expected"), [(5.0, 5.0), (600.0, MAX_429_PAUSE), (1800.0, MAX_429_PAUSE)])
@respx.mock
async def test_429_with_a_far_reset_is_a_real_exhaustion_and_keeps_the_30s_cap(
    ahead: float, expected: float
) -> None:
    """Remaining 0 with a Reset far ahead (the hourly window): wait for it, at most 30 s."""
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(429, headers=reset_in(ahead)),
            httpx.Response(200, json={"quotes": []}),
        ]
    )
    async with QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=Sleeps()) as c:
        assert await c.quotes([1]) == []
        assert pauses(c) == pytest.approx(expected, abs=0.01)
    assert MAX_429_PAUSE == 30.0


# --- the opening-bar batch on a virtual clock -----------------------------------------------------------


class VirtualTime:
    """A discrete-event clock: sleep() parks the caller until a driver advances time to its wake-up."""

    def __init__(self) -> None:
        self.now = 0.0
        self._heap: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        if s <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self.now + s, next(self._seq), fut))
        await fut

    async def run(self, work: "asyncio.Task[object]") -> None:
        while not work.done():
            for _ in range(50):
                await asyncio.sleep(0)
                if work.done():
                    return
            if not self._heap:
                await asyncio.sleep(0.001)  # a real await (e.g. the first token fetch's thread)
                continue
            wake, _, fut = heapq.heappop(self._heap)
            self.now = max(self.now, wake)
            if not fut.done():
                fut.set_result(None)


class VirtualClock:
    def __init__(self, vt: VirtualTime) -> None:
        self._vt = vt

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self._vt.now)


def too_many(reset_at_vt: float) -> httpx.Response:
    reset = NOW.timestamp() + reset_at_vt
    return httpx.Response(429, headers={"X-RateLimit-Reset": str(reset), "X-RateLimit-Remaining": "0"})


def every_nth_429(vt: VirtualTime, every: int) -> Callable[[httpx.Request], httpx.Response]:
    """Worst case: a 429 every `every` requests whatever the pace, Reset 0.1-0.4 s ahead."""
    offsets = itertools.cycle([0.1, 0.2, 0.3, 0.4])
    seen = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if next(seen) % every == 0:
            return too_many(vt.now + next(offsets))
        return httpx.Response(200, json={"candles": []})

    return handler


def per_second_window(
    vt: VirtualTime, limit: int, external_rps: int
) -> Callable[[httpx.Request], httpx.Response]:
    """Questrade-like: at most `limit` requests per fixed 1 s window, shared with `external_rps` of other
    load spread evenly over each window; over the limit answers 429 with Reset = the window's end."""
    ours: dict[int, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        window = int(vt.now // 1)
        external = int((vt.now - window) * external_rps) + 1 if external_rps else 0
        if ours.get(window, 0) + external >= limit:
            return too_many(window + 1.0)
        ours[window] = ours.get(window, 0) + 1
        return httpx.Response(200, json={"candles": []})

    return handler


async def run_opening_batch(
    n: int, server: Callable[[VirtualTime], Callable[[httpx.Request], httpx.Response]]
) -> tuple[float, QuestradeClient, int]:
    vt = VirtualTime()
    respx.get(url__regex=BASE + r"markets/candles/\d+").mock(side_effect=server(vt))
    reqs = [CandleRequest(i, START, START + timedelta(minutes=5), "FiveMinutes") for i in range(1, n + 1)]
    c = QuestradeClient(FakeTokens(), VirtualClock(vt), sleep=vt.sleep, monotonic=vt.monotonic)
    work = asyncio.create_task(c.candles_many(reqs))
    await vt.run(work)
    got = work.result()
    ok = sum(1 for v in got.values() if isinstance(v, list))
    await c.__aexit__(None, None, None)
    return vt.now, c, ok


@respx.mock
async def test_543_request_batch_alone_takes_the_17_rps_floor() -> None:
    elapsed, c, ok = await run_opening_batch(543, lambda vt: every_nth_429(vt, 10**9))
    assert ok == 543
    assert elapsed == pytest.approx(542 / 17, abs=0.1)  # ~31.9 s
    assert c.stats["market"].http_429 == 0


@respx.mock
async def test_543_request_batch_with_a_429_every_15_requests_finishes_inside_the_deadline() -> None:
    """Worst case where pacing does not reduce the 429s at all: ~38 x 429, each pause follows Reset
    (0.15-0.45 s) instead of the old 0.5 s floor: ~44 s here, the old code (20 rps) ~49 s."""
    elapsed, c, ok = await run_opening_batch(543, lambda vt: every_nth_429(vt, 15))
    stats = c.stats["market"]
    assert ok == 543
    assert stats.http_429 >= 36
    assert stats.pause_s < 0.5 * stats.http_429  # never the old 0.5 s floor
    assert elapsed < FETCH_DEADLINE_S, f"batch took {elapsed:.1f} s on the virtual clock"


@pytest.mark.parametrize(("external_rps", "bound_s"), [(0, 33.0), (3, 33.0), (4, 36.0)])
@respx.mock
async def test_543_request_batch_under_a_20_per_second_window_finishes_well_under_the_deadline(
    external_rps: int, bound_s: float
) -> None:
    """The measured 9:35 case: Questrade's 20 req/s window shared with ~3 req/s of other load.
    Same model, old code (20 rps, 0.5 s floor): 36.4 s with 3 rps, 39.4 s with 4 rps."""
    elapsed, c, ok = await run_opening_batch(
        543, lambda vt: per_second_window(vt, limit=20, external_rps=external_rps)
    )
    assert ok == 543
    stats = c.stats["market"]
    assert elapsed < bound_s, (
        f"batch took {elapsed:.1f} s, {stats.http_429} x 429, {stats.pause_s:.2f} s paused"
    )
