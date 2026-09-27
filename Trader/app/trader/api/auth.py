"""Web authentication (SPEC §2, §10, §14; BR-56): Argon2id passwords, server-side sessions in a signed
`HttpOnly; Secure; SameSite=Strict` cookie, CSRF on every change, a per-IP login rate limit and a per-account
lockout, optional TOTP, and the first-start admin from env.

Stub (P4-T1): T4 implements every function and class below with these signatures.
"""

from collections.abc import Callable
from typing import Literal

from fastapi import Request
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from trader.api.deps import ApiServices, AuthUser
from trader.api.schemas import SessionOut, TotpSetupOut
from trader.market.clock import Clock

COOKIE_NAME = "trader_session"
CSRF_HEADER = "X-CSRF-Token"
MIN_PASSWORD_CHARS = 8  # Stephen, 2026-09-27: 7 is rejected, 8 accepted
USERNAME_PATTERN = r"^[a-z][a-z0-9_.-]{2,49}$"

AdminResult = Literal["created", "exists", "not_configured", "rejected"]


def hash_password(pw: str) -> str:
    """An Argon2id hash (argon2-cffi defaults)."""
    raise NotImplementedError("P4-T4")


def verify_password(stored: str, pw: str) -> tuple[bool, str | None]:
    """(ok, a new hash when a rehash is due)."""
    raise NotImplementedError("P4-T4")


class SessionSigner:
    """Signs session cookies: `<token>.<mac>`; the database keeps only the token's SHA-256."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    @classmethod
    def derive(cls, session_secret: str) -> "SessionSigner":
        """key = HMAC-SHA256(session_secret, "trader.web.session.v1")."""
        raise NotImplementedError("P4-T4")

    def issue(self) -> tuple[str, str]:
        """(the cookie value `<token>.<mac>`, the SHA-256 hex of the token)."""
        raise NotImplementedError("P4-T4")

    def parse(self, cookie: str) -> str | None:
        """The token's SHA-256 hex, or None when the cookie is malformed or its MAC is wrong."""
        raise NotImplementedError("P4-T4")


class LoginLimiter:
    """`per_minute()` login attempts per client IP per sliding minute (in memory: one uvicorn worker)."""

    def __init__(self, clock: Clock, per_minute: Callable[[], int]) -> None:
        self._clock = clock
        self._per_minute = per_minute

    def allow(self, ip: str) -> float | None:
        """None when allowed, else the seconds to wait."""
        raise NotImplementedError("P4-T4")


def login(
    services: ApiServices,
    username: str,
    password: str,
    totp: str | None,
    ip: str | None,
    user_agent: str | None,
) -> tuple[SessionOut, str]:
    """(the session, the cookie value). Raises ApiError 401 / 429."""
    raise NotImplementedError("P4-T4")


def logout(services: ApiServices, user: AuthUser) -> None:
    raise NotImplementedError("P4-T4")


def authenticate(request: Request, services: ApiServices) -> AuthUser:
    """The user of a valid session cookie, else ApiError 401 (and the response clears the cookie)."""
    raise NotImplementedError("P4-T4")


def check_csrf(request: Request, user: AuthUser) -> AuthUser:
    """On POST/PUT/DELETE: `X-CSRF-Token` equals the session's token and any `Origin` is ours, else 403."""
    raise NotImplementedError("P4-T4")


def change_password(services: ApiServices, user: AuthUser, current: str, new: str, totp: str | None) -> None:
    raise NotImplementedError("P4-T4")


def totp_setup(services: ApiServices, user: AuthUser, password: str) -> TotpSetupOut:
    raise NotImplementedError("P4-T4")


def totp_confirm(services: ApiServices, user: AuthUser, code: str) -> None:
    raise NotImplementedError("P4-T4")


def totp_disable(services: ApiServices, user: AuthUser, password: str, code: str) -> None:
    raise NotImplementedError("P4-T4")


def ensure_admin(
    factory: sessionmaker[Session], clock: Clock, username: str | None, password: SecretStr | None
) -> AdminResult:
    """Creates the first user from env (`trader create-admin`, T18) only when the table is empty."""
    raise NotImplementedError("P4-T4")
