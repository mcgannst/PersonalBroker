"""P3-T6 gauntlet breaker (phase Review Focus 2): forged, replayed or foreign Telegram callbacks, double
decisions, ordering races, re-tapped buttons, Telegram failures and secrets.

FakeTelegramApi, FakeRenderer and the testcontainers database only; never real Telegram. Decisions go through
the real ProposalService (with the kill-switch entry guard), so an order count is the ground truth.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.factories import add_strategy_config, add_symbol
from tests.fakes_telegram import FakeMessenger, FakeRenderer, FakeTelegramApi
from trader.adapters.questrade.models import QtQuote
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer
from trader.adapters.telegram.commands import CommandDeps, Commands
from trader.adapters.telegram.types import CallbackQuery, TelegramApiError, Update
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.proposals import Decision, DecisionResult, ProposalService, Via
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.notify.types import OutboundMessage
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import EnterLong, Exit

pytestmark = pytest.mark.db
CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # 09:40 ET, a normal session
DAY = date(2026, 10, 6)
CHAT = 4242
FROM = 4242  # a private chat: the sender is the chat
FOREIGN = 999_001
SESSION_SECRET = "breaker-session-secret-Zq81xK"  # a test value, not a real secret
TOKEN = "123456789:AAE-breaker-FAKE-token-value"  # a test value, not a real token
SIGNER = CallbackSigner.derive(SESSION_SECRET)
HMAC_KEY = hmac.new(SESSION_SECRET.encode(), b"trader.telegram.callback.v1", hashlib.sha256).digest()


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
    api: FakeTelegramApi
    render: FakeRenderer
    commands: FakeCommands
    issuer: DbCallbackIssuer
    svc: ProposalService
    ks: KillSwitches
    broker: SimBroker
    clock: FixedClock
    factory: sessionmaker[Session]
    run_id: int
    sym: int
    cfg: int
    signal_id: int
    sleeps: list[float] = field(default_factory=list)


def make_bot(
    env: Env,
    decide: Callable[[int, Decision, Via, str], DecisionResult] | None = None,
    commands: Any = None,
) -> TelegramBot:
    async def fake_sleep(seconds: float) -> None:
        env.sleeps.append(seconds)

    return TelegramBot(
        env.api,
        CHAT,
        env.factory,
        env.clock,
        env.issuer,
        SIGNER,
        decide or env.svc.decide,
        commands or env.commands,
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
            session_date=DAY,
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
    ks = KillSwitches(db_factory, clock)
    svc = ProposalService(db_factory, clock, store, broker, run.id, entry_blocked=ks.entry_guard())
    return Env(
        api=FakeTelegramApi(),
        render=FakeRenderer(),
        commands=FakeCommands(),
        issuer=DbCallbackIssuer(db_factory, clock, SIGNER),
        svc=svc,
        ks=ks,
        broker=broker,
        clock=clock,
        factory=db_factory,
        run_id=run.id,
        sym=sym,
        cfg=cfg,
        signal_id=signal_id,
    )


# --- helpers -------------------------------------------------------------------------------------------
def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


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


def open_position(env: Env) -> int:
    env.broker.submit(
        OrderSpec(env.sym, "buy", "market", 10, stop_loss=Decimal("19.00"), strategy_config_id=env.cfg)
    )
    q = QtQuote(
        env.sym,
        "AAA",
        Decimal("20.00"),
        Decimal("20.01"),
        Decimal("20.00"),
        None,
        1000,
        env.clock.now() - timedelta(seconds=1),
        0,
        False,
        None,
    )
    (ev,) = env.broker.on_quotes([q], env.clock.now())
    return ev.position_id


def new_stop(env: Env, position_id: int) -> int:
    spec = OrderSpec(
        env.sym,
        "sell",
        "stop",
        10,
        stop=Decimal("19.00"),
        purpose="stop",
        position_id=position_id,
        reason="protective_stop",
    )
    sized = SizedOrder(
        Exit(position_id, "stop", Decimal("19.00"), "protective_stop"),
        "stop",
        10,
        spec,
        position_id=position_id,
    )
    return env.svc.create(env.signal_id, sized, "stop").id


async def send(env: Env, bot: TelegramBot, pid: int) -> tuple[int, dict[str, str]]:
    """Send proposal `pid` through the bot: (message id, {action: callback data})."""
    assert await bot.send_proposal(pid)
    (row,) = env.api.last_sent()["buttons"]
    return len(env.api.calls_of("send_message")), {"a": row[0].callback_data, "r": row[1].callback_data}


def tap(env: Env, data: str, message_id: int | None, chat_id: int = CHAT, from_id: int = FROM) -> Update:
    return env.api.callback_update(data, chat_id=chat_id, message_id=message_id, from_id=from_id)


def answers(env: Env) -> list[str | None]:
    return [kw["text"] for kw in env.api.calls_of("answer_callback")]


def answer_by_callback(env: Env) -> dict[str, str | None]:
    return {kw["callback_id"]: kw["text"] for kw in env.api.calls_of("answer_callback")}


def status_of(env: Env, pid: int) -> str:
    with env.factory() as s:
        p = s.get(m.Proposal, pid)
        assert p is not None
        return p.status


def order_count(env: Env) -> int:
    with env.factory() as s:
        return int(s.scalar(select(func.count()).select_from(m.Order)) or 0)


def tg_events(env: Env, level: str | None = None) -> list[m.EventLog]:
    with env.factory() as s:
        q = select(m.EventLog).where(m.EventLog.source == "telegram")
        if level is not None:
            q = q.where(m.EventLog.level == level)
        return list(s.execute(q).scalars())


def gated_claim(env: Env, parties: int) -> None:
    """Make every caller of issuer.claim wait until `parties` callers arrived, so the claims overlap."""
    original = env.issuer.claim
    barrier = threading.Barrier(parties)

    def claim(*args: Any, **kwargs: Any) -> Any:
        barrier.wait(timeout=20)
        return original(*args, **kwargs)

    env.issuer.claim = claim  # type: ignore[method-assign]


# --- 1. Forgery -----------------------------------------------------------------------------------------
async def test_forged_truncated_overlong_and_unicode_callback_data_never_decide(env: Env) -> None:
    bot = make_bot(env)
    pid = new_entry(env)
    mid, data = await send(env, bot, pid)
    good = data["a"]
    body, mac = good.rsplit(":", 1)
    k, ref, action, nonce = body.split(":")
    flipped = mac[:-1] + ("B" if mac[-1] != "B" else "C")
    forged = [
        f"{body}:{flipped}",  # one MAC character changed
        good[:-1],  # truncated MAC
        body,  # no MAC at all
        good[:12],  # cut mid-way
        "",  # empty
        ":::::",  # only separators
        good + "A" * 60,  # overlong (> 64 bytes)
        f"{body}:{mac}:extra",  # a sixth field
        f"{k}:{ref}:r:{nonce}:{mac}",  # approve's MAC on reject
        f"s:{ref}:{action}:{nonce}:{mac}",  # kind swapped
        good.replace(":", "："),  # fullwidth colons
        f"{k}:{ref}​:{action}:{nonce}:{mac}",  # zero-width space in the id
        f"{k}:{''.join(chr(0x0660 + int(c)) for c in ref)}:{action}:{nonce}:{mac}",  # Arabic-Indic digits
        f"{k}:{ref}\n:{action}:{nonce}:{mac}",  # a trailing newline ($ matches before it)
        f"{k}:{ref}:{action}:{nonce}:{mac[:-1]}А",  # Cyrillic A lookalike in the MAC
        f"{k}:{ref}:{action}:{nonce}\x00:{mac}",  # NUL byte
        f"{body}:{mac.upper() if mac.upper() != mac else mac.lower()}",  # case-changed MAC
    ]
    for d in forged:
        await bot.handle_update(tap(env, d, mid))
    assert status_of(env, pid) == "pending" and order_count(env) == 0
    assert answers(env) == ["Invalid button"] * len(forged)
    for ev in tg_events(env):
        assert good not in ev.message and good not in repr(ev.data)
    # The genuine button was never used up by the forgeries.
    await bot.handle_update(tap(env, good, mid))
    assert answers(env)[-1] == "Approved" and status_of(env, pid) == "submitted" and order_count(env) == 1


async def test_a_nonce_or_mac_spliced_from_another_message_is_refused(env: Env) -> None:
    bot = make_bot(env)
    pid_a, pid_b = new_entry(env), new_entry(env)
    mid_a, data_a = await send(env, bot, pid_a)
    mid_b, data_b = await send(env, bot, pid_b)
    _, ref_a, _, nonce_a, mac_a = data_a["a"].split(":")
    _, ref_b, _, nonce_b, mac_b = data_b["a"].split(":")
    pause_nonce, _ = env.issuer.issue("pause", str(env.run_id), ["y", "n"], CHAT, 60)
    spliced = [
        (f"p:{ref_a}:a:{nonce_b}:{mac_b}", mid_a),  # B's nonce and MAC under A's id
        (f"p:{ref_a}:a:{nonce_a}:{mac_b}", mid_a),  # B's MAC on A's body
        (f"p:{ref_b}:a:{nonce_a}:{mac_a}", mid_b),  # A's nonce and MAC under B's id
        # Even correctly signed (a leaked key), a nonce only works for the ref and kind it was issued for:
        (SIGNER.data("p", ref_a, "a", nonce_b), mid_a),
        (SIGNER.data("p", ref_a, "a", pause_nonce), mid_a),
        (SIGNER.data("p", ref_a, "a", "AAAAAAAA"), mid_a),  # a nonce that was never issued
    ]
    for d, mid in spliced:
        await bot.handle_update(tap(env, d, mid))
    await bot.handle_update(tap(env, data_a["a"], mid_b))  # A's genuine button on B's message
    assert status_of(env, pid_a) == "pending" and status_of(env, pid_b) == "pending"
    assert order_count(env) == 0
    assert answers(env) == ["Invalid button"] * len(spliced) + ["Old message"]
    # Nothing was used up: both genuine buttons still work, each deciding its own proposal.
    await bot.handle_update(tap(env, data_a["r"], mid_a))
    await bot.handle_update(tap(env, data_b["a"], mid_b))
    assert answers(env)[-2:] == ["Rejected", "Approved"]
    assert status_of(env, pid_a) == "rejected" and status_of(env, pid_b) == "submitted"
    assert order_count(env) == 1


# --- 2. Replay ------------------------------------------------------------------------------------------
async def test_a_replay_after_success_and_after_a_restart_decides_once(env: Env) -> None:
    bot = make_bot(env)
    pid = new_entry(env)
    mid, data = await send(env, bot, pid)
    first = tap(env, data["a"], mid)
    await bot.handle_update(first)
    await bot.handle_update(first)  # the identical update delivered again
    restarted = make_bot(env)  # a new worker process: only the database remembers the nonce
    await restarted.handle_update(tap(env, data["a"], mid))
    await restarted.handle_update(tap(env, data["r"], mid))
    env.clock.advance(timedelta(days=3))  # much later, after the proposal's own expiry
    await restarted.handle_update(tap(env, data["a"], mid))
    assert answers(env) == ["Approved"] + ["Already answered"] * 4
    assert status_of(env, pid) == "submitted" and order_count(env) == 1
    assert len(env.api.calls_of("edit_message")) == 1
    with env.factory() as s:
        audits = s.scalar(
            select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action.like("proposal.%"))
        )
    assert audits == 1


def test_the_same_tap_delivered_concurrently_decides_exactly_once(env: Env) -> None:
    bot = make_bot(env)
    pid = new_entry(env)
    mid, data = run(send(env, bot, pid))
    n = 6
    gated_claim(env, n)
    updates = [tap(env, data["a" if i % 2 == 0 else "r"], mid) for i in range(n)]
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda u: run(bot.handle_update(u)), updates))
    got = answers(env)
    winners = [a for a in got if a in ("Approved", "Rejected")]
    assert len(winners) == 1, got
    assert sorted(got) == sorted(winners + ["Already answered"] * (n - 1))
    final = status_of(env, pid)
    assert final == ("submitted" if winners == ["Approved"] else "rejected")
    assert order_count(env) == (1 if final == "submitted" else 0)
    assert len(env.api.calls_of("edit_message")) == 1


# --- 3. Foreign sources ---------------------------------------------------------------------------------
async def test_taps_and_texts_from_a_foreign_chat_are_ignored_without_logging_content(env: Env) -> None:
    bot = make_bot(env)
    pid = new_entry(env)
    mid, data = await send(env, bot, pid)
    good = data["a"]
    secret_words = "/pause launch-codes-XYZ"
    sends_before = len(env.api.calls_of("send_message"))

    def cb_update(update_chat: int | None, cb_chat: int, from_id: int) -> Update:
        cb = CallbackQuery(
            id=f"f{update_chat}{cb_chat}", data=good, chat_id=cb_chat, from_id=from_id, message_id=mid
        )
        return Update(
            update_id=90_000 + len(env.api.calls),
            chat_id=update_chat,
            from_id=from_id,
            text=None,
            callback=cb,
        )

    foreign = [
        tap(env, good, mid, chat_id=FOREIGN, from_id=FOREIGN),  # a stranger taps a forwarded button
        cb_update(CHAT, FOREIGN, FOREIGN),  # outer chat right, callback chat foreign
        cb_update(FOREIGN, CHAT, FOREIGN),  # callback chat right, outer chat foreign
        cb_update(None, FOREIGN, FOREIGN),  # no outer chat at all
        env.api.text_update(secret_words, chat_id=FOREIGN, from_id=FOREIGN),
        env.api.text_update("/resume", chat_id=-100_123, from_id=FOREIGN),  # a group chat
    ]
    for u in foreign:
        await bot.handle_update(u)
    assert status_of(env, pid) == "pending" and order_count(env) == 0
    assert env.api.calls_of("answer_callback") == []  # no reply of any kind to a stranger
    assert len(env.api.calls_of("send_message")) == sends_before
    assert env.commands.handled == [] and env.commands.confirmed == []
    evs = tg_events(env, "warning")
    assert len(evs) == len(foreign)
    for ev in evs:
        blob = ev.message + repr(ev.data)
        assert good not in blob and "launch-codes" not in blob and "/resume" not in blob
    # The genuine button still works for Stephen.
    await bot.handle_update(tap(env, good, mid))
    assert answers(env) == ["Approved"] and order_count(env) == 1


# --- 4. Ordering ----------------------------------------------------------------------------------------
def test_approve_racing_the_ttl_expiry(env: Env) -> None:
    bot = make_bot(env)
    # Exactly at expires_at: expired (the boundary belongs to expiry), the message closes as expired.
    pid = new_entry(env)
    mid, data = run(send(env, bot, pid))
    env.clock.set(T + timedelta(minutes=5))
    run(bot.handle_update(tap(env, data["a"], mid)))
    assert answers(env) == ["Already expired"] and status_of(env, pid) == "expired" and order_count(env) == 0
    method, args = env.render.calls[-1]
    assert method == "proposal_closed" and args[1] == "expired"
    assert env.api.calls_of("edit_message")[-1]["buttons"] == ()
    # One microsecond before expiry: approved.
    pid2 = new_entry(env)
    mid2, data2 = run(send(env, bot, pid2))
    env.clock.set(env.clock.now() + timedelta(minutes=5) - timedelta(microseconds=1))
    run(bot.handle_update(tap(env, data2["a"], mid2)))
    assert answers(env)[-1] == "Approved" and status_of(env, pid2) == "submitted"
    submitted_so_far = order_count(env)

    # The tap and the worker's expiry tick at the same moment: one outcome, and the answer matches it.
    outcomes: list[str] = []
    for _ in range(4):
        pid = new_entry(env)
        mid, data = run(send(env, bot, pid))
        with env.factory() as s:
            expires_at = s.get(m.Proposal, pid).expires_at  # type: ignore[union-attr]
        env.clock.set(expires_at - timedelta(milliseconds=1))
        barrier = threading.Barrier(2)
        u = tap(env, data["a"], mid)

        def do_tap(u: Update = u, barrier: threading.Barrier = barrier) -> None:
            barrier.wait(timeout=20)
            run(bot.handle_update(u))

        def do_expire(expires_at: datetime = expires_at, barrier: threading.Barrier = barrier) -> None:
            barrier.wait(timeout=20)
            env.svc.expire_due(expires_at)

        with ThreadPoolExecutor(max_workers=2) as pool:
            for f in [pool.submit(do_tap), pool.submit(do_expire)]:
                f.result()
        final = status_of(env, pid)
        answer = answer_by_callback(env)[u.callback.id]  # type: ignore[union-attr]
        assert final in ("submitted", "expired")
        assert answer == ("Approved" if final == "submitted" else "Already expired"), (final, answer)
        outcomes.append(final)
    assert order_count(env) == submitted_so_far + outcomes.count("submitted")


def test_approve_racing_a_web_decision_gives_one_decision_and_an_honest_answer(env: Env) -> None:
    bot = make_bot(env)
    submitted = 0
    for _ in range(5):
        pid = new_entry(env)
        mid, data = run(send(env, bot, pid))
        barrier = threading.Barrier(2)
        u = tap(env, data["a"], mid)
        web: list[DecisionResult] = []

        def do_tap(u: Update = u, barrier: threading.Barrier = barrier) -> None:
            barrier.wait(timeout=20)
            run(bot.handle_update(u))

        def do_web(
            pid: int = pid, barrier: threading.Barrier = barrier, web: list[DecisionResult] = web
        ) -> None:
            barrier.wait(timeout=20)
            web.append(env.svc.decide(pid, "reject", "web", "web:stephen"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            for f in [pool.submit(do_tap), pool.submit(do_web)]:
                f.result()
        final = status_of(env, pid)
        answer = answer_by_callback(env)[u.callback.id]  # type: ignore[union-attr]
        (w,) = web
        if final == "submitted":
            submitted += 1
            assert answer == "Approved" and w.already_decided and w.status == "submitted"
        else:
            assert final == "rejected" and not w.already_decided
            assert answer == "Already rejected"
        method, args = env.render.calls[-1]
        assert method == "proposal_closed" and args[1] == final
    assert order_count(env) == submitted


async def test_while_paused_an_entry_is_blocked_with_a_reason_and_a_stop_still_goes_through(env: Env) -> None:
    bot = make_bot(env)
    position_id = open_position(env)
    orders_before = order_count(env)
    entry = new_entry(env)
    stop = new_stop(env, position_id)
    mid_e, data_e = await send(env, bot, entry)
    mid_s, data_s = await send(env, bot, stop)
    assert env.ks.pause(env.run_id, DAY, actor="telegram")

    await bot.handle_update(tap(env, data_e["a"], mid_e))
    blocked = answers(env)[-1]
    assert blocked is not None and "blocked" in blocked.lower() and "manual_pause" in blocked
    assert blocked != "Approved" and len(blocked) <= 200 and blocked.isascii()
    assert status_of(env, entry) == "rejected" and order_count(env) == orders_before
    method, args = env.render.calls[-1]
    assert method == "proposal_closed" and args[1] == "rejected"
    assert env.api.calls_of("edit_message")[-1]["message_id"] == mid_e

    await bot.handle_update(tap(env, data_s["a"], mid_s))  # the protective stop must not be trapped
    assert answers(env)[-1] == "Approved" and status_of(env, stop) == "submitted"
    with env.factory() as s:
        stops = s.execute(select(m.Order).where(m.Order.purpose == "stop")).scalars().all()
    assert len(stops) == 1 and stops[0].position_id == position_id


# --- 5. Buttons -----------------------------------------------------------------------------------------
def test_journal_answers_tapped_twice_keep_the_first(env: Env) -> None:
    bot = make_bot(env)
    _, data = env.issuer.issue("journal", "20261006", ["y", "n"], CHAT, None)
    run(bot.handle_update(tap(env, data["y"], 9)))
    run(bot.handle_update(tap(env, data["n"], 9)))
    run(bot.handle_update(tap(env, data["y"], 9)))
    assert answers(env) == ["Saved", "Already answered", "Already answered"]
    with env.factory() as s:
        row = s.get(m.Journal, (env.run_id, DAY))
    assert row is not None and row.rules_followed is True
    assert len(env.api.calls_of("send_message")) == 1  # one journal_answered reply
    assert len(env.api.calls_of("edit_buttons")) == 1

    # Yes and No tapped at the same moment on the next day's summary: exactly one answer is saved.
    _, data2 = env.issuer.issue("journal", "20261007", ["y", "n"], CHAT, None)
    gated_claim(env, 2)
    both = [tap(env, data2["y"], 10), tap(env, data2["n"], 10)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda u: run(bot.handle_update(u)), both))
    got = answer_by_callback(env)
    results = [got[u.callback.id] for u in both]  # type: ignore[union-attr]
    assert sorted(results) == ["Already answered", "Saved"]
    winner = "y" if results[0] == "Saved" else "n"
    with env.factory() as s:
        row2 = s.get(m.Journal, (env.run_id, date(2026, 10, 7)))
    assert row2 is not None and row2.rules_followed is (winner == "y")
    assert len(env.api.calls_of("send_message")) == 2


async def test_a_pause_confirmation_tapped_after_resume_does_not_pause_again(env: Env) -> None:
    def unused(*_: Any) -> Any:
        raise AssertionError("not used by /pause or /resume")

    deps = CommandDeps(
        factory=env.factory,
        clock=env.clock,
        calendar=CAL,
        settings=RuntimeSettings,
        killswitches=env.ks,
        run_id=env.run_id,
        chat_id=CHAT,
        plan=unused,
        fired=unused,
        token_health=unused,
        quotes=None,
        messenger=FakeMessenger(),
        issuer=env.issuer,
        render=env.render,
    )
    bot = make_bot(env, commands=Commands(deps))

    def paused() -> bool:
        return any(a.switch == "manual_pause" for a in env.ks.active(env.run_id, DAY))

    async def pause_buttons() -> dict[str, str]:
        await bot.handle_update(env.api.text_update("/pause", chat_id=CHAT))
        (row,) = env.api.last_sent()["buttons"]
        return {"y": row[0].callback_data, "n": row[1].callback_data}

    first = await pause_buttons()
    await bot.handle_update(tap(env, first["y"], 1))
    assert paused()
    await bot.handle_update(env.api.text_update("/resume", chat_id=CHAT))
    assert not paused()
    second = await pause_buttons()  # a new confirmation, left unanswered
    await bot.handle_update(tap(env, first["y"], 1))  # the old Yes, tapped again after /resume
    await bot.handle_update(tap(env, first["n"], 1))
    assert not paused()
    env.clock.advance(timedelta(seconds=61))
    await bot.handle_update(tap(env, second["y"], 3))  # past the 60 s confirmation TTL
    assert not paused()
    assert answers(env) == [
        "Paused: new entries are blocked.",
        "Already answered",
        "Already answered",
        "Button expired",
    ]


# --- 6. Telegram failures -------------------------------------------------------------------------------
async def test_409_conflicts_and_message_not_modified_never_break_the_bot(env: Env) -> None:
    bot = make_bot(env)
    # (a) The polling loop: a 409 storm mixed with a timeout logs one critical event, backs off 30 s per
    # 409 and 1 s for the timeout, and then handles the waiting update.
    stop = asyncio.Event()
    env.commands.on_handle = stop.set
    conflict = TelegramApiError(409, "Conflict: terminated by other getUpdates request")
    env.api.fail("get_updates", conflict)
    env.api.fail("get_updates", TelegramApiError(None, "timed out"))
    env.api.fail("get_updates", conflict, times=3)
    env.api.queue_updates(env.api.text_update("/status", chat_id=CHAT))
    await asyncio.wait_for(bot.run(stop), timeout=10)
    assert env.sleeps == [30, 1, 30, 30, 30]
    assert len(tg_events(env, "critical")) == 1
    assert env.commands.handled == ["/status"]

    # (b) A 400 "message is not modified" on the edit after a tap: the decision stands, no retry storm.
    pid = new_entry(env)
    mid, data = await send(env, bot, pid)
    not_modified = TelegramApiError(400, "Bad Request: message is not modified")
    env.api.fail("edit_message", not_modified)
    await bot.handle_update(tap(env, data["a"], mid))
    assert answers(env)[-1] == "Approved" and status_of(env, pid) == "submitted" and order_count(env) == 1
    await bot.handle_update(tap(env, data["a"], mid))
    assert answers(env)[-1] == "Already answered" and len(env.api.calls_of("edit_message")) == 1

    # (c) The same 400 from sync_closed after a web decision: closed once, never retried on every step.
    pid2 = new_entry(env)
    mid2, data2 = await send(env, bot, pid2)
    env.svc.decide(pid2, "reject", "web", "web:stephen")
    env.api.fail("edit_message", not_modified)
    await bot.sync_closed()
    await bot.sync_closed()
    await bot.sync_closed()
    assert len(env.api.calls_of("edit_message")) == 2
    await bot.handle_update(tap(env, data2["a"], mid2))
    assert answers(env)[-1] == "Already answered" and status_of(env, pid2) == "rejected"


# --- 7. Secrets -----------------------------------------------------------------------------------------
async def test_no_secret_or_live_callback_data_in_any_log_event_or_reply(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    def flaky_decide(pid: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/answerCallbackQuery failed")
        return env.svc.decide(pid, decision, via, actor)

    bot = make_bot(env, decide=flaky_decide)
    live: list[str] = []
    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        pid = new_entry(env)
        mid, data = await send(env, bot, pid)
        pid2 = new_entry(env)
        mid2, data2 = await send(env, bot, pid2)
        live += [data2["a"], data2["r"]]  # never tapped: a working bearer capability until used
        body, mac = data["a"].rsplit(":", 1)
        await bot.handle_update(tap(env, f"{body}:{mac[::-1]}", mid))  # forged
        await bot.handle_update(tap(env, data2["a"], mid2, chat_id=FOREIGN, from_id=FOREIGN))  # foreign
        await bot.handle_update(tap(env, data2["a"], mid2 + 50))  # old message
        await bot.handle_update(tap(env, data["a"], mid))  # decide raises with the token in its text
        env.api.fail("answer_callback", RuntimeError(f"https://api.telegram.org/bot{TOKEN}/x"))
        env.api.fail("edit_message", TelegramApiError(400, "Bad Request: message is not modified"))
        await bot.handle_update(tap(env, data["a"], mid))  # decides; answer and edit both fail
        _, jdata = env.issuer.issue("journal", "20261006", ["y", "n"], CHAT, None)
        live.append(jdata["n"])
        await bot.handle_update(tap(env, jdata["y"], 7))
        stop = asyncio.Event()
        env.commands.on_handle = stop.set
        env.api.fail(
            "get_updates", RuntimeError(f"ConnectError https://api.telegram.org/bot{TOKEN}/getUpdates")
        )
        env.api.fail("get_updates", TelegramApiError(409, "Conflict: terminated by other getUpdates request"))
        env.api.queue_updates(env.api.text_update("/status", chat_id=CHAT))
        await asyncio.wait_for(bot.run(stop), timeout=10)
    assert status_of(env, pid) == "submitted"
    assert any(e.get("event") == "telegram.decide_failed" for e in logs)  # the capture saw the bot's logs

    with env.factory() as s:
        evs = [
            f"{e.source} {e.level} {e.message} {e.data!r}" for e in s.execute(select(m.EventLog)).scalars()
        ]
        audits = [
            f"{a.actor} {a.action} {a.before!r} {a.after!r}" for a in s.execute(select(m.AuditLog)).scalars()
        ]
    log_blob = "\n".join([repr(logs), caplog.text, *evs, *audits])
    replies = [kw.get("text") for name, kw in env.api.calls if name in ("answer_callback", "edit_message")]
    replies += [kw["text"] for kw in env.api.calls_of("send_message")]
    reply_blob = "\n".join(str(r) for r in replies)
    secrets = [
        SESSION_SECRET,
        TOKEN,
        TOKEN.split(":", 1)[1],
        HMAC_KEY.hex(),
        base64.b64encode(HMAC_KEY).decode().rstrip("="),
        base64.urlsafe_b64encode(HMAC_KEY).decode().rstrip("="),
        repr(HMAC_KEY)[2:-1],
    ]
    for secret in secrets:
        assert secret not in log_blob, f"a secret reached the logs/events: {secret[:6]}..."
        assert secret not in reply_blob, f"a secret reached a Telegram reply: {secret[:6]}..."
    for d in live:
        assert d not in log_blob, "live signed callback data was logged"
        mac = d.rsplit(":", 1)[1]
        assert mac not in log_blob, "a live MAC was logged"
