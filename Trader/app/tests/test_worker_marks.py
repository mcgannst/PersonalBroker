"""DB-T2 acceptance test 12: the worker runs the mark publisher (`WorkerDeps.marks`) as a supervised task.

It starts after the decisions loop, beside the relay; a `run` that raises is restarted after 30 s (virtual
time); stop cancels it without waiting; `--once` never starts it; with `marks=None` nothing changes (the
existing tests/test_worker.py passes unchanged).
"""

import asyncio
import dataclasses
import time
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.test_worker import TUE, Harness, VirtualTime, _run, et
from trader.market.clock import FixedClock
from trader.worker import Worker, WorkerDeps

pytestmark = pytest.mark.db


class FakeLoop:
    """A `run(stop)` task body: raises on the first `crash` calls, then waits for stop (or never returns)."""

    def __init__(
        self, name: str, clock: FixedClock, order: list[str], *, crash: int = 0, ignore_stop: bool = False
    ):
        self.name = name
        self.clock = clock
        self.order = order
        self.crash = crash
        self.ignore_stop = ignore_stop
        self.calls: list[datetime] = []
        self.cancelled = False

    async def run(self, stop: asyncio.Event) -> None:
        self.calls.append(self.clock.now())
        self.order.append(self.name)
        if len(self.calls) <= self.crash:
            raise RuntimeError(f"{self.name} crashed")
        try:
            if self.ignore_stop:
                await asyncio.Future()  # never returns: only a cancel ends it
            await stop.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_marks_is_the_last_worker_dep_and_defaults_to_none() -> None:
    fields = dataclasses.fields(WorkerDeps)
    assert fields[-1].name == "marks" and fields[-1].default is None
    assert [f.name for f in fields][-2] == "decisions"


async def test_the_worker_starts_the_publisher_after_the_decisions_loop_and_restarts_it_after_30_s(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    order: list[str] = []
    decisions = FakeLoop("decisions", clock, order)
    marks = FakeLoop("marks", clock, order, crash=1)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    h.on_relay = lambda: stop.set() if len(h.relay_calls) >= 25 else None
    worker = Worker(WorkerDeps(**{**vars(h.deps()), "decisions": decisions, "marks": marks}))
    await _run(worker, stop, vt)
    start = et(TUE, 10, 0)
    assert order[:2] == ["decisions", "marks"]  # started after the decisions loop
    assert marks.calls == [start, start + timedelta(seconds=30)]  # restarted after it raised
    assert (
        len(h.relay_calls) == 25 and h.engines and len(h.engines[-1].polls) >= 25
    )  # the step loop unchanged


async def test_stop_cancels_the_publisher_without_waiting(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    marks = FakeLoop("marks", clock, [], ignore_stop=True)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    h.on_relay = lambda: stop.set() if len(h.relay_calls) >= 3 else None
    worker = Worker(WorkerDeps(**{**vars(h.deps()), "marks": marks}))
    started = time.monotonic()
    await _run(worker, stop, vt)
    assert marks.calls and marks.cancelled
    assert time.monotonic() - started < 5.0


async def test_once_never_starts_the_publisher(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    marks = FakeLoop("marks", clock, [])
    h = Harness(db_factory, clock, sleep=vt.sleep)
    worker = Worker(WorkerDeps(**{**vars(h.deps()), "marks": marks}))
    await _run(worker, asyncio.Event(), vt, once=True)
    assert marks.calls == [] and len(h.relay_calls) == 1


async def test_without_a_publisher_the_worker_runs_as_before(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    h.on_relay = lambda: stop.set() if len(h.relay_calls) >= 5 else None
    deps: Any = h.deps()
    assert deps.marks is None
    await _run(Worker(deps), stop, vt)
    assert len(h.relay_calls) == 5
