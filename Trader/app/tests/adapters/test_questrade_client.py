import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import (
    MAX_ATTEMPTS,
    TOKEN_REUSE_MARGIN,
    QuestradeApiError,
    QuestradeClient,
    TokenBucket,
)
from trader.adapters.questrade.models import CandleRequest
from trader.market.clock import FixedClock

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class FakeTokens:
    def __init__(self) -> None:
        self.n = 1
        self.forced = 0

    def access(self) -> AccessToken:
        return AccessToken(f"tok-{self.n}", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self) -> AccessToken:
        self.forced += 1
        self.n += 1
        return self.access()


async def no_sleep(_: float) -> None:
    return None


def client(tokens: FakeTokens | None = None) -> QuestradeClient:
    return QuestradeClient(tokens or FakeTokens(), FixedClock(NOW), sleep=no_sleep)


def candle_json(start: str, o: float, v: int) -> dict[str, object]:
    return {
        "start": start,
        "end": start,
        "low": o - 1,
        "high": o + 1,
        "open": o,
        "close": o + 0.5,
        "volume": v,
        "VWAP": o + 0.25,
    }


@respx.mock
async def test_symbols_by_names_chunks_and_skips_unknown() -> None:
    names = [f"S{i}" for i in range(150)] + ["BAD"]

    def handler(request: httpx.Request) -> httpx.Response:
        asked = request.url.params["names"].split(",")
        if "BAD" in asked and len(asked) > 1:
            return httpx.Response(400, json={"code": 1002, "message": "Invalid or malformed argument"})
        if asked == ["BAD"]:
            return httpx.Response(400, json={"code": 1002, "message": "Invalid or malformed argument"})
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

    route = respx.get(BASE + "symbols").mock(side_effect=handler)
    async with client() as c:
        got = await c.symbols_by_names(names)
    assert set(got) == set(names) - {"BAD"}
    assert all(len(call.request.url.params["names"].split(",")) <= 100 for call in route.calls)


@respx.mock
async def test_quotes_parse_decimals_and_delay() -> None:
    respx.get(BASE + "markets/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "quotes": [
                    {
                        "symbol": "SPY",
                        "symbolId": 34987,
                        "bidPrice": None,
                        "askPrice": None,
                        "lastTradePrice": 771.35,
                        "lastTradePriceTrHrs": 771.35,
                        "volume": 57884,
                        "lastTradeTime": "2026-09-25T00:00:00.000000-04:00",
                        "delay": 0,
                        "isHalted": False,
                        "VWAP": 770.1,
                    }
                ]
            },
        )
    )
    async with client() as c:
        (q,) = await c.quotes([34987])
    assert q.last == Decimal("771.35") and q.bid is None and q.delay == 0
    assert q.last_trade_time == datetime(2026, 9, 25, 4, 0, tzinfo=UTC)


@respx.mock
async def test_candles_parse_to_utc_decimal() -> None:
    respx.get(BASE + "markets/candles/8049").mock(
        return_value=httpx.Response(
            200, json={"candles": [candle_json("2026-09-25T09:30:00.000000-04:00", 336.04, 403790)]}
        )
    )
    start = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    async with client() as c:
        (bar,) = await c.candles(8049, start, start + timedelta(minutes=5), "FiveMinutes")
    assert bar.start == start
    assert bar.open == Decimal("336.04") and bar.volume == 403790


@respx.mock
async def test_intraday_start_is_clamped_to_available_history() -> None:
    route = respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": []}))
    async with client() as c:
        # FifteenMinutes: 88 days of OneMinute would trip the 20,000-candle guard.
        await c.candles(1, NOW - timedelta(days=400), NOW, "FifteenMinutes")
        await c.candles(1, NOW - timedelta(days=400), NOW, "OneDay")
    intraday_start = datetime.fromisoformat(route.calls[0].request.url.params["startTime"])
    daily_start = datetime.fromisoformat(route.calls[1].request.url.params["startTime"])
    assert intraday_start >= NOW - timedelta(days=88, seconds=1)
    assert daily_start == NOW - timedelta(days=400)


@respx.mock
async def test_window_entirely_before_history_returns_empty_without_request() -> None:
    route = respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": []}))
    async with client() as c:
        got = await c.candles(1, NOW - timedelta(days=200), NOW - timedelta(days=150), "FiveMinutes")
    assert got == [] and route.call_count == 0


@respx.mock
async def test_401_forces_one_refresh_then_succeeds() -> None:
    tokens = FakeTokens()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer tok-1":
            return httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})
        return httpx.Response(200, json={"time": "2026-09-27T08:00:00.000000-04:00"})

    respx.get(BASE + "time").mock(side_effect=handler)
    async with client(tokens) as c:
        t = await c.server_time()
    assert tokens.forced == 1
    assert t == datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@respx.mock
async def test_429_then_success_and_rate_limit_recorded() -> None:
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}),
            httpx.Response(200, headers={"X-RateLimit-Remaining": "14990"}, json={"quotes": []}),
        ]
    )
    async with client() as c:
        assert await c.quotes([1]) == []
        assert c.rate_limit_remaining["market"] == 14990


@respx.mock
async def test_candles_many_reports_missing() -> None:
    """Review Focus 4: a symbol with no bar and a symbol that errors don't break the scan."""
    respx.get(BASE + "markets/candles/1").mock(
        return_value=httpx.Response(
            200, json={"candles": [candle_json("2026-09-25T09:30:00.000000-04:00", 10.0, 100)]}
        )
    )
    respx.get(BASE + "markets/candles/2").mock(return_value=httpx.Response(200, json={"candles": []}))
    respx.get(BASE + "markets/candles/3").mock(return_value=httpx.Response(400, json={"code": 1002}))
    start = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    reqs = [CandleRequest(i, start, start + timedelta(minutes=5), "FiveMinutes") for i in (1, 2, 3)]
    async with client() as c:
        got = await c.candles_many(reqs)
    assert len(got[reqs[0]]) == 1  # type: ignore[arg-type]
    assert got[reqs[1]] == []
    assert isinstance(got[reqs[2]], QuestradeApiError) and got[reqs[2]].status == 400


async def test_token_bucket_spaces_requests() -> None:
    t = {"now": 0.0}
    waits: list[float] = []

    async def fake_sleep(s: float) -> None:
        waits.append(s)
        t["now"] += s

    bucket = TokenBucket(20.0, monotonic=lambda: t["now"], sleep=fake_sleep)
    for _ in range(3):
        await bucket.acquire()
    assert waits == pytest.approx([0.05, 0.05])


# --- P1-T7 attempt 2 regression tests (gauntlet findings) ---


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def recording_client(sleeps: Sleeps) -> QuestradeClient:
    return QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=sleeps)


BAR = {
    "start": "2026-09-25T13:30:00Z",
    "end": "2026-09-25T13:35:00Z",
    "low": 9,
    "high": 11,
    "open": 10,
    "close": 11,
    "volume": 1000,
}
START = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)


@respx.mock
async def test_transport_error_is_retried_then_succeeds() -> None:
    """Finding 1: a timeout is retried with the 5xx backoff."""
    sleeps = Sleeps()
    route = respx.get(BASE + "time").mock(
        side_effect=[
            httpx.ConnectTimeout("timed out"),
            httpx.Response(200, json={"time": "2026-09-27T12:00:00Z"}),
        ]
    )
    async with recording_client(sleeps) as c:
        assert await c.server_time() == NOW
    assert route.call_count == 2
    assert 0.5 in sleeps.calls


@respx.mock
async def test_transport_error_on_every_attempt_raises_status_0_without_url_or_token() -> None:
    """Findings 1 and 3: QuestradeApiError(0, ...) naming the exception type, no trailing sleep."""
    sleeps = Sleeps()
    route = respx.get(BASE + "time").mock(
        side_effect=httpx.ConnectError(f"cannot reach {BASE}time with tok-1")
    )
    async with recording_client(sleeps) as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.server_time()
    assert route.call_count == MAX_ATTEMPTS
    assert err.value.status == 0
    assert "ConnectError" in str(err.value)
    assert BASE not in str(err.value)
    assert "tok-1" not in str(err.value)
    assert [s for s in sleeps.calls if s >= 0.5] == [0.5, 1.0, 2.0, 4.0]


@respx.mock
async def test_429_on_every_attempt_does_not_pause_after_the_last() -> None:
    """Finding 3 (429 path): four pauses for five attempts, growing exponentially."""
    sleeps = Sleeps()
    respx.get(BASE + "markets/quotes").mock(
        return_value=httpx.Response(429, headers={"X-RateLimit-Reset": "junk"})
    )
    async with recording_client(sleeps) as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.quotes([1])
    assert err.value.status == 429
    backoffs = [s for s in sleeps.calls if s >= 0.4]
    assert backoffs == pytest.approx([0.5, 1.0, 2.0, 4.0], abs=0.05)


@respx.mock
async def test_429_pause_follows_reset_header_capped_at_30s() -> None:
    """Finding 4: the reset time wins when it is later than the backoff, capped at 30 s."""
    sleeps = Sleeps()
    far = str(int(NOW.timestamp()) + 600)
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Reset": far}),
            httpx.Response(200, json={"quotes": []}),
        ]
    )
    async with recording_client(sleeps) as c:
        assert await c.quotes([1]) == []
    assert max(sleeps.calls) == pytest.approx(30.0, abs=0.05)


async def test_pause_until_holds_back_every_caller() -> None:
    """Finding 4: a pause delays new callers and callers already queued for an earlier slot."""
    t = {"now": 0.0}
    dispatched: list[float] = []

    async def fake_sleep(s: float) -> None:
        await asyncio.sleep(0)
        t["now"] = max(t["now"], t["now"] + s)

    bucket = TokenBucket(20.0, monotonic=lambda: t["now"], sleep=fake_sleep)

    async def caller() -> None:
        await bucket.acquire()
        dispatched.append(t["now"])

    queued = [asyncio.create_task(caller()) for _ in range(5)]
    await asyncio.sleep(0)  # all five have taken slots 0.00..0.20 and four are waiting
    bucket.pause_until(3.0)
    await asyncio.gather(*queued, *(caller() for _ in range(5)))
    assert len(dispatched) == 10
    # The first caller's slot was "now" (no wait), so it went through before the pause.
    held = sorted(dispatched)[1:]
    assert all(d >= 3.0 for d in held)
    gaps = [b - a for a, b in zip(held, held[1:], strict=False)]
    assert all(g >= 0.05 - 1e-9 for g in gaps)


@respx.mock
async def test_429_pauses_the_shared_bucket_for_concurrent_callers() -> None:
    """Finding 4: a 429 pauses the category's shared bucket, so a different caller waits too.

    Virtual time stands still (the fake sleep only records), so each wait shows the slot the
    bucket handed out: without the shared pause the candles call would wait 0.1 s, not 0.55 s.
    """
    waits: list[float] = []

    async def record(s: float) -> None:
        waits.append(s)

    respx.get(BASE + "markets/quotes").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"quotes": []})]
    )
    respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": []}))
    async with client() as c:
        c._buckets["market"] = TokenBucket(20.0, monotonic=lambda: 0.0, sleep=record)
        assert await c.quotes([1]) == []
        assert waits == pytest.approx([0.5])  # the 429'd caller waited out its own pause
        await c.candles(1, START, START + timedelta(minutes=5), "FiveMinutes")
    assert waits[1] == pytest.approx(0.55)


@respx.mock
async def test_candles_many_turns_parse_errors_into_per_request_errors() -> None:
    """Finding 2: bad JSON, missing keys, bad numbers and naive timestamps don't abort the scan."""
    bodies = {
        2: httpx.Response(200, text="<html>not json"),
        3: httpx.Response(200, json={"candles": [{"start": "x"}]}),
        4: httpx.Response(200, json={"candles": [{**BAR, "open": "abc"}]}),
        5: httpx.Response(200, json={"candles": [{**BAR, "start": "2026-09-25T13:30:00"}]}),
        6: httpx.Response(200, json={"candles": [{**BAR, "volume": None}]}),
        7: httpx.Response(200, json=["not", "an", "object"]),
    }
    respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": [BAR]}))
    for sid, resp in bodies.items():
        respx.get(BASE + f"markets/candles/{sid}").mock(return_value=resp)
    reqs = [CandleRequest(i, START, START + timedelta(minutes=5), "FiveMinutes") for i in range(1, 8)]
    async with client() as c:
        got = await c.candles_many(reqs)
    assert len(got[reqs[0]]) == 1  # type: ignore[arg-type]
    for r in reqs[1:]:
        v = got[r]
        assert isinstance(v, QuestradeApiError), (r.symbol_id, v)
        assert v.status == 0


@respx.mock
async def test_bad_rate_limit_headers_are_ignored() -> None:
    """Finding 2: non-numeric X-RateLimit-* headers never raise."""
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Reset": "soon", "X-RateLimit-Remaining": "?"}),
            httpx.Response(200, json={"quotes": []}),
        ]
    )
    async with client() as c:
        assert await c.quotes([1]) == []
        assert "market" not in c.rate_limit_remaining


@respx.mock
async def test_candles_sends_second_precision_times_and_rejects_naive() -> None:
    """Finding 5."""
    route = respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": []}))
    start = START.replace(microsecond=123456)
    async with client() as c:
        await c.candles(1, start, start + timedelta(minutes=5), "FiveMinutes")
        with pytest.raises(ValueError, match="timezone"):
            await c.candles(1, START.replace(tzinfo=None), START + timedelta(minutes=5), "FiveMinutes")
        with pytest.raises(ValueError, match="timezone"):
            await c.candles(1, START, (START + timedelta(minutes=5)).replace(tzinfo=None), "FiveMinutes")
    params = route.calls[0].request.url.params
    assert params["startTime"] == "2026-09-25T13:30:00+00:00"
    assert params["endTime"] == "2026-09-25T13:35:00+00:00"
    assert route.call_count == 1


async def test_aexit_closes_only_a_client_it_created() -> None:
    """Finding 6."""
    shared = httpx.AsyncClient()
    async with QuestradeClient(FakeTokens(), FixedClock(NOW), http=shared, sleep=no_sleep):
        pass
    assert not shared.is_closed
    await shared.aclose()
    own = QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=no_sleep)
    async with own:
        pass
    assert own._http.is_closed


@respx.mock
async def test_missing_delay_is_none_not_real_time() -> None:
    """Finding 8."""
    respx.get(BASE + "markets/quotes").mock(
        return_value=httpx.Response(
            200, json={"quotes": [{"symbol": "A", "symbolId": 1}, {"symbol": "B", "symbolId": 2, "delay": 0}]}
        )
    )
    async with client() as c:
        a, b = await c.quotes([1, 2])
    assert a.delay is None
    assert b.delay == 0


@respx.mock
async def test_candles_rejects_windows_over_20000_bars() -> None:
    """Finding 9: OneMinute over 14 days could exceed 20,000 bars; 13 days cannot."""
    route = respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": []}))
    async with client() as c:
        await c.candles(1, NOW - timedelta(days=13), NOW, "OneMinute")
        with pytest.raises(ValueError, match="20000"):
            await c.candles(1, NOW - timedelta(days=14), NOW, "OneMinute")
        got = await c.candles_many([CandleRequest(1, NOW - timedelta(days=14), NOW, "OneMinute")])
    assert route.call_count == 1
    (err,) = got.values()
    assert isinstance(err, QuestradeApiError)
    assert err.status == 0


@respx.mock
async def test_server_time_without_time_raises_api_error() -> None:
    """Finding 10: explicit errors instead of asserts."""
    respx.get(BASE + "time").mock(return_value=httpx.Response(200, json={"time": ""}))
    async with client() as c:
        with pytest.raises(QuestradeApiError):
            await c.server_time()


class CountingTokens:
    """A TokenSource that counts calls. Each fetched token lives 30 minutes from the clock's now,
    and access() is slow enough that unguarded concurrent callers would all reach it."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.n = 0
        self.accessed = 0
        self.forced = 0

    def _new(self) -> AccessToken:
        self.n += 1
        return AccessToken(f"tok-{self.n}", BASE, self.clock.now() + timedelta(minutes=30))

    def access(self) -> AccessToken:
        self.accessed += 1
        time.sleep(0.01)
        return self._new()

    def force_refresh(self) -> AccessToken:
        self.forced += 1
        return self._new()


def bearer(call: Any) -> str:
    return str(call.request.headers["Authorization"])


def ok_time(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"time": "2026-09-27T08:00:00.000000-04:00"})


def reject_tok_1(request: httpx.Request) -> httpx.Response:
    if request.headers["Authorization"] == "Bearer tok-1":
        return httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})
    return ok_time(request)


@respx.mock
async def test_concurrent_requests_fetch_the_access_token_once() -> None:
    """P1-REVIEW should-fix 2: 50 concurrent candle requests share one access() call."""
    clock = FixedClock(NOW)
    tokens = CountingTokens(clock)
    route = respx.get(url__regex=BASE + r"markets/candles/\d+").mock(
        return_value=httpx.Response(200, json={"candles": []})
    )
    reqs = [CandleRequest(i, NOW - timedelta(days=1), NOW, "FiveMinutes") for i in range(50)]
    async with QuestradeClient(tokens, clock, sleep=no_sleep) as c:
        got = await c.candles_many(reqs)
    assert all(v == [] for v in got.values())
    assert route.call_count == 50
    assert tokens.accessed == 1
    assert {bearer(call) for call in route.calls} == {"Bearer tok-1"}


@respx.mock
async def test_expired_cached_token_is_fetched_again_exactly_once() -> None:
    clock = FixedClock(NOW)
    tokens = CountingTokens(clock)
    route = respx.get(BASE + "time").mock(side_effect=ok_time)
    async with QuestradeClient(tokens, clock, sleep=no_sleep) as c:
        await c.server_time()
        expires = NOW + timedelta(minutes=30)
        clock.set(expires - TOKEN_REUSE_MARGIN - timedelta(seconds=1))  # still reusable
        await c.server_time()
        assert tokens.accessed == 1
        clock.set(expires - TOKEN_REUSE_MARGIN)  # reuse window over: fetch once, then reuse again
        await asyncio.gather(*(c.server_time() for _ in range(10)))
        await c.server_time()
    assert tokens.accessed == 2
    assert tokens.forced == 0
    assert [bearer(call) for call in route.calls] == ["Bearer tok-1"] * 2 + ["Bearer tok-2"] * 11


@respx.mock
async def test_401_replaces_the_cached_token() -> None:
    clock = FixedClock(NOW)
    tokens = CountingTokens(clock)
    route = respx.get(BASE + "time").mock(side_effect=reject_tok_1)
    async with QuestradeClient(tokens, clock, sleep=no_sleep) as c:
        await c.server_time()
        await c.server_time()  # uses the refreshed token straight from the cache
    assert tokens.accessed == 1
    assert tokens.forced == 1
    assert [bearer(call) for call in route.calls] == ["Bearer tok-1", "Bearer tok-2", "Bearer tok-2"]


@respx.mock
async def test_concurrent_401s_force_one_refresh() -> None:
    """Requests rejected with the same stale token share one forced refresh."""
    clock = FixedClock(NOW)
    tokens = CountingTokens(clock)
    respx.get(BASE + "time").mock(side_effect=reject_tok_1)
    async with QuestradeClient(tokens, clock, sleep=no_sleep) as c:
        await asyncio.gather(*(c.server_time() for _ in range(10)))
    assert tokens.accessed == 1
    assert tokens.forced == 1
