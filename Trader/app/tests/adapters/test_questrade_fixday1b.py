"""FIX-DAY1b: every quote carries `requested_at`, when the request that answered it was sent (the attempt
that got the 200, after the bucket's pacing and any 429 pause), so a capture can judge `quote_late` against
when it asked rather than when the answer came back."""

from datetime import UTC, datetime, timedelta

import httpx
import respx

from tests.adapters.test_questrade_quotebar import BASE, FakeTokens, quote_json
from trader.adapters.questrade.client import QuestradeClient
from trader.market.clock import FixedClock

T0 = datetime(2026, 10, 1, 13, 35, 0, tzinfo=UTC)


class VirtualTime:
    """One virtual clock for the client's wall clock, its buckets' monotonic time and its sleeps."""

    def __init__(self) -> None:
        self.clock = FixedClock(T0)
        self.mono = 1000.0

    def monotonic(self) -> float:
        return self.mono

    async def sleep(self, s: float) -> None:
        self.mono += s
        self.clock.advance(timedelta(seconds=s))


@respx.mock
async def test_quotes_carry_the_send_time_of_the_answering_attempt() -> None:
    vt = VirtualTime()
    respx.get(BASE + "markets/quotes").mock(
        side_effect=[
            httpx.Response(429, json={"code": 1006, "message": "Too many requests"}),  # no Reset: 0.5 s pause
            httpx.Response(200, json={"quotes": [quote_json()]}),
        ]
    )
    async with QuestradeClient(FakeTokens(), vt.clock, sleep=vt.sleep, monotonic=vt.monotonic) as c:
        (q,) = await c.quotes([34987])
    assert q.requested_at == T0 + timedelta(seconds=0.5)  # the retry's send, not the first attempt's
    assert q.fetched_at == T0 + timedelta(seconds=0.5)


@respx.mock
async def test_a_quote_without_a_pause_was_requested_when_fetched() -> None:
    vt = VirtualTime()
    respx.get(BASE + "markets/quotes").mock(return_value=httpx.Response(200, json={"quotes": [quote_json()]}))
    async with QuestradeClient(FakeTokens(), vt.clock, sleep=vt.sleep, monotonic=vt.monotonic) as c:
        (q,) = await c.quotes([34987])
    assert q.requested_at == T0
