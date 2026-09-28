"""P5-T17: the Phase 5 CLI wiring. `trader replay` (acceptance tests 1-2, with `open_replay_deps` replaced by
the fakes of tests/fakes_replay.py), `trader weekly` (test 3), the retry policies of the day-level jobs
(test 4), the `cron` / `replay` log mirrors of the commands (test 6), and the crontab, launcher and SPEC /
master-plan text (tests 7-8). Real test database; no network."""

# ruff: noqa: F811  (the imported `world` / `wired` fixtures are test parameters)

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
import trader.cli as cli
import trader.runtime as rt
from tests.fakes_api import test_core
from tests.fakes_replay import EngineCall, FakeReplayEngine, FakeReplayMarket, seed_replay_world
from tests.test_runtime import World, use_fake_engine, world  # noqa: F401 (the fixture)
from trader.api.launcher import CLI_ARGS, NO_OPTIONS
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine import scheduler
from trader.jobs import runner as job_runner
from trader.jobs.runner import JobOutcome, RetryPolicy
from trader.logging_mirror import EventLogMirror
from trader.logging_setup import configure_logging  # the real one (conftest stubs the module attribute)
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.replay import runner as replay_runner
from trader.replay.types import ReplayRun, load_replay_run
from trader.settings_store import SettingsStore

pytestmark = pytest.mark.db

CAL = SessionCalendar()
SAT = datetime(2026, 11, 28, 12, 0, tzinfo=ET).astimezone(UTC)  # full data mode, after the week's close
MON_23, TUE_24, WED_25, FRI_27 = (
    date(2026, 11, 23),
    date(2026, 11, 24),
    date(2026, 11, 25),
    date(2026, 11, 27),
)
DAY = date(2026, 10, 6)  # tests.test_runtime's session (a Tuesday)
ROOT = Path(__file__).resolve().parents[3]  # the repository root (Trader/app/tests -> repo)


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Logging configured for real (conftest stubs the commands' `configure_logging`), so structlog lines
    reach the root logger and its mirror; undone after each test (root handlers, level, structlog). The
    replay command's `os.nice` is replaced, so the test process keeps its priority."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for stray in handlers:  # a mirror another test left installed is set aside for this test
        if type(stray).__name__ == "_MirrorHandler":
            root.removeHandler(stray)
    structlog.reset_defaults()
    configure_logging("test")
    monkeypatch.setattr(cli, "_lower_priority", lambda: None)
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    structlog.reset_defaults()


def events(factory: sessionmaker[Session], source: str | None = None) -> list[m.EventLog]:
    with factory() as s:
        q = select(m.EventLog).order_by(m.EventLog.id)
        if source is not None:
            q = q.where(m.EventLog.source == source)
        return list(s.execute(q).scalars())


def mirror_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if type(h).__name__ == "_MirrorHandler"]


# --- the replay world ---------------------------------------------------------------------------------------


@dataclass
class ReplayWorld:
    core: Core
    wall: FixedClock
    engines: list[FakeReplayEngine] = field(default_factory=list)
    hook: Any = None
    opened: list[str] = field(default_factory=list)  # the data modes open_replay_deps was asked for
    mirrors: list[EventLogMirror | None] = field(default_factory=list)

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory


@pytest.fixture
def rw(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> ReplayWorld:
    wall = FixedClock(SAT)
    core = test_core(db_factory, wall)
    seed_replay_world(db_factory, clock=wall)
    w = ReplayWorld(core, wall)

    def market(run: ReplayRun, clock: Any) -> FakeReplayMarket:
        return FakeReplayMarket(clock)

    def engine(run: ReplayRun, clock: Any, market: Any, catalysts: Any) -> FakeReplayEngine:
        e = FakeReplayEngine(clock, hook=w.hook)
        w.engines.append(e)
        return e

    @asynccontextmanager
    async def fake_deps(core: Core, *, data_mode: str) -> AsyncIterator[replay_runner.ReplayDeps]:
        w.opened.append(data_mode)
        yield replay_runner.ReplayDeps(core.factory, core.clock, CAL, market, lambda run: None, engine)

    real_install = rt.install_log_mirror

    def recording_install(core: Core, process: str, **kw: Any) -> EventLogMirror | None:
        mirror = real_install(core, process, **kw)
        w.mirrors.append(mirror)
        return mirror

    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    monkeypatch.setattr(replay_runner, "open_replay_deps", fake_deps)
    monkeypatch.setattr(rt, "install_log_mirror", recording_install)
    return w


def replay_runs(factory: sessionmaker[Session]) -> list[m.Run]:
    with factory() as s:
        return list(s.execute(select(m.Run).where(m.Run.mode == "replay").order_by(m.Run.id)).scalars())


def add_trade(factory: sessionmaker[Session], run_id: int, day: date, pnl: str, pnl_r: str) -> None:
    """One closed position and its trade for the replay run (as the engine would write them)."""
    with session_scope(factory) as s:
        symbol_id = s.execute(select(m.Symbol.id).where(m.Symbol.ticker == "AAA")).scalar_one()
        at = et(day, 10, 0)
        pos = m.Position(
            run_id=run_id,
            symbol_id=symbol_id,
            strategy_config_id=None,
            qty=0,
            avg_price=Decimal("10"),
            stop_loss=None,
            planned_risk=Decimal("20"),
            session_date=day,
            opened_at=at,
            closed_at=at,
            entry_order_id=1,
            stop_order_id=None,
            unprotected_since=None,
            unprotected_seconds=0,
        )
        s.add(pos)
        s.flush()
        s.add(
            m.Trade(
                run_id=run_id,
                position_id=pos.id,
                symbol_id=symbol_id,
                session_date=day,
                entry_price=Decimal("10"),
                exit_price=Decimal("9.64"),
                qty=20,
                pnl=Decimal(pnl),
                pnl_r=Decimal(pnl_r),
                planned_risk=Decimal("20"),
                exit_reason="stop",
                slippage_total=Decimal("0"),
                fees_total=Decimal("0"),
                opened_at=at,
                closed_at=at,
            )
        )


# --- 1. trader replay --from --to ---------------------------------------------------------------------------


def test_replay_from_to_creates_and_runs_and_prints_a_line_per_session(rw: ReplayWorld) -> None:
    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        if call.method == "end_of_session" and call.args[0] == TUE_24:
            [run] = replay_runs(rw.factory)
            add_trade(rw.factory, run.id, TUE_24, "-7.20", "-0.36")

    rw.hook = hook
    result = CliRunner().invoke(
        app, ["replay", "--from", "2026-11-23", "--to", "2026-11-25", "--label", "cli check"]
    )
    assert result.exit_code == 0, result.output
    [run] = replay_runs(rw.factory)
    assert run.status == "completed" and run.label == "cli check"
    lines = result.output.splitlines()
    assert "2026-11-23 done: 0 trades, P&L 0.00" in lines
    assert "2026-11-24 done: 1 trade, P&L -7.20" in lines
    assert "2026-11-25 done: 0 trades, P&L 0.00" in lines
    summary = lines[-1]
    for part in (f"replay {run.id}", "completed", "trades 1", "expectancy -0.3600R", "P&L -7.20"):
        assert part in summary, summary
    assert "biased days none" in summary
    with rw.factory() as s:  # created by the CLI actor
        audit = s.execute(select(m.AuditLog).where(m.AuditLog.action == "replay.start")).scalar_one()
    assert audit.actor == "cli"
    assert rw.opened == ["full"]  # Saturday: a full-mode run


def test_replay_set_overrides_reach_the_run(rw: ReplayWorld) -> None:
    result = CliRunner().invoke(
        app,
        ["replay", "--from", "2026-11-23", "--to", "2026-11-23", "--set", "risk_pct=0.01", "--offline"],
    )
    assert result.exit_code == 0, result.output
    [row] = replay_runs(rw.factory)
    run = load_replay_run(rw.factory, row.id)
    assert run.overrides == {"risk_pct": "0.01"}
    assert run.settings.risk_pct == Decimal("0.01")
    assert run.data_mode == "offline" and rw.opened == ["offline"]


def test_replay_invalid_range_exits_1_with_one_line_per_problem(rw: ReplayWorld) -> None:
    result = CliRunner().invoke(
        app, ["replay", "--from", "2026-11-25", "--to", "2026-11-23", "--set", "nope=1"]
    )
    assert result.exit_code == 1, result.output
    assert "date_to:" in result.output or "date_from:" in result.output
    assert "overrides.nope:" in result.output
    assert replay_runs(rw.factory) == []


@pytest.mark.parametrize(
    "args",
    [
        ["replay"],
        ["replay", "--from", "2026-11-23"],
        ["replay", "--from", "yesterday", "--to", "2026-11-23"],
        ["replay", "--run", "1", "--from", "2026-11-23", "--to", "2026-11-23"],
        ["replay", "--from", "2026-11-23", "--to", "2026-11-23", "--set", "risk_pct"],
    ],
)
def test_replay_bad_arguments_exit_1(rw: ReplayWorld, args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert replay_runs(rw.factory) == []


def test_replay_while_the_lock_is_held_exits_2(rw: ReplayWorld) -> None:
    bind = rw.factory.kw["bind"]
    with bind.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": replay_runner.REPLAY_LOCK_KEY})
        try:
            result = CliRunner().invoke(app, ["replay", "--from", "2026-11-23", "--to", "2026-11-23"])
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": replay_runner.REPLAY_LOCK_KEY})
    assert result.exit_code == 2, result.output
    assert replay_runs(rw.factory) == []


def test_a_failed_replay_exits_1(rw: ReplayWorld) -> None:
    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        if call.method == "end_of_session":
            raise RuntimeError("engine broke")

    rw.hook = hook
    result = CliRunner().invoke(app, ["replay", "--from", "2026-11-23", "--to", "2026-11-24"])
    assert result.exit_code == 1, result.output
    [run] = replay_runs(rw.factory)
    assert run.status == "failed"
    assert "failed" in result.output.splitlines()[-1]


# --- 2. trader replay --run ---------------------------------------------------------------------------------


def _queued(rw: ReplayWorld) -> int:
    from trader.replay.types import ReplayRequest
    from trader.strategies.registry import StrategyRegistry

    return replay_runner.create_replay(
        rw.factory,
        rw.wall,
        CAL,
        SettingsStore(rw.factory, now=rw.wall.now),
        StrategyRegistry(rw.factory, rw.wall),
        ReplayRequest(MON_23, TUE_24),
        "web:stephen",
    )


def test_replay_run_runs_a_queued_run_and_refuses_a_completed_one(rw: ReplayWorld) -> None:
    run_id = _queued(rw)
    result = CliRunner().invoke(app, ["replay", "--run", str(run_id)])
    assert result.exit_code == 0, result.output
    assert load_replay_run(rw.factory, run_id).status == "completed"
    assert "2026-11-24 done: 0 trades, P&L 0.00" in result.output.splitlines()

    again = CliRunner().invoke(app, ["replay", "--run", str(run_id)])
    assert again.exit_code == 1, again.output
    assert "completed" in again.output
    assert len(rw.engines) == 1  # the completed run was not run again


def test_replay_run_of_an_unknown_or_live_run_exits_1(rw: ReplayWorld) -> None:
    with rw.factory() as s:
        live = s.execute(select(m.Run.id).where(m.Run.mode == "live")).scalar_one()
    for run_id in (live, 999_999):
        result = CliRunner().invoke(app, ["replay", "--run", str(run_id)])
        assert result.exit_code == 1, result.output


# --- 3. trader weekly ---------------------------------------------------------------------------------------


def _weekly_world(world: World) -> None:
    world.clock.set(SAT)


def test_weekly_runs_the_week_once_and_force_reruns_it(world: World) -> None:
    _weekly_world(world)
    first = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25"])
    assert first.exit_code == 0, first.output
    assert "weekly 2026-11-27: succeeded" in first.output
    second = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25"])
    assert second.exit_code == 0 and "weekly 2026-11-27: skipped" in second.output, second.output
    forced = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25", "--force"])
    assert forced.exit_code == 0 and "weekly 2026-11-27: succeeded" in forced.output, forced.output
    with world.factory() as s:
        runs = s.execute(
            select(m.JobRun.session_date, m.JobRun.status)
            .where(m.JobRun.job == "weekly")
            .order_by(m.JobRun.id)
        ).all()
        report = s.get(m.WeeklyReport, FRI_27)
    assert [tuple(r) for r in runs] == [(FRI_27, "succeeded"), (FRI_27, "succeeded")]
    assert report is not None and report.week_start == MON_23


def test_weekly_without_a_date_on_saturday_reports_the_week_just_ended(world: World) -> None:
    _weekly_world(world)
    result = CliRunner().invoke(app, ["weekly"])
    assert result.exit_code == 0, result.output
    assert "weekly 2026-11-27: succeeded" in result.output


def test_weekly_of_a_week_without_sessions_prints_no_sessions(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _weekly_world(world)
    monkeypatch.setattr(SessionCalendar, "is_session", lambda self, d: False)
    result = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25"])
    assert result.exit_code == 0, result.output
    assert "no sessions" in result.output
    with world.factory() as s:
        assert s.execute(select(m.JobRun).where(m.JobRun.job == "weekly")).first() is None


@pytest.mark.parametrize("date_arg", ["not-a-date", "2026-12-02"])  # bad, and a week not yet ended
def test_weekly_refuses_a_bad_date_or_an_unfinished_week(world: World, date_arg: str) -> None:
    _weekly_world(world)
    result = CliRunner().invoke(app, ["weekly", "--date", date_arg])
    assert result.exit_code == 1, result.output


# --- 4. retries ---------------------------------------------------------------------------------------------


@dataclass
class Recorded:
    calls: list[tuple[str, RetryPolicy | None]] = field(default_factory=list)


def _record_sync(rec: Recorded) -> Any:
    def run_job(
        factory: Any, clock: Any, job: str, session_date: date, fn: Any, force: bool = False, **kw: Any
    ):  # type: ignore[no-untyped-def]
        rec.calls.append((job, kw.get("retry")))
        return JobOutcome("skipped", {"reason": "recorded"})

    return run_job


def _record_async(rec: Recorded) -> Any:
    async def run_job_async(  # type: ignore[no-untyped-def]
        factory: Any, clock: Any, job: str, session_date: date, fn: Any, force: bool = False, **kw: Any
    ):
        rec.calls.append((job, kw.get("retry")))
        return JobOutcome("skipped", {"reason": "recorded"})

    return run_job_async


@pytest.fixture
def recorded(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recorded:
    rec = Recorded()
    monkeypatch.setattr(rt, "RetryPolicy", RetryPolicy)  # the real one (conftest pins one attempt)
    monkeypatch.setattr(job_runner, "run_job", _record_sync(rec))
    monkeypatch.setattr(rt, "run_job_async", _record_async(rec))
    monkeypatch.setattr(scheduler, "run_job_async", _record_async(rec))
    monkeypatch.setenv(rt.FINVIZ_CACHE_ENV, str(tmp_path / "finviz"))
    use_fake_engine(world, monkeypatch)
    return rec


def test_day_level_jobs_pass_the_settings_retry_policy_and_events_do_not(
    world: World,
    recorded: Recorded,
) -> None:
    runner = CliRunner()
    world.clock.set(et(DAY, 7, 55))
    assert runner.invoke(app, ["premarket"]).exit_code == 0
    world.clock.set(et(DAY, 9, 20))
    assert runner.invoke(app, ["preopen"]).exit_code == 0
    world.clock.set(et(DAY, 11, 30))
    assert runner.invoke(app, ["checkin", "--at", "11:30"]).exit_code == 0
    assert runner.invoke(app, ["event", "orb_open", "--force"]).exit_code == 0
    world.clock.set(et(DAY, 16, 15))
    assert runner.invoke(app, ["postclose"]).exit_code == 0
    world.clock.set(et(DAY, 20, 0))
    assert runner.invoke(app, ["nightly", "--force"]).exit_code == 0
    world.clock.set(SAT)
    assert runner.invoke(app, ["weekly"]).exit_code == 0

    policies = dict(recorded.calls)
    base = RetryPolicy.from_settings(world.core.settings.load())
    assert base.attempts == 3 and base.first_delay_s == 120.0
    assert policies["premarket"] == RetryPolicy.from_settings(
        world.core.settings.load(), deadline=et(DAY, 9, 18)
    )
    assert policies["preopen"] == RetryPolicy.from_settings(
        world.core.settings.load(), deadline=et(DAY, 9, 28)
    )
    assert policies["postclose"] == base
    assert policies["nightly"] == base
    assert policies["weekly"] == base
    assert policies["checkin@11:30"] is None
    assert policies["event:orb_open"] is None


def test_the_retry_policy_follows_the_stored_settings(world: World, recorded: Recorded) -> None:
    world.core.settings.set("jobs.retry_attempts", 2, actor="test")
    world.core.settings.set("jobs.retry_delay_seconds", 30, actor="test")
    world.clock.set(et(DAY, 16, 15))
    assert CliRunner().invoke(app, ["postclose"]).exit_code == 0
    [(job, policy)] = recorded.calls
    assert job == "postclose" and policy == RetryPolicy(attempts=2, first_delay_s=30.0)


# --- 6. the log mirror of the commands ----------------------------------------------------------------------


def test_nightly_mirrors_an_error_line_as_log_cron_and_closes_the_mirror(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def failing_run_job(*args: Any, **kw: Any) -> JobOutcome:
        structlog.get_logger("trader.test").error("nightly.boom", symbol="AAA")
        return JobOutcome("failed", {}, "boom")

    monkeypatch.setattr(job_runner, "run_job", failing_run_job)
    monkeypatch.setenv(rt.FINVIZ_CACHE_ENV, str(tmp_path / "finviz"))
    world.clock.set(et(DAY, 20, 0))
    result = CliRunner().invoke(app, ["nightly", "--force"])
    assert result.exit_code == 1 and "nightly 2026-10-07: failed" in result.output, result.output
    [row] = events(world.factory, "log.cron")
    assert row.message == "trader.test: nightly.boom" and row.run_id is None
    assert mirror_handlers() == []  # closed at exit


def test_a_core_command_installs_one_cron_mirror(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[tuple[str, Any]] = []
    real = rt.install_log_mirror

    def spy(core: Core, process: str, **kw: Any) -> EventLogMirror | None:
        mirror = real(core, process, **kw)
        installed.append((process, kw.get("run_id")))
        return mirror

    monkeypatch.setattr(rt, "install_log_mirror", spy)
    world.clock.set(et(date(2026, 10, 10), 9, 20))  # a Saturday: preopen does nothing
    assert CliRunner().invoke(app, ["preopen"]).exit_code == 0
    assert installed == [("cron", None)]
    assert mirror_handlers() == []


def test_mirror_off_installs_nothing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.core.settings.set("logging.mirror_level", "off", actor="test")
    seen: list[Any] = []
    real = rt.install_log_mirror

    def spy(core: Core, process: str, **kw: Any) -> EventLogMirror | None:
        mirror = real(core, process, **kw)
        seen.append(mirror)
        return mirror

    monkeypatch.setattr(rt, "install_log_mirror", spy)
    world.clock.set(et(date(2026, 10, 10), 9, 20))
    assert CliRunner().invoke(app, ["preopen"]).exit_code == 0
    assert seen == [None]


def test_replay_mirrors_with_the_replay_run_id_and_no_cron_mirror(rw: ReplayWorld) -> None:
    def hook(engine: FakeReplayEngine, call: EngineCall) -> None:
        if call.method == "end_of_session":
            structlog.get_logger("trader.test").error("replay.step_failed")

    rw.hook = hook
    result = CliRunner().invoke(app, ["replay", "--from", "2026-11-23", "--to", "2026-11-23"])
    assert result.exit_code == 0, result.output
    [run] = replay_runs(rw.factory)
    assert len(rw.mirrors) == 1 and rw.mirrors[0] is not None
    assert rw.mirrors[0].process == "replay" and rw.mirrors[0].run_id == run.id
    rows = events(rw.factory, "log.replay")
    assert [r.message for r in rows] == ["trader.test: replay.step_failed"]
    assert {r.run_id for r in rows} == {run.id}
    assert events(rw.factory, "log.cron") == []
    assert mirror_handlers() == []


# --- 7 and 8. crontab, launcher, SPEC and master plan -------------------------------------------------------


def test_the_launcher_runs_weekly_with_a_date_and_force() -> None:
    assert CLI_ARGS["weekly"] == ("weekly",)
    assert "weekly" not in NO_OPTIONS
    result = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25", "--force", "--help"])
    assert result.exit_code == 0


def test_the_crontab_runs_weekly_on_saturday_at_nine_et() -> None:
    crontab = (ROOT / "Trader" / "docker" / "crontab").read_text()
    lines = [line.split() for line in crontab.splitlines() if line.strip() and not line.startswith("#")]
    assert ["0", "9", "*", "*", "6", "trader", "weekly"] in lines
    assert "added by P5-T6" not in crontab
    assert "trader weekly" in "\n".join(line for line in crontab.splitlines() if line.startswith("#"))


def test_spec_section_9_has_the_weekly_note_and_the_retry_paragraph() -> None:
    spec = (ROOT / "Trader" / "docs" / "SPEC.md").read_text()
    section = spec.split("## 9. Schedule", 1)[1].split("## 10.", 1)[0]
    assert "keyed by that week's last session date" in section
    assert "does nothing when that week had no session" in section
    assert "retry a failed run in-process up to `jobs.retry_attempts` times in all" in section
    assert "only the final failure is an `error` event (one Telegram alert per job and session)" in section
    assert "Events keep their own retries (30/60/120 s)" in section


def test_master_plan_7_1_has_the_phase_5_rows() -> None:
    plan = (ROOT / "Trader" / "docs" / "plans" / "2026-09-26-build-master-plan.md").read_text()
    section = plan.split("### 7.1 Cross-phase contracts", 1)[1].split("### 7.2", 1)[0]
    rows = {line.split("|")[1].strip() for line in section.splitlines() if line.startswith("| ")}
    for name in ("Replay", "Metrics", "Weekly report", "Log mirror", "Strategy config scope"):
        assert name in rows, name
    assert "on_candles(candles: Mapping[int, Candle], now" in section
    assert "audit_auto" in section
    assert "/api/replays" in section and "/api/reports/weekly" in section
    assert "RetryPolicy" in section
    assert "weekly_report" in section
