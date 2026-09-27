"""P1-T6 Breaker: try to break trader.adapters.questrade.auth.

Questrade is mocked with respx and the database is the throwaway testcontainers one (db_factory).
Nothing here touches the real token chain or trader_dev.
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from trader.adapters.questrade.auth import TOKEN_URL, QuestradeAuth, QuestradeAuthError
from trader.crypto import Crypto
from trader.db.models import ApiCredential
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
CRYPTO = Crypto(Fernet.generate_key().decode())


def ok(n: int, api_server: str = "https://api05.iq.questrade.com/") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": f"access-{n}",
            "refresh_token": f"refresh-{n}",
            "token_type": "Bearer",
            "expires_in": 1800,
            "api_server": api_server,
        },
    )


def stored_refresh(factory: sessionmaker[Session]) -> str | None:
    with factory() as s:
        row = s.get(ApiCredential, "questrade")
        assert row is not None
        return CRYPTO.decrypt(row.refresh_token_enc)


def seeded(factory: sessionmaker[Session], clock: FixedClock, token: str = "refresh-0") -> QuestradeAuth:
    auth = QuestradeAuth(factory, CRYPTO, clock)
    auth.seed(token)
    return auth


def run_threads(targets: list[Any]) -> list[BaseException]:
    errors: list[BaseException] = []

    def wrap(fn: Any) -> Any:
        def inner() -> None:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - collected and asserted on
                errors.append(exc)

        return inner

    threads = [threading.Thread(target=wrap(fn)) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors


@respx.mock
def test_five_processes_on_expired_token_exchange_exactly_once(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 1 with more than two racers, on a token that WAS valid and has now expired."""
    calls: list[str] = []

    def slow(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["refresh_token"])
        time.sleep(0.2)
        return ok(len(calls))

    respx.get(TOKEN_URL).mock(side_effect=slow)
    clock = FixedClock(T0)
    seeded(db_factory, clock).access()  # access-1 / refresh-1
    clock.advance(timedelta(minutes=31))  # access-1 now expired
    results: list[str] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        auth = QuestradeAuth(db_factory, CRYPTO, clock)  # one instance per "process"
        barrier.wait(timeout=10)
        results.append(auth.access().token)

    errors = run_threads([worker] * 5)
    assert errors == []
    assert calls == ["refresh-0", "refresh-1"]  # one initial + exactly one for the race
    assert results == ["access-2"] * 5
    assert stored_refresh(db_factory) == "refresh-2"


@respx.mock
def test_forced_refresh_racing_normal_access_exchanges_once(db_factory: sessionmaker[Session]) -> None:
    """A 401 in one process (force_refresh) while another finds the token expired (access)."""
    calls: list[str] = []

    def slow(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["refresh_token"])
        time.sleep(0.2)
        return ok(len(calls))

    respx.get(TOKEN_URL).mock(side_effect=slow)
    clock = FixedClock(T0)
    seeded(db_factory, clock).access()
    clock.advance(timedelta(minutes=31))
    results: dict[str, str] = {}
    barrier = threading.Barrier(4)

    def forced(name: str) -> Any:
        def fn() -> None:
            auth = QuestradeAuth(db_factory, CRYPTO, clock)
            barrier.wait(timeout=10)
            results[name] = auth.force_refresh().token

        return fn

    def normal(name: str) -> Any:
        def fn() -> None:
            auth = QuestradeAuth(db_factory, CRYPTO, clock)
            barrier.wait(timeout=10)
            results[name] = auth.access().token

        return fn

    errors = run_threads([forced("f1"), normal("a1"), forced("f2"), normal("a2")])
    assert errors == []
    assert calls == ["refresh-0", "refresh-1"]
    assert set(results.values()) == {"access-2"}
    assert stored_refresh(db_factory) == "refresh-2"


@respx.mock
def test_commit_after_exchange_failing_is_critical_and_never_silent(
    db_factory: sessionmaker[Session],
) -> None:
    """SPEC §4.1: if saving the rotated token fails, log critical. The spent token must not be
    reported as success, and the caller must get an explanation."""

    class CommitFails(Session):
        def commit(self) -> None:
            raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

    route = respx.get(TOKEN_URL).mock(return_value=ok(1))
    clock = FixedClock(T0)
    seeded(db_factory, clock)
    failing = sessionmaker(db_factory.kw["bind"], class_=CommitFails, expire_on_commit=False)
    auth = QuestradeAuth(failing, CRYPTO, clock)
    with capture_logs() as logs, pytest.raises(QuestradeAuthError) as exc:
        auth.access()
    assert route.call_count == 1
    assert "couldn't be saved" in str(exc.value) or "could not be saved" in str(exc.value)
    assert any(e["log_level"] == "critical" for e in logs), logs
    assert "refresh-1" not in str(exc.value)
    assert "access-1" not in str(exc.value)
    # Nothing half-written: the DB still holds only the old token and no access token.
    with db_factory() as s:
        row = s.get(ApiCredential, "questrade")
        assert row is not None
        assert CRYPTO.decrypt(row.access_token_enc) is None
    assert stored_refresh(db_factory) == "refresh-0"


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200, json={"access_token": "access-1", "api_server": "https://x/", "expires_in": 1800}
        ),
        httpx.Response(200, text="<html>maintenance</html>"),
    ],
    ids=["missing_refresh_token", "non_json_body"],
)
def test_malformed_200_is_auth_error_recorded_and_throttled(
    db_factory: sessionmaker[Session], response: httpx.Response
) -> None:
    """A 200 without a new refresh token means the old one may already be spent. It must be a
    QuestradeAuthError (not a raw crash), visible in health(), and not retried at API request rate."""
    route = respx.get(TOKEN_URL).mock(return_value=response)
    clock = FixedClock(T0)
    auth = seeded(db_factory, clock)
    with pytest.raises(QuestradeAuthError):
        auth.access()
    assert auth.health().last_error is not None
    clock.advance(timedelta(seconds=10))
    with pytest.raises(QuestradeAuthError):
        auth.access()
    assert route.call_count == 1
    with db_factory() as s:
        row = s.get(ApiCredential, "questrade")
        assert row is not None
        assert row.access_token_enc is None


@respx.mock
def test_network_error_is_auth_error_without_retry_storm(db_factory: sessionmaker[Session]) -> None:
    """SPEC §4.1: a failed refresh has a 60 s cooldown. A network failure is a failed refresh; without
    the cooldown every API request (up to 20/s) hits login.questrade.com."""
    route = respx.get(TOKEN_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    clock = FixedClock(T0)
    auth = seeded(db_factory, clock)
    with pytest.raises(QuestradeAuthError) as exc:
        auth.access()
    assert "refresh-0" not in str(exc.value)
    assert route.call_count == 1  # no internal retry loop
    for _ in range(5):
        clock.advance(timedelta(seconds=5))
        with pytest.raises(QuestradeAuthError):
            auth.access()
    assert route.call_count == 1, f"{route.call_count} exchanges within 25 s of a failed refresh"
    # The token was never spent, so once the cooldown passes and the network is back it works.
    route.side_effect = None
    route.return_value = ok(1)
    clock.advance(timedelta(seconds=61))
    assert auth.access().token == "access-1"


@respx.mock
@pytest.mark.parametrize(
    "api_server",
    [
        "https://api05.iq.questrade.com",
        "https://api05.iq.questrade.com/",
        "https://api05.iq.questrade.com/v1",
        "https://api05.iq.questrade.com/v1/",
    ],
)
def test_api_server_forms_normalise_to_one_base(db_factory: sessionmaker[Session], api_server: str) -> None:
    respx.get(TOKEN_URL).mock(return_value=ok(1, api_server=api_server))
    clock = FixedClock(T0)
    auth = seeded(db_factory, clock)
    assert auth.access().api_base == "https://api05.iq.questrade.com/v1/"
    clock.advance(timedelta(minutes=5))
    assert auth.access().api_base == "https://api05.iq.questrade.com/v1/"  # the stored path too


@respx.mock
def test_seed_strips_whitespace_keep_alive_never_refreshed_and_blank_seed_rejected(
    db_factory: sessionmaker[Session],
) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = seeded(db_factory, clock, token="  \trefresh-0 \r\n")
    assert stored_refresh(db_factory) == "refresh-0"
    assert auth.health().last_refresh_at is None

    # keep_alive on a chain that has never been refreshed must extend it, not return early.
    tok = auth.keep_alive()
    assert route.call_count == 1
    assert route.calls.last.request.url.params["refresh_token"] == "refresh-0"
    assert tok.token == "access-1"
    assert auth.health().last_refresh_at == T0
    assert stored_refresh(db_factory) == "refresh-1"

    # A blank QUESTRADE_REFRESH_TOKEN must not overwrite (and so destroy) the live chain.
    for blank in ["", "   ", "\n", " \r\n\t"]:
        with pytest.raises((QuestradeAuthError, ValueError)):
            auth.seed(blank)
    assert stored_refresh(db_factory) == "refresh-1"


@respx.mock
def test_unseeded_and_rotated_key_give_clear_auth_errors(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(return_value=ok(1))
    clock = FixedClock(T0)
    auth = QuestradeAuth(db_factory, CRYPTO, clock)

    # Before seeding: health() reports unseeded and every entry point is a clean QuestradeAuthError.
    health = auth.health()
    assert (health.seeded, health.expires_at, health.last_refresh_at, health.last_error) == (
        False,
        None,
        None,
        None,
    )
    for call in (auth.access, auth.force_refresh, auth.keep_alive):
        with pytest.raises(QuestradeAuthError, match="set up"):
            call()
    assert route.call_count == 0

    # Seed and refresh with the old key, then rotate APP_ENCRYPTION_KEY.
    auth.seed("refresh-0")
    auth.access()
    assert route.call_count == 1
    rotated = QuestradeAuth(db_factory, Crypto(Fernet.generate_key().decode()), clock)
    for call in (rotated.access, rotated.force_refresh, rotated.keep_alive):
        with pytest.raises(QuestradeAuthError) as exc:
            call()
        assert "refresh token" in str(exc.value).lower() or "token" in str(exc.value).lower()
    assert route.call_count == 1  # never sends an empty/garbage refresh token to Questrade
    rotated.health()  # must not raise
    # The old key still reads the chain: nothing was overwritten while the key was wrong.
    assert stored_refresh(db_factory) == "refresh-1"
