"""Phase 3 Telegram contracts: updates, the low-level API protocol and its error, and the protocols the
bot, the commands and the relay use to talk to each other (SPEC §4.4, §14).

Only `trader.adapters.telegram.api` (P3-T5) touches python-telegram-bot; everything else uses these
types, and tests use the fakes in tests/fakes_telegram.py.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from trader.notify.types import Buttons, OutboundMessage

CallbackKind = Literal["proposal", "pause", "journal"]


@dataclass(frozen=True, slots=True)
class CallbackQuery:
    """A button tap. `message_id` is the message the button was on (None if Telegram omits it)."""

    id: str
    data: str
    chat_id: int
    from_id: int
    message_id: int | None


@dataclass(frozen=True, slots=True)
class Update:
    """One getUpdates item: a text message (`text`), a button tap (`callback`), or neither (any other
    update kind, which the bot ignores)."""

    update_id: int
    chat_id: int | None
    from_id: int | None
    text: str | None
    callback: CallbackQuery | None


class TelegramApiError(Exception):
    """A failed Telegram call. `status` is the HTTP-style code (400, 403, 409, 429 ...) or None for a
    network error or timeout; `description` is Telegram's error text. Never carries the bot token."""

    def __init__(self, status: int | None, description: str, retry_after: float | None = None) -> None:
        super().__init__(f"{status} {description}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


class TelegramApi(Protocol):
    """The Bot API calls the app uses. Every method raises TelegramApiError on failure."""

    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]: ...

    async def send_message(self, chat_id: int, text: str, buttons: Buttons = (), silent: bool = False) -> int:
        """Send HTML text; returns the message id."""
        ...

    async def edit_message(self, chat_id: int, message_id: int, text: str, buttons: Buttons = ()) -> None: ...

    async def edit_buttons(self, chat_id: int, message_id: int, buttons: Buttons = ()) -> None:
        """Change only the inline keyboard (empty removes it)."""
        ...

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...

    async def aclose(self) -> None: ...


class CallbackIssuer(Protocol):
    def issue(
        self,
        kind: CallbackKind,
        ref: str,
        actions: Sequence[str],
        chat_id: int,
        ttl_seconds: int | None,
    ) -> tuple[str, dict[str, str]]:
        """Create a single-use nonce for one message; returns (nonce, {action: callback data})."""
        ...

    def bind(self, nonce: str, message_id: int) -> None:
        """Record the message the nonce's buttons were sent on."""
        ...


class ProposalMessenger(Protocol):
    async def send_proposal(self, proposal_id: int, *, resend: bool = False) -> bool:
        """Send the approval message for a pending proposal; False when skipped."""
        ...

    async def sync_closed(self) -> int:
        """Edit messages of proposals no longer pending to their final text; returns how many."""
        ...


class CommandHandler(Protocol):
    async def handle(self, text: str) -> list[OutboundMessage]: ...

    async def confirm_pause(self, action: str) -> str:
        """Act on a pause confirmation tap ("y" or "n"); returns the callback answer text."""
        ...
