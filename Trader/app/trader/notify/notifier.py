"""The Notifier: sends OutboundMessages through the Telegram API, deduplicated through `notifications`,
never raising, logging Telegram's error text and never the token (SPEC §4.4, §14).

Delivery is at most once for a message with a `dedupe_key`: the `notifications` row is claimed (inserted
with status `sending`) before the send, so a crash between the claim and the send loses the message but
never doubles it, and a key whose send failed is not tried again. A message without a key is always sent.

Telegram never blocks trading: `send` catches every exception (API, network, database) and logs it.
`asyncio.CancelledError` still propagates, so a worker can shut down mid-send.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import ClassVar

import structlog
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import TelegramApi, TelegramApiError
from trader.db.models import Notification
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock
from trader.notify.types import Buttons, OutboundMessage

log = structlog.get_logger("notify.notifier")

TELEGRAM_LIMIT = 4096
MIN_SEND_INTERVAL = 1.0  # seconds between two sends of one notifier (Telegram's per-chat limit)
MAX_RETRY_AFTER = 30.0  # longest 429 wait honoured before the single retry
NETWORK_RETRY_WAIT = 2.0  # wait before retrying a network error or timeout (status None)


def split_text(text: str, limit: int = 4096) -> list[str]:
    """Cut `text` into parts of at most `limit` characters, at the last line break before the limit
    (the break itself is dropped), or hard at the limit when a part has no line break."""
    if limit < 1:
        raise ValueError("limit must be positive")
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit + 1)
        if cut <= 0:
            parts.append(rest[:limit])
            rest = rest[limit:]
        else:
            parts.append(rest[:cut])
            rest = rest[cut + 1 :]
    parts.append(rest)
    return parts


def _describe(exc: Exception) -> str:
    """The failure as stored and logged: Telegram's status and description, or only the exception type
    (an unexpected exception's text could carry a URL with the token)."""
    if isinstance(exc, TelegramApiError):
        return f"{exc.status} {exc.description}"
    return type(exc).__name__


class _SendFailed(Exception):
    def __init__(self, cause: Exception, attempts: int, message_ids: list[int]) -> None:
        super().__init__(_describe(cause))
        self.cause = cause
        self.attempts = attempts
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
            row_id = self._claim(msg)
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
            row_id = None
        else:
            if row_id is None:
                log.info("notify.duplicate_skipped", kind=msg.kind, dedupe_key=msg.dedupe_key)
                return

        try:
            async with self._lock:
                message_ids, attempts = await self._deliver(msg)
        except _SendFailed as failed:
            self._record_failure(msg, row_id, failed)
            return
        self._record_success(row_id, message_ids, attempts)

    def _claim(self, msg: OutboundMessage) -> int | None:
        """Insert the `sending` row; None when the dedupe key already exists."""
        stmt = (
            insert(Notification)
            .values(
                kind=msg.kind,
                dedupe_key=msg.dedupe_key,
                text=msg.text,
                created_at=self.clock.now(),
                status="sending",
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
            .returning(Notification.id)
        )
        with session_scope(self.factory) as session:
            return session.execute(stmt).scalar_one_or_none()

    async def _deliver(self, msg: OutboundMessage) -> tuple[list[int], int]:
        parts = split_text(msg.text, TELEGRAM_LIMIT)
        message_ids: list[int] = []
        attempts = 0
        for index, part in enumerate(parts):
            buttons: Buttons = msg.buttons if index == len(parts) - 1 else ()
            retried = False
            while True:
                await self._pace()
                attempts += 1
                try:
                    message_ids.append(await self.api.send_message(self.chat_id, part, buttons, msg.silent))
                    break
                except Exception as exc:
                    wait = self._retry_wait(exc)
                    if retried or wait is None:
                        raise _SendFailed(exc, attempts, message_ids) from None
                    retried = True
                    log.info("notify.retrying", kind=msg.kind, error=_describe(exc), wait_seconds=wait)
                    await self.sleep(wait)
        return message_ids, attempts

    @staticmethod
    def _retry_wait(exc: Exception) -> float | None:
        """Seconds to wait before the single retry, or None when the error is not worth retrying."""
        if not isinstance(exc, TelegramApiError):
            return None
        if exc.status == 429:
            return min(exc.retry_after if exc.retry_after is not None else 1.0, MAX_RETRY_AFTER)
        if exc.status is None:
            return NETWORK_RETRY_WAIT
        return None

    async def _pace(self) -> None:
        """Keep API calls at least MIN_SEND_INTERVAL apart, then note this call's time."""
        if self._last_call is not None:
            elapsed = (self.clock.now() - self._last_call).total_seconds()
            if elapsed < MIN_SEND_INTERVAL:
                await self.sleep(MIN_SEND_INTERVAL - elapsed)
        self._last_call = self.clock.now()

    def _record_success(self, row_id: int | None, message_ids: list[int], attempts: int) -> None:
        if row_id is None:
            return
        try:
            with session_scope(self.factory) as session:
                session.execute(
                    update(Notification)
                    .where(Notification.id == row_id)
                    .values(
                        status="sent", sent_at=self.clock.now(), message_ids=message_ids, attempts=attempts
                    )
                )
        except Exception as exc:
            log.error("notify.db_failed", step="record_sent", notification_id=row_id, error=_describe(exc))

    def _record_failure(self, msg: OutboundMessage, row_id: int | None, failed: _SendFailed) -> None:
        error = _describe(failed.cause)
        status = failed.cause.status if isinstance(failed.cause, TelegramApiError) else None
        description = failed.cause.description if isinstance(failed.cause, TelegramApiError) else error
        log.warning(
            "telegram.send_failed",
            kind=msg.kind,
            dedupe_key=msg.dedupe_key,
            notification_id=row_id,
            status=status,
            description=description,
            attempts=failed.attempts,
        )
        try:
            with session_scope(self.factory) as session:
                if row_id is not None:
                    session.execute(
                        update(Notification)
                        .where(Notification.id == row_id)
                        .values(
                            status="failed",
                            error=error,
                            attempts=failed.attempts,
                            message_ids=failed.message_ids or None,
                        )
                    )
                log_event(
                    session,
                    self.clock,
                    "warning",
                    "telegram",
                    f"Telegram send failed: {error}",
                    {
                        "kind": msg.kind,
                        "dedupe_key": msg.dedupe_key,
                        "notification_id": row_id,
                        "status": status,
                        "description": description,
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
