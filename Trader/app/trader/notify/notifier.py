"""The Notifier: sends OutboundMessages through the Telegram API, deduplicated through `notifications`,
never raising, logging Telegram's error text and never the token (SPEC §4.4, §14).

Delivery is at most once for a message with a `dedupe_key`: the `notifications` row is claimed (inserted
with status `sending`) before the send, so a crash between the claim and the send loses the message but
never doubles it. A message without a key is always sent.

Retry and delivery rules (fix round 1):
- Inside one send, a failure is retried once only when the request surely never reached Telegram: a 429
  (after `retry_after`, capped at 30 s), a 5xx or a connect-phase network error (after 2 s; the API
  client raises these with `not_sent = True`).
- A failure that may have delivered the message (a read timeout, a dropped connection, any unexpected
  exception) is recorded with status `unknown` and never re-sent.
- A failure that surely did not deliver it (Telegram refused it, or the retries ran out) is recorded
  `failed`. A later send with the same key may claim a `failed` row again, up to MAX_SEND_ATTEMPTS sends
  in total (the row's `attempts` counts sends, not API calls), so a 429 storm doesn't lose an alert for
  good. A row where some parts of a split message went out is never claimed again.
- `sent`, `unknown` and `sending` rows are never re-sent.

Telegram never blocks trading: `send` catches every exception (API, network, database) and logs it.
`asyncio.CancelledError` still propagates, so a worker can shut down mid-send.

The database work of a send (the claim and the two record steps) runs in a worker thread
(`asyncio.to_thread`, P4-T18), so the API's Telegram test route never runs synchronous queries on its
event loop; the order of the steps is unchanged.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal

import structlog
from sqlalchemy import and_, null, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import TelegramApi, TelegramApiError
from trader.db.models import Notification
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock
from trader.notify.messages import TELEGRAM_LIMIT  # the one definition (re-exported here)
from trader.notify.types import Buttons, OutboundMessage

log = structlog.get_logger("notify.notifier")

MIN_SEND_INTERVAL = 1.0  # seconds between two sends of one notifier (Telegram's per-chat limit)
MAX_RETRY_AFTER = 30.0  # longest 429 wait honoured before the single retry
NETWORK_RETRY_WAIT = 2.0  # wait before retrying a connect-phase error or a 5xx
MAX_SEND_ATTEMPTS = 3  # sends of one dedupe key in total (a `failed` row may be claimed again)


# --- split_text -------------------------------------------------------------------------------------------

# One atom each: a tag, an entity, or a single character. A part is never cut inside an atom.
_ATOM = re.compile(r"<[^<>]*>|&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);|[\s\S]")
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)[^<>]*>")
_ANY_TAG = re.compile(r"<[^<>]*>")


@dataclass(frozen=True, slots=True)
class _Atom:
    text: str
    opens: str | None = None  # the tag name this atom opens
    closes: str | None = None  # the tag name this atom closes


def _atoms(text: str) -> list[_Atom]:
    atoms: list[_Atom] = []
    for m in _ATOM.finditer(text):
        s = m.group()
        tag = _TAG.fullmatch(s) if s.startswith("<") else None
        if tag is None or s.endswith("/>"):
            atoms.append(_Atom(s))
        elif tag.group(1):
            atoms.append(_Atom(s, closes=tag.group(2).lower()))
        else:
            atoms.append(_Atom(s, opens=tag.group(2).lower()))
    return atoms


Stack = tuple[tuple[str, str], ...]  # the open tags: (name, the opening tag as written)


def _apply(stack: Stack, atom: _Atom) -> Stack:
    if atom.opens is not None:
        return (*stack, (atom.opens, atom.text))
    if atom.closes is not None:
        names = [name for name, _ in stack]
        if atom.closes in names:  # close it (and anything left open inside it)
            return stack[: len(names) - 1 - names[::-1].index(atom.closes)]
    return stack


def _closers(stack: Stack) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _visible(part: str) -> bool:
    return bool(_ANY_TAG.sub("", part).strip())


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Cut `text` into Telegram-sendable parts of at most `limit` characters.

    A cut prefers the last line break that fits, then the last space (the separator itself is dropped),
    else it is hard at the limit, but never inside an HTML entity (`&amp;`) or a tag (`<b>`). Tags stay
    balanced: a tag open at a cut is closed at the end of its part and reopened at the start of the next.
    Empty or whitespace-only parts are dropped, so the buttons (on the last part) always go out with text.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text] if _visible(text) else []
    atoms = _atoms(text)
    parts: list[str] = []
    stack: Stack = ()
    i = 0
    while i < len(atoms):
        prefix = "".join(opening for _, opening in stack)
        if len(prefix) + len(_closers(stack)) >= limit:
            stack, prefix = (), ""  # pathological nesting: give up the formatting rather than loop
        length, cur = len(prefix), stack
        j = i
        newline: tuple[int, Stack] | None = None
        space: tuple[int, Stack] | None = None
        while j < len(atoms):
            atom = atoms[j]
            nxt = _apply(cur, atom)
            if length + len(atom.text) + len(_closers(nxt)) > limit:
                break
            if atom.text == "\n" and j > i:
                newline = (j, cur)
            elif atom.text in (" ", "\t") and j > i:
                space = (j, cur)
            length += len(atom.text)
            cur = nxt
            j += 1
        if j == len(atoms):
            end, next_i, end_stack = j, j, cur
        elif newline is not None:
            end, next_i, end_stack = newline[0], newline[0] + 1, newline[1]
        elif space is not None:
            end, next_i, end_stack = space[0], space[0] + 1, space[1]
        elif j > i:
            end, next_i, end_stack = j, j, cur
        elif stack:
            stack = ()  # the reopened tags leave no room for the next atom: drop them
            continue
        else:  # one atom longer than the limit (a giant tag): cut its text raw
            big = atoms[i].text
            parts.extend(big[k : k + limit] for k in range(0, len(big), limit))
            i += 1
            continue
        parts.append(prefix + "".join(a.text for a in atoms[i:end]) + _closers(end_stack))
        stack, i = end_stack, next_i
    return [p for p in parts if _visible(p)]


# --- the notifier -----------------------------------------------------------------------------------------

Outcome = Literal["failed", "unknown"]


def _describe(exc: Exception) -> str:
    """The failure as stored and logged: Telegram's status and description (just the description for a
    network error), or only the exception type (an unexpected exception's text could carry a URL with the
    token)."""
    if isinstance(exc, TelegramApiError):
        if exc.status is None:
            return exc.description
        return f"{exc.status} {exc.description}"
    return type(exc).__name__


def _not_sent(exc: Exception) -> bool:
    """True when the request surely never reached Telegram."""
    return isinstance(exc, TelegramApiError) and getattr(exc, "not_sent", False) is True


def _outcome(exc: Exception) -> Outcome:
    """`failed` when the message was surely not delivered (Telegram refused it, a 429, or it never got
    there); `unknown` when it may have been (a timeout after sending, an unexpected exception)."""
    if isinstance(exc, TelegramApiError) and (exc.status is not None or _not_sent(exc)):
        return "failed"
    return "unknown"


class _SendFailed(Exception):
    def __init__(self, cause: Exception, calls: int, message_ids: list[int]) -> None:
        super().__init__(_describe(cause))
        self.cause = cause
        self.calls = calls
        self.message_ids = message_ids


class TelegramNotifier:
    """Notifier over a TelegramApi, sending to one chat."""

    def __init__(
        self,
        api: TelegramApi,
        chat_id: int,
        factory: sessionmaker[Session],
        clock: Clock,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api = api
        self.chat_id = chat_id
        self.factory = factory
        self.clock = clock
        self.sleep = sleep
        self._lock = asyncio.Lock()
        self._last_call: datetime | None = None

    async def send(self, msg: OutboundMessage) -> None:
        try:
            await self._send(msg)
        except Exception as exc:  # never raise: Telegram must not block trading
            log.error("notify.send_crashed", kind=msg.kind, dedupe_key=msg.dedupe_key, error=_describe(exc))

    async def _send(self, msg: OutboundMessage) -> None:
        try:
            claim = await asyncio.to_thread(self._claim, msg)
        except Exception as exc:
            log.error(
                "notify.db_failed",
                step="claim",
                kind=msg.kind,
                dedupe_key=msg.dedupe_key,
                error=_describe(exc),
            )
            if msg.dedupe_key is not None:
                return  # can't prove the key is unused: hold the message back rather than risk a double
            claim = None
        else:
            if claim is None:
                log.info("notify.duplicate_skipped", kind=msg.kind, dedupe_key=msg.dedupe_key)
                return
        row_id, attempt = claim if claim is not None else (None, 1)

        try:
            async with self._lock:
                message_ids, calls = await self._deliver(msg)
        except _SendFailed as failed:
            await asyncio.to_thread(self._record_failure, msg, row_id, attempt, failed)
            return
        await asyncio.to_thread(self._record_success, row_id, message_ids, calls)

    def _claim(self, msg: OutboundMessage) -> tuple[int, int] | None:
        """Insert the `sending` row, or take back a `failed` row of the same key that nothing of was
        delivered and that has sends left; returns (row id, this send's number), or None when the key
        is taken (sending, sent, unknown, or out of sends)."""
        table = Notification.__table__.c
        stmt = (
            insert(Notification)
            .values(
                kind=msg.kind,
                dedupe_key=msg.dedupe_key,
                text=msg.text,
                created_at=self.clock.now(),
                status="sending",
                attempts=1,
            )
            .on_conflict_do_update(
                index_elements=[Notification.dedupe_key],
                set_={"status": "sending", "attempts": table.attempts + 1, "error": None},
                where=and_(
                    table.status == "failed",
                    table.attempts < MAX_SEND_ATTEMPTS,
                    table.message_ids.is_(None),
                ),
            )
            .returning(Notification.id, Notification.attempts)
        )
        with session_scope(self.factory) as session:
            row = session.execute(stmt).one_or_none()
        return None if row is None else (row[0], row[1])

    async def _deliver(self, msg: OutboundMessage) -> tuple[list[int], int]:
        parts = split_text(msg.text, TELEGRAM_LIMIT)
        if not parts:
            raise _SendFailed(TelegramApiError(400, "empty message (nothing to send)"), 0, [])
        message_ids: list[int] = []
        calls = 0
        for index, part in enumerate(parts):
            buttons: Buttons = msg.buttons if index == len(parts) - 1 else ()
            retried = False
            while True:
                await self._pace()
                calls += 1
                try:
                    message_ids.append(await self.api.send_message(self.chat_id, part, buttons, msg.silent))
                    break
                except Exception as exc:
                    wait = self._retry_wait(exc)
                    if retried or wait is None:
                        raise _SendFailed(exc, calls, message_ids) from None
                    retried = True
                    log.info("notify.retrying", kind=msg.kind, error=_describe(exc), wait_seconds=wait)
                    await self.sleep(wait)
        return message_ids, calls

    @staticmethod
    def _retry_wait(exc: Exception) -> float | None:
        """Seconds to wait before the single retry, or None when a retry could double the message or
        cannot help (only a 429, a 5xx or a connect-phase error is retried)."""
        if not isinstance(exc, TelegramApiError):
            return None
        if exc.status == 429:
            return min(exc.retry_after if exc.retry_after is not None else 1.0, MAX_RETRY_AFTER)
        if (exc.status is not None and exc.status >= 500) or _not_sent(exc):
            return NETWORK_RETRY_WAIT
        return None

    async def _pace(self) -> None:
        """Keep API calls at least MIN_SEND_INTERVAL apart, then note this call's time."""
        if self._last_call is not None:
            elapsed = (self.clock.now() - self._last_call).total_seconds()
            if elapsed < MIN_SEND_INTERVAL:
                await self.sleep(MIN_SEND_INTERVAL - elapsed)
        self._last_call = self.clock.now()

    def _record_success(self, row_id: int | None, message_ids: list[int], calls: int) -> None:
        if row_id is None:
            return
        try:
            with session_scope(self.factory) as session:
                session.execute(
                    update(Notification)
                    .where(Notification.id == row_id)
                    .values(status="sent", sent_at=self.clock.now(), message_ids=message_ids)
                )
        except Exception as exc:
            log.error(
                "notify.db_failed",
                step="record_sent",
                notification_id=row_id,
                calls=calls,
                error=_describe(exc),
            )

    def _record_failure(
        self, msg: OutboundMessage, row_id: int | None, attempt: int, failed: _SendFailed
    ) -> None:
        error = _describe(failed.cause)
        outcome = _outcome(failed.cause)
        status = failed.cause.status if isinstance(failed.cause, TelegramApiError) else None
        description = failed.cause.description if isinstance(failed.cause, TelegramApiError) else error
        retry_later = (
            outcome == "failed"
            and msg.dedupe_key is not None
            and not failed.message_ids
            and attempt < MAX_SEND_ATTEMPTS
        )
        log.warning(
            "telegram.send_failed",
            kind=msg.kind,
            dedupe_key=msg.dedupe_key,
            notification_id=row_id,
            outcome=outcome,
            status=status,
            description=description,
            attempt=attempt,
            calls=failed.calls,
        )
        if outcome == "unknown":
            message = f"Telegram send may or may not have been delivered (not re-sent): {error}"
        else:
            message = f"Telegram send failed: {error}"
        try:
            with session_scope(self.factory) as session:
                if row_id is not None:
                    session.execute(
                        update(Notification)
                        .where(Notification.id == row_id)
                        .values(status=outcome, error=error, message_ids=failed.message_ids or null())
                    )
                log_event(
                    session,
                    self.clock,
                    "warning",
                    "telegram",
                    message,
                    {
                        "kind": msg.kind,
                        "dedupe_key": msg.dedupe_key,
                        "notification_id": row_id,
                        "outcome": outcome,
                        "status": status,
                        "description": description,
                        "attempt": attempt,
                        "retry_later": retry_later,
                    },
                )
        except Exception as exc:
            log.error("notify.db_failed", step="record_failed", notification_id=row_id, error=_describe(exc))


class NullNotifier:
    """Notifier used when Telegram is not configured: logs once per process, records nothing."""

    _logged: ClassVar[bool] = False

    async def send(self, msg: OutboundMessage) -> None:
        if not NullNotifier._logged:
            NullNotifier._logged = True
            log.warning("telegram.not_configured", kind=msg.kind)
