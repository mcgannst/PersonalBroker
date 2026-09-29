"""FIX-401 (g): the worker re-reads the live run in its idle phase too, every LIVE_RUN_CHECK_SECONDS (60 s)
by its clock, so a switch made at any time is noticed within a minute (on 09-29 run 193, created at
02:12 ET, was only seen at 09:29 ET). The idle poll never sleeps longer than that while a check is wired.
Fake clock throughout; no real waiting."""

import asyncio
import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.test_worker import SAT, TUE, Harness, VirtualTime, _run, et
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings
from trader.worker import LIVE_RUN_CHECK_SECONDS, Worker

pytestmark = pytest.mark.db


class Check:
    def __init__(
        self, clock: FixedClock, *, changes_at: datetime | None = None, stop: asyncio.Event | None = None
    ):
        self.clock = clock
        self.calls: list[datetime] = []
        self.changes_at = changes_at
        self.stop = stop
        self.error: Exception | None = None

    def __call__(self) -> bool:
        now = self.clock.now()
        self.calls.append(now)
        if self.error is not None:
            raise self.error
        if self.changes_at is not None and now >= self.changes_at:
            if self.stop is not None:
                self.stop.set()
            return False
        return True


def worker(h: Harness, check: Check) -> Worker:
    return Worker(h.deps(), live_run_check=check)


def test_the_check_period_is_a_minute() -> None:
    assert LIVE_RUN_CHECK_SECONDS == 60.0


async def test_idle_steps_check_the_live_run_at_most_once_a_minute(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(SAT, 3, 0))
    h = Harness(db_factory, clock)
    check = Check(clock)
    w = worker(h, check)
    for _ in range(9):  # 0, 15, ..., 120 s
        await w.step()
        clock.advance(timedelta(seconds=15))
    t0 = et(SAT, 3, 0)
    assert check.calls == [t0, t0 + timedelta(seconds=60), t0 + timedelta(seconds=120)]


async def test_pre_market_idle_steps_check_too(db_factory: sessionmaker[Session]) -> None:
    """02:12 ET on a session day (the 09-29 switch): idle, pre-market, long before the session loop."""
    clock = FixedClock(et(TUE, 2, 12))
    h = Harness(db_factory, clock)
    check = Check(clock)
    w = worker(h, check)
    report = await w.step()
    assert report.phase != "open" and report.fired == []
    assert check.calls == [et(TUE, 2, 12)]
    assert h.engines == []  # nothing else is built in the idle phase


async def test_session_steps_leave_the_check_to_the_session_path(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    h = Harness(db_factory, clock)
    check = Check(clock)
    w = worker(h, check)
    await w.step()
    assert check.calls == []  # in the session, `fired` and `engine_for` carry the watch (runtime)


async def test_a_failing_check_never_breaks_the_idle_step(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(SAT, 3, 0))
    h = Harness(db_factory, clock)
    check = Check(clock)
    check.error = RuntimeError("db down")
    w = worker(h, check)
    report = await w.step()
    assert report.fired == [] and check.calls == [et(SAT, 3, 0)]


async def test_the_idle_poll_is_capped_at_the_check_period(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(SAT, 3, 0))
    h = Harness(db_factory, clock, settings=RuntimeSettings.model_validate({"worker.idle_poll_seconds": 300}))
    w = worker(h, Check(clock))
    await w.step()
    assert w._interval() == LIVE_RUN_CHECK_SECONDS
    plain = Worker(h.deps())  # without a check the configured idle poll stands
    await plain.step()
    assert plain._interval() == 300


async def test_a_switch_at_night_stops_the_running_worker_within_a_minute(
    db_factory: sessionmaker[Session],
) -> None:
    start = et(SAT, 2, 0)
    clock = FixedClock(start)
    vt = VirtualTime(clock)
    h = Harness(
        db_factory,
        clock,
        sleep=vt.sleep,
        settings=RuntimeSettings.model_validate({"worker.idle_poll_seconds": 300}),
    )
    stop = asyncio.Event()
    switch = start + timedelta(minutes=12, seconds=4)
    check = Check(clock, changes_at=switch, stop=stop)
    await _run(worker(h, check), stop, vt)
    assert check.calls[-1] - switch < timedelta(seconds=LIVE_RUN_CHECK_SECONDS)
    assert check.calls[-1] >= switch
    assert all(b - a >= timedelta(seconds=LIVE_RUN_CHECK_SECONDS) for a, b in itertools.pairwise(check.calls))
