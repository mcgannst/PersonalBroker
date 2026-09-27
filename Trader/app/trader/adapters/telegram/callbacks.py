"""Signed, single-use Telegram callback data (SPEC §14).

Data format (at most 64 bytes): `<k>:<ref>:<a>:<nonce>:<mac>`, k = p (proposal) | s (pause) | j (journal).
The MAC is the first 16 characters of the base64url HMAC-SHA256 of `v1|<k>:<ref>:<a>:<nonce>`, keyed
with HMAC-SHA256(SESSION_SECRET, "trader.telegram.callback.v1"). A forged or altered button therefore
never parses. Nonces live in `telegram_callbacks`, one per message (shared by its buttons); the first
claim of a nonce wins, so a replayed tap or the second button of a message is refused.
"""

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Self

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import CallbackKind
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock

ClaimResult = Literal["ok", "unknown", "used", "expired", "wrong_message"]

KEY_LABEL = b"trader.telegram.callback.v1"
MAC_VERSION = "v1"
MAC_CHARS = 16
NONCE_BYTES = 6  # 8 URL-safe base64 characters
MAX_DATA_BYTES = 64  # Telegram's callback_data limit

KIND_CODES: dict[CallbackKind, str] = {"proposal": "p", "pause": "s", "journal": "j"}
CODE_KINDS: dict[str, CallbackKind] = {code: kind for kind, code in KIND_CODES.items()}
ACTIONS: dict[CallbackKind, frozenset[str]] = {
    "proposal": frozenset({"a", "r"}),
    "pause": frozenset({"y", "n"}),
    "journal": frozenset({"y", "n"}),
}
REF_PATTERNS: dict[CallbackKind, re.Pattern[str]] = {
    "proposal": re.compile(r"^[0-9]{1,19}$"),  # proposal id
    "pause": re.compile(r"^[0-9]{1,19}$"),  # run id
    "journal": re.compile(r"^[0-9]{8}$"),  # YYYYMMDD
}
NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8}$")


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
        return cls(hmac.new(session_secret.encode(), KEY_LABEL, hashlib.sha256).digest())

    def _mac(self, body: str) -> str:
        digest = hmac.new(self._secret, f"{MAC_VERSION}|{body}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode()[:MAC_CHARS]

    def data(self, kind_code: str, ref: str, action: str, nonce: str) -> str:
        parts = (kind_code, ref, action, nonce)
        if any(not p or ":" in p for p in parts):
            raise ValueError("callback data parts must be non-empty and contain no ':'")
        body = ":".join(parts)
        data = f"{body}:{self._mac(body)}"
        if len(data.encode()) > MAX_DATA_BYTES:
            raise ValueError(f"callback data is longer than {MAX_DATA_BYTES} bytes")
        return data

    def parse(self, data: str) -> ParsedCallback | None:
        """None when malformed or the MAC is wrong."""
        parts = data.split(":")
        if len(parts) != 5 or len(data.encode()) > MAX_DATA_BYTES:
            return None
        code, ref, action, nonce, mac = parts
        body = ":".join(parts[:4])
        if not hmac.compare_digest(self._mac(body).encode(), mac.encode()):
            return None
        kind = CODE_KINDS.get(code)
        if kind is None or action not in ACTIONS[kind]:
            return None
        if not REF_PATTERNS[kind].match(ref) or not NONCE_PATTERN.match(nonce):
            return None
        return ParsedCallback(kind, ref, action, nonce)


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
        code = KIND_CODES[kind]
        now = self.clock.now()
        nonce = secrets.token_urlsafe(NONCE_BYTES)
        data = {action: self.signer.data(code, ref, action, nonce) for action in actions}
        with session_scope(self.factory) as s:
            s.add(
                m.TelegramCallback(
                    nonce=nonce,
                    kind=kind,
                    ref=ref,
                    chat_id=chat_id,
                    message_id=None,
                    created_at=now,
                    expires_at=None if ttl_seconds is None else now + timedelta(seconds=ttl_seconds),
                    used_at=None,
                    used_action=None,
                )
            )
        return nonce, data

    def bind(self, nonce: str, message_id: int) -> None:
        with session_scope(self.factory) as s:
            s.execute(
                update(m.TelegramCallback)
                .where(m.TelegramCallback.nonce == nonce)
                .values(message_id=message_id)
            )

    def claim(self, parsed: ParsedCallback, chat_id: int, message_id: int | None) -> ClaimResult:
        """Use up the nonce in its own transaction (row lock, so two taps can't both win)."""
        now = self.clock.now()
        with session_scope(self.factory) as s:
            row = s.execute(
                select(m.TelegramCallback).where(m.TelegramCallback.nonce == parsed.nonce).with_for_update()
            ).scalar_one_or_none()
            if row is None or (row.kind, row.ref, row.chat_id) != (parsed.kind, parsed.ref, chat_id):
                return "unknown"
            if row.used_at is not None:
                return "used"
            if row.expires_at is not None and now >= row.expires_at:
                return "expired"
            if row.message_id is not None and message_id != row.message_id:
                return "wrong_message"
            row.used_at, row.used_action = now, parsed.action
            return "ok"

    def release(self, nonce: str) -> None:
        """Make a claimed nonce usable again (the action failed before anything was decided)."""
        with session_scope(self.factory) as s:
            s.execute(
                update(m.TelegramCallback)
                .where(m.TelegramCallback.nonce == nonce)
                .values(used_at=None, used_action=None)
            )
