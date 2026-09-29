"""P5-T1 acceptance tests 3, 5 and 6: the Phase 5 backend contracts that T2-T16 build against.

The typed assignments in `_structural_checks` are what mypy verifies; the runtime tests pin every public name
T1 created, so a builder who renames a contract breaks this test. The owning tasks replace the stubs' bodies,
never their names. The stub checks only require that a stub which still raises `NotImplementedError` names its
owner, so a task that implements its stubs never needs to edit this file (the dummy arguments here may then
make the real code fail in some other way, which these checks accept).
"""

import dataclasses
import importlib
import inspect
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_api import FakeReplayLauncher, make_services, test_core
from tests.fakes_replay import (
    FakeReplayBroker,
    FakeReplayEngine,
    FakeReplayMarket,
    candle,
    minute_series,
    seed_replay_world,
)
from tests.fakes_telegram import FakeRenderer
from trader.adapters.claude.reports import CommentaryWriter
from trader.api.deps import ReplayLauncher
from trader.api.replay_launcher import SubprocessReplayLauncher
from trader.api.routers import ROUTERS
from trader.broker.fill_model import FillParams
from trader.broker.sim_broker import SimBroker
from trader.broker.types import FillModel
from trader.db import models as m
from trader.engine.orchestrator import Engine
from trader.engine.proposals import ProposalService
from trader.jobs import runner
from trader.jobs.runner import RetryPolicy, run_job, run_job_async
from trader.jobs.weekly import WeeklyDeps, run_weekly
from trader.logging_mirror import EventLogMirror, install_event_mirror
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, FixedClock
from trader.notify.messages import MessageRenderer
from trader.notify.types import DailySummaryView, Renderer, RunToDateView, WeeklyReportView
from trader.replay import runner as replay_runner
from trader.replay import setup as replay_setup
from trader.replay.candle_fill_model import CandleFillModel
from trader.replay.catalysts import ReplayCatalysts
from trader.replay.clock import ReplayClock
from trader.replay.data import ReplayData
from trader.replay.types import (
    REPLAY_OVERRIDE_KEYS,
    PinnedStrategy,
    ReplayBroker,
    ReplayEngine,
    ReplayInvalid,
    ReplayMarket,
    ReplayNotFound,
    ReplayProgress,
    ReplayRequest,
    StrategyOverride,
    load_replay_run,
)
from trader.reports import metrics, weekly
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import CatalystSource
from trader.strategies.registry import StrategyConfigView, StrategyRegistry

T0 = datetime(2026, 11, 24, 14, 35, tzinfo=UTC)  # 09:35 ET, Tuesday
DAY = date(2026, 11, 24)
CAL = SessionCalendar()

CONTRACT_NAMES: dict[str, set[str]] = {
    "trader.replay": set(),
    "trader.replay.types": {
        "ReplayStatus",
        "DataMode",
        "CatalystMode",
        "REPLAY_OVERRIDE_KEYS",
        "StrategyOverride",
        "ReplayRequest",
        "PinnedStrategy",
        "ReplayProgress",
        "ReplayRun",
        "load_replay_run",
        "ReplayInvalid",
        "ReplayBusy",
        "ReplayNotFound",
        "ReplayBroker",
        "ReplayEngine",
        "ReplayMarket",
    },
    "trader.replay.clock": {"ReplayClock"},
    "trader.replay.candle_fill_model": {"CandleFillModel", "CANDLE_SNAPSHOT_SOURCE"},
    "trader.replay.data": {"ReplayData", "BIASED_SOURCE"},
    "trader.replay.catalysts": {"ReplayCatalysts", "UNKNOWN_REASON"},
    "trader.replay.setup": {"SnapshotSettings", "PinnedRegistry", "build_replay_engine"},
    "trader.replay.runner": {
        "REPLAY_LOCK",
        "create_replay",
        "ReplayDeps",
        "run_replay",
        "request_cancel",
        "reconcile_abandoned",
        "open_replay_deps",
    },
    "trader.reports.metrics": {
        "TradeRow",
        "HistogramBin",
        "Metrics",
        "R_LOW",
        "R_HIGH",
        "R_BINS",
        "metrics_from_rows",
        "r_histogram",
        "max_drawdown",
        "compute_metrics",
    },
    "trader.reports.weekly": {
        "WeekWindow",
        "week_window",
        "last_completed_week",
        "build_facts",
        "check_numbers",
        "claude_spent",
        "upsert_report",
        "CommentaryOutcome",
    },
    "trader.adapters.claude.reports": {
        "COMMENTARY_SYSTEM_PROMPT",
        "MAX_COMMENTARY_TOKENS",
        "Commentary",
        "CommentaryWriter",
    },
    "trader.jobs.weekly": {"WeeklyDeps", "run_weekly"},
    "trader.logging_mirror": {"MIRROR_SOURCE_PREFIX", "EventLogMirror", "install_event_mirror"},
    "trader.api.replay_launcher": {"SubprocessReplayLauncher"},
    "trader.api.routers.replays": {"router"},
    "trader.api.routers.reports": {"router"},
    "trader.api.deps": {"ReplayLauncher"},
    "trader.api.views": {"metrics_out"},
    "trader.api.schemas": {
        "ReplayStatus",
        "DataMode",
        "CatalystMode",
        "CommentaryStatus",
        "ReplayStrategyIn",
        "ReplayIn",
        "ReplayStrategyOut",
        "ReplayProgressOut",
        "ReplaySummaryOut",
        "ReplayOut",
        "ReplayOptionsOut",
        "WeeklyReportOut",
    },
    "trader.notify.types": {"RunToDateView", "WeeklyReportView"},
    "trader.jobs.runner": {"RetryPolicy"},
}


@pytest.mark.parametrize("module", sorted(CONTRACT_NAMES))
def test_every_contract_module_imports_with_its_names(module: str) -> None:
    mod = importlib.import_module(module)
    assert sorted(name for name in CONTRACT_NAMES[module] if not hasattr(mod, name)) == []


def test_routers_are_seventeen_with_replays_and_reports_before_stream() -> None:
    assert len(ROUTERS) == 20  # P6-T12 added decisions, DB-T1 live and control (all before stream)
    tags = [r.tags[0] for r in ROUTERS]
    assert tags[-6:] == ["replays", "reports", "decisions", "live", "control", "stream"]
    for name in ("replays", "reports"):
        assert importlib.import_module(f"trader.api.routers.{name}").router.tags == [name]


# --- test 5: structural checks (mypy) -----------------------------------------------------------------------


def _structural_checks(
    fake_launcher: FakeReplayLauncher,
    real_launcher: SubprocessReplayLauncher,
    fake_market: FakeReplayMarket,
    real_market: ReplayData,
    fake_engine: FakeReplayEngine,
    real_engine: Engine,
    fake_broker: FakeReplayBroker,
    real_broker: SimBroker,
    renderer: MessageRenderer,
    fake_renderer: FakeRenderer,
    replay_clock: ReplayClock,
    candle_model: CandleFillModel,
    catalysts: ReplayCatalysts,
) -> None:
    """Never called: mypy checks these assignments (the protocols are satisfied structurally)."""
    a: ReplayLauncher = fake_launcher
    b: ReplayLauncher = real_launcher
    c: ReplayMarket = fake_market
    d: ReplayMarket = real_market
    e: ReplayEngine = fake_engine
    f: ReplayEngine = real_engine
    g: ReplayBroker = fake_broker
    h: ReplayBroker = real_broker
    i: Renderer = renderer
    j: Renderer = fake_renderer
    k: Clock = replay_clock
    l_: FillModel = candle_model
    n: CatalystSource = catalysts
    del a, b, c, d, e, f, g, h, i, j, k, l_, n


# --- test 5: every stub raises NotImplementedError naming its owner -----------------------------------------


FILL_PARAMS = FillParams.from_settings(RuntimeSettings())
CLOCK = FixedClock(T0)
BAR = candle(T0, "10", "10.5", "9.9", "10.2")


def _stubs() -> list[tuple[str, str, Callable[[], Any]]]:
    """(owner, label, a call that should raise NotImplementedError(owner))."""
    rc = ReplayClock(T0)
    model = CandleFillModel(FILL_PARAMS, Decimal("5"))
    none: Any = None
    data = ReplayData(
        none,
        CLOCK,
        CLOCK,
        CAL,
        None,
        run_id=1,
        date_from=DAY,
        date_to=DAY,
        half_spread_bps=Decimal("5"),
        questrade_window_days=85,
        lookback_sessions=14,
    )
    launcher = SubprocessReplayLauncher(none, CLOCK)
    mirror = EventLogMirror(none, CLOCK, process="test", level="error", max_per_minute=30)
    snapshot = replay_setup.SnapshotSettings(RuntimeSettings())
    writer = CommentaryWriter(None, RuntimeSettings)
    week = weekly.WeekWindow(DAY, DAY, (DAY,), DAY)
    return [
        ("P5-T2", "metrics_from_rows", lambda: metrics.metrics_from_rows(1, None, None, [], [], [])),
        ("P5-T2", "r_histogram", lambda: metrics.r_histogram([])),
        ("P5-T2", "max_drawdown", lambda: metrics.max_drawdown([])),
        ("P5-T2", "compute_metrics", lambda: metrics.compute_metrics(none, 1)),
        ("P5-T3", "ReplayClock.now", rc.now),
        ("P5-T3", "ReplayClock.set", lambda: rc.set(T0)),
        ("P5-T3", "ReplayClock.advance", lambda: rc.advance(timedelta(seconds=1))),
        ("P5-T3", "CandleFillModel.slip", lambda: model.slip(Decimal(10))),
        ("P5-T3", "CandleFillModel.half_spread", lambda: model.half_spread(Decimal(10))),
        ("P5-T3", "CandleFillModel.fees", lambda: model.fees("buy", 1, Decimal(10))),
        ("P5-T3", "CandleFillModel.assess", lambda: model.assess(none, BAR, T0)),
        ("P5-T3", "CandleFillModel.evaluate", lambda: model.evaluate(none, BAR, T0)),
        ("P5-T4", "SimBroker.on_candles", lambda: SimBroker.on_candles(none, {}, T0)),
        ("P5-T4", "Engine.on_candles", lambda: Engine.on_candles(none, {}, T0)),
        (
            "P5-T4",
            "StrategyRegistry.create_replay_config",
            lambda: StrategyRegistry.create_replay_config(
                none, "orb_sip", base=none, params={}, enabled=True, created_by="replay:1"
            ),
        ),
        ("P5-T5", "ReplayData.universe", lambda: data.universe(DAY)),
        ("P5-T5", "ReplayData.universe_status", lambda: data.universe_status(DAY)),
        ("P5-T5", "ReplayData.open_bar_stats", lambda: data.open_bar_stats(DAY)),
        ("P5-T5", "ReplayData.opening_bars", lambda: data.opening_bars(DAY)),
        ("P5-T5", "ReplayData.quotes", lambda: data.quotes([1])),
        ("P5-T5", "ReplayData.candles", lambda: data.candles(1, T0, T0, "OneMinute")),
        ("P5-T5", "ReplayData.prior_close", lambda: data.prior_close(1, DAY)),
        ("P5-T5", "ReplayData.prior_closes", lambda: data.prior_closes([1], DAY)),
        ("P5-T5", "ReplayData.symbol_ids", lambda: data.symbol_ids(["AAA"])),
        ("P5-T5", "ReplayData.prepare_day", lambda: data.prepare_day(DAY)),
        ("P5-T5", "ReplayData.load_minute_bars", lambda: data.load_minute_bars([1], DAY)),
        ("P5-T5", "ReplayData.bar_ending_at", lambda: data.bar_ending_at(1, T0)),
        ("P5-T5", "ReplayData.last_close", lambda: data.last_close(1, T0)),
        ("P5-T5", "ReplayData.progress_counts", data.progress_counts),
        ("P5-T5", "ReplayData.biased_days", lambda: data.biased_days),
        ("P5-T5", "ReplayCatalysts.get", lambda: ReplayCatalysts(none, "stored").get([1], DAY)),
        ("P5-T6", "SnapshotSettings.load", snapshot.load),
        ("P5-T6", "SnapshotSettings.set", lambda: snapshot.set("risk_pct", "0.01", "test")),
        ("P5-T6", "PinnedRegistry.keys", lambda: replay_setup.PinnedRegistry.keys(none)),
        ("P5-T6", "PinnedRegistry.current", lambda: replay_setup.PinnedRegistry.current(none, "orb_sip")),
        (
            "P5-T6",
            "PinnedRegistry.update",
            lambda: replay_setup.PinnedRegistry.update(none, "orb_sip", actor="x"),
        ),
        (
            "P5-T6",
            "PinnedRegistry.ensure_defaults",
            lambda: replay_setup.PinnedRegistry.ensure_defaults(none),
        ),
        (
            "P5-T6",
            "build_replay_engine",
            lambda: replay_setup.build_replay_engine(none, rc, CAL, none, none, none, none),
        ),
        (
            "P5-T6",
            "create_replay",
            lambda: replay_runner.create_replay(none, CLOCK, CAL, none, none, ReplayRequest(DAY, DAY), "cli"),
        ),
        ("P5-T6", "run_replay", lambda: replay_runner.run_replay(none, 1)),
        ("P5-T6", "request_cancel", lambda: replay_runner.request_cancel(none, CLOCK, 1, "cli")),
        ("P5-T6", "reconcile_abandoned", lambda: replay_runner.reconcile_abandoned(none, CLOCK)),
        (
            "P5-T6",
            "open_replay_deps",
            lambda: _enter(replay_runner.open_replay_deps(none, data_mode="offline")),
        ),
        ("P5-T7", "SubprocessReplayLauncher.launch", lambda: launcher.launch(1)),
        ("P5-T7", "SubprocessReplayLauncher.running", launcher.running),
        ("P5-T9", "week_window", lambda: weekly.week_window(CAL, DAY)),
        ("P5-T9", "last_completed_week", lambda: weekly.last_completed_week(CAL, DAY)),
        ("P5-T9", "build_facts", lambda: weekly.build_facts(none, CAL, 1, week)),
        ("P5-T9", "check_numbers", lambda: weekly.check_numbers("4 trades", {})),
        ("P5-T9", "claude_spent", lambda: weekly.claude_spent(none, DAY)),
        (
            "P5-T9",
            "upsert_report",
            lambda: weekly.upsert_report(
                none, CLOCK, week, 1, {}, weekly.CommentaryOutcome("ok", "x", None, None, 0, 0, Decimal(0))
            ),
        ),
        ("P5-T9", "CommentaryWriter.write", lambda: writer.write({})),
        ("P5-T9", "run_weekly", lambda: run_weekly(none, week)),
        (
            "P5-T10",
            "MessageRenderer.weekly_report",
            lambda: MessageRenderer("https://testserver", ET).weekly_report(none),
        ),
        ("P5-T14", "EventLogMirror.install", mirror.install),
        ("P5-T14", "EventLogMirror.start", mirror.start),
        ("P5-T14", "EventLogMirror.flush", mirror.flush),
        ("P5-T14", "EventLogMirror.close", mirror.close),
        (
            "P5-T14",
            "install_event_mirror",
            lambda: install_event_mirror(none, CLOCK, "test", RuntimeSettings()),
        ),
    ]


async def _enter(cm: Any) -> None:
    async with cm:
        pass


STUB_IDS = [label for _, label, _ in _stubs()]


async def _not_implemented_message(call: Callable[[], Any]) -> str | None:
    """The message of the NotImplementedError `call` raises, or None when it raises something else or
    nothing (the owner has implemented it; the dummy arguments may make the real code fail)."""
    try:
        result = call()
        if inspect.isawaitable(result):
            await result
    except NotImplementedError as exc:
        return str(exc)
    except Exception:
        return None
    return None


@pytest.mark.parametrize("index", range(len(STUB_IDS)), ids=STUB_IDS)
async def test_every_stub_names_its_owner(index: int) -> None:
    owner, _label, call = _stubs()[index]
    message = await _not_implemented_message(call)
    assert message in (None, owner)


# --- signatures and additive fields -------------------------------------------------------------------------


def test_additive_fields_are_last_and_defaulted() -> None:
    assert [f.name for f in dataclasses.fields(StrategyConfigView)][-1] == "scope"
    assert dataclasses.fields(StrategyConfigView)[-1].default == "live"
    # P6-T11 appended `decision_log` after it: run_to_date is the last field but one, still defaulted
    assert [f.name for f in dataclasses.fields(DailySummaryView)][-2:] == ["run_to_date", "decision_log"]
    assert all(f.default is None for f in dataclasses.fields(DailySummaryView)[-2:])
    assert "audit_auto" in inspect.signature(ProposalService).parameters
    assert inspect.signature(ProposalService).parameters["audit_auto"].default is True
    sig = inspect.signature(SimBroker.on_candles).parameters
    assert (
        list(sig) == ["self", "candles", "now", "orders"]
        and sig["orders"].kind is inspect.Parameter.KEYWORD_ONLY
    )
    assert list(inspect.signature(Engine.on_candles).parameters) == ["self", "candles", "now"]
    assert inspect.iscoroutinefunction(Engine.on_candles)
    for fn in (run_job, run_job_async):
        params = inspect.signature(fn).parameters
        assert params["retry"].default is None and "sleep" in params


def test_notify_views() -> None:
    rtd = RunToDateView(12, Decimal("0.4167"), Decimal("0.18"), Decimal("23.40"), 12, 50)
    assert rtd.expectancy_min_trades == 50
    names = [f.name for f in dataclasses.fields(WeeklyReportView)]
    assert names[:2] == ["week_start", "week_ending"] and names[-2:] == ["commentary", "commentary_note"]


def test_replay_override_keys_are_runtime_settings() -> None:
    aliases = {f.alias or name for name, f in RuntimeSettings.model_fields.items()}
    assert REPLAY_OVERRIDE_KEYS <= aliases
    assert len(REPLAY_OVERRIDE_KEYS) == 14


def test_request_defaults_are_independent() -> None:
    a, b = ReplayRequest(DAY, DAY), ReplayRequest(DAY, DAY)
    assert a.overrides == {} and a.strategies == {} and a.offline is False and a.label is None
    assert a.overrides is not b.overrides
    assert StrategyOverride().enabled is None and StrategyOverride().params is None


def test_replay_invalid_carries_every_error() -> None:
    exc = ReplayInvalid([("date_to", "after today"), ("overrides.risk_pct", "too large")])
    assert exc.errors == [("date_to", "after today"), ("overrides.risk_pct", "too large")]
    assert "overrides.risk_pct" in str(exc)


# --- test 3: progress JSON and load_replay_run --------------------------------------------------------------


def test_progress_round_trips_through_json() -> None:
    p = ReplayProgress(
        sessions_total=5,
        sessions_done=2,
        current_date=DAY,
        trades=3,
        forced_closes=1,
        biased_days=(date(2026, 11, 23), DAY),
        missing_opening_bars=4,
        missing_minute_bars=2,
        questrade_requests=7,
    )
    data = p.to_json()
    assert data["current_date"] == "2026-11-24" and data["biased_days"] == ["2026-11-23", "2026-11-24"]
    assert ReplayProgress.from_json(data) == p
    assert ReplayProgress.from_json(None) == ReplayProgress()
    assert ReplayProgress().to_json()["current_date"] is None


def _replay_params(settings: RuntimeSettings) -> dict[str, Any]:
    pinned = PinnedStrategy("orb_sip", 5, 2, "1.0.0", "replay", True, {"top_n": 10})
    return {
        "kind": "replay",
        "date_from": "2026-11-23",
        "date_to": "2026-11-27",
        "label": "what if",
        "data_mode": "offline",
        "catalyst_mode": "stored",
        "half_spread_bps": "5",
        "settings": settings.model_dump(mode="json", by_alias=True),
        "overrides": {"risk_pct": "0.01"},
        "strategies": [pinned.to_json()],
        "code_version": "abc1234",
    }


@pytest.mark.db
def test_load_replay_run_reads_a_hand_written_row(db_factory: sessionmaker[Session]) -> None:
    settings = RuntimeSettings.model_validate({"risk_pct": "0.01", "approval_mode": "auto"})
    progress = ReplayProgress(sessions_total=4, sessions_done=1, current_date=date(2026, 11, 23))
    with db_factory() as s:
        row = m.Run(
            mode="replay",
            started_at=T0,
            params=_replay_params(settings),
            status="running",
            label="what if",
            updated_at=T0,
            progress=progress.to_json(),
        )
        s.add(row)
        s.commit()
        run_id = row.id
    run = load_replay_run(db_factory, run_id)
    assert (run.id, run.status, run.label) == (run_id, "running", "what if")
    assert (run.date_from, run.date_to) == (date(2026, 11, 23), date(2026, 11, 27))
    assert (run.data_mode, run.catalyst_mode, run.half_spread_bps) == ("offline", "stored", Decimal("5"))
    assert run.settings == settings and run.settings.approval_mode == "auto"
    assert run.strategies == (PinnedStrategy("orb_sip", 5, 2, "1.0.0", "replay", True, {"top_n": 10}),)
    assert run.overrides == {"risk_pct": "0.01"} and run.progress == progress
    assert run.created_at == T0 and run.finished_at is None and run.error is None
    assert run.cancel_requested is False


@pytest.mark.db
def test_load_replay_run_refuses_the_live_run_and_unknown_ids(db_factory: sessionmaker[Session]) -> None:
    world = seed_replay_world(db_factory, strategies=False)
    with pytest.raises(ReplayNotFound):
        load_replay_run(db_factory, world.live_run_id)
    with pytest.raises(ReplayNotFound):
        load_replay_run(db_factory, 987654)


# --- test 6: the job runner's retry keyword -----------------------------------------------------------------


def test_retry_policy_from_settings() -> None:
    s = RuntimeSettings.model_validate({"jobs.retry_attempts": 2, "jobs.retry_delay_seconds": 90})
    assert RetryPolicy.from_settings(s) == RetryPolicy(attempts=2, first_delay_s=90.0, backoff=2.0)
    assert RetryPolicy.from_settings(RuntimeSettings()) == RetryPolicy(3, 120.0, 2.0)
    assert RetryPolicy() == RetryPolicy(1, 120.0, 2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        RetryPolicy().attempts = 2  # type: ignore[misc]


@pytest.mark.db
@pytest.mark.parametrize("retry", [None, RetryPolicy(attempts=1)], ids=["none", "one_attempt"])
def test_run_job_without_retries_behaves_as_before(
    db_factory: sessionmaker[Session], retry: RetryPolicy | None
) -> None:
    slept: list[float] = []

    def boom() -> dict[str, Any]:
        raise RuntimeError("FinViz down")

    out = run_job(db_factory, CLOCK, "nightly", DAY, boom, retry=retry, sleep=slept.append)
    assert out.status == "failed" and out.error == "RuntimeError: FinViz down"
    ok = run_job(db_factory, CLOCK, "nightly", DAY, lambda: {"n": 1}, retry=retry, sleep=slept.append)
    assert ok.status == "succeeded" and ok.detail == {"n": 1}
    assert slept == []
    with db_factory() as s:
        assert list(s.execute(select(m.JobRun.status).order_by(m.JobRun.id)).scalars()) == [
            "failed",
            "succeeded",
        ]
        assert list(s.execute(select(m.EventLog.level)).scalars()) == ["error"]


@pytest.mark.db
async def test_run_job_async_without_retries_behaves_as_before(db_factory: sessionmaker[Session]) -> None:
    async def body() -> dict[str, Any]:
        return {"ok": True}

    async def no_sleep(_: float) -> None:
        raise AssertionError("no sleep without retries")

    out = await run_job_async(db_factory, CLOCK, "premarket", DAY, body, retry=RetryPolicy(), sleep=no_sleep)
    assert out.status == "succeeded" and out.detail == {"ok": True}


async def test_more_than_one_attempt_is_owned_by_p5_t15() -> None:
    none: Any = None

    async def body() -> dict[str, Any]:
        return {}

    sync = await _not_implemented_message(
        lambda: run_job(none, CLOCK, "nightly", DAY, dict, retry=RetryPolicy(attempts=2))
    )
    async_ = await _not_implemented_message(
        lambda: run_job_async(none, CLOCK, "nightly", DAY, body, retry=RetryPolicy(attempts=3))
    )
    assert sync in (None, "P5-T15") and async_ in (None, "P5-T15")


def test_runner_keeps_its_p3_names() -> None:
    for name in ("run_job", "run_job_async", "JobFailure", "JobOutcome", "OUTCOME_UNKNOWN", "lock_key"):
        assert hasattr(runner, name)


# --- the fakes behave like the contracts say ----------------------------------------------------------------


async def test_fake_market_never_returns_a_bar_after_its_clock() -> None:
    clock = FixedClock(datetime(2026, 11, 24, 14, 34, 59, tzinfo=UTC))  # 09:34:59 ET
    market = FakeReplayMarket(clock)
    market.add_symbol("AAA", 1)
    opening = candle(datetime(2026, 11, 24, 14, 30, tzinfo=UTC), "10", "10.4", "9.9", "10.2", minutes=5)
    market.set_opening_bar(DAY, 1, opening)
    market.set_minute_bars(
        1,
        DAY,
        minute_series(datetime(2026, 11, 24, 14, 30, tzinfo=UTC), ["10", "10.1", "10.2", "10.3", "10.4"]),
    )
    got = await market.opening_bars(DAY, [1])
    assert got.bars == {} and got.missing == {1: "bar_not_complete"}
    clock.set(datetime(2026, 11, 24, 14, 35, 4, tzinfo=UTC))
    assert (await market.opening_bars(DAY, [1])).bars == {1: opening}
    clock.set(datetime(2026, 11, 24, 14, 32, 30, tzinfo=UTC))
    quote = (await market.quotes([1]))[1]
    assert quote.last == Decimal("10.1") and quote.last_trade_time == datetime(
        2026, 11, 24, 14, 32, tzinfo=UTC
    )
    assert quote.bid is not None and quote.ask is not None and quote.bid < quote.last < quote.ask
    assert quote.delay == 0
    assert market.load_calls == [((1,), DAY)]
    assert market.bar_ending_at(1, datetime(2026, 11, 24, 14, 32, tzinfo=UTC)) is not None


async def test_fake_engine_records_calls_with_the_clock_time() -> None:
    clock = FixedClock(T0)
    seen: list[str] = []

    def hook(engine: FakeReplayEngine, call: Any) -> None:
        seen.append(call.method)
        if call.method == "run_event":
            engine.broker.add_order(1)

    engine = FakeReplayEngine(clock, hook=hook)
    await engine.run_event("orb_open", DAY)
    clock.set(T0 + timedelta(minutes=1))
    await engine.on_candles({1: BAR}, clock.now())
    await engine.tick(clock.now())
    assert await engine.end_of_session(DAY) == []
    assert [(c.method, c.at) for c in engine.calls] == [
        ("run_event", T0),
        ("on_candles", T0 + timedelta(minutes=1)),
        ("tick", T0 + timedelta(minutes=1)),
        ("end_of_session", T0 + timedelta(minutes=1)),
    ]
    assert seen == ["run_event", "on_candles", "tick", "end_of_session"]
    assert engine.broker.working_symbol_ids() == [1]


def test_fake_replay_launcher_records() -> None:
    import asyncio

    launcher = FakeReplayLauncher()
    asyncio.run(launcher.launch(7))
    assert launcher.launched == [7] and launcher.running() is False
    launcher.fail_with = OSError("spawn failed")
    with pytest.raises(OSError):
        asyncio.run(launcher.launch(8))
    assert launcher.launched == [7]


@pytest.mark.db
def test_make_services_has_a_fake_replay_launcher(db_factory: sessionmaker[Session]) -> None:
    services = make_services(test_core(db_factory, FixedClock(T0)))
    assert isinstance(services.replays, FakeReplayLauncher)
    assert make_services(test_core(db_factory, FixedClock(T0)), replays=None).replays is None


@pytest.mark.db
def test_seed_replay_world(db_factory: sessionmaker[Session]) -> None:
    bars = minute_series(datetime(2026, 11, 24, 14, 30, tzinfo=UTC), ["10", "10.1"])
    world = seed_replay_world(
        db_factory,
        universe_days=[DAY],
        stats={("AAA", DAY): ("150000", "1.25")},
        archive={("AAA", "1m"): bars},
        catalysts={("AAA", DAY): "news"},
    )
    assert set(world.symbols) == {"SPY", "AAA", "BBB"}
    assert {"orb_sip", "spy_overlay"} <= set(world.configs)
    with db_factory() as s:
        assert s.get(m.Run, world.live_run_id) is not None
        assert len(s.execute(select(m.UniverseSnapshot)).scalars().all()) == 3
        assert len(s.execute(select(m.CandleArchive)).scalars().all()) == 2
        assert s.execute(select(m.Catalyst.catalyst_type)).scalar_one() == "news"
        scopes = set(s.execute(select(m.StrategyConfig.scope)).scalars())
    assert scopes == {"live"}


def test_weekly_deps_is_frozen_and_typed() -> None:
    assert WeeklyDeps.__dataclass_params__.frozen  # type: ignore[attr-defined]
    fields = [f.name for f in dataclasses.fields(WeeklyDeps)]
    assert fields == ["factory", "clock", "calendar", "settings", "writer", "notifier", "render", "run_id"]


def test_snapshot_settings_is_a_settings_store_and_pinned_registry_a_registry() -> None:
    assert issubclass(replay_setup.SnapshotSettings, SettingsStore)
    assert issubclass(replay_setup.PinnedRegistry, StrategyRegistry)


def test_replay_deps_fields() -> None:
    assert [f.name for f in dataclasses.fields(replay_runner.ReplayDeps)] == [
        "factory",
        "wall",
        "calendar",
        "market_factory",
        "catalysts_factory",
        "engine_factory",
        "decisions",  # P6-T10 (additive, defaults to None)
    ]
