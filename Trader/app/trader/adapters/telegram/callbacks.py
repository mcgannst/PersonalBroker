"""Signed, single-use Telegram callback data (SPEC §14).

Data format (at most 64 bytes): `<k>:<ref>:<a>:<nonce>:<mac>`, k = p (proposal) | s (pause) | j (journal).
Nonces live in `telegram_callbacks`; the first claim of a nonce wins.

P3-T1 stub: the contracts are final, P3-T6 implements them.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Self

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import CallbackKind
from trader.market.clock import Clock

ClaimResult = Literal["ok", "unknown", "used", "expired", "wrong_message"]


@dataclass(frozen=True, slots=True)
class ParsedCallback:
    kind: CallbackKind
    ref: str
    action: str
    nonce: str


class CallbackSigner:
    """HMAC-SHA256 signing of callback data with a key derived from SESSION_SECRET."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    @classmethod
    def derive(cls, session_secret: str) -> Self:
        """Key = HMAC-SHA256(session secret, "trader.telegram.callback.v1")."""
        raise NotImplementedError("P3-T6")

    def data(self, kind_code: str, ref: str, action: str, nonce: str) -> str:
        raise NotImplementedError("P3-T6")

    def parse(self, data: str) -> ParsedCallback | None:
        """None when malformed or the MAC is wrong."""
        raise NotImplementedError("P3-T6")


class DbCallbackIssuer:
    """Implements trader.adapters.telegram.types.CallbackIssuer on `telegram_callbacks`."""

    def __init__(self, factory: sessionmaker[Session], clock: Clock, signer: CallbackSigner) -> None:
        self.factory = factory
        self.clock = clock
        self.signer = signer

    def issue(
        self,
        kind: CallbackKind,
        ref: str,
        actions: Sequence[str],
        chat_id: int,
        ttl_seconds: int | None,
    ) -> tuple[str, dict[str, str]]:
        raise NotImplementedError("P3-T6")

    def bind(self, nonce: str, message_id: int) -> None:
        raise NotImplementedError("P3-T6")

    def claim(self, parsed: ParsedCallback, chat_id: int, message_id: int | None) -> ClaimResult:
        raise NotImplementedError("P3-T6")

    def release(self, nonce: str) -> None:
        raise NotImplementedError("P3-T6")
