"""P3-T12 gauntlet breaker: the wiring (trader/runtime.py, the cron CLI commands, docker/crontab and
trader/notify/views.py) under the Phase 3 Review Focus.

Fakes and the testcontainers database only: FakeQuestrade, FakeCatalysts and FakeTelegramApi are injected
through the runtime's builders (as tests/test_runtime.py does). Never real Telegram, Questrade or Claude.
"""

import asyncio
import re
import threading
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer.main
from cryptography.fernet import Fernet
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs
from typer.testing import CliRunner, Result

import trader.bootstrap
import trader.config
import trader.logging_setup
import trader.notify.views
import trader.runtime as rt
import trader.worker
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_telegram import FakeMessenger, FakeRenderer, FakeTelegramApi
from tests.strategies.fakes import FakeCatalysts
from tests.test_runtime import (
    BOT_TOKEN,
    CHAT,
    DAY,
    FakeRunner,
    Trading,
    World,
    _QtContext,
    make_env,
    new_entry,
    new_stop,
    open_position,
    orders,
    tap_approve,
    trading,
    wired_bot,
)
from trader.adapters.questrade.auth import QuestradeAuth, QuestradeAuthError
from trader.adapters.questrade.client import QuestradeApiError
from trader.bootstrap import Core
from trader.broker.types import OrderSpec
from trader.cli import app
from trader.crypto import Crypto
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.risk import SizedOrder
from trader.jobs.nightly import earliest_run_time, target_session
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.notify.notifier import TelegramNotifier
from trader.notify.relay import NotificationRelay
from trader.notify.types import StatusView
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import Cancel, EnterLong, Exit
from trader.worker import Worker, acquire_single_instance, release_single_instance

pytestmark = pytest.mark.db

CAL = SessionCalendar()
THANKSGIVING = date(2026, 11, 26)
EARLY_CLOSE = date(2026, 11, 27)  # 13:00 ET close
CRONTAB = Path(__file__).resolve().parents[3] / "docker" / "crontab"
QT_SECRET = "QTSECRETVALUE0123456789"


def et(h: int, mi: int, s: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, mi, s, tzinfo=ET).astimezone(UTC)


@pytest.fixture
def world(
    db_factory: sessionmaker[Session], migrated_engine: SqlEngine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[World]:
    clock = FixedClock(et(9, 40))
    env = make_env()
    core = Core(
        env,
        migrated_engine,
        db_factory,
        Crypto(env.app_encryption_key.get_secret_value()),
        clock,
        CAL,
        SettingsStore(db_factory, now=clock.now),
    )
    w = World(core, clock, FakeTelegramApi(), _QtContext(FakeQuestrade()), FakeRunner())

    async def fake_catalysts(core: Core, stack: Any) -> FakeCatalysts:
        return FakeCatalysts()

    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    monkeypatch.setattr(rt, "build_telegram_api", lambda env: w.api)
    monkeypatch.setattr(rt, "questrade_client", lambda core: w.qt)
    monkeypatch.setattr(rt, "open_catalysts", fake_catalysts)
    yield w


def use_engine(world: World, monkeypatch: pytest.MonkeyPatch, runner: Any) -> None:
    async def fake_open_engine(core: Core, stack: Any, *, client: Any = None) -> Any:
        world.engines_opened.append(1)
        return runner

    monkeypatch.setattr(rt, "open_engine", fake_open_engine)


def job_rows(world: World, prefix: str) -> list[tuple[str, str]]:
    with world.factory() as s:
        rows = s.execute(
            select(m.JobRun.job, m.JobRun.status).where(m.JobRun.job.startswith(prefix)).order_by(m.JobRun.id)
        ).all()
    return [(j, st) for j, st in rows]


def invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


LOG_LINE = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \["
)  # structlog's console lines (logging is off here)


def lines_of(result: Result) -> list[str]:
    """What the command itself printed (not log lines)."""
    return [ln for ln in result.output.splitlines() if ln.strip() and not LOG_LINE.match(ln)]


def clean_exit(result: Result) -> bool:
    """Exited through typer/click (SystemExit), not an uncaught exception (a traceback in a real process)."""
    return result.exception is None or isinstance(result.exception, SystemExit)


# --- 1. entry guard on every approval path ---


def entry_sized(t: Trading) -> SizedOrder:
    intent = EnterLong(t.sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
    spec = OrderSpec(
        t.sym, "buy", "stop", 10, stop=Decimal("20.01"), stop_loss=Decimal("19.91"),
        strategy_config_id=t.cfg, reason="orb_breakout",
    )  # fmt: skip
    return SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10", "per_share_risk": "0.10"})


def cancel_sized(order_id: int, position_id: int) -> SizedOrder:
    return SizedOrder(
        Cancel(order_id, "replace_stop"),
        "cancel",
        10,
        None,
        cancel_order_id=order_id,
        position_id=position_id,
    )


def exit_sized(t: Trading, position_id: int) -> SizedOrder:
    spec = OrderSpec(t.sym, "sell", "market", 10, purpose="exit", position_id=position_id, reason="flatten")
    return SizedOrder(Exit(position_id, "market", None, "flatten"), "exit", 10, spec, position_id=position_id)


def proposal(world: World, pid: int) -> m.Proposal:
    with world.factory() as s:
        return s.get_one(m.Proposal, pid)


def order(world: World, oid: int) -> m.Order:
    with world.factory() as s:
        return s.get_one(m.Order, oid)


async def test_entry_guard_blocks_entries_on_every_approval_path_but_never_stops_exits_or_cancels(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paused or tripped: an entry approved by a tap on the worker's own bot, by the runtime decider (the
    web path) or by auto mode through the session engine places no order; a stop, a cancel and an exit
    approved the same ways still go through."""
    t = trading(world)
    position_id = open_position(world, t)
    base = len(orders(world, t.run_id))
    assert KillSwitches(world.factory, world.clock).pause(t.run_id, DAY, actor="telegram")

    # A. a tap on the bot that run_worker builds (not a hand-wired one)
    answers: dict[str, str | None] = {}

    async def tap_on_the_worker_bot(self: Worker, stop: Any, *, once: bool = False) -> None:
        assert self.deps.bot is not None
        bot = self.deps.bot.__self__  # type: ignore[attr-defined]
        answers["entry"] = await tap_approve(world, bot, new_entry(t))
        answers["stop"] = await tap_approve(world, bot, new_stop(t, position_id))

    with monkeypatch.context() as mp:
        mp.setattr(Worker, "run", tap_on_the_worker_bot)
        assert await rt.run_worker(once=True) == 0
    assert answers["entry"] is not None and "entry blocked" in answers["entry"]
    assert answers["stop"] == "Approved"
    after_a = orders(world, t.run_id)
    assert len(after_a) == base + 1 and after_a[-1].purpose == "stop" and after_a[-1].status == "working"
    stop_order_id = after_a[-1].id

    # C. auto mode through the session engine the runtime opens: the entry is refused, the cancel runs
    world.core.settings.set("approval_mode", "auto", "test")
    async with AsyncExitStack() as stack:
        engine = await rt.open_engine(world.core, stack)
        auto_entry = engine.proposals.create(t.signal_id, entry_sized(t), "entry").id
        auto_cancel = engine.proposals.create(
            t.signal_id, cancel_sized(stop_order_id, position_id), "cancel"
        ).id
    p = proposal(world, auto_entry)
    assert p.status == "rejected" and p.order_id is None and "entry blocked" in (p.error or "")
    assert proposal(world, auto_cancel).status == "submitted"
    assert order(world, stop_order_id).status != "working"

    # B. the runtime decider (the path a web approval takes) with an automatic kill switch tripped too
    world.core.settings.set("approval_mode", "manual", "test")
    with world.factory() as s:
        s.add(
            m.KillSwitchEvent(
                run_id=t.run_id, switch="max_drawdown_pct", session_date=DAY, tripped_at=world.clock.now()
            )
        )
        s.commit()
    decide = rt.build_decider(world.core, t.run_id)
    entry = decide(t.svc.create(t.signal_id, entry_sized(t), "entry").id, "approve", "web", "web:stephen")
    assert entry.blocked is not None and entry.order_id is None
    flatten = decide(
        t.svc.create(t.signal_id, exit_sized(t, position_id), "exit").id, "approve", "web", "web:stephen"
    )
    assert flatten.blocked is None and flatten.order_id is not None

    purposes = [o.purpose for o in orders(world, t.run_id)[base:]]
    assert purposes == ["stop", "exit"]  # not one entry order from any path


# --- 2. worker vs cron ---


class GatedRunner(FakeRunner):
    """Holds `flatten` open until the other process has finished, so the two really overlap."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate
        self._lock = threading.Lock()

    async def run_event(self, event_key: str, session_date: date) -> Any:
        with self._lock:
            self.events.append((event_key, session_date))
        if event_key == "flatten":
            await asyncio.to_thread(self.gate.wait, 15)
        return None


def test_a_worker_once_run_racing_the_cron_flatten_backup_flattens_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    runner = GatedRunner(gate)
    use_engine(world, monkeypatch, runner)
    [flatten] = [e for e in rt.plan_builder(world.core)(DAY).events if e.key == "flatten"]
    world.clock.set(flatten.at)  # the worker's step and the 15:55 cron line land on the same moment
    out: dict[str, Any] = {}

    def worker() -> None:
        try:
            out["worker"] = asyncio.run(rt.run_worker(once=True))
        finally:
            gate.set()

    def cron() -> None:
        try:
            out["cron"] = asyncio.run(rt.event_backup(world.core, "flatten", DAY))
        finally:
            gate.set()

    threads = [threading.Thread(target=worker), threading.Thread(target=cron)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert out["worker"] == 0
    [cron_result] = out["cron"]
    assert cron_result.status in ("fired", "skipped")
    assert runner.events.count(("flatten", DAY)) == 1
    assert [st for j, st in job_rows(world, "event:flatten")] == ["succeeded"]


# --- 3. exit codes ---


class FailingRunner(FakeRunner):
    async def run_event(self, event_key: str, session_date: date) -> Any:
        raise QuestradeApiError(401, f"https://api01.iq.questrade.com/v1/markets?access_token={QT_SECRET}")


def test_exit_codes_second_worker_2_lost_lock_3_missed_or_failed_event_1_holiday_0(
    world: World, migrated_engine: SqlEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # python -m trader.worker: a second worker exits 2, a lost lock 3, a holiday once-run 0
    world.clock.set(et(10, 0, day=THANKSGIVING))
    rival = acquire_single_instance(migrated_engine)
    assert rival is not None
    try:
        assert trader.worker.main(["--once"]) == 2
    finally:
        release_single_instance(rival)
    assert trader.worker.main(["--once"]) == 0
    with monkeypatch.context() as mp:

        async def lost(self: Worker, stop: Any, *, once: bool = False) -> None:
            raise SystemExit(3)

        mp.setattr(Worker, "run", lost)
        assert trader.worker.main(["--once"]) == 3

    # the cron side: a missed event and a failed event each exit 1 with one line and no traceback
    use_engine(world, monkeypatch, FailingRunner())
    world.clock.set(et(9, 40))  # orb_open at 09:35 is past its 120 s grace
    missed = invoke(["event", "orb_open"])
    assert missed.exit_code == 1 and clean_exit(missed), missed.output
    assert len(lines_of(missed)) == 1 and "missed" in missed.output
    world.clock.set(et(11, 30, 5))
    failed = invoke(["event", "entry_cancel"])
    assert failed.exit_code == 1 and clean_exit(failed), failed.output
    assert len(lines_of(failed)) == 1 and "failed" in failed.output
    assert QT_SECRET not in failed.output

    # a holiday: every job command says so and exits 0, building nothing
    world.clock.set(et(10, 0, day=THANKSGIVING))
    world.engines_opened.clear()
    for args in (
        ["preopen"],
        ["checkin", "--at", "11:30"],
        ["event", "flatten"],
        ["event", "--due"],
        ["postclose"],
    ):
        result = invoke(args)
        assert result.exit_code == 0 and "not a trading session" in result.output, (args, result.output)
    assert world.engines_opened == [] and world.qt.entered == 0


# --- 4. CLI force rules ---


def test_forced_due_is_refused_and_a_forced_checkin_never_forces_its_backup_fire(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_engine(world, monkeypatch, world.runner)
    world.clock.set(et(11, 31))
    for args in (["event", "--due", "--force"], ["event", "--force", "--due"]):
        refused = invoke(args)
        assert refused.exit_code == 2 and "refused" in refused.output and clean_exit(refused)
    assert job_rows(world, "") == [] and world.engines_opened == []  # nothing ran, nothing was built

    world.clock.set(et(9, 36))
    assert invoke(["event", "orb_open"]).exit_code == 0
    world.clock.set(et(11, 31))
    assert invoke(["checkin", "--at", "11:30"]).exit_code == 0  # backs up entry_cancel
    world.clock.set(et(11, 32))
    forced = invoke(["checkin", "--at", "11:30", "--force"])
    assert forced.exit_code == 0, forced.output
    assert world.runner.events == [("orb_open", DAY), ("entry_cancel", DAY)]  # nothing re-run
    assert [st for _, st in job_rows(world, "event:")] == ["succeeded", "succeeded"]


# --- 5. no secret and no traceback on a Questrade or Telegram failure ---


class EndOfSessionFails(FakeRunner):
    async def end_of_session(self, session_date: date) -> list[Any]:
        raise QuestradeApiError(401, f"GET /v1/accounts?access_token={QT_SECRET} refused")


def test_no_command_prints_a_secret_or_a_traceback_on_a_questrade_or_telegram_failure(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    problems: list[str] = []
    telegram_error = httpx.ConnectError(f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage failed")

    def check(name: str, result: Result, code: int) -> None:
        if result.exit_code != code:
            problems.append(f"{name}: exit {result.exit_code}, expected {code}")
        if not clean_exit(result):
            problems.append(f"{name}: uncaught {type(result.exception).__name__} (a traceback in cron)")
        for secret in (QT_SECRET, BOT_TOKEN, BOT_TOKEN.split(":")[1]):
            if secret in result.output:
                problems.append(f"{name}: printed a secret: {result.output.strip()!r}")

    # Questrade fails inside the post-close body
    use_engine(world, monkeypatch, EndOfSessionFails())
    world.clock.set(et(16, 15))
    check("postclose", invoke(["postclose"]), 1)

    # Telegram fails for every send of the pre-open check (it still succeeds: Telegram never blocks)
    world.api.fail("send_message", telegram_error, times=10)
    world.clock.set(et(9, 20))
    check("preopen", invoke(["preopen"]), 0)

    # telegram-test with a client error that carries the bot URL
    world.api.fail("send_message", telegram_error, times=10)
    monkeypatch.setattr(trader.config, "get_env", lambda: world.core.env)
    check("telegram-test", invoke(["telegram-test"]), 1)

    # token-refresh when the stored chain can't be decrypted (APP_ENCRYPTION_KEY changed): one line, exit 1
    def undecryptable(self: QuestradeAuth, *a: Any, **k: Any) -> None:
        Fernet(Fernet.generate_key()).decrypt(b"not-a-fernet-token")

    monkeypatch.setattr(QuestradeAuth, "keep_alive", undecryptable)
    check("token-refresh", invoke(["token-refresh"]), 1)
    with world.factory() as s:
        token_events = s.execute(select(m.EventLog.id).where(m.EventLog.source == "questrade.token")).all()
    if not token_events:
        problems.append("token-refresh: a non-QuestradeAuthError failure wrote no questrade.token event")
    assert not problems, "\n".join(problems)


# --- 6. logging is configured before anything logs ---


def test_configure_logging_runs_before_the_first_log_line(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_engine(world, monkeypatch, world.runner)
    world.clock.set(et(9, 40))  # orb_open is missed: the scheduler logs a warning
    with capture_logs() as logs:
        seen: list[tuple[str, int]] = []

        def configure(process: str, **kwargs: Any) -> None:
            seen.append((process, len(logs)))

        monkeypatch.setattr(trader.logging_setup, "configure_logging", configure)
        result = invoke(["event", "orb_open"])
        assert result.exit_code == 1
        cli_logged = len(logs)
        bare = Core(**{**world.core.__dict__, "env": make_env(telegram=False)})
        monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: bare)  # it warns: no Telegram
        world.clock.set(et(10, 0))
        assert trader.worker.main(["--once"]) == 0
    assert seen == [("cron", 0), ("worker", cli_logged)], seen
    assert cli_logged > 0 and len(logs) > cli_logged  # both really logged, after configuring


# --- 7. the crontab ---

FIELD = re.compile(r"^(\*|\d+(-\d+)?(,\d+(-\d+)?)*)$")


def _values(field: str, lo: int, hi: int) -> set[int]:
    if field == "*":
        return set(range(lo, hi + 1))
    out: set[int] = set()
    for part in field.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def crontab() -> list[tuple[int, int, set[int], list[str]]]:
    """(minute, hour, cron weekdays 0=Sun, argv after `trader`) for each line, checking the syntax."""
    lines = [ln.strip() for ln in CRONTAB.read_text().splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    assert lines[0] == "CRON_TZ=America/New_York"
    out = []
    for line in lines[1:]:
        fields = line.split()
        minute, hour, dom, month, dow = fields[:5]
        assert all(FIELD.match(f) for f in fields[:5]), line
        assert dom == "*" and month == "*", line
        assert fields[5] == "trader", line
        out.append((int(minute), int(hour), _values(dow, 0, 6), fields[6:]))
    return out


def fires(entry: tuple[int, int, set[int], list[str]], day: date) -> datetime | None:
    minute, hour, dows, _ = entry
    if (day.weekday() + 1) % 7 not in dows:
        return None
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).astimezone(UTC)


def test_crontab_parses_its_commands_exist_and_its_times_suit_the_live_plan_in_edt_and_est(
    world: World,
) -> None:
    entries = crontab()
    group = typer.main.get_command(app)
    for *_, argv in entries:
        cmd = group.get_command(None, argv[0])  # type: ignore[attr-defined]
        assert cmd is not None, argv
        cmd.make_context(argv[0], list(argv[1:]))  # its options parse

    def times(*argv: str, day: date) -> list[datetime]:
        found = [fires(e, day) for e in entries if e[3] == list(argv)]
        return sorted(t for t in found if t is not None)

    settings = RuntimeSettings()
    grace = timedelta(seconds=settings.scheduler_late_grace_seconds)
    plan = rt.plan_builder(world.core)
    start = date(2026, 10, 1)
    days = (start + timedelta(days=i) for i in range(92))  # October to December: DST ends Nov 1
    sessions = [d for d in days if CAL.is_session(d)]
    assert EARLY_CLOSE in sessions and THANKSGIVING not in sessions
    for day in sessions:
        opens, close = CAL.session_open(day), CAL.session_close(day)
        events = {e.key: e.at for e in plan(day).events}
        [orb] = times("event", "orb_open", day=day)
        assert timedelta(0) <= orb - events["orb_open"] <= grace, day  # the backup can still fire it
        flattens = times("event", "flatten", day=day)
        assert any(events["flatten"] <= f < close for f in flattens), day  # early closes included
        [checkin] = times("checkin", "--at", "11:30", day=day)
        assert events["entry_cancel"] <= checkin < close, day
        assert all(t < opens for t in times("premarket", day=day) + times("preopen", day=day)), day
        assert all(t >= close for t in times("postclose", day=day)), day
        nightly = [t for d in range(1, 6) for t in times("nightly", day=day - timedelta(days=d))]
        ok = [
            t for t in nightly
            if earliest_run_time(CAL, day, settings.open_bar_lookback_sessions) <= t < opens
            and target_session(CAL, FixedClock(t)) == day
        ]  # fmt: skip
        assert ok, f"no nightly run prepares {day}"
    # wall-clock ET across the November change: 09:36 ET is 13:36Z in EDT and 14:36Z in EST
    assert times("event", "orb_open", day=date(2026, 10, 6)) == [datetime(2026, 10, 6, 13, 36, tzinfo=UTC)]
    assert times("event", "orb_open", day=date(2026, 12, 1)) == [datetime(2026, 12, 1, 14, 36, tzinfo=UTC)]


# --- 8. no Telegram ---


async def test_the_worker_runs_without_telegram_with_one_warning(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = Core(**{**world.core.__dict__, "env": make_env(telegram=False)})
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: bare)

    def no_client(env: Any) -> Any:
        raise AssertionError("a Telegram client was built without Telegram configured")

    monkeypatch.setattr(rt, "build_telegram_api", no_client)
    use_engine(world, monkeypatch, world.runner)
    world.clock.set(et(9, 36))  # in session: the step fires orb_open
    with capture_logs() as logs:
        assert await rt.run_worker(once=True) == 0
    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert [e["event"] for e in warnings if "telegram" in e["event"]] == ["worker.telegram_not_configured"]
    assert world.runner.events == [("orb_open", DAY)]
    with world.factory() as s:
        hb = s.get(m.WorkerHeartbeat, "worker")
        cursors = s.execute(select(m.NotifyCursor.stream)).scalars().all()
        notes = s.execute(select(m.Notification.id)).all()
    assert hb is not None and hb.phase == "stopped"
    assert cursors == [] and notes == []  # no relay ran
    assert world.api.calls == []


# --- 9. one Questrade client per worker process ---


class ClientEngine(FakeRunner):
    def __init__(self, n: int, client: Any) -> None:
        super().__init__()
        self.n = n
        self.client = client


async def test_one_questrade_client_per_worker_and_each_session_stack_closes_before_the_next_opens(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal: list[tuple[str, int]] = []
    clients: list[Any] = []

    async def fake_open_engine(core: Core, stack: AsyncExitStack, *, client: Any = None) -> ClientEngine:
        n = len(clients) + 1
        clients.append(client)
        journal.append(("open", n))

        async def closed() -> None:
            journal.append(("close", n))

        stack.push_async_callback(closed)
        return ClientEngine(n, client)

    monkeypatch.setattr(rt, "open_engine", fake_open_engine)
    world.qt.qt.add_symbol("AAA", 11)
    world.qt.qt.set_quote(11, "20.00", "20.01", "20.00", world.clock.now())
    day2 = CAL.next_session(DAY)

    async def two_sessions(self: Worker, stop: Any, *, once: bool = False) -> None:
        first = await self.deps.engine_for(DAY)
        await first.client.quotes([11])  # type: ignore[attr-defined]
        second = await self.deps.engine_for(day2)
        await second.client.quotes([11])  # type: ignore[attr-defined]
        assert self.deps.bot is not None
        commands = self.deps.bot.__self__.commands.target  # type: ignore[attr-defined]
        await commands.deps.quotes([])  # the /positions quotes share it too
        journal.append(("worker done", 0))

    monkeypatch.setattr(Worker, "run", two_sessions)
    assert await rt.run_worker(once=True) == 0
    assert len(clients) == 2 and clients[0] is clients[1] and clients[0] is not None
    assert journal[:4] == [("open", 1), ("close", 1), ("open", 2), ("worker done", 0)]
    assert ("close", 2) in journal  # closed when the worker stopped
    assert world.qt.entered == 1  # one Questrade client for the whole process


# --- 10. shared views ---


def budget_entry(t: Trading) -> SizedOrder:
    intent = EnterLong(t.sym, "stop", Decimal("20.01"), None, Decimal("19.87"), "orb_breakout", {})
    spec = OrderSpec(
        t.sym, "buy", "stop", 33, stop=Decimal("20.01"), stop_loss=Decimal("19.87"),
        strategy_config_id=t.cfg, reason="orb_breakout",
    )  # fmt: skip
    sizing = {"shares": "33", "per_share_risk": "0.14", "risk_dollars": "5.00"}
    return SizedOrder(intent, "entry", 33, spec, sizing=sizing)


async def test_bot_and_relay_show_the_same_risk_and_status_and_checkin_the_same_status_view(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_engine(world, monkeypatch, world.runner)
    world.clock.set(et(11, 31))
    t = trading(world)

    # ProposalView: the bot's approval message and the relay's auto-mode message
    bot_render, relay_render = FakeRenderer(), FakeRenderer()
    relay = NotificationRelay(
        world.factory,
        world.clock,
        TelegramNotifier(world.api, CHAT, world.factory, world.clock),
        relay_render,
        FakeMessenger(),
        t.run_id,
        settings=world.core.settings.load,
    )
    await relay.pump()  # cursors start at the current rows
    bot = wired_bot(world, t)
    bot.render = bot_render
    manual = t.svc.create(t.signal_id, budget_entry(t), "entry").id
    assert await bot.send_proposal(manual)
    world.core.settings.set("approval_mode", "auto", "test")
    t.svc.create(t.signal_id, budget_entry(t), "entry")
    world.clock.set(et(11, 31, 2))
    await relay.pump()
    [bot_view] = [a[0] for name, a in bot_render.calls if name == "proposal"]
    [relay_view] = [a[0] for name, a in relay_render.calls if name == "proposal"]
    assert bot_view.risk_usd == relay_view.risk_usd == Decimal("4.62")  # 33 x 0.14, not the 5.00 budget
    world.core.settings.set("approval_mode", "manual", "test")

    # StatusView: /status (the worker's Commands) and the 11:30 check-in, at the same moment
    open_position(world, t)
    world.qt.qt.add_symbol("AAA", 11)
    world.qt.qt.set_quote(11, "20.40", "20.41", "20.40", world.clock.now())
    await rt.event_backup(world.core, None, DAY, due=True)  # settle what is due, so neither fires more
    views: list[StatusView] = []
    real = trader.notify.views.status_view

    async def spy(**kwargs: Any) -> StatusView:
        view = await real(**kwargs)
        views.append(view)
        return view

    monkeypatch.setattr(trader.notify.views, "status_view", spy)
    out = await rt.checkin_job(world.core, DAY, "11:30", force=False)
    assert out.status == "succeeded"

    async def ask_status(self: Worker, stop: Any, *, once: bool = False) -> None:
        assert self.deps.bot is not None
        await self.deps.bot.__self__.commands.target.handle("/status")  # type: ignore[attr-defined]

    monkeypatch.setattr(Worker, "run", ask_status)
    assert await rt.run_worker(once=True) == 0
    checkin_view, status_view = views
    assert checkin_view.positions and checkin_view.positions[0].last == Decimal("20.40")
    assert checkin_view == status_view


# --- 11. premarket ---


class _Scraper:
    def __init__(self, **kwargs: object) -> None:
        pass

    def __enter__(self) -> "_Scraper":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_premarket_sends_the_brief_once_across_two_runs_through_the_real_notifier(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes (the second a forced re-run that succeeds again): one Telegram message, because the
    dedupe key `premarket:<date>` lives in the notifications table, not in a process."""
    import trader.adapters.finviz.scraper
    import trader.jobs.runner
    from trader.jobs.runner import JobOutcome

    monkeypatch.setattr(trader.adapters.finviz.scraper, "FinvizScraper", _Scraper)
    monkeypatch.setattr(
        trader.jobs.runner, "run_job", lambda *a, **k: JobOutcome("succeeded", {"brief": "Gappers: AAA"})
    )
    world.clock.set(et(8, 0))
    first = invoke(["premarket"])
    second = invoke(["premarket", "--force"])
    assert first.exit_code == 0 and second.exit_code == 0, (first.output, second.output)
    sent = world.api.calls_of("send_message")
    assert len(sent) == 1 and "Gappers: AAA" in sent[0]["text"] and sent[0]["chat_id"] == CHAT
    with world.factory() as s:
        keys = s.execute(select(m.Notification.dedupe_key)).scalars().all()
    assert keys == ["premarket:2026-10-06"]


# --- 12. token-refresh failure reaches Telegram through the relay ---


async def test_token_refresh_failure_writes_an_event_that_the_relay_forwards_as_a_token_alert(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.clock.set(et(2, 0))
    run_id = trading(world).run_id
    relay = NotificationRelay(
        world.factory,
        world.clock,
        TelegramNotifier(world.api, CHAT, world.factory, world.clock),
        rt.build_renderer(world.core),
        FakeMessenger(),
        run_id,
        settings=world.core.settings.load,
    )
    await relay.pump()  # the running worker's relay, before the 02:00 cron line

    def dead_chain(self: QuestradeAuth, *a: Any, **k: Any) -> None:
        raise QuestradeAuthError(
            "Token refresh failed (the refresh token has already been used or has expired). "
            "Generate a new manual token in Questrade."
        )

    monkeypatch.setattr(QuestradeAuth, "keep_alive", dead_chain)
    result = await asyncio.to_thread(invoke, ["token-refresh"])
    assert result.exit_code == 1 and clean_exit(result)
    with world.factory() as s:
        [row] = s.execute(select(m.EventLog).where(m.EventLog.source == "questrade.token")).scalars().all()
    assert row.level == "error" and row.run_id is None
    world.clock.set(et(2, 0, 3))
    report = await relay.pump()
    assert report.events == 1
    [alert] = world.api.calls_of("send_message")
    assert "already been used" in alert["text"] and alert["chat_id"] == CHAT
    with world.factory() as s:
        [note] = s.execute(select(m.Notification)).scalars().all()
    assert note.kind == "token_failure" and note.status == "sent"
