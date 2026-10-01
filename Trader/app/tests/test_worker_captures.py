"""FIX-DAY1 (2): the worker's timed quote captures on a virtual clock.

Wed 2026-09-30 the 9:35 quotes were read 6.3 s after 09:35:00 (inside the 09:35:05 ORB event), so a breakout's
day high had already moved past the bar's (NVTS). The worker now runs two timed captures: `open` just before
09:30:00 (the volume at the open) and `bar` at 09:35:00.0 sharp, waking for them whatever its poll cadence;
the ORB event at 09:35:05 then builds its bars from the stored capture. A capture whose time passed more than
CAPTURE_WINDOW ago (a restart) is not run.
"""

import asyncio
import dataclasses
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.test_worker import CAL, TUE, Harness, VirtualTime, _run, et
from trader.market.clock import FixedClock
from trader.market.quote_bars import CAPTURE_BAR, CAPTURE_OPEN, OPEN_CAPTURE_LEAD
from trader.worker import Worker

pytestmark = pytest.mark.db


class FakeCaptures:
    def __init__(self, clock: FixedClock, *, fail: bool = False) -> None:
        self.clock = clock
        self.calls: list[tuple[str, date, datetime]] = []
        self.fail = fail

    def times(self, day: date) -> Sequence[tuple[str, datetime]]:
        if not CAL.is_session(day):
            return []
        open_ = CAL.session_open(day)
        return [(CAPTURE_OPEN, open_ - OPEN_CAPTURE_LEAD), (CAPTURE_BAR, open_ + timedelta(minutes=5))]

    async def capture(self, kind: str, day: date) -> Any:
        self.calls.append((kind, day, self.clock.now()))
        if self.fail:
            raise RuntimeError("quotes down")
        return {"kind": kind}


async def test_captures_fire_at_09_29_55_and_09_35_00_sharp_on_a_virtual_clock(
    db_factory: sessionmaker[Session],
) -> None:
    """The quote-poll cadence (2 s) starts at an odd 09:29:01.3; the worker still wakes on the second."""
    clock = FixedClock(et(TUE, 9, 29, 1) + timedelta(milliseconds=300))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    caps = FakeCaptures(clock)
    stop = asyncio.Event()
    w = Worker(h.deps(), captures=caps)
    h.on_relay = lambda: stop.set() if clock.now() >= et(TUE, 9, 35, 8) else None
    await _run(w, stop, vt)
    assert caps.calls == [
        (CAPTURE_OPEN, TUE, et(TUE, 9, 29, 55)),  # FIX-DAY1b: 5 s before the open
        (CAPTURE_BAR, TUE, et(TUE, 9, 35, 0)),
    ]
    # the ORB event fires after the bar capture, at its first step at or after 09:35:05
    orb = [at for key, _, at in h.fire_calls if key == "orb_open"]
    assert len(orb) == 1 and et(TUE, 9, 35, 5) <= orb[0] < et(TUE, 9, 35, 7)
    assert orb[0] > caps.calls[1][2]


async def test_each_capture_runs_once_per_session(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 9, 35, 0))
    h = Harness(db_factory, clock)
    caps = FakeCaptures(clock)
    w = Worker(h.deps(), captures=caps)
    for _ in range(4):
        await w.step()
        clock.advance(timedelta(seconds=1))
    # the open capture's time passed 302 s ago: skipped; the bar capture runs once
    assert [(k, at) for k, _, at in caps.calls] == [(CAPTURE_BAR, et(TUE, 9, 35, 0))]


async def test_a_capture_runs_before_the_events_of_the_same_step(db_factory: sessionmaker[Session]) -> None:
    """A worker busy until 09:35:00.5 still captures first (inside the 1 s window, FIX-DAY1b), and an
    orb_open that is due in the same step fires after it."""
    clock = FixedClock(et(TUE, 9, 35, 0))
    h = Harness(db_factory, clock)
    order: list[str] = []
    caps = FakeCaptures(clock)
    real_capture, real_fire = caps.capture, h.fire

    async def capture(kind: str, day: date) -> Any:
        order.append(f"capture:{kind}")
        return await real_capture(kind, day)

    async def fire(key: str, d: date) -> Any:
        order.append(f"fire:{key}")
        return await real_fire(key, d)

    caps.capture = capture  # type: ignore[method-assign]
    h.fire = fire  # type: ignore[method-assign]
    w = Worker(h.deps(), captures=caps)
    clock.set(et(TUE, 9, 35, 0) + timedelta(milliseconds=500))
    h.plan = lambda d: dataclasses.replace(  # type: ignore[method-assign]
        Harness.plan(h, d),
        events=tuple(
            dataclasses.replace(e, at=e.at - timedelta(seconds=5)) if e.key == "orb_open" else e
            for e in Harness.plan(h, d).events
        ),
    )
    w = Worker(h.deps(), captures=caps)
    await w.step()
    assert order == ["capture:bar", "fire:orb_open"]


async def test_a_restart_after_the_window_never_captures_late(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 9, 35, 5) + timedelta(milliseconds=1))
    h = Harness(db_factory, clock)
    caps = FakeCaptures(clock)
    w = Worker(h.deps(), captures=caps)
    await w.step()
    assert caps.calls == []  # 5.001 s late: the quotes would not be the 09:35:00 ones


async def test_a_failing_capture_is_counted_once_and_the_step_goes_on(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 9, 35, 0))
    h = Harness(db_factory, clock)
    caps = FakeCaptures(clock, fail=True)
    w = Worker(h.deps(), captures=caps)
    report = await w.step()
    assert len(caps.calls) == 1 and report.phase == "open"
    assert len(h.engines[0].polls) == 1  # the quote poll still ran
    clock.advance(timedelta(seconds=2))
    await w.step()
    assert len(caps.calls) == 1  # not retried: a second try would be late


async def test_no_captures_on_a_non_session_day(db_factory: sessionmaker[Session]) -> None:
    sat = date(2026, 10, 3)
    clock = FixedClock(et(sat, 9, 35, 0))
    h = Harness(db_factory, clock)
    caps = FakeCaptures(clock)
    w = Worker(h.deps(), captures=caps)
    await w.step()
    assert caps.calls == []


async def test_without_captures_the_session_interval_is_the_quote_poll(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 9, 34, 59))
    h = Harness(db_factory, clock)
    w = Worker(h.deps())
    await w.step()
    assert w._interval() == h.settings.quote_poll_seconds


async def test_the_session_interval_wakes_for_the_next_capture(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 9, 34, 59) + timedelta(milliseconds=250))
    h = Harness(db_factory, clock)
    w = Worker(h.deps(), captures=FakeCaptures(clock))
    await w.step()
    assert w._interval() == pytest.approx(0.75)


# --- FIX-DAY1b: per-kind windows ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("at", "runs"),
    [
        (et(TUE, 9, 35, 1), True),
        (et(TUE, 9, 35, 1) + timedelta(milliseconds=1), False),  # the bar would be read > 1 s late
    ],
)
async def test_the_bar_capture_runs_only_within_one_second_of_09_35_00(
    db_factory: sessionmaker[Session], at: datetime, runs: bool
) -> None:
    clock = FixedClock(at)
    h = Harness(db_factory, clock)
    caps = FakeCaptures(clock)
    await Worker(h.deps(), captures=caps).step()
    assert [k for k, _, _ in caps.calls] == ([CAPTURE_BAR] if runs else [])


@pytest.mark.parametrize(
    ("at", "runs"),
    [
        (et(TUE, 9, 29, 58), True),  # still 2 s before the opening print
        (et(TUE, 9, 29, 58) + timedelta(milliseconds=1), False),
    ],
)
async def test_the_open_capture_runs_only_up_to_two_seconds_before_the_open(
    db_factory: sessionmaker[Session], at: datetime, runs: bool
) -> None:
    clock = FixedClock(at)
    h = Harness(db_factory, clock)
    caps = FakeCaptures(clock)
    await Worker(h.deps(), captures=caps).step()
    assert [k for k, _, _ in caps.calls] == ([CAPTURE_OPEN] if runs else [])


async def test_the_interval_stops_waking_for_a_capture_past_its_window(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 9, 35, 1) + timedelta(milliseconds=500))
    h = Harness(db_factory, clock)
    w = Worker(h.deps(), captures=FakeCaptures(clock))
    assert w._until_next_capture(clock.now()) is None
