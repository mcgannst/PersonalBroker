"""P6-T11 gauntlet (Breaker): the decision log wiring (worker loop, CLI hooks, post-close, Telegram line, CLI,
D2). Written against d9cb318; every test uses fakes or the test database, never a real service.

Targets: the worker is never slowed or broken by the loop (a hanging pass, a database outage, the 09:34-09:38
ET quiet minutes, DST and early-close days, one warning per streak, no relayed alert); the CLI hooks never
change a job's outcome or exit code; the post-close's final pass and the summary line; `trader decisions`
exit codes and masking; D2 (no decision-path file modified, golden files unchanged, the live-unchanged test
really compares trading rows and chat).
"""

import ast
import asyncio
import dataclasses as dc
import inspect
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
import trader.decisions.loop as loop_mod
import trader.runtime as rt
from tests.decisions.test_wiring import (
    CAL,
    Events,
    FakeRecorder,
    NoDb,
    _day_summary,
    _postclose_world,
    et,
    make_loop,
)
from tests.factories import add_run
from tests.test_cli import PRE_OPEN, SESSION, _fake_core, _premarket
from trader.cli import app
from trader.db import models as m
from trader.decisions.loop import DecisionsLoop, FinalPass
from trader.decisions.prune import PruneResult
from trader.decisions.types import RecorderDeps, RecordResult
from trader.engine.scheduler import FireResult
from trader.market.clock import FixedClock
from trader.notify.relay import ALERT_LEVELS
from trader.settings_store import RuntimeSettings

ORIGINAL_HOOK = rt.record_decisions_quietly  # the real hook (tests/test_cli.py's helpers replace it)
TUE = date(2026, 10, 6)
REPO = Path(__file__).resolve().parents[4]
BUILT = "d9cb318"  # the P6-T11 commit under review
DEPLOYED = "phase-5-complete"  # the commit trader-dev runs (eaafe21)


@pytest.fixture
def no_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_mod, "event_running", lambda factory, day: False)


# --- the loop: the quiet minutes ----------------------------------------------------------------------------


@pytest.mark.usefixtures("no_running")
async def test_no_pass_starts_in_the_quiet_minutes_even_when_the_gate_reads_are_slow() -> None:
    """The quiet-window check uses the time taken BEFORE the gate's database reads. A slow settings read
    (lock contention, a busy database) that starts at 09:33:50 and returns at 09:34:10 must not start a pass
    at 09:34:10: the plan says no pass STARTS between 09:34:00 and 09:38:00 ET."""
    clock = FixedClock(et(TUE, 9, 33, 50))

    def slow_settings() -> RuntimeSettings:
        clock.advance(timedelta(seconds=20))  # the read took 20 s
        return RuntimeSettings()

    loop, rec, _ = make_loop(clock, settings=slow_settings)
    await loop.run_once()
    started = [c[3] for c in rec.calls]
    assert not any(et(TUE, 9, 34) <= t < et(TUE, 9, 38) for t in started), started


@pytest.mark.usefixtures("no_running")
async def test_a_pass_started_just_before_0934_runs_and_the_next_waits_for_0938() -> None:
    """09:33:59.9 is outside the quiet minutes, so that pass runs (acceptable: a pass takes seconds, the scan
    starts at 09:35:05); every later iteration until 09:38 is `scan_quiet`."""
    start = et(TUE, 9, 33, 59) + timedelta(milliseconds=900)
    clock = FixedClock(start)
    stop = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(timedelta(seconds=seconds))
        if clock.now() > et(TUE, 9, 39, 30):
            stop.set()

    loop, rec, _ = make_loop(clock, sleep=sleep)
    await asyncio.wait_for(loop.run(stop), timeout=10)
    assert [c[3] for c in rec.calls] == [start, start + timedelta(minutes=5)]  # 09:33:59.9, 09:38:59.9


@pytest.mark.parametrize(
    ("day", "hms", "records"),
    [
        # the Monday after DST ends (EST): the window is in ET, not a fixed UTC offset
        (date(2026, 11, 2), (7, 49, 59), False),
        (date(2026, 11, 2), (7, 50, 0), True),
        (date(2026, 11, 2), (9, 35, 5), False),
        (date(2026, 11, 2), (16, 29, 59), True),
        (date(2026, 11, 2), (16, 30, 0), False),
        # the Monday after DST starts (EDT)
        (date(2026, 3, 9), (7, 50, 0), True),
        (date(2026, 3, 9), (9, 37, 59), False),
        (date(2026, 3, 9), (9, 38, 0), True),
        # Christmas Eve, a 13:00 close: the window ends at 13:30
        (date(2026, 12, 24), (13, 29, 59), True),
        (date(2026, 12, 24), (13, 30, 0), False),
        (date(2026, 12, 24), (15, 0, 0), False),
        # Independence Day observed (Fri 2026-07-03) and a Sunday: not sessions
        (date(2026, 7, 3), (10, 0, 0), False),
        (date(2026, 11, 1), (10, 0, 0), False),
    ],
)
@pytest.mark.usefixtures("no_running")
async def test_the_window_on_dst_early_close_and_holiday_days(
    day: date, hms: tuple[int, int, int], records: bool
) -> None:
    loop, rec, _ = make_loop(FixedClock(et(day, *hms)))
    step = await loop.run_once()
    assert (len(rec.calls) == 1) is records, step


# --- the loop: failures -------------------------------------------------------------------------------------


@pytest.mark.usefixtures("no_running")
async def test_one_warning_per_streak_even_across_skips_then_one_info() -> None:
    """A streak interrupted by skips (the log disabled for a while, the quiet minutes) is still ONE streak:
    no second warning; the next successful pass writes one info."""
    clock = FixedClock(et(TUE, 10, 0))
    enabled = {"on": True}
    loop, rec, events = make_loop(
        clock, settings=lambda: RuntimeSettings(reports_decisions_enabled=enabled["on"])
    )
    rec.fail = 3
    assert (await loop.run_once()).skipped == "error"
    enabled["on"] = False
    assert (await loop.run_once()).skipped == "disabled"
    enabled["on"] = True
    assert (await loop.run_once()).skipped == "error"
    clock.set(et(TUE, 9, 36) + timedelta(days=1))  # next day, in the quiet minutes
    assert (await loop.run_once()).skipped == "scan_quiet"
    clock.set(et(TUE, 10, 0) + timedelta(days=1))
    assert (await loop.run_once()).skipped == "error"
    assert (await loop.run_once()).skipped is None
    assert [e[0] for e in events.rows] == ["warning", "info"]
    assert all(e[0] not in ALERT_LEVELS for e in events.rows)


class Flaky:
    """A session factory whose database can go down and come back."""

    def __init__(self, real: sessionmaker[Session]) -> None:
        self.real = real
        self.kw = real.kw
        self.down = True

    def __call__(self) -> Session:
        if self.down:
            raise OperationalError("SELECT 1", {}, Exception("connection refused password=hunter2"))
        return self.real()


@pytest.mark.db
async def test_a_database_outage_never_raises_and_recovery_writes_one_unrelayed_info(
    db_factory: sessionmaker[Session],
) -> None:
    """Through the runtime's own event writer: during an outage the loop keeps going (every iteration an
    `error` skip, no exception, nothing relayed); on recovery one `info` event, source `decisions`."""
    with db_factory.begin() as s:
        run_id = add_run(s)
    clock = FixedClock(et(TUE, 10, 0))
    flaky = Flaky(db_factory)
    core = SimpleNamespace(
        factory=flaky, clock=clock, calendar=CAL, settings=SimpleNamespace(load=RuntimeSettings)
    )
    rec = FakeRecorder(clock=clock)
    loop = DecisionsLoop(
        rt.recorder_deps(core),  # type: ignore[arg-type]
        lambda: run_id,
        record=rec,
        event=rt.decisions_event_writer(core),  # type: ignore[arg-type]
    )
    for _ in range(4):
        assert (await loop.run_once()).skipped == "error"
        clock.advance(timedelta(seconds=60))
    assert rec.calls == []
    flaky.down = False
    assert (await loop.run_once()).skipped is None
    assert (await loop.run_once()).skipped is None
    with db_factory() as s:
        rows = s.execute(select(m.EventLog).order_by(m.EventLog.id)).scalars().all()
    assert [(r.source, r.level) for r in rows] == [
        ("decisions", "info")
    ]  # the warning was lost in the outage
    assert all(r.level not in ALERT_LEVELS for r in rows) and "hunter2" not in rows[0].message


# --- the worker: a hanging pass -----------------------------------------------------------------------------


@pytest.mark.db
async def test_a_hanging_pass_from_0933_never_delays_the_935_scan_or_the_stop(
    db_factory: sessionmaker[Session], no_running: None
) -> None:
    """A pass that starts at 09:33:59.9 and never returns: the worker still fires orb_open on time, keeps
    polling, stops promptly (the pass is cancelled, not awaited) and writes no worker error (no relayed
    alert)."""
    from tests.test_worker import Harness, VirtualTime, _events, _run
    from trader.worker import Worker, WorkerDeps

    clock = FixedClock(et(TUE, 9, 33, 59) + timedelta(milliseconds=900))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    calls: list[datetime] = []
    cancelled: list[bool] = []

    async def hang(
        deps: RecorderDeps,
        run_id: int,
        session_date: date,
        *,
        final: bool = False,
        rebuild: bool = False,
    ) -> RecordResult:
        calls.append(clock.now())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        raise AssertionError("unreachable")

    deps = RecorderDeps(NoDb(), clock, CAL, RuntimeSettings, None)  # type: ignore[arg-type]
    loop = DecisionsLoop(deps, lambda: 7, record=hang, sleep=vt.sleep, event=Events())
    stop = asyncio.Event()
    after_fire: list[int] = []

    def on_relay() -> None:
        if any(k == "orb_open" for k, _, _ in h.fire_calls):
            after_fire.append(1)
        if len(after_fire) >= 5:
            stop.set()

    h.on_relay = on_relay
    worker = Worker(WorkerDeps(**{**vars(h.deps()), "decisions": loop}))
    started = time.monotonic()
    await _run(worker, stop, vt)
    assert time.monotonic() - started < 5
    assert calls == [et(TUE, 9, 33, 59) + timedelta(milliseconds=900)]
    (fire,) = [at for k, _, at in h.fire_calls if k == "orb_open"]
    assert et(TUE, 9, 35, 5) <= fire <= et(TUE, 9, 35, 15)
    assert cancelled == [True]
    assert [e.level for e in _events(db_factory) if e.level in ALERT_LEVELS] == []


# --- the CLI hooks ------------------------------------------------------------------------------------------


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    monkeypatch.setattr(rt, "_record_event", lambda *a, **k: rows.append(a))
    return rows


def test_a_failing_hook_after_a_succeeded_premarket_keeps_exit_0_and_the_brief_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _premarket(monkeypatch, PRE_OPEN)
    order: list[str] = []

    async def brief(core: Any, day: date, text: str) -> None:
        order.append("brief")

    def broken_run(factory: Any) -> int:
        order.append("hook")
        raise OperationalError("SELECT", {}, Exception("server closed password=hunter2"))

    monkeypatch.setattr(rt, "send_premarket_brief", brief)
    monkeypatch.setattr(rt, "record_decisions_quietly", ORIGINAL_HOOK)
    monkeypatch.setattr(rt, "active_live_run_id", broken_run)
    events = _capture_events(monkeypatch)
    result = CliRunner().invoke(app, ["premarket"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("Pre-market brief\n")
    assert order == ["brief", "hook"]
    assert [(e[1], e[2]) for e in events] == [("warning", "decisions")]
    assert "hunter2" not in events[0][3] and "hunter2" not in result.output


@pytest.mark.parametrize(
    ("statuses", "hook_runs"),
    [
        (["fired", "failed"], True),
        (["fired", "missed"], True),
        (["skipped", "failed"], False),
        (["too_early", "missed"], False),
    ],
)
def test_event_due_mixed_results_exit_1_whatever_the_hook_does(
    monkeypatch: pytest.MonkeyPatch, statuses: list[str], hook_runs: bool
) -> None:
    results = [FireResult(f"k{i}", SESSION, s, {}) for i, s in enumerate(statuses)]  # type: ignore[arg-type]
    recorded: list[date] = []

    async def backup(core: Any, key: Any, day: date, **kw: Any) -> list[FireResult]:
        return results

    async def boom(deps: Any, run_id: int, d: date, **kw: Any) -> Any:
        recorded.append(d)
        raise RuntimeError("deadlock detected token=abc123")

    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: _fake_core())
    monkeypatch.setattr(rt, "install_log_mirror", lambda *a, **k: None)
    monkeypatch.setattr(rt, "event_backup", backup)
    monkeypatch.setattr(rt, "record_decisions_quietly", ORIGINAL_HOOK)
    monkeypatch.setattr(rt, "active_live_run_id", lambda factory: 1)
    monkeypatch.setattr(rt, "record_day", boom)
    events = _capture_events(monkeypatch)
    result = CliRunner().invoke(app, ["event", "--due"])
    assert result.exit_code == 1, result.output
    assert recorded == ([SESSION] if hook_runs else [])
    assert len(events) == (1 if hook_runs else 0)
    assert "abc123" not in result.output and "Traceback" not in result.output


def test_the_hooks_live_only_in_the_cli_and_only_on_success() -> None:
    """Static: jobs/premarket.py (a decision-path file under D2) never mentions the decision log; in
    cli.premarket the hook is in the succeeded branch only, after the brief; in cli.event it is guarded by a
    `fired` result."""
    app_dir = Path(trader.bootstrap.__file__).resolve().parent
    premarket_src = (app_dir / "jobs" / "premarket.py").read_text()
    assert "decisions" not in premarket_src.replace("decisions_", "")  # no import, no call
    assert "record_decisions" not in premarket_src and "trader.decisions" not in premarket_src

    tree = ast.parse((app_dir / "cli.py").read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def calls(node: ast.AST, name: str) -> list[int]:
        out = []
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if called == name:
                    out.append(n.lineno)
        return out

    pm = funcs["premarket"]
    (branch,) = [
        n
        for n in ast.walk(pm)
        if isinstance(n, ast.If)
        and "succeeded" in ast.unparse(n.test)
        and "out.status" in ast.unparse(n.test)
    ]
    body = ast.Module(body=branch.body, type_ignores=[])
    other = ast.Module(body=branch.orelse, type_ignores=[])
    (hook,) = calls(body, "_record_decisions")
    (brief,) = calls(body, "send_premarket_brief")
    assert brief < hook and calls(other, "_record_decisions") == []
    assert len(calls(pm, "_record_decisions")) == 1

    ev = funcs["event"]
    guards = [
        n
        for n in ast.walk(ev)
        if isinstance(n, ast.If) and calls(ast.Module(n.body, []), "_record_decisions")
    ]
    assert len(guards) == 1 and "fired" in ast.unparse(guards[0].test)


# --- the post-close -----------------------------------------------------------------------------------------


@pytest.mark.db
async def test_postclose_detail_shape_prune_error_and_in_summary_off(
    db_factory: sessionmaker[Session],
) -> None:
    """A prune failure is reported in the detail and the line still goes out; with
    `reports.decisions_in_summary` off the pass (and prune) still run, the detail is there, no line."""
    from trader.jobs.postclose import run_postclose

    w = _postclose_world(db_factory)
    now = et(TUE, 16, 15)
    passes: list[date] = []

    async def pruned_badly(d: date) -> FinalPass:
        passes.append(d)
        result = RecordResult(w.run_id, d, None, {"day": 1, "scan": 4}, True)
        return FinalPass(result, _day_summary(w.run_id), 5, None, "OperationalError")

    out = await run_postclose(dc.replace(w.deps(now), decisions=pruned_badly), TUE)
    assert out["decisions"] == {"final": True, "rows": 5, "pruned": {"error": "OperationalError"}}
    assert out["summary_sent"] is True
    (view,) = w.summaries()
    assert view.decision_log is not None and view.decision_log.link == "/reports?day=2026-10-06"

    w.settings = RuntimeSettings(reports_decisions_in_summary=False)

    async def fine(d: date) -> FinalPass:
        passes.append(d)
        result = RecordResult(w.run_id, d, None, {"day": 1}, True)
        return FinalPass(result, _day_summary(w.run_id), 3, PruneResult(2, 0))

    out = await run_postclose(dc.replace(w.deps(et(TUE, 16, 20)), decisions=fine), TUE)  # a forced re-run
    assert out["decisions"] == {"final": True, "rows": 3, "pruned": {"live": 2, "replay": 0}}
    assert passes == [TUE, TUE]
    assert len(w.summaries()) == 2 and w.summaries()[-1].decision_log is None


def test_the_line_escapes_rules_and_links_under_a_base_url_with_a_path() -> None:
    from tests.decisions.test_wiring import _summary_view
    from trader.jobs.postclose import decisions_line
    from trader.notify.messages import TELEGRAM_LIMIT, MessageRenderer
    from trader.notify.types import DecisionsLineView

    clock = FixedClock(et(TUE, 16, 15))
    render = MessageRenderer("https://lan.test/trader/", ZoneInfo("America/Edmonton"), clock=clock)
    line = dc.replace(
        decisions_line(_day_summary(1), TUE),
        top_rejects=(('"quoted"<rule>', 3), ("a&b", 2)),
    )
    msg = render.daily_summary(_summary_view(decision_log=line), ())
    (text,) = [x for x in msg.text.split("\n") if "scanned" in x]
    assert '<a href="https://lan.test/trader/reports?day=2026-10-06">details</a>' in text
    assert '"quoted"&lt;rule&gt; 3' in text  # text escaping (quote=False is fine outside attributes)
    assert "<rule>" not in text and "a&amp;b 2" in text
    plain = render.daily_summary(_summary_view(), ())
    assert (msg.silent, msg.kind) == (plain.silent, plain.kind) and len(msg.text) <= TELEGRAM_LIMIT
    empty = DecisionsLineView(0, 0, 0, 0, 0, 0, 0, 0, (), "/reports?day=2026-10-06")
    zero = [
        x
        for x in render.daily_summary(_summary_view(decision_log=empty), ()).text.split("\n")
        if "scanned" in x
    ]
    assert zero and "top rejects" not in zero[0] and "fill" not in zero[0]


# --- trader decisions ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["record", "--date", "2026-10-06"],
        ["record", "--date", "2026-10-06", "--run", "3"],
        ["show", "--date", "2026-10-06"],
        ["export", "--date", "2026-10-06"],
        ["prune"],
    ],
)
def test_a_database_error_exits_1_and_never_echoes_a_secret(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    fake = _fake_core()

    def broken() -> Any:
        raise OperationalError(
            "SELECT", {}, Exception("postgresql://trader:hunter2@db/trader password=hunter2 failed")
        )

    fake.factory = broken
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: fake)
    monkeypatch.setattr(rt, "install_log_mirror", lambda *a, **k: None)
    out = CliRunner().invoke(app, ["decisions", *args])
    assert out.exit_code == 1, (args, out.output)
    assert "hunter2" not in out.output and "Traceback" not in out.output
    if args[0] != "prune":  # a Saturday: "not a trading session", exit 0, the (broken) database untouched
        weekend = CliRunner().invoke(app, ["decisions", args[0], "--date", "2026-10-03", "--run", "5"])
        assert weekend.exit_code == 0 and "not a trading session" in weekend.output, weekend.output


# --- D2 -----------------------------------------------------------------------------------------------------


def _git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"git unavailable: {exc}")
    if done.returncode != 0:
        pytest.skip(f"git {' '.join(args)}: {done.stderr.strip()[:200]}")
    return done.stdout


DECISION_PATH = (
    "Trader/app/trader/strategies",
    "Trader/app/trader/engine",
    "Trader/app/trader/broker",
    "Trader/app/trader/market",
    "Trader/app/trader/jobs/nightly.py",
    "Trader/app/trader/jobs/premarket.py",
    "Trader/app/trader/adapters/claude/catalyst.py",
    "Trader/app/trader/settings_store.py",
)
RULE3_TESTS = tuple(
    f"Trader/app/tests/{d}" for d in ("engine", "strategies", "broker", "market", "jobs", "integration")
)


def test_d2_the_t9_to_t12_deploy_modifies_no_decision_path_file() -> None:
    """D2 (a)-(c) for the T11 deploy, from the deployed commit to the one under review: only ADDED
    `reports.decisions_*` fields in settings_store.py; no removed line in the rule-3 test folders and no
    modified file there except test_replay_isolation.py's MAY_CHANGE; golden files and crontab job lines
    unchanged."""
    _git("rev-parse", "--verify", "-q", f"{DEPLOYED}^{{commit}}")
    _git("rev-parse", "--verify", "-q", f"{BUILT}^{{commit}}")
    changed = _git("diff", "--name-status", DEPLOYED, BUILT, "--", *DECISION_PATH).split("\n")
    assert [x for x in changed if x] == ["M\tTrader/app/trader/settings_store.py"]
    diff = _git("diff", "-U0", DEPLOYED, BUILT, "--", "Trader/app/trader/settings_store.py")
    body = [x for x in diff.splitlines() if x[:1] in "+-" and not x.startswith(("+++", "---"))]
    assert not [x for x in body if x.startswith("-")]
    code = [x[1:].strip() for x in body if x[1:].strip() and not x[1:].strip().startswith("#")]
    assert code and all("reports" in x or x == ")" for x in code), code
    assert sum('alias="reports.decisions_' in x for x in code) == 6

    status = _git("diff", "--name-status", DEPLOYED, BUILT, "--", *RULE3_TESTS).split("\n")
    modified = sorted(x.split("\t", 1)[1] for x in status if x and not x.startswith("A"))
    assert modified == ["Trader/app/tests/integration/test_replay_isolation.py"]
    iso = _git("diff", "-U0", DEPLOYED, BUILT, "--", modified[0])
    iso_body = [x for x in iso.splitlines() if x[:1] in "+-" and not x.startswith(("+++", "---"))]
    assert iso_body == ['+        "decision_log",']

    assert _git("diff", "--name-only", DEPLOYED, BUILT, "--", "Trader/app/tests/replay/golden").strip() == ""
    cron = _git("diff", "-U0", DEPLOYED, BUILT, "--", "Trader/docker/crontab")
    removed = [x[1:] for x in cron.splitlines() if x.startswith("-") and not x.startswith("---")]
    assert all(x.startswith("#") for x in removed), removed


def test_d2_the_live_unchanged_test_compares_every_trading_table_and_the_chat() -> None:
    """T11 test 4 must compare the full trading rows AND the chat with the loop on vs off, dropping only the
    one line the decision log adds (not the existing human-decision "Decisions: n" line)."""
    from tests.integration import test_decisions_live_unchanged as t4
    from tests.integration.test_decisions_day import trading_rows

    src = inspect.getsource(trading_rows)
    for model in (
        "Candidate",
        "Signal",
        "Proposal",
        "Order",
        "Fill",
        "Trade",
        "CashLedger",
        "EquitySnapshot",
    ):
        assert f"m.{model})" in src, model
    test_src = inspect.getsource(t4.test_4_a_worker_day_is_identical_with_the_decisions_loop_on_and_off)
    assert "trading_rows(on_factory) == trading_rows(off_factory)" in test_src
    assert "chat_on == chat_off" in test_src
    manual = inspect.getsource(t4.manual_day)
    assert "run_once()" in manual and "reports.decisions_enabled" in manual

    sent = [SimpleNamespace(text="Daily\nDecisions: 2, average 1m\nDecisions: 5 scanned · 1 ranked\nEnd")]
    fake_world: Any = SimpleNamespace(api=SimpleNamespace(sent=sent))
    assert t4.chat(fake_world) == ["Daily\nDecisions: 2, average 1m\nEnd"]
