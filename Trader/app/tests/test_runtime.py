"""P3-T12: the composition root. A test Core on the testcontainers database, FakeQuestrade, FakeCatalysts
and FakeTelegramApi injected through the runtime's builders; no network, no real sleeping."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
import trader.runtime as rt
from tests.factories import add_strategy_config, add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_telegram import FakeRenderer, FakeTelegramApi
from tests.strategies.fakes import FakeCatalysts
from trader.adapters.questrade.auth import QuestradeAuth, QuestradeAuthError
from trader.adapters.telegram.api import PtbTelegramApi
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer
from trader.bootstrap import Core
from trader.broker.types import OrderSpec
from trader.cli import app
from trader.config import EnvSettings
from trader.crypto import Crypto
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.proposals import ProposalService
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.engine.scheduler import DayPlan, PlanProblem
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.notify.messages import MessageRenderer
from trader.notify.notifier import NullNotifier, TelegramNotifier
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import EnterLong, Exit
from trader.worker import Worker, acquire_single_instance, release_single_instance

pytestmark = pytest.mark.db

REAL_BUILD_API = rt.build_telegram_api
CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tue
CHAT = 4242
SECRET = "test-session-secret"
BOT_TOKEN = "123456:TEST-TOKEN-NOT-REAL"


def et(h: int, mi: int, s: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, mi, s, tzinfo=ET).astimezone(UTC)


def make_env(*, telegram: bool = True) -> EnvSettings:
    return EnvSettings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr("postgresql+psycopg://unused"),
        migration_database_url=SecretStr("postgresql+psycopg://unused"),
        app_encryption_key=SecretStr(Fernet.generate_key().decode()),
        session_secret=SecretStr(SECRET),
        telegram_bot_token=SecretStr(BOT_TOKEN) if telegram else None,
        telegram_chat_id=CHAT if telegram else None,
        public_base_url="https://trader.test",
        tz_display="America/Edmonton",
        anthropic_api_key=None,
    )


class _QtContext:
    def __init__(self, qt: FakeQuestrade) -> None:
        self.qt = qt
        self.entered = 0

    async def __aenter__(self) -> FakeQuestrade:
        self.entered += 1
        return self.qt

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeRunner:
    """Stands in for the Engine: records events and session ends."""

    def __init__(self) -> None:
        self.events: list[tuple[str, date]] = []
        self.ended: list[date] = []

    async def run_event(self, event_key: str, session_date: date) -> Any:
        self.events.append((event_key, session_date))
        return None

    async def poll_quotes(self) -> list[Any]:
        return []

    async def tick(self, now: datetime) -> None:
        return None

    async def end_of_session(self, session_date: date) -> list[Any]:
        self.ended.append(session_date)
        return []


@dataclass
class World:
    core: Core
    clock: FixedClock
    api: FakeTelegramApi
    qt: _QtContext
    runner: FakeRunner
    engines_opened: list[int] = field(default_factory=list)

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory


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


def use_fake_engine(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_open_engine(core: Core, stack: Any, *, client: Any = None) -> FakeRunner:
        world.engines_opened.append(1)
        return world.runner

    monkeypatch.setattr(rt, "open_engine", fake_open_engine)


def job_runs(world: World, prefix: str = "") -> list[tuple[str, str, str | None]]:
    with world.factory() as s:
        rows = s.execute(
            select(m.JobRun.job, m.JobRun.status, m.JobRun.error)
            .where(m.JobRun.job.startswith(prefix))
            .order_by(m.JobRun.id)
        ).all()
    return [(j, st, e) for j, st, e in rows]


# --- 1. builders --------------------------------------------------------------------------------------------


def test_build_notifier_is_null_without_telegram_and_telegram_with_it(world: World) -> None:
    bare = Core(**{**world.core.__dict__, "env": make_env(telegram=False)})
    assert isinstance(rt.build_notifier(bare), NullNotifier)
    notifier = rt.build_notifier(world.core, world.api)
    assert isinstance(notifier, TelegramNotifier)
    assert world.api.calls == []  # building sends nothing


def test_build_notifier_builds_a_real_client_without_network(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rt, "build_telegram_api", REAL_BUILD_API)  # constructing it makes no request
    notifier = rt.build_notifier(world.core)
    assert isinstance(notifier, TelegramNotifier)
    assert isinstance(notifier.api, PtbTelegramApi)


async def test_open_telegram_is_none_without_configuration(world: World) -> None:
    from contextlib import AsyncExitStack

    bare = Core(**{**world.core.__dict__, "env": make_env(telegram=False)})
    async with AsyncExitStack() as stack:
        assert await rt.open_telegram(bare, stack) is None
        assert await rt.open_telegram(world.core, stack) is world.api
    assert world.api.calls_of("aclose")  # closed with the stack


def test_renderer_and_signer_come_from_the_environment(world: World) -> None:
    renderer = rt.build_renderer(world.core)
    assert isinstance(renderer, MessageRenderer)
    assert renderer.base_url == "https://trader.test" and str(renderer.tz) == "America/Edmonton"
    assert renderer.clock is world.clock  # alerts from another day show their date by the injected clock
    signer = rt.build_signer(world.core)
    data = signer.data("p", "12", "a", "abcdefgh")
    assert CallbackSigner.derive(SECRET).parse(data) is not None


# --- 2. SAFETY: the bot's decider honours the kill switches and /pause --------------------------------------


@dataclass
class Trading:
    run_id: int
    sym: int
    cfg: int
    signal_id: int
    svc: ProposalService


def trading(world: World) -> Trading:
    run = get_live_run(world.factory, world.clock, RuntimeSettings())
    with world.factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        sig = m.Signal(
            run_id=run.id,
            strategy_config_id=cfg,
            symbol_id=sym,
            session_date=DAY,
            event_key="orb_open",
            ts=world.clock.now(),
            intent={},
            evidence={},
        )
        s.add(sig)
        s.commit()
        signal_id = sig.id
    broker = rt.build_sim_broker(world.core, run.id, RuntimeSettings())
    svc = ProposalService(world.factory, world.clock, world.core.settings, broker, run.id)
    return Trading(run.id, sym, cfg, signal_id, svc)


def new_entry(t: Trading) -> int:
    intent = EnterLong(t.sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
    spec = OrderSpec(
        t.sym, "buy", "stop", 10, stop=Decimal("20.01"), stop_loss=Decimal("19.91"),
        strategy_config_id=t.cfg, reason="orb_breakout",
    )  # fmt: skip
    sized = SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10", "per_share_risk": "0.10"})
    return t.svc.create(t.signal_id, sized, "entry").id


def open_position(world: World, t: Trading) -> int:
    from trader.adapters.questrade.models import QtQuote

    broker = rt.build_sim_broker(world.core, t.run_id, RuntimeSettings())
    broker.submit(OrderSpec(t.sym, "buy", "market", 10, stop_loss=Decimal("19.00"), strategy_config_id=t.cfg))
    now = world.clock.now()
    q = QtQuote(
        t.sym, "AAA", Decimal("20.00"), Decimal("20.01"), Decimal("20.00"), None, 1000,
        now - timedelta(seconds=1), 0, False, None,
    )  # fmt: skip
    (fill,) = broker.on_quotes([q], now)
    assert fill.position_id is not None
    return fill.position_id


def new_stop(t: Trading, position_id: int) -> int:
    spec = OrderSpec(
        t.sym, "sell", "stop", 10, stop=Decimal("19.00"), purpose="stop", position_id=position_id,
        reason="protective_stop",
    )  # fmt: skip
    sized = SizedOrder(
        Exit(position_id, "stop", Decimal("19.00"), "protective_stop"),
        "stop",
        10,
        spec,
        position_id=position_id,
    )
    return t.svc.create(t.signal_id, sized, "stop").id


class NoCommands:
    async def handle(self, text: str) -> list[Any]:
        return []

    async def confirm_pause(self, action: str) -> str:
        return "Cancelled."


def wired_bot(world: World, t: Trading) -> TelegramBot:
    signer = rt.build_signer(world.core)
    return TelegramBot(
        world.api,
        CHAT,
        world.factory,
        world.clock,
        DbCallbackIssuer(world.factory, world.clock, signer),
        signer,
        rt.build_decider(world.core, t.run_id),
        NoCommands(),
        FakeRenderer(),
        t.run_id,
        settings=RuntimeSettings,
    )


async def tap_approve(world: World, bot: TelegramBot, proposal_id: int) -> str | None:
    assert await bot.send_proposal(proposal_id)
    (row,) = world.api.last_sent()["buttons"]
    message_id = len(world.api.calls_of("send_message"))
    await bot.handle_update(world.api.callback_update(row[0].callback_data, CHAT, message_id))
    answer: str | None = world.api.calls_of("answer_callback")[-1]["text"]
    return answer


def orders(world: World, run_id: int) -> list[m.Order]:
    with world.factory() as s:
        return list(s.execute(select(m.Order).where(m.Order.run_id == run_id).order_by(m.Order.id)).scalars())


async def test_paused_entry_tapped_places_no_order_but_a_stop_goes_through(world: World) -> None:
    t = trading(world)
    position_id = open_position(world, t)
    before = len(orders(world, t.run_id))
    assert KillSwitches(world.factory, world.clock).pause(t.run_id, DAY, actor="telegram")
    bot = wired_bot(world, t)

    entry = new_entry(t)
    answer = await tap_approve(world, bot, entry)
    assert answer is not None and "entry blocked" in answer and "manual_pause" in answer
    assert len(orders(world, t.run_id)) == before  # no order for the entry
    with world.factory() as s:
        p = s.get_one(m.Proposal, entry)
        assert p.status == "rejected" and p.error is not None and "entry blocked" in p.error

    stop = new_stop(t, position_id)
    assert await tap_approve(world, bot, stop) == "Approved"
    after = orders(world, t.run_id)
    assert len(after) == before + 1
    assert after[-1].purpose == "stop" and after[-1].status == "working"


def test_decider_blocks_an_entry_while_a_kill_switch_is_tripped(world: World) -> None:
    t = trading(world)
    with world.factory() as s:
        s.add(
            m.KillSwitchEvent(
                run_id=t.run_id, switch="max_drawdown_pct", session_date=DAY, tripped_at=world.clock.now()
            )
        )
        s.commit()
    result = rt.build_decider(world.core, t.run_id)(new_entry(t), "approve", "telegram", "telegram:1")
    assert result.blocked == "kill switch max_drawdown_pct is tripped" and result.order_id is None


def test_decider_submits_an_entry_when_nothing_blocks(world: World) -> None:
    t = trading(world)
    result = rt.build_decider(world.core, t.run_id)(new_entry(t), "approve", "telegram", "telegram:1")
    assert result.blocked is None and result.order_id is not None


# --- 3. plans: live_day_plan plus report_plan_problems ------------------------------------------------------


def test_plan_builder_uses_live_day_plan_and_reports_problems_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, date]] = []
    problem = PlanProblem("key_time_conflict", "flatten", "flatten at two times", ("a", "b"))

    def fake_live(factory: Any, registry: Any, run_id: int, cal: Any, day: date, settings: Any) -> DayPlan:
        calls.append((run_id, day))
        return DayPlan(day, True, None, None, (), (problem,))

    monkeypatch.setattr(rt, "live_day_plan", fake_live)
    plan = rt.plan_builder(world.core)
    plan(DAY)
    plan(DAY)
    run = get_live_run(world.factory, world.clock, RuntimeSettings())
    assert calls == [(run.id, DAY), (run.id, DAY)]
    with world.factory() as s:
        events = s.execute(select(m.EventLog).where(m.EventLog.source == "scheduler")).scalars().all()
    assert len(events) == 1 and events[0].level == "error"  # reported once per session


def test_plan_builder_plans_the_default_strategies_on_a_fresh_database(world: World) -> None:
    plan = rt.plan_builder(world.core)(DAY)
    keys = [e.key for e in plan.events]
    assert {"orb_open", "entry_cancel", "overlay_decision", "flatten"} <= set(keys)


# --- 4. event backups ---------------------------------------------------------------------------------------


async def test_event_backup_fires_once_and_then_skips(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_engine(world, monkeypatch)
    world.clock.set(et(9, 36))
    [first] = await rt.event_backup(world.core, "orb_open", DAY)
    [second] = await rt.event_backup(world.core, "orb_open", DAY)
    assert (first.status, second.status) == ("fired", "skipped")
    assert world.runner.events == [("orb_open", DAY)]


async def test_a_skipped_or_missed_backup_builds_no_engine(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_engine(world, monkeypatch)
    world.clock.set(et(9, 40))  # 295 s late with a 120 s grace
    [missed] = await rt.event_backup(world.core, "orb_open", DAY)
    world.clock.set(et(12, 55))  # the 12:55 flatten backup on a normal day
    [early] = await rt.event_backup(world.core, "flatten", DAY)
    assert (missed.status, early.status) == ("missed", "too_early")
    assert world.engines_opened == [] and world.qt.entered == 0  # no engine, no Questrade client


async def test_event_key_force_is_bound_into_fire(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_engine(world, monkeypatch)
    world.clock.set(et(9, 30))  # too early without force
    [result] = await rt.event_backup(world.core, "orb_open", DAY, force=True)
    assert result.status == "fired" and world.runner.events == [("orb_open", DAY)]


async def test_event_due_force_is_rejected(world: World) -> None:
    with pytest.raises(rt.ForcedDueRejected):
        await rt.event_backup(world.core, None, DAY, due=True, force=True)


async def test_event_due_fires_only_the_unsettled_due_events(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_engine(world, monkeypatch)
    world.clock.set(et(9, 36))
    await rt.event_backup(world.core, "orb_open", DAY)
    world.clock.set(et(11, 31))
    results = await rt.event_backup(world.core, None, DAY, due=True)
    assert [(r.key, r.status) for r in results] == [("entry_cancel", "fired")]
    assert world.runner.events == [("orb_open", DAY), ("entry_cancel", DAY)]


async def test_event_backup_on_a_holiday_is_not_a_session(world: World) -> None:
    [result] = await rt.event_backup(world.core, "flatten", date(2026, 11, 26))
    assert result.status == "not_session"


# --- 5. cron jobs -------------------------------------------------------------------------------------------


async def test_checkin_backup_is_never_forced_and_the_message_is_sent_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_engine(world, monkeypatch)
    seen: list[Any] = []
    real = rt.run_checkin

    async def spy(deps: Any, session_date: date, at_label: str) -> dict[str, Any]:
        seen.append(deps)
        return await real(deps, session_date, at_label)

    monkeypatch.setattr(rt, "run_checkin", spy)
    world.clock.set(et(11, 31))
    await rt.event_backup(world.core, "entry_cancel", DAY)  # the worker fired it already
    first = await rt.checkin_job(world.core, DAY, "11:30", force=False)
    again = await rt.checkin_job(world.core, DAY, "11:30", force=True)  # a forced job re-run
    assert first.status == again.status == "succeeded"
    fired = {f["key"]: f["status"] for f in again.detail["fired"]}
    assert fired.get("entry_cancel", "skipped") == "skipped"  # the backup fire is not forced
    assert world.runner.events.count(("entry_cancel", DAY)) == 1
    assert len(world.api.calls_of("send_message")) == 1  # dedupe checkin:<date>:<label>
    assert [j for j, _, _ in job_runs(world, "checkin")] == ["checkin@11:30", "checkin@11:30"]
    deps = seen[0]
    assert deps.token_health is not None and deps.killswitches is not None
    world.clock.set(et(9, 30))
    assert (await deps.fire("orb_open", DAY)).status == "too_early"  # its fire is never forced


async def test_preopen_job_sends_its_checks(world: World) -> None:
    world.clock.set(et(9, 20))
    out = await rt.preopen_job(world.core, DAY, force=False)
    assert out.status == "succeeded" and out.detail["ok"] is False  # no token, no universe, no worker
    [sent] = world.api.calls_of("send_message")
    assert "Pre-open" in sent["text"] and sent["chat_id"] == CHAT
    assert world.qt.entered == 0  # the token check and the universe status need no Questrade client
    assert [j for j, _, _ in job_runs(world, "preopen")] == ["preopen"]


async def test_postclose_builds_the_engine_only_when_its_body_runs(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_engine(world, monkeypatch)
    world.clock.set(et(16, 15))
    first = await rt.postclose_job(world.core, DAY, force=False)
    second = await rt.postclose_job(world.core, DAY, force=False)
    assert first.status == "succeeded" and second.status == "skipped"
    assert world.runner.ended == [DAY] and world.engines_opened == [1]
    assert len(world.api.calls_of("send_message")) == 1  # the daily summary
    [summary] = world.api.calls_of("send_message")
    assert summary["buttons"] and len(summary["buttons"][0]) == 2  # Rules followed? Yes / No


def test_token_refresh_failure_writes_the_token_error_event(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self: QuestradeAuth) -> None:
        raise QuestradeAuthError("refresh token already used or expired")

    monkeypatch.setattr(QuestradeAuth, "keep_alive", fail)
    result = CliRunner().invoke(app, ["token-refresh"])
    assert result.exit_code == 1
    with world.factory() as s:
        [row] = s.execute(select(m.EventLog).where(m.EventLog.source == "questrade.token")).scalars().all()
    assert row.level == "error" and "already used or expired" in row.message


# --- 6. the worker ------------------------------------------------------------------------------------------


def heartbeat(world: World) -> m.WorkerHeartbeat | None:
    with world.factory() as s:
        return s.get(m.WorkerHeartbeat, "worker")


async def test_run_worker_once_on_a_weekend(world: World) -> None:
    world.clock.set(datetime(2026, 10, 3, 16, 0, tzinfo=UTC))  # Saturday
    assert await rt.run_worker(once=True) == 0
    hb = heartbeat(world)
    assert hb is not None and hb.phase == "stopped" and hb.beat_at == world.clock.now()
    assert world.api.calls_of("get_updates") == []  # --once never polls Telegram


async def test_run_worker_once_in_a_session_uses_the_live_plan(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 10:00 ET the 09:35 entry event is too late: the worker's plan (live_day_plan) and fire_event
    record it as missed, once. The relay pumps once (its cursors are created), the bot never polls."""
    use_fake_engine(world, monkeypatch)
    plans: list[date] = []
    real_live = rt.live_day_plan

    def spy(*args: Any) -> DayPlan:
        plans.append(args[4])
        result: DayPlan = real_live(*args)
        return result

    monkeypatch.setattr(rt, "live_day_plan", spy)
    world.clock.set(et(10, 0))
    assert await rt.run_worker(once=True) == 0
    assert DAY in plans
    assert [(j, s) for j, s, _ in job_runs(world, "event:")] == [("event:orb_open", "failed")]
    assert job_runs(world, "event:")[0][2] is not None and job_runs(world, "event:")[0][2].startswith(
        "missed:"
    )  # type: ignore[union-attr]
    hb = heartbeat(world)
    assert hb is not None and hb.phase == "stopped" and hb.session_date == DAY
    with world.factory() as s:
        streams = set(s.execute(select(m.NotifyCursor.stream)).scalars())
    assert {"proposals", "fills", "events"} <= streams
    assert world.api.calls_of("get_updates") == []


async def test_run_worker_does_not_take_the_lock_itself(world: World, migrated_engine: SqlEngine) -> None:
    """Worker.run takes the single-instance lock. Were run_worker to take it first, Worker.run's own
    pg_try_advisory_lock (another connection of the same process) would be refused and it would exit 2:
    the once-run above returning 0 proves it doesn't. With a real rival holding the lock, it exits 2."""
    world.clock.set(datetime(2026, 10, 3, 16, 0, tzinfo=UTC))
    rival = acquire_single_instance(migrated_engine)
    assert rival is not None
    try:
        assert await rt.run_worker(once=True) == 2
    finally:
        release_single_instance(rival)
    assert await rt.run_worker(once=True) == 0


async def test_run_worker_maps_a_lost_lock_to_exit_3(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def lost(self: Worker, stop: Any, *, once: bool = False) -> None:
        raise SystemExit(3)

    monkeypatch.setattr(Worker, "run", lost)
    assert await rt.run_worker(once=True) == 3


async def test_run_worker_without_telegram_runs_no_bot_or_relay(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = Core(**{**world.core.__dict__, "env": make_env(telegram=False)})
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: bare)
    captured: list[Any] = []

    async def capture(self: Worker, stop: Any, *, once: bool = False) -> None:
        captured.append(self.deps)

    monkeypatch.setattr(Worker, "run", capture)
    assert await rt.run_worker(once=True) == 0
    [deps] = captured
    assert deps.relay is None and deps.bot is None and deps.process == "worker"


async def test_the_worker_deps_share_one_plan_and_bind_no_force(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_engine(world, monkeypatch)
    captured: list[Any] = []

    async def capture(self: Worker, stop: Any, *, once: bool = False) -> None:
        captured.append(self.deps)

    monkeypatch.setattr(Worker, "run", capture)
    assert await rt.run_worker(once=True) == 0
    [deps] = captured
    assert deps.relay is not None and deps.bot is not None
    world.clock.set(et(9, 30))
    assert (await deps.fire("orb_open", DAY)).status == "too_early"  # not forced
    engine = await deps.engine_for(DAY)
    assert engine is world.runner and await deps.engine_for(DAY) is engine  # one per session
    await deps.end_session(DAY, lambda: _ok())
    assert [j for j, _, _ in job_runs(world, "session_end")] == ["session_end"]


async def _ok() -> dict[str, Any]:
    return {"open_positions": 0}


def test_worker_main_configures_logging_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.logging_setup
    import trader.worker

    order: list[str] = []
    monkeypatch.setattr(trader.logging_setup, "configure_logging", lambda process, **k: order.append(process))

    async def fake_run_worker(once: bool = False) -> int:
        order.append(f"run once={once}")
        return 0

    monkeypatch.setattr(rt, "run_worker", fake_run_worker)
    assert trader.worker.main(["--once"]) == 0
    assert order == ["worker", "run once=True"]


async def test_forwarding_commands_before_and_after_the_target() -> None:
    forward = rt.ForwardingCommands()
    assert await forward.handle("/status") == []

    class Target:
        async def handle(self, text: str) -> list[Any]:
            return [text]

        async def confirm_pause(self, action: str) -> str:
            return f"confirmed {action}"

    forward.target = Target()  # type: ignore[assignment]
    assert await forward.handle("/status") == ["/status"]
    assert await forward.confirm_pause("y") == "confirmed y"
