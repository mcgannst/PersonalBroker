"""DB-T2 gauntlet (live dashboard): breaker tests for the worker's `QuoteTap` and the `MarkPublisher`.

The tap sits in front of every decision-path market-data call of the live worker during the soak, including
the 9:35 opening-bar batch that failed on Mon 2026-09-28. These tests drive the REAL `QuestradeClient`
(respx, never the network) and the REAL `MarketDataService.opening_bars` on an event loop whose clock is
virtual (it jumps to the next timer instead of waiting), so a 45 s batch at 17 req/s with 429 pauses runs in
milliseconds and every dispatch time, cancellation and loop iteration can be compared byte for byte with and
without the tap. The publisher tests use fakes, the worker harness and the testcontainer database.
"""

import ast
import asyncio
import dataclasses
import gc
import json
import selectors
import threading
import time
import tracemalloc
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.factories import add_run, add_symbol
from tests.marks.test_publisher import Events, Tap
from tests.marks.test_publisher_db import _position, q
from tests.test_worker import TUE, Harness, VirtualTime, _run, et
from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import MARKET_RPS, CallStats, QuestradeApiError, QuestradeClient
from trader.adapters.questrade.models import CandleRequest, QtQuote, QtSymbol
from trader.db import models as m
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import FETCH_DEADLINE_S, MarketDataService
from trader.market.types import Candle, Interval, OpeningBars
from trader.marks import publisher as pub_mod
from trader.marks import tap as tap_mod
from trader.marks.publisher import MarkPublisher, MarkPublisherDeps
from trader.marks.tap import QuoteTap
from trader.marks.types import MAX_OBSERVATIONS_PER_SYMBOL, MAX_TAP_SYMBOLS
from trader.notify.relay import ALERT_LEVELS
from trader.worker import Worker

ET = ZoneInfo("America/New_York")
BASE = "https://api05.iq.questrade.com/v1/"
CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # a Tuesday session (EDT)
OPEN = CAL.session_open(DAY)
AFTER_BAR = OPEN + timedelta(minutes=5, seconds=5)  # the 9:35:05 ET opening-bar scan
TOKEN = AccessToken("tok-db-t2-gauntlet", BASE, AFTER_BAR + timedelta(hours=2))
BAR_JSON: dict[str, object] = {
    "start": OPEN.isoformat(),
    "end": (OPEN + timedelta(minutes=5)).isoformat(),
    "open": 21.0,
    "high": 21.5,
    "low": 20.9,
    "close": 21.4,
    "volume": 5000,
}
CANDLES_URL = BASE + r"markets/candles/\d+"
QUOTES_URL = BASE + "markets/quotes"
EPS = 1e-9
TRADER = Path(__file__).resolve().parents[2] / "trader"
MAX_ITERATIONS = 3_000_000
NO_DB: Any = None  # the factory of a publisher whose database step is replaced (never called)


# --- a virtual-time event loop ----------------------------------------------------------------------------


class _JumpSelector(selectors.BaseSelector):
    """Never waits: a select with a timeout moves virtual time forward by it and polls. Counts iterations."""

    def __init__(self) -> None:
        self._real = selectors.DefaultSelector()
        self.vt = 0.0
        self.iterations = 0

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._real.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.modify(fileobj, events, data)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        self.iterations += 1
        if self.iterations > MAX_ITERATIONS:
            raise RuntimeError("virtual loop ran away (nothing scheduled: a deadlock)")
        if timeout is not None and timeout > 0:
            self.vt += timeout
        return self._real.select(0)

    def get_map(self) -> Any:
        return self._real.get_map()

    def close(self) -> None:
        self._real.close()


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self.sel = _JumpSelector()
        super().__init__(self.sel)

    def time(self) -> float:
        return self.sel.vt


def run_virtual[T](main: Callable[[], Coroutine[Any, Any, T]]) -> tuple[T, VirtualLoop]:
    loop = VirtualLoop()
    try:
        out = loop.run_until_complete(main())
        left = [t for t in asyncio.all_tasks(loop) if not t.done()]
        assert left == [], f"tasks outlived the call: {left}"
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
    return out, loop


class LoopClock:
    """Wall time that follows the running loop's (virtual) clock."""

    def __init__(self, base: datetime) -> None:
        self.base = base

    def now(self) -> datetime:
        try:
            t = asyncio.get_running_loop().time()
        except RuntimeError:
            t = 0.0
        return self.base + timedelta(seconds=t)


class FakeTokens:
    def access(self) -> AccessToken:
        return TOKEN

    def force_refresh(self) -> AccessToken:
        return TOKEN


class RecordingClient(QuestradeClient):
    """The real client; records what each public call returned or raised (the very objects)."""

    def __init__(self, clock: Any, **kw: Any) -> None:
        super().__init__(FakeTokens(), clock, monotonic=asyncio.get_running_loop().time, **kw)
        self._cache_token(TOKEN)  # no token thread: virtual time must not jump while a thread works
        self.returned: list[tuple[str, object]] = []
        self.raised: list[tuple[str, BaseException]] = []

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        try:
            out = await super().quotes(ids)
        except BaseException as exc:
            self.raised.append(("quotes", exc))
            raise
        self.returned.append(("quotes", out))
        return out

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        try:
            out = await super().candles(symbol_id, start, end, interval)
        except BaseException as exc:
            self.raised.append(("candles", exc))
            raise
        self.returned.append(("candles", out))
        return out

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        try:
            out = await super().candles_many(reqs, deadline_s=deadline_s)
        except BaseException as exc:
            self.raised.append(("candles_many", exc))
            raise
        self.returned.append(("candles_many", out))
        return out

    def got(self, name: str) -> list[object]:
        return [o for n, o in self.returned if n == name]

    def threw(self, name: str) -> list[BaseException]:
        return [e for n, e in self.raised if n == name]


class Server:
    """respx side effect for candles and quotes. Records dispatch and cancellation times (virtual)."""

    def __init__(
        self,
        *,
        latency: float = 0.2,
        hang: frozenset[int] = frozenset(),
        not_found: frozenset[int] = frozenset(),
        every_429: int = 0,
        reset_in: float = 1.0,
        quotes_status: int = 200,
    ) -> None:
        self.latency = latency
        self.hang = hang
        self.not_found = not_found
        self.every_429 = every_429
        self.reset_in = reset_in
        self.quotes_status = quotes_status
        self.n = 0
        self.dispatched: list[tuple[float, int]] = []
        self.cancelled: list[tuple[float, int]] = []
        self.throttled: list[float] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        loop = asyncio.get_running_loop()
        tail = request.url.path.rsplit("/", 1)[1]
        qid = int(tail) if tail.isdigit() else -1
        self.n += 1
        n = self.n
        self.dispatched.append((loop.time(), qid))
        try:
            if qid in self.hang:
                await asyncio.Event().wait()
            await asyncio.sleep(self.latency)
        except asyncio.CancelledError:
            self.cancelled.append((loop.time(), qid))
            raise
        if self.every_429 and n % self.every_429 == 0:
            self.throttled.append(loop.time())
            reset = (AFTER_BAR + timedelta(seconds=loop.time())).timestamp() + self.reset_in
            return httpx.Response(
                429, headers={"X-RateLimit-Reset": str(reset), "X-RateLimit-Remaining": "0"}
            )
        if qid == -1:  # markets/quotes
            if self.quotes_status != 200:
                return httpx.Response(self.quotes_status, text="nope")
            ids = [int(x) for x in request.url.params["ids"].split(",")]
            return httpx.Response(200, json={"quotes": [_quote_json(i) for i in ids]})
        if qid in self.not_found:
            return httpx.Response(404, text="no such symbol")
        return httpx.Response(200, json={"candles": [BAR_JSON | {"volume": qid}]})


def _quote_json(i: int) -> dict[str, object]:
    return {
        "symbolId": i,
        "symbol": f"S{i}",
        "bidPrice": 10.0,
        "askPrice": 10.2,
        "lastTradePrice": 10.1,
        "lastTradePriceTrHrs": 10.1,
        "volume": 100,
        "lastTradeTime": AFTER_BAR.isoformat(),
        "delay": 0,
        "isHalted": False,
    }


def creq(qid: int) -> CandleRequest:
    return CandleRequest(qid, OPEN, OPEN + timedelta(minutes=5), "FiveMinutes")


class RecordingService(MarketDataService):
    """The real service; records the dict `_fetch_opening_bars` handed back (to compare identities)."""

    received: list[object]

    async def _fetch_opening_bars(
        self, session_date: date, reqs: list[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        out = await super()._fetch_opening_bars(session_date, reqs)
        self.received.append(out)
        return out


def _market_logs(logs: list[Any]) -> list[dict[str, Any]]:
    return [dict(e) for e in logs if str(e.get("event", "")).startswith(("market.", "marks."))]


def _stats(client: QuestradeClient) -> dict[str, dict[str, Any]]:
    return {k: dataclasses.asdict(v) for k, v in client.stats.items()}


# === 1. the 9:35 batch through the real service and client, with and without the tap ======================

N_BATCH = 800
FIRST_QID = 5000
HANG = frozenset(FIRST_QID + i for i in (3, 150, 377, 512, 640))
NOT_FOUND = frozenset(FIRST_QID + i for i in (42, 99))


@dataclasses.dataclass
class BatchRun:
    got: OpeningBars
    dispatched: list[tuple[float, int]]
    cancelled: list[tuple[float, int]]
    throttled: list[float]
    logs: list[dict[str, Any]]
    stats: dict[str, dict[str, Any]]
    iterations: int
    vt: float
    same_object: bool
    batches: list[dict[str, Any]]


def _opening_run(factory: sessionmaker[Session], sids: list[int], *, tapped: bool) -> BatchRun:
    server = Server(latency=0.2, hang=HANG, not_found=NOT_FOUND, every_429=97)

    async def main() -> tuple[OpeningBars, RecordingClient, RecordingService, Any]:
        clock = LoopClock(AFTER_BAR)
        async with RecordingClient(clock) as client:
            inner: Any = QuoteTap(client, clock) if tapped else client
            svc = RecordingService(factory, clock, CAL, inner)  # the production deadline (45 s)
            svc.received = []
            got = await svc.opening_bars(DAY, sids)
        return got, client, svc, inner

    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=CANDLES_URL).mock(side_effect=server)
        with capture_logs() as logs:
            (got, client, svc, inner), loop = run_virtual(main)
    batches = inner.health_detail()["candle_batches"] if tapped else []
    with factory() as s:  # the next run starts from an empty candle cache again
        s.execute(delete(m.IntradayCandle))
        s.commit()
    return BatchRun(
        got=got,
        dispatched=server.dispatched,
        cancelled=server.cancelled,
        throttled=server.throttled,
        logs=_market_logs(logs),
        stats=_stats(client),
        iterations=loop.sel.iterations,
        vt=loop.time(),
        same_object=client.got("candles_many")[0] is svc.received[0],
        batches=batches,
    )


@pytest.mark.db
def test_0935_batch_through_the_tap_is_byte_identical_on_a_virtual_clock(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        sids = [add_symbol(s, f"S{qid}", questrade_id=qid) for qid in range(FIRST_QID, FIRST_QID + N_BATCH)]
        s.commit()
    sid_of = dict(zip(range(FIRST_QID, FIRST_QID + N_BATCH), sids, strict=True))
    plain = _opening_run(db_factory, sids, tapped=False)
    tapped = _opening_run(db_factory, sids, tapped=True)

    # The batch itself behaves as FIX-OPENBARS / FIX-PACING intend (so the comparison below is not vacuous).
    (fields,) = [e for e in plain.logs if e["event"].startswith("market.opening_bars")]
    assert fields["event"] == "market.opening_bars_timeout"
    assert fields["deadline_s"] == FETCH_DEADLINE_S == 45.0 and fields["elapsed_s"] == 45.0
    assert 500 < fields["completed"] < N_BATCH and fields["completed"] + fields["outstanding"] == N_BATCH
    times = [t for t, _ in plain.dispatched]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert min(gaps) >= 1 / MARKET_RPS - EPS  # paced at 17 req/s
    assert len(plain.throttled) >= 5 and plain.stats["market"]["http_429"] == len(plain.throttled)
    for r in plain.throttled:  # each 429's pause (Reset + margin = 1.05 s) holds every caller back
        assert not [t for t in times if r < t < r + 1.05 - 1e-6]  # (Reset is a float epoch: ~1e-7 s)
    assert {qid for _, qid in plain.cancelled} >= HANG
    assert all(t == pytest.approx(45.0) for t, _ in plain.cancelled)  # cancelled at the deadline, no later
    assert all(plain.got.missing[sid_of[qid]] == "timeout" for qid in HANG)
    assert all(plain.got.missing[sid_of[qid]] == "questrade_error: HTTP 404" for qid in NOT_FOUND)
    assert plain.vt == pytest.approx(45.0 + 0.0, abs=0.5)
    assert plain.same_object and tapped.same_object  # the service got the client's very dict

    # Through the tap: the same dispatches (time and order), cancellations, 429 pauses, results, log fields,
    # client counters, and even the same number of event-loop iterations (the tap adds no scheduling point).
    assert tapped.dispatched == plain.dispatched
    assert tapped.cancelled == plain.cancelled
    assert tapped.throttled == plain.throttled
    assert tapped.got == plain.got
    assert tapped.logs == plain.logs
    assert tapped.stats == plain.stats
    assert tapped.iterations == plain.iterations
    assert tapped.vt == plain.vt

    # ... and the tap counted the batch as S10 says.
    (batch,) = tapped.batches
    assert batch["symbols"] == N_BATCH and batch["raised"] is None
    assert batch["completed"] + batch["errors"] == fields["completed"] and batch["errors"] == len(NOT_FOUND)
    assert batch["outstanding"] == fields["outstanding"]
    assert batch["elapsed_s"] == 45.0 and batch["deadline_s"] == 45.0
    assert batch["http_429"] == plain.stats["market"]["http_429"]
    assert batch["pause_s"] == round(plain.stats["market"]["pause_s"], 3)


# === 2. caller cancellation mid-batch ======================================================================


def _cancel_run(*, tapped: bool) -> dict[str, Any]:
    server = Server(latency=0.2)

    async def main() -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        clock = LoopClock(AFTER_BAR)
        seen: dict[str, BaseException] = {}
        async with RecordingClient(clock) as client:
            inner: Any = QuoteTap(client, clock) if tapped else client
            reqs = [creq(i) for i in range(1, 301)]

            async def caller() -> Any:
                try:
                    return await inner.candles_many(reqs, deadline_s=45.0)
                except BaseException as exc:
                    seen["caller"] = exc
                    raise

            task = loop.create_task(caller())
            loop.call_at(10.0, task.cancel)
            with pytest.raises(asyncio.CancelledError):
                await task
            stopped_at = loop.time()
            leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
            before_probe = len(server.dispatched)
            await client.candles(9999, OPEN, OPEN + timedelta(minutes=5), "FiveMinutes")
            (inner_exc,) = client.threw("candles_many")
            return {
                "stopped_at": stopped_at,
                "leftovers": leftovers,
                "dispatched": list(server.dispatched),
                "cancelled": list(server.cancelled),
                "probe": server.dispatched[before_probe],
                "same_exc": inner_exc is seen["caller"],
                "batches": inner.health_detail()["candle_batches"] if tapped else [],
            }

    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=CANDLES_URL).mock(side_effect=server)
        out, loop = run_virtual(main)
    out["iterations"] = loop.sel.iterations
    return out


def test_cancelling_the_caller_mid_batch_through_the_tap_cancels_every_child_as_before() -> None:
    plain = _cancel_run(tapped=False)
    tapped = _cancel_run(tapped=True)
    assert plain["stopped_at"] == pytest.approx(10.0)
    assert plain["leftovers"] == [] and tapped["leftovers"] == []
    assert 150 < len(plain["dispatched"]) - 1 < 300  # ~17/s for 10 s, then cut
    assert plain["cancelled"] and all(t == pytest.approx(10.0) for t, _ in plain["cancelled"])
    assert not [t for t, qid in plain["dispatched"] if 10.0 < t and qid != 9999]  # nothing after the cancel
    # the bucket gave the abandoned queue back: the next call is paced from the last slot actually used
    last_used = max(t for t, qid in plain["dispatched"] if qid != 9999)
    assert plain["probe"][0] == pytest.approx(max(10.0, last_used + 1 / MARKET_RPS))
    for key in ("stopped_at", "dispatched", "cancelled", "probe", "iterations"):
        assert tapped[key] == plain[key], key
    assert plain["same_exc"] and tapped["same_exc"]  # the caller got the inner CancelledError itself
    (batch,) = tapped["batches"]
    assert batch["raised"] == "CancelledError" and batch["outstanding"] == 300 and batch["completed"] == 0


# === 3. no scheduling point of its own =====================================================================


def _one_call(call: str, *, tapped: bool) -> tuple[Any, int, float, bool]:
    server = Server(latency=0.3, hang=frozenset({3}))

    async def main() -> tuple[Any, bool]:
        clock = LoopClock(AFTER_BAR)
        async with RecordingClient(clock) as client:
            inner: Any = QuoteTap(client, clock) if tapped else client
            if call == "quotes":
                out: Any = await inner.quotes([1, 2, 3])
            elif call == "candles":
                out = await inner.candles(7, OPEN, OPEN + timedelta(minutes=5), "FiveMinutes")
            else:
                out = await inner.candles_many([creq(i) for i in (1, 2, 3, 4, 2)], deadline_s=45.0)
            return out, client.got(call)[-1] is out

    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=CANDLES_URL).mock(side_effect=server)
        router.get(QUOTES_URL).mock(side_effect=server)
        (out, same), loop = run_virtual(main)
    return out, loop.sel.iterations, loop.time(), same


async def _sync_first_send(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError("suspended although the real client completes without suspending")


@pytest.mark.parametrize("call", ["quotes", "candles", "candles_many"])
async def test_the_tap_never_adds_a_loop_iteration_for_suspending_or_synchronous_inner_calls(
    call: str,
) -> None:
    # a real client that suspends (HTTP latency, a request that hangs until the 45 s deadline)
    plain = await asyncio.to_thread(_one_call, call, tapped=False)
    tapped = await asyncio.to_thread(_one_call, call, tapped=True)
    assert tapped[0] == plain[0] and tapped[3] and plain[3]  # equal results; the tap returned the very object
    assert (tapped[1], tapped[2]) == (plain[1], plain[2])  # same loop iterations and virtual end time
    # a real client that completes synchronously (nothing to fetch): the tapped coroutine does too
    async with QuestradeClient(FakeTokens(), FixedClock(AFTER_BAR)) as client:
        tap = QuoteTap(client, FixedClock(AFTER_BAR))
        if call == "quotes":
            assert await _sync_first_send(tap.quotes([])) == []
        elif call == "candles":
            late = OPEN + timedelta(minutes=5)
            assert await _sync_first_send(tap.candles(1, late, late, "FiveMinutes")) == []
        else:
            assert await _sync_first_send(tap.candles_many([], deadline_s=45.0)) == {}


# === 4. exceptions from the real client reach the caller as the same instance =============================


@respx.mock
async def test_real_client_exceptions_reach_the_caller_as_the_same_instance() -> None:
    server = Server(latency=0.0, quotes_status=404, hang=frozenset({77}))
    respx.get(QUOTES_URL).mock(side_effect=server)
    respx.get(url__regex=CANDLES_URL).mock(side_effect=server)
    async with RecordingClient(FixedClock(AFTER_BAR)) as client:
        tap = QuoteTap(client, FixedClock(AFTER_BAR))
        with capture_logs() as logs:
            # a QuestradeApiError (HTTP 404)
            with pytest.raises(QuestradeApiError) as api:
                await tap.quotes([1, 2])
            assert api.value is client.threw("quotes")[-1] and api.value.status == 404
            assert api.value.__context__ is None and tap.drain() == []
            # a non-API exception from candles (naive datetimes)
            with pytest.raises(ValueError, match="timezone-aware") as bad:
                await tap.candles(
                    1, datetime(2026, 10, 6, 9, 30), datetime(2026, 10, 6, 9, 35), "FiveMinutes"
                )
            assert bad.value is client.threw("candles")[-1]
            # a non-API exception from candles_many (an unhashable request) that ALSO breaks the tap's own
            # count of the batch (it hashes the requests too): the caller still gets the inner instance
            weird: Any = [["not", "hashable"]]
            with pytest.raises(TypeError) as unhashable:
                await tap.candles_many(weird, deadline_s=45.0)
            assert unhashable.value is client.threw("candles_many")[-1]
            assert unhashable.value.__context__ is None
            # a CancelledError while the quote request is in flight
            seen: dict[str, BaseException] = {}

            async def caller() -> None:
                try:
                    await tap.candles(77, OPEN, OPEN + timedelta(minutes=5), "FiveMinutes")
                except BaseException as exc:
                    seen["exc"] = exc
                    raise

            task = asyncio.create_task(caller())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert seen["exc"] is client.threw("candles")[-1]
            assert isinstance(seen["exc"], asyncio.CancelledError)
    warnings = [e for e in logs if e.get("event") == "marks.tap_bookkeeping_failed"]
    assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"  # the TypeError count, once


# === 5. the tap's bookkeeping raising never changes the caller's result or exception ====================


class IgnoresDeadline:
    """A QuoteClient that ignores `deadline_s` and sleeps: only the service's own guard stops it."""

    def __init__(self) -> None:
        self.stats: dict[str, CallStats] = {"market": CallStats(), "account": CallStats()}
        self.deadlines: list[float | None] = []

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        return []

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        return []

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.deadlines.append(deadline_s)
        self.stats["market"].requests += len(reqs)
        await asyncio.sleep(3600)
        return {}


def _guard_run(factory: sessionmaker[Session], sids: list[int], *, tapped: bool) -> tuple[Any, ...]:
    async def main() -> tuple[OpeningBars, list[float | None]]:
        clock = LoopClock(AFTER_BAR)
        client = IgnoresDeadline()
        inner: Any = QuoteTap(client, clock) if tapped else client
        got = await MarketDataService(factory, clock, CAL, inner).opening_bars(DAY, sids)
        return got, client.deadlines

    with capture_logs() as logs:
        (got, deadlines), loop = run_virtual(main)
    fields = [
        {k: v for k, v in e.items() if k != "log_level"}
        for e in logs
        if str(e.get("event", "")).startswith("market.opening_bars")
    ]
    return got, deadlines, loop.time(), loop.sel.iterations, fields


def _boom(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("tap bookkeeping bug")


@pytest.mark.db
@pytest.mark.parametrize("broken", ["CandleBatch", "_roll_day", "_market", "clock"])
def test_a_bookkeeping_bug_never_changes_the_callers_result_or_exception(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    with db_factory() as s:
        sids = [add_symbol(s, t, questrade_id=101 + i) for i, t in enumerate(["AAA", "BBB", "CCC"])]
        s.commit()
    plain = _guard_run(db_factory, sids, tapped=False)
    if broken == "CandleBatch":  # the end-of-batch record fails while the guard's CancelledError propagates
        monkeypatch.setattr(tap_mod, "CandleBatch", _boom)
    elif broken == "_market":  # the snapshot before the call works, the one after it fails
        real_market = QuoteTap._market
        calls = {"n": 0}

        def market_once(self: QuoteTap) -> dict[str, float]:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("tap bookkeeping bug")
            return real_market(self)

        monkeypatch.setattr(QuoteTap, "_market", market_once)
    elif broken == "clock":  # the tap's own clock (not the service's) raises everywhere
        real_init = QuoteTap.__init__

        class BadClock:
            def now(self) -> datetime:
                raise RuntimeError("tap clock broke")

        def init(self: QuoteTap, inner: Any, clock: Any) -> None:
            real_init(self, inner, BadClock())

        monkeypatch.setattr(QuoteTap, "__init__", init)
    else:
        monkeypatch.setattr(QuoteTap, broken, _boom)
    tapped = _guard_run(db_factory, sids, tapped=True)
    # the 9:35 guard fired identically: same all-"timeout" result at the same (virtual) moment, the same
    # deadline forwarded, the same log fields, the same loop iterations, and no exception of the tap's own
    assert plain[0].bars == {} and set(plain[0].missing.values()) == {"timeout"}
    assert plain[2] == pytest.approx(45.0 + 5.0)
    assert tapped[0] == plain[0] and tapped[1] == plain[1] == [45.0]
    assert tapped[2] == plain[2] and tapped[3] == plain[3]
    assert [{k: v for k, v in f.items() if k != "elapsed_s"} for f in tapped[4]] == [
        {k: v for k, v in f.items() if k != "elapsed_s"} for f in plain[4]
    ]


# === 6. a double fault: the bookkeeping fails AND its warning cannot be logged ============================


class RaisingLog:
    def warning(self, *_a: Any, **_k: Any) -> None:
        raise RuntimeError("log sink broke")

    info = debug = warning


@pytest.mark.xfail(
    strict=True,
    reason="DB-T2 gauntlet finding F1: QuoteTap._failed (and health_detail's log) call the logger unguarded "
    "inside `except Exception:`; a raising logger replaces the caller's result or exception (S1a)",
)
async def test_a_raising_logger_during_a_bookkeeping_failure_still_leaves_the_callers_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quotes = [QtQuote(1, "S1", None, None, Decimal("1"), None, 0, None, 0, False, None)]

    class Inner:
        stats = None

        async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
            return quotes

        async def candles(self, *a: Any) -> list[Candle]:
            return []

        async def candles_many(
            self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
        ) -> Any:
            raise asyncio.CancelledError("the 9:35 guard")

    monkeypatch.setattr(tap_mod, "log", RaisingLog())
    monkeypatch.setattr(QuoteTap, "_observe", _boom)
    monkeypatch.setattr(tap_mod, "CandleBatch", _boom)
    outcome: list[str] = []
    try:  # a fresh tap for each call: each one starts a new failure streak (the first failure logs)
        assert await QuoteTap(Inner(), FixedClock(AFTER_BAR)).quotes([1]) is quotes
        outcome.append("quotes ok")
    except Exception as exc:  # noqa: BLE001
        outcome.append(f"quotes raised {type(exc).__name__}")
    try:
        await QuoteTap(Inner(), FixedClock(AFTER_BAR)).candles_many([creq(1)], deadline_s=45.0)
    except asyncio.CancelledError:
        outcome.append("cancel kept")
    except Exception as exc:  # noqa: BLE001
        outcome.append(f"cancel replaced by {type(exc).__name__}")
    assert outcome == ["quotes ok", "cancel kept"]


# === 7. bounded memory under 10,000 symbols and many polls ===============================================


class PollInner:
    """Every call returns a fresh list of fresh quotes (the client builds new objects each poll)."""

    stats = None

    def __init__(self) -> None:
        self.price = Decimal("10.00")

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        p = self.price
        return [QtQuote(i, "S", p, p, p, p, 1, None, 0, False, None) for i in ids]

    async def candles(self, *a: Any) -> list[Candle]:
        return []

    async def candles_many(self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None) -> Any:
        return {}


def _held(tap: QuoteTap) -> tuple[int, int, int]:
    pending = tap._pending
    return (
        len(pending),
        max((len(d) for d in pending.values()), default=0),
        sum(len(d) for d in pending.values()),
    )


async def test_memory_stays_bounded_under_10000_symbols_and_many_polls() -> None:
    inner = PollInner()
    tap = QuoteTap(inner, FixedClock(AFTER_BAR))
    big = list(range(1, 10_001))
    small = list(range(20_001, 21_001))  # the 1,000 most recent: kept, up to 30 each
    tracemalloc.start()
    try:
        for _ in range(20):
            await tap.quotes(big)
            assert _held(tap) == (MAX_TAP_SYMBOLS, 1, MAX_TAP_SYMBOLS)
        samples: list[int] = []
        for i in range(90):
            await tap.quotes(small)
            if i in (40, 89):
                gc.collect()
                samples.append(tracemalloc.get_traced_memory()[0])
        assert _held(tap) == (MAX_TAP_SYMBOLS, MAX_OBSERVATIONS_PER_SYMBOL, 30_000)
        assert samples[1] - samples[0] < 256 * 1024, f"grew {samples[1] - samples[0]} bytes at the cap"
    finally:
        tracemalloc.stop()
    # the bookkeeping cost on the loop stays small: a realistic 550-symbol scan quote, and the worst case
    fresh = await inner.quotes(big)
    t0 = time.perf_counter()
    tap._observe(fresh)
    worst = time.perf_counter() - t0
    scan = await inner.quotes(list(range(30_001, 30_551)))
    t0 = time.perf_counter()
    tap._observe(scan)
    typical = time.perf_counter() - t0
    assert typical < 0.005 and worst < 0.25, (typical, worst)
    drained = tap.drain()
    # the 1,000 most recently observed ids survive (the scan's 550 and the big poll's last 450), one each
    assert [o.qt_id for o in drained] == list(range(9_551, 10_001)) + list(range(30_001, 30_551))
    assert tap.drain() == [] and _held(tap) == (0, 0, 0)


# === 8. the S10 day roll at ET midnight across both DST changes ========================================


class Counting:
    def __init__(self) -> None:
        self.stats: dict[str, CallStats] = {"market": CallStats(requests=100), "account": CallStats()}

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        self.stats["market"].requests += 1
        return []

    async def candles(self, *a: Any) -> list[Candle]:
        return []

    async def candles_many(self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None) -> Any:
        self.stats["market"].requests += len(reqs)
        self.stats["market"].http_429 += 1
        self.stats["market"].pause_s += 0.25
        return {}


def _et(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=ET).astimezone(UTC)


def _qt(tap: QuoteTap) -> tuple[str, str, int, int, int]:
    detail = tap.health_detail()
    json.dumps(detail, allow_nan=False)
    qt = detail["questrade"]
    return (
        qt["day"],
        qt["since"],
        qt["market"]["requests"],
        qt["market"]["http_429"],
        len(detail["candle_batches"]),
    )


async def test_the_day_roll_happens_at_et_midnight_across_both_dst_changes() -> None:
    clock = FixedClock(_et(2026, 10, 31, 22, 0))
    inner = Counting()
    tap = QuoteTap(inner, clock)
    built = clock.now().isoformat()
    assert _qt(tap) == ("2026-10-31", built, 100, 0, 0)  # zero baseline: the counts the process has
    clock.set(_et(2026, 10, 31, 23, 59, 59))
    await tap.quotes([1])
    assert _qt(tap)[2] == 101
    # 00:00:01 EDT on Sun Nov 1 (DST ends at 02:00): the baseline is the counters BEFORE this batch
    roll = datetime(2026, 11, 1, 4, 0, 1, tzinfo=UTC)
    clock.set(roll)
    await tap.candles_many([creq(1)], deadline_s=45.0)
    assert _qt(tap) == ("2026-11-01", roll.isoformat(), 1, 1, 1)
    # 24 h after that midnight is only 23:00 EST: still Nov 1 (a 25-hour day)
    clock.set(datetime(2026, 11, 2, 4, 30, tzinfo=UTC))
    await tap.quotes([1])
    assert _qt(tap) == ("2026-11-01", roll.isoformat(), 2, 1, 1)
    clock.set(datetime(2026, 11, 2, 4, 59, 59, tzinfo=UTC))  # 23:59:59 EST Nov 1
    await tap.candles_many([creq(1)], deadline_s=45.0)
    assert _qt(tap)[0] == "2026-11-01" and _qt(tap)[4] == 2
    # 00:00:00 EST Nov 2: a health read alone rolls the day; yesterday's batches are not today's
    clock.set(datetime(2026, 11, 2, 5, 0, tzinfo=UTC))
    assert _qt(tap) == ("2026-11-02", clock.now().isoformat(), 0, 0, 0)
    # spring forward (Sun Mar 14 2027, 02:00 EST -> 03:00 EDT): a 23-hour day
    clock.set(datetime(2027, 3, 14, 5, 0, 30, tzinfo=UTC))  # 00:00:30 EST Mar 14
    await tap.quotes([1])
    assert _qt(tap)[0] == "2027-03-14"
    clock.set(datetime(2027, 3, 15, 4, 0, 30, tzinfo=UTC))  # 00:00:30 EDT Mar 15 (23:00:30 at a fixed -05:00)
    await tap.quotes([1])
    day, since, requests, _, _ = _qt(tap)
    assert (day, since, requests) == ("2027-03-15", clock.now().isoformat(), 1)


# === 9. the publisher uses only its own single thread ==================================================


def _obs() -> list[Any]:
    return [q(101, "10.00")]


async def test_the_publisher_never_uses_the_default_executor_and_needs_nothing_from_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-pool")
    loop.set_default_executor(default)
    submitted: list[tuple[Any, Any]] = []
    real = loop.run_in_executor

    def spy(executor: Any, fn: Any, *args: Any) -> Any:
        submitted.append((executor, fn))
        return real(executor, fn, *args)

    monkeypatch.setattr(loop, "run_in_executor", spy)
    threads: list[str] = []
    hang = threading.Event()

    pub = MarkPublisher(MarkPublisherDeps(NO_DB, FixedClock(AFTER_BAR), Tap(every=_obs()), lambda: 7))

    def write(observed: Any, run_id: int, now: datetime) -> tuple[int, int]:
        threads.append(threading.current_thread().name)
        if len(threads) > 1:
            hang.wait(10)  # the second pass: a hung database
        return (len(observed), 0)

    monkeypatch.setattr(pub, "_write", write)
    blocked = threading.Event()
    stuck: asyncio.Task[Any] | None = None
    try:
        # (a) the default executor saturated by a blocked task: the publisher's pass still runs at once
        saturating = loop.run_in_executor(None, blocked.wait, 10)
        step = await asyncio.wait_for(pub.run_once(), 1.0)
        assert step.skipped is None and step.marks_written == 1
        blocked.set()
        await saturating
        # (b) a pass stuck in the database: the token fetch (asyncio.to_thread, the default executor) and the
        # loop are untouched, even with a single default worker
        stuck = asyncio.create_task(pub.run_once())
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        assert await asyncio.wait_for(asyncio.to_thread(TOKEN.api_base.upper), 1.0) == BASE.upper()
        assert time.perf_counter() - t0 < 0.2
        ticks = 0
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1
        assert ticks == 10 and not stuck.done()
        assert len(pub._executor._threads) == 1
    finally:
        hang.set()
        blocked.set()
    assert stuck is not None
    await asyncio.wait_for(stuck, 2.0)
    pub.close()
    default.shutdown(wait=True)
    assert threads and all(t.startswith(pub_mod.THREAD_PREFIX) for t in threads)
    mine = [ex for ex, fn in submitted if getattr(fn, "__self__", None) is pub]
    assert mine and all(ex is pub._executor for ex in mine)  # never None (the default executor)


# === 10. a hung database (lock_timeout, statement_timeout) never blocks the loop; alerts stay below relay ==


def _live_world(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with factory() as s:
        live = add_run(s)
        a = add_symbol(s, "AAA", questrade_id=101)
        b = add_symbol(s, "BBB", questrade_id=102)
        _position(s, live, a)
        _position(s, live, b)
        s.commit()
    return live, a, b


async def _ticking(during: Coroutine[Any, Any, Any]) -> tuple[Any, float, float]:
    """Runs `during` while a coroutine ticks every 10 ms; (result, elapsed, the longest tick gap)."""
    stamps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        while not done.is_set():
            stamps.append(time.perf_counter())
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    t0 = time.perf_counter()
    try:
        out = await asyncio.wait_for(during, 10)
    finally:
        done.set()
        await t
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    return out, time.perf_counter() - t0, max(gaps, default=0.0)


@pytest.mark.db
async def test_a_hung_database_never_blocks_the_loop_and_its_events_never_reach_the_relay(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    live, _, _ = _live_world(db_factory)
    monkeypatch.setattr(pub_mod, "PUBLISH_STATEMENT_TIMEOUT_MS", 400)
    events = Events()
    tap = Tap(every=[q(101, "10.00"), q(102, "20.00")])
    pub = MarkPublisher(MarkPublisherDeps(db_factory, FixedClock(AFTER_BAR), tap, lambda: live, events))
    blocker = db_factory()
    try:
        # another session holds the mark tables (as a stuck migration or VACUUM FULL would): lock_timeout
        blocker.execute(text("LOCK TABLE trader.quote_marks, trader.mark_bars IN ACCESS EXCLUSIVE MODE"))
        for _ in range(3):
            step, took, gap = await _ticking(pub.run_once())
            assert step.skipped == "error" and 0.3 < took < 3.0
            assert gap < 0.1, f"the loop stalled {gap:.3f} s"
        assert pub.health_detail()["failing"] is True
    finally:
        blocker.rollback()
        blocker.close()
    step = await pub.run_once()
    assert step.skipped is None and step.marks_written == 2
    # statement_timeout: a statement that would run for 30 s is stopped by the server
    real_apply = pub._apply

    def slow(s: Session, observed: Any, run_id: int, now: datetime) -> tuple[int, int]:
        s.execute(text("SELECT pg_sleep(30)"))
        return real_apply(s, observed, run_id, now)

    monkeypatch.setattr(pub, "_apply", slow)
    step, took, gap = await _ticking(pub.run_once())
    pub.close()
    assert step.skipped == "error" and took < 3.0 and gap < 0.1
    levels = [row[0] for row in events.rows]
    assert levels == ["warning", "info", "warning"]  # one per streak, one recovery
    assert not set(levels) & set(ALERT_LEVELS)  # the relay only relays error/critical
    assert all(t.startswith(pub_mod.THREAD_PREFIX) for t in events.threads)


# === 11. stop cancels promptly while a pass is stuck; a stuck pass holds one thread only ===================


async def test_stop_cancels_a_publisher_stuck_in_its_thread_promptly(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    entered = threading.Event()
    pub = MarkPublisher(
        MarkPublisherDeps(NO_DB, FixedClock(AFTER_BAR), Tap(every=_obs()), lambda: 7), interval_s=0.01
    )

    def write(observed: Any, run_id: int, now: datetime) -> tuple[int, int]:
        entered.set()
        gate.wait(10)
        return (1, 0)

    monkeypatch.setattr(pub, "_write", write)
    stop = asyncio.Event()
    try:
        task = asyncio.create_task(pub.run(stop))
        await asyncio.to_thread(entered.wait, 2)
        t0 = time.perf_counter()
        stop.set()
        await Worker._finish(task, 0)  # exactly how the worker ends it
        assert time.perf_counter() - t0 < 0.1 and task.done()
        # a restarted run (supervisor) queues behind the stuck pass: still one marks thread
        again = asyncio.create_task(pub.run(asyncio.Event()))
        await asyncio.sleep(0.1)
        threads = set(pub._executor._threads)
        assert len(threads) == 1 and all(t.name.startswith(pub_mod.THREAD_PREFIX) for t in threads)
        again.cancel()
        await asyncio.gather(again, return_exceptions=True)
        t0 = time.perf_counter()
        pub.close()
        pub.close()
        assert time.perf_counter() - t0 < 0.1  # close never waits for the stuck thread
    finally:
        gate.set()
    for _ in range(100):  # the stuck thread ends once its statement ends, then exits (closed pool)
        if not any(t.is_alive() for t in threads):
            break
        await asyncio.sleep(0.02)
    assert not any(t.is_alive() for t in threads)


# === 12. the worker's step cadence is identical with the publisher off, on, and hung =======================


async def _worker_day(
    factory: sessionmaker[Session], make: Callable[[FixedClock, VirtualTime], MarkPublisher | None]
) -> tuple[Any, ...]:
    clock = FixedClock(et(TUE, 9, 34, 0))
    vt = VirtualTime(clock)
    h = Harness(factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    h.on_relay = lambda: stop.set() if len(h.relay_calls) >= 200 else None
    pub = make(clock, vt)
    try:
        await _run(Worker(dataclasses.replace(h.deps(), marks=pub)), stop, vt)
    finally:
        if pub is not None:
            pub.close()
    return (
        [(k, d, at) for k, d, at in h.fire_calls],
        [(e.session_date, e.polls, e.ticks) for e in h.engines],
        list(h.relay_calls),
    )


@pytest.mark.db
async def test_the_worker_step_cadence_is_identical_with_the_publisher_off_on_and_hung(
    db_factory: sessionmaker[Session],
) -> None:
    live, a, _ = _live_world(db_factory)
    gate = threading.Event()

    def on(clock: FixedClock, vt: VirtualTime) -> MarkPublisher:
        deps = MarkPublisherDeps(db_factory, clock, Tap(every=[q(101, "10.00")]), lambda: live, Events())
        return MarkPublisher(deps, sleep=vt.sleep)

    def hung(clock: FixedClock, vt: VirtualTime) -> MarkPublisher:
        pub = on(clock, vt)

        def stuck(observed: Any, run_id: int, now: datetime) -> tuple[int, int]:
            gate.wait(20)
            return (0, 0)

        pub._write = stuck  # type: ignore[method-assign]
        return pub

    off = await _worker_day(db_factory, lambda _c, _v: None)
    with_marks = await _worker_day(db_factory, on)
    try:
        with_hung = await _worker_day(db_factory, hung)
    finally:
        gate.set()
    fires, engines, relays = off
    assert [k for k, _, _ in fires] == ["orb_open"] and len(relays) == 200
    assert len(engines[0][1]) >= 200  # a real simulated stretch across the 9:35 scan
    assert with_marks == off
    assert with_hung == off
    with db_factory() as s:  # the publisher really ran during the "on" day
        assert s.execute(select(m.QuoteMark).where(m.QuoteMark.symbol_id == a)).scalar_one().run_id == live


# === 13. static: nothing on the decision path reads the mark tables; nothing turns them into candles =====

DECISION_DIRS = (
    "strategies",
    "engine",
    "broker",
    "market",
    "jobs",
    "reports",
    "replay",
    "decisions",
    "notify",
)
MARK_NAMES = {"QuoteMark", "MarkBar", "quote_marks", "mark_bars"}


def _refs(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.asname or node.name.rsplit(".", 1)[-1])
    return out


def _imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def _code_strings(tree: ast.Module) -> list[str]:
    docs = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]


def test_no_decision_path_module_reads_the_mark_tables_and_no_mark_becomes_a_candle() -> None:
    offenders: list[str] = []
    readers: list[Path] = []
    for path in sorted(TRADER.rglob("*.py")):
        rel = path.relative_to(TRADER)
        tree = ast.parse(path.read_text())
        refs = _refs(tree)
        strings = " ".join(_code_strings(tree))
        touches = bool(refs & MARK_NAMES) or any(n in strings for n in ("quote_marks", "mark_bars"))
        if rel.parts[0] in DECISION_DIRS:
            if touches or any(i.startswith("trader.marks") for i in _imports(tree)):
                offenders.append(f"{rel}: references the mark tables or trader.marks")
        if touches and rel.parts[:2] != ("db", "migrations") and rel.as_posix() != "db/models.py":
            readers.append(rel)
            if "Candle" in refs:
                offenders.append(f"{rel}: reads the mark tables and names Candle")
    assert offenders == []
    assert readers, "the scan found no reader at all (it would pass vacuously)"
    assert {r.parts[0] for r in readers} <= {"marks", "api", "worker.py", "runtime.py"}, readers


# === 14. the FK checks' KEY SHARE locks vs the nightly symbol upsert ====================================


@pytest.mark.db
@pytest.mark.xfail(
    strict=True,
    reason="DB-T2 gauntlet finding F2: a publisher insert holds KEY SHARE on one symbols row (FK check) "
    "while waiting on another that nightly's upsert_symbols holds FOR UPDATE (ON CONFLICT SET "
    "ticker/exchange, unique key columns); nightly then waits on the first and is the deadlock victim",
)
async def test_the_nightly_symbol_upsert_is_never_the_deadlock_victim_of_a_publisher_pass(
    db_factory: sessionmaker[Session],
) -> None:
    live, a, b = _live_world(db_factory)
    assert a < b  # the publisher inserts in symbol order: A's KEY SHARE first
    tap = Tap([q(101, "10.00"), q(102, "20.00")])
    pub = MarkPublisher(MarkPublisherDeps(db_factory, FixedClock(AFTER_BAR), tap, lambda: live, Events()))
    nightly = db_factory()
    upserted: list[str] = []

    def upsert(qid: int, ticker: str) -> None:
        repo.upsert_symbols(nightly, [QtSymbol(qid, ticker, "NASDAQ", "USD", f"{ticker} Inc", True, True)])
        upserted.append(ticker)

    error: BaseException | None = None
    try:
        await asyncio.to_thread(upsert, 102, "BBB")  # nightly's one transaction: B locked FOR UPDATE
        passing = asyncio.create_task(pub.run_once())  # KEY SHARE on A, then waits for B
        await asyncio.sleep(1.5)  # past the publisher's own deadlock check (deadlock_timeout = 1 s)
        try:
            await asyncio.wait_for(asyncio.to_thread(upsert, 101, "AAA"), 8)  # nightly reaches A
        except DBAPIError as exc:
            error = exc
        nightly.rollback()
        await asyncio.wait_for(passing, 8)
    finally:
        nightly.close()
        pub.close()
    assert error is None, f"nightly's transaction was aborted: {str(error).splitlines()[0]}"
    assert upserted == ["BBB", "AAA"]
