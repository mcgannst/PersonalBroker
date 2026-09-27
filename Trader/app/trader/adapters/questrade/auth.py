"""Questrade token owner (SPEC §4.1), ported from FinanceTracker backend/app/core/questrade.py.

Exchanging a refresh token returns a NEW one and invalidates the old. api, worker and cron all
share this token, so every exchange happens under a row lock, re-checks freshness under the lock,
and commits the rotated token before anyone uses the access token.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.crypto import Crypto
from trader.db.models import ApiCredential
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
    def force_refresh(self) -> AccessToken: ...


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
        now = self._clock.now()
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER) or ApiCredential(provider=PROVIDER)
            row.refresh_token_enc = self._crypto.encrypt(refresh_token.strip())
            row.access_token_enc = None
            row.api_server = None
            row.expires_at = None
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

    def force_refresh(self) -> AccessToken:
        return self._refresh(forced=True)

    def keep_alive(self, min_age: timedelta = timedelta(hours=1)) -> AccessToken:
        """Daily job: extend the refresh-token chain unless it was extended recently.

        The chain's age is what matters here, not the access token's: min_age (1 h) is longer
        than an access token lives (30 min), so requiring a fresh access token would make every
        call exchange. The returned token may therefore be expired; callers that need a usable
        access token call access().
        """
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER)
            if (
                row is not None
                and row.last_refresh_at is not None
                and self._crypto.decrypt(row.access_token_enc)
                and row.api_server
                and self._clock.now() - row.last_refresh_at < min_age
            ):
                return self._token(row)
        return self._refresh(forced=True, cooldown=timedelta(0))

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

    def _refresh(self, forced: bool, cooldown: timedelta = FORCED_REFRESH_COOLDOWN) -> AccessToken:
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
            if (
                forced
                and row.last_refresh_at is not None
                and now - row.last_refresh_at < cooldown
                and self._is_fresh(row)
            ):
                return self._token(row)
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
                raise QuestradeAuthError(f"Could not reach Questrade: {exc}") from exc
            if resp.status_code != 200:
                detail = (
                    "the refresh token has already been used or has expired"
                    if resp.status_code == 400
                    else f"HTTP {resp.status_code}"
                )
                row.last_error = f"Token refresh failed ({detail}). Generate a new manual token in Questrade."
                row.updated_at = now
                s.commit()
                raise QuestradeAuthError(row.last_error)
            data = resp.json()
            if not data.get("refresh_token") or not data.get("access_token"):
                raise QuestradeAuthError("Questrade's response was missing a token.")
            row.refresh_token_enc = self._crypto.encrypt(data["refresh_token"])
            row.access_token_enc = self._crypto.encrypt(data["access_token"])
            row.api_server = data.get("api_server")
            row.expires_at = now + timedelta(seconds=int(data.get("expires_in", 1800)))
            row.last_refresh_at = now
            row.last_error = None
            row.updated_at = now
            try:
                s.commit()
            except Exception as exc:
                s.rollback()
                log.critical("questrade_rotated_token_not_saved", error=str(exc))
                raise QuestradeAuthError(
                    "The token was refreshed but couldn't be saved, so the connection is broken. "
                    "Paste a new manual token."
                ) from exc
            return self._token(row)
