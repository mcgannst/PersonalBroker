"""Web authentication (SPEC §2, §10, §14; BR-56): Argon2id passwords, server-side sessions in a signed
`HttpOnly; Secure; SameSite=Strict` cookie, CSRF on every change, a per-IP login rate limit and a per-account
lockout, optional TOTP, and the first-start admin from env.

- The cookie is `<token>.<mac>` (HMAC with a key derived from `SESSION_SECRET`); the database keeps only the
  token's SHA-256, so logout, a password change and expiry really end a session.
- Every failure a visitor could learn from is the same generic 401 ("Invalid username or password"); a
  locked account is a 429. Nothing here logs, audits or returns a password, a code, a TOTP secret (except the
  one-time setup response) or a session token.
"""

import base64
import hashlib
import hmac
import math
import re
import secrets
import threading
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import pyotp
import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trader.api.deps import ApiServices, AuthUser, _settings, actor
from trader.api.errors import ApiError
from trader.api.schemas import SessionOut, TotpSetupOut, UserOut
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock

log = structlog.get_logger("api.auth")

COOKIE_NAME = "trader_session"
CSRF_HEADER = "X-CSRF-Token"
MIN_PASSWORD_CHARS = 8  # Stephen, 2026-09-27: 7 is rejected, 8 accepted
# At most 46 characters, so the audit actor `web:<username>` fits `audit_log.actor` and
# `manual_watchlists.uploaded_by` (varchar(50), P4-T18).
USERNAME_PATTERN = r"^[a-z][a-z0-9_.-]{2,45}$"

AdminResult = Literal["created", "exists", "not_configured", "rejected"]

# Sent with every 401 and with logout: the browser drops the session cookie.
CLEAR_COOKIE = (
    f'{COOKIE_NAME}=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/; '
    "HttpOnly; Secure; SameSite=Strict"
)
INVALID_LOGIN = "Invalid username or password"
LAST_SEEN_EVERY = timedelta(minutes=1)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_COOKIE_CHARS = 256
TOTP_WINDOW = 1  # steps either side of now

USED_CODE = "That code was already used. Wait for the next code, then try again."
ARGON2_CONCURRENCY = 4  # each Argon2 call takes ~64 MiB: a login flood queues instead of using GBs

_hasher = PasswordHasher()  # Argon2id, argon2-cffi's defaults
_argon2_slots = threading.BoundedSemaphore(ARGON2_CONCURRENCY)


# --- passwords ----------------------------------------------------------------------------------------------


def hash_password(pw: str) -> str:
    """An Argon2id hash (argon2-cffi defaults)."""
    with _argon2_slots:
        return _hasher.hash(pw)


def verify_password(stored: str, pw: str) -> tuple[bool, str | None]:
    """(ok, a new hash when a rehash is due). One Argon2 slot covers the check and any rehash."""
    with _argon2_slots:
        try:
            _hasher.verify(stored, pw)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, None
        try:
            return True, (_hasher.hash(pw) if _hasher.check_needs_rehash(stored) else None)
        except InvalidHashError:
            return True, None


# Checked for an unknown username, so both paths cost one Argon2 verification. Made at import, so the first
# unknown-name login is not slower than the rest.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


def _dummy_hash() -> str:
    return _DUMMY_HASH


# --- the session cookie -------------------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionSigner:
    """Signs session cookies: `<token>.<mac>`; the database keeps only the token's SHA-256."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    @classmethod
    def derive(cls, session_secret: str) -> "SessionSigner":
        """key = HMAC-SHA256(session_secret, "trader.web.session.v1")."""
        return cls(hmac.new(session_secret.encode(), b"trader.web.session.v1", hashlib.sha256).digest())

    def _mac(self, token: str) -> str:
        return _b64(hmac.new(self._secret, token.encode(), hashlib.sha256).digest())

    def issue(self) -> tuple[str, str]:
        """(the cookie value `<token>.<mac>`, the SHA-256 hex of the token)."""
        token = secrets.token_urlsafe(32)
        return f"{token}.{self._mac(token)}", _sha256(token)

    def parse(self, cookie: str) -> str | None:
        """The token's SHA-256 hex, or None when the cookie is malformed or its MAC is wrong."""
        if not cookie or len(cookie) > MAX_COOKIE_CHARS or not cookie.isascii():
            return None
        parts = cookie.split(".")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        token, mac = parts
        if not hmac.compare_digest(self._mac(token).encode(), mac.encode()):
            return None
        return _sha256(token)


def _signer(services: ApiServices) -> SessionSigner:
    return SessionSigner.derive(services.core.env.session_secret.get_secret_value())


# --- the login rate limiter ---------------------------------------------------------------------------------


class LoginLimiter:
    """`per_minute()` login attempts per client IP per sliding minute (in memory: one uvicorn worker).
    Only allowed attempts are counted, so a refused flood does not extend its own wait."""

    WINDOW = 60.0
    MAX_IPS = 10_000  # past this, idle IPs are dropped

    def __init__(self, clock: Clock, per_minute: Callable[[], int]) -> None:
        self._clock = clock
        self._per_minute = per_minute
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> float | None:
        """None when allowed, else the seconds to wait."""
        limit = self._per_minute()
        now = self._clock.now().timestamp()
        with self._lock:
            if len(self._attempts) > self.MAX_IPS:
                self._prune(now)
            q = self._attempts.setdefault(ip, deque())
            while q and q[0] <= now - self.WINDOW:
                q.popleft()
            if len(q) >= limit:
                return max(q[0] + self.WINDOW - now, 0.001)
            q.append(now)
            return None

    def _prune(self, now: float) -> None:
        for ip in [ip for ip, q in self._attempts.items() if not q or q[-1] <= now - self.WINDOW]:
            del self._attempts[ip]


_limiter_lock = threading.Lock()


def login_limiter(request: Request, services: ApiServices) -> LoginLimiter:
    """The app's one limiter (kept on `app.state`, created on first use)."""
    with _limiter_lock:
        limiter = getattr(request.app.state, "login_limiter", None)
        if not isinstance(limiter, LoginLimiter):
            limiter = LoginLimiter(services.core.clock, lambda: _settings(services).web_login_rate_per_minute)
            request.app.state.login_limiter = limiter
        return limiter


class GuessLimiter:
    """At most `MAX` distinct wrong passwords or codes per session per `WINDOW` on the signed-in routes
    (`PUT /auth/password`, `POST /auth/totp/setup|confirm|disable`); past that, 429 until the oldest one ages
    out. Repeating a wrong guess already counted is not a new guess (it teaches nothing), but a code is a new
    guess in each 30-second TOTP step. Separate from the login lockout: a signed-in user's mistakes never lock
    the login (orchestrator ruling). In memory (one uvicorn worker); guesses are kept only as HMACs under a
    random per-process key."""

    MAX = 5
    WINDOW = timedelta(minutes=15)
    MAX_SESSIONS = 10_000  # past this, idle sessions are dropped

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._guesses: dict[int, deque[tuple[datetime, str]]] = {}
        self._lock = threading.Lock()

    def _live(self, session_id: int, now: datetime) -> deque[tuple[datetime, str]]:
        q = self._guesses.setdefault(session_id, deque())
        while q and q[0][0] <= now - self.WINDOW:
            q.popleft()
        return q

    def wait(self, session_id: int) -> float | None:
        """None while the session has guesses left, else the seconds until the oldest one ages out."""
        now = self._clock.now()
        with self._lock:
            q = self._live(session_id, now)
            if len(q) < self.MAX:
                return None
            return max((q[0][0] + self.WINDOW - now).total_seconds(), 0.001)

    def record(self, session_id: int, kind: Literal["password", "code"], value: str) -> None:
        """Counts one wrong guess (once per distinct value; a code once per TOTP step)."""
        now = self._clock.now()
        if kind == "code":
            value = f"{value}@{int(now.timestamp()) // 30}"
        digest = hmac.new(self._key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
        with self._lock:
            if len(self._guesses) > self.MAX_SESSIONS:
                cutoff = now - self.WINDOW
                for sid in [k for k, q in self._guesses.items() if not q or q[-1][0] <= cutoff]:
                    del self._guesses[sid]
            q = self._live(session_id, now)
            if all(d != digest for _, d in q):
                q.append((now, digest))


def guess_limiter(request: Request, services: ApiServices) -> GuessLimiter:
    """The app's one `GuessLimiter` (kept on `app.state`, created on first use)."""
    with _limiter_lock:
        limiter = getattr(request.app.state, "guess_limiter", None)
        if not isinstance(limiter, GuessLimiter):
            limiter = GuessLimiter(services.core.clock)
            request.app.state.guess_limiter = limiter
        return limiter


def client_ip(request: Request) -> str | None:
    """The client address uvicorn resolved: behind a trusted proxy (`TRADER_FORWARDED_ALLOW_IPS`, see
    `trader.api.__main__`), the rightmost `X-Forwarded-For` hop outside the trusted networks, i.e. the address
    NPM appended; otherwise the TCP peer."""
    return request.client.host if request.client else None


# --- origin -------------------------------------------------------------------------------------------------


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    if port is None or (scheme, port) in (("http", 80), ("https", 443)):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def check_origin(request: Request) -> None:
    """An `Origin` header, when present, must be `PUBLIC_BASE_URL`'s origin or the request's own, else 403."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    services = request.app.state.services
    allowed = {
        _origin_of(services.core.env.public_base_url),
        _origin_of(f"{request.url.scheme}://{request.url.netloc}"),
    } - {None}
    if _origin_of(origin) not in allowed:
        log.warning("auth.origin_refused", method=request.method, path=request.url.path)
        raise ApiError(403, "forbidden", "Cross-site request refused")


# --- TOTP ---------------------------------------------------------------------------------------------------


def _totp_match(secret: str, code: str | None, now: datetime) -> int | None:
    """The latest step within ±`TOTP_WINDOW` of now whose code this is, else None."""
    if not code or not re.fullmatch(r"[0-9]{6}", code):
        return None
    totp = pyotp.TOTP(secret)
    current = totp.timecode(now)
    matched = None
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if hmac.compare_digest(totp.generate_otp(step).encode(), code.encode()):
            matched = step
    return matched


def _totp_step(secret: str, code: str | None, now: datetime, last_step: int | None) -> int | None:
    """The step a code matches (±1 step, later than `last_step`), else None."""
    matched = _totp_match(secret, code, now)
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def _decrypt(services: ApiServices, value: str | None) -> str | None:
    return services.core.crypto.decrypt(value)


# --- helpers ------------------------------------------------------------------------------------------------


def _audit(s: Session, now: datetime, who: str, action: str, after: Mapping[str, Any] | None = None) -> None:
    s.add(m.AuditLog(ts=now, actor=who, action=action, before=None, after=dict(after) if after else None))


def _unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", "Please log in", headers={"Set-Cookie": CLEAR_COOKIE})


def _too_many(what: str, wait_seconds: float) -> ApiError:
    seconds = max(math.ceil(wait_seconds), 1)
    minutes = math.ceil(seconds / 60)
    unit = "minute" if minutes == 1 else "minutes"
    return ApiError(
        429,
        "too_many_requests",
        f"Too many {what}. Try again in {minutes} {unit}.",
        headers={"Retry-After": str(seconds)},
    )


def _locked_error(locked_until: datetime, now: datetime) -> ApiError:
    return _too_many("failed attempts", (locked_until - now).total_seconds())


def _check_guesses(guesses: GuessLimiter | None, user: AuthUser) -> None:
    """429 when this session has used up its wrong passwords and codes (see `GuessLimiter`)."""
    wait = guesses.wait(user.session_id) if guesses is not None else None
    if wait is not None:
        log.warning("auth.guess_limit", username=user.username)
        raise _too_many("wrong passwords or codes", wait)


def _wrong_password(guesses: GuessLimiter | None, user: AuthUser, password: str) -> ApiError:
    if guesses is not None:
        guesses.record(user.session_id, "password", password)
    return _bad_password()


def _wrong_code(guesses: GuessLimiter | None, user: AuthUser, code: str | None) -> ApiError:
    if guesses is not None and code:  # a missing code is not a guess
        guesses.record(user.session_id, "code", code)
    return _bad_code()


def _load_user(s: Session, user: AuthUser, *, lock: bool = False) -> m.User:
    stmt = select(m.User).where(m.User.id == user.id)
    row = s.execute(stmt.with_for_update() if lock else stmt).scalar_one_or_none()
    if row is None:
        raise _unauthorized()
    return row


def _session_out(row: m.User, web_session: m.WebSession) -> SessionOut:
    return SessionOut(
        user=UserOut(username=row.username, totp_enabled=row.totp_secret_enc is not None),
        csrf_token=web_session.csrf_token,
        expires_at=web_session.expires_at,
    )


# A signed-in user's wrong password or code is a 403, never a 401: the web app treats every 401 outside
# login as a lost session and would sign Stephen out mid-form (orchestrator ruling, 2026-09-27).
def _bad_password() -> ApiError:
    return ApiError(403, "bad_credentials", "The password is not correct")


def _bad_code() -> ApiError:
    return ApiError(403, "bad_credentials", "The code is not correct")


CodeResult = Literal["ok", "wrong", "used"]


def _code_result(
    services: ApiServices, row: m.User, secret_enc: str | None, code: str | None, now: datetime
) -> CodeResult:
    """Checks a code against a stored secret and records its step (no replay): `used` when it is a valid code
    of a step at or before the last one accepted."""
    secret = _decrypt(services, secret_enc)
    if secret is None:
        return "wrong"
    matched = _totp_match(secret, code, now)
    if matched is None:
        return "wrong"
    if row.totp_last_step is not None and matched <= row.totp_last_step:
        return "used"
    row.totp_last_step = matched
    return "ok"


def _check_code(
    services: ApiServices, row: m.User, secret_enc: str | None, code: str | None, now: datetime
) -> bool:
    return _code_result(services, row, secret_enc, code, now) == "ok"


# --- login and sessions -------------------------------------------------------------------------------------


def login(
    services: ApiServices,
    username: str,
    password: str,
    totp: str | None,
    ip: str | None,
    user_agent: str | None,
) -> tuple[SessionOut, str]:
    """(the session, the cookie value). Raises ApiError 401 / 429."""
    core = services.core
    settings = _settings(services)
    now = core.clock.now()
    name = username.strip().lower()
    error: ApiError | None = None
    result: tuple[SessionOut, str] | None = None
    with session_scope(core.factory) as s:
        row = None
        # Every user matches USERNAME_PATTERN (`ensure_admin`), so any other name (a NUL byte, which
        # PostgreSQL text refuses, a homoglyph, SQL-ish text) is unknown and never reaches the database.
        if re.fullmatch(USERNAME_PATTERN, name):
            row = s.execute(
                select(m.User).where(m.User.username == name).with_for_update()
            ).scalar_one_or_none()
        if row is None:
            verify_password(_dummy_hash(), password)
            _audit(s, now, "web:?", "auth.login_failed", {"ip": ip, "reason": "unknown_user"})
            error = ApiError(401, "unauthorized", INVALID_LOGIN)
        elif row.locked_until is not None and now < row.locked_until:
            _audit(s, now, f"web:{row.username}", "auth.login_failed", {"ip": ip, "reason": "locked"})
            error = _locked_error(row.locked_until, now)
        else:
            if row.locked_until is not None:  # the lock has run out: count afresh
                row.locked_until, row.failed_logins = None, 0
            ok, new_hash = verify_password(row.password_hash, password)
            reason = None if ok else "password"
            if ok and row.totp_secret_enc is not None:
                if not totp:
                    reason = "totp_missing"
                else:
                    code = _code_result(services, row, row.totp_secret_enc, totp, now)
                    reason = {"ok": None, "wrong": "totp", "used": "totp_used"}[code]
            who = f"web:{row.username}"
            if reason is not None:
                row.failed_logins += 1
                row.updated_at = now
                _audit(s, now, who, "auth.login_failed", {"ip": ip, "reason": reason})
                if row.failed_logins >= settings.web_login_max_failures:
                    row.locked_until = now + timedelta(minutes=settings.web_lockout_minutes)
                    _audit(s, now, who, "auth.lockout", {"ip": ip, "until": row.locked_until.isoformat()})
                    log.warning("auth.lockout", username=row.username, minutes=settings.web_lockout_minutes)
                # Only someone with the right password learns the code was a used one (e.g. the code just
                # used to turn TOTP on): it still counts as a failure, like any other refused code.
                error = ApiError(401, "unauthorized", USED_CODE if reason == "totp_used" else INVALID_LOGIN)
            else:
                if new_hash is not None:
                    row.password_hash = new_hash
                row.failed_logins, row.locked_until = 0, None
                row.last_login_at = row.updated_at = now
                cookie, token_hash = _signer(services).issue()
                web_session = m.WebSession(
                    user_id=row.id,
                    token_hash=token_hash,
                    csrf_token=secrets.token_urlsafe(32),
                    created_at=now,
                    last_seen_at=now,
                    expires_at=now + timedelta(days=settings.web_session_max_days),
                    ip=ip[:45] if ip else None,
                    user_agent=user_agent[:200] if user_agent else None,
                )
                s.add(web_session)
                _audit(s, now, who, "auth.login", {"ip": ip})
                result = (_session_out(row, web_session), cookie)
    if error is not None:
        raise error
    assert result is not None
    log.info("auth.login", username=result[0].user.username)
    return result


def cookie_max_age(services: ApiServices) -> int:
    return _settings(services).web_session_max_days * 86400


def logout(services: ApiServices, user: AuthUser) -> None:
    core = services.core
    now = core.clock.now()
    with session_scope(core.factory) as s:
        s.execute(
            update(m.WebSession)
            .where(m.WebSession.id == user.session_id, m.WebSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        _audit(s, now, actor(user), "auth.logout")


def authenticate(request: Request, services: ApiServices) -> AuthUser:
    """The user of a valid session cookie, else ApiError 401 (and the response clears the cookie)."""
    cookie = request.cookies.get(COOKIE_NAME)
    token_hash = _signer(services).parse(cookie) if cookie else None
    if token_hash is None:
        raise _unauthorized()
    core = services.core
    now = core.clock.now()
    idle = timedelta(hours=_settings(services).web_session_idle_hours)
    with session_scope(core.factory) as s:
        found = s.execute(
            select(m.WebSession, m.User)
            .join(m.User, m.User.id == m.WebSession.user_id)
            .where(m.WebSession.token_hash == token_hash)
        ).first()
        if found is None:
            raise _unauthorized()
        web_session, user = found
        if (
            web_session.revoked_at is not None
            or now >= web_session.expires_at
            or now - web_session.last_seen_at >= idle
        ):
            raise _unauthorized()
        if now - web_session.last_seen_at >= LAST_SEEN_EVERY:
            web_session.last_seen_at = now
        return AuthUser(
            id=user.id, username=user.username, session_id=web_session.id, csrf_token=web_session.csrf_token
        )


def check_csrf(request: Request, user: AuthUser) -> AuthUser:
    """On POST/PUT/DELETE: `X-CSRF-Token` equals the session's token and any `Origin` is ours, else 403."""
    if request.method.upper() not in UNSAFE_METHODS:
        return user
    sent = request.headers.get(CSRF_HEADER)
    if not sent or not hmac.compare_digest(sent.encode(), user.csrf_token.encode()):
        raise ApiError(403, "csrf", "Missing or invalid CSRF token")
    check_origin(request)
    return user


def session_info(services: ApiServices, user: AuthUser) -> SessionOut:
    """The signed-in session (`GET /api/auth/me`)."""
    with services.core.factory() as s:
        row = _load_user(s, user)
        web_session = s.get(m.WebSession, user.session_id)
        if web_session is None:
            raise _unauthorized()
        return _session_out(row, web_session)


# --- password and TOTP changes ------------------------------------------------------------------------------


def _validation(field: str, msg: str) -> ApiError:
    return ApiError(422, "validation", "Invalid request", [{"loc": ["body", field], "msg": msg}])


# The four signed-in checks below take the app's `GuessLimiter` (the router passes `guess_limiter(...)`): the
# check and the count run under the user's row lock, so concurrent guesses cannot overrun the limit.


def change_password(
    services: ApiServices,
    user: AuthUser,
    current: str,
    new: str,
    totp: str | None,
    *,
    guesses: GuessLimiter | None = None,
) -> None:
    if len(new) < MIN_PASSWORD_CHARS:
        raise _validation("new_password", f"Use at least {MIN_PASSWORD_CHARS} characters")
    if new == current:
        raise _validation("new_password", "The new password must differ from the current one")
    core = services.core
    now = core.clock.now()
    error: ApiError | None = None
    with session_scope(core.factory) as s:
        row = _load_user(s, user, lock=True)
        _check_guesses(guesses, user)
        if not verify_password(row.password_hash, current)[0]:
            error = _wrong_password(guesses, user, current)
        elif row.totp_secret_enc is not None and not _check_code(
            services, row, row.totp_secret_enc, totp, now
        ):
            error = _wrong_code(guesses, user, totp)
        else:
            row.password_hash = hash_password(new)
            row.password_changed_at = row.updated_at = now
            revoked = s.execute(
                update(m.WebSession)
                .where(
                    m.WebSession.user_id == row.id,
                    m.WebSession.id != user.session_id,
                    m.WebSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
                .returning(m.WebSession.id)
            ).all()
            _audit(s, now, actor(user), "auth.password_change", {"sessions_revoked": len(revoked)})
    if error is not None:
        raise error
    log.info("auth.password_change", username=user.username)


def totp_setup(
    services: ApiServices, user: AuthUser, password: str, *, guesses: GuessLimiter | None = None
) -> TotpSetupOut:
    core = services.core
    now = core.clock.now()
    with session_scope(core.factory) as s:
        row = _load_user(s, user, lock=True)
        _check_guesses(guesses, user)
        if not verify_password(row.password_hash, password)[0]:
            raise _wrong_password(guesses, user, password)
        if row.totp_secret_enc is not None:
            raise ApiError(409, "conflict", "Two-step codes are already on")
        secret = pyotp.random_base32()
        row.totp_pending_enc = core.crypto.encrypt(secret)
        row.updated_at = now
        username = row.username
    label = quote(f"Trader ({core.env.app_env}):{username}", safe="():")
    uri = f"otpauth://totp/{label}?secret={secret}&issuer=Trader"
    return TotpSetupOut(secret=secret, otpauth_uri=uri)


def totp_confirm(
    services: ApiServices, user: AuthUser, code: str, *, guesses: GuessLimiter | None = None
) -> None:
    core = services.core
    now = core.clock.now()
    error: ApiError | None = None
    with session_scope(core.factory) as s:
        row = _load_user(s, user, lock=True)
        _check_guesses(guesses, user)
        if row.totp_pending_enc is None:
            error = ApiError(409, "conflict", "Start the two-step setup first")
        elif not _check_code(services, row, row.totp_pending_enc, code, now):
            error = _wrong_code(guesses, user, code)
        else:
            row.totp_secret_enc, row.totp_pending_enc = row.totp_pending_enc, None
            row.updated_at = now
            _audit(s, now, actor(user), "auth.totp_enable")
    if error is not None:
        raise error


def totp_disable(
    services: ApiServices, user: AuthUser, password: str, code: str, *, guesses: GuessLimiter | None = None
) -> None:
    core = services.core
    now = core.clock.now()
    error: ApiError | None = None
    with session_scope(core.factory) as s:
        row = _load_user(s, user, lock=True)
        _check_guesses(guesses, user)
        if not verify_password(row.password_hash, password)[0]:
            error = _wrong_password(guesses, user, password)
        elif row.totp_secret_enc is None:
            error = ApiError(409, "conflict", "Two-step codes are off")
        elif not _check_code(services, row, row.totp_secret_enc, code, now):
            error = _wrong_code(guesses, user, code)
        else:
            row.totp_secret_enc = row.totp_pending_enc = None
            row.totp_last_step = None
            row.updated_at = now
            _audit(s, now, actor(user), "auth.totp_disable")
    if error is not None:
        raise error


# --- the first-start admin ----------------------------------------------------------------------------------


def _reject(factory: sessionmaker[Session], clock: Clock, reason: str) -> AdminResult:
    log.critical("auth.admin_rejected", reason=reason)
    try:
        with session_scope(factory) as s:
            log_event(s, clock, "critical", "auth", f"Admin user not created: {reason}")
    except Exception as exc:  # the log line above is the record that matters
        log.warning("auth.admin_rejected_event_failed", error_type=type(exc).__name__)
    return "rejected"


def ensure_admin(
    factory: sessionmaker[Session], clock: Clock, username: str | None, password: SecretStr | None
) -> AdminResult:
    """Creates the first user from env (`trader create-admin`, T18) only when the table is empty."""
    if not username or password is None or not password.get_secret_value():
        return "not_configured"
    with factory() as s:
        if s.execute(select(func.count()).select_from(m.User)).scalar_one() > 0:
            return "exists"
    if not re.fullmatch(USERNAME_PATTERN, username):
        return _reject(
            factory, clock, "ADMIN_USERNAME must be 3-46 lowercase letters, digits, '_', '.' or '-'"
        )
    pw = password.get_secret_value()
    if len(pw) < MIN_PASSWORD_CHARS:
        return _reject(
            factory, clock, f"ADMIN_PASSWORD_INITIAL is shorter than {MIN_PASSWORD_CHARS} characters"
        )
    now = clock.now()
    try:
        with session_scope(factory) as s:
            # One creator at a time: a second concurrent start waits here, then sees the row.
            s.execute(select(func.pg_advisory_xact_lock(0x7472_6164_6D69)))  # "tradmi"
            if s.execute(select(func.count()).select_from(m.User)).scalar_one() > 0:
                return "exists"
            s.add(
                m.User(
                    username=username,
                    password_hash=hash_password(pw),
                    failed_logins=0,
                    created_at=now,
                    updated_at=now,
                    password_changed_at=now,
                )
            )
            _audit(s, now, "system", "auth.admin_created", {"username": username})
    except IntegrityError:
        return "exists"
    log.info("auth.admin_created", username=username)
    return "created"


# --- a password reset from the command line -----------------------------------------------------------------


class ResetRefused(ValueError):
    """`reset_password` changed nothing. The message is safe to print (never a password)."""


def reset_password(
    factory: sessionmaker[Session],
    clock: Clock,
    new_password: str,
    *,
    username: str | None = None,
    who: str = "cli",
) -> tuple[str, int]:
    """Set a user's password (`trader user-password`, P4-T18): the only user, or `username` when there are
    several. Signs out EVERY session of that user (a web password change keeps the current one, but here
    there is none), clears the login lockout, and audits `auth.password_reset` (never the password).
    Returns (username, sessions signed out). Raises ResetRefused when nothing was changed."""
    if len(new_password) < MIN_PASSWORD_CHARS:
        raise ResetRefused(f"Use at least {MIN_PASSWORD_CHARS} characters")
    now = clock.now()
    with session_scope(factory) as s:
        query = select(m.User).order_by(m.User.id).with_for_update()
        if username is not None:
            query = query.where(m.User.username == username.strip().lower())
        rows = list(s.execute(query).scalars())
        if not rows:
            raise ResetRefused(
                "no user yet: run trader create-admin" if username is None else "no user with that name"
            )
        if len(rows) > 1:
            raise ResetRefused("more than one user: pass --username")
        row = rows[0]
        row.password_hash = hash_password(new_password)
        row.password_changed_at = row.updated_at = now
        row.failed_logins = 0
        row.locked_until = None
        revoked = s.execute(
            update(m.WebSession)
            .where(m.WebSession.user_id == row.id, m.WebSession.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(m.WebSession.id)
        ).all()
        name = row.username
        _audit(s, now, who, "auth.password_reset", {"username": name, "sessions_revoked": len(revoked)})
    log.info("auth.password_reset", username=name, sessions_revoked=len(revoked))
    return name, len(revoked)
