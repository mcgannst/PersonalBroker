"""Session event scheduler (P3-T3): day plans, due events, and firing each (event, session) once."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from trader.db import models as m
from trader.db.models import EventLog, JobRun
from trader.engine.scheduler import (
    DayPlan,
    EventRunner,
    FireDeps,
    FireResult,
    PlannedEvent,
    day_plan,
    due_events,
    event_job,
    fire_event,
    fired_keys,
    live_day_plan,
    report_plan_problems,
    retry_backoff,
)
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings
from trader.strategies.base import ScheduledEvent, SessionOffset, Strategy
from trader.strategies.orb_sip import OrbSip, OrbSipParams
from trader.strategies.registry import StrategyRegistry
from trader.strategies.spy_overlay import SpyOverlay

ET = ZoneInfo("America/New_York")
CAL = SessionCalendar()
TUE = date(2026, 10, 6)
EARLY = date(2026, 11, 27)  # the day after Thanksgiving, 13:00 ET close
THANKSGIVING = date(2026, 11, 26)
SETTINGS = RuntimeSettings()


def et(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=ET)


def utc(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=UTC)


def default_strategies() -> list[Strategy]:
    return [OrbSip(), SpyOverlay()]


@dataclass
class FakeStrategy:
    key: str
    events: list[ScheduledEvent]

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return self.events


@dataclass
class FakeRunner:
    calls: list[tuple[str, date]] = field(default_factory=list)
    fail: bool = False

    async def run_event(self, event_key: str, session_date: date) -> Any:
        self.calls.append((event_key, session_date))
        await asyncio.sleep(0)  # let a concurrent fire_event interleave
        if self.fail:
            raise RuntimeError("engine blew up")
        return SimpleNamespace(event_key=event_key, strategies=["orb_sip"], outcomes=["a", "b"])


@dataclass
class Harness:
    factory: sessionmaker[Session]
    clock: FixedClock
    strategies: list[Strategy]
    runner: FakeRunner = field(default_factory=FakeRunner)
    built: int = 0

    async def build_runner(self) -> EventRunner:
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
            runner=self.build_runner,
        )

    async def fire(self, key: str, d: date, *, force: bool = False) -> FireResult:
        return await fire_event(self.deps, key, d, force=force)

    async def worker_round(self, d: date) -> list[FireResult]:
        """One worker step: fire every due, unsettled event."""
        deps = self.deps
        due = due_events(deps.plan(d), self.clock.now(), fired_keys(self.factory, d))
        return [await fire_event(deps, ev.key, d) for ev in due]


def harness(
    factory: sessionmaker[Session], at: datetime, strategies: list[Strategy] | None = None
) -> Harness:
    return Harness(factory, FixedClock(at), strategies if strategies is not None else default_strategies())


def job_rows(factory: sessionmaker[Session]) -> list[tuple[str, str, str | None]]:
    with factory() as s:
        rows = s.execute(select(JobRun.job, JobRun.status, JobRun.error).order_by(JobRun.id)).all()
    return [(r[0], r[1], r[2]) for r in rows]


def error_events(factory: sessionmaker[Session]) -> list[EventLog]:
    with factory() as s:
        return list(
            s.execute(select(EventLog).where(EventLog.level == "error").order_by(EventLog.id)).scalars()
        )


# --- pure: plans and due events ----------------------------------------------------------------


def test_event_job_name() -> None:
    assert event_job("orb_open") == "event:orb_open"


def test_1_day_plan_normal_day_and_early_close() -> None:
    plan = day_plan(default_strategies(), CAL, TUE, SETTINGS)
    assert plan.is_session and plan.open == utc(TUE, 13, 30) and plan.close == utc(TUE, 20, 0)
    assert [(e.key, e.at) for e in plan.events] == [
        ("orb_open", utc(TUE, 13, 35, 5)),
        ("entry_cancel", utc(TUE, 15, 30)),
        ("overlay_decision", utc(TUE, 19, 30)),
        ("flatten", utc(TUE, 19, 50)),
    ]
    assert {e.key: e.always_fire_late for e in plan.events} == {
        "orb_open": False,
        "entry_cancel": True,
        "overlay_decision": True,
        "flatten": True,
    }
    assert plan.events[0].strategies == ("orb_sip",)
    assert plan.events[2].strategies == ("spy_overlay",)

    early = day_plan(default_strategies(), CAL, EARLY, SETTINGS)
    at = {e.key: e.at for e in early.events}
    assert at["overlay_decision"] == utc(EARLY, 17, 30) and at["flatten"] == utc(EARLY, 17, 50)
    assert early.close == utc(EARLY, 18, 0)


def test_2_thanksgiving_plan_is_empty() -> None:
    plan = day_plan(default_strategies(), CAL, THANKSGIVING, SETTINGS)
    assert plan == DayPlan(THANKSGIVING, False, None, None, ())


def test_always_fire_late_follows_settings() -> None:
    s = RuntimeSettings.model_validate({"scheduler.always_fire_late": ["orb_open"]})
    plan = day_plan(default_strategies(), CAL, TUE, s)
    assert [e.key for e in plan.events if e.always_fire_late] == ["orb_open"]


def test_events_sorted_by_time_then_key() -> None:
    same = SessionOffset.parse("open+10m")
    strategies: list[Strategy] = [
        FakeStrategy(
            "b", [ScheduledEvent("zeta", same), ScheduledEvent("alpha", SessionOffset.parse("open+20m"))]
        ),
        FakeStrategy("a", [ScheduledEvent("beta", same)]),
    ]
    plan = day_plan(strategies, CAL, TUE, SETTINGS)
    assert [e.key for e in plan.events] == ["beta", "zeta", "alpha"]


def test_9_shared_key_at_different_times_takes_the_earliest_and_logs_one_error() -> None:
    strategies: list[Strategy] = [
        FakeStrategy("late_one", [ScheduledEvent("flatten", SessionOffset.parse("close-10m"))]),
        FakeStrategy("early_one", [ScheduledEvent("flatten", SessionOffset.parse("close-15m"))]),
    ]
    with structlog.testing.capture_logs() as logs:
        plan = day_plan(strategies, CAL, TUE, SETTINGS)
    assert plan.events == (PlannedEvent("flatten", utc(TUE, 19, 45), ("late_one", "early_one"), True),)
    errors = [e for e in logs if e["log_level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["source"] == "scheduler"
    assert "late_one" in str(errors[0]) and "early_one" in str(errors[0])


def test_shared_key_at_the_same_time_is_not_an_error() -> None:
    at = SessionOffset.parse("close-10m")
    strategies: list[Strategy] = [
        FakeStrategy("one", [ScheduledEvent("flatten", at)]),
        FakeStrategy("two", [ScheduledEvent("flatten", at)]),
    ]
    with structlog.testing.capture_logs() as logs:
        plan = day_plan(strategies, CAL, TUE, SETTINGS)
    assert [e.strategies for e in plan.events] == [("one", "two")]
    assert [e for e in logs if e["log_level"] == "error"] == []


def test_too_long_key_is_left_out_with_one_error() -> None:
    long_key = "k" * 40
    strategies: list[Strategy] = [
        FakeStrategy("s", [ScheduledEvent(long_key, SessionOffset.parse("open+1m"))]),
        FakeStrategy("t", [ScheduledEvent("k" * 39, SessionOffset.parse("open+2m"))]),
    ]
    with structlog.testing.capture_logs() as logs:
        plan = day_plan(strategies, CAL, TUE, SETTINGS)
    assert [e.key for e in plan.events] == ["k" * 39]
    assert len([e for e in logs if e["log_level"] == "error"]) == 1


def test_due_events_are_unfired_and_not_in_the_future() -> None:
    plan = day_plan(default_strategies(), CAL, TUE, SETTINGS)
    assert due_events(plan, et(TUE, 9, 35, 4), set()) == []
    assert [e.key for e in due_events(plan, et(TUE, 9, 35, 5), set())] == ["orb_open"]
    assert [e.key for e in due_events(plan, et(TUE, 15, 55), {"orb_open"})] == [
        "entry_cancel",
        "overlay_decision",
        "flatten",
    ]
    assert due_events(DayPlan(THANKSGIVING, False, None, None, ()), et(THANKSGIVING, 12, 0), set()) == []


# --- firing (real DB) --------------------------------------------------------------------------

pytestdb = pytest.mark.db


@pytestdb
async def test_2_fire_on_a_holiday_is_not_session_and_writes_nothing(
    db_factory: sessionmaker[Session],
) -> None:
    h = harness(db_factory, et(THANKSGIVING, 9, 36))
    out = await h.fire("orb_open", THANKSGIVING)
    assert out.status == "not_session"
    assert job_rows(db_factory) == [] and error_events(db_factory) == [] and h.built == 0


@pytestdb
async def test_unknown_key_is_not_scheduled(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 10, 0))
    assert (await h.fire("nope", TUE)).status == "not_scheduled"
    assert job_rows(db_factory) == [] and h.built == 0


@pytestdb
async def test_3_fires_once_then_skips(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 35, 5))
    first = await h.fire("orb_open", TUE)
    assert first.status == "fired"
    assert first.detail == {"strategies": ["orb_sip"], "outcomes": 2}
    assert h.runner.calls == [("orb_open", TUE)]
    assert job_rows(db_factory) == [("event:orb_open", "succeeded", None)]
    second = await h.fire("orb_open", TUE)
    assert second.status == "skipped" and second.detail == {"reason": "already succeeded"}
    assert len(h.runner.calls) == 1 and h.built == 1


@pytestdb
async def test_4_concurrent_fires_run_once(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 35, 5))
    a, b = await asyncio.gather(h.fire("orb_open", TUE), h.fire("orb_open", TUE))
    assert sorted([a.status, b.status]) == ["fired", "skipped"]
    assert len(h.runner.calls) == 1
    assert job_rows(db_factory) == [("event:orb_open", "succeeded", None)]


@pytestdb
async def test_5_too_early_unless_forced(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 35, 0))
    assert (await h.fire("orb_open", TUE)).status == "too_early"
    assert job_rows(db_factory) == [] and h.built == 0
    assert (await h.fire("orb_open", TUE, force=True)).status == "fired"
    assert len(h.runner.calls) == 1


@pytestdb
async def test_6_within_grace_fires(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 36, 0))  # 55 s late
    assert (await h.fire("orb_open", TUE)).status == "fired"


@pytestdb
async def test_6_beyond_grace_is_missed(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 40, 0))  # 295 s late, grace 120
    out = await h.fire("orb_open", TUE)
    assert out.status == "missed" and out.detail["late_seconds"] == 295
    assert job_rows(db_factory) == [("event:orb_open", "failed", "missed: 295s late")]
    errors = error_events(db_factory)
    assert len(errors) == 1 and errors[0].source == "job.event:orb_open"
    assert h.built == 0 and h.runner.calls == []
    # force still runs it (an explicit `trader event orb_open --force`)
    assert (await h.fire("orb_open", TUE, force=True)).status == "fired"


@pytestdb
async def test_7_flatten_twenty_minutes_late_before_the_close_fires(
    db_factory: sessionmaker[Session],
) -> None:
    # exit_at close-30m (15:30 ET), so 20 minutes late is 15:50, still before the 16:00 close
    h = harness(db_factory, et(TUE, 15, 50), [OrbSip(OrbSipParams(exit_at="close-30m"))])
    assert (await h.fire("flatten", TUE)).status == "fired"


@pytestdb
async def test_7_flatten_after_the_close_is_missed(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 16, 0, 1))
    out = await h.fire("flatten", TUE)
    assert out.status == "missed"
    (row,) = job_rows(db_factory)
    assert row[1] == "failed" and (row[2] or "").startswith("missed:")
    assert h.built == 0


@pytestdb
async def test_7_flatten_already_succeeded_is_skipped_by_the_late_backup(
    db_factory: sessionmaker[Session],
) -> None:
    h = harness(db_factory, et(TUE, 15, 50))
    assert (await h.fire("flatten", TUE)).status == "fired"
    h.clock.set(et(TUE, 15, 55))  # the cron backup, 5 minutes late
    out = await h.fire("flatten", TUE)
    assert out.status == "skipped" and out.detail == {"reason": "already succeeded"}
    assert len(h.runner.calls) == 1 and h.built == 1


@pytestdb
async def test_missed_entry_that_already_succeeded_is_skipped(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 35, 5))
    assert (await h.fire("orb_open", TUE)).status == "fired"
    h.clock.set(et(TUE, 10, 0))
    assert (await h.fire("orb_open", TUE)).status == "skipped"
    assert job_rows(db_factory) == [("event:orb_open", "succeeded", None)]


@pytestdb
async def test_8_runner_failure_is_recorded_and_retried(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 35, 5))
    h.runner.fail = True
    out = await h.fire("orb_open", TUE)
    assert out.status == "failed" and "engine blew up" in out.detail["error"]
    assert job_rows(db_factory) == [("event:orb_open", "failed", "RuntimeError: engine blew up")]
    assert len(error_events(db_factory)) == 1
    h.runner.fail = False
    # fix round 1: the retry waits out a 30 s backoff after the first failure
    h.clock.advance(timedelta(seconds=29))
    waiting = await h.fire("orb_open", TUE)
    assert waiting.status == "skipped" and waiting.detail["reason"] == "retry backoff"
    assert waiting.detail["attempts"] == 1 and len(h.runner.calls) == 1
    h.clock.advance(timedelta(seconds=1))
    assert (await h.fire("orb_open", TUE)).status == "fired"
    assert len(h.runner.calls) == 2


def test_retry_backoff_is_30_60_then_120_seconds() -> None:
    assert [retry_backoff(n).total_seconds() for n in (1, 2, 3, 4, 9)] == [30, 60, 120, 120, 120]


@pytestdb
async def test_grace_is_compared_exactly_not_truncated(db_factory: sessionmaker[Session]) -> None:
    late = utc(TUE, 13, 35, 5) + timedelta(seconds=120, milliseconds=900)
    h = harness(db_factory, late)
    out = await h.fire("orb_open", TUE)
    assert out.status == "missed" and out.detail["late_seconds"] == 121
    assert h.built == 0


@pytestdb
async def test_12_missed_event_is_settled_after_one_alert(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 40))
    first = await h.worker_round(TUE)
    assert [(r.key, r.status) for r in first] == [("orb_open", "missed")]
    assert "orb_open" in fired_keys(db_factory, TUE)
    for _ in range(10):
        h.clock.advance(timedelta(seconds=2))
        assert await h.worker_round(TUE) == []
    assert job_rows(db_factory) == [("event:orb_open", "failed", "missed: 295s late")]
    assert len(error_events(db_factory)) == 1


@pytestdb
async def test_12_three_failures_settle_the_key(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 9, 35, 5))
    h.runner.fail = True
    # attempts at +0 s, +30 s and +90 s (the 30 s and 60 s backoffs), all inside the 120 s grace;
    # the rounds in between are skipped by the backoff and never run the engine
    for wait in (30, 60, 0):
        assert [r.status for r in await h.worker_round(TUE)] == ["failed"]
        for _ in range(3):
            h.clock.advance(timedelta(seconds=wait / 4))
            if wait:
                assert [r.status for r in await h.worker_round(TUE)] == ["skipped"]
        h.clock.advance(timedelta(seconds=wait / 4))
    assert "orb_open" in fired_keys(db_factory, TUE)
    assert await h.worker_round(TUE) == []
    assert len(h.runner.calls) == 3


@pytestdb
async def test_safety_key_keeps_retrying_with_backoff_until_the_close(
    db_factory: sessionmaker[Session],
) -> None:
    # flatten at 15:50 ET, the close at 16:00: failures at 15:50:00, :30, 15:51:30, 15:53:30, 15:55:30, ...
    h = harness(db_factory, et(TUE, 15, 50))
    h.runner.fail = True
    attempts = 0
    while h.clock.now() < et(TUE, 16, 0):
        results = await h.worker_round(TUE)
        attempts += sum(1 for r in results if r.key == "flatten" and r.status == "failed")
        h.clock.advance(timedelta(seconds=10))
    # 15:50:00, 15:50:30, 15:51:30, then every 120 s: 15:53:30, 15:55:30, 15:57:30, 15:59:30
    assert attempts == 7
    assert "flatten" not in fired_keys(db_factory, TUE, always_fire_late=["flatten"])
    assert "flatten" not in fired_keys(db_factory, TUE)  # from the stored (default) settings
    out = await h.fire("flatten", TUE)  # at the close: missed, and settled
    assert out.status == "missed"
    assert "flatten" in fired_keys(db_factory, TUE)


@pytestdb
async def test_leftover_running_entry_event_is_settled_as_outcome_unknown_not_rerun(
    db_factory: sessionmaker[Session],
) -> None:
    h = harness(db_factory, et(TUE, 9, 35, 30))
    with db_factory() as s:  # a worker died mid-run (or its success could not be recorded)
        s.add(
            JobRun(job="event:orb_open", session_date=TUE, started_at=utc(TUE, 13, 35, 5), status="running")
        )
        s.commit()
    out = await h.fire("orb_open", TUE)
    assert out.status == "failed" and out.detail["error"] == "outcome unknown: not re-run automatically"
    assert h.built == 0 and h.runner.calls == []
    assert job_rows(db_factory) == [("event:orb_open", "failed", "outcome unknown: not re-run automatically")]
    with db_factory() as s:
        crit = s.execute(select(EventLog).where(EventLog.level == "critical")).scalars().all()
    assert [e.source for e in crit] == ["job.event:orb_open"]
    assert "orb_open" in fired_keys(db_factory, TUE)
    h.clock.advance(timedelta(seconds=5))
    assert await h.worker_round(TUE) == []
    # an explicit `trader event orb_open --force` still runs it
    assert (await h.fire("orb_open", TUE, force=True)).status == "fired"
    assert len(h.runner.calls) == 1


@pytestdb
async def test_leftover_running_safety_event_is_rerun(db_factory: sessionmaker[Session]) -> None:
    h = harness(db_factory, et(TUE, 15, 51))
    with db_factory() as s:
        s.add(JobRun(job="event:flatten", session_date=TUE, started_at=utc(TUE, 19, 50), status="running"))
        s.commit()
    assert (await h.fire("flatten", TUE)).status == "fired"
    assert job_rows(db_factory) == [
        ("event:flatten", "failed", "abandoned"),
        ("event:flatten", "succeeded", None),
    ]


@pytestdb
async def test_plan_problems_are_reported_once_per_session(db_factory: sessionmaker[Session]) -> None:
    strategies: list[Strategy] = [
        FakeStrategy("late_one", [ScheduledEvent("flatten", SessionOffset.parse("close-10m"))]),
        FakeStrategy("early_one", [ScheduledEvent("flatten", SessionOffset.parse("close-15m"))]),
        FakeStrategy("wordy", [ScheduledEvent("k" * 40, SessionOffset.parse("open+1m"))]),
    ]
    plan = day_plan(strategies, CAL, TUE, SETTINGS)
    assert sorted(p.problem_id for p in plan.problems) == [
        "key_time_conflict:flatten",
        f"key_too_long:{'k' * 40}",
    ]
    clock = FixedClock(utc(TUE, 14, 0))
    assert report_plan_problems(db_factory, clock, plan) == 2
    for _ in range(5):
        assert report_plan_problems(db_factory, clock, day_plan(strategies, CAL, TUE, SETTINGS)) == 0
    # fire_event reports them too (still once)
    h = harness(db_factory, et(TUE, 15, 45), strategies)
    assert (await h.fire("flatten", TUE)).status == "fired"
    rows = error_events(db_factory)
    assert [(e.source, e.data["kind"]) for e in rows] == [
        ("scheduler", "key_too_long"),
        ("scheduler", "key_time_conflict"),
    ]
    # the next session reports again
    wed = date(2026, 10, 7)
    assert report_plan_problems(db_factory, clock, day_plan(strategies, CAL, wed, SETTINGS)) == 2


@pytestdb
async def test_fired_keys_are_per_session_and_ignore_other_jobs(db_factory: sessionmaker[Session]) -> None:
    now = utc(TUE, 14, 0)
    with db_factory() as s:
        s.add(JobRun(job="event:orb_open", session_date=TUE, started_at=now, status="succeeded"))
        s.add(JobRun(job="event:flatten", session_date=date(2026, 10, 5), started_at=now, status="succeeded"))
        s.add(JobRun(job="nightly", session_date=TUE, started_at=now, status="succeeded"))
        s.add(JobRun(job="event:entry_cancel", session_date=TUE, started_at=now, status="failed", error="x"))
        s.add(JobRun(job="event:entry_cancel", session_date=TUE, started_at=now, status="failed", error="x"))
        s.add(JobRun(job="event:flatten", session_date=TUE, started_at=now, status="running"))
        s.commit()
    assert fired_keys(db_factory, TUE) == {"orb_open"}


# --- P2-REVIEW: a disabled strategy that still owns something gets its safety events ------------


def test_exits_only_strategies_contribute_only_their_safety_events() -> None:
    plan = day_plan([SpyOverlay()], CAL, TUE, SETTINGS, exits_only=[OrbSip()])
    by_key = {e.key: e for e in plan.events}
    assert sorted(by_key) == ["entry_cancel", "flatten", "overlay_decision"]  # no orb_open entry
    assert by_key["flatten"].strategies == ("orb_sip",) and by_key["flatten"].always_fire_late
    # an exits-only strategy that is also enabled is just enabled
    both = day_plan([OrbSip()], CAL, TUE, SETTINGS, exits_only=[OrbSip()])
    assert both.events == day_plan([OrbSip()], CAL, TUE, SETTINGS).events


@pytestdb
def test_live_day_plan_keeps_a_disabled_owners_flatten(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(utc(TUE, 14, 0))
    registry = StrategyRegistry(db_factory, clock)
    registry.ensure_defaults()
    registry.update("orb_sip", enabled=False, actor="test")
    with db_factory() as s:
        run_id = add_run(s)
        symbol_id = add_symbol(s, "AAA")
        s.commit()

    def keys() -> list[str]:
        return sorted(e.key for e in live_day_plan(db_factory, registry, run_id, CAL, TUE, SETTINGS).events)

    assert "flatten" not in keys() and "orb_open" not in keys()  # disabled and owning nothing: not run
    config_id = min(registry.config_ids("orb_sip"))  # the revision in force before it was disabled
    with db_factory() as s:  # it still has a working entry stop from before it was disabled
        s.add(
            m.Order(
                run_id=run_id,
                symbol_id=symbol_id,
                strategy_config_id=config_id,
                position_id=None,
                side="buy",
                order_type="stop",
                purpose="entry",
                qty=10,
                stop_price=Decimal("21.55"),
                stop_loss=Decimal("21.41"),
                tif="day",
                status="working",
                reason="orb breakout",
                session_date=TUE,
                submitted_at=clock.now(),
                stale_alerted=False,
            )
        )
        s.commit()
    assert {"entry_cancel", "flatten"} <= set(keys()) and "orb_open" not in keys()
