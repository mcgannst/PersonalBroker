"""Owner prompts: the questions a strategy asks Stephen (OPTSIM task plan T10; table `owner_prompts`).

`DbPromptStore` is the `PromptStore`: asking is idempotent by `dedupe_key`, an answer is taken once (the
row lock decides between two answers), and an answered prompt waits as `undelivered` until the host hands
it to the plug-in that asked. `PromptSender` sends every pending prompt to Telegram with one signed button
per choice, and again every `options.prompt_repeat_hours` until it is answered.

Choices that need free text can't be a Telegram button (risk R12): `w` always, and `a` when the prompt
has `needs_text`. The message shows those as a link to the Options page instead.

A prompt changes no option cash, order, structure or position, so no method takes the book lock.
"""

from collections.abc import Callable
from datetime import datetime, timedelta

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import CallbackIssuer, CallbackKind
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock
from trader.notify.types import Button, Buttons, Notifier
from trader.options.protocols import AnswerResult, OptionRenderer, PromptStore
from trader.options.settings import OptionSettings, OptionSettingsStore
from trader.options.types import (
    MAX_DEDUPE_KEY,
    PROMPT_CHOICE_CODES,
    AnsweredVia,
    OwnerPromptRequest,
    PromptChoice,
    PromptView,
)

log = structlog.get_logger("options.prompts")

CALLBACK_KIND: CallbackKind = "prompt"
TEXT_CHOICES = ("a", "w")  # with `needs_text`, these choices need a non-empty text
WEB_ONLY_CHOICE = "w"  # "write on the web": never a Telegram button
BUTTONS_PER_ROW = 2
MAX_ACTOR = 50  # owner_prompts.answered_by is varchar(50)


def needs_web(prompt: PromptView, code: str) -> bool:
    """True when the choice can only be answered on the Options page (a link in Telegram, not a button)."""
    return code == WEB_ONLY_CHOICE or (prompt.needs_text and code in TEXT_CHOICES)


def button_choices(prompt: PromptView) -> tuple[PromptChoice, ...]:
    return tuple(c for c in prompt.choices if not needs_web(prompt, c.code))


def link_choices(prompt: PromptView) -> tuple[PromptChoice, ...]:
    return tuple(c for c in prompt.choices if needs_web(prompt, c.code))


def prompt_path(prompt_id: int) -> str:
    """The web path that opens the prompt on the Options page."""
    return f"/options?prompt={prompt_id}"


def _view(row: m.OwnerPrompt) -> PromptView:
    return PromptView(
        id=row.id,
        source=row.source,
        kind=row.kind,
        scope_key=row.scope_key,
        dedupe_key=row.dedupe_key,
        title=row.title,
        body=row.body,
        choices=tuple(PromptChoice(str(c["code"]), str(c["label"])) for c in row.choices),
        needs_text=row.needs_text,
        default_choice=row.default_choice,
        data=dict(row.data or {}),
        status=row.status,  # type: ignore[arg-type]
        asked_at=row.asked_at,
        last_sent_at=row.last_sent_at,
        send_count=row.send_count,
        answered_at=row.answered_at,
        answer=row.answer,
        answer_text=row.answer_text,
        answered_via=row.answered_via,  # type: ignore[arg-type]
        delivered_at=row.delivered_at,
    )


def _check(req: OwnerPromptRequest) -> None:
    codes = [c.code for c in req.choices]
    if not codes or len(set(codes)) != len(codes):
        raise ValueError("a prompt needs at least one choice and no code twice")
    bad = [code for code in codes if len(code) != 1 or code not in PROMPT_CHOICE_CODES]
    if bad:
        raise ValueError(f"unknown prompt choice code: {bad[0]!r}")
    if not req.dedupe_key or len(req.dedupe_key) > MAX_DEDUPE_KEY:
        raise ValueError(f"a prompt's dedupe_key must be 1 to {MAX_DEDUPE_KEY} characters")
    if req.default_choice is not None and req.default_choice not in codes:
        raise ValueError("default_choice is not one of the choices")


class DbPromptStore:
    """`PromptStore` on `owner_prompts`. Every method is its own short transaction."""

    def __init__(self, factory: sessionmaker[Session], clock: Clock) -> None:
        self.factory = factory
        self.clock = clock

    def ensure(self, run_id: int, source: str, req: OwnerPromptRequest) -> PromptView:
        """Insert by `dedupe_key`, or return the existing row unchanged (whatever its status)."""
        _check(req)
        stmt = (
            insert(m.OwnerPrompt)
            .values(
                run_id=run_id,
                source=source,
                kind=req.kind,
                scope_key=req.scope_key,
                dedupe_key=req.dedupe_key,
                title=req.title,
                body=req.body,
                choices=[{"code": c.code, "label": c.label} for c in req.choices],
                needs_text=req.needs_text,
                default_choice=req.default_choice,
                data=dict(req.data),
                status="pending",
                asked_at=self.clock.now(),
                send_count=0,
            )
            .on_conflict_do_nothing(index_elements=[m.OwnerPrompt.dedupe_key])
        )
        with session_scope(self.factory) as s:
            s.execute(stmt)
            row = s.execute(
                select(m.OwnerPrompt).where(m.OwnerPrompt.dedupe_key == req.dedupe_key)
            ).scalar_one()
            return _view(row)

    def get(self, prompt_id: int) -> PromptView | None:
        with self.factory() as s:
            row = s.get(m.OwnerPrompt, prompt_id)
            return None if row is None else _view(row)

    def by_key(self, dedupe_key: str) -> PromptView | None:
        with self.factory() as s:
            row = s.execute(
                select(m.OwnerPrompt).where(m.OwnerPrompt.dedupe_key == dedupe_key)
            ).scalar_one_or_none()
            return None if row is None else _view(row)

    def pending(self, source: str | None = None) -> list[PromptView]:
        q = select(m.OwnerPrompt).where(m.OwnerPrompt.status == "pending")
        if source is not None:
            q = q.where(m.OwnerPrompt.source == source)
        with self.factory() as s:
            return [_view(row) for row in s.execute(q.order_by(m.OwnerPrompt.id)).scalars()]

    def answer(
        self, prompt_id: int, choice: str, *, text: str | None = None, via: AnsweredVia, actor: str
    ) -> AnswerResult:
        """Store the owner's answer. The row is locked first, so of two answers at once the second one
        sees the first and gets `already`."""
        with session_scope(self.factory) as s:
            row = s.execute(
                select(m.OwnerPrompt).where(m.OwnerPrompt.id == prompt_id).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return AnswerResult("unknown", None)
            prompt = _view(row)
            if row.status != "pending":
                return AnswerResult("already", prompt)
            if choice not in {c.code for c in prompt.choices}:
                return AnswerResult("invalid_choice", prompt)
            cleaned = (text or "").strip()
            if prompt.needs_text and choice in TEXT_CHOICES and not cleaned:
                return AnswerResult("text_required", prompt)
            row.status = "answered"
            row.answered_at = self.clock.now()
            row.answer = choice
            row.answer_text = cleaned or None
            row.answered_via = via
            row.answered_by = actor[:MAX_ACTOR]
            s.flush()
            return AnswerResult("ok", _view(row))

    def undelivered(self, source: str) -> list[PromptView]:
        """Answered prompts of `source` whose answer has not reached the plug-in yet, oldest answer first."""
        q = (
            select(m.OwnerPrompt)
            .where(
                m.OwnerPrompt.source == source,
                m.OwnerPrompt.status == "answered",
                m.OwnerPrompt.delivered_at.is_(None),
            )
            .order_by(m.OwnerPrompt.answered_at, m.OwnerPrompt.id)
        )
        with self.factory() as s:
            return [_view(row) for row in s.execute(q).scalars()]

    def mark_delivered(self, prompt_id: int) -> None:
        with session_scope(self.factory) as s:
            s.execute(
                update(m.OwnerPrompt)
                .where(m.OwnerPrompt.id == prompt_id, m.OwnerPrompt.delivered_at.is_(None))
                .values(delivered_at=self.clock.now())
            )

    def due_for_send(self, now: datetime) -> list[PromptView]:
        """Pending prompts of an active run that were never sent, or last sent at least
        `options.prompt_repeat_hours` ago (the stored setting, read now)."""
        hours = OptionSettingsStore(self.factory, self.clock.now).load().prompt_repeat_hours
        q = (
            select(m.OwnerPrompt)
            .join(m.Run, m.Run.id == m.OwnerPrompt.run_id)
            .where(
                m.OwnerPrompt.status == "pending",
                m.Run.status == "active",
                or_(
                    m.OwnerPrompt.last_sent_at.is_(None),
                    m.OwnerPrompt.last_sent_at <= now - timedelta(hours=hours),
                ),
            )
            .order_by(m.OwnerPrompt.id)
        )
        with self.factory() as s:
            return [_view(row) for row in s.execute(q).scalars()]

    def mark_sent(self, prompt_id: int, now: datetime) -> None:
        with session_scope(self.factory) as s:
            s.execute(
                update(m.OwnerPrompt)
                .where(m.OwnerPrompt.id == prompt_id)
                .values(last_sent_at=now, send_count=m.OwnerPrompt.send_count + 1)
            )

    def cancel(self, dedupe_key: str) -> None:
        """A pending prompt that is no longer wanted; an answered one is left as it is."""
        with session_scope(self.factory) as s:
            s.execute(
                update(m.OwnerPrompt)
                .where(m.OwnerPrompt.dedupe_key == dedupe_key, m.OwnerPrompt.status == "pending")
                .values(status="cancelled")
            )


class PromptSender:
    """Sends the prompts that are due to Telegram (`send_due`, called by the options worker's loop).

    Each send issues a fresh nonce without expiry for the message's buttons, so the buttons of an earlier
    reminder keep working too; whichever is tapped first answers the prompt and the others then say
    "Already answered". The message goes through the Notifier with dedupe key `opt:prompt:<id>:<n>` (n is
    the number of this send), so a crash between the send and `mark_sent` can't double it. The Notifier
    never says whether Telegram took the message: a send that failed is repeated at the next interval, and
    the prompt is on the Options page meanwhile. `chat_id` is None when Telegram is not configured:
    nothing is sent and nothing is marked, so the prompts go out once it is. `settings` is read on every
    call."""

    def __init__(
        self,
        store: PromptStore,
        notifier: Notifier,
        issuer: CallbackIssuer,
        renderer: OptionRenderer,
        chat_id: int | None,
        settings: Callable[[], OptionSettings],
        clock: Clock,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.issuer = issuer
        self.renderer = renderer
        self.chat_id = chat_id
        self.settings = settings
        self.clock = clock

    async def send_due(self, now: datetime) -> int:
        """Send every due prompt; returns how many were sent."""
        if self.chat_id is None:
            return 0
        repeat = timedelta(hours=self.settings().prompt_repeat_hours)
        sent = 0
        for prompt in self.store.due_for_send(now):
            if prompt.last_sent_at is not None and now - prompt.last_sent_at < repeat:
                continue
            try:
                msg = self.renderer.prompt(prompt, self._buttons(prompt, self.chat_id))
                await self.notifier.send(msg)
                self.store.mark_sent(prompt.id, now)
            except Exception as exc:  # one bad prompt never stops the others
                log.error("options.prompt_send_failed", prompt_id=prompt.id, error_type=type(exc).__name__)
                continue
            sent += 1
        return sent

    def _buttons(self, prompt: PromptView, chat_id: int) -> Buttons:
        choices = button_choices(prompt)
        if not choices:
            return ()
        _, data = self.issuer.issue(CALLBACK_KIND, str(prompt.id), [c.code for c in choices], chat_id, None)
        buttons = [Button(c.label, data[c.code]) for c in choices]
        return tuple(tuple(buttons[i : i + BUTTONS_PER_ROW]) for i in range(0, len(buttons), BUTTONS_PER_ROW))
