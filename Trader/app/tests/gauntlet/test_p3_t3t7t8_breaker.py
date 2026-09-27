"""P3-B1 gauntlet: try to break the session event scheduler (P3-T3), the Telegram commands (P3-T7) and the
notification relay (P3-T8).

Fakes only (FakeRenderer, FakeIssuer, FakeMessenger, RecordingNotifier and small wrappers below) and the
testcontainers database. Never real Telegram or Questrade.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_telegram import FakeIssuer, FakeMessenger, FakeRenderer, RecordingNotifier
from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.telegram.commands import UNKNOWN, CommandDeps, Commands
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches
from trader.engine.runs import get_live_run
from trader.engine.scheduler import (
    DayPlan,
    EventRunner,
    FireDeps,
    FireResult,
    day_plan,
    due_events,
    event_job,
    fire_event,
    fired_keys,
    report_plan_problems,
)
from trader.jobs import runner as runner_mod
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.notify.relay import NotificationRelay
from trader.notify.types import AlertView, OutboundMessage
from trader.settings_store import RuntimeSettings
from trader.strategies.base import ScheduledEvent, SessionOffset, Strategy
from trader.strategies.orb_sip import OrbSip
from trader.strategies.spy_overlay import SpyOverlay

pytestmark = pytest.mark.db

CAL = SessionCalendar()
SETTINGS = RuntimeSettings()
TUE = date(2026, 10, 6)
WED = date(2026, 10, 7)
EARLY = date(2026, 11, 27)  # day after Thanksgiving, 13:00 ET close
XMAS_EVE = date(2026, 12, 24)  # 13:00 ET close
FALL_MON = date(2026, 11, 2)  # first session after the clocks fell back (Sun 2026-11-01)
FALL_FRI = date(2026, 10, 30)
SPRING_FRI = date(2027, 3, 12)
SPRING_MON = date(2027, 3, 15)  # first session after the clocks sprang forward (Sun 2027-03-14)
CHAT = 4242
BOT_TOKEN = "123456789:AAFakeBreakerTokenDoNotUse_x9"  # not a real token: only checked for leaks


def utc(d: date, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, mi, s, tzinfo=UTC)


# ======================================================================================================
# Scheduler harness
# ======================================================================================================


@dataclass
class FakeStrategy:
    key: str
    events: list[ScheduledEvent]

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return self.events


@dataclass
class CountingRunner:
    calls: list[tuple[str, date]] = field(default_factory=list)
    gate: asyncio.Event | None = None  # when set, run_event waits for it
    started: asyncio.Event = field(default_factory=asyncio.Event)
    after: Any = None  # called at the end of every run_event

    async def run_event(self, event_key: str, session_date: date) -> Any:
        self.calls.append((event_key, session_date))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.after is not None:
            self.after()
        return SimpleNamespace(strategies=["orb_sip"], outcomes=[])


@dataclass
class Sched:
    factory: sessionmaker[Session]
    clock: FixedClock
    strategies: list[Strategy]
    runner: CountingRunner = field(default_factory=CountingRunner)
    built: int = 0

    async def _build(self) -> EventRunner:
        self.built += 1
        return self.runner

    @property
    def deps(self) -> FireDeps:
        return FireDeps(
            factory=self.factory,
            clock=self.clock,
            calendar=CAL,
            settings=lambda: SETTINGS,
            plan=lambda d: day_plan(self.strategies, CAL, d, SETTINGS),
            runner=self._build,
        )

    async def fire(self, key: str, d: date, *, force: bool = False) -> FireResult:
        return await fire_event(self.deps, key, d, force=force)

    async def worker_round(self, d: date) -> list[FireResult]:
        deps = self.deps
        plan = deps.plan(d)
        # Fix round 1 harness adaptation: day_plan stays pure, and the worker step reports the plan's
        # problems through the DB-aware report_plan_problems (fire_event also calls it).
        report_plan_problems(self.factory, self.clock, plan)
        due = due_events(plan, self.clock.now(), fired_keys(self.factory, d))
        return [await fire_event(deps, ev.key, d) for ev in due]


def sched(factory: sessionmaker[Session], at: datetime, strategies: list[Strategy] | None = None) -> Sched:
    return Sched(factory, FixedClock(at), strategies if strategies is not None else [OrbSip(), SpyOverlay()])


def job_rows(factory: sessionmaker[Session], job: str | None = None) -> list[tuple[str, str, str | None]]:
    with factory() as s:
        q = select(m.JobRun.job, m.JobRun.status, m.JobRun.error).order_by(m.JobRun.id)
        if job is not None:
            q = q.where(m.JobRun.job == job)
        return [(a, b, c) for a, b, c in s.execute(q).all()]


def alert_rows(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(
            s.execute(
                select(m.EventLog).where(m.EventLog.level.in_(("error", "critical"))).order_by(m.EventLog.id)
            ).scalars()
        )


# --- S1: worker and cron backup racing in two processes; a settled event is never re-fired ------------
async def test_worker_and_cron_backup_race_across_threads_runs_once_and_never_refires(
    db_factory: sessionmaker[Session],
) -> None:
    """The worker fires orb_open at 09:35:05 and is still running it when the 09:36 cron backup starts in
    another process (here: another thread with its own event loop and DB connection). Exactly one engine
    run; the backup is skipped; later worker rounds over the whole day never run it again."""
    worker = sched(db_factory, utc(TUE, 13, 35, 5))
    worker.runner.gate = asyncio.Event()
    cron = Sched(db_factory, worker.clock, worker.strategies)

    task = asyncio.create_task(worker.fire("orb_open", TUE))
    await worker.runner.started.wait()
    worker.clock.set(utc(TUE, 13, 36, 0))
    backup = await asyncio.to_thread(lambda: asyncio.run(cron.fire("orb_open", TUE)))
    worker.runner.gate.set()
    first = await task

    assert first.status == "fired"
    assert backup.status == "skipped" and backup.detail == {"reason": "already running"}
    assert cron.built == 0 and cron.runner.calls == []

    # the backup again after the worker finished, then the worker's rounds through the rest of the day
    assert (await cron.fire("orb_open", TUE)).status == "skipped"
    for t in (utc(TUE, 13, 37), utc(TUE, 15, 31), utc(TUE, 19, 31), utc(TUE, 19, 51), utc(TUE, 20, 5)):
        worker.clock.set(t)
        await worker.worker_round(TUE)
    assert [k for k, _ in worker.runner.calls].count("orb_open") == 1
    assert cron.runner.calls == []
    assert job_rows(db_factory, event_job("orb_open")) == [("event:orb_open", "succeeded", None)]


# --- S2: the grace boundary ----------------------------------------------------------------------------
async def test_grace_boundary_120s_fires_121s_is_missed(db_factory: sessionmaker[Session]) -> None:
    on_time = sched(db_factory, utc(TUE, 13, 37, 5))  # orb_open at 13:35:05Z + exactly 120 s
    assert (await on_time.fire("orb_open", TUE)).status == "fired"
    assert len(on_time.runner.calls) == 1

    late = sched(db_factory, utc(WED, 13, 37, 6))  # + 121 s
    out = await late.fire("orb_open", WED)
    assert out.status == "missed" and out.detail["late_seconds"] == 121
    assert late.built == 0
    assert job_rows(db_factory, event_job("orb_open"))[-1] == (
        "event:orb_open",
        "failed",
        "missed: 121s late",
    )


# --- S3: safety events late but before the close fire; at or after the close never ------------------
async def test_safety_event_fires_late_until_the_early_close_and_never_after(
    db_factory: sessionmaker[Session],
) -> None:
    # 2026-11-27 closes 18:00Z; flatten is at 17:50Z. One second before the close it still fires.
    before = sched(db_factory, utc(EARLY, 17, 59, 59))
    assert (await before.fire("flatten", EARLY)).status == "fired"
    assert len(before.runner.calls) == 1

    # 2026-12-24 also closes 18:00Z. At the close and after, flatten (and every other key) is missed,
    # the engine is never built, and each key is recorded once however many rounds run.
    after = sched(db_factory, utc(XMAS_EVE, 18, 0, 0))
    out = await after.fire("flatten", XMAS_EVE)
    assert out.status == "missed"
    for t in (utc(XMAS_EVE, 18, 0, 2), utc(XMAS_EVE, 18, 5), utc(XMAS_EVE, 20, 55)):
        after.clock.set(t)
        results = await after.worker_round(XMAS_EVE)
        assert all(r.status == "missed" for r in results)
    assert after.built == 0 and after.runner.calls == []
    flatten_rows = job_rows(db_factory, event_job("flatten"))
    assert [r[1] for r in flatten_rows] == ["succeeded", "failed"]  # EARLY fired, XMAS_EVE missed once
    assert (flatten_rows[1][2] or "").startswith("missed:") and "after the close" in (
        flatten_rows[1][2] or ""
    )
    with db_factory() as s:
        xmas = s.execute(select(m.JobRun).where(m.JobRun.session_date == XMAS_EVE)).scalars().all()
    assert sorted(r.job for r in xmas) == sorted(
        event_job(k) for k in ("orb_open", "entry_cancel", "overlay_decision", "flatten")
    )


# --- S4: the DST change days -----------------------------------------------------------------------------
async def test_dst_change_days_follow_the_real_open(db_factory: sessionmaker[Session]) -> None:
    def at(d: date) -> dict[str, datetime]:
        return {e.key: e.at for e in day_plan([OrbSip(), SpyOverlay()], CAL, d, SETTINGS).events}

    assert at(FALL_FRI)["orb_open"] == utc(FALL_FRI, 13, 35, 5)
    assert at(FALL_MON)["orb_open"] == utc(FALL_MON, 14, 35, 5)
    assert at(FALL_MON)["flatten"] == utc(FALL_MON, 20, 50)
    assert at(SPRING_FRI)["orb_open"] == utc(SPRING_FRI, 14, 35, 5)
    assert at(SPRING_MON)["orb_open"] == utc(SPRING_MON, 13, 35, 5)
    assert at(SPRING_MON)["flatten"] == utc(SPRING_MON, 19, 50)

    # on the Monday after the fall change, the old (EDT) time is an hour early and must not fire
    h = sched(db_factory, utc(FALL_MON, 13, 35, 5))
    assert (await h.fire("orb_open", FALL_MON)).status == "too_early"
    assert await h.worker_round(FALL_MON) == []
    h.clock.set(utc(FALL_MON, 14, 35, 5))
    assert (await h.fire("orb_open", FALL_MON)).status == "fired"
    assert h.runner.calls == [("orb_open", FALL_MON)]


# --- S5: a job whose success can't be recorded -----------------------------------------------------------
async def test_event_whose_success_cannot_be_recorded_is_alerted_and_not_rerun(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine ran orb_open (entries placed) but the `succeeded` update hit a transient DB error. The
    run ends cleanly as `failed` (P1-REVIEW should-fix 4). What SHOULD follow: Stephen hears about it
    (an error/critical event_log row, which the relay turns into an alert), and the worker's next step
    does not run the entry event a second time (the body already succeeded)."""
    real = runner_mod.session_scope
    state = {"fail": False}

    @contextmanager
    def flaky(factory: sessionmaker[Session]) -> Iterator[Session]:
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("server closed the connection unexpectedly")
        with real(factory) as s:
            yield s

    monkeypatch.setattr(runner_mod, "session_scope", flaky)
    h = sched(db_factory, utc(TUE, 13, 35, 5))
    h.runner.after = lambda: state.update(fail=len(h.runner.calls) == 1)

    out = await h.fire("orb_open", TUE)
    assert out.status == "failed" and "could not be recorded" in str(out.detail.get("error"))

    problems: list[str] = []
    if not alert_rows(db_factory):
        problems.append("no error/critical event_log row: the relay never alerts Stephen")
    for _ in range(3):  # the worker's next steps, still inside the 120 s grace
        h.clock.advance(timedelta(seconds=2))
        await h.worker_round(TUE)
    if len(h.runner.calls) != 1:
        problems.append(
            f"orb_open ran {len(h.runner.calls)} times: the unrecorded success left a `running` row, "
            "fired_keys ignores it, and the next round marks it abandoned and runs the entry event again"
        )
    assert not problems, "; ".join(problems)


# --- S6: known deviation: day_plan's conflict / over-long key errors never reach event_log -------------
async def test_day_plan_key_conflict_and_long_key_alert_stephen_once(
    db_factory: sessionmaker[Session],
) -> None:
    """Plan T3 says a shared key at different times, and a key over 39 characters, each produce ONE
    `error` event (source `scheduler`), which the relay turns into an alert Stephen sees. The build logs
    them to structlog only, so nothing reaches event_log and no alert fires. Driven the way the system
    drives it: the plan is built on every worker round (10 rounds), then the relay pumps."""
    strategies: list[Strategy] = [
        FakeStrategy("late_one", [ScheduledEvent("flatten", SessionOffset.parse("close-10m"))]),
        FakeStrategy("early_one", [ScheduledEvent("flatten", SessionOffset.parse("close-15m"))]),
        FakeStrategy("wordy", [ScheduledEvent("k" * 40, SessionOffset.parse("open+1m"))]),
    ]
    with session_scope(db_factory) as s:
        run_id = add_run(s)
    notifier, render = RecordingNotifier(), FakeRenderer()
    relay = NotificationRelay(
        db_factory,
        FixedClock(utc(TUE, 19, 0)),
        notifier,
        render,
        FakeMessenger(),
        run_id,
        settings=lambda: SETTINGS,
    )
    await relay.pump()  # cursors start here

    h = sched(db_factory, utc(TUE, 19, 0), strategies)
    for _ in range(10):
        h.clock.advance(timedelta(seconds=2))
        await h.worker_round(TUE)
    await relay.pump()

    with db_factory() as s:
        rows = s.execute(select(m.EventLog).where(m.EventLog.source == "scheduler")).scalars().all()
    alerts = [c[1][0] for c in render.calls if c[0] == "alert" and c[1][0].source == "scheduler"]
    assert len([r for r in rows if r.level == "error"]) == 2, (
        "one error event per plan problem, not per round"
    )
    assert len(alerts) == 2, "Stephen should see one alert for the conflict and one for the long key"


# ======================================================================================================
# Commands (P3-T7)
# ======================================================================================================


@dataclass
class CmdEnv:
    factory: sessionmaker[Session]
    clock: FixedClock
    run_id: int
    renderer: FakeRenderer
    issuer: FakeIssuer
    messenger: FakeMessenger
    killswitches: KillSwitches

    def deps(self) -> CommandDeps:
        def plan(d: date) -> DayPlan:
            return day_plan([OrbSip(), SpyOverlay()], CAL, d, SETTINGS)

        return CommandDeps(
            factory=self.factory,
            clock=self.clock,
            calendar=CAL,
            settings=lambda: SETTINGS,
            killswitches=self.killswitches,
            run_id=self.run_id,
            chat_id=CHAT,
            plan=plan,
            fired=lambda d: set(),
            token_health=lambda: TokenHealth(
                seeded=True,
                expires_at=self.clock.now() + timedelta(minutes=20),
                last_refresh_at=self.clock.now() - timedelta(hours=1),
                last_error=None,
            ),
            quotes=None,
            messenger=self.messenger,
            issuer=self.issuer,
            render=self.renderer,
        )


@pytest.fixture
def cmd_env(db_factory: sessionmaker[Session]) -> CmdEnv:
    clock = FixedClock(utc(TUE, 14, 0))
    run = get_live_run(db_factory, clock, SETTINGS)
    return CmdEnv(
        db_factory,
        clock,
        run.id,
        FakeRenderer(),
        FakeIssuer(),
        FakeMessenger(),
        KillSwitches(db_factory, clock),
    )


def audit_actions(factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    with factory() as s:
        return [(a.action, a.actor) for a in s.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars()]


# --- C1: /pause racing a stale confirm button, then /resume with automatic switches tripped ---------------
async def test_pause_double_confirm_race_and_resume_never_resets_automatic_switches(cmd_env: CmdEnv) -> None:
    e = cmd_env
    cmds = Commands(e.deps())
    await cmds.handle("/pause")
    await cmds.handle("/pause")  # a second prompt while the first is still on screen: two live buttons
    assert len(e.issuer.issued) == 2
    for issued in e.issuer.issued:  # bound to the configured chat, and expiring
        assert issued["chat_id"] == CHAT and issued["ttl_seconds"] == SETTINGS.telegram_confirm_ttl_seconds

    # both "Yes" buttons tapped at once, delivered to two handlers on two DB connections
    def tap() -> str:
        return asyncio.run(Commands(e.deps()).confirm_pause("y"))

    replies = await asyncio.gather(asyncio.to_thread(tap), asyncio.to_thread(tap))
    assert sorted(replies) == ["Already paused.", "Paused: new entries are blocked."]
    assert audit_actions(e.factory) == [("killswitch.pause", "telegram")]
    assert e.killswitches.blocking(e.run_id, TUE) == "manual_pause"

    # automatic switches trip while paused
    with session_scope(e.factory) as s:
        for switch in ("max_drawdown_pct", "daily_loss_pct"):
            s.add(
                m.KillSwitchEvent(
                    run_id=e.run_id,
                    switch=switch,
                    session_date=TUE,
                    tripped_at=e.clock.now(),
                    value=Decimal("0.2"),
                    threshold=Decimal("0.1"),
                )
            )
    await cmds.handle("/resume")
    first = e.renderer.calls[-1][1][0]
    assert first.startswith("Manual pause lifted.")
    assert "max_drawdown_pct" in first and "daily_loss_pct" in first
    await cmds.handle("/resume")
    assert e.renderer.calls[-1] == ("reply", ("Not paused.",))
    assert {a.switch for a in e.killswitches.active(e.run_id, TUE)} == {"max_drawdown_pct", "daily_loss_pct"}
    assert e.killswitches.blocking(e.run_id, TUE) is not None
    assert audit_actions(e.factory) == [("killswitch.pause", "telegram"), ("killswitch.resume", "telegram")]


# --- C2: unknown, garbage and oversized input --------------------------------------------------------------
async def test_garbage_unknown_and_oversized_commands_are_harmless(cmd_env: CmdEnv) -> None:
    e = cmd_env
    cmds = Commands(e.deps())
    garbage = [
        "",
        "   \n\t ",
        "/",
        "/@",
        "@StephenTraderDevBot",
        "status",
        "/nonsense",
        "/pausee",
        "/ pause",
        "\x00/pause",
        "‮/pause",
        "/pa​use",
        "/ѕtatus",  # Cyrillic dze, looks like /status
        "/" + "a" * 200_000,
        "x" * 1_000_000,
        "<b>/pause</b>",
    ]
    for text in garbage:
        out = await cmds.handle(text)
        assert len(out) == 1, repr(text[:40])
        assert e.renderer.calls[-1] == ("reply", (UNKNOWN,)), repr(text[:40])  # never echoes the input
    assert e.issuer.issued == [] and e.messenger.calls == []
    assert audit_actions(e.factory) == []
    assert e.killswitches.active(e.run_id, TUE) == []

    # the recognised forms still work: case, a bot suffix, trailing words and a huge tail
    await cmds.handle("/HELP@StephenTraderDevBot")
    assert e.renderer.calls[-1][0] == "help"
    await cmds.handle("/resume " + "z" * 100_000)
    assert e.renderer.calls[-1] == ("reply", ("Not paused.",))


# ======================================================================================================
# Relay (P3-T8)
# ======================================================================================================


@dataclass
class RelayWorld:
    factory: sessionmaker[Session]
    clock: FixedClock
    run_id: int
    symbol_id: int
    config_id: int
    render: FakeRenderer
    messenger: FakeMessenger
    settings: RuntimeSettings

    def relay(self, notifier: Any, render: FakeRenderer | None = None) -> NotificationRelay:
        return NotificationRelay(
            self.factory,
            self.clock,
            notifier,
            render or self.render,
            self.messenger,
            self.run_id,
            settings=lambda: self.settings,
        )

    def add_event(self, message: str, level: str = "error", source: str = "killswitch") -> int:
        with session_scope(self.factory) as s:
            row = m.EventLog(
                ts=self.clock.now(),
                level=level,
                source=source,
                run_id=self.run_id,
                message=message,
                data={"n": message},
            )
            s.add(row)
            s.flush()
            return row.id

    def add_fill(self) -> int:
        with session_scope(self.factory) as s:
            o = m.Order(
                run_id=self.run_id,
                symbol_id=self.symbol_id,
                strategy_config_id=self.config_id,
                position_id=None,
                side="buy",
                order_type="stop",
                purpose="entry",
                qty=10,
                stop_price=Decimal("21.55"),
                stop_loss=Decimal("21.41"),
                tif="day",
                status="filled",
                reason="orb breakout",
                session_date=TUE,
                submitted_at=self.clock.now(),
                closed_at=self.clock.now(),
                stale_alerted=False,
            )
            s.add(o)
            s.flush()
            f = m.Fill(
                run_id=self.run_id,
                order_id=o.id,
                ts=self.clock.now(),
                qty=10,
                price=Decimal("21.56"),
                fees={"total": "0"},
                quote_snapshot={},
                slippage=Decimal("0"),
            )
            s.add(f)
            s.flush()
            return f.id


@pytest.fixture
def rw(db_factory: sessionmaker[Session]) -> RelayWorld:
    with session_scope(db_factory) as s:
        run_id = add_run(s)
        symbol_id = add_symbol(s, "AAA")
        config_id = add_strategy_config(s, "orb_sip")
    return RelayWorld(
        db_factory,
        FixedClock(utc(TUE, 14, 0)),
        run_id,
        symbol_id,
        config_id,
        FakeRenderer(),
        FakeMessenger(),
        RuntimeSettings(),
    )


class ProcessCrash(BaseException):
    """Stands in for the process dying (not an Exception, so nothing in pump() catches it)."""


@dataclass
class CrashAfterSend:
    """Delivers the message, then 'crashes' on the n-th send, before the relay can advance its cursor."""

    inner: RecordingNotifier
    n: int
    count: int = 0

    async def send(self, msg: OutboundMessage) -> None:
        self.count += 1
        await self.inner.send(msg)
        if self.count == self.n:
            raise ProcessCrash


@dataclass
class Yielding:
    """The real notifier awaits the network: yield so two concurrent relays interleave."""

    inner: RecordingNotifier

    async def send(self, msg: OutboundMessage) -> None:
        await asyncio.sleep(0)
        await self.inner.send(msg)


def row_keys(n: RecordingNotifier) -> list[str]:
    return sorted(msg.dedupe_key or "" for msg in n.sent)


# --- R1: crash between send and cursor advance, then two relays pumping at once -------------------------
async def test_crash_between_send_and_advance_then_two_relays_deliver_each_row_once(rw: RelayWorld) -> None:
    sent = RecordingNotifier()  # the notifier's dedupe store survives the restart (the notifications table)
    await rw.relay(sent).pump()  # cursors created at the current maximum
    fills = [rw.add_fill() for _ in range(3)]
    events = [rw.add_event(f"kill switch trip {i}") for i in range(3)]

    with pytest.raises(ProcessCrash):
        await rw.relay(CrashAfterSend(sent, n=2)).pump()  # fill 2 delivered, cursor still at fill 1

    shared = Yielding(sent)
    await asyncio.gather(rw.relay(shared).pump(), rw.relay(shared).pump())
    await rw.relay(shared).pump()

    expected = sorted([f"fill:{i}" for i in fills] + [f"event:{i}" for i in events])
    assert row_keys(sent) == expected  # every row once: none lost, none twice


# --- R2: a row whose id is lower than the cursor commits late (concurrent writers) -------------------------
async def test_row_committed_after_a_higher_id_is_still_relayed(rw: RelayWorld) -> None:
    """The engine's transaction takes event id N and is still open when a cron job inserts and commits N+1.
    The relay pumps in between and moves the cursor to N+1. When N commits, it must still be relayed:
    it is the kill-switch trip, and 'exactly once' includes 'at least once'."""
    sent = RecordingNotifier()
    await rw.relay(sent).pump()
    slow = rw.factory()
    try:
        slow_row = m.EventLog(
            ts=rw.clock.now(),
            level="error",
            source="killswitch",
            run_id=rw.run_id,
            message="max_drawdown_pct tripped",
            data={},
        )
        slow.add(slow_row)
        slow.flush()  # id assigned from the sequence, not committed yet
        fast_id = rw.add_event("job.nightly failed", source="job.nightly")
        assert fast_id > slow_row.id
        await rw.relay(sent).pump()
        slow.commit()
        slow_id = slow_row.id
    finally:
        slow.close()
    await rw.relay(sent).pump()
    await rw.relay(sent).pump()
    assert row_keys(sent) == sorted([f"event:{fast_id}", f"event:{slow_id}"])


# --- R3: Telegram 429/400 failures, a row that can't be rendered, no secrets in logs ---
@dataclass
class FlakyTelegram:
    """Fails like a Telegram client under 429 then 400, with the bot token in the exception text (httpx and
    PTB put it in the URL), then recovers."""

    inner: RecordingNotifier
    failures: list[str] = field(
        default_factory=lambda: [
            f"429 Too Many Requests: retry after 5 for url 'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'",
            f"400 Bad Request: can't parse entities, url https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        ]
    )

    async def send(self, msg: OutboundMessage) -> None:
        if self.failures:
            raise RuntimeError(self.failures.pop(0))
        await self.inner.send(msg)


class PoisonRenderer(FakeRenderer):
    """Can't format one particular alert (as a real renderer could fail on odd data)."""

    def alert(self, v: AlertView) -> OutboundMessage:
        if "poison" in v.message:
            raise ValueError("cannot render this row")
        return super().alert(v)


async def test_telegram_failures_and_a_poison_row_do_not_block_later_alerts_or_leak_the_token(
    rw: RelayWorld,
) -> None:
    sent = RecordingNotifier()
    flaky = FlakyTelegram(sent)
    render = PoisonRenderer()
    await rw.relay(flaky, render).pump()
    rw.add_fill()
    rw.add_event("daily_loss_pct tripped")
    rw.add_event("poison: a row the renderer chokes on")
    with structlog.testing.capture_logs() as logs:
        await rw.relay(flaky, render).pump()
        later = rw.add_event("max_drawdown_pct tripped")
        await rw.relay(flaky, render).pump()
        await rw.relay(flaky, render).pump()

    assert BOT_TOKEN not in repr(logs) and "AAFakeBreakerToken" not in repr(logs)
    keys = row_keys(sent)
    assert f"event:{later}" in keys, (
        "an alert recorded after an unrenderable row must still reach Stephen; the events step fails as a "
        "whole and its cursor never moves, so every later alert (kill switches included) is blocked"
    )


# --- R4: catch-up capping after an outage, on every stream at once ----------------------------------------
async def test_catch_up_after_an_outage_is_capped_per_stream_and_then_normal(rw: RelayWorld) -> None:
    rw.settings = RuntimeSettings.model_validate({"telegram.relay_catchup_max": 20})
    sent = RecordingNotifier()
    await rw.relay(sent).pump()
    fills = [rw.add_fill() for _ in range(30)]
    events = [rw.add_event(f"alert {i}") for i in range(30)]
    rw.add_event("info noise", level="info", source="engine")  # never relayed, but the cursor passes it

    report = await rw.relay(sent).pump()
    keys = [msg.dedupe_key or "" for msg in sent.sent]
    assert sorted(k for k in keys if k.startswith("fill:")) == sorted(f"fill:{i}" for i in fills[-20:])
    assert sorted(k for k in keys if k.startswith("event:")) == sorted(f"event:{i}" for i in events[-20:])
    summaries = [msg for msg in sent.sent if (msg.dedupe_key or "").startswith("relay:")]
    assert len(summaries) == 2
    summary_views = [c[1][0] for c in rw.render.calls if c[0] == "alert" and c[1][0].source == "relay"]
    assert sorted(v.data["stream"] for v in summary_views) == ["events", "fills"]
    assert all(v.data["skipped"] == 10 for v in summary_views)
    assert report.fills == 20 and report.events == 20 and report.skipped == 20

    before = len(sent.sent)
    again = await rw.relay(sent).pump()
    assert len(sent.sent) == before and again.skipped == 0

    new = rw.add_event("fresh alert")
    await rw.relay(sent).pump()
    assert [msg.dedupe_key for msg in sent.sent[before:]] == [f"event:{new}"]

    # a crash right after the summary went out repeats nothing when the relay comes back
    for _ in range(25):
        rw.add_fill()
    with pytest.raises(ProcessCrash):
        await rw.relay(CrashAfterSend(sent, n=1)).pump()
    count = len(sent.sent)
    await rw.relay(sent).pump()
    fresh = [msg.dedupe_key or "" for msg in sent.sent[count:]]
    assert len([k for k in fresh if k.startswith("fill:")]) == 20
    assert not any(k.startswith("relay:") for k in fresh)
