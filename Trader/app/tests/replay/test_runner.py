"""P5-T6: the replay runner. Real DB for create / cancel / abandon / progress; the loop with
`FakeReplayEngine` and `FakeReplayMarket` (tests/fakes_replay.py), so the T3-T5 code is never needed.

Acceptance tests (plan P5-T6): 1 create, 2 override rows, 3 validation, 4 loop order, 5 determinism,
6 progress and cancel, 7 failure, 9 busy and abandoned, 10 data mode, 11 forced close. (8 is in test_setup.)
"""

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_replay import (
    EngineCall,
    FakeReplayEngine,
    FakeReplayMarket,
    ReplayWorld,
    candle,
    minute_series,
    seed_replay_world,
)
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle
from trader.replay import runner
from trader.replay.clock import ReplayClock
from trader.replay.types import (
    ReplayBusy,
    ReplayInvalid,
    ReplayProgress,
    ReplayRequest,
    ReplayRun,
    StrategyOverride,
    load_replay_run,
)
from trader.settings_store import SettingsStore
from trader.strategies.base import CatalystInfo, ScheduledEvent, SessionOffset
from trader.strategies.orb_sip import OrbSip
from trader.strategies.registry import StrategyConfigView, StrategyRegistry, load_all

CAL = SessionCalendar()
SAT = datetime(2026, 11, 28, 12, 0, tzinfo=ET)  # a Saturday: `full` data mode
MON_23 = date(2026, 11, 23)
TUE_24 = date(2026, 11, 24)
WED_25 = date(2026, 11, 25)
FRI_27 = date(2026, 11, 27)  # the day after Thanksgiving: 13:00 ET close


def et(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=ET).astimezone(UTC)


# --- a stand-in replay clock while P5-T3's is a stub -------------------------------------------------------
class _MonotonicClock:
    def __init__(self, start: datetime) -> None:
        self._at = start

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        if at.tzinfo is None or at < self._at:
            raise ValueError("the replay clock never goes backwards")
        self._at = at

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("negative delta")
        self._at += delta


@pytest.fixture(autouse=True)
def _replay_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        ReplayClock(SAT).now()
    except NotImplementedError:  # P5-T3 not on trunk yet
        monkeypatch.setattr(runner, "ReplayClock", _MonotonicClock)


# --- the world ----------------------------------------------------------------------------------------------
@dataclass
class World:
    factory: sessionmaker[Session]
    wall: FixedClock
    settings: SettingsStore
    registry: StrategyRegistry
    seeded: ReplayWorld
    written: list[StrategyConfigView] = field(default_factory=list)

    @property
    def aaa(self) -> int:
        return self.seeded.symbols["AAA"]

    def writer(
        self,
        key: str,
        *,
        base: StrategyConfigView,
        params: Mapping[str, Any],
        enabled: bool,
        created_by: str,
    ) -> StrategyConfigView:
        """A stand-in for `StrategyRegistry.create_replay_config` (P5-T4): one `replay` row."""
        with session_scope(self.factory) as s:
            row = m.StrategyConfig(
                strategy_key=key,
                version=base.version,
                revision=base.revision,
                params=dict(params),
                enabled=enabled,
                created_at=self.wall.now(),
                created_by=created_by,
                scope="replay",
            )
            s.add(row)
            s.flush()
            view = StrategyConfigView(
                row.id,
                key,
                row.version,
                row.revision,
                dict(row.params),
                row.enabled,
                row.created_at,
                "replay",
            )
        self.written.append(view)
        return view

    def create(self, request: ReplayRequest, **kw: Any) -> int:
        kw.setdefault("config_writer", self.writer)
        return runner.create_replay(
            self.factory, self.wall, CAL, self.settings, self.registry, request, "web:stephen", **kw
        )

    def count(self, model: Any) -> int:
        with self.factory() as s:
            return int(s.execute(select(func.count()).select_from(model)).scalar_one())


@pytest.fixture
def world(db_factory: sessionmaker[Session]) -> World:
    wall = FixedClock(SAT)
    seeded = seed_replay_world(db_factory, clock=wall)
    return World(
        db_factory, wall, SettingsStore(db_factory, now=wall.now), StrategyRegistry(db_factory, wall), seeded
    )


class _NoCatalysts:
    async def get(self, symbol_ids: Sequence[int], session_date: date) -> Mapping[int, CatalystInfo]:
        return {}


Hook = Callable[[FakeReplayEngine, EngineCall], Any]


@dataclass
class Harness:
    world: World
    hook: Hook | None = None
    bars_until: tuple[int, int] = (16, 0)  # ET time of the last minute bar's end
    markets: list[FakeReplayMarket] = field(default_factory=list)
    engines: list[FakeReplayEngine] = field(default_factory=list)

    def market(self, run: ReplayRun, clock: Any) -> FakeReplayMarket:
        market = FakeReplayMarket(clock)
        market.add_symbol("AAA", self.world.aaa)
        d = run.date_from
        while d <= run.date_to:
            if CAL.is_session(d):
                open_ = CAL.session_open(d)
                end = min(CAL.session_close(d), et(d, *self.bars_until))
                n = int((end - open_) / timedelta(minutes=1))
                closes = [Decimal("10.00") + Decimal(i % 7) / 100 for i in range(n)]
                market.set_minute_bars(self.world.aaa, d, minute_series(open_, closes))
            d += timedelta(days=1)
        self.markets.append(market)
        return market

    def engine(self, run: ReplayRun, clock: Any, market: Any, catalysts: Any) -> FakeReplayEngine:
        engine = FakeReplayEngine(clock, hook=self.hook)
        self.engines.append(engine)
        return engine

    def deps(self) -> runner.ReplayDeps:
        return runner.ReplayDeps(
            self.world.factory,
            self.world.wall,
            CAL,
            self.market,
            lambda run: _NoCatalysts(),
            self.engine,
        )


def _progress(factory: sessionmaker[Session], run_id: int) -> ReplayProgress:
    with factory() as s:
        return ReplayProgress.from_json(s.get(m.Run, run_id).progress)  # type: ignore[union-attr]


def _events(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(s.execute(select(m.EventLog).order_by(m.EventLog.id)).scalars())


# --- 1. create ----------------------------------------------------------------------------------------------
def test_create_writes_a_queued_replay_and_changes_nothing_live(world: World) -> None:
    live_revisions = {k: world.registry.current(k).revision for k in world.registry.keys()}
    run_id = world.create(ReplayRequest(MON_23, TUE_24, overrides={"risk_pct": "0.01"}), app_version="1.2.3")
    with world.factory() as s:
        row = s.get(m.Run, run_id)
        assert row is not None
        assert (row.mode, row.status, row.label) == ("replay", "queued", "replay 2026-11-23..2026-11-24")
        assert row.started_at == row.updated_at == SAT
        assert row.cancel_requested is False and row.finished_at is None
        p = row.params
        audits = list(s.execute(select(m.AuditLog)).scalars())
    assert set(p) == {
        "kind",
        "date_from",
        "date_to",
        "label",
        "data_mode",
        "catalyst_mode",
        "half_spread_bps",
        "settings",
        "overrides",
        "strategies",
        "code_version",
    }
    assert p["kind"] == "replay" and p["code_version"] == "1.2.3" and p["data_mode"] == "full"
    assert p["settings"]["approval_mode"] == "auto"
    assert p["settings"]["risk_pct"] == "0.01"
    assert p["overrides"] == {"risk_pct": "0.01"}
    assert (p["catalyst_mode"], p["half_spread_bps"]) == ("stored", "5")
    assert [(x["key"], x["config_id"], x["scope"]) for x in p["strategies"]] == [
        (k, world.seeded.configs[k], "live") for k in sorted(world.seeded.configs)
    ]
    assert [(a.action, a.actor) for a in audits] == [("replay.start", "web:stephen")]
    assert audits[0].after["run_id"] == run_id and audits[0].after["data_mode"] == "full"
    run = load_replay_run(world.factory, run_id)
    assert run.settings.approval_mode == "auto" and run.settings.risk_pct == Decimal("0.01")
    # nothing live changed
    assert world.settings.load().approval_mode == "manual"
    assert world.count(m.Setting) == 0
    assert {k: world.registry.current(k).revision for k in world.registry.keys()} == live_revisions
    assert world.count(m.StrategyConfig) == 2


# --- 2. override rows ---------------------------------------------------------------------------------------
def _t4_landed() -> bool:
    try:
        StrategyRegistry.create_replay_config(
            None,  # type: ignore[arg-type]
            "x",
            base=None,  # type: ignore[arg-type]
            params={},
            enabled=True,
            created_by="x",
        )
    except NotImplementedError:
        return False
    except Exception:
        return True
    return True


def test_strategy_override_pins_a_replay_scoped_row(world: World) -> None:
    run_id = world.create(
        ReplayRequest(MON_23, TUE_24, strategies={"orb_sip": StrategyOverride(params={"top_n": 5})})
    )
    assert len(world.written) == 1
    with world.factory() as s:
        row = s.get(m.StrategyConfig, world.written[0].id)
        assert row is not None
        assert (row.scope, row.created_by, row.revision) == ("replay", f"replay:{run_id}", 1)
        assert row.params["top_n"] == 5
        live = s.execute(
            select(m.StrategyConfig).where(
                m.StrategyConfig.strategy_key == "orb_sip", m.StrategyConfig.scope == "live"
            )
        ).scalar_one()
        assert (live.revision, live.params["top_n"]) == (1, 20)
    pins = {p.key: p for p in load_replay_run(world.factory, run_id).strategies}
    assert (pins["orb_sip"].config_id, pins["orb_sip"].scope, pins["orb_sip"].params["top_n"]) == (
        row.id,
        "replay",
        5,
    )
    assert (pins["spy_overlay"].scope, pins["spy_overlay"].config_id) == (
        "live",
        world.seeded.configs["spy_overlay"],
    )
    if _t4_landed():  # `_latest` only sees live rows from P5-T4 on
        assert world.registry.current("orb_sip").revision == 1
        assert world.registry.current("orb_sip").params["top_n"] == 20


@pytest.mark.skipif(not _t4_landed(), reason="P5-T4 create_replay_config not on trunk yet")
def test_default_config_writer_is_the_registry(world: World) -> None:
    run_id = world.create(
        ReplayRequest(MON_23, TUE_24, strategies={"orb_sip": StrategyOverride(params={"top_n": 5})}),
        config_writer=None,
    )
    pins = {p.key: p for p in load_replay_run(world.factory, run_id).strategies}
    assert pins["orb_sip"].scope == "replay" and pins["orb_sip"].params["top_n"] == 5
    assert world.registry.current("orb_sip").params["top_n"] == 20


# --- 3. validation ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("request_", "field_", "wall"),
    [
        (ReplayRequest(TUE_24, MON_23), "date_from", SAT),
        (ReplayRequest(MON_23, date(2026, 12, 1)), "date_to", SAT),
        (ReplayRequest(MON_23, date(2026, 11, 30)), "date_to", datetime(2026, 11, 30, 16, 10, tzinfo=ET)),
        (ReplayRequest(date(2026, 9, 5), date(2026, 9, 7)), "date_to", SAT),  # Labor Day weekend
        (ReplayRequest(date(2026, 1, 2), FRI_27), "date_to", SAT),  # > 130 sessions
        (
            ReplayRequest(MON_23, TUE_24, overrides={"approval_mode": "manual"}),
            "overrides.approval_mode",
            SAT,
        ),
        (ReplayRequest(MON_23, TUE_24, overrides={"risk_pct": "0.5"}), "overrides.risk_pct", SAT),
        (
            ReplayRequest(MON_23, TUE_24, strategies={"nope": StrategyOverride(enabled=True)}),
            "strategies.nope",
            SAT,
        ),
        (
            ReplayRequest(MON_23, TUE_24, strategies={"orb_sip": StrategyOverride(params={"top_n": 0})}),
            "strategies.orb_sip.params.top_n",
            SAT,
        ),
        (
            ReplayRequest(MON_23, TUE_24, strategies={"orb_sip": StrategyOverride(enabled=False)}),
            "strategies",
            SAT,
        ),
        (ReplayRequest(MON_23, TUE_24, label="x" * 201), "label", SAT),
    ],
)
def test_invalid_requests_write_nothing(
    world: World, request_: ReplayRequest, field_: str, wall: datetime
) -> None:
    world.wall.set(wall)
    before = [world.count(t) for t in (m.Run, m.AuditLog, m.StrategyConfig, m.Setting)]
    with pytest.raises(ReplayInvalid) as info:
        world.create(request_)
    assert field_ in [loc for loc, _ in info.value.errors]
    assert [world.count(t) for t in (m.Run, m.AuditLog, m.StrategyConfig, m.Setting)] == before


def test_today_is_allowed_from_15_minutes_after_the_close(world: World) -> None:
    world.wall.set(datetime(2026, 11, 30, 16, 16, tzinfo=ET))
    run_id = world.create(ReplayRequest(MON_23, date(2026, 11, 30)))
    assert load_replay_run(world.factory, run_id).date_to == date(2026, 11, 30)


# --- 4. loop order ------------------------------------------------------------------------------------------
def _entry_until_1002(world: World) -> Hook:
    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        if call.method == "run_event" and call.args[0] == "orb_open":
            engine.broker.add_order(world.aaa, submitted_at=call.at)
        if call.method == "on_candles" and call.at == et(call.at.astimezone(ET).date(), 10, 2):
            for order_id in list(engine.broker.orders):
                engine.broker.remove_order(order_id)

    return hook


def _expected_day(d: date, *, bars: bool) -> list[tuple[str, datetime]]:
    close = CAL.session_close(d)
    cal = [("run_event", et(d, 9, 35, 5)), ("tick", et(d, 9, 35, 5))]
    if bars:
        for minute in range(36, 63):
            t = et(d, 9, 0) + timedelta(minutes=minute)
            cal += [("on_candles", t), ("tick", t)]
    events = [et(d, 11, 30), close - timedelta(minutes=30), close - timedelta(minutes=10)]
    for t in events:
        cal += [("run_event", t), ("tick", t)]
    return [*cal, ("end_of_session", close)]


async def test_loop_visits_bars_then_events_then_tick(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, MON_23))
    h = Harness(world, hook=_entry_until_1002(world))
    final = await runner.run_replay(h.deps(), run_id)
    assert final.status == "completed"
    calls = h.engines[0].calls
    assert [(c.method, c.at) for c in calls] == _expected_day(MON_23, bars=True)
    keys = [c.args[0] for c in calls if c.method == "run_event"]
    assert keys == ["orb_open", "entry_cancel", "overlay_decision", "flatten"]
    for c in calls:  # every call carries the clock time of its visit
        if c.method == "on_candles":
            (bars, now) = c.args
            assert now == c.at and list(bars) == [world.aaa] and bars[world.aaa].end == c.at
        if c.method == "tick":
            assert c.args == (c.at,)
    market = h.markets[0]
    assert market.prepared == [MON_23]
    assert market.load_calls == [((world.aaa,), MON_23)]  # loaded once, before the first bar


async def test_bars_come_before_the_event_of_the_same_instant(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, MON_23))

    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        if call.method == "run_event" and call.args[0] == "orb_open":
            engine.broker.add_order(world.aaa, submitted_at=call.at)
        if call.method == "run_event" and call.args[0] == "entry_cancel":
            for order_id in list(engine.broker.orders):
                engine.broker.remove_order(order_id)

    h = Harness(world, hook=hook)
    await runner.run_replay(h.deps(), run_id)
    at_1130 = [c.method for c in h.engines[0].calls if c.at == et(MON_23, 11, 30)]
    assert at_1130 == ["on_candles", "run_event", "tick"]
    after = [c.method for c in h.engines[0].calls if et(MON_23, 11, 30) < c.at < et(MON_23, 15, 30)]
    assert after == []  # nothing working after the cancel: no minute steps


async def test_loop_follows_the_early_close(world: World) -> None:
    run_id = world.create(ReplayRequest(FRI_27, FRI_27))
    h = Harness(world)
    await runner.run_replay(h.deps(), run_id)
    calls = [(c.method, c.at) for c in h.engines[0].calls]
    assert CAL.session_close(FRI_27) == et(FRI_27, 13, 0)
    assert calls == _expected_day(FRI_27, bars=False)
    assert [c.args[0] for c in h.engines[0].calls if c.method == "run_event"] == [
        "orb_open",
        "entry_cancel",
        "overlay_decision",
        "flatten",
    ]


# --- 5. determinism -----------------------------------------------------------------------------------------
async def test_two_runs_of_the_same_request_match(world: World) -> None:
    results = []
    for _ in range(2):
        run_id = world.create(ReplayRequest(MON_23, WED_25))
        h = Harness(world, hook=_entry_until_1002(world))
        final = await runner.run_replay(h.deps(), run_id)
        assert final.status == "completed"
        results.append(([(c.method, c.at, c.args) for c in h.engines[0].calls], final.progress.to_json()))
    assert results[0] == results[1]
    assert results[0][1]["sessions_done"] == 3


# --- 6. progress and cancel ---------------------------------------------------------------------------------
async def test_progress_after_every_session_and_cancel_during_day_2(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, WED_25))
    seen: list[tuple[date, int]] = []

    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        if call.method == "run_event" and call.args[0] == "orb_open":
            day = call.args[1]
            seen.append((day, _progress(world.factory, run_id).sessions_done))
            if day == TUE_24:
                assert runner.request_cancel(world.factory, world.wall, run_id, "web:stephen")

    h = Harness(world, hook=hook)
    final = await runner.run_replay(h.deps(), run_id)
    assert seen == [(MON_23, 0), (TUE_24, 1)]
    assert final.status == "cancelled" and final.finished_at is not None
    assert (final.progress.sessions_total, final.progress.sessions_done) == (3, 2)
    assert final.progress.current_date == TUE_24
    assert h.markets[0].prepared == [MON_23, TUE_24]
    with world.factory() as s:
        actions = list(s.execute(select(m.AuditLog.action).order_by(m.AuditLog.id)).scalars())
    assert actions == ["replay.start", "replay.cancel"]


def test_cancel_a_queued_run_at_once(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, TUE_24))
    assert runner.request_cancel(world.factory, world.wall, run_id, "cli") is True
    run = load_replay_run(world.factory, run_id)
    assert (run.status, run.cancel_requested, run.finished_at) == ("cancelled", True, SAT)
    assert runner.request_cancel(world.factory, world.wall, run_id, "cli") is False
    assert runner.request_cancel(world.factory, world.wall, world.seeded.live_run_id, "cli") is False
    assert runner.request_cancel(world.factory, world.wall, 999_999, "cli") is False


async def test_a_cancelled_run_is_not_started(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, TUE_24))
    runner.request_cancel(world.factory, world.wall, run_id, "cli")
    h = Harness(world)
    final = await runner.run_replay(h.deps(), run_id)
    assert final.status == "cancelled" and h.engines == []


# --- 7. failure ---------------------------------------------------------------------------------------------
async def test_a_failing_engine_fails_the_run_once_and_releases_the_lock(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, WED_25))

    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        if call.method == "run_event" and call.args[1] == TUE_24:
            raise RuntimeError("boom\nsecond line password=hunter2")

    h = Harness(world, hook=hook)
    final = await runner.run_replay(h.deps(), run_id)
    assert final.status == "failed" and final.finished_at is not None
    assert final.error is not None
    assert "\n" not in final.error and "hunter2" not in final.error
    assert final.error.startswith("RuntimeError: boom second line")
    assert final.progress.sessions_done == 1
    events = _events(world.factory)
    errors = [e for e in events if e.level == "error"]
    assert len(errors) == 1 and errors[0].run_id == run_id and errors[0].source == "replay"
    assert all(e.run_id == run_id for e in events)
    # the lock is free: another run starts and completes
    second = world.create(ReplayRequest(MON_23, MON_23))
    assert (await runner.run_replay(Harness(world).deps(), second)).status == "completed"


async def test_a_run_writes_its_account_at_the_first_open(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, MON_23, overrides={"starting_cash": "1000"}))
    await runner.run_replay(Harness(world).deps(), run_id)
    with world.factory() as s:
        acct = s.execute(select(m.SimAccount).where(m.SimAccount.run_id == run_id)).scalar_one()
        ledger = s.execute(select(m.CashLedger).where(m.CashLedger.run_id == run_id)).scalar_one()
    assert acct.starting_cash == Decimal("1000") and acct.created_at == et(MON_23, 9, 29)
    assert (ledger.kind, ledger.ts) == ("deposit", et(MON_23, 9, 29))


# --- 9. busy and abandoned ----------------------------------------------------------------------------------
@pytest.fixture
def held_lock(db_factory: sessionmaker[Session]) -> Iterator[Callable[[], None]]:
    bind = db_factory.kw["bind"]
    assert isinstance(bind, Engine)
    conn = bind.connect()
    conn.execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": runner.REPLAY_LOCK_KEY})
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": runner.REPLAY_LOCK_KEY})
            released = True

    yield release
    release()
    conn.close()


def _insert_run(world: World, status: str, started: datetime) -> int:
    with session_scope(world.factory) as s:
        row = m.Run(mode="replay", started_at=started, params={"kind": "replay"}, status=status, label="x")
        s.add(row)
        s.flush()
        return row.id


async def test_busy_while_the_lock_is_held(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, MON_23))
    bind = world.factory.kw["bind"]
    with bind.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": runner.REPLAY_LOCK_KEY})
        try:
            with pytest.raises(ReplayBusy):
                await runner.run_replay(Harness(world).deps(), run_id)
            with world.factory() as s:  # the queued row is recent: create refuses on the lock
                s.execute(text("UPDATE trader.runs SET status = 'completed' WHERE id = :i"), {"i": run_id})
                s.commit()
            with pytest.raises(ReplayBusy):
                world.create(ReplayRequest(MON_23, MON_23))
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": runner.REPLAY_LOCK_KEY})


def test_busy_while_another_replay_is_queued(world: World) -> None:
    world.create(ReplayRequest(MON_23, MON_23))
    before = world.count(m.Run)
    with pytest.raises(ReplayBusy):
        world.create(ReplayRequest(MON_23, MON_23))
    assert world.count(m.Run) == before


def test_reconcile_abandoned(world: World, held_lock: Callable[[], None]) -> None:
    running = _insert_run(world, "running", SAT - timedelta(minutes=10))
    old = _insert_run(world, "queued", SAT - timedelta(minutes=3))
    recent = _insert_run(world, "queued", SAT - timedelta(seconds=30))
    assert runner.reconcile_abandoned(world.factory, world.wall) == []  # locked: a live runner owns them
    with world.factory() as s:
        assert s.get(m.Run, running).status == "running"  # type: ignore[union-attr]
    held_lock()
    assert runner.reconcile_abandoned(world.factory, world.wall) == [running, old]
    with world.factory() as s:
        rows = {r.id: r for r in s.execute(select(m.Run).where(m.Run.mode == "replay")).scalars()}
    assert (rows[running].status, rows[running].error, rows[running].finished_at) == (
        "failed",
        "abandoned",
        SAT,
    )
    assert (rows[old].status, rows[old].error) == ("failed", "abandoned")
    assert rows[recent].status == "queued"
    assert runner.reconcile_abandoned(world.factory, world.wall) == []


# --- 10. data mode ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("wall", "offline", "mode"),
    [
        (datetime(2026, 11, 30, 10, 0, tzinfo=ET), False, "offline"),  # a session day, market hours
        (datetime(2026, 11, 30, 9, 15, tzinfo=ET), False, "offline"),
        (datetime(2026, 11, 30, 9, 14, tzinfo=ET), False, "full"),
        (datetime(2026, 11, 30, 17, 0, tzinfo=ET), False, "full"),
        (SAT, False, "full"),
        (SAT, True, "offline"),
    ],
)
def test_data_mode(world: World, wall: datetime, offline: bool, mode: str) -> None:
    world.wall.set(wall)
    run_id = world.create(ReplayRequest(MON_23, TUE_24, offline=offline))
    assert load_replay_run(world.factory, run_id).data_mode == mode


# --- 11. forced close ---------------------------------------------------------------------------------------
async def test_forced_close_at_the_last_close(world: World) -> None:
    run_id = world.create(ReplayRequest(MON_23, TUE_24))
    positions_at_open: list[int] = []

    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        broker = engine.broker
        if call.method == "run_event" and call.args[0] == "orb_open":
            positions_at_open.append(len(broker.open_positions()))
            if call.args[1] == MON_23:
                pid = broker.add_position(world.aaa, qty=10, avg_price="10.00")
                broker.add_order(
                    world.aaa, purpose="stop", stop="9.50", position_id=pid, submitted_at=call.at
                )
        if call.method == "on_candles":
            bars: dict[int, Candle] = call.args[0]
            exits = [o for o in broker.orders.values() if o.purpose == "exit"]
            if exits and world.aaa in bars:  # the forced exit fills on this bar
                for o in exits:
                    broker.remove_order(o.id)
                    broker.positions.pop(o.position_id or 0, None)

    h = Harness(world, hook=hook, bars_until=(15, 40))
    final = await runner.run_replay(h.deps(), run_id)
    assert final.status == "completed"
    broker = h.engines[0].broker
    close = et(MON_23, 16, 0)
    assert broker.cancelled == [(1, "replay_forced_close")]
    [(exit_id, spec)] = broker.submitted
    assert (spec.purpose, spec.order_type, spec.side, spec.qty, spec.reason) == (
        "exit",
        "market",
        "sell",
        10,
        "replay_forced_close",
    )
    day1 = [c for c in h.engines[0].calls if c.at.astimezone(ET).date() == MON_23]
    # the stop keeps the loop stepping every minute; no bars after 15:40, so no on_candles until the close
    candle_calls = [c for c in day1 if c.method == "on_candles"]
    assert candle_calls[-2].at == et(MON_23, 15, 40)
    last = candle_calls[-1]
    assert last.at == close
    bar = last.args[0][world.aaa]
    last_close = h.markets[0].minute[(world.aaa, MON_23)][-1].close
    assert (bar.start, bar.end, bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        close - timedelta(minutes=1),
        close,
        last_close,
        last_close,
        last_close,
        last_close,
        1,
    )
    forced_visit = [(c.method, c.at) for c in day1 if c.at == et(MON_23, 15, 59)]
    assert forced_visit == [("tick", et(MON_23, 15, 59))]
    assert positions_at_open == [0, 0]
    assert final.progress.forced_closes == 1


# --- fix round 1 --------------------------------------------------------------------------------------------
def _exit_fills_on_volume(world: World) -> Hook:
    """The forced exit fills on a bar only when the bar has volume (as the candle fill model does)."""

    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        broker = engine.broker
        if call.method == "run_event" and call.args[0] == "orb_open" and call.args[1] == MON_23:
            pid = broker.add_position(world.aaa, qty=10, avg_price="10.00")
            broker.add_order(world.aaa, purpose="stop", stop="9.50", position_id=pid, submitted_at=call.at)
        if call.method == "on_candles":
            bar: Candle | None = call.args[0].get(world.aaa)
            if bar is not None and bar.volume > 0:
                for o in [o for o in broker.orders.values() if o.purpose == "exit"]:
                    broker.remove_order(o.id)
                    broker.positions.pop(o.position_id or 0, None)

    return hook


async def test_forced_exit_not_filled_by_a_zero_volume_close_bar_gets_the_synthetic_bar(world: World) -> None:
    """Breaker test 11's case with fakes: every bar from 15:40 has zero volume, so the real 16:00 bar can't
    fill the forced exit; the close-out then applies the synthetic bar at the last close."""
    run_id = world.create(ReplayRequest(MON_23, MON_23))
    h = Harness(world, hook=_exit_fills_on_volume(world))

    def market(run: ReplayRun, clock: Any) -> FakeReplayMarket:
        mk = FakeReplayMarket(clock)
        mk.add_symbol("AAA", world.aaa)
        bars = minute_series(CAL.session_open(MON_23), ["10.00"] * 390)
        zero_from = et(MON_23, 15, 40)
        mk.set_minute_bars(
            world.aaa, MON_23, [replace(b, volume=0) if b.start >= zero_from else b for b in bars]
        )
        h.markets.append(mk)
        return mk

    deps = replace(h.deps(), market_factory=market)
    final = await runner.run_replay(deps, run_id)
    assert final.status == "completed" and final.progress.forced_closes == 1
    engine = h.engines[0]
    at_close = [c for c in engine.calls if c.method == "on_candles" and c.at == et(MON_23, 16, 0)]
    assert [c.args[0][world.aaa].volume for c in at_close] == [0, 1]  # the real bar, then the synthetic one
    assert at_close[1].args[0][world.aaa].close == Decimal("10.00")
    assert engine.broker.open_positions() == []


class _TargetingEngine(FakeReplayEngine):
    """A fake engine with `Engine.on_candles_for`: it fills the named orders (exits) at the bar's open."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.targeted: list[tuple[datetime, dict[int, Candle], list[int]]] = []

    async def on_candles_for(
        self, candles: Mapping[int, Candle], now: datetime, orders: Sequence[int]
    ) -> list[Any]:
        self.targeted.append((self.clock.now(), dict(candles), list(orders)))
        for order_id in orders:
            order = self.broker.orders.pop(order_id)
            self.broker.positions.pop(order.position_id or 0, None)
        return []


async def test_a_position_opened_by_the_close_bar_is_force_closed_at_the_close(world: World) -> None:
    """`no_entry_before_close_minutes = 0`: nothing is open at 15:59, then the 15:59-16:00 bar fills an entry.
    The re-check at the close force-closes it at once through `on_candles_for` (its exit is submitted at the
    close, so no bar ends after it), and nothing is carried into the next session."""
    run_id = world.create(ReplayRequest(MON_23, TUE_24))

    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        broker = engine.broker
        if call.method == "run_event" and call.args[0] == "orb_open" and call.args[1] == MON_23:
            broker.add_order(world.aaa, submitted_at=call.at)  # an entry working all day
        if call.method == "on_candles" and call.at == et(MON_23, 16, 0) and broker.orders:
            for order_id in list(broker.orders):
                broker.remove_order(order_id)
            broker.add_position(world.aaa, qty=10, avg_price="10.05")

    h = Harness(world, hook=hook)
    engines: list[_TargetingEngine] = []

    def engine_factory(run: ReplayRun, clock: Any, market: Any, catalysts: Any) -> _TargetingEngine:
        engine = _TargetingEngine(clock, hook=hook)
        engines.append(engine)
        return engine

    final = await runner.run_replay(replace(h.deps(), engine_factory=engine_factory), run_id)
    assert final.status == "completed" and final.progress.forced_closes == 1
    engine = engines[0]
    [(exit_id, spec)] = engine.broker.submitted
    assert (spec.reason, spec.order_type, spec.qty) == ("replay_forced_close", "market", 10)
    [(at, bars, orders)] = engine.targeted
    last_close = h.markets[0].minute[(world.aaa, MON_23)][-1].close
    assert (at, orders, bars[world.aaa].close, bars[world.aaa].end) == (
        et(MON_23, 16, 0),
        [exit_id],
        last_close,
        et(MON_23, 16, 0),
    )
    assert engine.broker.open_positions() == []
    ends = [c for c in engine.calls if c.method == "end_of_session"]
    assert [c.at for c in ends] == [et(MON_23, 16, 0), et(TUE_24, 16, 0)]


async def test_a_pre_open_event_of_day_1_runs_at_its_own_time(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1: the replay clock starts at day 1's first event when it is before open - 1 minute."""

    class EarlyOrb(OrbSip):
        def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
            return [ScheduledEvent("early_probe", SessionOffset.parse("open-30m")), *super().schedule(cal)]

    plugins = {**load_all(), "orb_sip": EarlyOrb}
    monkeypatch.setattr(runner, "load_all", lambda: plugins)
    run_id = world.create(ReplayRequest(MON_23, TUE_24))
    h = Harness(world)
    assert (await runner.run_replay(h.deps(), run_id)).status == "completed"
    early = [c.at for c in h.engines[0].calls if c.method == "run_event" and c.args[0] == "early_probe"]
    assert early == [et(MON_23, 9, 0), et(TUE_24, 9, 0)]
    with world.factory() as s:
        acct = s.execute(select(m.SimAccount).where(m.SimAccount.run_id == run_id)).scalar_one()
    assert acct.created_at == et(MON_23, 9, 0)


def test_reconcile_and_create_never_take_the_lock(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix round 1: they read pg_locks instead, so a runner starting at that moment never sees ReplayBusy."""

    def refuse(_factory: Any) -> None:
        raise AssertionError("took the replay lock")

    monkeypatch.setattr(runner, "_acquire", refuse)
    _insert_run(world, "running", SAT - timedelta(minutes=10))
    assert len(runner.reconcile_abandoned(world.factory, world.wall)) == 1
    run_id = world.create(ReplayRequest(MON_23, MON_23))
    assert load_replay_run(world.factory, run_id).status == "queued"


def test_lock_held_reads_pg_locks(world: World, held_lock: Callable[[], None]) -> None:
    with world.factory() as s:
        assert runner._lock_held(s) is True
    held_lock()
    with world.factory() as s:
        assert runner._lock_held(s) is False
    conn = runner._acquire(world.factory)
    assert conn is not None
    try:
        with world.factory() as s:
            assert runner._lock_held(s) is True
    finally:
        runner._release(conn)


def test_a_runner_starting_while_reconcile_runs_is_never_busy(world: World) -> None:
    """The race of the review: the list route's `reconcile_abandoned` and a runner's lock acquisition at the
    same moment. Hammered from another thread, the runner always gets the lock (the old reconcile took it
    for a moment, so the runner could fail with ReplayBusy)."""
    stop = threading.Event()
    errors: list[BaseException] = []

    def reconcile_loop() -> None:
        try:
            while not stop.is_set():
                runner.reconcile_abandoned(world.factory, world.wall)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    worker = threading.Thread(target=reconcile_loop)
    worker.start()
    try:
        for _ in range(150):
            conn = runner._acquire(world.factory)
            assert conn is not None, "a reconcile held the replay lock"
            runner._release(conn)
    finally:
        stop.set()
        worker.join()
    assert errors == []


# --- fix round 1: a biased day through the real composition -------------------------------------------------
async def test_real_orb_sip_trades_a_biased_day(db_factory: sessionmaker[Session]) -> None:
    """MON has no universe snapshot (the only one is WED's, a later date): the members' price, average volume
    and ATR come from the daily bars before MON, so the real orb_sip, through the real ReplayData, still
    screens the names and emits an entry intent."""
    lookback = CAL.sessions_before(MON_23, 14)
    open_bars = [
        candle(CAL.session_open(d), "10.00", "10.30", "9.90", "10.10", 10_000, minutes=5) for d in lookback
    ]
    open_bars.append(candle(CAL.session_open(MON_23), "10.00", "10.50", "9.95", "10.40", 50_000, minutes=5))
    days = [d for d in (MON_23 - timedelta(days=n) for n in range(45, 0, -1)) if CAL.is_session(d)]
    daily = [(d, _day_bar(d, "10.00")) for d in days]
    w = seed_replay_world(
        db_factory,
        tickers=("SPY", "AAA"),
        universe_days=[WED_25],
        universe_tickers=["AAA"],
        archive={("AAA", "5m"): open_bars},
        daily={"AAA": daily},
    )
    wall = FixedClock(SAT)
    run_id = runner.create_replay(
        db_factory,
        wall,
        CAL,
        SettingsStore(db_factory, now=wall.now),
        StrategyRegistry(db_factory, wall),
        ReplayRequest(
            MON_23,
            MON_23,
            strategies={"orb_sip": StrategyOverride(params={"require_catalyst": False})},
            offline=True,
        ),
        "test",
    )
    core = SimpleNamespace(
        factory=db_factory,
        clock=wall,
        calendar=CAL,
        settings=SettingsStore(db_factory, now=wall.now),
        crypto=None,
    )
    async with runner.open_replay_deps(cast(Any, core), data_mode="offline") as deps:
        final = await runner.run_replay(deps, run_id)
    assert final.status == "completed", final.error
    assert final.progress.biased_days == (MON_23,)
    with db_factory() as s:
        signals = list(
            s.execute(
                select(m.Signal).where(m.Signal.run_id == run_id, m.Signal.symbol_id == w.symbols["AAA"])
            ).scalars()
        )
        entries = list(
            s.execute(
                select(m.Order).where(
                    m.Order.run_id == run_id,
                    m.Order.symbol_id == w.symbols["AAA"],
                    m.Order.purpose == "entry",
                )
            ).scalars()
        )
    orb = [sg for sg in signals if sg.event_key == "orb_open"]
    assert [sg.session_date for sg in orb] == [MON_23]  # (the entry is later cancelled by entry_cancel)
    assert "rejection" not in orb[0].evidence, orb[0].evidence
    assert len(entries) == 1 and entries[0].stop_price == Decimal("10.51")


def _day_bar(d: date, close: str) -> Candle:
    start = datetime.combine(d, time(0), tzinfo=ET)
    c = Decimal(close)
    return Candle(
        start, start + timedelta(days=1), c, c + Decimal("0.75"), c - Decimal("0.75"), c, 2_000_000, None
    )
