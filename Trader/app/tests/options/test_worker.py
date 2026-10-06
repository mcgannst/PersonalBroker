"""The options worker (OPTSIM task plan T12): the step's parts and their cadences, fills and messages,
alerts, the heartbeat, snapshots, the single-instance lock and a restart, on the T1 fakes and a stepped
clock."""

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_telegram import RecordingNotifier
from tests.options.factories import (
    EXPIRY,
    SESSION,
    T0,
    add_contract,
    add_options_run,
    add_underlying,
    make_request,
)
from tests.options.fakes import FakeOptionBroker, FakeOptionMarket, RecordingHost
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs.runner import JobOutcome
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.option_strategies.base import OptionEvent
from trader.options.messages import OptionMessages
from trader.options.settings import OptionSettings
from trader.options.types import OptionAccountState
from trader.options.worker import (
    EVENT_SOURCE,
    HEARTBEAT_PROCESS,
    LOCK_NAME,
    OptionsWorker,
    OptionsWorkerDeps,
    acquire_single_instance,
    release_single_instance,
    run_event_cli,
)

pytestmark = pytest.mark.db

SATURDAY = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
CLOSE = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)  # 16:00 ET
EARLY_CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # the day after Thanksgiving closes at 13:00 ET
TOY = ("toy", OptionEvent("toy_buy", SESSION))
IDLE_PARTS = ("deliver_answers", "sync_prompts", "send_prompts")


class StickyHost(RecordingHost):
    """Like the real host, an event stays due until its run succeeded."""

    async def fire(self, strategy_key: str, event: OptionEvent, *, force: bool = False) -> JobOutcome:
        due = list(self.due)
        outcome = await super().fire(strategy_key, event, force=force)
        if outcome.status != "succeeded":
            self.due = due
        return outcome


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def send_due(self, now: datetime) -> int:
        self.calls.append(now)
        return 0


class Sleeper:
    """A sleep that moves the clock instead of waiting; `hook(n)` runs after the n-th sleep."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.calls: list[float] = []
        self.hook: Callable[[int], None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))
        if self.hook is not None:
            self.hook(len(self.calls))
        await asyncio.sleep(0)


class Harness:
    """The fakes of one options world: F at 15 with one put (contract 1, strike 14.50, 0.45 / 0.50), stored
    in the database too so marks can be written for it."""

    def __init__(self, factory: sessionmaker[Session], at: datetime = T0, *, run: bool = True) -> None:
        self.factory = factory
        self.clock = FixedClock(at)
        self.run_id: int | None = None
        with session_scope(factory) as s:
            if run:
                self.run_id = add_options_run(s)
            contract_id = add_contract(s, add_underlying(s, "F"))
        self.market = FakeOptionMarket(self.clock)
        self.market.add_underlying("F", "15")
        self.put = self.market.add_contract("F", EXPIRY, "14.50", "put", bid="0.45", ask="0.50")
        assert self.put.id == contract_id
        self.broker = FakeOptionBroker(self.market)
        self.host = StickyHost()
        self.notifier = RecordingNotifier()
        self.sender = FakeSender()
        self.sleep = Sleeper(self.clock)
        self.settings = OptionSettings()
        self.marks: list[tuple[int, ...]] = []
        self.underlying_price: Decimal | None = None  # stored with each mark when set

    async def record_marks(self, contract_ids: Sequence[int]) -> int:
        self.marks.append(tuple(contract_ids))
        if self.underlying_price is not None:
            with session_scope(self.factory) as s:
                for cid in contract_ids:
                    s.merge(
                        m.OptionQuoteMark(
                            contract_id=cid,
                            fetched_at=self.clock.now(),
                            is_halted=False,
                            underlying_price=self.underlying_price,
                        )
                    )
        return len(contract_ids)

    def worker(self) -> OptionsWorker:
        """A new worker on the same world (a restart)."""
        engine = self.factory.kw["bind"]
        assert isinstance(engine, Engine)
        return OptionsWorker(
            OptionsWorkerDeps(
                factory=self.factory,
                engine=engine,
                clock=self.clock,
                calendar=SessionCalendar(),
                settings=lambda: self.settings,
                run_id=self.run_id,
                broker=self.broker,
                record_marks=self.record_marks,
                host=self.host,
                prompt_sender=self.sender,
                notifier=self.notifier,
                renderer=OptionMessages("https://trader.example"),
                sleep=self.sleep,
            )
        )

    async def sell_put(self, *, fill: bool = True) -> int:
        """A manual order selling the put at 0.45; filled at once unless `fill` is False."""
        order = (await self.broker.submit(make_request())).order
        if fill:
            self.broker.fill(order.id, {1: Decimal("0.45")})
        return order.id

    async def steps(self, w: OptionsWorker, count: int, every: float) -> None:
        for n in range(count):
            if n:
                self.clock.advance(timedelta(seconds=every))
            await w.step()

    def events(self, level: str | None = None) -> list[m.EventLog]:
        with self.factory() as s:
            rows = s.execute(
                select(m.EventLog).where(m.EventLog.source == EVENT_SOURCE).order_by(m.EventLog.id)
            ).scalars()
            return [row for row in rows if level is None or row.level == level]

    def heartbeat(self) -> m.WorkerHeartbeat | None:
        with self.factory() as s:
            return s.get(m.WorkerHeartbeat, HEARTBEAT_PROCESS)

    def snapshots(self) -> list[m.EquitySnapshot]:
        with self.factory() as s:
            return list(s.execute(select(m.EquitySnapshot).order_by(m.EquitySnapshot.ts)).scalars())

    def host_calls(self) -> list[str]:
        return [name for name, _ in self.host.calls]


def _raises(*_: Any, **__: Any) -> Any:
    raise RuntimeError("boom")


# --- the run and the lock -----------------------------------------------------------------------------------


async def test_idle_without_an_options_run(db_factory: sessionmaker[Session]) -> None:
    h = Harness(db_factory, run=False)
    w = h.worker()
    report = await w.step()
    assert (report.phase, report.parts) == ("idle", ("ensure_defaults",))
    assert h.broker.calls == [] and h.host_calls() == ["ensure_defaults"] and h.sender.calls == []
    beat = h.heartbeat()
    assert beat is not None and beat.phase == "idle"
    with session_scope(db_factory) as s:  # a run appears: the worker asks for a restart on it (exit 4)
        add_options_run(s)
    await w.step()
    assert w._exit_code == 4 and h.broker.calls == []


async def test_second_worker_gets_exit_2(db_factory: sessionmaker[Session], migrated_engine: Engine) -> None:
    assert LOCK_NAME == "trader.options_worker"
    h = Harness(db_factory)
    first = acquire_single_instance(migrated_engine)
    assert first is not None
    try:
        with pytest.raises(SystemExit) as exit_info:
            await h.worker().run(asyncio.Event())
    finally:
        release_single_instance(first)
    assert exit_info.value.code == 2
    assert h.sleep.calls == [30.0] and h.host.calls == [] and h.heartbeat() is None


def _take_lock_when_free(engine: Engine) -> Connection:
    """PostgreSQL drops a terminated backend's locks shortly after: poll (bounded, real time)."""
    deadline = time.monotonic() + 10
    while True:
        conn = acquire_single_instance(engine)
        if conn is not None:
            return conn
        assert time.monotonic() < deadline, "the terminated backend's lock was never released"
        time.sleep(0.05)


async def test_lost_lock_stops_with_exit_3(
    db_factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    h = Harness(db_factory)
    w = h.worker()
    taken: list[Connection] = []

    def rival(sleeps: int) -> None:
        if sleeps != 2:
            return
        assert w._lock is not None
        pid = w._lock.execute(text("SELECT pg_backend_pid()")).scalar_one()
        with migrated_engine.connect() as admin:
            admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            admin.commit()
        taken.append(_take_lock_when_free(migrated_engine))

    h.sleep.hook = rival
    try:
        with pytest.raises(SystemExit) as exit_info:
            await asyncio.wait_for(w.run(asyncio.Event()), timeout=30)
    finally:
        for conn in taken:
            release_single_instance(conn)
    assert exit_info.value.code == 3
    assert h.broker.calls.count("poll") == 2  # the third step saw the lock gone and did nothing
    critical = h.events("critical")
    assert len(critical) == 1 and "lost its single-instance lock" in critical[0].message
    beat = h.heartbeat()
    assert beat is not None and beat.phase == "session"  # never `stopped` over the new owner's row


# --- the session step ---------------------------------------------------------------------------------------


async def test_session_step_order(db_factory: sessionmaker[Session]) -> None:
    h = Harness(db_factory)
    h.host.due = [TOY]
    await h.sell_put(fill=False)
    h.broker.fill_on_poll = True
    h.broker.calls.clear()
    report = await h.worker().step()
    assert report.phase == "session"
    assert report.parts == (
        "ensure_defaults",
        "due_events",
        "fire",
        "poll",
        "fill_message",
        "deliver_fill",
        "walk",
        "structures",
        "record_marks",
        "take_profits",
        "strike_touch",
        "snapshot",
        *IDLE_PARTS,
    )
    assert report.events == (("toy", "toy_buy", "succeeded"),) and report.fills == 1
    assert h.host_calls() == [
        "ensure_defaults",
        "due_events",
        "fire",
        "deliver_fill",
        "deliver_answers",
        "sync_prompts",
    ]
    assert h.broker.calls == ["poll", "walk", "take_profits"]
    assert h.marks == [(h.put.id,)] and h.sender.calls == [T0]


async def test_fill_is_notified_once_and_delivered(db_factory: sessionmaker[Session]) -> None:
    h = Harness(db_factory)
    order_id = await h.sell_put(fill=False)
    h.broker.fill_on_poll = True
    w = h.worker()
    await h.steps(w, 3, 5)
    assert [(msg.kind, msg.dedupe_key) for msg in h.notifier.sent] == [("fill", f"opt:fill:{order_id}")]
    assert "F 2026-11-20" in h.notifier.sent[0].text  # the contract label comes from the structure
    assert [fill.order_id for fill in h.host.fills] == [order_id]
    beat = h.heartbeat()
    assert beat is not None and beat.detail["fills_today"] == 1


async def test_walk_and_marks_follow_their_intervals(db_factory: sessionmaker[Session]) -> None:
    h = Harness(db_factory)
    await h.sell_put()
    h.broker.calls.clear()
    await h.steps(h.worker(), 26, 5)  # 0 s to 125 s
    assert h.broker.calls.count("poll") == 26
    assert h.broker.calls.count("walk") == 3  # options.reprice_seconds = 60: at 0, 60 and 120 s
    assert h.broker.calls.count("take_profits") == 3 and h.marks == [(h.put.id,)] * 3  # mark_seconds = 60


async def test_take_profit_and_strike_touch_once_per_session(db_factory: sessionmaker[Session]) -> None:
    h = Harness(db_factory)
    await h.sell_put()
    h.broker.calls.clear()
    h.underlying_price = Decimal("15")
    w = h.worker()
    await h.steps(w, 2, 60)
    assert h.notifier.sent == [] and h.events("warning") == []  # above the short put's strike
    h.underlying_price = Decimal("14.50")
    await h.steps(w, 4, 60)
    assert h.broker.calls.count("take_profits") == 5  # every mark pass
    alerts = [msg for msg in h.notifier.sent if msg.kind == "alert"]
    assert len(alerts) == 1 and alerts[0].dedupe_key == f"opt:alert:strike_touched:1:{SESSION}"
    assert "STRIKE TOUCHED" in alerts[0].text.upper() and "14.5" in alerts[0].text
    (warning,) = h.events("warning")
    assert warning.data["structure_id"] == 1 and warning.data["underlying"] == "F"
    assert warning.run_id == h.run_id
    h.clock.set(T0 + timedelta(days=1))  # the next session: once more
    await w.step()
    assert len(h.notifier.sent) == 2
    h.settings = OptionSettings.model_validate({"options.strike_touch_alerts": False})
    h.clock.set(T0 + timedelta(days=2))
    await w.step()
    assert len(h.notifier.sent) == 2


async def test_answers_and_prompts_run_outside_the_session(db_factory: sessionmaker[Session]) -> None:
    h = Harness(db_factory, SATURDAY)
    w = h.worker()
    report = await w.step()
    assert (report.phase, report.parts) == ("idle", ("ensure_defaults", *IDLE_PARTS))
    assert h.host_calls() == ["ensure_defaults", "deliver_answers", "sync_prompts"]
    assert h.sender.calls == [SATURDAY] and h.broker.calls == []
    assert w._interval() == 30.0
    h.clock.set(datetime(2026, 10, 12, 13, 29, 50, tzinfo=UTC))  # Monday, 10 s before the open
    await w.step()
    assert w._interval() == 10.0  # on time for the open


@pytest.mark.parametrize("close", [CLOSE, EARLY_CLOSE], ids=["normal", "early_close"])
async def test_day_orders_expire_at_the_close_including_early_close(
    db_factory: sessionmaker[Session], close: datetime
) -> None:
    h = Harness(db_factory, close - timedelta(minutes=1))
    order_id = await h.sell_put(fill=False)
    h.broker.calls.clear()
    w = h.worker()
    report = await w.step()
    assert report.phase == "session" and "expire_day_orders" not in h.broker.calls
    h.clock.set(close)
    report = await w.step()
    assert report.phase == "idle" and report.parts == ("expire_day_orders", *IDLE_PARTS)
    order = await h.broker.order(order_id)
    assert order is not None and order.status == "expired"
    await h.steps(w, 3, 30)
    assert h.broker.calls.count("expire_day_orders") == 1 and h.broker.calls.count("poll") == 1
    beat = h.heartbeat()
    assert beat is not None and (beat.phase, beat.session_date) == ("idle", close.date())


# --- failures -----------------------------------------------------------------------------------------------


async def test_a_failing_part_does_not_stop_the_others(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(db_factory)
    await h.sell_put()
    h.host.due = [TOY]
    monkeypatch.setattr(h.broker, "poll", _raises)
    monkeypatch.setattr(h.host, "deliver_answers", _raises)
    monkeypatch.setattr(h, "record_marks", _raises)
    w = h.worker()
    report = await w.step()
    assert "walk" in h.broker.calls and "take_profits" in h.broker.calls
    assert h.host.fired and h.sender.calls == [T0] and "sync_prompts" in h.host_calls()
    assert report.parts[-4:] == ("snapshot", *IDLE_PARTS) and len(h.snapshots()) == 1
    errors = h.events("error")
    assert sorted(e.data["part"] for e in errors) == ["deliver_answers", "poll", "record_marks"]
    assert all("RuntimeError: boom" in e.message for e in errors)
    beat = h.heartbeat()
    assert beat is not None and beat.phase == "session"


async def test_ten_failures_raise_one_critical_event(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(db_factory)
    w = h.worker()
    with monkeypatch.context() as patch:
        patch.setattr(h.broker, "poll", _raises)
        await h.steps(w, 12, 5)
    assert [e.level for e in h.events()] == ["error", "critical"]
    assert "failed 10 times in a row" in h.events("critical")[0].message
    h.clock.advance(timedelta(seconds=5))
    await w.step()
    assert h.events()[-1].message == "options worker poll recovered after 12 failures"


async def test_a_failing_event_is_retried_with_back_off(db_factory: sessionmaker[Session]) -> None:
    h = Harness(db_factory)
    h.host.due = [TOY]
    h.host.outcome = JobOutcome("failed", error="boom")
    w = h.worker()
    await h.steps(w, 61, 5)  # 0 s to 300 s: offered at 0, 60 and 180 s, not on every poll
    assert len(h.host.fired) == 3 and h.host_calls().count("due_events") == 61
    assert h.events() == []  # the host records a failed run itself
    h.host.outcome = JobOutcome("succeeded", {})
    h.clock.set(T0 + timedelta(seconds=420))
    report = await w.step()
    assert report.events == (("toy", "toy_buy", "succeeded"),) and h.host.due == []


# --- heartbeat and snapshots --------------------------------------------------------------------------------


async def test_heartbeat_row_and_phases(db_factory: sessionmaker[Session], migrated_engine: Engine) -> None:
    h = Harness(db_factory)
    w = h.worker()
    await w.step()
    beat = h.heartbeat()
    assert beat is not None and beat.process == "options-worker"
    assert (beat.phase, beat.session_date, beat.beat_at) == ("session", SESSION, T0)
    assert beat.detail == {"run_id": h.run_id, "fills_today": 0, "last_event": None}
    assert w._interval() == 5.0
    h.clock.set(SATURDAY)
    await w.step()
    beat = h.heartbeat()
    assert beat is not None and (beat.phase, beat.session_date, beat.beat_at) == ("idle", None, SATURDAY)
    await h.worker().run(asyncio.Event(), once=True)
    beat = h.heartbeat()
    assert beat is not None and (beat.phase, beat.started_at) == ("stopped", SATURDAY)
    again = acquire_single_instance(migrated_engine)  # the once run released the lock
    assert again is not None
    release_single_instance(again)


async def test_snapshot_spacing_peak_and_drawdown(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(db_factory)
    equities = [Decimal(5000), Decimal(5500), Decimal(4950)]

    async def account() -> OptionAccountState:
        equity = equities.pop(0)
        return OptionAccountState(
            cash=Decimal(4800),
            reserved=Decimal(0),
            free_cash=Decimal(4800),
            positions_value=equity - 4800,
            equity=equity,
            premium_collected=Decimal(0),
            as_of=h.clock.now(),
            marks_complete=True,
        )

    monkeypatch.setattr(h.broker, "account", account)
    await h.steps(h.worker(), 11, 60)  # 0 to 600 s; options.snapshot_seconds = 300
    rows = h.snapshots()
    assert [(r.ts - T0).total_seconds() for r in rows] == [0, 300, 600]
    assert [(r.equity, r.peak_equity, r.drawdown_pct) for r in rows] == [
        (Decimal(5000), Decimal(5000), Decimal(0)),
        (Decimal(5500), Decimal(5500), Decimal(0)),
        (Decimal(4950), Decimal(5500), Decimal("0.1")),
    ]
    assert all(r.run_id == h.run_id and r.cash == r.settled_cash == Decimal(4800) for r in rows)


# --- a restart ----------------------------------------------------------------------------------------------


async def test_restart_mid_session_continues_without_duplicates(db_factory: sessionmaker[Session]) -> None:
    h = Harness(db_factory)
    h.host.due = [TOY]
    first_order = await h.sell_put(fill=False)
    h.broker.fill_on_poll = True
    await h.steps(h.worker(), 2, 5)  # fills the first order, fires the event
    second_order = await h.sell_put(fill=False)  # submitted while no worker runs
    h.clock.advance(timedelta(seconds=5))
    await h.steps(h.worker(), 2, 5)  # the restart
    await h.steps(h.worker(), 2, 5)  # and another
    assert [msg.dedupe_key for msg in h.notifier.sent] == [
        f"opt:fill:{first_order}",
        f"opt:fill:{second_order}",
    ]
    assert [fill.order_id for fill in h.host.fills] == [first_order, second_order]
    assert len(h.host.fired) == 1  # the event ran once
    assert len(h.snapshots()) == 1  # the spacing is read from the table, not from memory
    assert h.host_calls().count("ensure_defaults") == 3 and len(h.marks) == 3  # timers restart as due
    assert h.events() == []


# --- the command line ---------------------------------------------------------------------------------------


async def test_run_event_cli_due_and_named(capsys: pytest.CaptureFixture[str]) -> None:
    host = RecordingHost()
    other = ("toy", OptionEvent("toy_sell", SESSION))
    host.due = [TOY, other]

    async def cli(**kw: Any) -> int:
        args: dict[str, Any] = {"strategy": None, "key": None, "force": False, "due": False, **kw}
        return await run_event_cli(host, session_date=SESSION, now=T0, **args)

    assert await cli(due=True) == 0
    assert host.fired == [(*TOY, False), (*other, False)]
    assert await cli(due=True) == 0 and "nothing is due" in capsys.readouterr().out
    host.fired.clear()
    assert await cli(strategy="toy", key="toy_buy", force=True) == 0
    assert host.fired == [("toy", OptionEvent("toy_buy", SESSION, scheduled=False), True)]
    host.outcome = JobOutcome("failed", error="boom")
    assert await cli(strategy="toy", key="toy_buy") == 1
    assert "failed: boom" in capsys.readouterr().out
    assert await cli() == 2


def test_dates_used_are_sessions() -> None:
    cal = SessionCalendar()
    assert (
        cal.session_close(date(2026, 10, 6)) == CLOSE and cal.session_close(date(2026, 11, 27)) == EARLY_CLOSE
    )
