"""FIX-DAY1b breaker (check attempt 1, Thu 2026-10-01): the concurrent timed captures driven through the REAL
QuestradeClient (its TokenBucket, 429 pauses and 401 refresh) over a mock transport, the late-cutoff
boundaries, the capture-done failure path and the after-hours end of the volume-factor candles.
No network, no database."""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.adapters.test_questrade_quotebar import FakeTokens, quote_json
from tests.market.test_data_service_fixday1 import DAY, OPEN
from trader.adapters.questrade.client import MARKET_RPS, QuestradeClient
from trader.market import data_service as ds
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.quote_bars import QUOTE_LATE, late_cutoff, quote_bar
from trader.market.types import Candle

CAL = SessionCalendar()
BAR_END = OPEN + timedelta(minutes=5)
S = timedelta(seconds=1)
N = 550  # six requests of <= 100 ids


class WallClock:
    """The client's wall clock, running with the real monotonic clock from `at`."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.m0 = time.monotonic()

    def now(self) -> datetime:
        return self.at + timedelta(seconds=time.monotonic() - self.m0)


class Server:
    """Questrade stand-in: each request takes `latency` s; records sends and the peak in flight."""

    def __init__(self, clock: WallClock, latency: float = 0.05, script: list[int] | None = None) -> None:
        self.clock = clock
        self.latency = latency
        self.script = list(script or [])  # statuses for the first requests (then 200)
        self.sends: list[tuple[float, int]] = []  # (monotonic, status)
        self.in_flight = 0
        self.peak = 0
        self.cancelled = 0
        self.reject: set[str] = set()  # Authorization headers answered 401 (an expired token)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        status = self.script.pop(0) if self.script else 200
        if request.headers.get("Authorization") in self.reject:
            status = 401
        self.sends.append((time.monotonic(), status))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.latency)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.in_flight -= 1
        if status == 429:
            return httpx.Response(429, json={"code": 1006, "message": "Too many requests"})
        if status == 401:
            return httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})
        wanted = request.url.params["ids"].split(",")
        served = self.clock.now().astimezone(BAR_END.tzinfo).isoformat()
        return httpx.Response(
            200,
            json={
                "quotes": [quote_json(symbolId=int(i), symbol=f"S{i}", lastTradeTime=served) for i in wanted]
            },
        )


class _NoDb:
    def __call__(self) -> Any:
        raise RuntimeError("no database in this breaker")


def _service(client: Any, clock: Any) -> ds.MarketDataService:
    service = ds.MarketDataService(_NoDb(), clock, CAL, client, opening_bar_source="quotes")  # type: ignore[arg-type]
    service._quote_qids.update({sid: 10_000 + sid for sid in range(1, N + 1)})
    return service


def _client(server: Server, tokens: FakeTokens | None = None) -> QuestradeClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    return QuestradeClient(tokens or FakeTokens(), server.clock, http=http)  # type: ignore[arg-type]


# 1. pacing ---------------------------------------------------------------------------------------------
async def test_concurrent_capture_keeps_the_rate_limit_and_holds_every_send_during_a_429_pause() -> None:
    clock = WallClock(BAR_END)
    server = Server(clock, latency=0.05, script=[429])  # the first request is refused: no Reset, 0.5 s pause
    async with _client(server) as client:
        detail = await _service(client, clock).capture_quotes(DAY, "bar", list(range(1, N + 1)))
    assert detail["quoted"] == N and detail["failed"] == 0
    times = sorted(t for t, _ in server.sends)
    assert len(times) == 7  # six requests plus the one retry
    gap = 1.0 / MARKET_RPS
    assert all(b - a >= gap * 0.9 for a, b in zip(times, times[1:], strict=False)), times
    assert server.peak <= 6
    t429 = next(t for t, s in server.sends if s == 429)
    # nothing is sent between the refused request's send + pause start and the pause's end (0.5 s)
    after = [t for t in times if t > t429]
    in_pause = [t for t in after if t < t429 + 0.5 - 0.02]
    assert len(in_pause) <= 1, f"sends during the 429 pause: {[round(t - t429, 3) for t in in_pause]}"


# 2. requested_at of the retried request -------------------------------------------------------------------
async def test_requested_at_is_the_successful_attempts_send_and_never_before_the_bar_end() -> None:
    clock = WallClock(BAR_END)
    server = Server(clock, latency=0.02, script=[429])
    async with _client(server) as client:
        quotes, errors = await _service(client, clock)._capture_pass(
            [(s, 10_000 + s) for s in range(1, N + 1)], ds.CAPTURE_DEADLINE_S
        )
    assert not errors and len(quotes) == N
    sent = sorted({q.requested_at for q in quotes.values()})  # one per request
    assert len(sent) == 6 and all(t is not None and t >= BAR_END for t in sent)
    assert max(sent) >= BAR_END + 0.45 * S  # the retried request: after its 0.5 s pause, not its first send
    for q in quotes.values():  # with a fresh print at the answer, no quote is late against its own send
        assert q.last_trade_time is not None and q.last_trade_time <= late_cutoff(BAR_END, q.requested_at)


# 3. a 401 mid-capture -------------------------------------------------------------------------------------
async def test_an_expired_token_mid_capture_is_refreshed_once_for_all_concurrent_requests() -> None:
    clock = WallClock(BAR_END)
    tokens = FakeTokens()
    server = Server(clock, latency=0.15)  # slow answers: all six are in flight with the old token
    server.reject.add("Bearer tok-0")
    async with _client(server, tokens) as client:
        detail = await _service(client, clock).capture_quotes(DAY, "bar", list(range(1, N + 1)))
    assert detail["quoted"] == N and detail["failed"] == 0
    assert tokens.forced == 1, "concurrent 401s on one expired token share one forced refresh"


async def test_a_persistent_401_mid_capture_cancels_the_rest_cleanly() -> None:
    """Every token refused (an account-level 401). Finding: concurrently, each of the six requests already
    had its first 401 before any second one, so each forces its own refresh (the refresh token rotates up to
    six times; the sequential pass rotated it once). Bounded by the request count, and nothing is left
    running."""
    clock = WallClock(BAR_END)
    tokens = FakeTokens()
    server = Server(clock, latency=0.05, script=[401] * 40)
    async with _client(server, tokens) as client:
        detail = await _service(client, clock).capture_quotes(DAY, "bar", list(range(1, N + 1)))
        await asyncio.sleep(0.1)
        assert server.in_flight == 0  # nothing left running against the server
    assert detail["quoted"] == 0 and detail["failed"] == N
    assert set(detail["missing_reasons"]) == {"questrade_error: HTTP 401"}
    assert 1 <= tokens.forced <= 6
    assert len(server.sends) <= 12
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert leftovers == []


# 4. the late-cutoff boundaries --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sent_after", "trade_after", "late"),
    [
        (None, 2.0, False),  # exactly +2 s after the bar end: not late (strictly after is late)
        (None, 2.000001, True),
        (0.0, 2.0, False),
        (1.9, 3.9, False),
        (1.9, 3.900001, True),
        (2.0, 4.0, False),  # sent at 09:35:02.0: cutoff 09:35:04.0
        (2.0, 4.000001, True),
        (2.1, 4.0, False),  # sent at 09:35:02.1: capped at 02.0, same cutoff
        (2.1, 4.1, True),
        (-0.5, 2.0, False),  # a send before the bar end counts as the bar end
        (-0.5, 2.000001, True),
    ],
)
def test_late_cutoff_boundaries(sent_after: float | None, trade_after: float, late: bool) -> None:
    from tests.market.test_data_service_fixday1 import quote

    asked = None if sent_after is None else BAR_END + sent_after * S
    q = quote(1, 5_000, BAR_END + trade_after * S, requested_at=asked)
    got = quote_bar(q, OPEN, BAR_END, asked_at=asked)
    assert (got == QUOTE_LATE) is late, got
    assert (q.last_trade_time > late_cutoff(BAR_END, asked)) is late  # type: ignore[operator]


# 5. capture-done unreadable --------------------------------------------------------------------------------
async def test_capture_done_unreadable_still_captures_once_and_a_readable_retry_never_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = WallClock(BAR_END)
    server = Server(clock, latency=0.01)
    async with _client(server) as client:
        service = _service(client, clock)  # every database call raises
        first = await service.capture_quotes(DAY, "bar", list(range(1, N + 1)))
        assert first["quoted"] == N and "skipped" not in first
        assert len(server.sends) == 6
        monkeypatch.setattr(service, "_capture_stored", lambda session_date, kind: True)
        again = await service.capture_quotes(DAY, "bar", list(range(1, N + 1)))
    assert again.get("skipped") == "already_captured"
    assert len(server.sends) == 6


# 6. the after-hours end of the factor's candles ------------------------------------------------------------
@pytest.mark.parametrize(
    ("after_close", "expected_after_close"),
    [
        (timedelta(minutes=15), timedelta(0)),  # the 16:15 post-close run: unchanged
        (timedelta(minutes=14, seconds=59), timedelta(0)),
        (timedelta(minutes=19, seconds=59), timedelta(0)),  # 16:04:59 published: no full candle yet
        (timedelta(minutes=20), timedelta(minutes=5)),
        (timedelta(hours=1, minutes=2), timedelta(minutes=45)),
        (timedelta(hours=4, minutes=14), timedelta(hours=3, minutes=55)),
        (timedelta(hours=4, minutes=15), timedelta(hours=4)),  # 20:00 ET
        (timedelta(hours=11), timedelta(hours=4)),  # the next night's re-run: never past 20:00 ET
        (-timedelta(hours=2), timedelta(0)),  # before the close
    ],
)
def test_afterhours_end_is_published_candles_only_and_at_most_20_00(
    after_close: timedelta, expected_after_close: timedelta
) -> None:
    close = CAL.session_close(DAY)
    service = _service(object(), FixedClock(close + after_close))
    end = service._afterhours_end(close)
    assert end == close + expected_after_close
    assert end <= close + ds.AFTERHOURS_SPAN
    assert end == close or end <= close + after_close - ds.CANDLE_PUBLISH_LAG


def test_afterhours_numerator_ignores_candles_past_the_end_even_if_served() -> None:
    """The filter in measure_volume_scale (`close <= c.start < candles_end`) over a served list that runs
    past the requested end: only the published window counts, the regular session never does."""
    close = CAL.session_close(DAY)
    step = timedelta(minutes=5)
    served = [
        Candle(close - step, close, *(1,) * 4, 1_000, None),  # type: ignore[arg-type]
        *[Candle(close + step * i, close + step * (i + 1), *(1,) * 4, 10, None) for i in range(60)],  # type: ignore[arg-type]
    ]
    service = _service(object(), FixedClock(close + timedelta(hours=1, minutes=2)))
    end = service._afterhours_end(close)
    counted = sum(c.volume for c in served if close <= c.start < end)
    assert counted == 9 * 10
