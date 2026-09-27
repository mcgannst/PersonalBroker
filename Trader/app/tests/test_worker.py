"""P3-T9 acceptance tests: the worker loop (SPEC §1, §9; Review Focus 1 and 4).

The engine, the event firing, the relay, the bot and the session end are fakes; the heartbeat, the error
events and the single-instance lock use the testcontainers database. Time is a FixedClock moved by hand
(`step()` tests) or by `VirtualTime`, a fake sleep that advances the clock (`run()` tests). No test waits
in real time.
"""

import asyncio
import heapq
import itertools
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from trader import worker as worker_mod
from trader.db.models import EventLog, WorkerHeartbeat
from trader.engine.scheduler import DayPlan, FireResult, PlannedEvent
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings
from trader.worker import (
    StepReport,
    Worker,
    WorkerDeps,
    acquire_single_instance,
    main,
    release_single_instance,
)

pytestmark = pytest.mark.db

ET = ZoneInfo("America/New_York")
CAL = SessionCalendar()
TUE = date(2026, 10, 6)
WED = date(2026, 10, 7)
SAT = date(2026, 10, 3)
EARLY = date(2026, 11, 27)  # the day after Thanksgiving, 13:00 ET close
SAFETY = ("flatten", "entry_cancel", "overlay_decision")


def et(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=ET).astimezone(UTC)


class FakeEngine:
    def __init__(self, session_date: date, clock: FixedClock) -> None:
        self.session_date = session_date
        self.clock = clock
        self.polls: list[datetime] = []
        self.ticks: list[datetime] = []
        self.ended: list[date] = []
        self.poll_error: Exception | None = None
        self.fills_per_poll = 0

    async def run_event(self, event_key: str, session_date: date) -> Any:
        return None

    async def poll_quotes(self) -> list[Any]:
        self.polls.append(self.clock.now())
        if self.poll_error is not None:
            raise self.poll_error
        return [object()] * self.fills_per_poll

    async def tick(self, now: datetime) -> None:
        self.ticks.append(now)

    async def end_of_session(self, session_date: date) -> list[Any]:
        self.ended.append(session_date)
        return []


@dataclass
class Harness:
    """The worker's collaborators as recording fakes. `fire` marks a key fired (as the real one records a
    succeeded job run), and `fired` reads that back, so the worker sees what the database would say."""

    factory: sessionmaker[Session]
    clock: FixedClock
    settings: RuntimeSettings = field(default_factory=RuntimeSettings)
    engines: list[FakeEngine] = field(default_factory=list)
    fire_calls: list[tuple[str, date, datetime]] = field(default_factory=list)
    settled: dict[date, set[str]] = field(default_factory=dict)
    relay_calls: list[datetime] = field(default_factory=list)
    end_calls: list[tuple[date, datetime]] = field(default_factory=list)
    fire_error: Exception | None = None
    relay_error: Exception | None = None
    on_relay: Callable[[], None] | None = None
    bot: Callable[[asyncio.Event], Awaitable[None]] | None = None
    sleep: Callable[[float], Awaitable[None]] | None = None

    def plan(self, d: date) -> DayPlan:
        if not CAL.is_session(d):
            return DayPlan(d, False, None, None, ())
        open_, close = CAL.session_open(d), CAL.session_close(d)
        events = [
            ("orb_open", open_ + timedelta(minutes=5, seconds=5)),
            ("entry_cancel", open_ + timedelta(hours=2)),
            ("overlay_decision", close - timedelta(minutes=30)),
            ("flatten", close - timedelta(minutes=10)),
        ]
        planned = tuple(
            PlannedEvent(key, at, ("orb_sip",), key in SAFETY)
            for key, at in sorted(events, key=lambda e: e[1])
        )
        return DayPlan(d, True, open_, close, planned)

    async def fire(self, key: str, d: date) -> FireResult:
        self.fire_calls.append((key, d, self.clock.now()))
        if self.fire_error is not None:
            raise self.fire_error
        self.settled.setdefault(d, set()).add(key)
        return FireResult(key, d, "fired", {"strategies": ["orb_sip"], "outcomes": 0})

    def fired(self, d: date) -> set[str]:
        return set(self.settled.get(d, set()))

    async def relay(self) -> None:
        self.relay_calls.append(self.clock.now())
        if self.on_relay is not None:
            self.on_relay()
        if self.relay_error is not None:
            raise self.relay_error

    async def engine_for(self, d: date) -> FakeEngine:
        eng = FakeEngine(d, self.clock)
        self.engines.append(eng)
        return eng

    async def end_session(self, d: date, body: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        self.end_calls.append((d, self.clock.now()))
        return await body()

    def deps(self) -> WorkerDeps:
        extra: dict[str, Any] = {"sleep": self.sleep} if self.sleep is not None else {}
        return WorkerDeps(
            factory=self.factory,
            clock=self.clock,
            calendar=CAL,
            settings=lambda: self.settings,
            engine_for=self.engine_for,
            plan=self.plan,
            fire=self.fire,
            fired=self.fired,
            relay=self.relay,
            bot=self.bot,
            end_session=self.end_session,
            host="test-host",
            **extra,
        )


class VirtualTime:
    """A fake sleep on virtual time: each sleeper wakes, in time order, once every runnable task has had
    its turn, and the clock jumps to its wake time. Several tasks (the quote loop and the bot) can sleep
    at once, each seeing its own cadence."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self._heap: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self._driver: asyncio.Task[None] | None = None

    async def sleep(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        heapq.heappush(self._heap, (self.clock.now() + timedelta(seconds=seconds), next(self._seq), fut))
        if self._driver is None or self._driver.done():
            self._driver = loop.create_task(self._drive())
        await fut

    async def _drive(self) -> None:
        while self._heap:
            for _ in range(50):
                await asyncio.sleep(0)
            if not self._heap:
                return
            at, _, fut = heapq.heappop(self._heap)
            if fut.done():  # a cancelled sleep (the stop event won the race)
                continue
            if at > self.clock.now():
                self.clock.set(at)
            fut.set_result(None)

    async def aclose(self) -> None:
        if self._driver is not None and not self._driver.done():
            self._driver.cancel()
            await asyncio.gather(self._driver, return_exceptions=True)


def _events(factory: sessionmaker[Session], source: str = "worker") -> list[EventLog]:
    with factory() as s:
        return list(
            s.execute(select(EventLog).where(EventLog.source == source).order_by(EventLog.id)).scalars()
        )


def _heartbeat(factory: sessionmaker[Session]) -> WorkerHeartbeat | None:
    with factory() as s:
        return s.get(WorkerHeartbeat, "worker")


def _gaps(times: list[datetime]) -> set[float]:
    return {(b - a).total_seconds() for a, b in itertools.pairwise(times)}


async def _steps(w: Worker, clock: FixedClock, until: datetime, every: float) -> list[StepReport]:
    reports = []
    while clock.now() <= until:
        reports.append(await w.step())
        clock.advance(timedelta(seconds=every))
    return reports


async def _run(w: Worker, stop: asyncio.Event, vt: VirtualTime, **kw: Any) -> None:
    try:
        await asyncio.wait_for(w.run(stop, **kw), timeout=30)  # a real-time guard against a hung loop only
    finally:
        await vt.aclose()


# 1 ---------------------------------------------------------------------------------------------------------


async def test_orb_open_fires_once_at_the_first_step_after_its_time(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 9, 35, 0))
    h = Harness(db_factory, clock)
    w = Worker(h.deps())
    reports = await _steps(w, clock, et(TUE, 9, 35, 10), 2)
    assert [(k, d, at) for k, d, at in h.fire_calls] == [("orb_open", TUE, et(TUE, 9, 35, 6))]
    fired_at = [r.now for r in reports if r.fired]
    assert fired_at == [et(TUE, 9, 35, 6)] and reports[3].fired[0].status == "fired"
    assert all(r.phase == "open" for r in reports)


# 2 ---------------------------------------------------------------------------------------------------------


async def test_session_step_polls_ticks_relays_once_and_sleeps_quote_poll(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    h.on_relay = lambda: stop.set() if len(h.relay_calls) >= 6 else None
    w = Worker(h.deps())
    report = await w.step()  # one step on its own: each part exactly once
    assert (len(h.engines[0].polls), len(h.engines[0].ticks), len(h.relay_calls)) == (1, 1, 1)
    assert report.relayed and report.phase == "open"
    h.relay_calls.clear()
    await _run(w, stop, vt)
    eng = h.engines[0]
    assert len(eng.polls) == len(eng.ticks) == 1 + len(h.relay_calls) == 7
    assert _gaps(h.relay_calls) == {h.settings.quote_poll_seconds}


async def test_saturday_step_only_relays_and_sleeps_idle_poll(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(SAT, 11, 0))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    h.on_relay = lambda: stop.set() if len(h.relay_calls) >= 4 else None
    await _run(Worker(h.deps()), stop, vt)
    assert h.engines == [] and h.fire_calls == [] and h.end_calls == []
    assert len(h.relay_calls) == 4
    assert _gaps(h.relay_calls) == {h.settings.worker_idle_poll_seconds}


# 3 ---------------------------------------------------------------------------------------------------------


async def test_engine_is_built_once_per_session_and_rebuilt_for_the_next(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    h = Harness(db_factory, clock)
    w = Worker(h.deps())
    await _steps(w, clock, et(TUE, 10, 1), 2)
    assert [e.session_date for e in h.engines] == [TUE]
    clock.set(et(TUE, 18, 0))
    await w.step()  # after the close: no new engine for a closed session
    clock.set(et(WED, 9, 45))
    await _steps(w, clock, et(WED, 9, 46), 2)
    assert [e.session_date for e in h.engines] == [TUE, WED]
    assert len(h.engines[1].polls) == 31  # the new session's engine is the one polled


# 4 ---------------------------------------------------------------------------------------------------------


async def test_end_session_runs_once_after_the_close(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 15, 59, 50))
    h = Harness(db_factory, clock)
    h.settled[TUE] = {"orb_open", "entry_cancel", "overlay_decision", "flatten"}
    w = Worker(h.deps())
    reports = await _steps(w, clock, et(TUE, 16, 0, 20), 2)
    for _ in range(20):
        clock.advance(timedelta(seconds=30))
        reports.append(await w.step())
    assert h.end_calls == [(TUE, et(TUE, 16, 0))]
    assert [r.now for r in reports if r.ended] == [et(TUE, 16, 0)]
    assert h.engines[0].ended == [TUE]  # the body ran the engine's end_of_session
    assert all(r.relayed for r in reports)


async def test_after_close_start_fires_due_safety_events_before_ending(
    db_factory: sessionmaker[Session],
) -> None:
    """A worker that first runs after the close still offers the due events to `fire` (which decides:
    the real one records a safety event after the close as missed), then ends the session."""
    clock = FixedClock(et(TUE, 16, 5))
    h = Harness(db_factory, clock)
    h.settled[TUE] = {"orb_open", "entry_cancel", "overlay_decision"}
    w = Worker(h.deps())
    report = await w.step()
    assert [k for k, _, _ in h.fire_calls] == ["flatten"]
    assert report.ended and [e.ended for e in h.engines] == [[TUE]]


# 5 ---------------------------------------------------------------------------------------------------------


async def test_failing_parts_are_logged_and_the_step_goes_on(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 9, 35, 6))
    h = Harness(db_factory, clock)
    h.fire_error = RuntimeError("fire blew up")
    h.relay_error = RuntimeError("relay blew up")
    w = Worker(h.deps())

    async def engine_for(d: date) -> FakeEngine:
        eng = await Harness.engine_for(h, d)
        eng.poll_error = RuntimeError("quotes blew up")
        return eng

    w.deps = WorkerDeps(**{**vars(h.deps()), "engine_for": engine_for})
    report = await w.step()
    assert [k for k, _, _ in h.fire_calls] == ["orb_open"]
    eng = h.engines[0]
    assert len(eng.polls) == 1 and len(eng.ticks) == 1 and len(h.relay_calls) == 1
    assert report.fired == [] and report.fills == 0 and report.relayed is False
    events = _events(db_factory)
    assert [e.level for e in events] == ["error"] * 3
    joined = " ".join(e.message for e in events)
    assert "fire" in joined and "poll_quotes" in joined and "relay" in joined


async def test_ten_consecutive_failed_steps_log_one_critical_event(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(SAT, 11, 0))
    h = Harness(db_factory, clock)
    h.relay_error = RuntimeError("relay down")
    w = Worker(h.deps())
    for _ in range(15):
        await w.step()
        clock.advance(timedelta(seconds=30))
    levels = [e.level for e in _events(db_factory)]
    assert levels.count("critical") == 1 and levels.count("error") == 15


# 6 ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("start", [et(SAT, 11, 0), et(TUE, 10, 0)], ids=["idle", "session"])
async def test_heartbeat_written_at_start_kept_fresh_and_marked_stopped(
    db_factory: sessionmaker[Session], start: datetime
) -> None:
    clock = FixedClock(start)
    vt = VirtualTime(clock)
    beat_gaps: list[float] = []
    first_seen: list[str] = []

    async def sleep(seconds: float) -> None:
        await vt.sleep(seconds)
        hb = _heartbeat(db_factory)
        assert hb is not None
        beat_gaps.append((clock.now() - hb.beat_at).total_seconds())

    h = Harness(db_factory, clock, sleep=sleep)
    stop = asyncio.Event()

    def on_relay() -> None:
        if not first_seen:
            hb = _heartbeat(db_factory)
            first_seen.append(hb.phase if hb else "missing")
        if clock.now() - start >= timedelta(seconds=120):
            stop.set()

    h.on_relay = on_relay
    await _run(Worker(h.deps()), stop, vt)
    assert first_seen == ["starting"]
    assert len(beat_gaps) >= 8 and max(beat_gaps) <= h.settings.worker_heartbeat_seconds
    hb = _heartbeat(db_factory)
    assert hb is not None
    assert (hb.phase, hb.pid, hb.host, hb.started_at) == ("stopped", os.getpid(), "test-host", start)
    assert hb.beat_at == clock.now()
    assert set(hb.detail) == {"fills_today", "last_event"}


# 7 ---------------------------------------------------------------------------------------------------------


async def test_restart_mid_session_does_not_refire_and_keeps_polling(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    h = Harness(db_factory, clock)
    h.settled[TUE] = {"orb_open"}  # what the database says the first worker already did
    w = Worker(h.deps())
    reports = await _steps(w, clock, et(TUE, 10, 0, 10), 2)
    assert h.fire_calls == []
    assert len(h.engines) == 1 and len(h.engines[0].polls) == len(h.engines[0].ticks) == 6
    assert all(r.phase == "open" and r.relayed for r in reports)


# 8 ---------------------------------------------------------------------------------------------------------


async def test_second_worker_is_refused_and_exits_with_code_2(
    db_factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    first = acquire_single_instance(migrated_engine)
    assert first is not None
    try:
        assert acquire_single_instance(migrated_engine) is None
        clock = FixedClock(et(TUE, 10, 0))
        h = Harness(db_factory, clock)
        with pytest.raises(SystemExit) as exc:
            await Worker(h.deps()).run(asyncio.Event())
        assert exc.value.code == 2
        assert h.engines == [] and h.relay_calls == [] and _heartbeat(db_factory) is None
    finally:
        release_single_instance(first)
    again = acquire_single_instance(migrated_engine)  # released: a new worker can start
    assert again is not None
    release_single_instance(again)


# 9 ---------------------------------------------------------------------------------------------------------


async def test_crashing_bot_is_restarted_after_30s_without_disturbing_the_quote_loop(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    bot_calls: list[datetime] = []

    async def bot(stop: asyncio.Event) -> None:
        bot_calls.append(clock.now())
        if len(bot_calls) == 1:
            raise RuntimeError("bot crashed")
        await stop.wait()

    h = Harness(db_factory, clock, sleep=vt.sleep, bot=bot)
    stop = asyncio.Event()
    h.on_relay = lambda: stop.set() if len(h.relay_calls) >= 25 else None
    await _run(Worker(h.deps()), stop, vt)
    start = et(TUE, 10, 0)
    assert bot_calls == [start, start + timedelta(seconds=30)]
    assert _gaps(h.relay_calls) == {2.0} and len(h.relay_calls) == 25
    assert any("bot" in e.message for e in _events(db_factory))


# 10 --------------------------------------------------------------------------------------------------------


async def test_early_close_day_ends_the_session_at_13_00_et(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(EARLY, 12, 59, 50))
    h = Harness(db_factory, clock)
    h.settled[EARLY] = {"orb_open", "entry_cancel", "overlay_decision", "flatten"}
    w = Worker(h.deps())
    reports = await _steps(w, clock, et(EARLY, 13, 0, 10), 2)
    assert [r.phase for r in reports] == ["open"] * 5 + ["after_close"] * 6
    assert h.end_calls == [(EARLY, et(EARLY, 13, 0))]


# 11 --------------------------------------------------------------------------------------------------------


def test_main_calls_run_worker_and_returns_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.runtime

    seen: list[bool] = []

    async def fake_run_worker(once: bool = False) -> int:
        seen.append(once)
        return 0 if once else 3

    monkeypatch.setattr(trader.runtime, "run_worker", fake_run_worker)
    assert main(["--once"]) == 0
    assert main([]) == 3
    assert seen == [True, False]


def test_main_returns_the_code_of_a_refused_second_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.runtime

    async def refused(once: bool = False) -> int:
        raise SystemExit(2)

    monkeypatch.setattr(trader.runtime, "run_worker", refused)
    assert main(["--once"]) == 2


async def test_once_mode_runs_one_step_and_never_starts_the_bot(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    bot_calls: list[datetime] = []

    async def bot(stop: asyncio.Event) -> None:
        bot_calls.append(clock.now())

    h = Harness(db_factory, clock, sleep=vt.sleep, bot=bot)
    await _run(Worker(h.deps()), asyncio.Event(), vt, once=True)
    assert bot_calls == [] and len(h.relay_calls) == 1 and len(h.engines[0].polls) == 1
    hb = _heartbeat(db_factory)
    assert hb is not None and hb.phase == "stopped"
    again = acquire_single_instance(db_factory.kw["bind"])  # the once run released the lock
    assert again is not None
    release_single_instance(again)


def test_module_is_runnable_as_python_dash_m() -> None:
    src = worker_mod.__file__
    assert src is not None
    with open(src) as f:
        assert 'if __name__ == "__main__":\n    raise SystemExit(main())' in f.read()
