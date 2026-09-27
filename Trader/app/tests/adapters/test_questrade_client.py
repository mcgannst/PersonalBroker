from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient, TokenBucket
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
        await c.candles(1, NOW - timedelta(days=400), NOW, "OneMinute")
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
