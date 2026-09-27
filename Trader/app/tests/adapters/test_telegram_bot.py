"""P3-T6: the Telegram bot: chat filter, signed single-use callbacks, approvals, pause and journal answers,
proposal messages and the polling loop. FakeTelegramApi, FakeRenderer and a real test database."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from tests.fakes_telegram import FakeRenderer, FakeTelegramApi
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer
from trader.adapters.telegram.types import ProposalMessenger, TelegramApiError, Update
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.engine.proposals import Decision, DecisionResult, ProposalService, Via
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.notify.types import OutboundMessage, ProposalView
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import EnterLong

pytestmark = pytest.mark.db
CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)
CHAT = 4242
FROM = 777
SIGNER = CallbackSigner.derive("test-session-secret")


class FakeDecide:
    """Records calls; returns `result`, or raises `raises` (once each, in order)."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, Decision, Via, str]] = []
        self.result = DecisionResult("submitted", False, 1)
        self.raises: list[BaseException] = []

    def __call__(self, proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
        self.calls.append((proposal_id, decision, via, actor))
        if self.raises:
            raise self.raises.pop(0)
        return self.result


class FakeCommands:
    def __init__(self) -> None:
        self.handled: list[str] = []
        self.confirmed: list[str] = []
        self.on_handle: Callable[[], None] | None = None

    async def handle(self, text: str) -> list[OutboundMessage]:
        self.handled.append(text)
        if self.on_handle is not None:
            self.on_handle()
        return [OutboundMessage(kind="reply", text=f"reply to {text}")]

    async def confirm_pause(self, action: str) -> str:
        self.confirmed.append(action)
        return "Paused: new entries are blocked." if action == "y" else "Cancelled."


@dataclass
class Env:
    bot: TelegramBot
    api: FakeTelegramApi
    render: FakeRenderer
    decide: FakeDecide
    commands: FakeCommands
    issuer: DbCallbackIssuer
    svc: ProposalService
    clock: FixedClock
    factory: sessionmaker[Session]
    run_id: int
    sym: int
    cfg: int
    signal_id: int
    sleeps: list[float] = field(default_factory=list)


def _make_bot(env: Env, decide: Callable[[int, Decision, Via, str], DecisionResult]) -> TelegramBot:
    async def fake_sleep(seconds: float) -> None:
        env.sleeps.append(seconds)

    return TelegramBot(
        env.api,
        CHAT,
        env.factory,
        env.clock,
        env.issuer,
        SIGNER,
        decide,
        env.commands,
        env.render,
        env.run_id,
        settings=RuntimeSettings,
        sleep=fake_sleep,
    )


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        sig = m.Signal(
            run_id=run.id,
            strategy_config_id=cfg,
            symbol_id=sym,
            session_date=T.date(),
            event_key="orb_open",
            ts=T,
            intent={},
            evidence={},
        )
        s.add(sig)
        s.commit()
        signal_id = sig.id
    store = SettingsStore(db_factory, now=clock.now)
    broker = SimBroker(db_factory, clock, Ledger(CAL), QuoteFillModel(FillParams()), run.id)
    svc = ProposalService(db_factory, clock, store, broker, run.id)
    e = Env(
        bot=None,  # type: ignore[arg-type]
        api=FakeTelegramApi(),
        render=FakeRenderer(),
        decide=FakeDecide(),
        commands=FakeCommands(),
        issuer=DbCallbackIssuer(db_factory, clock, SIGNER),
        svc=svc,
        clock=clock,
        factory=db_factory,
        run_id=run.id,
        sym=sym,
        cfg=cfg,
        signal_id=signal_id,
    )
    e.bot = _make_bot(e, e.decide)
    return e


def real_bot(env: Env) -> TelegramBot:
    return _make_bot(env, env.svc.decide)


def new_entry(env: Env) -> int:
    intent = EnterLong(env.sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
    spec = OrderSpec(
        env.sym,
        "buy",
        "stop",
        10,
        stop=Decimal("20.01"),
        stop_loss=Decimal("19.91"),
        strategy_config_id=env.cfg,
        reason="orb_breakout",
    )
    sized = SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10", "per_share_risk": "0.10"})
    return env.svc.create(env.signal_id, sized, "entry").id


async def sent_proposal(env: Env, bot: TelegramBot | None = None) -> tuple[int, int, dict[str, str]]:
    """A pending entry proposal sent through the bot: (proposal id, message id, {action: data})."""
    pid = new_entry(env)
    assert await (bot or env.bot).send_proposal(pid)
    (row,) = env.api.last_sent()["buttons"]
    data = {"a": row[0].callback_data, "r": row[1].callback_data}
    message_id = len(env.api.calls_of("send_message"))
    return pid, message_id, data


def answers(env: Env) -> list[str | None]:
    return [kw["text"] for kw in env.api.calls_of("answer_callback")]


def events(env: Env, level: str | None = None) -> list[m.EventLog]:
    with env.factory() as s:
        q = select(m.EventLog).where(m.EventLog.source == "telegram")
        if level is not None:
            q = q.where(m.EventLog.level == level)
        return list(s.execute(q).scalars())


def tap(env: Env, data: str, message_id: int | None, chat_id: int = CHAT) -> Update:
    return env.api.callback_update(data, chat_id=chat_id, message_id=message_id, from_id=FROM)


# --- 1. Approve ----------
async def test_approve_calls_decide_once_answers_then_edits(env: Env) -> None:
    pid, mid, data = await sent_proposal(env)
    await env.bot.handle_update(tap(env, data["a"], mid))
    assert env.decide.calls == [(pid, "approve", "telegram", f"telegram:{FROM}")]
    names = [name for name, _ in env.api.calls if name in ("answer_callback", "edit_message")]
    assert names == ["answer_callback", "edit_message"]
    assert answers(env) == ["Approved"]
    edit = env.api.calls_of("edit_message")[0]
    assert edit["message_id"] == mid and edit["buttons"] == () and edit["chat_id"] == CHAT
    method, args = env.render.calls[-1]
    assert method == "proposal_closed" and args[1] == "submitted"


async def test_reject_answers_rejected(env: Env) -> None:
    pid, mid, data = await sent_proposal(env)
    env.decide.result = DecisionResult("rejected", False)
    await env.bot.handle_update(tap(env, data["r"], mid))
    assert env.decide.calls == [(pid, "reject", "telegram", f"telegram:{FROM}")]
    assert answers(env) == ["Rejected"]


async def test_a_failed_order_and_an_earlier_decision_are_answered_as_such(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    env.decide.result = DecisionResult("failed", False)
    await env.bot.handle_update(tap(env, data["a"], mid))
    _, mid2, data2 = await sent_proposal(env)
    env.decide.result = DecisionResult("rejected", True)
    await env.bot.handle_update(tap(env, data2["a"], mid2))
    assert answers(env) == ["Approved, but the order failed", "Already rejected"]


# --- 2. Replay ----------
async def test_a_replayed_callback_decides_once(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    await env.bot.handle_update(tap(env, data["a"], mid))
    await env.bot.handle_update(tap(env, data["a"], mid))
    assert len(env.decide.calls) == 1
    assert answers(env) == ["Approved", "Already answered"]


# --- 3. Forged ----------
async def test_a_forged_callback_is_refused(env: Env) -> None:
    pid, mid, data = await sent_proposal(env)
    body, mac = data["a"].rsplit(":", 1)
    bad_mac = f"{body}:{mac[:-1]}{'B' if mac[-1] == 'A' else 'A'}"
    other_id = data["a"].replace(f"p:{pid}:", f"p:{pid + 1}:", 1)
    await env.bot.handle_update(tap(env, bad_mac, mid))
    await env.bot.handle_update(tap(env, other_id, mid))
    assert env.decide.calls == []
    assert answers(env) == ["Invalid button", "Invalid button"]
    assert len(events(env, "warning")) == 2


# --- 4. Foreign chat ----------
async def test_a_callback_from_another_chat_is_ignored(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    await env.bot.handle_update(tap(env, data["a"], mid, chat_id=999))
    assert env.decide.calls == [] and answers(env) == []
    (ev,) = events(env, "warning")
    assert ev.data["chat_id"] == 999
    assert data["a"] not in repr(ev.data) and data["a"] not in ev.message


async def test_a_text_from_another_chat_is_ignored_without_logging_the_text(env: Env) -> None:
    await env.bot.handle_update(env.api.text_update("/pause secret words", chat_id=999))
    assert env.commands.handled == [] and env.api.calls_of("send_message") == []
    (ev,) = events(env, "warning")
    assert "secret" not in ev.message and "secret" not in repr(ev.data)


# --- 5. Double decision ----------
async def test_reject_after_approve_on_the_same_message_is_refused(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    await env.bot.handle_update(tap(env, data["a"], mid))
    await env.bot.handle_update(tap(env, data["r"], mid))
    assert [c[1] for c in env.decide.calls] == ["approve"]
    assert answers(env) == ["Approved", "Already answered"]


async def test_a_web_decision_first_then_a_tap_gives_already_submitted(env: Env) -> None:
    bot = real_bot(env)
    pid, mid, data = await sent_proposal(env, bot)
    assert env.svc.decide(pid, "approve", "web", "web:stephen").status == "submitted"
    await bot.handle_update(tap(env, data["a"], mid))
    assert answers(env) == ["Already submitted"]
    with env.factory() as s:
        assert s.scalar(select(func.count()).select_from(m.Order)) == 1
        assert s.get(m.Proposal, pid).decided_via == "web"  # type: ignore[union-attr]


# --- 6. Expired proposal ----------
async def test_a_tap_after_the_proposal_expired(env: Env) -> None:
    bot = real_bot(env)
    pid, mid, data = await sent_proposal(env, bot)
    env.clock.advance(timedelta(minutes=6))
    await bot.handle_update(tap(env, data["a"], mid))
    assert answers(env) == ["Already expired"]
    assert len(env.api.calls_of("edit_message")) == 1
    method, args = env.render.calls[-1]
    assert method == "proposal_closed" and args[1] == "expired"
    with env.factory() as s:
        assert s.get(m.Proposal, pid).status == "expired"  # type: ignore[union-attr]


# --- 7. Telegram failures never undo a decision ----------
async def test_a_failed_answer_still_decides_and_edits(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    env.api.fail("answer_callback", TelegramApiError(400, "Bad Request: query is too old"))
    await env.bot.handle_update(tap(env, data["a"], mid))
    assert len(env.decide.calls) == 1
    assert len(env.api.calls_of("edit_message")) == 1


async def test_a_failed_edit_is_logged_and_the_loop_continues(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    env.api.fail("edit_message", TelegramApiError(400, "Bad Request: message is not modified"))
    stop = asyncio.Event()
    env.commands.on_handle = stop.set
    env.api.queue_updates(tap(env, data["a"], mid), env.api.text_update("/status", chat_id=CHAT))
    await asyncio.wait_for(env.bot.run(stop), timeout=5)
    assert len(env.decide.calls) == 1 and env.commands.handled == ["/status"]


# --- 8. decide raising ----------
async def test_an_error_in_decide_releases_the_nonce(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    env.decide.raises = [RuntimeError("db down")]
    await env.bot.handle_update(tap(env, data["a"], mid))
    await env.bot.handle_update(tap(env, data["a"], mid))
    assert len(env.decide.calls) == 2
    assert answers(env) == ["Error, try again", "Approved"]


async def test_an_unknown_proposal_keeps_the_nonce_used(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    env.decide.raises = [KeyError("proposal 1 not found")]
    await env.bot.handle_update(tap(env, data["a"], mid))
    await env.bot.handle_update(tap(env, data["a"], mid))
    assert len(env.decide.calls) == 1
    assert answers(env) == ["Unknown proposal", "Already answered"]


# --- 9. Pause confirmation ----------
async def test_pause_confirmation_goes_to_the_command_handler(env: Env) -> None:
    nonce, data = env.issuer.issue("pause", str(env.run_id), ["y", "n"], CHAT, 60)
    env.issuer.bind(nonce, 5)
    await env.bot.handle_update(tap(env, data["y"], 5))
    assert env.commands.confirmed == ["y"]
    assert answers(env) == ["Paused: new entries are blocked."]
    assert env.api.calls_of("edit_buttons") == [{"chat_id": CHAT, "message_id": 5, "buttons": ()}]


async def test_an_expired_pause_button(env: Env) -> None:
    nonce, data = env.issuer.issue("pause", str(env.run_id), ["y", "n"], CHAT, 60)
    env.issuer.bind(nonce, 5)
    env.clock.advance(timedelta(seconds=61))
    await env.bot.handle_update(tap(env, data["y"], 5))
    assert env.commands.confirmed == [] and answers(env) == ["Button expired"]


# --- 10. Old message ----------
async def test_a_callback_on_another_message_is_refused(env: Env) -> None:
    _, mid, data = await sent_proposal(env)
    await env.bot.handle_update(tap(env, data["a"], mid + 10))
    assert env.decide.calls == [] and answers(env) == ["Old message"]
    await env.bot.handle_update(tap(env, data["a"], mid))  # the nonce was not used up
    assert len(env.decide.calls) == 1


# --- 11. Proposal messages ----------
async def test_send_proposal_once_then_resend(env: Env) -> None:
    messenger: ProposalMessenger = env.bot
    pid = new_entry(env)
    assert await messenger.send_proposal(pid) is True
    assert await messenger.send_proposal(pid) is False
    assert len(env.api.calls_of("send_message")) == 1
    first = env.api.last_sent()["buttons"]
    assert await messenger.send_proposal(pid, resend=True) is True
    second = env.api.last_sent()["buttons"]
    assert len(env.api.calls_of("send_message")) == 2
    assert first[0][0].callback_data != second[0][0].callback_data  # a new nonce
    with env.factory() as s:
        rows = list(s.execute(select(m.TelegramCallback).order_by(m.TelegramCallback.message_id)).scalars())
    assert [(r.kind, r.ref, r.message_id, r.expires_at) for r in rows] == [
        ("proposal", str(pid), 1, None),
        ("proposal", str(pid), 2, None),
    ]


async def test_the_proposal_view(env: Env) -> None:
    pid = new_entry(env)
    await env.bot.send_proposal(pid)
    method, args = env.render.calls[-1]
    view = args[0]
    assert method == "proposal" and isinstance(view, ProposalView)
    assert (view.proposal_id, view.kind, view.status, view.ticker) == (pid, "entry", "pending", "AAA")
    assert (view.side, view.order_type, view.qty) == ("buy", "stop", 10)
    assert (view.stop, view.limit, view.stop_loss) == (Decimal("20.01"), None, Decimal("19.91"))
    assert view.risk_usd == Decimal("1.00") and view.reason == "orb_breakout"
    assert view.strategy_key == "orb_sip" and view.expires_at == T + timedelta(minutes=5)
    (row,) = args[1]
    assert [b.text for b in row] == ["✅ Approve", "❌ Reject"]


async def test_a_non_pending_or_unknown_proposal_is_not_sent(env: Env) -> None:
    pid = new_entry(env)
    env.svc.decide(pid, "reject", "web", "web:stephen")
    assert await env.bot.send_proposal(pid) is False
    assert await env.bot.send_proposal(pid + 100) is False
    assert env.api.calls_of("send_message") == []


async def test_a_refused_send_can_be_retried(env: Env) -> None:
    pid = new_entry(env)
    env.api.fail("send_message", TelegramApiError(400, "Bad Request: chat not found"))
    assert await env.bot.send_proposal(pid) is False
    assert await env.bot.send_proposal(pid) is True


async def test_sync_closed_edits_a_message_decided_on_the_web(env: Env) -> None:
    pid, mid, data = await sent_proposal(env)
    await sent_proposal(env)  # still pending: left alone
    assert await env.bot.sync_closed() == 0
    env.svc.decide(pid, "reject", "web", "web:stephen")
    assert await env.bot.sync_closed() == 1
    edit = env.api.calls_of("edit_message")[-1]
    assert edit["message_id"] == mid and edit["buttons"] == ()
    method, args = env.render.calls[-1]
    assert method == "proposal_closed" and args[1:] == ("rejected", "web")
    assert await env.bot.sync_closed() == 0  # closed once
    await env.bot.handle_update(tap(env, data["a"], mid))
    assert env.decide.calls == [] and answers(env) == ["Already answered"]


# --- 12. Journal ----------
async def test_a_journal_answer_is_saved(env: Env) -> None:
    _, data = env.issuer.issue("journal", "20261006", ["y", "n"], CHAT, None)
    await env.bot.handle_update(tap(env, data["y"], 9))
    with env.factory() as s:
        row = s.get(m.Journal, (env.run_id, date(2026, 10, 6)))
    assert row is not None and row.rules_followed is True and row.answered_via == "telegram"
    assert row.updated_at == T
    assert answers(env) == ["Saved"]
    assert env.api.calls_of("edit_buttons") == [{"chat_id": CHAT, "message_id": 9, "buttons": ()}]
    assert env.render.calls[-1] == ("journal_answered", (date(2026, 10, 6), True))
    assert len(env.api.calls_of("send_message")) == 1


async def test_a_journal_answer_overwrites_the_empty_row(env: Env) -> None:
    with env.factory() as s:
        s.add(m.Journal(run_id=env.run_id, session_date=date(2026, 10, 6), notes="kept"))
        s.commit()
    _, data = env.issuer.issue("journal", "20261006", ["y", "n"], CHAT, None)
    await env.bot.handle_update(tap(env, data["n"], 9))
    with env.factory() as s:
        row = s.get(m.Journal, (env.run_id, date(2026, 10, 6)))
    assert row is not None and row.rules_followed is False and row.notes == "kept"


# --- 13. The polling loop ----------
async def test_a_conflict_logs_one_critical_event_and_backs_off(env: Env) -> None:
    stop = asyncio.Event()
    conflict = TelegramApiError(409, "Conflict: terminated by other getUpdates request")
    env.api.fail("get_updates", conflict, times=2)

    async def fake_sleep(seconds: float) -> None:
        env.sleeps.append(seconds)
        if len(env.sleeps) == 2:
            stop.set()

    env.bot.sleep = fake_sleep
    await asyncio.wait_for(env.bot.run(stop), timeout=5)
    assert env.sleeps == [30, 30]
    assert len(events(env, "critical")) == 1


async def test_other_errors_back_off_exponentially(env: Env) -> None:
    stop = asyncio.Event()
    env.api.fail("get_updates", TelegramApiError(None, "timed out"), times=8)

    async def fake_sleep(seconds: float) -> None:
        env.sleeps.append(seconds)
        if len(env.sleeps) == 8:
            stop.set()

    env.bot.sleep = fake_sleep
    await asyncio.wait_for(env.bot.run(stop), timeout=5)
    assert env.sleeps == [1, 2, 4, 8, 16, 32, 60, 60]


async def test_a_command_goes_to_the_handler_and_its_reply_is_sent(env: Env) -> None:
    stop = asyncio.Event()
    env.commands.on_handle = stop.set
    env.api.queue_updates(env.api.text_update("/status", chat_id=CHAT))
    await asyncio.wait_for(env.bot.run(stop), timeout=5)
    assert env.commands.handled == ["/status"]
    assert env.api.last_sent()["text"] == "reply to /status"
    first = env.api.calls_of("get_updates")[0]
    assert first == {"offset": None, "timeout": 30}


async def test_the_offset_confirms_handled_updates(env: Env) -> None:
    stop = asyncio.Event()
    u1 = env.api.text_update("/status", chat_id=CHAT)
    u2 = env.api.text_update("/help", chat_id=CHAT)
    env.api.queue_updates(u1, u2)
    seen: list[Any] = []

    def on_handle() -> None:
        seen.append(1)
        if len(seen) == 2:
            stop.set()

    env.commands.on_handle = on_handle
    await asyncio.wait_for(env.bot.run(stop), timeout=5)
    assert env.commands.handled == ["/status", "/help"]  # each once
    stop2 = asyncio.Event()
    calls_before = len(env.api.calls_of("get_updates"))

    async def stop_after_one(seconds: float) -> None:
        stop2.set()

    env.api.fail("get_updates", TelegramApiError(None, "x"))
    env.bot.sleep = stop_after_one
    await asyncio.wait_for(env.bot.run(stop2), timeout=5)
    assert env.api.calls_of("get_updates")[calls_before]["offset"] == u2.update_id + 1


async def test_a_bad_update_does_not_stop_the_loop(env: Env) -> None:
    stop = asyncio.Event()
    env.commands.on_handle = stop.set
    env.api.queue_updates(tap(env, "p:1:a:zzzzzzzz:0123456789abcdef", 1))  # invalid, answered
    env.api.fail("answer_callback", RuntimeError("unexpected"))  # not even a TelegramApiError
    env.api.queue_updates(env.api.text_update("/status", chat_id=CHAT))
    await asyncio.wait_for(env.bot.run(stop), timeout=5)
    assert env.commands.handled == ["/status"]


async def test_a_failing_pause_confirmation_releases_the_nonce(env: Env) -> None:
    nonce, data = env.issuer.issue("pause", str(env.run_id), ["y", "n"], CHAT, 60)
    env.issuer.bind(nonce, 5)
    calls: list[str] = []

    async def flaky(action: str) -> str:
        calls.append(action)
        if len(calls) == 1:
            raise RuntimeError("db down")
        return "Paused: new entries are blocked."

    env.commands.confirm_pause = flaky  # type: ignore[method-assign]
    await env.bot.handle_update(tap(env, data["y"], 5))
    await env.bot.handle_update(tap(env, data["y"], 5))
    assert calls == ["y", "y"]
    assert answers(env) == ["Error, try again", "Paused: new entries are blocked."]
