"""DB-T2 acceptance tests 1-5 and 13-15: the `QuoteTap` is transparent (live dashboard plan S1a, S10).

The tap sits in front of every worker market-data call during the soak, so these tests pin that it returns
the very objects the wrapped client returned, re-raises the very exceptions (cancellation included), forwards
`candles_many`'s `deadline_s` and `reqs` untouched, never suspends where the wrapped client does not, keeps
bounded memory, and that its source has the S1a shape (one await per method, no lock, task, timeout or I/O).
"""

import ast
import asyncio
import dataclasses
import gc
import json
import math
import weakref
from collections.abc import Awaitable, Callable, Generator, Iterator, MutableMapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.questrade.client import CallStats, QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle, Interval
from trader.marks import tap as tap_mod
from trader.marks.tap import QuoteTap
from trader.marks.types import MAX_CANDLE_BATCHES, MAX_OBSERVATIONS_PER_SYMBOL, MAX_TAP_SYMBOLS

ET = ZoneInfo("America/New_York")
CAL = SessionCalendar()
TUE = date(2026, 10, 6)  # EDT
EST_DAY = date(2026, 12, 1)  # EST
TRADER = Path(__file__).resolve().parents[2] / "trader"
T0 = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)


def et(d: date, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, mi, s, tzinfo=ET).astimezone(UTC)


def quote(qid: int, last: str = "10.00") -> QtQuote:
    return QtQuote(
        symbol_id=qid,
        symbol=f"Q{qid}",
        bid=Decimal(last) - Decimal("0.01"),
        ask=Decimal(last) + Decimal("0.01"),
        last=Decimal(last),
        last_regular=Decimal(last),
        volume=1000,
        last_trade_time=T0,
        delay=0,
        is_halted=False,
        vwap=None,
    )


def req(qid: int) -> CandleRequest:
    return CandleRequest(qid, T0, T0 + timedelta(minutes=5), "FiveMinutes")


def candle() -> Candle:
    return Candle(T0, T0 + timedelta(minutes=5), Decimal(1), Decimal(1), Decimal(1), Decimal(1), 10, None)


class Result(dict[CandleRequest, Any]):
    """A dict that can be weakly referenced (plain dicts cannot)."""


class Inner:
    """A recording QuoteClient. `exc` is raised by every method; results are returned as given (same
    objects)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.exc: BaseException | None = None
        self.quotes_result: list[QtQuote] = []
        self.candles_result: list[Candle] = [candle()]
        self.many_result: Result = Result()
        self.stats: Any = None
        self.stats_after: Any = None  # set as `stats` inside candles_many (a client opening in the call)
        self.touched_at_call: int | None = None

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        self.calls.append(("quotes", ids))
        if self.exc is not None:
            raise self.exc
        return self.quotes_result

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        self.calls.append(("candles", symbol_id, start, end, interval))
        if self.exc is not None:
            raise self.exc
        return self.candles_result

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.calls.append(("candles_many", reqs, deadline_s))
        if isinstance(reqs, Recording):
            self.touched_at_call = len(reqs.touched)
        if self.stats_after is not None:
            self.stats = self.stats_after
        if self.exc is not None:
            raise self.exc
        return self.many_result


class Recording(Sequence[CandleRequest]):
    """A request sequence that records every access (the tap must not touch it before the inner call)."""

    def __init__(self, items: list[CandleRequest]) -> None:
        self._items = items
        self.touched: list[str] = []

    def __len__(self) -> int:
        self.touched.append("len")
        return len(self._items)

    def __getitem__(self, i: Any) -> Any:
        self.touched.append("getitem")
        return self._items[i]

    def __iter__(self) -> Iterator[CandleRequest]:
        self.touched.append("iter")
        return iter(self._items)


class BadClock:
    def now(self) -> datetime:
        raise RuntimeError("clock broke")


def _tail_code(tb: TracebackType | None) -> Any:
    assert tb is not None
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_frame.f_code


def _warnings(logs: Sequence[MutableMapping[str, Any]]) -> list[MutableMapping[str, Any]]:
    return [e for e in logs if e.get("log_level") == "warning"]


# --- 1. quotes: same objects --------------------------------------------------------------------------------


async def test_quotes_return_the_identical_list_and_record_the_identical_quotes() -> None:
    inner = Inner()
    inner.quotes_result = [quote(101), quote(102, "20.00")]
    clock = FixedClock(T0)
    tap = QuoteTap(inner, clock)
    ids = [101, 102]
    got = await tap.quotes(ids)
    assert got is inner.quotes_result
    assert len(inner.calls) == 1 and inner.calls[0][1] is ids
    drained = tap.drain()
    assert [o.quote for o in drained] == got
    assert all(o.quote is q for o, q in zip(drained, got, strict=True))
    assert [(o.qt_id, o.observed_at) for o in drained] == [(101, T0), (102, T0)]
    assert tap.drain() == []  # drained: the tap starts empty again


async def test_drain_returns_observations_in_observation_order_across_symbols() -> None:
    inner = Inner()
    clock = FixedClock(T0)
    tap = QuoteTap(inner, clock)
    seen: list[QtQuote] = []
    for qids in ([1, 2], [3], [1], [2, 3]):
        inner.quotes_result = [quote(q) for q in qids]
        seen.extend(await tap.quotes(qids))
        clock.advance(timedelta(seconds=2))
    drained = tap.drain()
    assert len(drained) == len(seen) and all(o.quote is q for o, q in zip(drained, seen, strict=True))


# --- 2. errors: same instances ------------------------------------------------------------------------------


def _errors() -> list[BaseException]:
    return [
        QuestradeApiError(429, "slow down"),
        RuntimeError("boom"),
        asyncio.CancelledError(),
        KeyboardInterrupt(),
    ]


@pytest.mark.parametrize("method", ["quotes", "candles", "candles_many"])
@pytest.mark.parametrize("kind", range(4))
async def test_inner_exceptions_propagate_as_the_same_instance(method: str, kind: int) -> None:
    inner = Inner()
    inner.exc = err = _errors()[kind]
    tap = QuoteTap(inner, FixedClock(T0))
    calls: dict[str, Callable[[], Awaitable[Any]]] = {
        "quotes": lambda: tap.quotes([101]),
        "candles": lambda: tap.candles(101, T0, T0, "OneMinute"),
        "candles_many": lambda: tap.candles_many([req(101)], deadline_s=45.0),
    }
    with pytest.raises(BaseException) as info:  # noqa: PT011 (identity is checked below)
        await calls[method]()
    assert info.value is err
    assert _tail_code(err.__traceback__) is getattr(Inner, method).__code__
    assert err.__context__ is None and err.__cause__ is None
    assert tap.drain() == []  # nothing recorded for quotes
    if method == "candles_many":
        (batch,) = tap.health_detail()["candle_batches"]
        assert batch["raised"] == type(err).__name__


async def test_a_recording_failure_is_swallowed_logged_once_per_streak_and_the_result_is_identical() -> None:
    inner = Inner()
    inner.quotes_result = [quote(101)]
    tap = QuoteTap(inner, BadClock())
    with capture_logs() as logs:
        first = await tap.quotes([101])
        second = await tap.quotes([101])
    assert first is inner.quotes_result and second is inner.quotes_result
    warnings = _warnings(logs)
    assert len(warnings) == 1, logs
    assert not [e for e in logs if e.get("log_level") in ("error", "critical")]


async def test_a_failing_deque_is_swallowed_and_a_new_streak_logs_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = Inner()
    inner.quotes_result = [quote(101)]
    tap = QuoteTap(inner, FixedClock(T0))

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise MemoryError("no deque")  # an Exception subclass the bookkeeping must absorb

    with capture_logs() as logs:
        monkeypatch.setattr(tap_mod, "deque", broken)
        assert await tap.quotes([101]) is inner.quotes_result
        assert await tap.quotes([101]) is inner.quotes_result
        monkeypatch.undo()
        assert await tap.quotes([101]) is inner.quotes_result  # the streak ends
        monkeypatch.setattr(tap_mod, "deque", broken)
        inner.quotes_result = [quote(202)]  # a new symbol needs a new deque
        assert await tap.quotes([202]) is inner.quotes_result
    assert len(_warnings(logs)) == 2, logs


async def test_a_recording_failure_while_the_inner_call_raises_leaves_the_inner_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = Inner()
    inner.exc = err = RuntimeError("inner failed")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("bookkeeping failed")

    monkeypatch.setattr(tap_mod, "CandleBatch", broken)
    tap = QuoteTap(inner, BadClock())
    with capture_logs():
        with pytest.raises(RuntimeError) as info:
            await tap.candles_many([req(1)], deadline_s=1.0)
    assert info.value is err
    assert err.__context__ is None and err.__cause__ is None


# --- 3. candles_many untouched ------------------------------------------------------------------------------


@pytest.mark.parametrize("deadline", [45.0, None, "omitted"])
async def test_candles_many_forwards_the_same_reqs_and_deadline_and_returns_the_same_dict(
    deadline: Any,
) -> None:
    inner = Inner()
    inner.many_result = Result({req(1): [candle()]})
    tap = QuoteTap(inner, FixedClock(T0))
    reqs = Recording([req(1)])
    if deadline == "omitted":
        got = await tap.candles_many(reqs)
    else:
        got = await tap.candles_many(reqs, deadline_s=deadline)
    assert got is inner.many_result
    (call,) = inner.calls
    assert call[1] is reqs
    assert call[2] == (None if deadline == "omitted" else deadline)
    assert inner.touched_at_call == 0  # not iterated, measured or indexed before the inner call


async def test_candles_many_counts_the_batch_and_the_market_deltas() -> None:
    inner = Inner()
    r1, r2, r3, r4 = req(1), req(2), req(3), req(4)
    inner.many_result = Result({r1: [candle()], r2: QuestradeApiError(500, "x")})
    inner.stats_after = {"market": CallStats(requests=5, http_429=2, pause_s=1.5), "account": CallStats()}
    clock = FixedClock(et(TUE, 9, 35, 5))
    tap = QuoteTap(inner, clock)
    await tap.candles_many([r1, r2, r2, r3, r4], deadline_s=45.0)
    (batch,) = tap.health_detail()["candle_batches"]
    assert batch["symbols"] == 4  # duplicates once, as the client dedupes them
    assert (batch["completed"], batch["errors"], batch["outstanding"]) == (1, 1, 2)
    assert (batch["http_429"], batch["pause_s"]) == (2, 1.5)  # stats None before the call: from zero
    assert batch["deadline_s"] == 45.0 and batch["raised"] is None
    assert batch["started_at"] == et(TUE, 9, 35, 5).isoformat()
    assert batch["elapsed_s"] >= 0


async def test_the_tap_keeps_no_reference_to_the_result_or_the_requests() -> None:
    inner = Inner()
    inner.many_result = Result({req(1): [candle()]})
    tap = QuoteTap(inner, FixedClock(T0))
    reqs = Recording([req(1)])
    got = await tap.candles_many(reqs, deadline_s=1.0)
    result_ref = weakref.ref(got)
    reqs_ref = weakref.ref(reqs)
    del got, reqs
    inner.many_result = Result()
    inner.calls.clear()
    gc.collect()
    assert result_ref() is None and reqs_ref() is None
    assert len(tap.health_detail()["candle_batches"]) == 1


async def test_candles_is_a_pure_pass_through() -> None:
    inner = Inner()
    tap = QuoteTap(inner, FixedClock(T0))
    start, end = T0, T0 + timedelta(hours=1)
    got = await tap.candles(7, start, end, "OneMinute")
    assert got is inner.candles_result
    assert inner.calls == [("candles", 7, start, end, "OneMinute")]
    assert inner.calls[0][2] is start and inner.calls[0][3] is end
    assert tap.drain() == [] and tap.health_detail().get("candle_batches", []) == []


# --- 4. stats are live --------------------------------------------------------------------------------------


class LazyLike:
    """Like `LazyQuestrade`: `stats` is None until the client opens, then a fresh dict on every access."""

    def __init__(self) -> None:
        self.opened = False
        self._stats = {"market": CallStats(), "account": CallStats()}

    @property
    def stats(self) -> dict[str, Any] | None:
        return dict(self._stats) if self.opened else None


def test_stats_read_the_inner_stats_on_every_access() -> None:
    inner = Inner()
    tap = QuoteTap(inner, FixedClock(T0))
    assert tap.stats is None
    inner.stats = first = {"market": CallStats(requests=1)}
    assert tap.stats is first
    inner.stats = second = {"market": CallStats(requests=2)}
    assert tap.stats is second
    lazy = LazyLike()
    tap2 = QuoteTap(lazy, FixedClock(T0))
    assert tap2.stats is None
    lazy.opened = True
    assert tap2.stats == lazy.stats and tap2.stats is not tap2.stats  # a fresh dict each time, as the inner's
    lazy._stats["market"].requests = 9
    assert tap2.stats["market"].requests == 9
    assert not hasattr(tap2, "rate_limit_remaining")  # the tap forwards nothing else


# --- 5. bounds and health -----------------------------------------------------------------------------------


async def test_one_symbol_keeps_the_newest_30_observations() -> None:
    inner = Inner()
    tap = QuoteTap(inner, FixedClock(T0))
    returned: list[QtQuote] = []
    for i in range(50):
        inner.quotes_result = [quote(101, f"{10 + i}.00")]
        returned.extend(await tap.quotes([101]))
    drained = tap.drain()
    assert len(drained) == MAX_OBSERVATIONS_PER_SYMBOL == 30
    assert all(o.quote is q for o, q in zip(drained, returned[-30:], strict=True))


async def test_1200_symbols_keep_the_1000_most_recently_observed() -> None:
    inner = Inner()
    tap = QuoteTap(inner, FixedClock(T0))
    for start in range(0, 1200, 100):
        inner.quotes_result = [quote(q) for q in range(start, start + 100)]
        await tap.quotes(list(range(start, start + 100)))
    inner.quotes_result = [quote(5)]  # an old symbol observed again moves to the most recent end
    await tap.quotes([5])
    drained = tap.drain()
    assert len(drained) == MAX_TAP_SYMBOLS == 1000
    assert {o.qt_id for o in drained} == set(range(201, 1200)) | {5}


def _market(detail: dict[str, Any]) -> dict[str, Any]:
    return dict(detail["questrade"]["market"])


class Opening(LazyLike):
    """A `LazyQuestrade` whose client opens inside the 9:35 batch itself (the S10 baseline case)."""

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.opened = True
        self._stats["market"].requests += 3
        self._stats["market"].http_429 += 1
        self._stats["market"].pause_s += 2.0
        return Result({r: [candle()] for r in reqs})


async def test_health_counts_a_batch_that_opened_the_client_from_zero() -> None:
    clock = FixedClock(et(TUE, 9, 35, 5))
    tap = QuoteTap(Opening(), clock)  # type: ignore[arg-type]
    assert "questrade" not in tap.health_detail()  # the client is not open yet
    await tap.candles_many([req(1), req(2), req(3)], deadline_s=45.0)
    detail = tap.health_detail()
    assert _market(detail) == {
        "requests": 3,
        "http_429": 1,
        "pause_s": 2.0,
        "http_5xx": 0,
        "transport_errors": 0,
    }
    assert detail["questrade"]["day"] == "2026-10-06"
    (batch,) = detail["candle_batches"]
    assert (batch["http_429"], batch["pause_s"], batch["completed"]) == (1, 2.0, 3)
    json.dumps(detail, allow_nan=False)


@pytest.mark.parametrize("day", [TUE, EST_DAY])
async def test_the_baseline_resets_at_the_et_date_change_before_the_first_call(day: date) -> None:
    inner = Inner()
    stats = {"market": CallStats(requests=10, http_429=1), "account": CallStats(requests=4)}
    inner.stats = stats
    clock = FixedClock(et(day, 23, 59, 50))
    tap = QuoteTap(inner, clock)
    inner.many_result = Result({req(1): [candle()]})
    await tap.candles_many([req(1)], deadline_s=45.0)  # yesterday's batch
    assert _market(tap.health_detail())["requests"] == 10  # zero baseline: every count is this process's
    clock.advance(timedelta(seconds=20))  # 00:00:10 ET of the next day
    next_day = day + timedelta(days=1)

    async def counting(reqs: Sequence[CandleRequest], *, deadline_s: float | None = None) -> Any:
        stats["market"].requests += 2
        stats["market"].http_429 += 1
        return Result({r: [candle()] for r in reqs})

    inner.candles_many = counting  # type: ignore[method-assign]
    await tap.candles_many([req(2)], deadline_s=45.0)
    detail = tap.health_detail()
    assert detail["questrade"]["day"] == next_day.isoformat()
    assert detail["questrade"]["since"] == et(next_day, 0, 0, 10).isoformat()
    assert _market(detail)["requests"] == 2 and _market(detail)["http_429"] == 1  # taken before the call
    assert detail["questrade"]["account"]["requests"] == 0
    assert [b["started_at"] for b in detail["candle_batches"]] == [et(next_day, 0, 0, 10).isoformat()]
    json.dumps(detail, allow_nan=False)


async def test_a_health_read_resets_the_baseline_on_a_new_day_and_the_batches_are_capped() -> None:
    inner = Inner()
    inner.stats = {"market": CallStats(requests=3), "account": CallStats()}
    clock = FixedClock(et(TUE, 10, 0))
    tap = QuoteTap(inner, clock)
    for _ in range(MAX_CANDLE_BATCHES + 3):
        await tap.candles_many([req(1)], deadline_s=1.0)
    assert len(tap.health_detail()["candle_batches"]) == MAX_CANDLE_BATCHES
    clock.advance(timedelta(days=1))
    detail = tap.health_detail()
    assert _market(detail)["requests"] == 0 and detail["candle_batches"] == []
    assert detail["questrade"]["since"] == et(TUE + timedelta(days=1), 10, 0).isoformat()


async def test_health_is_json_safe_and_never_raises() -> None:
    inner = Inner()
    inner.stats = {"market": CallStats(pause_s=math.nan, requests=1), "account": CallStats(pause_s=math.inf)}
    tap = QuoteTap(inner, FixedClock(T0))
    await tap.candles_many([req(1)], deadline_s=math.inf)
    json.dumps(tap.health_detail(), allow_nan=False)
    inner.stats = {"market": "garbage"}
    json.dumps(tap.health_detail(), allow_nan=False)
    broken = QuoteTap(inner, BadClock())
    with capture_logs() as logs:
        assert broken.health_detail() == {}
        assert broken.health_detail() == {}
    assert len(_warnings(logs)) == 1


# --- 4 and 15. through a real MarketDataService (db) --------------------------------------------------------

OPEN = CAL.session_open(TUE)
AFTER_BAR = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)


def c5(start: datetime) -> Candle:
    return Candle(
        start,
        start + timedelta(minutes=5),
        Decimal("21"),
        Decimal("21.5"),
        Decimal("20.9"),
        Decimal("21.4"),
        5000,
        None,
    )


class StatsQuestrade(FakeQuestrade):
    """The in-memory client with per-category counters, as the real client keeps them."""

    def __init__(self) -> None:
        super().__init__()
        self.stats = {"market": CallStats(), "account": CallStats()}
        self.deadlines: list[float | None] = []

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.deadlines.append(deadline_s)
        self.stats["market"].requests += len(reqs)
        self.stats["market"].http_429 += 1
        self.stats["market"].pause_s += 0.25
        return await super().candles_many(reqs, deadline_s=deadline_s)


class HalfInTime(StatsQuestrade):
    """Honours `deadline_s`: half the requests complete, the rest are still outstanding at the deadline."""

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        out = await super().candles_many(reqs[: len(reqs) // 2], deadline_s=deadline_s)
        self.deadlines[-1] = deadline_s
        await asyncio.sleep(deadline_s or 0)
        return out


class IgnoresDeadline(StatsQuestrade):
    """Ignores `deadline_s` and sleeps: only `_fetch_opening_bars`'s own guard stops it. Records the loop time
    it was entered and cancelled at."""

    entered: float = 0.0
    cancelled: float = 0.0

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        loop = asyncio.get_running_loop()
        self.deadlines.append(deadline_s)
        self.entered = loop.time()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = loop.time()
            raise
        raise AssertionError("unreachable")


@pytest.fixture
def universe(db_factory: sessionmaker[Session]) -> dict[int, int]:
    """Six symbols with Questrade ids 101-106 in TUE's universe (symbols.id -> questrade id)."""
    out: dict[int, int] = {}
    with db_factory() as s:
        for i in range(6):
            sid = add_symbol(s, f"S{i}", questrade_id=101 + i)
            out[sid] = 101 + i
            s.add(
                m.UniverseSnapshot(
                    session_date=TUE,
                    symbol_id=sid,
                    price=Decimal("20"),
                    avg_volume=1,
                    atr14=None,
                    source="finviz",
                )
            )
        s.commit()
    return out


def _clear_candles(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        s.execute(delete(m.IntradayCandle))
        s.commit()


async def _opening(
    factory: sessionmaker[Session], client: Any, *, tapped: bool, deadline: float = 45.0
) -> tuple[Any, list[dict[str, Any]], QuoteTap | None, float]:
    _clear_candles(factory)
    clock = FixedClock(AFTER_BAR)
    tap = QuoteTap(client, clock) if tapped else None
    svc = MarketDataService(factory, clock, CAL, tap or client, fetch_deadline_s=deadline)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with capture_logs() as logs:
        got = await svc.opening_bars(TUE)
    found = [dict(e) for e in logs if e["event"].startswith("market.opening_bars")]
    return got, found, tap, loop.time() - started


def _without_elapsed(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k != "elapsed_s"} for e in logs]


@pytest.mark.db
async def test_opening_bars_through_the_tap_equal_opening_bars_without_it(
    db_factory: sessionmaker[Session], universe: dict[int, int]
) -> None:
    def client() -> StatsQuestrade:
        qt = StatsQuestrade()
        for qid in list(universe.values())[:4]:
            qt.add_bars(qid, "FiveMinutes", [c5(OPEN)])
        return qt

    plain, plain_logs, _, _ = await _opening(db_factory, client(), tapped=False)
    tapped, tapped_logs, tap, _ = await _opening(db_factory, client(), tapped=True)
    assert tapped == plain
    assert len(plain_logs) == 1 and "client_stats" in plain_logs[0]
    assert _without_elapsed(tapped_logs) == _without_elapsed(plain_logs)
    assert tap is not None and len(tap.health_detail()["candle_batches"]) == 1


@pytest.mark.db
async def test_the_deadline_is_honoured_identically_through_the_tap(
    db_factory: sessionmaker[Session], universe: dict[int, int]
) -> None:
    runs = []
    for tapped in (False, True):
        qt = HalfInTime()
        for qid in universe.values():
            qt.add_bars(qid, "FiveMinutes", [c5(OPEN)])
        got, logs, _, _ = await _opening(db_factory, qt, tapped=tapped, deadline=0.2)
        runs.append((got, qt.deadlines))
    (plain, plain_deadlines), (through, tap_deadlines) = runs
    assert through.missing == plain.missing
    assert sorted(through.missing.values()) == ["timeout"] * 3
    assert plain_deadlines == tap_deadlines == [0.2]


@pytest.mark.db
async def test_the_0935_guard_stops_a_client_that_ignores_the_deadline_at_the_same_moment(
    db_factory: sessionmaker[Session], universe: dict[int, int]
) -> None:
    plain_qt, tap_qt = IgnoresDeadline(), IgnoresDeadline()
    plain, _, _, _ = await _opening(db_factory, plain_qt, tapped=False, deadline=0.2)
    through, _, tap, _ = await _opening(db_factory, tap_qt, tapped=True, deadline=0.2)
    assert through == plain
    assert set(through.missing.values()) == {"timeout"} and not through.bars
    plain_guard = plain_qt.cancelled - plain_qt.entered
    tap_guard = tap_qt.cancelled - tap_qt.entered
    assert 0.39 <= plain_guard < 0.5  # deadline 0.2 + min(DEADLINE_GUARD_S, 0.2)
    assert abs(tap_guard - plain_guard) < 0.010, (plain_guard, tap_guard)
    assert plain_qt.deadlines == tap_qt.deadlines == [0.2]
    assert tap is not None
    (batch,) = tap.health_detail()["candle_batches"]
    assert batch["raised"] == "CancelledError" and batch["deadline_s"] == 0.2


# --- 14. no yield of its own --------------------------------------------------------------------------------


def _first_send(coro: Any) -> Any:
    with pytest.raises(StopIteration) as info:
        coro.send(None)
    return info.value.value


async def test_the_tap_never_suspends_where_the_inner_call_does_not() -> None:
    inner = Inner()
    inner.quotes_result = [quote(1)]
    inner.many_result = Result({req(1): [candle()]})
    tap = QuoteTap(inner, FixedClock(T0))
    assert _first_send(tap.quotes([1])) is inner.quotes_result
    assert _first_send(tap.candles(1, T0, T0, "OneMinute")) is inner.candles_result
    assert _first_send(tap.candles_many([req(1)], deadline_s=45.0)) is inner.many_result


class _Once:
    def __await__(self) -> Generator[None, None, None]:
        yield None


class SuspendsOnce(Inner):
    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        await _Once()
        return self.quotes_result

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        await _Once()
        return self.candles_result

    async def candles_many(
        self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        await _Once()
        return self.many_result


def _suspensions(coro: Any) -> tuple[int, Any]:
    count = 0
    while True:
        try:
            coro.send(None)
        except StopIteration as stop:
            return count, stop.value
        count += 1


async def test_the_tap_suspends_exactly_as_often_as_the_inner_call() -> None:
    inner = SuspendsOnce()
    inner.many_result = Result()
    tap = QuoteTap(inner, FixedClock(T0))
    assert _suspensions(tap.quotes([1])) == (1, inner.quotes_result)
    assert _suspensions(tap.candles(1, T0, T0, "OneMinute")) == (1, inner.candles_result)
    count, value = _suspensions(tap.candles_many([req(1)], deadline_s=45.0))
    assert count == 1 and value is inner.many_result


# --- 13. static shape ---------------------------------------------------------------------------------------

MARKS = sorted((TRADER / "marks").glob("*.py"))
TAP = TRADER / "marks" / "tap.py"
DECISION_WRITES = {"submit", "snapshot_equity", "evaluate", "pause", "reset", "record", "log_event"}
TAP_FORBIDDEN = {
    "create_task",
    "ensure_future",
    "gather",
    "wait",
    "wait_for",
    "shield",
    "timeout",
    "sleep",
    "to_thread",
    "run_in_executor",
    "Lock",
    "Semaphore",
    "Event",
    "Condition",
    "open",
    "sessionmaker",
    "Session",
    "select",
    "insert",
    "httpx",
}
DECISION_PATH = (
    "strategies",
    "engine",
    "broker",
    "market",
    "jobs/nightly.py",
    "jobs/premarket.py",
    "adapters/claude/catalyst.py",
    "settings_store.py",
)


def _names(tree: ast.AST) -> set[str]:
    """Every identifier a module references: names, attributes, imported modules and names, and aliases."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.update(node.name.split("."))
            if node.asname:
                out.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.update(node.module.split("."))
    return out


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_marks_reference_no_decision_path_write_api() -> None:
    assert len(MARKS) >= 4
    for path in MARKS:
        found = _names(_tree(path)) & DECISION_WRITES
        assert not found, (path.name, found)


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else type(node).__name__


def test_marks_write_only_quote_marks_and_mark_bars() -> None:
    targets: list[str] = []
    for path in MARKS:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in ("insert", "update", "delete") and node.args:
                    targets.append(_dotted(node.args[0]))
                assert name not in ("merge", "add_all", "bulk_save_objects", "bulk_insert_mappings"), path
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                words = node.value.upper().split()
                assert not {"INSERT", "UPDATE", "DELETE", "TRUNCATE"} & set(words[:1]), (
                    path.name,
                    node.value,
                )
    assert targets and set(targets) <= {"m.QuoteMark", "m.MarkBar"}, targets


def test_no_decision_path_module_imports_trader_marks() -> None:
    offenders = []
    for root in DECISION_PATH:
        target = TRADER / root
        for path in [target] if target.suffix == ".py" else sorted(target.rglob("*.py")):
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("trader.marks"):
                    offenders.append(str(path))
                if isinstance(node, ast.Import) and any(
                    a.name.startswith("trader.marks") for a in node.names
                ):
                    offenders.append(str(path))
                if isinstance(node, ast.ImportFrom) and node.module == "trader":
                    if any(a.name == "marks" for a in node.names):
                        offenders.append(str(path))
    assert not offenders, offenders


def _tap_class() -> ast.ClassDef:
    (cls,) = [n for n in _tree(TAP).body if isinstance(n, ast.ClassDef) and n.name == "QuoteTap"]
    return cls


@pytest.mark.parametrize("method", ["quotes", "candles", "candles_many"])
def test_each_proxied_method_awaits_exactly_once_straight_to_the_inner_method(method: str) -> None:
    (fn,) = [n for n in _tap_class().body if isinstance(n, ast.AsyncFunctionDef) and n.name == method]
    awaits = [n for n in ast.walk(fn) if isinstance(n, ast.Await)]
    assert len(awaits) == 1
    call = awaits[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    assert call.func.attr == method
    target = call.func.value
    assert isinstance(target, ast.Attribute) and target.attr == "_inner"
    assert isinstance(target.value, ast.Name) and target.value.id == "self"
    for node in ast.walk(fn):  # no async with / async for (hidden awaits), no context manager
        assert not isinstance(node, ast.AsyncWith | ast.AsyncFor | ast.With), method


def test_the_tap_module_awaits_only_in_the_three_proxied_methods() -> None:
    owners = []
    for fn in ast.walk(_tree(TAP)):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            if any(isinstance(n, ast.Await | ast.AsyncWith | ast.AsyncFor) for n in ast.walk(fn)):
                owners.append(fn.name)
    assert sorted(owners) == ["candles", "candles_many", "quotes"]


def test_the_tap_module_references_no_task_lock_timeout_or_io() -> None:
    tree = _tree(TAP)
    found = _names(tree) & TAP_FORBIDDEN
    assert not found, found
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.finalbody:
                assert not [n for n in ast.walk(stmt) if isinstance(n, ast.Return)], "return in finally"
        if isinstance(node, ast.Attribute) and node.attr in ("error", "critical", "exception"):
            assert not (isinstance(node.value, ast.Name) and node.value.id == "log"), node.attr


def test_the_real_client_stats_are_dataclasses_the_tap_can_snapshot() -> None:
    assert dataclasses.is_dataclass(CallStats)
    assert set(dataclasses.asdict(CallStats())) == {
        "requests",
        "http_429",
        "pause_s",
        "http_5xx",
        "transport_errors",
    }
