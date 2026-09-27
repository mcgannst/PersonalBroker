"""P4-T4: the authentication building blocks (`trader.api.auth`): Argon2 passwords, the session cookie signer,
the login rate limiter and the first-start admin (acceptance test 11). The HTTP flows are in
`test_auth_routes.py`."""

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from argon2 import PasswordHasher
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.api import auth
from trader.db import models as m
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
ADMIN_PW = "Initial-Pw-8842-qrst"


# --- passwords ----------------------------------------------------------------------------------------------


def test_hash_password_is_argon2id_and_verifies() -> None:
    stored = auth.hash_password("Some-Password-1")
    assert stored.startswith("$argon2id$")
    assert "Some-Password-1" not in stored
    assert auth.verify_password(stored, "Some-Password-1") == (True, None)
    assert auth.verify_password(stored, "Some-Password-2") == (False, None)


def test_verify_password_rehashes_outdated_parameters() -> None:
    old = PasswordHasher(time_cost=1).hash("Old-Params-Pw-1")
    ok, new_hash = auth.verify_password(old, "Old-Params-Pw-1")
    assert ok is True
    assert new_hash is not None and new_hash != old
    assert PasswordHasher().check_needs_rehash(new_hash) is False
    assert auth.verify_password(new_hash, "Old-Params-Pw-1") == (True, None)


def test_verify_password_refuses_a_malformed_hash() -> None:
    assert auth.verify_password("not-a-hash", "anything") == (False, None)


# --- the session cookie signer ------------------------------------------------------------------------------


def test_signer_issues_token_and_mac_and_stores_only_the_sha256() -> None:
    signer = auth.SessionSigner.derive("session-secret-value")
    cookie, token_hash = signer.issue()
    token, _, mac = cookie.partition(".")
    assert token and mac
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert len(token_hash) == 64 and token not in token_hash
    assert signer.parse(cookie) == token_hash
    # Every issue is fresh.
    assert signer.issue()[0] != cookie


def test_signer_refuses_a_changed_mac_a_foreign_key_and_malformed_values() -> None:
    signer = auth.SessionSigner.derive("session-secret-value")
    cookie, _ = signer.issue()
    flipped = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    assert signer.parse(flipped) is None
    assert auth.SessionSigner.derive("another-secret").parse(cookie) is None
    for bad in ("", ".", "abc", "a.b.c", cookie + ".x", "x" * 5000, "é.é"):
        assert signer.parse(bad) is None
    # derive() is deterministic: a restarted process accepts the cookies it issued before.
    assert auth.SessionSigner.derive("session-secret-value").parse(cookie) is not None


# --- the login rate limiter ---------------------------------------------------------------------------------


def test_login_limiter_allows_n_per_sliding_minute_per_ip() -> None:
    clock = FixedClock(NOW)
    limiter = auth.LoginLimiter(clock, lambda: 3)
    assert [limiter.allow("1.1.1.1") for _ in range(3)] == [None, None, None]
    wait = limiter.allow("1.1.1.1")
    assert wait is not None and 59 <= wait <= 60
    assert limiter.allow("2.2.2.2") is None  # another IP is unaffected
    clock.advance(timedelta(seconds=30))
    wait = limiter.allow("1.1.1.1")
    assert wait is not None and 29 <= wait <= 30  # refused attempts do not extend the window
    clock.advance(timedelta(seconds=30))
    assert limiter.allow("1.1.1.1") is None


def test_login_limiter_reads_the_limit_each_time() -> None:
    clock = FixedClock(NOW)
    limit = [1]
    limiter = auth.LoginLimiter(clock, lambda: limit[0])
    assert limiter.allow("ip") is None
    assert limiter.allow("ip") is not None
    limit[0] = 5
    assert limiter.allow("ip") is None


# --- ensure_admin (acceptance test 11) ----------------------------------------------------------------------


def _users(factory: sessionmaker[Session]) -> list[m.User]:
    with factory() as s:
        return list(s.execute(select(m.User)).scalars())


def _audit(factory: sessionmaker[Session], action: str) -> list[m.AuditLog]:
    with factory() as s:
        return list(s.execute(select(m.AuditLog).where(m.AuditLog.action == action)).scalars())


def test_ensure_admin_creates_once_then_exists(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    assert auth.ensure_admin(db_factory, clock, "stephen", SecretStr(ADMIN_PW)) == "created"
    [user] = _users(db_factory)
    assert user.username == "stephen"
    assert user.password_hash.startswith("$argon2id$") and ADMIN_PW not in user.password_hash
    assert auth.verify_password(user.password_hash, ADMIN_PW)[0] is True
    assert user.failed_logins == 0 and user.totp_secret_enc is None
    assert user.created_at == NOW and user.password_changed_at == NOW
    [row] = _audit(db_factory, "auth.admin_created")
    assert row.actor == "system" and ADMIN_PW not in repr((row.before, row.after))

    # Called again (every container start): the table is not empty, so nothing is overwritten.
    assert auth.ensure_admin(db_factory, clock, "stephen", SecretStr("Another-Pw-9999")) == "exists"
    assert auth.ensure_admin(db_factory, clock, "other", SecretStr("Another-Pw-9999")) == "exists"
    [same] = _users(db_factory)
    assert same.password_hash == user.password_hash


def test_ensure_admin_rejects_a_short_password(
    db_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = FixedClock(NOW)
    with structlog.testing.capture_logs() as logs:
        assert auth.ensure_admin(db_factory, clock, "stephen", SecretStr("Short-7")) == "rejected"
    assert _users(db_factory) == []
    assert any(e["log_level"] == "critical" for e in logs)
    assert "Short-7" not in repr(logs) + caplog.text
    # 8 characters is enough (Stephen, 2026-09-27).
    assert auth.ensure_admin(db_factory, clock, "stephen", SecretStr("Eight-88")) == "created"


def test_ensure_admin_rejects_a_bad_username(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    with structlog.testing.capture_logs() as logs:
        assert auth.ensure_admin(db_factory, clock, "Stephen!", SecretStr(ADMIN_PW)) == "rejected"
    assert _users(db_factory) == []
    assert any(e["log_level"] == "critical" for e in logs)
    assert ADMIN_PW not in repr(logs)
    with db_factory() as s:
        events = list(s.execute(select(m.EventLog).where(m.EventLog.level == "critical")).scalars())
    assert events and all(ADMIN_PW not in repr((e.message, e.data)) for e in events)


@pytest.mark.parametrize(
    ("username", "password"),
    [
        (None, SecretStr(ADMIN_PW)),
        ("stephen", None),
        (None, None),
        ("", SecretStr(ADMIN_PW)),
        ("stephen", SecretStr("")),
    ],
)
def test_ensure_admin_not_configured(
    db_factory: sessionmaker[Session], username: str | None, password: SecretStr | None
) -> None:
    assert auth.ensure_admin(db_factory, FixedClock(NOW), username, password) == "not_configured"
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.User)).scalar_one() == 0
