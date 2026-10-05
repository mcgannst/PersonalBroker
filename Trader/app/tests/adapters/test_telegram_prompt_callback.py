"""OPTSIM-T10: the Telegram callback kind `prompt`: signed data, and the bot's branch that answers an owner
prompt. The prompt goes out through the real sender, notifier and issuer over a FakeTelegramApi."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_telegram import FakeRenderer, FakeTelegramApi
from tests.options.factories import T0, add_options_run
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import (
    ACTIONS,
    MAX_DATA_BYTES,
    CallbackSigner,
    DbCallbackIssuer,
    ParsedCallback,
)
from trader.engine.proposals import Decision, DecisionResult, Via
from trader.market.clock import FixedClock
from trader.notify.notifier import TelegramNotifier
from trader.notify.types import OutboundMessage
from trader.options.messages import OptionMessages
from trader.options.prompts import DbPromptStore, PromptSender
from trader.options.settings import OptionSettings
from trader.options.types import OwnerPromptRequest, PromptChoice, PromptView
from trader.settings_store import RuntimeSettings

CHAT = 4242
SIGNER = CallbackSigner.derive("test-session-secret")
NONCE = "AbCd_-12"


def test_signer_accepts_prompt_data_and_rejects_bad_actions() -> None:
    data = SIGNER.data("o", "12", "h", NONCE)
    assert SIGNER.parse(data) == ParsedCallback("prompt", "12", "h", NONCE)
    assert ACTIONS["prompt"] == frozenset("aryncohbsk")
    for action in ("w", "z", "A"):  # `w` (write on the web) is never a button
        assert SIGNER.parse(SIGNER.data("o", "12", action, NONCE)) is None
    assert SIGNER.parse(SIGNER.data("o", "x12", "a", NONCE)) is None  # the ref is the prompt id
    assert SIGNER.parse(data.replace("o:12:", "o:13:", 1)) is None  # altered: the MAC no longer fits
    assert SIGNER.parse(SIGNER.data("p", "12", "h", NONCE)) is None  # the other kinds keep their actions


def test_prompt_data_fits_64_bytes() -> None:
    biggest_id = str(2**63 - 1)  # owner_prompts.id is a bigint
    for action in sorted(ACTIONS["prompt"]):
        data = SIGNER.data("o", biggest_id, action, NONCE)
        assert len(data.encode()) <= MAX_DATA_BYTES
        assert SIGNER.parse(data) == ParsedCallback("prompt", biggest_id, action, NONCE)


# --- the bot ------------------------------------------------------------------------------------------------


class NoCommands:
    async def handle(self, text: str) -> list[OutboundMessage]:
        return []

    async def confirm_pause(self, action: str) -> str:
        return ""


def no_decide(proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
    raise AssertionError("a prompt tap must not decide a proposal")


@dataclass
class Env:
    bot: TelegramBot
    api: FakeTelegramApi
    store: DbPromptStore
    sender: PromptSender
    clock: FixedClock
    prompt: PromptView

    async def send(self) -> tuple[int, dict[str, str]]:
        """Send the prompt when due: (message id, {button text: callback data})."""
        assert await self.sender.send_due(self.clock.now()) == 1
        sent = self.api.last_sent()
        return len(self.api.calls_of("send_message")), {b.text: b.callback_data for b in sent["buttons"][0]}

    async def tap(self, data: str, message_id: int) -> None:
        await self.bot.handle_update(self.api.callback_update(data, chat_id=CHAT, message_id=message_id))

    def answers(self) -> list[str | None]:
        return [kw["text"] for kw in self.api.calls_of("answer_callback")]


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T0)
    with db_factory() as s:
        run_id = add_options_run(s)
        s.commit()

    async def no_sleep(seconds: float) -> None:
        return None

    api = FakeTelegramApi()
    issuer = DbCallbackIssuer(db_factory, clock, SIGNER)
    store = DbPromptStore(db_factory, clock)
    notifier = TelegramNotifier(api, CHAT, db_factory, clock, sleep=no_sleep)
    sender = PromptSender(
        store, notifier, issuer, OptionMessages("https://t.example"), CHAT, OptionSettings, clock
    )
    bot = TelegramBot(
        api, CHAT, db_factory, clock, issuer, SIGNER, no_decide, NoCommands(), FakeRenderer(), run_id,
        settings=RuntimeSettings, sleep=no_sleep,
    )  # fmt: skip
    request = OwnerPromptRequest(
        kind="approve_ticker",
        scope_key="F",
        dedupe_key="wheel:approve:F",
        title="Approve F for the wheel?",
        body="F passed the screen.",
        choices=(PromptChoice("y", "Yes, approve"), PromptChoice("n", "No")),
    )
    return Env(bot, api, store, sender, clock, store.ensure(run_id, "wheel", request))


@pytest.mark.db
async def test_bot_prompt_tap_answers_and_removes_buttons(env: Env) -> None:
    message_id, data = await env.send()
    env.clock.advance(timedelta(minutes=3))
    await env.tap(data["Yes, approve"], message_id)
    assert env.answers() == ["Yes, approve"]  # the chosen label
    assert env.api.calls_of("edit_buttons") == [{"chat_id": CHAT, "message_id": message_id, "buttons": ()}]
    stored = env.store.get(env.prompt.id)
    assert stored is not None
    assert (stored.status, stored.answer, stored.answered_via) == ("answered", "y", "telegram")
    assert stored.answered_at == T0 + timedelta(minutes=3)
    assert env.store.undelivered("wheel") == [stored]  # waiting for the options worker to deliver it


@pytest.mark.db
async def test_bot_second_tap_says_already_answered(env: Env) -> None:
    first_id, first = await env.send()
    env.clock.advance(timedelta(hours=24))
    second_id, second = await env.send()  # the daily reminder: a new message with its own nonce
    await env.tap(first["No"], first_id)
    await env.tap(first["Yes, approve"], first_id)  # the same message again: its nonce is used up
    await env.tap(second["Yes, approve"], second_id)  # the reminder: the store refuses it
    assert env.answers() == ["No", "Already answered", "Already answered"]
    assert [kw["message_id"] for kw in env.api.calls_of("edit_buttons")] == [first_id, second_id]
    stored = env.store.get(env.prompt.id)
    assert stored is not None and stored.answer == "n"  # the first answer stands


@pytest.mark.db
async def test_bot_failure_releases_the_nonce(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    message_id, data = await env.send()
    real_answer = DbPromptStore.answer

    def broken(self: DbPromptStore, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database is away")

    monkeypatch.setattr(DbPromptStore, "answer", broken)
    await env.tap(data["Yes, approve"], message_id)
    assert env.answers() == ["Error, try again"]
    assert env.api.calls_of("edit_buttons") == []  # the buttons stay
    stored = env.store.get(env.prompt.id)
    assert stored is not None and stored.status == "pending"

    monkeypatch.setattr(DbPromptStore, "answer", real_answer)
    await env.tap(data["Yes, approve"], message_id)  # the nonce was released: the same button works
    assert env.answers() == ["Error, try again", "Yes, approve"]
    stored = env.store.get(env.prompt.id)
    assert stored is not None and stored.answer == "y"
