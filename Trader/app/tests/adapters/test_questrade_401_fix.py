"""FIX-401 (Tue 2026-09-29): every 9:35 opening-bar request was HTTP 401 for 45 s and nothing recovered.

(a) a QuestradeApiError keeps Questrade's own error code and message (masked, one line, at most 200 chars);
(b) a 401 hands the rejected token STRING to force_refresh, and the client compares token strings (not
object identity) before refreshing; (c) candles_many fails fast when its first completed results are all
401 after a refresh, instead of burning the whole deadline. No real Questrade: respx only.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import (
    FAIL_FAST_401,
    MAX_QT_MESSAGE,
    TOKEN_REUSE_MARGIN,
    QuestradeApiError,
    QuestradeClient,
    missing_reason,
)
from trader.adapters.questrade.models import CandleRequest
from trader.market.clock import FixedClock

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 9, 29, 13, 35, 5, tzinfo=UTC)
START = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
INVALID = {"code": 1017, "message": "Access token is invalid"}
CANDLES = BASE + r"markets/candles/\d+"
BAR = {
    "start": "2026-09-29T09:30:00.000000-04:00",
    "end": "2026-09-29T09:35:00.000000-04:00",
    "open": 20,
    "high": 21,
    "low": 19,
    "close": 20.5,
    "volume": 9000,
}


async def no_sleep(_: float) -> None:
    return None


def reqs(n: int) -> list[CandleRequest]:
    return [CandleRequest(i, START, START + timedelta(minutes=5), "FiveMinutes") for i in range(1, n + 1)]


def other_tasks() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


class RecordingTokens:
    """A TokenSource that records the rejected token each forced refresh was given. `access()` returns a
    NEW AccessToken object every call (same string until a refresh), as a TokenSource re-reading the
    database does."""

    def __init__(self, expires_in: timedelta = timedelta(minutes=30)) -> None:
        self.current = "tok-A"
        self.expires_in = expires_in
        self.rejected: list[str | None] = []
        self.accessed = 0

    def access(self) -> AccessToken:
        self.accessed += 1
        return AccessToken(self.current, BASE, NOW + self.expires_in)

    def force_refresh(self, rejected: str | None = None) -> AccessToken:
        self.rejected.append(rejected)
        if rejected is None or rejected == self.current:
            self.current = "tok-B" if self.current == "tok-A" else self.current + "'"
        self.expires_in = timedelta(minutes=30)
        return AccessToken(self.current, BASE, NOW + self.expires_in)


# --- (a) Questrade's error code and message are kept --------------------------------------------------------


@respx.mock
async def test_api_error_keeps_questrades_code_and_message() -> None:
    respx.get(url__regex=CANDLES).mock(return_value=httpx.Response(401, json=INVALID))
    async with QuestradeClient(RecordingTokens(), FixedClock(NOW), sleep=no_sleep) as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.candles(1, START, START + timedelta(minutes=5), "FiveMinutes")
    assert err.value.status == 401
    assert err.value.code == 1017
    assert err.value.qt_message == "Access token is invalid"
    assert err.value.summary == "HTTP 401 1017 Access token is invalid"
    assert missing_reason(err.value) == "questrade_error: HTTP 401 1017 Access token is invalid"


@respx.mock
async def test_a_body_without_a_questrade_error_keeps_the_bare_status() -> None:
    respx.get(url__regex=CANDLES).mock(return_value=httpx.Response(404, text="no such symbol"))
    async with QuestradeClient(RecordingTokens(), FixedClock(NOW), sleep=no_sleep) as c:
        with pytest.raises(QuestradeApiError) as err:
            await c.candles(1, START, START + timedelta(minutes=5), "FiveMinutes")
    assert err.value.code is None and err.value.qt_message is None
    assert err.value.summary == "HTTP 404"
    assert missing_reason(err.value) == "questrade_error: HTTP 404"
    assert missing_reason(QuestradeApiError(500, "fake error")) == "questrade_error: HTTP 500"


def test_the_questrade_message_is_masked_one_line_and_capped() -> None:
    secret = "Bearer abcDEF1234567890ghijk"
    body = {"code": 1017, "message": f"Access\ntoken  {secret} is invalid " + "x" * 400}
    exc = QuestradeApiError.from_response(httpx.Response(401, json=body))
    assert exc.qt_message is not None
    assert "\n" not in exc.qt_message and "  " not in exc.qt_message
    assert "abcDEF1234567890ghijk" not in exc.summary
    assert len(exc.qt_message) <= MAX_QT_MESSAGE
    assert len(missing_reason(exc)) <= 200
    assert missing_reason(exc).startswith("questrade_error: HTTP 401 1017 Access token")


# --- (b) the rejected token string goes to force_refresh ----------------------------------------------------


@respx.mock
async def test_a_batch_rejected_for_token_a_recovers_with_token_b() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer tok-A":
            return httpx.Response(401, json=INVALID)
        return httpx.Response(200, json={"candles": [BAR]})

    route = respx.get(url__regex=CANDLES).mock(side_effect=handler)
    tokens = RecordingTokens()
    async with QuestradeClient(tokens, FixedClock(NOW), sleep=no_sleep, market_rps=1000.0) as c:
        got = await c.candles_many(reqs(30), deadline_s=5)
    assert len(got) == 30 and all(isinstance(v, list) and len(v) == 1 for v in got.values())
    assert tokens.rejected == ["tok-A"]  # one refresh, given the rejected string
    assert route.call_count <= 60


@respx.mock
async def test_the_client_compares_token_strings_not_objects() -> None:
    """The source hands back a new object for the same string on every access (the token is inside the
    reuse margin, so every request re-reads it): the 401 must still force a refresh."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer tok-A":
            return httpx.Response(401, json=INVALID)
        return httpx.Response(200, json={"time": "2026-09-29T09:35:05.000000-04:00"})

    respx.get(BASE + "time").mock(side_effect=handler)
    tokens = RecordingTokens(expires_in=TOKEN_REUSE_MARGIN)  # never reusable: re-read every call
    async with QuestradeClient(tokens, FixedClock(NOW), sleep=no_sleep) as c:
        await c.server_time()
    assert tokens.rejected == ["tok-A"]


@respx.mock
async def test_401_for_every_token_refreshes_once_per_distinct_rejected_token() -> None:
    respx.get(url__regex=CANDLES).mock(return_value=httpx.Response(401, json=INVALID))
    tokens = RecordingTokens()
    async with QuestradeClient(tokens, FixedClock(NOW), sleep=no_sleep, market_rps=1000.0) as c:
        got = await c.candles_many(reqs(40), deadline_s=5)
    assert all(isinstance(v, QuestradeApiError) and v.status == 401 for v in got.values())
    assert len(tokens.rejected) == len(set(tokens.rejected))  # never twice for the same token
    assert None not in tokens.rejected
    assert len(tokens.rejected) <= 40  # at most one per request, never a storm


class LegacyTokens:
    """A TokenSource written before FIX-401: force_refresh takes no argument."""

    def __init__(self) -> None:
        self.n = 1

    def access(self) -> AccessToken:
        return AccessToken(f"tok-{self.n}", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self) -> AccessToken:
        self.n += 1
        return self.access()


@respx.mock
async def test_a_token_source_without_the_rejected_argument_still_works() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer tok-1":
            return httpx.Response(401, json=INVALID)
        return httpx.Response(200, json={"time": "2026-09-29T09:35:05.000000-04:00"})

    respx.get(BASE + "time").mock(side_effect=handler)
    tokens = LegacyTokens()
    async with QuestradeClient(tokens, FixedClock(NOW), sleep=no_sleep) as c:
        await c.server_time()
    assert tokens.n == 2


# --- (c) fail fast on a batch that is all 401 ---------------------------------------------------------------


@respx.mock
async def test_candles_many_fails_fast_when_the_first_results_are_all_401() -> None:
    """The first FAIL_FAST_401 requests answer 401 at once; the rest would hang past any deadline. The
    batch returns at once with every request, the outstanding ones marked with the same 401."""
    first = set(range(1, FAIL_FAST_401 + 1))
    never = asyncio.Event()
    cancelled: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sid = int(request.url.path.rsplit("/", 1)[1])
        if sid not in first:
            try:
                await never.wait()
            except asyncio.CancelledError:
                cancelled.append(sid)
                raise
        return httpx.Response(401, json=INVALID)

    respx.get(url__regex=CANDLES).mock(side_effect=handler)
    batch = reqs(60)
    async with QuestradeClient(RecordingTokens(), FixedClock(NOW), market_rps=1000.0) as c:
        with capture_logs() as logs:
            got = await asyncio.wait_for(c.candles_many(batch, deadline_s=30), timeout=5)
        assert other_tasks() == []
    assert set(got) == set(batch)
    for r in batch:
        v = got[r]
        assert isinstance(v, QuestradeApiError) and v.status == 401 and v.code == 1017
    assert cancelled  # the hanging requests were cancelled, not waited for
    (err,) = [e for e in logs if e["event"] == "questrade.candles_fail_fast"]
    assert err["log_level"] == "error"
    assert err["completed"] >= FAIL_FAST_401 and err["requests"] == 60
    assert err["reason"] == "HTTP 401 1017 Access token is invalid"
    assert "tok-" not in str(err)


@respx.mock
async def test_a_success_among_the_first_results_disables_the_fail_fast() -> None:
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        sid = int(request.url.path.rsplit("/", 1)[1])
        if sid == 1:
            return httpx.Response(200, json={"candles": [BAR]})
        if sid <= 25:
            return httpx.Response(401, json=INVALID)
        await never.wait()
        raise AssertionError("unreachable")

    respx.get(url__regex=CANDLES).mock(side_effect=handler)
    batch = reqs(30)
    async with QuestradeClient(RecordingTokens(), FixedClock(NOW), market_rps=1000.0) as c:
        got = await asyncio.wait_for(c.candles_many(batch, deadline_s=0.5), timeout=5)
    assert set(got) == set(batch[:25])  # the hanging ones are left out at the deadline, as before
    assert isinstance(got[batch[0]], list)


@respx.mock
async def test_fail_fast_can_be_switched_off() -> None:
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        sid = int(request.url.path.rsplit("/", 1)[1])
        if sid <= 25:
            return httpx.Response(401, json=INVALID)
        await never.wait()
        raise AssertionError("unreachable")

    respx.get(url__regex=CANDLES).mock(side_effect=handler)
    batch = reqs(30)
    async with QuestradeClient(RecordingTokens(), FixedClock(NOW), market_rps=1000.0) as c:
        got = await asyncio.wait_for(c.candles_many(batch, deadline_s=0.5, fail_fast_401=None), timeout=5)
    assert set(got) == set(batch[:25])
