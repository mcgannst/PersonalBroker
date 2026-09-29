"""DB-T2 acceptance tests 6, 10 and 11 (unit part): the `MarkPublisher` loop without a database.

- A pass with no new observations touches no database (test 6).
- A failing pass writes one warning event per failure streak and one info event on recovery, `failing` shows
  the streak, `run()` keeps looping and `run_once` never raises (test 10).
- The database step runs in the publisher's own single-thread executor (threads named `marks*`), never on the
  event loop and never in the default executor the Questrade token fetch uses (test 11).
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from structlog.testing import capture_logs

from trader.adapters.questrade.models import QtQuote
from trader.market.clock import FixedClock
from trader.marks.publisher import MarkPublisher, MarkPublisherDeps
from trader.marks.types import ObservedQuote

T0 = datetime(2026, 10, 6, 14, 0, 10, tzinfo=UTC)
RUN = 7


def observed(qid: int = 101, last: str = "10.00", at: datetime = T0) -> ObservedQuote:
    q = QtQuote(qid, f"Q{qid}", None, None, Decimal(last), None, 0, None, 0, False, None)
    return ObservedQuote(qid, q, at)


class Tap:
    """A LatestQuotes fake: each `drain()` returns the next batch (or `every` when set, or nothing)."""

    def __init__(self, *batches: list[ObservedQuote], every: list[ObservedQuote] | None = None) -> None:
        self.batches = list(batches)
        self.every = every
        self.drains = 0
        self.error: Exception | None = None

    def drain(self) -> list[ObservedQuote]:
        self.drains += 1
        if self.error is not None:
            raise self.error
        if self.every is not None:
            return list(self.every)
        return self.batches.pop(0) if self.batches else []


class NoDb:
    kw: dict[str, Any] = {}

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise RuntimeError("database down: password=hunter2")


@dataclass
class Events:
    rows: list[tuple[str, str, dict[str, Any], int | None]] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    fail: bool = False

    def __call__(self, level: str, message: str, data: dict[str, Any], run_id: int | None) -> None:
        self.threads.append(threading.current_thread().name)
        self.rows.append((level, message, data, run_id))
        if self.fail:
            raise RuntimeError("event log down")


def make(
    tap: Tap, *, factory: Any = None, run_id: Any = lambda: RUN, events: Events | None = None, **kw: Any
) -> tuple[MarkPublisher, Events, FixedClock]:
    ev = events if events is not None else Events()
    clock = FixedClock(T0)
    db: Any = factory if factory is not None else NoDb()
    deps = MarkPublisherDeps(db, clock, tap, run_id, ev)
    return MarkPublisher(deps, **kw), ev, clock


# --- 6. no observations, no database ------------------------------------------------------------------------


async def test_no_observations_means_no_database_work() -> None:
    factory = NoDb()

    def no_run_id() -> int | None:
        raise AssertionError("run_id must not be resolved without observations")

    pub, events, clock = make(Tap(), factory=factory, run_id=no_run_id)
    try:
        step = await pub.run_once()
    finally:
        pub.close()
    assert step.skipped == "no_new_quotes" and (step.marks_written, step.bars_written) == (0, 0)
    assert step.at == clock.now()
    assert factory.calls == 0 and events.rows == []
    assert pub.health_detail() == {"written_at": None, "symbols": 0, "failing": False}


# --- 10. failure policy -------------------------------------------------------------------------------------


async def test_one_warning_per_failure_streak_and_one_info_on_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pub, events, clock = make(Tap(every=[observed()]))
    try:
        with capture_logs() as logs:
            steps = [await pub.run_once() for _ in range(3)]
        assert [s.skipped for s in steps] == ["error"] * 3
        assert [e[0] for e in events.rows] == ["warning"]
        level, message, data, run_id = events.rows[0]
        assert "hunter2" not in message and "hunter2" not in str(data)
        assert data["error_type"] == "RuntimeError"
        assert pub.health_detail()["failing"] is True
        assert len([e for e in logs if e.get("log_level") == "warning"]) == 1
        assert "hunter2" not in str(logs)

        monkeypatch.setattr(pub, "_write", lambda obs, run_id, now: (1, 1))
        clock.advance(timedelta(seconds=2))
        ok = await pub.run_once()
        assert ok.skipped is None and (ok.marks_written, ok.bars_written) == (1, 1)
        assert [e[0] for e in events.rows] == ["warning", "info"]
        assert events.rows[1][1] == "marks recovered after 3 failures"
        assert pub.health_detail() == {"written_at": clock.now().isoformat(), "symbols": 1, "failing": False}
        await pub.run_once()
        assert len(events.rows) == 2  # no event while healthy
    finally:
        pub.close()


async def test_run_keeps_looping_through_failures_and_stops_on_stop() -> None:
    stop = asyncio.Event()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 5:
            stop.set()

    tap = Tap(every=[observed()])
    pub, events, _ = make(tap, sleep=sleep, interval_s=2.0)
    try:
        await asyncio.wait_for(pub.run(stop), timeout=10)
    finally:
        pub.close()
    assert tap.drains >= 5 and set(sleeps) == {2.0}
    assert [e[0] for e in events.rows] == ["warning"]


async def test_run_once_never_raises() -> None:
    tap = Tap()
    tap.error = RuntimeError("drain broke")
    pub, _, _ = make(tap)
    try:
        assert (await pub.run_once()).skipped == "error"
        tap.error = None
        tap.every = [observed()]
        failing_events = Events(fail=True)
        pub2, _, _ = make(tap, events=failing_events)
        try:
            assert (await pub2.run_once()).skipped == "error"  # the event writer raising is absorbed too
        finally:
            pub2.close()
    finally:
        pub.close()


async def test_no_run_discards_the_observations_without_error() -> None:
    pub, events, _ = make(Tap(every=[observed()]), run_id=lambda: None)
    try:
        step = await pub.run_once()
    finally:
        pub.close()
    assert step.skipped == "no_run" and events.rows == []
    assert pub.health_detail()["failing"] is False


async def test_close_is_idempotent_and_a_closed_publisher_still_never_raises() -> None:
    pub, _, _ = make(Tap(every=[observed()]))
    pub.close()
    pub.close()
    assert (await pub.run_once()).skipped == "error"


# --- 11. off the event loop and off the default executor ----------------------------------------------------


def test_a_stuck_database_step_blocks_neither_the_loop_nor_the_default_executor() -> None:
    threads: list[str] = []
    started = threading.Event()

    def stuck(obs: Any, run_id: int, now: datetime) -> tuple[int, int]:
        threads.append(threading.current_thread().name)
        started.set()
        time.sleep(3)
        return (1, 1)

    async def main() -> tuple[int, float, Any]:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        pub, _, _ = make(Tap(every=[observed()]))
        pub._write = stuck  # type: ignore[method-assign,assignment]
        ticks = 0
        done = False

        async def ticker() -> None:
            nonlocal ticks
            while not done:
                await asyncio.sleep(0.1)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        pass_task = asyncio.create_task(pub.run_once())
        while not started.is_set():
            await asyncio.sleep(0.01)
        t = loop.time()
        assert await asyncio.to_thread(lambda: 42) == 42  # the token fetch's path is free
        to_thread_s = loop.time() - t
        step = await pass_task
        done = True
        await tick_task
        pub.close()
        return ticks, to_thread_s, step

    ticks, to_thread_s, step = asyncio.run(main())
    assert step.skipped is None
    assert ticks >= 25, ticks
    assert to_thread_s < 0.5, to_thread_s
    assert threads and all(name.startswith("marks") for name in threads), threads


async def test_the_failure_event_is_written_in_the_publisher_thread() -> None:
    pub, events, _ = make(Tap(every=[observed()]))
    try:
        await pub.run_once()
    finally:
        pub.close()
    assert events.threads and all(name.startswith("marks") for name in events.threads)
