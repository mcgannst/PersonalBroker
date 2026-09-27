"""P3-B4 gauntlet: breaker tests for P3-T10 (pre-open, check-in, event backup) and P3-T11 (post-close and
the candle archive). Review Focus 5 (holidays, early closes, DST) and failure isolation.

Fakes and the testcontainers database only; no network, no real sleeping.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_telegram import FakeIssuer, FakeRenderer, FakeTelegramApi, RecordingNotifier
from trader.adapters.telegram.types import TelegramApiError
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.scheduler import DayPlan, FireDeps, FireResult, PlannedEvent, fire_event
from trader.jobs.checkin import CheckinDeps, run_checkin
from trader.jobs.events import run_event_backup
from trader.jobs.postclose import PostcloseDeps, archive_candles, daily_summary_view, run_postclose
from trader.jobs.preopen import PreopenDeps, run_preopen
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle, Interval, OpeningBars, UniverseMember, UniverseStatus
from trader.notify.notifier import TelegramNotifier
from trader.notify.types import Notifier, OutboundMessage, PreopenView
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tuesday, EDT
AFTER_FALL_BACK = date(2026, 11, 2)  # first session on EST (clocks changed Sun 2026-11-01)
AFTER_SPRING_FORWARD = date(2026, 3, 9)  # first session on EDT (clocks changed Sun 2026-03-08)
THANKSGIVING_FRIDAY = date(2026, 11, 27)  # 13:00 ET close
CHRISTMAS_EVE = date(2026, 12, 24)  # Thursday, 13:00 ET close
CHAT = 42
ONE_MIN = timedelta(minutes=1)


def et(d: date, h: int, mi: int, s: int = 0) -> datetime:
    return datetime.combine(d, time(h, mi, s), tzinfo=ET).astimezone(UTC)


async def no_sleep(_seconds: float) -> None:
    return None


def plan_for(d: date) -> DayPlan:
    """A realistic day plan from the calendar: ORB at open+5m5s, entry_cancel at 11:30 ET, overlay 30 min
    and flatten 10 min before the real close (early closes included)."""
    if not CAL.is_session(d):
        return DayPlan(d, False, None, None, ())
    open_, close = CAL.session_open(d), CAL.session_close(d)
    events = [PlannedEvent("orb_open", open_ + timedelta(minutes=5, seconds=5), ("orb_sip",), False)]
    if et(d, 11, 30) < close:
        events.append(PlannedEvent("entry_cancel", et(d, 11, 30), ("orb_sip",), True))
    events.append(PlannedEvent("overlay_decision", close - timedelta(minutes=30), ("spy_overlay",), True))
    events.append(PlannedEvent("flatten", close - timedelta(minutes=10), ("orb_sip",), True))
    events.sort(key=lambda e: (e.at, e.key))
    return DayPlan(d, True, open_, close, tuple(events))


def telegram_notifier(
    factory: sessionmaker[Session], clock: FixedClock
) -> tuple[TelegramNotifier, FakeTelegramApi]:
    """A real TelegramNotifier (dedupe through the `notifications` table) over a fake API: one per
    simulated cron process."""
    api = FakeTelegramApi()
    return TelegramNotifier(api, CHAT, factory, clock, sleep=no_sleep), api


# =========================================================================================================
# Pre-open
# =========================================================================================================


class Preopen:
    def __init__(self, factory: sessionmaker[Session], run_id: int, clock: FixedClock) -> None:
        self.factory = factory
        self.run_id = run_id
        self.clock = clock
        self.render = FakeRenderer()
        self.settings = RuntimeSettings()
        self.token_error: Exception | None = None
        self.universe: UniverseStatus | Exception = UniverseStatus("finviz", None, False, None)

    async def token_check(self) -> None:
        if self.token_error is not None:
            raise self.token_error

    async def universe_status(self, d: date) -> UniverseStatus:
        if isinstance(self.universe, Exception):
            raise self.universe
        return self.universe

    def deps(self, notifier: Notifier) -> PreopenDeps:
        return PreopenDeps(
            factory=self.factory,
            clock=self.clock,
            calendar=CAL,
            settings=lambda: self.settings,
            token_check=self.token_check,
            universe_status=self.universe_status,
            killswitches=KillSwitches(self.factory, self.clock),
            run_id=self.run_id,
            notifier=notifier,
            render=self.render,
        )


def seed_preopen_healthy(
    s: Session, d: date, now: datetime, beat_age: timedelta | None, phase: str = "idle"
) -> None:
    sid = add_symbol(s, "AAA")
    s.add(m.OpenBarStat(symbol_id=sid, session_date=d, avg_open_vol_14d=Decimal("1000"), atr14=None))
    s.add(
        m.JobRun(
            job="premarket",
            session_date=d,
            started_at=now - timedelta(hours=1, minutes=20),
            finished_at=now - timedelta(hours=1),
            status="succeeded",
            detail={},
        )
    )
    if beat_age is not None:
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=1,
                host="h",
                started_at=now - timedelta(hours=3),
                beat_at=now - beat_age,
                session_date=d,
                phase=phase,
            )
        )


def checks_of(detail: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in detail["checks"]}


@pytest.mark.parametrize(
    ("session", "utc_hour"),
    [(AFTER_FALL_BACK, 14), (AFTER_SPRING_FORWARD, 13)],
    ids=["first-session-on-EST", "first-session-on-EDT"],
)
async def test_preopen_on_the_first_session_after_a_dst_change(
    db_factory: sessionmaker[Session], session: date, utc_hour: int
) -> None:
    """09:20 ET is 14:20 UTC after the November change and 13:20 UTC after the March one. A heartbeat 30 s
    old is healthy; one exactly an hour old (a worker still on the old offset, or dead) is not."""
    now = et(session, 9, 20)
    assert now.hour == utc_hour and now.minute == 20
    with db_factory() as s:
        run_id = add_run(s)
        seed_preopen_healthy(s, session, now, timedelta(seconds=30))
        s.commit()
    h = Preopen(db_factory, run_id, FixedClock(now))
    notifier = RecordingNotifier()
    detail = await run_preopen(h.deps(notifier), session)
    assert detail["ok"] is True, detail
    assert detail["session_date"] == session.isoformat()
    assert [m_.dedupe_key for m_ in notifier.sent] == [f"preopen:{session.isoformat()}"]

    with db_factory() as s:
        s.query(m.WorkerHeartbeat).update({m.WorkerHeartbeat.beat_at: now - timedelta(hours=1)})
        s.commit()
    worker = checks_of(await run_preopen(h.deps(RecordingNotifier()), session))["worker"]
    assert worker["ok"] is False and worker["level"] == "error"


async def test_preopen_everything_failing_is_one_message_listing_all_and_a_rerun_is_deduped(
    db_factory: sessionmaker[Session],
) -> None:
    """Token dead, universe lookup raising, no open-bar stats, no pre-market run, an automatic kill switch
    and no worker: six non-OK checks in ONE message. A second cron process re-running the job the same
    morning (a new notifier, a new API client) must not send it again."""
    now = et(DAY, 9, 20)
    with db_factory() as s:
        run_id = add_run(s)
        s.add(
            m.KillSwitchEvent(
                run_id=run_id,
                switch="max_drawdown_pct",
                session_date=date(2026, 10, 5),
                tripped_at=now - timedelta(days=1),
                value=Decimal("0.2"),
                threshold=Decimal("0.15"),
            )
        )
        s.commit()
    clock = FixedClock(now)
    h = Preopen(db_factory, run_id, clock)
    h.settings = RuntimeSettings.model_validate({"preopen.notify_when_ok": False})
    h.token_error = RuntimeError("The refresh token was already used or has expired.")
    h.universe = RuntimeError("database is down")

    first, api1 = telegram_notifier(db_factory, clock)
    detail = await run_preopen(h.deps(first), DAY)
    checks = checks_of(detail)
    assert detail["ok"] is False
    assert list(checks) == ["token", "universe", "open_bar_stats", "premarket", "kill_switches", "worker"]
    assert all(not c["ok"] for c in checks.values())
    assert {n: c["level"] for n, c in checks.items()} == {
        "token": "error",
        "universe": "error",
        "open_bar_stats": "error",
        "premarket": "warning",
        "kill_switches": "error",
        "worker": "error",
    }
    views = [a[0] for name, a in h.render.calls if name == "preopen"]
    assert len(views) == 1 and isinstance(views[0], PreopenView)
    assert [c.name for c in views[0].checks] == list(checks)
    sent = api1.calls_of("send_message")
    assert len(sent) == 1
    for name in checks:
        assert name in sent[0]["text"]

    clock.set(now + timedelta(minutes=3))
    second, api2 = telegram_notifier(db_factory, clock)
    again = await run_preopen(h.deps(second), DAY)
    assert again["ok"] is False
    assert api2.calls_of("send_message") == []  # deduped through `notifications`, across processes
    with db_factory() as s:
        keys = s.execute(select(m.Notification.dedupe_key)).scalars().all()
    assert keys == ["preopen:2026-10-06"]


async def test_preopen_stale_heartbeat_vs_stopped_worker(db_factory: sessionmaker[Session]) -> None:
    """A heartbeat older than worker.heartbeat_stale_seconds and a fresh heartbeat whose phase is `stopped`
    are both `error`, with different explanations; a fresh `idle` one at exactly the limit is OK."""
    now = et(DAY, 9, 20)
    with db_factory() as s:
        run_id = add_run(s)
        seed_preopen_healthy(s, DAY, now, timedelta(seconds=121))
        s.commit()
    h = Preopen(db_factory, run_id, FixedClock(now))
    stale = checks_of(await run_preopen(h.deps(RecordingNotifier()), DAY))["worker"]
    assert stale["ok"] is False and stale["level"] == "error"
    assert "121s ago" in stale["detail"] and "worker not running" in stale["detail"]

    with db_factory() as s:
        s.query(m.WorkerHeartbeat).update(
            {m.WorkerHeartbeat.beat_at: now - timedelta(seconds=5), m.WorkerHeartbeat.phase: "stopped"}
        )
        s.commit()
    stopped = checks_of(await run_preopen(h.deps(RecordingNotifier()), DAY))["worker"]
    assert stopped["ok"] is False and stopped["level"] == "error"
    assert "stopped" in stopped["detail"] and stopped["detail"] != stale["detail"]

    with db_factory() as s:
        s.query(m.WorkerHeartbeat).update(
            {m.WorkerHeartbeat.beat_at: now - timedelta(seconds=120), m.WorkerHeartbeat.phase: "idle"}
        )
        s.commit()
    boundary = checks_of(await run_preopen(h.deps(RecordingNotifier()), DAY))["worker"]
    assert boundary["ok"] is True


# =========================================================================================================
# Check-in
# =========================================================================================================


class RaisingNotifier:
    """Breaks the Notifier contract on purpose: send raises (a Telegram 500 escaping)."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, msg: OutboundMessage) -> None:
        self.attempts += 1
        raise TelegramApiError(500, "Internal Server Error")


class Checkin:
    def __init__(self, factory: sessionmaker[Session], run_id: int, clock: FixedClock) -> None:
        self.factory = factory
        self.run_id = run_id
        self.clock = clock
        self.render = FakeRenderer()
        self.fired: set[str] = {"orb_open"}
        self.fire_calls: list[tuple[str, date]] = []
        self.raise_for: set[str] = set()

    async def fire(self, key: str, session_date: date) -> FireResult:
        self.fire_calls.append((key, session_date))
        if key in self.raise_for:
            raise RuntimeError(f"{key} blew up")
        return FireResult(key, session_date, "fired", {})

    def deps(self, notifier: Notifier) -> CheckinDeps:
        return CheckinDeps(
            factory=self.factory,
            clock=self.clock,
            calendar=CAL,
            settings=RuntimeSettings,
            run_id=self.run_id,
            notifier=notifier,
            render=self.render,
            plan=plan_for,
            fired=lambda d: set(self.fired),
            fire=self.fire,
            quotes=None,
        )


@pytest.fixture
def run_id(db_factory: sessionmaker[Session]) -> int:
    with db_factory() as s:
        rid = add_run(s)
        s.commit()
    return rid


async def test_checkin_on_christmas_eve_early_close(db_factory: sessionmaker[Session], run_id: int) -> None:
    """Christmas Eve closes at 13:00 ET. 13:30 (and 13:00 exactly) are after the close: skipped, nothing
    sent, nothing fired. 12:55 is still in session: the check-in runs and backs up the due flatten."""
    assert CAL.session_close(CHRISTMAS_EVE) == et(CHRISTMAS_EVE, 13, 0)
    for at in (et(CHRISTMAS_EVE, 13, 30), et(CHRISTMAS_EVE, 13, 0)):
        h = Checkin(db_factory, run_id, FixedClock(at))
        h.fired = {"orb_open", "entry_cancel"}
        notifier = RecordingNotifier()
        assert await run_checkin(h.deps(notifier), CHRISTMAS_EVE, "13:30") == {"skipped": "after close"}
        assert notifier.sent == [] and h.fire_calls == []

    h = Checkin(db_factory, run_id, FixedClock(et(CHRISTMAS_EVE, 12, 55)))
    h.fired = {"orb_open", "entry_cancel"}
    notifier = RecordingNotifier()
    detail = await run_checkin(h.deps(notifier), CHRISTMAS_EVE, "11:30")
    assert h.fire_calls == [("overlay_decision", CHRISTMAS_EVE), ("flatten", CHRISTMAS_EVE)]
    assert detail["sent"] is True and len(notifier.sent) == 1


async def test_checkin_with_the_notifier_failing_still_fires_due_events(
    db_factory: sessionmaker[Session], run_id: int
) -> None:
    """Telegram down at 11:30 must not cost the entry_cancel backup (SPEC §9)."""
    h = Checkin(db_factory, run_id, FixedClock(et(DAY, 11, 30, 30)))
    notifier = RaisingNotifier()
    detail = await run_checkin(h.deps(notifier), DAY, "11:30")
    assert notifier.attempts == 1
    assert h.fire_calls == [("entry_cancel", DAY)]
    assert detail["sent"] is False
    assert detail["fired"] == [{"key": "entry_cancel", "status": "fired"}]
    with db_factory() as s:
        events = s.query(m.EventLog).filter(m.EventLog.source == "job.checkin").all()
    assert [e.level for e in events] == ["error"]


async def test_a_due_event_that_raises_does_not_stop_the_later_ones(
    db_factory: sessionmaker[Session], run_id: int
) -> None:
    """At 15:51 ET entry_cancel, overlay_decision and flatten are all due. If firing entry_cancel raises
    (a DB blip inside fire), the flatten, the safety event, must still be fired, both by the check-in and
    by `trader event --due`. The raising one is reported as failed, not swallowed silently."""
    now = et(DAY, 15, 51)
    h = Checkin(db_factory, run_id, FixedClock(now))
    h.raise_for = {"entry_cancel"}
    detail = await run_checkin(h.deps(RecordingNotifier()), DAY, "13:30")
    assert [k for k, _ in h.fire_calls] == ["entry_cancel", "overlay_decision", "flatten"]
    by_key = {r["key"]: r["status"] for r in detail["fired"]}
    assert by_key == {"entry_cancel": "failed", "overlay_decision": "fired", "flatten": "fired"}

    h2 = Checkin(db_factory, run_id, FixedClock(now))
    h2.raise_for = {"entry_cancel"}
    results = await run_event_backup(
        h2.fire, CAL, FixedClock(now), None, DAY, due=True, plan=plan_for, fired=lambda d: {"orb_open"}
    )
    assert [k for k, _ in h2.fire_calls] == ["entry_cancel", "overlay_decision", "flatten"]
    assert {r.key: r.status for r in results} == {
        "entry_cancel": "failed",
        "overlay_decision": "fired",
        "flatten": "fired",
    }


# =========================================================================================================
# Event backup
# =========================================================================================================


@dataclass
class SlowRunner:
    """An EventRunner that yields to the event loop while it 'runs', so a racer can get in."""

    calls: list[tuple[str, date]] = field(default_factory=list)

    async def run_event(self, event_key: str, session_date: date) -> Any:
        self.calls.append((event_key, session_date))
        for _ in range(10):
            await asyncio.sleep(0)
        return None


async def test_event_backup_racing_the_worker_fires_exactly_once(db_factory: sessionmaker[Session]) -> None:
    """The worker and the cron backup fire orb_open at the same moment (two FireDeps, the real fire_event and
    the real job-run lock). Exactly one runs the engine; the other gets `skipped`; one succeeded job run."""
    clock = FixedClock(et(DAY, 9, 35, 30))
    runner = SlowRunner()

    async def get_runner() -> SlowRunner:
        return runner

    def deps() -> FireDeps:
        return FireDeps(db_factory, clock, CAL, RuntimeSettings, plan_for, get_runner)

    worker_deps, cron_deps = deps(), deps()

    async def cron_fire(key: str, d: date) -> FireResult:
        return await fire_event(cron_deps, key, d)

    worker_result, backup_results = await asyncio.gather(
        fire_event(worker_deps, "orb_open", DAY),
        run_event_backup(cron_fire, CAL, clock, "orb_open", DAY),
    )
    assert runner.calls == [("orb_open", DAY)]
    assert sorted([worker_result.status, *(r.status for r in backup_results)]) == ["fired", "skipped"]
    with db_factory() as s:
        ok = s.execute(
            select(func.count())
            .select_from(m.JobRun)
            .where(m.JobRun.job == "event:orb_open", m.JobRun.status == "succeeded")
        ).scalar_one()
    assert ok == 1

    later = await run_event_backup(cron_fire, CAL, clock, "orb_open", DAY)
    assert [r.status for r in later] == ["skipped"] and len(runner.calls) == 1


# =========================================================================================================
# Post-close
# =========================================================================================================


def bar(start: datetime, step: timedelta = ONE_MIN, close: str = "21.40") -> Candle:
    return Candle(
        start, start + step, Decimal("21.00"), Decimal("21.50"), Decimal("20.90"), Decimal(close), 5000, None
    )


def member(sid: int, ticker: str) -> UniverseMember:
    return UniverseMember(sid, ticker, None, Decimal("20"), 1_000_000, Decimal("1"), "finviz")


class Data:
    """ArchiveData. 1-minute bars at 04:00, 09:30, 12:59, 13:00, 15:59 and 19:59 ET. `outage_after` makes
    every candles call after that many raise (Questrade going down mid-archive); `opening_missing` ids get
    no opening bar."""

    def __init__(self, members: Sequence[UniverseMember]) -> None:
        self.members = list(members)
        self.outage_after: int | None = None
        self.opening_missing: set[int] = set()
        self.opening_raises: Exception | None = None
        self.candle_calls: list[tuple[int, datetime, datetime, Interval]] = []

    async def universe(self, session_date: date) -> list[UniverseMember]:
        return list(self.members)

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        if self.opening_raises is not None:
            raise self.opening_raises
        ids = list(symbol_ids) if symbol_ids is not None else [u.symbol_id for u in self.members]
        open_ = CAL.session_open(session_date)
        return OpeningBars(
            {sid: bar(open_, timedelta(minutes=5)) for sid in ids if sid not in self.opening_missing},
            {sid: "questrade_error: HTTP 503" for sid in ids if sid in self.opening_missing},
        )

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        self.candle_calls.append((symbol_id, start, end, interval))
        if self.outage_after is not None and len(self.candle_calls) > self.outage_after:
            raise ConnectionError("Questrade unreachable")
        d = start.astimezone(ET).date()
        return [bar(et(d, h, mi)) for h, mi in ((4, 0), (9, 30), (12, 59), (13, 0), (15, 59), (19, 59))]


@dataclass
class Engine:
    end_calls: list[date] = field(default_factory=list)

    async def run_event(self, event_key: str, session_date: date) -> Any:
        raise AssertionError("not used")

    async def poll_quotes(self) -> Sequence[Any]:
        return []

    async def tick(self, now: datetime) -> None:
        return None

    async def end_of_session(self, session_date: date) -> Sequence[Any]:
        self.end_calls.append(session_date)
        return []


@dataclass
class Post:
    factory: sessionmaker[Session]
    run_id: int
    ids: dict[str, int]
    data: Data
    engine: Engine = field(default_factory=Engine)
    render: FakeRenderer = field(default_factory=FakeRenderer)
    issuer: FakeIssuer = field(default_factory=FakeIssuer)
    settings: RuntimeSettings = field(default_factory=RuntimeSettings)

    def deps(self, now: datetime, notifier: Notifier) -> PostcloseDeps:
        return PostcloseDeps(
            factory=self.factory,
            clock=FixedClock(now),
            calendar=CAL,
            settings=lambda: self.settings,
            engine=self.engine,
            data=self.data,
            notifier=notifier,
            render=self.render,
            issuer=self.issuer,
            chat_id=CHAT,
            run_id=self.run_id,
        )


def make_post(
    factory: sessionmaker[Session], universe: Sequence[str], extra: Sequence[str] = ("SPY",)
) -> Post:
    with factory() as s:
        ids = {t: add_symbol(s, t) for t in (*universe, *extra)}
        rid = add_run(s)
        s.commit()
    return Post(factory, rid, ids, Data([member(ids[t], t) for t in universe]))


def add_candidates(s: Session, run_id: int, d: date, ranked: Sequence[int]) -> None:
    for rank, sid in enumerate(ranked, start=1):
        s.add(
            m.Candidate(
                run_id=run_id,
                session_date=d,
                strategy_key="orb_sip",
                symbol_id=sid,
                rank=rank,
                passed=True,
                created_at=et(d, 9, 35),
            )
        )


def archive_rows(factory: sessionmaker[Session], interval: str) -> list[m.CandleArchive]:
    with factory() as s:
        return list(
            s.execute(
                select(m.CandleArchive)
                .where(m.CandleArchive.interval == interval)
                .order_by(m.CandleArchive.symbol_id, m.CandleArchive.start_ts)
            ).scalars()
        )


def postclose_errors(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(
            s.execute(
                select(m.EventLog)
                .where(m.EventLog.level == "error", m.EventLog.source == "job.postclose")
                .order_by(m.EventLog.id)
            ).scalars()
        )


@pytest.mark.parametrize(
    ("session", "close_hour", "stored"),
    [
        (THANKSGIVING_FRIDAY, 13, {(9, 30), (12, 59)}),
        (CHRISTMAS_EVE, 13, {(9, 30), (12, 59)}),
        (AFTER_FALL_BACK, 16, {(9, 30), (12, 59), (13, 0), (15, 59)}),
        (AFTER_SPRING_FORWARD, 16, {(9, 30), (12, 59), (13, 0), (15, 59)}),
    ],
    ids=["thanksgiving-friday", "christmas-eve", "first-session-on-EST", "first-session-on-EDT"],
)
async def test_postclose_follows_the_real_close_on_early_close_and_dst_days(
    db_factory: sessionmaker[Session], session: date, close_hour: int, stored: set[tuple[int, int]]
) -> None:
    """The 1-minute window is [09:30 ET, the real close) in UTC: 13:00 on Thanksgiving Friday and Christmas
    Eve, and the right UTC offset on the first session after each clock change. The whole job still runs:
    end of session once, the journal row, opening bars at the real open, one summary."""
    post = make_post(db_factory, ["AAA", "BBB"])
    with db_factory() as s:
        add_candidates(s, post.run_id, session, [post.ids["AAA"]])
        s.commit()
    notifier = RecordingNotifier()
    out = await run_postclose(post.deps(et(session, 16, 15), notifier), session)

    assert post.engine.end_calls == [session]
    for sid in (post.ids["AAA"], post.ids["SPY"]):
        (call,) = [c for c in post.data.candle_calls if c[0] == sid]
        assert call[1:] == (et(session, 9, 30), et(session, close_hour, 0), "OneMinute")
    one_min = archive_rows(db_factory, "1m")
    assert {r.symbol_id for r in one_min} == {post.ids["AAA"], post.ids["SPY"]}
    expected = {et(session, h, mi) for h, mi in stored}
    assert {r.start_ts for r in one_min} == expected
    five = archive_rows(db_factory, "5m")
    assert {(r.symbol_id, r.start_ts) for r in five} == {
        (post.ids[t], et(session, 9, 30)) for t in ("AAA", "BBB")
    }
    assert out["archive"]["missing"] == [] and postclose_errors(db_factory) == []
    assert [msg.dedupe_key for msg in notifier.sent] == [f"summary:{session.isoformat()}"]
    assert post.issuer.issued[0]["ref"] == session.strftime("%Y%m%d")
    with db_factory() as s:
        (j,) = s.execute(select(m.Journal)).scalars().all()
    assert (j.session_date, j.rules_followed) == (session, None)


async def test_postclose_rerun_in_a_new_process_sends_once_keeps_the_answer_and_upserts(
    db_factory: sessionmaker[Session],
) -> None:
    """A forced re-run from a second cron process (a new TelegramNotifier): no second summary, the `No`
    answer (False, a falsy value) survives, and the archive has no duplicate rows. upsert_candle_archive
    itself is idempotent and keeps 1m and 5m bars at the same start apart."""
    post = make_post(db_factory, ["AAA", "BBB", "CCC"])
    with db_factory() as s:
        add_candidates(s, post.run_id, DAY, [post.ids["AAA"], post.ids["BBB"]])
        s.commit()
    clock1 = FixedClock(et(DAY, 16, 15))
    n1, api1 = telegram_notifier(db_factory, clock1)
    first = await run_postclose(post.deps(clock1.now(), n1), DAY)
    counts = (len(archive_rows(db_factory, "5m")), len(archive_rows(db_factory, "1m")))
    assert counts == (3, 12)
    with db_factory() as s:
        row = s.get(m.Journal, (post.run_id, DAY))
        assert row is not None
        row.rules_followed, row.answered_via = False, "telegram"
        s.commit()

    clock2 = FixedClock(et(DAY, 16, 45))
    n2, api2 = telegram_notifier(db_factory, clock2)
    second = await run_postclose(post.deps(clock2.now(), n2), DAY)
    assert (len(archive_rows(db_factory, "5m")), len(archive_rows(db_factory, "1m"))) == counts
    assert first["archive"]["1m"] == second["archive"]["1m"] == 12
    assert len(api1.calls_of("send_message")) == 1 and api2.calls_of("send_message") == []
    with db_factory() as s:
        journal = s.execute(select(m.Journal)).scalars().all()
    assert [(j.rules_followed, j.answered_via) for j in journal] == [(False, "telegram")]

    sid = post.ids["AAA"]
    candles = [bar(et(DAY, 9, 30)), bar(et(DAY, 9, 31))]
    with db_factory() as s:
        assert repo.upsert_candle_archive(s, sid, "1m", candles) == 2
        assert repo.upsert_candle_archive(s, sid, "1m", candles) == 2
        assert repo.upsert_candle_archive(s, sid, "5m", [bar(et(DAY, 9, 30), timedelta(minutes=5))]) == 1
        s.commit()
    rows = [(r.interval, r.start_ts) for r in archive_rows(db_factory, "1m") if r.symbol_id == sid]
    assert rows.count(("1m", et(DAY, 9, 30))) == 1 and rows.count(("1m", et(DAY, 9, 31))) == 1
    assert [r.start_ts for r in archive_rows(db_factory, "5m") if r.symbol_id == sid] == [et(DAY, 9, 30)]


async def test_postclose_questrade_outage_mid_archive(db_factory: sessionmaker[Session]) -> None:
    """Questrade degrades during the archive: 3 of 20 opening bars missing (15% > 5%), and every candles call
    after the second raises, so a candidate and SPY are lost. The archive keeps what it got, lists what it
    lost, logs two error events (opening bars, SPY), and the journal and summary still happen."""
    tickers = [f"U{i:02d}" for i in range(20)]
    post = make_post(db_factory, tickers)
    with db_factory() as s:
        add_candidates(s, post.run_id, DAY, [post.ids["U00"], post.ids["U01"], post.ids["U02"]])
        s.commit()
    post.data.opening_missing = {post.ids[t] for t in ("U17", "U18", "U19")}
    post.data.outage_after = 2
    notifier = RecordingNotifier()
    out = await run_postclose(post.deps(et(DAY, 16, 15), notifier), DAY)

    archive = out["archive"]
    assert archive["5m"] == 17
    assert archive["1m"] == 8  # two symbols x four regular-hours bars
    missing = {(x["ticker"], x["interval"]) for x in archive["missing"]}
    assert missing == {("U17", "5m"), ("U18", "5m"), ("U19", "5m"), ("U02", "1m"), ("SPY", "1m")}
    assert all(x["reason"] for x in archive["missing"])
    assert {r.symbol_id for r in archive_rows(db_factory, "1m")} == {post.ids["U00"], post.ids["U01"]}
    errors = postclose_errors(db_factory)
    assert len(errors) == 2
    assert any("opening bars" in e.message for e in errors) and any("SPY" in e.message for e in errors)
    assert out["summary_sent"] is True and [msg.kind for msg in notifier.sent] == ["daily_summary"]
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Journal)).scalar_one() == 1

    # the whole opening-bar batch failing is also survivable
    post.data.opening_raises = ConnectionError("Questrade unreachable")
    with db_factory() as s:
        s.query(m.CandleArchive).delete()
        s.commit()
    again = await archive_only(post, DAY)
    assert again["5m"] == 0
    assert len([x for x in again["missing"] if x["interval"] == "5m"]) == 20


async def archive_only(post: Post, d: date) -> dict[str, Any]:
    return await archive_candles(post.deps(et(d, 16, 15), RecordingNotifier()), d)


async def test_postclose_universe_and_symbols_without_spy(db_factory: sessionmaker[Session]) -> None:
    """No SPY anywhere (not in the universe, no symbols row): the job must not crash; it archives the
    candidates, raises the SPY error event, and still sends the summary."""
    post = make_post(db_factory, ["AAA", "BBB"], extra=())
    with db_factory() as s:
        add_candidates(s, post.run_id, DAY, [post.ids["AAA"]])
        s.commit()
    notifier = RecordingNotifier()
    out = await run_postclose(post.deps(et(DAY, 16, 15), notifier), DAY)
    assert {r.symbol_id for r in archive_rows(db_factory, "1m")} == {post.ids["AAA"]}
    assert len(post.data.candle_calls) == 1
    errors = postclose_errors(db_factory)
    assert len(errors) == 1 and "SPY" in errors[0].message
    assert out["summary_sent"] is True and len(notifier.sent) == 1


# =========================================================================================================
# Summary maths
# =========================================================================================================


def add_position(s: Session, run_id: int, symbol_id: int, unprotected_seconds: int) -> int:
    pos = m.Position(
        run_id=run_id,
        symbol_id=symbol_id,
        qty=100,
        avg_price=Decimal("21.0000"),
        stop_loss=Decimal("20.5000"),
        planned_risk=Decimal("50.0000"),
        session_date=DAY,
        opened_at=et(DAY, 9, 36),
        closed_at=et(DAY, 15, 55),
        entry_order_id=1,
        unprotected_seconds=unprotected_seconds,
    )
    s.add(pos)
    s.flush()
    return pos.id


def add_trade(s: Session, run_id: int, position_id: int, symbol_id: int, pnl: str, fees: str) -> None:
    s.add(
        m.Trade(
            run_id=run_id,
            position_id=position_id,
            symbol_id=symbol_id,
            session_date=DAY,
            entry_price=Decimal("21.0000"),
            exit_price=Decimal("21.1500"),
            qty=100,
            pnl=Decimal(pnl),
            pnl_r=Decimal("0.0100"),
            planned_risk=Decimal("50.0000"),
            exit_reason="flatten_close",
            slippage_total=Decimal("0.0200"),
            fees_total=Decimal(fees),
            opened_at=et(DAY, 9, 36),
            closed_at=et(DAY, 15, 55),
        )
    )


def add_proposal(
    s: Session, run_id: int, signal_id: int, created: datetime, latency_ms: int | None, via: str | None
) -> None:
    s.add(
        m.Proposal(
            run_id=run_id,
            signal_id=signal_id,
            kind="entry",
            order_spec={},
            qty=10,
            status="approved" if via != "auto" else "auto_approved",
            created_at=created,
            expires_at=created + timedelta(minutes=2),
            decided_at=created + timedelta(milliseconds=latency_ms or 0) if via else None,
            decided_via=via,
            decided_by="stephen" if via != "auto" else "auto",
            decision_latency_ms=latency_ms,
            escalations=0,
        )
    )


def test_summary_maths_decision_time_excludes_auto_and_money_is_exact_decimal(
    db_factory: sessionmaker[Session],
) -> None:
    """Human decisions 1000 ms (telegram) and 2000 ms (web) average 1.5 s. Auto approvals, even one that
    carries a non-zero latency, and an auto-flatten with none are not decisions. A decision on the previous
    ET day that is the same UTC day (23:59 ET = 03:59 UTC) is not counted. Money is exact Decimal:
    0.1000 + 0.2000 = 0.3000, not 0.30000000000000004."""
    with db_factory() as s:
        run_id = add_run(s)
        sid = add_symbol(s, "AAA")
        p1 = add_position(s, run_id, sid, 40)
        p2 = add_position(s, run_id, sid, 55)
        add_trade(s, run_id, p1, sid, "0.1000", "1.1000")
        add_trade(s, run_id, p2, sid, "0.2000", "2.2000")
        cfg = add_strategy_config(s)
        sig = m.Signal(
            run_id=run_id,
            strategy_config_id=cfg,
            symbol_id=sid,
            session_date=DAY,
            event_key="orb_open",
            ts=et(DAY, 9, 35),
            intent={},
            evidence={},
        )
        s.add(sig)
        s.flush()
        created = et(DAY, 9, 35)
        add_proposal(s, run_id, sig.id, created, 1000, "telegram")
        add_proposal(s, run_id, sig.id, created, 2000, "web")
        add_proposal(s, run_id, sig.id, created, 4000, "auto")
        add_proposal(s, run_id, sig.id, created, None, "auto")
        add_proposal(s, run_id, sig.id, et(DAY - timedelta(days=1), 23, 59), 9000, "telegram")
        s.add(
            m.EquitySnapshot(
                run_id=run_id,
                ts=et(DAY, 16, 0),
                equity=Decimal("10000.3000"),
                cash=Decimal("10000.3000"),
                settled_cash=Decimal("10000.0000"),
                peak_equity=Decimal("10000.3000"),
                drawdown_pct=Decimal("0.0000"),
            )
        )
        s.commit()
    view = daily_summary_view(db_factory, run_id, DAY, et(DAY, 16, 15), {"5m": 0, "1m": 0})
    assert view.decisions == 2
    assert view.avg_decision_seconds == pytest.approx(1.5)
    assert view.unprotected_seconds == 95
    for value in (view.realized_pnl, view.fees, view.equity, view.drawdown_pct):
        assert type(value) is Decimal, (value, type(value))
    assert view.realized_pnl == Decimal("0.3000")
    assert view.fees == Decimal("3.3000")
    assert view.equity == Decimal("10000.3000")
    for t in view.trades:
        for money in (t.entry, t.exit, t.pnl):
            assert type(money) is Decimal
    assert sum((t.pnl for t in view.trades), Decimal(0)) == view.realized_pnl
