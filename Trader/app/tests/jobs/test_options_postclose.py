"""OPTSIM T13: the options post-close job (`trader.jobs.options_postclose`), over the T1 fakes, the real
lifecycle engine on `FakeBook`, and a fixed clock at 16:20 ET on an expiry day.

The book in most tests: 5,000 cash; a short 14.50 put on F expiring today (source `alpha`, F closes at 15, so
it expires worthless for +45); a long 16 call expiring in December, bid 0.40 (so the account value is
5,040)."""

import dataclasses
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_telegram import RecordingNotifier
from tests.options.factories import (
    EXPIRY,
    add_contract,
    add_options_run,
    add_position,
    add_structure,
    add_underlying,
)
from tests.options.fakes import (
    FakeBook,
    FakeOptionBroker,
    FakeOptionMarket,
    FakePromptStore,
    FakeRegistry,
    RecordingHost,
)
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs.options_postclose import (
    POSTCLOSE_JOB,
    Lifecycle,
    OfficialClose,
    PostcloseDeps,
    official_close,
    postclose_job,
)
from trader.jobs.runner import JobOutcome, RetryPolicy
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle
from trader.notify.types import OutboundMessage
from trader.option_strategies.base import OptionEvent
from trader.options.lifecycle import LifecycleEngine
from trader.options.messages import OptionMessages
from trader.options.settings import OptionSettings
from trader.options.types import LifecycleEvent, OptionAccountState, OptionContract

db = pytest.mark.db

CAL = SessionCalendar()
SESSION = EXPIRY  # Fri 2026-11-20
NOW = datetime(2026, 11, 20, 21, 20, tzinfo=UTC)  # 16:20 ET
CLOSE = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)  # the session close
LATER = date(2026, 12, 18)
D = Decimal


class _Params(BaseModel):
    pass


def plugin(key: str) -> type[Any]:
    return type(key, (), {"key": key, "version": "1", "params_model": _Params})


def bar(day: date, close: str) -> Candle:
    start = datetime.combine(day, time(0), tzinfo=ET).astimezone(UTC)
    price = D(close)
    return Candle(start, start + timedelta(days=1), price, price, price, price, 1000, None)


class Market(FakeOptionMarket):
    """The fake market plus `record_marks`; `no_quote` takes the live share quote away."""

    def __init__(self, clock: FixedClock, steps: list[str]) -> None:
        super().__init__(clock)
        self.steps = steps
        self.no_quote = False
        self.marked: list[tuple[int, ...]] = []

    async def underlying_quote(self, underlying: str) -> QtQuote | None:
        return None if self.no_quote else await super().underlying_quote(underlying)

    async def refresh_chain(self, underlying: str, *, force: bool = False) -> int:
        return 0

    async def record_marks(self, contract_ids: Sequence[int]) -> int:
        self.steps.append("record_marks")
        self.marked.append(tuple(contract_ids))
        return len(contract_ids)


class Broker(FakeOptionBroker):
    steps: list[str]

    async def expire_day_orders(self, session_date: date, now: datetime) -> int:
        self.steps.append("expire_day_orders")
        return await super().expire_day_orders(session_date, now)

    async def account(self) -> OptionAccountState:
        self.steps.append("account")
        return await super().account()


class Engine(LifecycleEngine):
    steps: list[str]

    async def run_expiry(self, session_date: date) -> list[LifecycleEvent]:
        self.steps.append("run_expiry")
        return await super().run_expiry(session_date)

    async def run_early_assignment(self, session_date: date) -> list[LifecycleEvent]:
        self.steps.append("run_early_assignment")
        return await super().run_early_assignment(session_date)

    async def check_adjustments(self) -> list[LifecycleEvent]:
        self.steps.append("check_adjustments")
        return await super().check_adjustments()


class Host(RecordingHost):
    def __init__(self, world: "World") -> None:
        super().__init__()
        self.world = world
        self.snapshots_at_fire: list[int] = []

    async def deliver_lifecycle(self, event: LifecycleEvent) -> None:
        self.world.steps.append("deliver_lifecycle")
        await super().deliver_lifecycle(event)

    async def fire(self, strategy_key: str, event: OptionEvent, *, force: bool = False) -> JobOutcome:
        self.world.steps.append(f"fire:{strategy_key}")
        self.snapshots_at_fire.append(len(self.world.snapshots()))
        return await super().fire(strategy_key, event, force=force)

    async def sync_prompts(self) -> int:
        self.world.steps.append("sync_prompts")
        return await super().sync_prompts()


class Notifier(RecordingNotifier):
    def __init__(self, steps: list[str]) -> None:
        super().__init__()
        self.steps = steps

    async def send(self, msg: OutboundMessage) -> None:
        self.steps.append(f"send:{(msg.dedupe_key or '').rsplit(':', 1)[0]}")
        await super().send(msg)


class Sender:
    def __init__(self, steps: list[str]) -> None:
        self.steps = steps

    async def send_due(self, now: datetime) -> int:
        self.steps.append("send_due")
        return 0


class World:
    def __init__(
        self, factory: sessionmaker[Session], *, plugins: Sequence[str] = ("alpha",), run: bool = True
    ) -> None:
        self.factory = factory
        self.steps: list[str] = []
        self.clock = FixedClock(NOW)
        self.run_id: int | None = None
        if run:
            with session_scope(factory) as s:
                self.run_id = add_options_run(s)
        self.market = Market(self.clock, self.steps)
        self.market.add_underlying("F", "15")
        self.book = FakeBook("5000", self.market.contracts)
        self.broker = Broker(self.market, self.book)
        self.broker.steps = self.steps
        self.registry = FakeRegistry(self.clock, *[plugin(key) for key in plugins])
        self.host = Host(self)
        self.notifier = Notifier(self.steps)
        self.deps = PostcloseDeps(
            factory=factory,
            clock=self.clock,
            calendar=CAL,
            settings=OptionSettings,
            run_id=lambda: self.run_id,
            broker=self.broker,
            market=self.market,
            host=self.host,
            registry=self.registry,
            prompts=FakePromptStore(self.clock),
            prompt_sender=Sender(self.steps),
            notifier=self.notifier,
            renderer=OptionMessages("https://trader.example"),
            lifecycle=self._engine,
        )

    def _engine(self, run_id: int, settings: OptionSettings, close: OfficialClose) -> Lifecycle:
        engine = Engine(
            self.factory, self.clock, CAL, self.market, settings, run_id, lambda s: self.book, close
        )
        engine.steps = self.steps
        return engine

    def structure(self, kind: str, contract: OptionContract, qty: int, price: str, reserved: str) -> int:
        sid = self.book.add_structure(
            kind=kind,  # type: ignore[arg-type]
            source="alpha",
            strategy_config_id=1,
            underlying="F",
            qty=1,
            entry_net=D(0),
            reserved_cash=D(reserved),
            cover_structure_id=None,
            parent_structure_id=None,
            take_profit_net=None,
            meta={},
            ts=NOW,
        )
        self.book.apply(sid, "option", contract.id, qty, D(price), NOW)
        return sid

    def positions(self) -> tuple[int, int]:
        """The usual book: returns (the expiring short put's structure, the long call's)."""
        put = self.market.add_contract("F", EXPIRY, "14.50", "put")
        call = self.market.add_contract("F", LATER, "16", "call", bid="0.40", ask="0.50")
        return (
            self.structure("csp", put, -1, "0.45", "1450"),
            self.structure("long_call", call, 1, "0.50", "0"),
        )

    def snapshots(self) -> list[m.EquitySnapshot]:
        with self.factory() as s:
            return list(s.execute(select(m.EquitySnapshot).order_by(m.EquitySnapshot.ts)).scalars())

    def job_rows(self) -> list[tuple[str, str, str | None]]:
        with self.factory() as s:
            rows = s.execute(select(m.JobRun).order_by(m.JobRun.id)).scalars()
            return [(r.job, r.status, r.error) for r in rows]

    def sent(self, prefix: str) -> list[OutboundMessage]:
        return [msg for msg in self.notifier.sent if (msg.dedupe_key or "").startswith(prefix)]

    def state(self) -> Any:
        """Everything a repeated run must leave alone."""
        return (
            self.book.cash(),
            self.book.structures(open_only=False),
            list(self.book.lifecycle),
            [(r.ts, r.equity, r.cash, r.peak_equity, r.drawdown_pct) for r in self.snapshots()],
            list(self.notifier.sent),
            list(self.host.lifecycle),
        )


@db
async def test_postclose_step_order(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory)
    w.positions()

    outcome = await postclose_job(w.deps, SESSION, force=False)

    assert outcome.status == "succeeded", outcome.error
    assert w.steps == [
        "expire_day_orders",
        "run_expiry",
        "run_early_assignment",
        "check_adjustments",
        "send:opt:life",
        "deliver_lifecycle",
        "record_marks",
        "account",
        "fire:alpha",
        "sync_prompts",
        "send_due",
        "send:opt:summary",
    ]
    assert w.host.snapshots_at_fire == [1]  # the snapshot is there when the plug-ins' event runs
    assert outcome.detail == {
        "expired_orders": 0,
        "lifecycle": 1,
        "marks": 1,
        "equity": "5040.00",
        "marks_complete": True,
        "events": {"alpha": "succeeded"},
        "prompts_synced": 0,
        "prompts_sent": 0,
        "summary": "sent",
        "missing_closes": [],
    }
    assert w.job_rows() == [(POSTCLOSE_JOB, "succeeded", None)]


@db
async def test_postclose_twice_changes_nothing_more(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory)
    put, call = w.positions()
    await postclose_job(w.deps, SESSION, force=False)
    assert w.book.structure(put).close_reason == "expired"
    assert w.book.structure(call).state == "open"
    before = w.state()

    again = await postclose_job(w.deps, SESSION, force=False)
    assert (again.status, again.detail) == ("skipped", {"reason": "already succeeded"})
    forced = await postclose_job(w.deps, SESSION, force=True)

    assert forced.status == "succeeded"
    assert forced.detail["lifecycle"] == 0
    assert w.state() == before


async def test_official_close_prefers_regular_last_then_candle() -> None:
    clock = FixedClock(NOW)
    market = Market(clock, [])
    market.add_underlying("F", "15.10")
    previous = CAL.previous_session(SESSION)
    market.set_bars("F", [bar(previous, "14.20"), bar(SESSION, "15.00")])

    assert await official_close(market, CAL, clock, "F", SESSION) == D("15.10")  # the regular-hours last
    assert await official_close(market, CAL, clock, "F", previous) == D("14.20")  # an earlier day: its candle
    market.no_quote = True
    assert await official_close(market, CAL, clock, "F", SESSION) == D("15.00")  # no quote: today's candle
    market.set_bars("F", [bar(previous, "14.20")])
    assert await official_close(market, CAL, clock, "F", SESSION) is None  # neither: not known yet
    market.no_quote = False
    assert await official_close(market, CAL, clock, "F", date(2026, 11, 21)) is None  # not a session
    clock.set(CLOSE - timedelta(minutes=1))
    assert await official_close(market, CAL, clock, "F", SESSION) is None  # the session is still open


@db
async def test_lifecycle_events_are_messaged_and_delivered_once(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory)
    w.positions()

    await postclose_job(w.deps, SESSION, force=False)
    await postclose_job(w.deps, SESSION, force=True)

    (event,) = w.host.lifecycle
    assert (event.kind, event.source, event.underlying_close) == ("expired", "alpha", D("15"))
    (message,) = w.sent("opt:life:")
    assert message.dedupe_key == f"opt:life:{event.id}"
    assert "EXPIRED" in message.text


@db
async def test_undelivered_events_are_read_back_from_the_database(db_factory: sessionmaker[Session]) -> None:
    """What an earlier run settled but never delivered (a crash, a failing hook) goes out on the next run."""
    w = World(db_factory)
    assert w.run_id is not None
    with session_scope(db_factory) as s:
        symbol = add_underlying(s)
        contract = add_contract(s, symbol)
        structure = add_structure(s, w.run_id, symbol, source="alpha", state="closed")
        position = add_position(s, w.run_id, structure, symbol, contract_id=contract, qty=0)
        ids = []
        for kind, day, delivered_at in (("expired", date(2026, 11, 19), NOW), ("assigned", SESSION, None)):
            row = m.OptLifecycleEvent(
                run_id=w.run_id,
                structure_id=structure,
                position_id=position,
                contract_id=contract,
                kind=kind,
                session_date=day,
                ts=NOW,
                underlying_close=D("14"),
                strike=D("14.50"),
                qty=1,
                shares_delta=100,
                cash_delta=D("-1450"),
                delivered_at=delivered_at,
            )
            s.add(row)
            s.flush()
            ids.append(row.id)

    outcome = await postclose_job(w.deps, SESSION, force=False)

    assert outcome.status == "succeeded", outcome.error
    (event,) = w.host.lifecycle  # only the undelivered one
    assert (event.id, event.kind, event.source, event.shares_delta) == (ids[1], "assigned", "alpha", 100)
    assert event.contract is not None and event.contract.strike == D("14.50")
    assert [msg.dedupe_key for msg in w.sent("opt:life:")] == [f"opt:life:{ids[1]}"]
    (summary,) = w.sent("opt:summary:")
    assert "ASSIGNED" in summary.text  # the session's events are in the summary


@db
async def test_snapshot_written_at_session_close(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory)
    w.positions()
    assert w.run_id is not None
    with session_scope(db_factory) as s:  # an earlier high: the peak the drawdown is measured from
        s.add(
            m.EquitySnapshot(
                run_id=w.run_id,
                ts=CLOSE - timedelta(days=1),
                equity=D("5600"),
                cash=D("5600"),
                settled_cash=D("5600"),
                peak_equity=D("5600"),
                drawdown_pct=D("0"),
            )
        )

    await postclose_job(w.deps, SESSION, force=False)

    _, row = w.snapshots()
    assert row.ts == CLOSE == CAL.session_close(SESSION)
    assert (row.equity, row.cash, row.settled_cash) == (D("5040"), D("5000"), D("5000"))  # liquidation marks
    assert (row.peak_equity, row.drawdown_pct) == (D("5600"), D("0.1"))
    assert w.market.marked == [(2,)]  # the open contract's end-of-day mark
    (summary,) = w.sent("opt:summary:")
    assert "day change -$560.00" in summary.text


@db
async def test_summary_sent_once_per_day(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory)
    w.positions()

    await postclose_job(w.deps, SESSION, force=False)
    (summary,) = w.sent("opt:summary:")
    assert summary.kind == "daily_summary"
    assert summary.dedupe_key == f"opt:summary:{SESSION.isoformat()}"
    assert summary.text.startswith("<b>Options account value $5,040.00</b>")
    assert "day change +$40.00" in summary.text  # from the starting cash
    assert "Realized today: alpha +$45.00" in summary.text

    with session_scope(db_factory) as s:  # what the real notifier records
        s.add(
            m.Notification(
                kind="daily_summary",
                dedupe_key=summary.dedupe_key,
                text=summary.text,
                created_at=NOW,
                status="sent",
            )
        )
    forced = await postclose_job(w.deps, SESSION, force=True)

    assert forced.detail["summary"] == "duplicate"
    assert len(w.sent("opt:summary:")) == 1
    assert w.steps.count("send:opt:summary") == 1  # not even handed to the notifier again


@db
async def test_missing_close_fails_the_job_after_other_steps(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory)
    put, _ = w.positions()
    w.market.no_quote = True  # and no daily candle either: F has no official close yet
    seen: list[Any] = []

    async def sleep(seconds: float) -> None:  # between the attempts the close arrives
        seen.append((seconds, w.book.structure(put).state, list(w.steps), len(w.snapshots())))
        w.market.no_quote = False

    deps = dataclasses.replace(w.deps, retry=RetryPolicy(attempts=2, first_delay_s=30), sleep=sleep)
    outcome = await postclose_job(deps, SESSION, force=False)

    ((waited, state, first_attempt, snapshots),) = seen
    assert (waited, state, snapshots) == (30, "open", 1)  # nothing changed for the structure
    assert first_attempt[-5:] == ["account", "fire:alpha", "sync_prompts", "send_due", "send:opt:summary"]
    assert outcome.status == "succeeded"
    assert outcome.detail["attempts"] == 2
    assert w.job_rows() == [
        (POSTCLOSE_JOB, "failed", "PostcloseIncomplete: no official close for F on 2026-11-20"),
        (POSTCLOSE_JOB, "succeeded", None),
    ]
    assert w.book.structure(put).close_reason == "expired"
    assert len(w.host.lifecycle) == 1
    assert len(w.sent("opt:summary:")) == 1  # the retry does not send a second summary


@db
async def test_postclose_event_reaches_every_enabled_plugin(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory, plugins=("alpha", "beta", "gamma"))
    w.registry.update("beta", enabled=False, actor="test")

    outcome = await postclose_job(w.deps, SESSION, force=False)

    event = OptionEvent("postclose", SESSION, scheduled=False)
    assert w.host.fired == [("alpha", event, False), ("gamma", event, False)]
    assert outcome.detail["events"] == {"alpha": "succeeded", "gamma": "succeeded"}

    w.host.outcome = JobOutcome("failed", error="boom")  # a plug-in whose event failed fails the job
    failed = await postclose_job(w.deps, SESSION, force=True)
    assert failed.status == "failed"
    assert failed.error == (
        "PostcloseIncomplete: alpha: the post-close event failed; gamma: the post-close event failed"
    )


@db
async def test_not_a_session_and_no_run_are_skips(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory)
    saturday = await postclose_job(w.deps, date(2026, 11, 21), force=True)
    assert (saturday.status, saturday.detail) == ("skipped", {"reason": "not a session"})

    w.clock.set(CLOSE - timedelta(minutes=1))  # run by hand before the close: nothing may expire yet
    early = await postclose_job(w.deps, SESSION, force=True)
    assert (early.status, early.detail) == ("skipped", {"reason": "the session has not closed"})

    no_run = World(db_factory, run=False)
    outcome = await postclose_job(no_run.deps, SESSION, force=False)
    assert (outcome.status, outcome.detail) == ("skipped", {"reason": "no options run"})

    assert w.steps == [] and no_run.steps == []
    assert w.job_rows() == []
