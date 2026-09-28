"""FIX-OPENBARS gauntlet (Mon 2026-09-28): breaker tests for the opening-bar batch deadline.

TokenBucket after cancellations, candles_many(deadline_s=...), MarketDataService.opening_bars with a
partial batch, and the ORB scan over a partial set. Fakes and respx only, never real Questrade.
"""

import asyncio
import dataclasses
import gc
import heapq
import itertools
import random
import threading
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient, TokenBucket
from trader.adapters.questrade.models import CandleRequest
from trader.db import models as m
from trader.market import data_service as ds_mod
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
START = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)
AFTER_BAR = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)
SECRET = "tok-SUPERSECRET-123"
EPS = 1e-9
BAR_JSON: dict[str, object] = {
    "start": OPEN.isoformat(),
    "end": (OPEN + timedelta(minutes=5)).isoformat(),
    "open": 21.0,
    "high": 21.5,
    "low": 20.9,
    "close": 21.4,
    "volume": 5000,
}


# --- a virtual monotonic clock for TokenBucket ---------------------------------------------------


class VClock:
    """Fake monotonic time. `sleep` parks the caller until `advance` moves time past its wake time."""

    def __init__(self) -> None:
        self.t = 0.0
        self._heap: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def mono(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self.t + max(d, 0.0), next(self._seq), fut))
        await fut

    async def settle(self) -> None:
        for _ in range(8):
            await asyncio.sleep(0)

    async def advance(self, to: float) -> None:
        await self.settle()
        while self._heap and self._heap[0][0] <= to:
            when, _, fut = heapq.heappop(self._heap)
            if fut.done():
                continue
            self.t = max(self.t, when)
            fut.set_result(None)
            await self.settle()
        self.t = max(self.t, to)
        await self.settle()


def grant_task(bucket: TokenBucket, vc: VClock, grants: list[float]) -> asyncio.Task[None]:
    async def go() -> None:
        await bucket.acquire()
        grants.append(vc.t)

    return asyncio.create_task(go())


def assert_spacing(grants: list[float], interval: float) -> None:
    g = sorted(grants)
    gaps = [b - a for a, b in itertools.pairwise(g)]
    assert all(x >= interval - EPS for x in gaps), f"min gap {min(gaps, default=None)} < {interval}"


async def cancel_all(tasks: Sequence[asyncio.Task[Any]]) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# 1
async def test_bucket_bursts_of_cancel_and_reacquire_never_beat_the_rate() -> None:
    vc = VClock()
    bucket = TokenBucket(20.0, monotonic=vc.mono, sleep=vc.sleep)
    interval = 0.05
    grants: list[float] = []
    live: list[asyncio.Task[None]] = []
    for round_ in range(12):
        batch = [grant_task(bucket, vc, grants) for _ in range(15)]
        live += batch
        await vc.advance(vc.t + interval * (round_ % 4) + 0.013)  # let a few through
        pending = [t for t in batch if not t.done()]
        await cancel_all(pending[: len(pending) - (round_ % 3)])  # sometimes leave stragglers
        # immediately re-acquire in the same tick as the cancellations
        live += [grant_task(bucket, vc, grants) for _ in range(round_ % 5)]
    await vc.advance(vc.t + 60)
    await cancel_all([t for t in live if not t.done()])
    assert len(grants) > 20
    assert_spacing(grants, interval)
    # and a fresh call after everything settles goes straight through (the queue was given back)
    t0 = vc.t
    await vc.advance(vc.t)
    last = grant_task(bucket, vc, grants)
    await vc.advance(vc.t)
    assert last.done() and grants[-1] == pytest.approx(t0)


# 2
async def test_bucket_pause_from_a_429_interleaved_with_cancellations_holds() -> None:
    vc = VClock()
    bucket = TokenBucket(20.0, monotonic=vc.mono, sleep=vc.sleep)
    grants: list[float] = []
    waiters = [grant_task(bucket, vc, grants) for _ in range(40)]
    await vc.advance(0.30)  # ~7 granted
    pause_from, pause_to = vc.t, vc.t + 2.0
    bucket.pause_until(pause_to)  # a 429 arrives
    await vc.advance(vc.t + 0.2)
    await cancel_all(waiters[20:])  # the batch deadline fires during the pause
    more = [grant_task(bucket, vc, grants) for _ in range(5)]
    await vc.advance(vc.t + 0.5)
    await cancel_all(waiters[:20])  # every batch waiter gone, only `more` left
    await vc.advance(vc.t + 30)
    await cancel_all([t for t in more if not t.done()])
    assert_spacing(grants, 0.05)
    assert not [g for g in grants if pause_from < g < pause_to - EPS], "a call went through during the pause"
    assert all(t.done() and not t.cancelled() for t in more)
    assert min(g for g in grants if g > pause_from) >= pause_to - EPS


# 3
async def test_bucket_many_concurrent_waiters_random_cancellation_order() -> None:
    rng = random.Random(20260928)
    vc = VClock()
    bucket = TokenBucket(20.0, monotonic=vc.mono, sleep=vc.sleep)
    grants: list[float] = []
    tasks = [grant_task(bucket, vc, grants) for _ in range(600)]
    for step in range(30):
        await vc.advance(vc.t + rng.uniform(0.0, 0.4))
        pending = [t for t in tasks if not t.done()]
        rng.shuffle(pending)
        await cancel_all(pending[: rng.randint(0, 25)])
        if step % 7 == 0:
            bucket.pause_until(vc.t + rng.uniform(0.0, 1.0))
        tasks += [grant_task(bucket, vc, grants) for _ in range(rng.randint(0, 6))]
    await vc.advance(vc.t + 120)
    assert all(t.done() for t in tasks)
    assert_spacing(grants, 0.05)


# 4
@pytest.mark.parametrize("new_first", [True, False])
async def test_bucket_last_waiter_reset_racing_a_new_acquire(new_first: bool) -> None:
    vc = VClock()
    bucket = TokenBucket(20.0, monotonic=vc.mono, sleep=vc.sleep)
    grants: list[float] = []
    first = grant_task(bucket, vc, grants)
    await vc.advance(0.0)  # granted at 0
    queued = [grant_task(bucket, vc, grants) for _ in range(5)]
    await vc.settle()  # slots 0.05 .. 0.25 reserved
    await vc.advance(0.01)
    if new_first:
        newcomer = grant_task(bucket, vc, grants)
        await cancel_all(queued)
    else:
        for t in queued:
            t.cancel()
        newcomer = grant_task(bucket, vc, grants)  # created in the same tick the cancellations land
        await asyncio.gather(*queued, return_exceptions=True)
    await vc.advance(5.0)
    assert first.done() and newcomer.done() and not newcomer.cancelled()
    assert grants[0] == 0.0
    assert grants[1] >= 0.05 - EPS  # never closer than one interval to the last granted slot
    assert_spacing(grants, 0.05)


# --- candles_many ----------------------------------------------------------------------------------


class FakeTokens:
    def __init__(self) -> None:
        self.n = 1
        self.forced = 0

    def access(self) -> AccessToken:
        return AccessToken(f"{SECRET}-{self.n}", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self) -> AccessToken:
        self.forced += 1
        self.n += 1
        return self.access()


class ScriptedClient(QuestradeClient):
    """`candles` replaced: per symbol id, a delay (None: hang forever) or an exception to raise."""

    def __init__(self, behaviour: dict[int, float | None | BaseException]) -> None:
        super().__init__(FakeTokens(), FixedClock(NOW), market_rps=1000.0)
        self.behaviour = behaviour
        self.started: list[int] = []
        self.cancelled: list[int] = []

    async def candles(self, symbol_id: int, start: datetime, end: datetime, interval: Any) -> list[Candle]:
        self.started.append(symbol_id)
        b = self.behaviour.get(symbol_id, 0.0)
        try:
            if isinstance(b, BaseException):
                raise b
            if b is None:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(b)
        except asyncio.CancelledError:
            self.cancelled.append(symbol_id)
            raise
        c = Candle(
            start,
            end,
            Decimal(symbol_id),
            Decimal(symbol_id),
            Decimal(symbol_id),
            Decimal(symbol_id),
            symbol_id,
            None,
        )
        return [c]


def req(i: int) -> CandleRequest:
    return CandleRequest(i, START, START + timedelta(minutes=5), "FiveMinutes")


def leftover_tasks() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


# 5
async def test_candles_many_degenerate_deadlines_and_early_completion() -> None:
    loop = asyncio.get_running_loop()
    async with ScriptedClient({}) as c:
        assert await c.candles_many([], deadline_s=5.0) == {}
        assert await c.candles_many([]) == {}
        for d in (0.0, -1.0):
            got = await asyncio.wait_for(c.candles_many([req(1), req(2)], deadline_s=d), 2)
            assert set(got) <= {req(1), req(2)}  # nothing waited for, nothing raised
            assert leftover_tasks() == []
        # all complete early: returns as soon as they finish, not at the deadline
        t0 = loop.time()
        got = await c.candles_many([req(i) for i in range(1, 30)], deadline_s=30.0)
        assert loop.time() - t0 < 1.0
        assert set(got) == {req(i) for i in range(1, 30)}
        assert all(got[req(i)][0].volume == i for i in range(1, 30))  # type: ignore[index,union-attr]
    # all hang: returns at the deadline with nothing, every child cancelled
    async with ScriptedClient({i: None for i in range(1, 6)}) as c:
        t0 = loop.time()
        got = await asyncio.wait_for(c.candles_many([req(i) for i in range(1, 6)], deadline_s=0.2), 3)
        assert got == {}
        assert 0.15 <= loop.time() - t0 < 1.0
        assert sorted(c.cancelled) == [1, 2, 3, 4, 5]
        assert leftover_tasks() == []


# 6
async def test_candles_many_duplicates_are_fetched_once_and_keyed_right() -> None:
    async with ScriptedClient({2: 0.02, 3: QuestradeApiError(404, "nope")}) as c:
        reqs = [req(1), req(2), req(1), req(3), req(2), req(1)]
        got = await c.candles_many(reqs, deadline_s=5.0)
        assert sorted(c.started) == [1, 2, 3]  # each unique request once
        assert set(got) == {req(1), req(2), req(3)}
        assert got[req(1)][0].volume == 1  # type: ignore[index,union-attr]
        assert got[req(2)][0].volume == 2  # type: ignore[index,union-attr]
        err = got[req(3)]
        assert isinstance(err, QuestradeApiError) and err.status == 404


# 7
@pytest.mark.xfail(
    strict=True,
    reason="FIX-OPENBARS gauntlet should-fix: candles_many re-raises a non-API exception from one "
    "request via t.result() and drops every completed result (client.py:404). Remove this mark with the fix.",
)
async def test_candles_many_one_non_api_exception_does_not_lose_the_completed_bars() -> None:
    """A request that raises something that is not a QuestradeApiError (httpx.DecodingError, a
    QuestradeAuthError from a failed forced refresh, a bug) must not throw away every bar that
    already arrived: that is the exact production symptom this fix is about."""
    async with ScriptedClient({7: httpx.DecodingError("bad gzip"), 9: None}) as c:
        reqs = [req(i) for i in range(1, 10)]
        try:
            got = await asyncio.wait_for(c.candles_many(reqs, deadline_s=0.3), 3)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"candles_many raised {type(exc).__name__} and dropped 7 completed bars")
        assert leftover_tasks() == []
        assert {req(i) for i in range(1, 7)} | {req(8)} <= set(got)
        assert req(9) not in got


# 8
async def test_cancelling_the_caller_cancels_every_child_and_leaks_nothing() -> None:
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    old = loop.get_exception_handler()
    loop.set_exception_handler(lambda _l, ctx: errors.append(ctx))
    try:
        async with ScriptedClient({i: (0.0 if i <= 3 else None) for i in range(1, 21)}) as c:
            caller = asyncio.create_task(c.candles_many([req(i) for i in range(1, 21)], deadline_s=30.0))
            await asyncio.sleep(0.05)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert sorted(c.cancelled) == list(range(4, 21))
            assert leftover_tasks() == []
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(old)
    assert not [e for e in errors if "destroyed but it is pending" in str(e.get("message", ""))]
    assert errors == []


# 9
@respx.mock
async def test_a_401_refresh_in_flight_at_the_deadline_does_not_corrupt_the_cached_token() -> None:
    release = threading.Event()
    entered = threading.Event()

    class SlowRefreshTokens(FakeTokens):
        def force_refresh(self) -> AccessToken:
            entered.set()
            release.wait(5)
            return super().force_refresh()

    tokens = SlowRefreshTokens()

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["Authorization"]
        if auth == f"Bearer {SECRET}-1":
            return httpx.Response(401, json={"code": 1017})
        return httpx.Response(200, json={"candles": []})

    respx.get(url__regex=BASE + r"markets/candles/\d+").mock(side_effect=handler)
    async with QuestradeClient(tokens, FixedClock(NOW), market_rps=1000.0) as c:
        got = await asyncio.wait_for(c.candles_many([req(1), req(2)], deadline_s=0.2), 3)
        assert got == {}  # both were stuck behind the refresh
        assert entered.is_set()
        release.set()
        await asyncio.sleep(0.1)  # the orphaned refresh thread finishes
        cached = c._token
        assert cached is None or cached.token.startswith(SECRET)
        # the client still works afterwards and the token lock is free
        after = await asyncio.wait_for(c.candles(1, START, START + timedelta(minutes=5), "FiveMinutes"), 3)
        assert after == []
        assert c._token is not None and c._token.token != f"{SECRET}-1"
        assert not c._token_lock.locked()


# --- opening_bars ------------------------------------------------------------------------------------


@pytest.fixture
def ids(db_factory: sessionmaker[Session]) -> dict[str, int]:
    """AAA/BBB/CCC have Questrade ids 101-103; DDD has none."""
    out: dict[str, int] = {}
    with db_factory() as s:
        for i, t in enumerate(["AAA", "BBB", "CCC"]):
            out[t] = add_symbol(s, t, questrade_id=101 + i)
        out["DDD"] = add_symbol(s, "DDD")
        for sid in out.values():
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=sid,
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1.0000"),
                    source="finviz",
                )
            )
            s.add(
                m.OpenBarStat(
                    symbol_id=sid,
                    session_date=DAY,
                    avg_open_vol_14d=Decimal("1000.00"),
                    atr14=Decimal("1.0000"),
                )
            )
        s.commit()
    return out


def bar(volume: int, start: datetime = OPEN) -> Candle:
    return Candle(
        start,
        start + timedelta(minutes=5),
        Decimal("21.00"),
        Decimal("21.50"),
        Decimal("20.90"),
        Decimal("21.40"),
        volume,
        None,
    )


@dataclasses.dataclass
class LeakyStats:
    requests: int = 7
    http_429: int = 1
    pause_s: float = 2.5
    note: str = f"Bearer {SECRET} at {BASE}markets/candles/101"


class PartialQuestrade(FakeQuestrade):
    """Honours the deadline by returning only `answer`; can also ignore it and sleep first."""

    def __init__(self, answer: dict[int, list[Candle] | QuestradeApiError], sleep_s: float = 0.0) -> None:
        super().__init__()
        self.answer = answer
        self.sleep_s = sleep_s
        self.deadlines: list[float | None] = []
        self.stats = {"market": LeakyStats(), "account": LeakyStats()}

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.calls.append(("candles_many", len(reqs)))
        self.deadlines.append(deadline_s)
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        return {r: self.answer[r.symbol_id] for r in reqs if r.symbol_id in self.answer}


def svc(
    factory: sessionmaker[Session], qt: Any, *, now: datetime = AFTER_BAR, deadline: float = 0.3
) -> MarketDataService:
    return MarketDataService(factory, FixedClock(now), CAL, qt, fetch_deadline_s=deadline)


def cached_rows(factory: sessionmaker[Session]) -> list[tuple[int, datetime]]:
    with factory() as s:
        return sorted(
            (r.symbol_id, r.ts)
            for r in s.execute(select(m.IntradayCandle).where(m.IntradayCandle.ts == OPEN)).scalars()
        )


# 10
@pytest.mark.db
async def test_opening_bars_backstop_for_a_client_ignoring_the_deadline(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    loop = asyncio.get_running_loop()
    # ignores the deadline but finishes inside the guard: its answer is kept
    late = PartialQuestrade({101: [bar(5000)], 102: [bar(3000)], 103: [bar(4000)]}, sleep_s=0.4)
    got = await svc(db_factory, late, deadline=0.3).opening_bars(DAY)
    assert late.deadlines == [0.3]
    assert set(got.bars) == {ids["AAA"], ids["BBB"], ids["CCC"]}
    # ignores the deadline and hangs: stopped by the guard (deadline + min(5, deadline))
    with db_factory() as s:
        s.query(m.IntradayCandle).delete()
        s.commit()
    hang = PartialQuestrade({101: [bar(5000)]}, sleep_s=3600)
    t0 = loop.time()
    got = await asyncio.wait_for(svc(db_factory, hang, deadline=0.3).opening_bars(DAY), 5)
    assert 0.55 <= loop.time() - t0 < 1.5
    assert got.bars == {}
    assert got.missing == {ids[t]: "timeout" for t in ("AAA", "BBB", "CCC")} | {ids["DDD"]: "no_questrade_id"}


# 11
@pytest.mark.db
async def test_opening_bars_classify_and_cache_each_completed_bar_exactly_once(
    db_factory: sessionmaker[Session], ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    upserts: list[int] = []
    real = ds_mod.repo.upsert_intraday_candles

    def counting(s: Session, sid: int, code: Any, rows: Sequence[Candle]) -> Any:
        upserts.append(sid)
        return real(s, sid, code, rows)

    monkeypatch.setattr(ds_mod.repo, "upsert_intraday_candles", counting)
    forming_now = OPEN + timedelta(minutes=3)  # the 9:30 bar is still forming
    qt = PartialQuestrade({101: [bar(5000)], 102: QuestradeApiError(500, "boom")})  # 103 outstanding
    with capture_logs() as logs:
        got = await svc(db_factory, qt, now=forming_now).opening_bars(DAY)
    assert got.bars == {}
    assert got.missing == {
        ids["AAA"]: "bar_not_complete",
        ids["BBB"]: "questrade_error: HTTP 500",
        ids["CCC"]: "timeout",
        ids["DDD"]: "no_questrade_id",
    }
    assert upserts == [] and cached_rows(db_factory) == []
    text = str(logs)
    assert SECRET not in text and BASE not in text and "Bearer" not in text
    (warn,) = [e for e in logs if e["event"] == "market.opening_bars_timeout"]
    assert (warn["completed"], warn["outstanding"]) == (2, 1)

    # after the bar: AAA fetched, CCC still outstanding -> AAA cached once
    qt2 = PartialQuestrade({101: [bar(5000)], 102: [bar(3000)]})
    got = await svc(db_factory, qt2).opening_bars(DAY)
    assert set(got.bars) == {ids["AAA"], ids["BBB"]}
    assert got.missing == {ids["CCC"]: "timeout", ids["DDD"]: "no_questrade_id"}
    # next call only asks for CCC, caches CCC once, serves AAA/BBB from the cache
    qt3 = PartialQuestrade({101: [bar(1)], 102: [bar(1)], 103: [bar(4000)]})
    got = await svc(db_factory, qt3).opening_bars(DAY)
    assert qt3.calls == [("candles_many", 1)]
    assert got.bars[ids["AAA"]].volume == 5000 and got.bars[ids["CCC"]].volume == 4000
    assert sorted(upserts) == sorted([ids["AAA"], ids["BBB"], ids["CCC"]])
    assert [sid for sid, _ in cached_rows(db_factory)] == sorted([ids["AAA"], ids["BBB"], ids["CCC"]])


# 12
@pytest.mark.db
@respx.mock
async def test_opening_bars_real_client_stats_log_has_no_token_or_url(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "junk"}, text=f"slow down {SECRET}")
        return httpx.Response(200, json={"candles": [BAR_JSON]})

    respx.get(url__regex=BASE + r"markets/candles/\d+").mock(side_effect=handler)
    async with QuestradeClient(FakeTokens(), FixedClock(AFTER_BAR), market_rps=1000.0) as client:
        with capture_logs() as logs:
            got = await svc(db_factory, client, deadline=5.0).opening_bars(DAY)
    assert set(got.bars) == {ids["AAA"], ids["BBB"], ids["CCC"]}
    (info,) = [e for e in logs if e["event"].startswith("market.opening_bars")]
    assert info["client_stats"]["http_429"] == 1 and info["client_stats"]["requests"] == 4
    assert info["client_stats"]["pause_s"] > 0
    text = str(logs)
    assert SECRET not in text and "api05" not in text and "Bearer" not in text


# 13
@pytest.mark.db
@respx.mock
async def test_orb_ranks_the_present_set_like_a_full_batch_and_records_the_missing(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    from tests.strategies.fakes import FakeCatalyst, FakeCatalysts, make_ctx
    from trader.strategies.orb_sip import ORB_EVENT, OrbSip

    slow: set[int] = {103}
    volumes = {101: 5000, 102: 3000, 103: 9000}

    async def handler(request: httpx.Request) -> httpx.Response:
        qid = int(request.url.path.rsplit("/", 1)[1])
        if qid in slow:
            await asyncio.Event().wait()
        return httpx.Response(200, json={"candles": [BAR_JSON | {"volume": volumes[qid]}]})

    respx.get(url__regex=BASE + r"markets/candles/\d+").mock(side_effect=handler)
    cats = FakeCatalysts({ids[t]: FakeCatalyst() for t in ("AAA", "BBB", "CCC")})

    async def scan() -> tuple[list[tuple[int, int | None]], Any, list[Any]]:
        async with QuestradeClient(FakeTokens(), FixedClock(AFTER_BAR), market_rps=1000.0) as client:
            service = svc(db_factory, client, deadline=0.3)
            strategy = OrbSip()
            ctx = make_ctx(service, strategy.params, cats, now=AFTER_BAR, session=DAY)  # type: ignore[arg-type]
            orb = next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)
            intents = await asyncio.wait_for(strategy.on_event(ctx, orb), timeout=5)
        return [(c.symbol_id, c.rank) for c in ctx.candidates], ctx, intents

    partial, pctx, pint = await scan()
    slow.clear()
    full, _, fint = await scan()
    assert [sid for sid, _ in full] == [ids["CCC"], ids["AAA"], ids["BBB"]]
    # the partial ranking is the full ranking with the missing symbol removed, ranks renumbered
    assert partial == [(sid, i) for i, sid in enumerate([s for s, _ in full if s != ids["CCC"]], start=1)]
    assert pint and pint[0].symbol_id == ids["AAA"]
    assert fint and fint[0].symbol_id == ids["CCC"]
    note = next(n for n in pctx.notes if "no opening bar" in n.message)
    assert note.data["missing"] == {str(ids["CCC"]): "timeout", str(ids["DDD"]): "no_questrade_id"}
