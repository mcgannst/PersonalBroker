"""QUOTEBAR: the client reads the quote's session open/high/low, and a 401 with Questrade code 1022 (the
market data package does not cover that data, e.g. an intraday candle less than ~10 minutes old) is not a
token problem: it is raised at once, without a forced token refresh."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import respx

from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient
from trader.adapters.questrade.models import CandleRequest
from trader.market.clock import FixedClock

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)
PACKAGE_401 = {
    "code": 1022,
    "message": "The requested data is not included in your current market data package",
}


class FakeTokens:
    def __init__(self) -> None:
        self.forced = 0

    def access(self) -> AccessToken:
        return AccessToken(f"tok-{self.forced}", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self, rejected: str | None = None) -> AccessToken:
        self.forced += 1
        return self.access()


async def no_sleep(_: float) -> None:
    return None


def quote_json(**kw: object) -> dict[str, object]:
    out: dict[str, object] = {
        "symbol": "SPY",
        "symbolId": 34987,
        "bidPrice": 660.1,
        "askPrice": 660.2,
        "lastTradePrice": 660.15,
        "lastTradePriceTrHrs": 660.15,
        "volume": 36850000,
        "lastTradeTime": "2026-10-06T09:35:04.000000-04:00",
        "delay": 0,
        "isHalted": False,
        "VWAP": 659.9,
        "openPrice": 658.5,
        "highPrice": 660.9,
        "lowPrice": 657.8,
    }
    out.update(kw)
    return out


@respx.mock
async def test_quotes_carry_the_session_open_high_low() -> None:
    respx.get(BASE + "markets/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "quotes": [
                    quote_json(),
                    quote_json(symbolId=1, symbol="X", openPrice=None, highPrice=None, lowPrice=None),
                ]
            },
        )
    )
    async with QuestradeClient(FakeTokens(), FixedClock(NOW), sleep=no_sleep) as c:
        spy, x = await c.quotes([34987, 1])
    assert (spy.open, spy.high, spy.low) == (Decimal("658.5"), Decimal("660.9"), Decimal("657.8"))
    assert (x.open, x.high, x.low) == (None, None, None)


@respx.mock
async def test_a_1022_market_data_package_401_is_raised_without_a_token_refresh() -> None:
    route = respx.get(BASE + "markets/candles/34987").mock(return_value=httpx.Response(401, json=PACKAGE_401))
    tokens = FakeTokens()
    async with QuestradeClient(tokens, FixedClock(NOW), sleep=no_sleep) as c:
        try:
            await c.candles(34987, NOW - timedelta(minutes=5), NOW, "FiveMinutes")
        except QuestradeApiError as exc:
            err = exc
        else:  # pragma: no cover - the call must raise
            raise AssertionError("expected QuestradeApiError")
    assert err.status == 401 and err.code == 1022
    assert tokens.forced == 0  # the token is fine: no refresh (no rotation of the refresh token)
    assert route.call_count == 1


@respx.mock
async def test_candles_many_still_fails_fast_on_1022() -> None:
    route = respx.get(url__regex=BASE + r"markets/candles/\d+").mock(
        return_value=httpx.Response(401, json=PACKAGE_401)
    )
    tokens = FakeTokens()
    reqs = [CandleRequest(i, NOW - timedelta(minutes=5), NOW, "FiveMinutes") for i in range(1, 101)]
    async with QuestradeClient(tokens, FixedClock(NOW), sleep=no_sleep) as c:
        out = await c.candles_many(reqs, deadline_s=30)
    assert len(out) == 100
    assert all(isinstance(r, QuestradeApiError) and r.code == 1022 for r in out.values())
    assert tokens.forced == 0
    assert route.call_count < 100  # the rest were cancelled after the first 20 x 401


@respx.mock
async def test_a_1017_invalid_token_401_still_refreshes_once() -> None:
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"}),
            httpx.Response(200, json={"quotes": [quote_json()]}),
        ]
    )
    tokens = FakeTokens()
    async with QuestradeClient(tokens, FixedClock(NOW), sleep=no_sleep) as c:
        (spy,) = await c.quotes([34987])
    assert tokens.forced == 1 and spy.open == Decimal("658.5")
