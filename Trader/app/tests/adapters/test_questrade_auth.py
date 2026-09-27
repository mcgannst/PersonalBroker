import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import TOKEN_URL, QuestradeAuth, QuestradeAuthError
from trader.crypto import Crypto
from trader.db.models import ApiCredential
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
CRYPTO = Crypto(Fernet.generate_key().decode())


def ok(n: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": f"access-{n}",
            "refresh_token": f"refresh-{n}",
            "token_type": "Bearer",
            "expires_in": 1800,
            "api_server": "https://api05.iq.questrade.com/",
        },
    )


def make(factory: sessionmaker[Session], clock: FixedClock) -> QuestradeAuth:
    auth = QuestradeAuth(factory, CRYPTO, clock)
    auth.seed("refresh-0")
    return auth


def stored_refresh(factory: sessionmaker[Session]) -> str | None:
    with factory() as s:
        row = s.get(ApiCredential, "questrade")
        assert row is not None
        return CRYPTO.decrypt(row.refresh_token_enc)


@respx.mock
def test_first_access_exchanges_and_stores_rotated_token(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(return_value=ok(1))
    auth = make(db_factory, FixedClock(T0))
    tok = auth.access()
    assert tok.token == "access-1"
    assert tok.api_base == "https://api05.iq.questrade.com/v1/"
    assert route.calls.last.request.url.params["refresh_token"] == "refresh-0"
    assert stored_refresh(db_factory) == "refresh-1"


@respx.mock
def test_fresh_token_is_reused(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    clock.advance(timedelta(minutes=20))
    assert auth.access().token == "access-1"
    assert route.call_count == 1


@respx.mock
def test_refreshes_inside_expiry_skew(db_factory: sessionmaker[Session]) -> None:
    respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    clock.advance(timedelta(seconds=1800 - 119))
    assert auth.access().token == "access-2"
    assert stored_refresh(db_factory) == "refresh-2"


@respx.mock
def test_dead_token_error_is_stored_and_throttled(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(return_value=httpx.Response(400, text="bad"))
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    with pytest.raises(QuestradeAuthError, match="already been used or has expired"):
        auth.access()
    clock.advance(timedelta(seconds=30))
    with pytest.raises(QuestradeAuthError):
        auth.access()
    assert route.call_count == 1  # second call throttled by FAILED_REFRESH_COOLDOWN
    assert auth.health().last_error is not None


@respx.mock
def test_force_refresh_respects_cooldown(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    clock.advance(timedelta(seconds=60))
    assert auth.force_refresh().token == "access-1"  # within 90 s: no exchange
    clock.advance(timedelta(seconds=31))
    assert auth.force_refresh().token == "access-2"
    assert route.call_count == 2


@respx.mock
def test_seed_clears_previous_error(db_factory: sessionmaker[Session]) -> None:
    respx.get(TOKEN_URL).mock(side_effect=[httpx.Response(400), ok(5)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    with pytest.raises(QuestradeAuthError):
        auth.access()
    auth.seed("refresh-new")
    assert auth.access().token == "access-5"


@respx.mock
def test_keep_alive_exchanges_only_when_older_than_min_age(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.keep_alive()
    clock.advance(timedelta(minutes=30))
    auth.keep_alive()
    assert route.call_count == 1
    clock.advance(timedelta(minutes=31))
    auth.keep_alive()
    assert route.call_count == 2


@respx.mock
def test_concurrent_refresh_exchanges_once(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 1: two processes race on an expired token; exactly one exchange happens."""

    def slow_ok(request: httpx.Request) -> httpx.Response:
        time.sleep(0.3)
        return ok(1)

    route = respx.get(TOKEN_URL).mock(side_effect=slow_ok)
    clock = FixedClock(T0)
    make(db_factory, clock)
    results: list[str] = []

    def worker() -> None:
        results.append(QuestradeAuth(db_factory, CRYPTO, clock).access().token)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["access-1", "access-1"]
    assert route.call_count == 1
