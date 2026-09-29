"""Questrade token owner (SPEC §4.1), ported from FinanceTracker backend/app/core/questrade.py.

Exchanging a refresh token returns a NEW one and invalidates the old. api, worker and cron all
share this token, so every exchange happens under a row lock, re-checks freshness under the lock,
and commits the rotated token before anyone uses the access token.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.crypto import Crypto
from trader.db.models import ApiCredential
from trader.logging_setup import current_process
from trader.market.clock import Clock

log = structlog.get_logger("questrade.auth")

TOKEN_URL = "https://login.questrade.com/oauth2/token"  # noqa: S105 (a URL, not a secret)
TIMEOUT = 20.0
EXPIRY_SKEW = timedelta(seconds=120)
FORCED_REFRESH_COOLDOWN = timedelta(seconds=90)
FAILED_REFRESH_COOLDOWN = timedelta(seconds=60)
PROVIDER = "questrade"


class QuestradeAuthError(Exception):
    """The connection is unusable; the message is fit to show Stephen."""


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    api_base: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenHealth:
    seeded: bool
    expires_at: datetime | None
    last_refresh_at: datetime | None
    last_error: str | None


class TokenSource(Protocol):
    def access(self) -> AccessToken: ...

    # `rejected`: the access token string Questrade just refused (FIX-401), so it is never handed back.
    def force_refresh(self, rejected: str | None = None) -> AccessToken: ...


def api_base(api_server: str | None) -> str:
    base = (api_server or "").rstrip("/")
    if not base:
        raise QuestradeAuthError("Questrade did not return an API server URL.")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/"


class QuestradeAuth:
    def __init__(
        self, factory: sessionmaker[Session], crypto: Crypto, clock: Clock, http: httpx.Client | None = None
    ) -> None:
        self._factory = factory
        self._crypto = crypto
        self._clock = clock
        self._http = http or httpx.Client(timeout=TIMEOUT)

    # --- public -------------------------------------------------------------------
    def seed(self, refresh_token: str) -> None:
        """Start a new chain from a manual token. A blank token is rejected, never stored, so it
        can't overwrite (and so destroy) a live chain."""
        token = refresh_token.strip()
        if not token:
            raise QuestradeAuthError("The refresh token is empty. Paste a manual token from Questrade.")
        now = self._clock.now()
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER) or ApiCredential(provider=PROVIDER)
            row.refresh_token_enc = self._crypto.encrypt(token)
            row.access_token_enc = None
            row.api_server = None
            row.expires_at = None
            row.last_refresh_at = None
            row.last_error = None
            row.updated_at = now
            s.merge(row)
            s.commit()

    def access(self) -> AccessToken:
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER)
            if row is not None and self._is_fresh(row):
                return self._token(row)
        return self._refresh(forced=False)

    def force_refresh(self, rejected: str | None = None) -> AccessToken:
        """A new access token after a 401. With `rejected` (the token string Questrade refused): a stored
        token that differs from it is returned as it is (another request or process already replaced it);
        one equal to it is exchanged, even inside FORCED_REFRESH_COOLDOWN (FIX-401: the cooldown used to
        hand the rejected token straight back). The row lock makes that at most one exchange per rejected
        token. Without `rejected` the cooldown applies as before."""
        return self._refresh(forced=True, rejected=rejected)

    def keep_alive(self, min_age: timedelta = timedelta(hours=1)) -> AccessToken:
        """Daily job: extend the refresh-token chain unless it was extended recently.

        The chain's age is what matters here, not the access token's: min_age (1 h) is longer
        than an access token lives (30 min), so requiring a fresh access token would make every
        call exchange. The returned token may therefore be expired; callers that need a usable
        access token call access(). A chain with a recorded error never counts as recently
        extended, so a dead chain never reports ok.
        """
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER)
            if (
                row is not None
                and not row.last_error
                and row.last_refresh_at is not None
                and self._crypto.decrypt(row.access_token_enc)
                and row.api_server
                and self._clock.now() - row.last_refresh_at < min_age
            ):
                return self._token(row)
        return self._refresh(forced=True, cooldown=timedelta(0), kind="keep_alive")

    def health(self) -> TokenHealth:
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER)
            if row is None:
                return TokenHealth(False, None, None, None)
            return TokenHealth(
                bool(row.refresh_token_enc), row.expires_at, row.last_refresh_at, row.last_error
            )

    # --- internals ------------------------------------------------------------------
    def _is_fresh(self, row: ApiCredential) -> bool:
        return bool(
            self._crypto.decrypt(row.access_token_enc)
            and row.api_server
            and row.expires_at is not None
            and row.expires_at - EXPIRY_SKEW > self._clock.now()
        )

    def _token(self, row: ApiCredential) -> AccessToken:
        token = self._crypto.decrypt(row.access_token_enc)
        if not token or row.expires_at is None:
            raise QuestradeAuthError("No usable access token stored.")
        return AccessToken(token, api_base(row.api_server), row.expires_at)

    def _refresh(
        self,
        forced: bool,
        cooldown: timedelta = FORCED_REFRESH_COOLDOWN,
        *,
        rejected: str | None = None,
        kind: str | None = None,
    ) -> AccessToken:
        now = self._clock.now()
        with self._factory() as s:
            # populate_existing is load-bearing: without it a process that waited on the lock
            # would wake up holding the token the first one just spent.
            row = s.execute(
                select(ApiCredential)
                .where(ApiCredential.provider == PROVIDER)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if row is None:
                raise QuestradeAuthError("Questrade isn't set up. Paste a refresh token in Settings.")
            if not forced and self._is_fresh(row):
                return self._token(row)
            stored_rejected = rejected is not None and self._crypto.decrypt(row.access_token_enc) == rejected
            if forced and rejected is not None and not stored_rejected and self._is_fresh(row):
                return self._token(row)  # already replaced since that token was used: never exchange twice
            in_cooldown = (
                forced
                and row.last_refresh_at is not None
                and now - row.last_refresh_at < cooldown
                and self._is_fresh(row)
            )
            if in_cooldown and not stored_rejected:
                return self._token(row)
            if kind is None:
                kind = ("cooldown" if in_cooldown else "forced") if forced else "initial"
            if row.last_error and row.updated_at and now - row.updated_at < FAILED_REFRESH_COOLDOWN:
                raise QuestradeAuthError(row.last_error)
            refresh = self._crypto.decrypt(row.refresh_token_enc)
            if not refresh:
                raise QuestradeAuthError("No usable refresh token stored. Paste a new one from Questrade.")
            try:
                resp = self._http.get(
                    TOKEN_URL, params={"grant_type": "refresh_token", "refresh_token": refresh}
                )
            except httpx.HTTPError as exc:
                # The exception text can include the request URL, and the refresh token is in
                # its query string, so only the exception type is reported.
                raise self._fail(
                    s,
                    row,
                    now,
                    f"Could not reach Questrade ({type(exc).__name__}). The chain is probably "
                    "still valid; the refresh will be retried.",
                ) from None
            if resp.status_code != 200:
                raise self._fail(s, row, now, _status_error(resp.status_code))
            parsed = _parse_token_response(resp)
            if isinstance(parsed, str):
                raise self._fail(
                    s,
                    row,
                    now,
                    f"Questrade returned an unusable token response ({parsed}). The old refresh "
                    "token may already be spent. If the next refresh fails, generate a new manual "
                    "token in Questrade.",
                )
            new_refresh, new_access, server, expires_in = parsed
            row.refresh_token_enc = self._crypto.encrypt(new_refresh)
            row.access_token_enc = self._crypto.encrypt(new_access)
            row.api_server = server
            row.expires_at = now + timedelta(seconds=expires_in)
            row.last_refresh_at = now
            row.last_error = None
            row.updated_at = now
            try:
                s.commit()
            except Exception as exc:
                s.rollback()
                # Never str(exc): a DB error's text can carry the statement's parameters,
                # which here are the encrypted new tokens.
                log.critical("questrade_rotated_token_not_saved", error=type(exc).__name__)
                raise QuestradeAuthError(
                    "The token was refreshed but couldn't be saved, so the connection is broken. "
                    "Paste a new manual token."
                ) from None
            # FIX-401: one line per real exchange (which process, why), never a token.
            log.info(
                "questrade.token_exchanged",
                process=current_process(),
                pid=os.getpid(),
                kind=kind,
                expires_in=expires_in,
            )
            return self._token(row)

    @staticmethod
    def _fail(s: Session, row: ApiCredential, now: datetime, message: str) -> QuestradeAuthError:
        """Record a failed refresh so FAILED_REFRESH_COOLDOWN throttles retries and health() shows it."""
        row.last_error = message
        row.updated_at = now
        s.commit()
        return QuestradeAuthError(message)


def _status_error(status: int) -> str:
    if status == 400:
        return (
            "Token refresh failed (the refresh token has already been used or has expired). "
            "Generate a new manual token in Questrade."
        )
    if 500 <= status < 600:
        return (
            f"Questrade login service error (HTTP {status}); the chain is probably still valid. "
            "The refresh will be retried."
        )
    return f"Token refresh failed (HTTP {status}). Generate a new manual token in Questrade."


def _parse_token_response(resp: httpx.Response) -> tuple[str, str, str | None, int] | str:
    """(refresh_token, access_token, api_server, expires_in), or a short reason it is unusable.
    The reason never includes the body, which may contain tokens."""
    try:
        data = resp.json()
    except ValueError:
        return "body is not JSON"
    if not isinstance(data, dict):
        return "body is not a JSON object"
    refresh, access = data.get("refresh_token"), data.get("access_token")
    if not isinstance(refresh, str) or not refresh:
        return "missing refresh_token"
    if not isinstance(access, str) or not access:
        return "missing access_token"
    raw_expires = data.get("expires_in", 1800)
    if isinstance(raw_expires, bool):
        return "expires_in is not an integer"
    try:
        expires_in = int(raw_expires)
    except (TypeError, ValueError):
        return "expires_in is not an integer"
    if isinstance(raw_expires, float) and raw_expires != expires_in:
        return "expires_in is not an integer"
    server = data.get("api_server")
    return refresh, access, server if isinstance(server, str) else None, expires_in
