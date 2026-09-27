"""TelegramApi on python-telegram-bot's low-level `telegram.Bot` (PTB v21; its Application is not used).

The bot token lives only here. Never log a URL, a request, or an exception's repr from PTB or httpx: they
carry the token. Every failure leaves this module as a `TelegramApiError` whose text has the token
redacted and whose cause chain is cut (`from None`), so even a logged traceback cannot show it.

Error responses are read from Telegram's JSON body by `_RawErrorRequest`, so `description` is Telegram's
own text (PTB would rewrite "Bad Request: x" as "X") and `status` its HTTP code. Server errors (5xx) are
reported like network errors (status None): both are worth one retry. `Bot.initialize()` is never called:
it would make a network call on start-up and PTB puts the token in its InvalidToken message.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from types import TracebackType
from typing import Any, Self, TypeVar

import telegram
from pydantic import SecretStr
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.request import HTTPXRequest

from trader.adapters.telegram.types import CallbackQuery, TelegramApiError, Update
from trader.logging_setup import quiet_http_loggers
from trader.notify.types import Buttons

T = TypeVar("T")

ALLOWED_UPDATES = ("message", "callback_query")
REDACTED = "<redacted>"


class _ApiFailure(TelegramError):
    """Telegram answered with an error: its HTTP code, description and retry_after, as sent."""

    def __init__(self, status: int, description: str, retry_after: float | None) -> None:
        super().__init__(description)
        self.status = status
        self.description = description
        self.retry_after = retry_after


class _RawErrorRequest(HTTPXRequest):
    """HTTPXRequest that turns a non-2xx response into `_ApiFailure` before PTB rewrites it."""

    async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        code, payload = await super().do_request(*args, **kwargs)
        if 200 <= code <= 299:
            return code, payload
        description = f"HTTP {code}"
        retry_after: float | None = None
        try:
            body = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            body = None
        if isinstance(body, dict):
            description = str(body.get("description") or description)
            parameters = body.get("parameters")
            if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
                retry_after = float(parameters["retry_after"])
        raise _ApiFailure(code, description, retry_after)


def _keyboard(buttons: Buttons) -> telegram.InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return telegram.InlineKeyboardMarkup(
        [
            [telegram.InlineKeyboardButton(b.text, callback_data=b.callback_data) for b in row]
            for row in buttons
        ]
    )


def _update(u: telegram.Update) -> Update:
    """Only text messages and button taps carry content; any other update keeps both fields None."""
    if u.callback_query is not None:
        cq = u.callback_query
        message = cq.message
        if message is not None:
            return Update(
                update_id=u.update_id,
                chat_id=message.chat.id,
                from_id=cq.from_user.id,
                text=None,
                callback=CallbackQuery(
                    id=cq.id,
                    data=cq.data or "",
                    chat_id=message.chat.id,
                    from_id=cq.from_user.id,
                    message_id=message.message_id,
                ),
            )
    if u.message is not None:
        return Update(
            update_id=u.update_id,
            chat_id=u.message.chat.id,
            from_id=u.message.from_user.id if u.message.from_user is not None else None,
            text=u.message.text,
            callback=None,
        )
    chat = u.effective_chat
    user = u.effective_user
    return Update(
        update_id=u.update_id,
        chat_id=chat.id if chat is not None else None,
        from_id=user.id if user is not None else None,
        text=None,
        callback=None,
    )


class PtbTelegramApi:
    """Implements trader.adapters.telegram.types.TelegramApi."""

    def __init__(self, token: SecretStr, *, bot: telegram.Bot | None = None, timeout: float = 20.0) -> None:
        # httpx logs request URLs at INFO and PTB logs its base URL at DEBUG: both contain the token.
        quiet_http_loggers()
        logging.getLogger("telegram").setLevel(logging.WARNING)
        self._token = token
        self._timeout = timeout
        self._requests: tuple[HTTPXRequest, ...] = ()
        if bot is None:
            limits: dict[str, Any] = {
                "read_timeout": timeout,
                "write_timeout": timeout,
                "connect_timeout": timeout,
                "pool_timeout": timeout,
            }
            updates_request = _RawErrorRequest(connection_pool_size=1, **limits)
            request = _RawErrorRequest(connection_pool_size=4, **limits)
            self._requests = (updates_request, request)
            bot = telegram.Bot(token.get_secret_value(), request=request, get_updates_request=updates_request)
        self._bot = bot

    async def __aenter__(self) -> Self:
        for request in self._requests:
            await request.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _redact(self, text: str) -> str:
        token = self._token.get_secret_value()
        if token:
            text = text.replace(token, REDACTED)
            secret = token.partition(":")[2]
            if secret:
                text = text.replace(secret, REDACTED)
        return text

    def _error(self, exc: Exception) -> TelegramApiError:
        status: int | None
        retry_after: float | None = None
        if isinstance(exc, _ApiFailure):
            if exc.status >= 500:
                return TelegramApiError(None, self._redact(f"{exc.status} {exc.description}"))
            status, description, retry_after = exc.status, exc.description, exc.retry_after
        elif isinstance(exc, RetryAfter):
            raw = exc.retry_after
            retry_after = raw.total_seconds() if isinstance(raw, timedelta) else float(raw)
            status, description = 429, exc.message
        elif isinstance(exc, BadRequest | ChatMigrated):
            status, description = 400, exc.message
        elif isinstance(exc, Forbidden):
            status, description = 403, exc.message
        elif isinstance(exc, InvalidToken):
            status, description = 401, "Unauthorized (invalid token)"
        elif isinstance(exc, Conflict):
            status, description = 409, exc.message
        elif isinstance(exc, NetworkError | TelegramError):
            status, description = None, f"{type(exc).__name__}: {exc.message}"
        else:
            status, description = None, f"{type(exc).__name__}: {exc}"
        return TelegramApiError(status, self._redact(description), retry_after)

    async def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        try:
            return await fn()
        except Exception as exc:  # every failure leaves as TelegramApiError, without the PTB/httpx chain
            error = self._error(exc)
        raise error from None

    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        updates = await self._call(
            lambda: self._bot.get_updates(
                offset=offset,
                timeout=timeout,
                allowed_updates=list(ALLOWED_UPDATES),
                read_timeout=self._timeout + timeout,
            )
        )
        return [_update(u) for u in updates]

    async def send_message(self, chat_id: int, text: str, buttons: Buttons = (), silent: bool = False) -> int:
        message = await self._call(
            lambda: self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(buttons),
                disable_notification=silent,
            )
        )
        return message.message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str, buttons: Buttons = ()) -> None:
        # Without reply_markup Telegram removes the message's inline keyboard.
        await self._call(
            lambda: self._bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(buttons),
            )
        )

    async def edit_buttons(self, chat_id: int, message_id: int, buttons: Buttons = ()) -> None:
        await self._call(
            lambda: self._bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=_keyboard(buttons)
            )
        )

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self._call(lambda: self._bot.answer_callback_query(callback_id, text=text))

    async def aclose(self) -> None:
        try:
            if self._requests:
                for request in self._requests:
                    await request.shutdown()
            else:
                await self._bot.shutdown()
        except Exception as exc:
            raise self._error(exc) from None
