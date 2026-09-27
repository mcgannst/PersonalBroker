"""questrade-seed and token-refresh against the throwaway DB, with Questrade mocked by respx."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from trader import bootstrap
from trader.adapters.questrade.auth import TOKEN_URL, QuestradeAuth, QuestradeAuthError
from trader.cli import app
from trader.crypto import Crypto
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
            "expires_in": 1800,
            "api_server": "https://api05.iq.questrade.com/",
        },
    )


@pytest.fixture
def clock(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> FixedClock:
    fixed = FixedClock(T0)
    core = SimpleNamespace(
        env=SimpleNamespace(questrade_refresh_token=SecretStr("refresh-env")),
        factory=db_factory,
        crypto=CRYPTO,
        clock=fixed,
    )
    monkeypatch.setattr(bootstrap, "build_core", lambda: core)
    return fixed


def stored_refresh(db_factory: sessionmaker[Session]) -> str | None:
    from trader.db.models import ApiCredential

    with db_factory() as s:
        row = s.get(ApiCredential, "questrade")
        assert row is not None
        return CRYPTO.decrypt(row.refresh_token_enc)


@respx.mock
def test_seed_refuses_to_overwrite_a_healthy_chain(
    db_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    respx.get(TOKEN_URL).mock(return_value=ok(1))
    auth = QuestradeAuth(db_factory, CRYPTO, clock)
    auth.seed("refresh-0")
    auth.access()

    refused = CliRunner().invoke(app, ["questrade-seed"])
    assert refused.exit_code == 1
    assert "--force" in refused.output
    assert stored_refresh(db_factory) == "refresh-1"

    forced = CliRunner().invoke(app, ["questrade-seed", "--force"])
    assert forced.exit_code == 0, forced.output
    assert stored_refresh(db_factory) == "refresh-env"


def test_seed_on_empty_table_needs_no_force(db_factory: sessionmaker[Session], clock: FixedClock) -> None:
    result = CliRunner().invoke(app, ["questrade-seed"])
    assert result.exit_code == 0, result.output
    assert "seeded" in result.output
    assert stored_refresh(db_factory) == "refresh-env"


@respx.mock
def test_seed_replaces_a_dead_chain_without_force(
    db_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    respx.get(TOKEN_URL).mock(return_value=httpx.Response(400))
    auth = QuestradeAuth(db_factory, CRYPTO, clock)
    auth.seed("refresh-0")
    with pytest.raises(QuestradeAuthError, match="already been used"):
        auth.access()
    result = CliRunner().invoke(app, ["questrade-seed"])
    assert result.exit_code == 0, result.output
    assert stored_refresh(db_factory) == "refresh-env"


@respx.mock
def test_token_refresh_prints_chain_extension_time(
    db_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    respx.get(TOKEN_URL).mock(return_value=ok(1))
    QuestradeAuth(db_factory, CRYPTO, clock).seed("refresh-0")
    first = CliRunner().invoke(app, ["token-refresh"])
    assert first.exit_code == 0, first.output
    assert f"chain last extended {T0.isoformat()}" in first.output

    # 40 min later the access token has expired, but the chain was extended < 1 h ago: still ok,
    # and the message reports the extension time, not a stale access-token expiry.
    clock.advance(timedelta(minutes=40))
    second = CliRunner().invoke(app, ["token-refresh"])
    assert second.exit_code == 0, second.output
    assert f"chain last extended {T0.isoformat()}" in second.output
    assert "access-1" not in second.output


@respx.mock
def test_token_refresh_fails_on_dead_chain(db_factory: sessionmaker[Session], clock: FixedClock) -> None:
    respx.get(TOKEN_URL).mock(return_value=httpx.Response(400))
    QuestradeAuth(db_factory, CRYPTO, clock).seed("refresh-0")
    result = CliRunner().invoke(app, ["token-refresh"])
    assert result.exit_code == 1
    assert "already been used" in result.output
