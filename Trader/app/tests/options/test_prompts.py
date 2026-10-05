"""OPTSIM-T10: the owner-prompt store on a real test database, and the sender with a faked Telegram."""

import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_telegram import FakeIssuer, RecordingNotifier
from tests.options.factories import T0, add_options_run
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.options.messages import OptionMessages
from trader.options.prompts import DbPromptStore, PromptSender
from trader.options.protocols import PromptStore
from trader.options.settings import OptionSettings
from trader.options.types import OwnerPromptRequest, PromptChoice

pytestmark = pytest.mark.db
CHAT = 4242
YES_NO = (PromptChoice("y", "Yes"), PromptChoice("n", "No"))


def request(key: str = "wheel:approve:F", **changes: Any) -> OwnerPromptRequest:
    values: dict[str, Any] = {
        "kind": "approve_ticker",
        "scope_key": "F",
        "dedupe_key": key,
        "title": "Approve F for the wheel?",
        "body": "F passed the screen.",
        "choices": YES_NO,
    }
    return OwnerPromptRequest(**{**values, **changes})


@dataclass
class Env:
    store: DbPromptStore
    sender: PromptSender
    notifier: RecordingNotifier
    issuer: FakeIssuer
    clock: FixedClock
    factory: sessionmaker[Session]
    run_id: int

    def sender_for(self, chat_id: int | None) -> PromptSender:
        return PromptSender(
            self.store, self.notifier, self.issuer, OptionMessages("https://t.example"), chat_id,
            OptionSettings, self.clock,
        )  # fmt: skip


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T0)
    with db_factory() as s:
        run_id = add_options_run(s)
        s.commit()
    store: PromptStore = DbPromptStore(db_factory, clock)  # the class satisfies the protocol
    assert isinstance(store, DbPromptStore)
    e = Env(store, None, RecordingNotifier(), FakeIssuer(), clock, db_factory, run_id)  # type: ignore[arg-type]
    e.sender = e.sender_for(CHAT)
    return e


def test_ensure_is_idempotent_by_dedupe_key(env: Env) -> None:
    first = env.store.ensure(env.run_id, "wheel", request())
    env.clock.advance(timedelta(hours=1))
    again = env.store.ensure(
        env.run_id, "other", request(title="Changed", choices=(PromptChoice("a", "OK"),))
    )
    assert again == first  # the existing row, unchanged
    assert (first.status, first.source, first.asked_at, first.send_count) == ("pending", "wheel", T0, 0)
    assert first.choices == YES_NO
    assert env.store.by_key("wheel:approve:F") == first and env.store.get(first.id) == first
    assert env.store.pending() == [first] and env.store.pending("other") == []
    with pytest.raises(ValueError):
        env.store.ensure(env.run_id, "wheel", request("k2", choices=(PromptChoice("z", "Bad"),)))


def test_answer_outcomes(env: Env) -> None:
    plain = env.store.ensure(env.run_id, "wheel", request())
    texty = env.store.ensure(
        env.run_id, "wheel", request("k2", needs_text=True, choices=(PromptChoice("a", "Approve"), *YES_NO))
    )

    def status(prompt_id: int, choice: str, text: str | None = None) -> str:
        return env.store.answer(prompt_id, choice, text=text, via="web", actor="web:stephen").status

    assert status(999, "y") == "unknown"
    assert status(plain.id, "a") == "invalid_choice"
    assert status(texty.id, "a", "  ") == "text_required"
    assert env.store.pending() == [plain, texty]  # nothing was stored by the three refusals
    env.clock.advance(timedelta(minutes=5))
    result = env.store.answer(plain.id, "y", via="telegram", actor="telegram:4242")
    assert result.status == "ok" and result.prompt is not None
    answered = result.prompt
    assert (answered.status, answered.answer, answered.answered_via) == ("answered", "y", "telegram")
    assert answered.answered_at == T0 + timedelta(minutes=5) and answered.answer_text is None
    assert status(plain.id, "n") == "already"
    assert env.store.get(plain.id) == answered  # the first answer stands
    with env.factory() as s:
        assert s.get(m.OwnerPrompt, plain.id).answered_by == "telegram:4242"  # type: ignore[union-attr]


def test_two_answers_one_wins(env: Env) -> None:
    prompt = env.store.ensure(env.run_id, "wheel", request())
    start = threading.Barrier(2)
    statuses: dict[str, str] = {}

    def answer(choice: str) -> None:
        start.wait(timeout=10)
        result = DbPromptStore(env.factory, env.clock).answer(prompt.id, choice, via="web", actor=choice)
        statuses[choice] = result.status

    threads = [threading.Thread(target=answer, args=(choice,)) for choice in ("y", "n")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(statuses.values()) == ["already", "ok"]
    winner = next(choice for choice, status in statuses.items() if status == "ok")
    stored = env.store.get(prompt.id)
    assert stored is not None and stored.answer == winner


async def test_text_choices_need_text_and_are_links_in_telegram(env: Env) -> None:
    choices = (PromptChoice("a", "Approve"), PromptChoice("r", "Reject"), PromptChoice("w", "Write it"))
    prompt = env.store.ensure(env.run_id, "wheel", request(needs_text=True, choices=choices))
    assert await env.sender.send_due(T0) == 1
    (msg,) = env.notifier.sent
    assert env.issuer.issued[0]["actions"] == ["r"]  # only the choice without text is a button
    assert [[b.text for b in row] for row in msg.buttons] == [["Reject"]]
    href = f'<a href="https://t.example/options?prompt={prompt.id}">'
    assert f"{href}Approve: answer on the Options page</a>" in msg.text
    assert f"{href}Write it: answer on the Options page</a>" in msg.text

    for choice in ("a", "w"):
        assert env.store.answer(prompt.id, choice, via="telegram", actor="t").status == "text_required"
    result = env.store.answer(prompt.id, "a", text=" I would own it ", via="web", actor="web:stephen")
    assert result.status == "ok" and result.prompt is not None
    assert result.prompt.answer_text == "I would own it"


async def test_send_due_first_time_then_after_the_repeat_interval(env: Env) -> None:
    prompt = env.store.ensure(env.run_id, "wheel", request())
    assert await env.sender_for(None).send_due(T0) == 0  # Telegram is not configured: nothing is sent
    assert env.notifier.sent == [] and env.store.due_for_send(T0) == [prompt]

    assert await env.sender.send_due(T0) == 1
    assert await env.sender.send_due(T0 + timedelta(hours=23, minutes=59)) == 0
    assert await env.sender.send_due(T0 + timedelta(hours=24)) == 1
    assert [(msg.kind, msg.dedupe_key) for msg in env.notifier.sent] == [
        ("alert", f"opt:prompt:{prompt.id}:1"),
        ("alert", f"opt:prompt:{prompt.id}:2"),
    ]
    issued = env.issuer.issued
    assert [(i["kind"], i["ref"], i["actions"], i["chat_id"], i["ttl_seconds"]) for i in issued] == [
        ("prompt", str(prompt.id), ["y", "n"], CHAT, None)
    ] * 2
    first, second = env.notifier.sent
    assert [b.callback_data for b in first.buttons[0]] == list(issued[0]["data"].values())
    assert first.buttons != second.buttons  # each send has its own nonce
    stored = env.store.get(prompt.id)
    assert stored is not None
    assert (stored.send_count, stored.last_sent_at) == (2, T0 + timedelta(hours=24))


async def test_answered_or_cancelled_prompts_are_not_sent(env: Env) -> None:
    answered = env.store.ensure(env.run_id, "wheel", request("k1"))
    env.store.ensure(env.run_id, "wheel", request("k2"))
    env.store.answer(answered.id, "y", via="web", actor="web:stephen")
    env.store.cancel("k2")
    with env.factory() as s:  # a prompt of a retired run is not repeated either
        old_run = add_options_run(s, status="completed")
        s.commit()
    env.store.ensure(old_run, "wheel", request("k3"))
    assert env.store.due_for_send(T0) == []
    assert await env.sender.send_due(T0 + timedelta(days=3)) == 0
    assert env.notifier.sent == [] and env.issuer.issued == []


def test_undelivered_and_mark_delivered(env: Env) -> None:
    first = env.store.ensure(env.run_id, "wheel", request("k1"))
    second = env.store.ensure(env.run_id, "wheel", request("k2"))
    other = env.store.ensure(env.run_id, "toy_call", request("k3"))
    assert env.store.undelivered("wheel") == []  # a pending prompt has no answer to deliver
    env.store.answer(second.id, "n", via="web", actor="web:stephen")
    env.clock.advance(timedelta(minutes=1))
    env.store.answer(first.id, "y", via="telegram", actor="telegram:1")
    env.store.answer(other.id, "y", via="web", actor="web:stephen")
    assert [p.id for p in env.store.undelivered("wheel")] == [second.id, first.id]  # oldest answer first
    env.clock.advance(timedelta(minutes=1))
    env.store.mark_delivered(second.id)
    assert [p.id for p in env.store.undelivered("wheel")] == [first.id]
    delivered = env.store.get(second.id)
    assert delivered is not None and delivered.delivered_at == T0 + timedelta(minutes=2)
    assert [p.id for p in env.store.undelivered("toy_call")] == [other.id]


def test_cancel_by_key(env: Env) -> None:
    pending = env.store.ensure(env.run_id, "wheel", request("k1"))
    answered = env.store.ensure(env.run_id, "wheel", request("k2"))
    env.store.answer(answered.id, "y", via="web", actor="web:stephen")
    env.store.cancel("k1")
    env.store.cancel("k2")  # an answered prompt keeps its answer
    env.store.cancel("no-such-key")
    a, b = env.store.get(pending.id), env.store.get(answered.id)
    assert a is not None and b is not None
    assert (a.status, b.status, b.answer) == ("cancelled", "answered", "y")
    assert env.store.pending() == []
    assert env.store.answer(pending.id, "y", via="web", actor="web:stephen").status == "already"
    assert env.store.ensure(env.run_id, "wheel", request("k1")).status == "cancelled"  # not asked again
