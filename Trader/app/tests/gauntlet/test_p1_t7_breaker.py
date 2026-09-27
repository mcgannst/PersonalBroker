"""P1-T7 Breaker: try to break the Questrade data client (never touches the network: respx + fakes)."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import (
    MAX_ATTEMPTS,
    QuestradeApiError,
    QuestradeClient,
    TokenBucket,
)
from trader.adapters.questrade.models import CandleRequest
from trader.market.clock import FixedClock

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
SECRET_PREFIX = "sekrit-tok-"


class FakeTokens:
    def __init__(self) -> None:
        self.n = 1
        self.forced = 0

    def access(self) -> AccessToken:
        return AccessToken(f"{SECRET_PREFIX}{self.n}", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self) -> AccessToken:
        self.forced += 1
        self.n += 1
        return self.access()


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def make_client(tokens: FakeTokens | None = None, sleep: Sleeps | None = None) -> QuestradeClient:
    return QuestradeClient(tokens or FakeTokens(), FixedClock(NOW), sleep=sleep or Sleeps())


class VirtualBucket(TokenBucket):
    """A TokenBucket on a virtual clock that records the virtual time each call is let through.

    The fake sleep advances the virtual clock and never yields, so the time read right after
    acquire() returns is exactly the moment the request would be dispatched.
    """

    def __init__(self, rate: float) -> None:
        self.vt = [0.0]
        self.dispatched: list[float] = []

        async def fake_sleep(s: float) -> None:
            self.vt[0] = max(self.vt[0], self.vt[0] + s)

        super().__init__(rate, monotonic=lambda: self.vt[0], sleep=fake_sleep)

    async def acquire(self) -> None:
        await super().acquire()
        self.dispatched.append(self.vt[0])


def max_in_any_second(times: list[float]) -> int:
    ts = sorted(times)
    best = 0
    j = 0
    for i, t in enumerate(ts):
        while j < len(ts) and ts[j] < t + 1.0 - 1e-9:
            j += 1
        best = max(best, j - i)
    return best


@respx.mock
async def test_repeated_401_refreshes_once_then_raises_without_token_in_message() -> None:
    """A dead token must give one forced refresh per call and then an error, never a loop."""
    tokens = FakeTokens()
    route = respx.get(BASE + "time").mock(
        return_value=httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})
    )
    async with make_client(tokens) as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.server_time()
        assert err.value.status == 401
        assert tokens.forced == 1
        assert route.call_count == 2
        # A second call gets its own single refresh, not an unbounded chain.
        with pytest.raises(QuestradeApiError):
            await c.server_time()
        assert tokens.forced == 2
        assert route.call_count == 4
    for text in (str(err.value), repr(err.value), " ".join(map(str, err.value.args))):
        assert SECRET_PREFIX not in text


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-RateLimit-Reset": str(int((NOW - timedelta(hours=1)).timestamp()))}],
    ids=["missing-reset", "past-reset"],
)
@respx.mock
async def test_429_with_missing_or_past_reset_backs_off_boundedly(headers: dict[str, str]) -> None:
    sleeps = Sleeps()
    route = respx.get(BASE + "markets/quotes").mock(return_value=httpx.Response(429, headers=headers))
    async with make_client(sleep=sleeps) as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.quotes([1])
    assert err.value.status == 429
    assert route.call_count == MAX_ATTEMPTS
    backoffs = [s for s in sleeps.calls if s >= 0.5]  # ignore bucket spacing sleeps (0.05 s)
    assert backoffs, "429 must back off before retrying"
    assert all(0.5 <= s <= 5.0 for s in backoffs)


@respx.mock
async def test_5xx_every_attempt_raises_after_max_attempts_without_trailing_sleep() -> None:
    """Every attempt 503: QuestradeApiError after MAX_ATTEMPTS calls. Sleeping the longest backoff
    (8 s) after the final attempt only delays the error at a decision point (Review Focus 4)."""
    sleeps = Sleeps()
    route = respx.get(BASE + "markets/quotes").mock(return_value=httpx.Response(503, text="unavailable"))
    async with make_client(sleep=sleeps) as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.quotes([1])
    assert err.value.status == 503
    assert route.call_count == MAX_ATTEMPTS
    backoffs = [s for s in sleeps.calls if s >= 0.5]
    assert backoffs == sorted(backoffs)
    assert len(backoffs) == MAX_ATTEMPTS - 1, f"backoff sleeps {backoffs}: slept after the last attempt"


@respx.mock
async def test_candles_many_700_requests_never_exceed_20_per_second() -> None:
    route = respx.get(url__regex=BASE + r"markets/candles/\d+").mock(
        return_value=httpx.Response(200, json={"candles": []})
    )
    start = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    reqs = [CandleRequest(i, start, start + timedelta(minutes=5), "FiveMinutes") for i in range(1, 701)]
    bucket = VirtualBucket(20.0)
    async with make_client() as c:
        c._buckets["market"] = bucket
        got = await asyncio.wait_for(c.candles_many(reqs), timeout=60)
    assert route.call_count == 700
    assert len(got) == 700 and all(v == [] for v in got.values())
    assert len(bucket.dispatched) == 700
    assert max_in_any_second(bucket.dispatched) <= 20
    span = max(bucket.dispatched) - min(bucket.dispatched)
    assert span == pytest.approx(699 / 20, rel=0.01)  # completes at the rate, not slower


async def test_shared_bucket_keeps_spacing_across_gather_tasks_and_after_idle() -> None:
    bucket = VirtualBucket(20.0)
    await asyncio.gather(*(bucket.acquire() for _ in range(60)))
    first = sorted(bucket.dispatched)
    gaps = [b - a for a, b in zip(first, first[1:], strict=False)]
    assert all(g == pytest.approx(0.05) for g in gaps)
    # After a long idle the bucket must not have saved up a burst.
    bucket.vt[0] += 10.0
    bucket.dispatched.clear()
    await asyncio.gather(*(bucket.acquire() for _ in range(40)))
    assert max_in_any_second(bucket.dispatched) <= 20


@respx.mock
async def test_symbols_chunk_500_propagates_not_swallowed() -> None:
    names = [f"S{i}" for i in range(150)]

    def handler(request: httpx.Request) -> httpx.Response:
        asked = request.url.params["names"].split(",")
        if "S120" in asked:
            return httpx.Response(500, text="internal error")
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": n,
                        "symbolId": i + 1,
                        "listingExchange": "NASDAQ",
                        "currency": "USD",
                        "description": n,
                        "isTradable": True,
                        "isQuotable": True,
                    }
                    for i, n in enumerate(asked)
                ]
            },
        )

    respx.get(BASE + "symbols").mock(side_effect=handler)
    async with make_client() as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.symbols_by_names(names)
    assert err.value.status == 500


@respx.mock
async def test_odd_but_valid_payloads_parse() -> None:
    """Missing optional quote fields, string delay, Z / no-fraction timestamps, integer candle prices,
    and an empty (start == end) window."""
    respx.get(BASE + "markets/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "quotes": [
                    {"symbol": "AAA", "symbolId": 1, "delay": "15", "lastTradeTime": "2026-09-25T13:30:00Z"},
                    {
                        "symbol": "BBB",
                        "symbolId": 2,
                        "lastTradePrice": 10,
                        "volume": None,
                        "lastTradeTime": "2026-09-25T09:30:00-04:00",
                        "isHalted": True,
                    },
                ]
            },
        )
    )
    candles_route = respx.get(BASE + "markets/candles/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "candles": [
                    {
                        "start": "2026-09-25T13:30:00Z",
                        "end": "2026-09-25T13:35:00Z",
                        "low": 9,
                        "high": 11,
                        "open": 10,
                        "close": 11,
                        "volume": 1000,
                    }
                ]
            },
        )
    )
    start = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    async with make_client() as c:
        a, b = await c.quotes([1, 2])
        (bar,) = await c.candles(7, start, start + timedelta(minutes=5), "FiveMinutes")
        assert await c.candles(7, start, start, "FiveMinutes") == []
        assert await c.candles(7, start, start, "OneDay") == []
    assert a.delay == 15 and a.bid is None and a.last is None and a.vwap is None and a.volume == 0
    assert a.is_halted is False
    assert a.last_trade_time == datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    assert b.last == Decimal("10") and b.volume == 0 and b.is_halted is True
    assert b.last_trade_time == datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    assert bar.start == start and bar.end == start + timedelta(minutes=5)
    assert (bar.open, bar.high, bar.low, bar.close) == (Decimal(10), Decimal(11), Decimal(9), Decimal(11))
    assert all(isinstance(x, Decimal) for x in (bar.open, bar.high, bar.low, bar.close))
    assert bar.vwap is None
    assert candles_route.call_count == 1


@respx.mock
async def test_candles_many_isolates_transport_errors() -> None:
    """Review Focus 4: one symbol timing out must not abort the whole scan."""
    respx.get(BASE + "markets/candles/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "candles": [
                    {
                        "start": "2026-09-25T13:30:00Z",
                        "end": "2026-09-25T13:35:00Z",
                        "low": 9,
                        "high": 11,
                        "open": 10,
                        "close": 11,
                        "volume": 1000,
                    }
                ]
            },
        )
    )
    respx.get(BASE + "markets/candles/2").mock(side_effect=httpx.ReadTimeout("timed out"))
    respx.get(BASE + "markets/candles/3").mock(side_effect=httpx.ConnectError("connection refused"))
    start = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    reqs = [CandleRequest(i, start, start + timedelta(minutes=5), "FiveMinutes") for i in (1, 2, 3)]
    async with make_client() as c:
        got = await c.candles_many(reqs)
    assert len(got[reqs[0]]) == 1  # type: ignore[arg-type]
    assert isinstance(got[reqs[1]], QuestradeApiError)
    assert isinstance(got[reqs[2]], QuestradeApiError)
    for r in reqs[1:]:
        assert SECRET_PREFIX not in str(got[r])
