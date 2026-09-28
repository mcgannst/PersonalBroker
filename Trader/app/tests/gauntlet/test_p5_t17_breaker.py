"""P5-T17 Breaker (gauntlet attempt 1): the Phase 5 wiring.

Targets: one log mirror per process, closed on every exit path (an early failure, an unexpected exception,
repeated invocations in one process), the replay mirror's run id and wall clock; the retry deadlines across
DST and the absence of retries on event paths and in long-lived processes; that the conftest one-attempt
fixture hides nothing in production; `trader replay` exit codes, override validation without echoing values,
cancellation and nice; `trader weekly` on an early-close Friday, its Claude client and forced re-runs; the
API's replay launcher argv; the weekly crontab line against the replay quiet times. Real test database;
no network."""

# ruff: noqa: F811  (the imported fixtures are test parameters)

import ast
import asyncio
import inspect
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest
import structlog
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from typer.testing import CliRunner

import trader.bootstrap
import trader.cli as cli
import trader.runtime as rt
from tests.test_cli_phase5 import (  # noqa: F401 (the fixtures)
    SAT,
    Recorded,
    ReplayWorld,
    recorded,
    replay_runs,
    rw,
)
from tests.test_runtime import World, world  # noqa: F401 (the fixture)
from trader.api.replay_launcher import SubprocessReplayLauncher
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.jobs import runner as job_runner
from trader.logging_mirror import EventLogMirror
from trader.logging_setup import configure_logging  # the real one (conftest stubs the module attribute)
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET
from trader.replay import runner as replay_runner
from trader.replay.clock import ReplayClock
from trader.replay.data import QUIET_TIMES
from trader.replay.types import ReplayRequest, load_replay_run
from trader.settings_store import SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db

CAL = SessionCalendar()
APP_DIR = Path(__file__).resolve().parents[2]  # Trader/app
TRADER_DIR = APP_DIR / "trader"
REPO = APP_DIR.parents[1]
MON_23, TUE_24, WED_25, FRI_27 = (
    date(2026, 11, 23),
    date(2026, 11, 24),
    date(2026, 11, 25),
    date(2026, 11, 27),
)
SATURDAY_OCT_10 = date(2026, 10, 10)


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


NICE_CALLS: list[int] = []


@pytest.fixture(autouse=True)
def _logging_and_no_nice(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Real logging (so structlog lines reach the root logger and a mirror), stray mirrors set aside, and
    `os.nice` replaced by a recorder so this test process is never re-niced (the real `_lower_priority`
    runs)."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for stray in handlers:
        if type(stray).__name__ == "_MirrorHandler":
            root.removeHandler(stray)
    structlog.reset_defaults()
    configure_logging("test")
    NICE_CALLS.clear()

    def fake_nice(inc: int) -> int:
        NICE_CALLS.append(inc)
        return inc

    monkeypatch.setattr(os, "nice", fake_nice)
    yield
    cli.close_mirrors()
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    structlog.reset_defaults()


def mirror_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if type(h).__name__ == "_MirrorHandler"]


def spy_installs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int | None, int, EventLogMirror | None]]:
    """Record every install: (process, run_id, mirror handlers already on the root logger, the mirror)."""
    seen: list[tuple[str, int | None, int, EventLogMirror | None]] = []
    real = rt.install_log_mirror

    def spy(core: Core, process: str, **kw: Any) -> EventLogMirror | None:
        before = len(mirror_handlers())
        mirror = real(core, process, **kw)
        seen.append((process, kw.get("run_id"), before, mirror))
        return mirror

    monkeypatch.setattr(rt, "install_log_mirror", spy)
    return seen


def queued_replay(w: ReplayWorld, label: str | None = None) -> int:
    return replay_runner.create_replay(
        w.factory,
        w.wall,
        CAL,
        SettingsStore(w.factory, now=w.wall.now),
        StrategyRegistry(w.factory, w.wall),
        ReplayRequest(MON_23, TUE_24, label=label),
        "web:stephen",
    )


# --- 1. the replay mirror: the replay's run id and the wall clock -------------------------------------------


def test_replay_mirror_rows_carry_the_run_id_and_the_wall_clock_never_the_replay_clock(
    rw: ReplayWorld,
) -> None:
    def hook(engine: Any, call: Any) -> None:
        if call.method == "end_of_session":
            structlog.get_logger("trader.test").error("replay.day_failed")

    rw.hook = hook
    result = CliRunner().invoke(app, ["replay", "--from", "2026-11-23", "--to", "2026-11-23"])
    assert result.exit_code == 0, result.output
    [run] = replay_runs(rw.factory)
    [mirror] = rw.mirrors
    assert mirror is not None and mirror.process == "replay" and mirror.run_id == run.id
    assert mirror._clock is rw.core.clock and not isinstance(mirror._clock, ReplayClock)
    with rw.factory() as s:
        rows = list(s.execute(select(m.EventLog).where(m.EventLog.source == "log.replay")).scalars())
    assert len(rows) == 1
    # Logged while the replay clock stood on 2026-11-23; stamped with the wall clock (Saturday 11-28).
    assert rows[0].ts == SAT and rows[0].run_id == run.id
    assert mirror_handlers() == [] and cli._OPEN_MIRRORS == []


# --- 2. every exit path closes the command's mirror ---------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["weekly-bad-date", "preopen-bad-date", "weekly-unfinished-week", "unexpected-exception"],
)
def test_a_command_that_fails_early_still_closes_its_mirror(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    seen = spy_installs(monkeypatch)
    world.clock.set(SAT)
    if case == "weekly-bad-date":
        args = ["weekly", "--date", "2026-13-40"]
    elif case == "preopen-bad-date":
        args = ["preopen", "--date", "nope"]
    elif case == "weekly-unfinished-week":
        args = ["weekly", "--date", "2026-12-02"]
    else:

        def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(cli, "_session", boom)
        args = ["preopen"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert [(p, r) for p, r, _, _ in seen] == [("cron", None)]
    mirror = seen[0][3]
    assert mirror is not None and mirror._closing, "the mirror was never closed"
    assert mirror_handlers() == [] and cli._OPEN_MIRRORS == []


def test_repeated_cli_invocations_in_one_process_never_stack_mirror_handlers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = spy_installs(monkeypatch)
    world.clock.set(et(SATURDAY_OCT_10, 9, 20))  # not a session: preopen does nothing
    runner = CliRunner()
    for _ in range(3):
        assert runner.invoke(app, ["preopen"]).exit_code == 0
        assert mirror_handlers() == []
    assert runner.invoke(app, ["weekly", "--date", "bad"]).exit_code == 1
    assert [before for _, _, before, _ in seen] == [0, 0, 0, 0]  # never a second handler
    assert all(mirror is not None and mirror._closing for *_, mirror in seen)
    assert mirror_handlers() == [] and cli._OPEN_MIRRORS == []


# --- 3. retries ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "premarket_utc", "preopen_utc"),
    [
        (date(2026, 3, 6), (14, 18), (14, 28)),  # EST, the Friday before DST starts
        (date(2026, 3, 9), (13, 18), (13, 28)),  # EDT, the Monday after
        (date(2026, 10, 30), (13, 18), (13, 28)),  # EDT, the Friday before DST ends
        (date(2026, 11, 2), (14, 18), (14, 28)),  # EST, the Monday after
    ],
)
def test_premarket_and_preopen_deadlines_reach_the_real_retry_policy_on_both_sides_of_dst(
    world: World,
    recorded: Recorded,
    day: date,
    premarket_utc: tuple[int, int],
    preopen_utc: tuple[int, int],
) -> None:
    runner = CliRunner()
    world.clock.set(et(day, 7, 55))
    assert runner.invoke(app, ["premarket"]).exit_code == 0
    world.clock.set(et(day, 9, 20))
    assert runner.invoke(app, ["preopen"]).exit_code == 0
    policies = dict(recorded.calls)
    pre, opn = policies["premarket"], policies["preopen"]
    assert pre is not None and opn is not None
    assert type(pre) is job_runner.RetryPolicy and type(opn) is job_runner.RetryPolicy
    assert pre.attempts == 3 and opn.attempts == 3
    assert pre.deadline == datetime.combine(day, time(*premarket_utc), tzinfo=UTC)
    assert opn.deadline == datetime.combine(day, time(*preopen_utc), tzinfo=UTC)


def test_outside_the_test_fixture_the_runtime_uses_the_real_retry_policy() -> None:
    """The conftest fixture swaps `trader.runtime.RetryPolicy` for a one-attempt class. A fresh interpreter
    (no conftest) must see the real class and the settings' three attempts with 120 s first delay, and a
    premarket policy that ends at 09:18 ET."""
    assert getattr(rt, "RetryPolicy") is not job_runner.RetryPolicy  # noqa: B009 (the fixture is active)
    code = (
        "import datetime\n"
        "import trader.runtime as rt\n"
        "import trader.jobs.runner as r\n"
        "from trader.settings_store import RuntimeSettings\n"
        "assert rt.RetryPolicy is r.RetryPolicy\n"
        "p = rt.day_job_retry(RuntimeSettings())\n"
        "q = rt.premarket_retry(RuntimeSettings(), datetime.date(2026, 11, 24))\n"
        "print(type(p).__name__, p.attempts, p.first_delay_s, p.backoff, p.deadline)\n"
        "print(q.attempts, q.deadline.astimezone(datetime.UTC).isoformat())\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=APP_DIR, env=env, timeout=120
    )
    assert out.returncode == 0, out.stderr[-2000:]
    first, second = out.stdout.splitlines()
    assert first == "RetryPolicy 3 120.0 2 None" or first == "RetryPolicy 3 120.0 2.0 None", first
    assert second == "3 2026-11-24T14:18:00+00:00", second


def _calls_with_retry(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and any(kw.arg == "retry" for kw in node.keywords)
    ]


def test_no_event_path_and_no_long_lived_process_runs_a_retrying_day_job() -> None:
    """Session events (scheduler, check-ins, event backups) never pass `retry`; the worker and the API
    never call a day-level job in-process, so no production path of theirs can sleep minutes between
    attempts (the conftest one-attempt fixture could otherwise hide it)."""
    for rel in ("engine/scheduler.py", "jobs/events.py", "jobs/checkin.py", "worker.py"):
        tree = ast.parse((TRADER_DIR / rel).read_text())
        assert _calls_with_retry(tree) == [], rel
    runtime_tree = ast.parse((TRADER_DIR / "runtime.py").read_text())
    funcs = {
        n.name: n for n in ast.walk(runtime_tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for name in ("checkin_job", "event_backup", "run_worker", "_run_worker"):
        assert _calls_with_retry(funcs[name]) == [], name
    day_jobs = {
        "preopen_job",
        "postclose_job",
        "weekly_job",
        "run_cli_job",
        "day_job_retry",
        "premarket_retry",
    }
    for name in ("run_worker", "_run_worker"):
        used = {n.attr for n in ast.walk(funcs[name]) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(funcs[name]) if isinstance(n, ast.Name)
        }
        assert not used & day_jobs, (name, used & day_jobs)
    for path in [TRADER_DIR / "worker.py", *sorted((TRADER_DIR / "api").rglob("*.py"))]:
        tree = ast.parse(path.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        assert not names & (day_jobs | {"RetryPolicy"}), (path.name, names & day_jobs)


# --- 4. trader replay -------------------------------------------------------------------------------------


def test_replay_bad_override_values_exit_1_without_echoing_the_value(rw: ReplayWorld) -> None:
    secretish = "hunter2Zq9SecretValue"
    for args in (
        ["--set", f"risk_pct={secretish}"],
        ["--set", f"password={secretish}"],
        ["--set", "risk_pct=0.01", "--set", f"starting_cash={secretish}"],
    ):
        result = CliRunner().invoke(app, ["replay", "--from", "2026-11-23", "--to", "2026-11-23", *args])
        assert result.exit_code == 1, result.output
        assert secretish not in result.output, result.output
        assert "overrides." in result.output
    assert replay_runs(rw.factory) == []
    assert rw.engines == []


@pytest.mark.parametrize(
    "setting", ["risk_pct=NaN", "starting_cash=Infinity", "slippage_bps=-Infinity", "slippage_min=1e999"]
)
def test_replay_non_finite_override_values_are_invalid_not_a_crash(rw: ReplayWorld, setting: str) -> None:
    """`--set` parses VALUE as JSON, and Python's json accepts NaN / Infinity (and 1e999 overflows to inf):
    such a value must be a clean `ReplayInvalid` (exit 1, the field named), never a stored run or a
    traceback."""
    result = CliRunner().invoke(
        app, ["replay", "--from", "2026-11-23", "--to", "2026-11-23", "--set", setting]
    )
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert f"overrides.{setting.split('=')[0]}" in result.output, result.output
    assert replay_runs(rw.factory) == []


def test_replay_busy_exits_2_and_a_non_queued_run_is_refused(rw: ReplayWorld) -> None:
    queued = queued_replay(rw)
    # Another replay queued: a new one is refused as busy, nothing is created.
    busy = CliRunner().invoke(app, ["replay", "--from", "2026-11-23", "--to", "2026-11-23"])
    assert busy.exit_code == 2, busy.output
    assert [r.id for r in replay_runs(rw.factory)] == [queued]
    # The queued run itself while another process holds the replay lock: 2, and it stays queued.
    bind = rw.factory.kw["bind"]
    with bind.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": replay_runner.REPLAY_LOCK_KEY})
        try:
            held = CliRunner().invoke(app, ["replay", "--run", str(queued)])
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": replay_runner.REPLAY_LOCK_KEY})
    assert held.exit_code == 2, held.output
    assert load_replay_run(rw.factory, queued).status == "queued"
    assert rw.engines == []
    # A run some other process has already started: refused (1), never run twice.
    with rw.factory.begin() as s:
        s.execute(update(m.Run).where(m.Run.id == queued).values(status="running"))
    running = CliRunner().invoke(app, ["replay", "--run", str(queued)])
    assert running.exit_code == 1, running.output
    assert rw.engines == []
    assert mirror_handlers() == [] and cli._OPEN_MIRRORS == []


def test_replay_cancelled_mid_run_stops_after_that_session_and_exits_0(rw: ReplayWorld) -> None:
    def hook(engine: Any, call: Any) -> None:
        if call.method == "end_of_session" and call.args[0] == MON_23:
            [run] = replay_runs(rw.factory)
            assert replay_runner.request_cancel(rw.factory, rw.wall, run.id, "web:stephen")

    rw.hook = hook
    result = CliRunner().invoke(app, ["replay", "--from", "2026-11-23", "--to", "2026-11-25"])
    assert result.exit_code == 0, result.output
    [run] = replay_runs(rw.factory)
    assert run.status == "cancelled"
    lines = result.output.splitlines()
    assert "2026-11-23 done: 0 trades, P&L 0.00" in lines
    assert not any(line.startswith(("2026-11-24 done", "2026-11-25 done")) for line in lines)
    assert "cancelled" in lines[-1]
    assert NICE_CALLS == [10]  # the replay lowered its priority, through the recorder only


def test_the_api_replay_launcher_argv_is_fixed_and_shell_free(rw: ReplayWorld) -> None:
    """A label that looks like options never reaches the child's argv; the child is exec'd, not shelled."""
    label = "--offline --set risk_pct=1 --run 1; rm -rf /"
    run_id = queued_replay(rw, label=label)
    assert inspect.signature(SubprocessReplayLauncher).parameters["spawn"].default is (
        asyncio.create_subprocess_exec
    )
    services_src = ast.parse((TRADER_DIR / "api" / "services.py").read_text())
    [ctor] = [
        n
        for n in ast.walk(services_src)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "SubprocessReplayLauncher"
    ]
    assert ctor.keywords == []  # the default executable and spawn (create_subprocess_exec)

    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    class Proc:
        pid = 4242

        async def wait(self) -> int:
            return 0

    async def spawn(*argv: str, **kw: Any) -> Proc:
        calls.append((argv, kw))
        return Proc()

    async def go() -> None:
        launcher = SubprocessReplayLauncher(rw.factory, rw.wall, spawn=spawn)
        await launcher.launch(run_id)
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(go())
    [(argv, kw)] = calls
    assert argv == ("trader", "replay", "--run", str(run_id))
    assert "shell" not in kw
    # The child's own command line runs it, whatever the label says.
    result = CliRunner().invoke(app, ["replay", "--run", str(run_id)])
    assert result.exit_code == 0, result.output
    final = load_replay_run(rw.factory, run_id)
    assert final.status == "completed" and final.label is not None and final.label.startswith(label)


# --- 5. trader weekly -------------------------------------------------------------------------------------


def test_weekly_waits_for_the_early_close_friday_then_runs(world: World) -> None:
    """2026-11-27 closes at 13:00 ET: at 12:59 the week has not ended (1, nothing recorded); at 13:00 it
    has."""
    world.clock.set(et(FRI_27, 12, 59))
    early = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25"])
    assert early.exit_code == 1, early.output
    with world.factory() as s:
        assert (
            s.execute(select(func.count()).select_from(m.JobRun).where(m.JobRun.job == "weekly")).scalar()
            == 0
        )
    world.clock.set(et(FRI_27, 13, 0))
    done = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25"])
    assert done.exit_code == 0, done.output
    assert "weekly 2026-11-27: succeeded" in done.output
    assert NICE_CALLS == []  # only a replay lowers its priority


def test_weekly_cli_builds_claude_with_timeout_30_one_retry_and_force_sends_one_message(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[dict[str, Any]] = []
    closed: list[bool] = []

    class Messages:
        async def create(self, **kw: Any) -> Any:
            raise RuntimeError("no network in tests")

    class FakeClaude:
        def __init__(self, **kw: Any) -> None:
            clients.append(kw)
            self.messages = Messages()

        async def close(self) -> None:
            closed.append(True)

    keyed = Core(
        **{
            **world.core.__dict__,
            "env": world.core.env.model_copy(update={"anthropic_api_key": SecretStr("sk-test-not-real")}),
        }
    )
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: keyed)
    monkeypatch.setattr(rt, "claude_client_class", lambda: FakeClaude)
    world.clock.set(SAT)
    runner = CliRunner()
    first = runner.invoke(app, ["weekly"])
    assert first.exit_code == 0 and "weekly 2026-11-27: succeeded" in first.output, first.output
    forced = runner.invoke(app, ["weekly", "--force"])
    assert forced.exit_code == 0 and "weekly 2026-11-27: succeeded" in forced.output, forced.output
    assert "sk-test-not-real" not in first.output + forced.output
    assert len(clients) == 2 and len(closed) == 2
    for kw in clients:
        assert kw["timeout"] == 30 and kw["max_retries"] == 1 and kw["api_key"] == "sk-test-not-real"
    with world.factory() as s:
        reports = s.execute(select(func.count()).select_from(m.WeeklyReport)).scalar()
        notes = s.execute(
            select(func.count())
            .select_from(m.Notification)
            .where(m.Notification.dedupe_key == "weekly:2026-11-27")
        ).scalar()
    assert reports == 1 and notes == 1
    sends = [c for c in world.api.calls if c[0] == "send_message"]
    assert len(sends) == 1, sends


# --- 6. crontab -------------------------------------------------------------------------------------------


def test_the_weekly_crontab_line_is_supercronic_shaped_and_a_replay_quiet_time() -> None:
    crontab = (REPO / "Trader" / "docker" / "crontab").read_text()
    body = [ln for ln in crontab.splitlines() if ln.strip() and not ln.startswith("#")]
    assert body[0] == "CRON_TZ=America/New_York"
    commands = set(typer_commands())
    quiet = {(t, days) for t, days in QUIET_TIMES}
    for line in body[1:]:
        fields = line.split()
        minute, hour, dom, month, dow, *cmd = fields
        assert minute.isdigit() and hour.isdigit() and dom == "*" and month == "*", line
        assert cmd[0] == "trader" and cmd[1] in commands, line
        if "-" in dow:
            lo, hi = (int(x) for x in dow.split("-"))
            cron_days = set(range(lo, hi + 1))
        elif dow == "*":
            cron_days = set(range(7))
        else:
            cron_days = {int(dow)}
        py_days = frozenset((d - 1) % 7 for d in cron_days)  # cron 0 = Sunday, Python 6 = Sunday
        assert (time(int(hour), int(minute)), py_days) in quiet, line
    assert "0 9 * * 6      trader weekly" in body
    assert (time(9, 0), frozenset({5})) in quiet


def typer_commands() -> list[str]:
    import typer.main

    group = typer.main.get_command(app)
    return list(group.commands)  # type: ignore[attr-defined]
