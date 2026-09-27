"""P4-T4 acceptance tests 1-10, 12 and 13: the auth routes and the real session and CSRF dependencies, on the
test database, through `TestClient` on `https://testserver` with the auth router plus test-only protected
routes (Review Focus 2).

Test 12 runs in every test here: the autouse `leak_guard` fixture records each response body, every captured
log line (structlog and stdlib) and the audit rows, and fails the test if any secret it used appears in them.
"""

import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pyotp
import pytest
import structlog
from argon2 import PasswordHasher
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_api import make_services, test_core
from trader.api import auth
from trader.api.deps import ApiServices, CsrfUser, CurrentUser
from trader.api.errors import install_error_handlers
from trader.api.routers.auth import router as auth_router
from trader.bootstrap import Core
from trader.db import models as m
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
BASE_URL = "https://testserver"
PUBLIC = "https://trader.example.com"
USERNAME = "stephen"
PASSWORD = "Right-Password-5531"
WRONG = "Wrong-Password-7719"


# --- the app and the leak guard -----------------------------------------------------------------------------

protected = APIRouter()


@protected.get("/test/protected")
def protected_get(user: CurrentUser) -> dict[str, Any]:
    return {"username": user.username, "session_id": user.session_id}


@protected.post("/test/protected")
def protected_post(user: CsrfUser) -> dict[str, Any]:
    return {"username": user.username}


@dataclass
class LeakGuard:
    """Secrets a test used, and every place they must never appear."""

    secrets: list[str] = field(default_factory=list)
    bodies: list[tuple[str, str]] = field(default_factory=list)  # (request path, response body)
    # Secrets that one response legitimately carries (the TOTP secret in the setup response only).
    allowed: dict[str, str] = field(default_factory=dict)

    def add(self, *values: str) -> None:
        self.secrets.extend(v for v in values if v)

    def hook(self, response: httpx.Response) -> None:
        response.read()
        self.bodies.append((response.request.url.path, response.text))


@dataclass
class Env:
    core: Core
    clock: FixedClock
    services: ApiServices
    app: FastAPI
    guard: LeakGuard

    def client(self, ip: str = "10.0.0.1") -> TestClient:
        c = TestClient(self.app, base_url=BASE_URL, client=(ip, 50000))
        c.event_hooks["response"].append(self.guard.hook)
        return c


@pytest.fixture
def leak_guard(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> Iterator[LeakGuard]:
    guard = LeakGuard()
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        yield guard
    captured = capsys.readouterr()
    logs_text = repr(logs) + caplog.text + captured.out + captured.err
    for secret in guard.secrets:
        assert secret not in logs_text, "a secret reached a log line"
        for path, body in guard.bodies:
            if guard.allowed.get(secret) == path:
                continue
            assert secret not in body, f"a secret reached the response body of {path}"


@pytest.fixture
def env(db_factory: sessionmaker[Session], leak_guard: LeakGuard) -> Iterator[Env]:
    clock = FixedClock(NOW)
    core = test_core(db_factory, clock, public_base_url=PUBLIC, app_env="dev")
    services = make_services(core)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(auth_router, prefix="/api")
    app.include_router(protected, prefix="/api")
    app.state.services = services
    assert auth.ensure_admin(db_factory, clock, USERNAME, SecretStr(PASSWORD)) == "created"
    leak_guard.add(PASSWORD, WRONG)
    yield Env(core, clock, services, app, leak_guard)
    assert leak_guard.bodies, "the leak guard saw no responses"
    # The audit trail never holds a secret either.
    with db_factory() as s:
        rows = list(s.execute(select(m.AuditLog)).scalars())
    audit_text = repr([(r.actor, r.action, r.before, r.after) for r in rows])
    for secret in leak_guard.secrets:
        assert secret not in audit_text, "a secret reached the audit log"


def _login(
    c: TestClient, env: Env, password: str = PASSWORD, totp: str | None = None, username: str = USERNAME
) -> httpx.Response:
    body: dict[str, Any] = {"username": username, "password": password}
    if totp is not None:
        body["totp"] = totp
        env.guard.add(totp)
    resp = c.post("/api/auth/login", json=body)
    cookie = resp.cookies.get(auth.COOKIE_NAME)
    if cookie:
        env.guard.add(cookie, cookie.partition(".")[0])
    return resp


def _cookie_of(resp: httpx.Response) -> str:
    value = resp.cookies.get(auth.COOKIE_NAME)
    assert value
    return value


def _get(c: TestClient, cookie: str | None) -> httpx.Response:
    c.cookies.clear()
    headers = {"Cookie": f"{auth.COOKIE_NAME}={cookie}"} if cookie is not None else {}
    return c.get("/api/test/protected", headers=headers)


def _clears_cookie(resp: httpx.Response) -> bool:
    header = resp.headers.get("set-cookie", "")
    return header.startswith(f"{auth.COOKIE_NAME}=") and "max-age=0" in header.lower()


def _user(env: Env) -> m.User:
    with env.core.factory() as s:
        return s.execute(select(m.User).where(m.User.username == USERNAME)).scalar_one()


# --- 1. login -----------------------------------------------------------------------------------------------


def test_1_login_sets_a_secure_cookie_and_stores_only_the_token_hash(env: Env) -> None:
    c = env.client()
    resp = _login(c, env)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"] == {"username": USERNAME, "totp_enabled": False}
    assert body["csrf_token"] and len(body["csrf_token"]) >= 32
    assert body["expires_at"] == (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z")

    header = resp.headers["set-cookie"]
    parts = [p.strip().lower() for p in header.split(";")]
    assert "httponly" in parts and "secure" in parts and "samesite=strict" in parts and "path=/" in parts
    assert f"max-age={30 * 86400}" in parts

    cookie = _cookie_of(resp)
    token = cookie.partition(".")[0]
    with env.core.factory() as s:
        [row] = list(s.execute(select(m.WebSession)).scalars())
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.token_hash != token and token not in row.token_hash
    assert row.csrf_token == body["csrf_token"]
    assert row.expires_at == NOW + timedelta(days=30) and row.revoked_at is None
    assert row.ip == "10.0.0.1"
    with env.core.factory() as s:
        [audit] = list(s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.login")).scalars())
    assert audit.actor == "web:stephen" and audit.after == {"ip": "10.0.0.1"}
    assert _user(env).last_login_at == NOW

    # The cookie works, and /auth/me returns the same session.
    assert _get(c, cookie).json()["username"] == USERNAME
    me = c.get("/api/auth/me", headers={"Cookie": f"{auth.COOKIE_NAME}={cookie}"})
    assert me.status_code == 200 and me.json() == body


def test_1_login_accepts_the_username_in_any_case(env: Env) -> None:
    assert _login(env.client(), env, username="Stephen").status_code == 200


def test_1_login_needs_a_json_body(env: Env) -> None:
    c = env.client()
    resp = c.post("/api/auth/login", data={"username": USERNAME, "password": PASSWORD})
    assert resp.status_code == 422


def test_1_user_agent_is_cut_to_200(env: Env) -> None:
    c = env.client()
    c.headers["user-agent"] = "x" * 500
    assert _login(c, env).status_code == 200
    with env.core.factory() as s:
        [row] = list(s.execute(select(m.WebSession)).scalars())
    assert row.user_agent == "x" * 200


# --- 2. protected routes refuse anything but a valid live session -------------------------------------------


def test_2_protected_route_refuses_missing_tampered_revoked_expired_and_idle(env: Env) -> None:
    c = env.client()
    cookie = _cookie_of(_login(c, env))
    assert _get(c, cookie).status_code == 200

    for value in (None, cookie[:-1] + ("A" if cookie[-1] != "A" else "B"), "garbage", ""):
        resp = _get(c, value)
        assert resp.status_code == 401, value
        assert resp.json()["error"]["code"] == "unauthorized"
        assert _clears_cookie(resp)

    # Revoked.
    with env.core.factory() as s:
        row = s.execute(select(m.WebSession)).scalar_one()
        row.revoked_at = NOW
        s.commit()
    resp = _get(c, cookie)
    assert resp.status_code == 401 and _clears_cookie(resp)


def test_2_session_ends_at_expires_at(env: Env) -> None:
    env.core.settings.set("web.session_max_days", 1, "test")
    c = env.client()
    cookie = _cookie_of(_login(c, env))
    # Stay active (inside the idle limit) until the maximum age is reached.
    for _ in range(4):
        env.clock.advance(timedelta(hours=5))
        assert _get(c, cookie).status_code == 200
    env.clock.advance(timedelta(hours=4, minutes=1))  # 24 h 1 min after login
    resp = _get(c, cookie)
    assert resp.status_code == 401 and _clears_cookie(resp)


def test_2_session_ends_after_the_idle_limit(env: Env) -> None:
    c = env.client()
    cookie = _cookie_of(_login(c, env))
    env.clock.advance(timedelta(hours=167, minutes=59))
    assert _get(c, cookie).status_code == 200  # still inside 7 days, and this use moves last_seen_at
    env.clock.advance(timedelta(hours=167, minutes=59))
    assert _get(c, cookie).status_code == 200
    env.clock.advance(timedelta(hours=168))
    resp = _get(c, cookie)
    assert resp.status_code == 401 and _clears_cookie(resp)


def test_2_last_seen_moves_at_most_once_a_minute(env: Env) -> None:
    c = env.client()
    cookie = _cookie_of(_login(c, env))

    def last_seen() -> datetime:
        with env.core.factory() as s:
            return s.execute(select(m.WebSession.last_seen_at)).scalar_one()

    env.clock.advance(timedelta(seconds=30))
    _get(c, cookie)
    assert last_seen() == NOW
    env.clock.advance(timedelta(seconds=31))
    _get(c, cookie)
    assert last_seen() == NOW + timedelta(seconds=61)


# --- 3. no hint whether the name or the password was wrong --------------------------------------------------


def test_3_wrong_password_and_unknown_user_look_the_same(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = auth.verify_password

    def spy(stored: str, pw: str) -> tuple[bool, str | None]:
        calls.append(stored)
        return real(stored, pw)

    monkeypatch.setattr(auth, "verify_password", spy)
    c = env.client()
    wrong = _login(c, env, password=WRONG)
    assert len(calls) == 1
    unknown = _login(c, env, username="nobody", password=WRONG)
    assert len(calls) == 2
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["message"] == "Invalid username or password"
    assert "set-cookie" not in wrong.headers or not wrong.cookies.get(auth.COOKIE_NAME)
    with env.core.factory() as s:
        rows = list(s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.login_failed")).scalars())
    assert len(rows) == 2 and all(r.after and "ip" in r.after and "reason" in r.after for r in rows)


# --- 4. lockout ---------------------------------------------------------------------------------------------


def test_4_five_failures_lock_the_login_even_for_the_right_password(env: Env) -> None:
    c = env.client()
    for _ in range(5):
        assert _login(c, env, password=WRONG).status_code == 401
    assert _user(env).locked_until == NOW + timedelta(minutes=15)

    resp = _login(c, env)
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == str(15 * 60)
    assert resp.json()["error"]["message"] == "Too many failed attempts. Try again in 15 minutes."
    assert not resp.cookies.get(auth.COOKIE_NAME)
    with env.core.factory() as s:
        assert s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.lockout")).scalars().all()

    env.clock.advance(timedelta(minutes=14))
    assert _login(c, env).status_code == 429
    env.clock.advance(timedelta(minutes=1))
    assert _login(c, env).status_code == 200
    user = _user(env)
    assert user.failed_logins == 0 and user.locked_until is None


def test_4_a_success_resets_the_counter(env: Env) -> None:
    c = env.client()
    for _ in range(4):
        _login(c, env, password=WRONG)
    assert _user(env).failed_logins == 4
    assert _login(c, env).status_code == 200
    assert _user(env).failed_logins == 0
    for _ in range(4):
        assert _login(c, env, password=WRONG).status_code == 401  # a fresh count, not locked yet


# --- 5. per-IP rate limit -----------------------------------------------------------------------------------


def test_5_eleventh_attempt_in_a_minute_is_refused_before_the_database(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = env.client("10.9.9.9")
    for _ in range(10):
        assert _login(c, env, username="nobody", password=WRONG).status_code == 401

    calls: list[str] = []
    real_login = auth.login

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append("login")
        return real_login(*args, **kwargs)

    monkeypatch.setattr(auth, "login", counting)
    resp = _login(c, env)  # even the right password
    assert resp.status_code == 429 and int(resp.headers["retry-after"]) > 0
    assert resp.json()["error"]["code"] == "too_many_requests"
    assert calls == []
    with env.core.factory() as s:
        failed = s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.login_failed")).scalars().all()
    assert len(failed) == 10

    # Another IP is unaffected; the first one is allowed again after the minute.
    assert _login(env.client("10.8.8.8"), env).status_code == 200
    env.clock.advance(timedelta(seconds=61))
    assert _login(c, env).status_code == 200


# --- 6. TOTP ------------------------------------------------------------------------------------------------


def test_6_totp_setup_confirm_login_replay_and_disable(env: Env) -> None:
    c = env.client()
    resp = _login(c, env)
    csrf = resp.json()["csrf_token"]
    h = {auth.CSRF_HEADER: csrf}

    wrong_pw = c.post("/api/auth/totp/setup", json={"password": WRONG}, headers=h)
    assert wrong_pw.status_code == 403

    setup = c.post("/api/auth/totp/setup", json={"password": PASSWORD}, headers=h)
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    env.guard.add(secret)
    env.guard.allowed[secret] = "/api/auth/totp/setup"
    uri = setup.json()["otpauth_uri"]
    assert uri.startswith("otpauth://totp/Trader%20(dev):stephen?")
    assert f"secret={secret}" in uri and "issuer=Trader" in uri
    user = _user(env)
    assert user.totp_pending_enc and secret not in user.totp_pending_enc and user.totp_secret_enc is None
    assert c.get("/api/auth/me").json()["user"]["totp_enabled"] is False

    totp = pyotp.TOTP(secret)
    bad = totp.at(env.clock.now() + timedelta(minutes=5))
    env.guard.add(bad)
    assert c.post("/api/auth/totp/confirm", json={"code": bad}, headers=h).status_code == 403
    code = totp.at(env.clock.now())
    env.guard.add(code)
    assert c.post("/api/auth/totp/confirm", json={"code": code}, headers=h).status_code == 200
    assert _user(env).totp_secret_enc and _user(env).totp_pending_enc is None
    assert c.get("/api/auth/me").json()["user"]["totp_enabled"] is True

    other = env.client("10.0.0.2")
    no_code = _login(other, env)
    assert no_code.status_code == 401
    assert no_code.json()["error"]["message"] == "Invalid username or password"
    # The confirm step's code is already used: a login in the same step is a replay.
    assert _login(other, env, totp=code).status_code == 401
    env.clock.advance(timedelta(seconds=30))
    code2 = totp.at(env.clock.now())
    assert _login(other, env, totp=code2).status_code == 200
    assert _login(env.client("10.0.0.3"), env, totp=code2).status_code == 401  # same code again

    # A code from the previous step is still accepted (window ±1) when not yet used.
    env.clock.advance(timedelta(seconds=60))
    previous = totp.at(env.clock.now() - timedelta(seconds=30))
    assert _login(env.client("10.0.0.4"), env, totp=previous).status_code == 200

    env.clock.advance(timedelta(seconds=30))
    code3 = totp.at(env.clock.now())
    env.guard.add(code3)
    assert (
        c.post("/api/auth/totp/disable", json={"password": WRONG, "code": code3}, headers=h).status_code
        == 403
    )
    assert (
        c.post("/api/auth/totp/disable", json={"password": PASSWORD, "code": code3}, headers=h).status_code
        == 200
    )
    user = _user(env)
    assert user.totp_secret_enc is None and user.totp_pending_enc is None
    assert _login(env.client("10.0.0.5"), env).status_code == 200
    with env.core.factory() as s:
        actions = {r.action for r in s.execute(select(m.AuditLog)).scalars()}
    assert {"auth.totp_enable", "auth.totp_disable"} <= actions


# --- a signed-in user's wrong password or code is 403, never 401 --------------------------------------------


def _bad_credentials(resp: httpx.Response) -> bool:
    return (
        resp.status_code == 403
        and resp.json()["error"]["code"] == "bad_credentials"
        and "set-cookie" not in resp.headers
    )


def test_wrong_password_or_code_while_signed_in_is_403_and_keeps_the_session(env: Env) -> None:
    c = env.client()
    h = {auth.CSRF_HEADER: _login(c, env).json()["csrf_token"]}

    # TOTP setup with the wrong password.
    assert _bad_credentials(c.post("/api/auth/totp/setup", json={"password": WRONG}, headers=h))
    setup = c.post("/api/auth/totp/setup", json={"password": PASSWORD}, headers=h)
    secret = setup.json()["secret"]
    env.guard.add(secret)
    env.guard.allowed[secret] = "/api/auth/totp/setup"
    totp = pyotp.TOTP(secret)
    wrong_code = totp.at(env.clock.now() + timedelta(minutes=10))
    env.guard.add(wrong_code)

    # TOTP confirm with a wrong code.
    assert _bad_credentials(c.post("/api/auth/totp/confirm", json={"code": wrong_code}, headers=h))
    code = totp.at(env.clock.now())
    env.guard.add(code)
    assert c.post("/api/auth/totp/confirm", json={"code": code}, headers=h).status_code == 200

    # Password change: wrong current password; right password but a missing or wrong code (TOTP is on).
    body = {"current_password": WRONG, "new_password": "New-Password-1"}
    assert _bad_credentials(c.put("/api/auth/password", json=body, headers=h))
    body = {"current_password": PASSWORD, "new_password": "New-Password-1"}
    assert _bad_credentials(c.put("/api/auth/password", json=body, headers=h))
    assert _bad_credentials(c.put("/api/auth/password", json={**body, "totp": wrong_code}, headers=h))

    # TOTP disable: wrong password; right password and a wrong code.
    env.clock.advance(timedelta(seconds=30))
    fresh = totp.at(env.clock.now())
    env.guard.add(fresh)
    disable = {"password": WRONG, "code": fresh}
    assert _bad_credentials(c.post("/api/auth/totp/disable", json=disable, headers=h))
    disable = {"password": PASSWORD, "code": wrong_code}
    assert _bad_credentials(c.post("/api/auth/totp/disable", json=disable, headers=h))

    # The session survived every refusal, and nothing was changed.
    assert c.get("/api/test/protected").status_code == 200
    assert _user(env).totp_secret_enc is not None
    assert (
        c.post("/api/auth/totp/disable", json={"password": PASSWORD, "code": fresh}, headers=h).status_code
        == 200
    )


# --- 7. password change -------------------------------------------------------------------------------------


def test_7_password_change(env: Env) -> None:
    c = env.client()
    csrf = _login(c, env).json()["csrf_token"]
    other = env.client("10.0.0.2")
    other_cookie = _cookie_of(_login(other, env))
    h = {auth.CSRF_HEADER: csrf}

    seven = "Seven-7"
    env.guard.add(seven)
    resp = c.put("/api/auth/password", json={"current_password": PASSWORD, "new_password": seven}, headers=h)
    assert resp.status_code == 422 and seven not in resp.text and PASSWORD not in resp.text

    same = c.put(
        "/api/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD}, headers=h
    )
    assert same.status_code == 422

    wrong = c.put(
        "/api/auth/password", json={"current_password": WRONG, "new_password": "Eight-88"}, headers=h
    )
    assert wrong.status_code == 403

    eight = "Eight-88"
    env.guard.add(eight)
    ok = c.put("/api/auth/password", json={"current_password": PASSWORD, "new_password": eight}, headers=h)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert _get(other, other_cookie).status_code == 401  # other sessions end
    assert c.get("/api/test/protected").status_code == 200  # the current one continues
    assert _user(env).password_changed_at == NOW
    with env.core.factory() as s:
        [row] = list(
            s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.password_change")).scalars()
        )
    assert eight not in repr((row.before, row.after)) and PASSWORD not in repr((row.before, row.after))

    assert _login(env.client("10.0.0.3"), env).status_code == 401
    assert _login(env.client("10.0.0.3"), env, password=eight).status_code == 200


# --- 8. CSRF ------------------------------------------------------------------------------------------------


def test_8_csrf_header_required_on_changes(env: Env) -> None:
    c = env.client()
    csrf = _login(c, env).json()["csrf_token"]
    other_csrf = _login(env.client("10.0.0.2"), env).json()["csrf_token"]

    missing = c.post("/api/test/protected")
    assert missing.status_code == 403 and missing.json()["error"]["code"] == "csrf"
    foreign = c.post("/api/test/protected", headers={auth.CSRF_HEADER: other_csrf})
    assert foreign.status_code == 403 and foreign.json()["error"]["code"] == "csrf"
    assert c.post("/api/test/protected", headers={auth.CSRF_HEADER: csrf}).status_code == 200
    assert c.get("/api/test/protected").status_code == 200
    # Logout is a change too.
    assert c.post("/api/auth/logout").status_code == 403


# --- 9. Origin ----------------------------------------------------------------------------------------------


def test_9_foreign_origin_refused_on_login_and_changes(env: Env) -> None:
    c = env.client()
    evil = {"Origin": "https://evil.example"}
    resp = c.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=evil)
    assert resp.status_code == 403 and not resp.cookies.get(auth.COOKIE_NAME)

    for origin in (PUBLIC, BASE_URL):
        c2 = env.client()
        r = c2.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers={"Origin": origin}
        )
        assert r.status_code == 200, origin
        env.guard.add(_cookie_of(r))

    csrf = _login(c, env).json()["csrf_token"]
    h = {auth.CSRF_HEADER: csrf}
    assert c.post("/api/test/protected", headers={**h, **evil}).status_code == 403
    assert c.post("/api/test/protected", headers={**h, "Origin": "null"}).status_code == 403
    assert c.post("/api/test/protected", headers={**h, "Origin": PUBLIC}).status_code == 200
    assert c.post("/api/test/protected", headers={**h, "Origin": BASE_URL}).status_code == 200


# --- 10. logout ---------------------------------------------------------------------------------------------


def test_10_logout_revokes_and_clears(env: Env) -> None:
    c = env.client()
    resp = _login(c, env)
    cookie = _cookie_of(resp)
    out = c.post("/api/auth/logout", headers={auth.CSRF_HEADER: resp.json()["csrf_token"]})
    assert out.status_code == 200 and out.json()["ok"] is True
    assert _clears_cookie(out)
    with env.core.factory() as s:
        row = s.execute(select(m.WebSession)).scalar_one()
        assert row.revoked_at == NOW
        assert s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.logout")).scalars().all()
    assert _get(c, cookie).status_code == 401


# --- 13. rehash on login ------------------------------------------------------------------------------------


def test_13_login_with_outdated_argon2_parameters_rehashes(env: Env) -> None:
    old = PasswordHasher(time_cost=1).hash(PASSWORD)
    with env.core.factory() as s:
        user = s.execute(select(m.User)).scalar_one()
        user.password_hash = old
        s.commit()
    assert _login(env.client(), env).status_code == 200
    stored = _user(env).password_hash
    assert stored != old and PasswordHasher().check_needs_rehash(stored) is False
    assert auth.verify_password(stored, PASSWORD)[0] is True
