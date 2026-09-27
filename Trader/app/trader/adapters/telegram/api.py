"""TelegramApi on python-telegram-bot's low-level `telegram.Bot` (PTB v21; its Application is not used).

The bot token lives only here. Never log a URL, a request, or an exception's repr from PTB or httpx: they
carry the token.

P3-T1 stub: the contracts are final, P3-T5 implements them.
"""

from types import TracebackType
from typing import Self

import telegram
from pydantic import SecretStr

from trader.adapters.telegram.types import Update
from trader.notify.types import Buttons


class PtbTelegramApi:
    """Implements trader.adapters.telegram.types.TelegramApi."""

    def __init__(self, token: SecretStr, *, bot: telegram.Bot | None = None, timeout: float = 20.0) -> None:
        self._token = token
        self._bot = bot
        self._timeout = timeout

    async def __aenter__(self) -> Self:
        raise NotImplementedError("P3-T5")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError("P3-T5")

    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        raise NotImplementedError("P3-T5")

    async def send_message(self, chat_id: int, text: str, buttons: Buttons = (), silent: bool = False) -> int:
        raise NotImplementedError("P3-T5")

    async def edit_message(self, chat_id: int, message_id: int, text: str, buttons: Buttons = ()) -> None:
        raise NotImplementedError("P3-T5")

    async def edit_buttons(self, chat_id: int, message_id: int, buttons: Buttons = ()) -> None:
        raise NotImplementedError("P3-T5")

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        raise NotImplementedError("P3-T5")

    async def aclose(self) -> None:
        raise NotImplementedError("P3-T5")
