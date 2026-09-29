"""FIX-401 (Tue 2026-09-29): a forced refresh inside the 90 s cooldown handed back the very token that
had just been rejected, so a batch rejected with it could never recover. Now force_refresh takes the
rejected token string: the stored token equal to it is exchanged even inside the cooldown, at most once
per rejected token (the row lock serialises the callers); a stored token that differs from it is
returned without an exchange. Every exchange logs one info line with the process and its kind, never
the token. No real Questrade: respx only."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from trader.adapters.questrade.auth import TOKEN_URL, QuestradeAuth
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient
from trader.adapters.questrade.models import CandleRequest
from trader.crypto import Crypto
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db
T0 = datetime(2026, 9, 29, 13, 20, tzinfo=UTC)
CRYPTO = Crypto(Fernet.generate_key().decode())
API = "https://api05.iq.questrade.com/v1/"


class Exchanges:
    """The token endpoint: each exchange mints access-<n> / refresh-<n>."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.n += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{self.n}",
                "refresh_token": f"refresh-{self.n}",
                "token_type": "Bearer",
                "expires_in": 1800,
                "api_server": "https://api05.iq.questrade.com/",
            },
        )


def make(factory: sessionmaker[Session], clock: FixedClock) -> QuestradeAuth:
    auth = QuestradeAuth(factory, CRYPTO, clock)
    auth.seed("refresh-0")
    return auth


def exchanged(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == "questrade.token_exchanged"]


@respx.mock
def test_the_rejected_token_is_exchanged_even_inside_the_cooldown(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=Exchanges())
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    assert auth.access().token == "access-1"
    clock.advance(timedelta(seconds=10))  # well inside the 90 s cooldown
    assert auth.force_refresh("access-1").token == "access-2"
    assert route.call_count == 2


@respx.mock
def test_the_same_rejected_token_is_exchanged_only_once(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=Exchanges())
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    assert auth.force_refresh("access-1").token == "access-2"
    assert auth.force_refresh("access-1").token == "access-2"  # another caller, same rejected token
    assert route.call_count == 2
    clock.advance(timedelta(seconds=5))
    assert auth.force_refresh("access-2").token == "access-3"  # a new rejected token: one more
    assert route.call_count == 3


@respx.mock
def test_a_stored_token_newer_than_the_rejected_one_is_returned_without_an_exchange(
    db_factory: sessionmaker[Session],
) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=Exchanges())
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    clock.advance(timedelta(minutes=5))  # past the cooldown too
    assert auth.force_refresh("access-0-old").token == "access-1"
    assert route.call_count == 1


@respx.mock
def test_force_refresh_without_a_rejected_token_keeps_the_cooldown(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=Exchanges())
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    clock.advance(timedelta(seconds=60))
    assert auth.force_refresh().token == "access-1"
    assert route.call_count == 1


@respx.mock
def test_every_exchange_logs_its_process_and_kind_but_never_the_token(
    db_factory: sessionmaker[Session],
) -> None:
    respx.get(TOKEN_URL).mock(side_effect=Exchanges())
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    with capture_logs() as logs:
        auth.access()  # initial
        clock.advance(timedelta(seconds=10))
        auth.force_refresh("access-1")  # inside the cooldown, the stored token was rejected
        clock.advance(timedelta(minutes=2))
        auth.force_refresh("access-2")  # past the cooldown
        auth.force_refresh("access-2")  # no exchange: no line
    lines = exchanged(logs)
    assert [e["kind"] for e in lines] == ["initial", "cooldown", "forced"]
    assert all(e["log_level"] == "info" and "process" in e for e in lines)
    text = str(logs)
    for n in range(4):
        assert f"access-{n}" not in text and f"refresh-{n}" not in text


@respx.mock
async def test_a_client_on_the_real_auth_recovers_from_a_token_rejected_inside_the_cooldown(
    db_factory: sessionmaker[Session],
) -> None:
    """The 09-29 sequence: the preopen minted access-1 13 min before; the worker's first requests are
    rejected with it. The forced refresh, inside the cooldown, exchanges access-1 once and gives
    access-2, which Questrade accepts."""
    token_route = respx.get(TOKEN_URL).mock(side_effect=Exchanges())
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()  # access-1, as the preopen left it
    clock.advance(timedelta(seconds=30))  # inside the cooldown of that exchange

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer access-1":
            return httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})
        return httpx.Response(200, json={"candles": []})

    respx.get(url__regex=API + r"markets/candles/\d+").mock(side_effect=handler)
    start = T0 + timedelta(minutes=10)
    batch = [CandleRequest(i, start, start + timedelta(minutes=5), "FiveMinutes") for i in range(1, 31)]
    async with QuestradeClient(auth, clock, market_rps=1000.0) as c:
        got = await asyncio.wait_for(c.candles_many(batch, deadline_s=10), timeout=20)
    assert all(v == [] for v in got.values())
    assert token_route.call_count == 2  # the initial exchange and ONE forced one


@respx.mock
async def test_401_for_every_token_is_one_exchange_per_distinct_rejected_token(
    db_factory: sessionmaker[Session],
) -> None:
    token_route = respx.get(TOKEN_URL).mock(side_effect=Exchanges())
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"].removeprefix("Bearer "))
        return httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})

    respx.get(url__regex=API + r"markets/candles/\d+").mock(side_effect=handler)
    start = T0 + timedelta(minutes=10)
    batch = [CandleRequest(i, start, start + timedelta(minutes=5), "FiveMinutes") for i in range(1, 41)]
    async with QuestradeClient(auth, clock, market_rps=1000.0) as c:
        got = await asyncio.wait_for(c.candles_many(batch, deadline_s=10), timeout=20)
    assert all(isinstance(v, QuestradeApiError) and v.status == 401 for v in got.values())
    distinct = set(seen)
    # one initial exchange, then at most one per distinct token Questrade rejected (never a storm)
    assert token_route.call_count >= 2  # the rejected token was exchanged, not handed back
    # every forced exchange spent a token Questrade had rejected, each one once
    assert {f"access-{i}" for i in range(1, token_route.call_count)} <= distinct
    assert token_route.call_count - 1 <= len(distinct)
