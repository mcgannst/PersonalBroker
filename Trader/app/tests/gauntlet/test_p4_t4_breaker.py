"""P4-T4 Breaker (attempt 1): authentication holes (phase 4 Review Focus 2).

Adversarial tests on the real auth router and dependencies, on the testcontainers database, through
`TestClient` on `https://testserver` (no network). Every test runs under a leak guard: no password, code,
TOTP secret (outside its one setup response) or session token may appear in a response body, a captured log
line (structlog, stdlib, stdout/stderr), an `audit_log` row or an `event_log` row.
"""

import hashlib
import hmac
import logging
import statistics
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import argon2
import httpx
import pyotp
import pytest
import structlog
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from tests.fakes_api import make_services, test_core
from trader.adapters.telegram.callbacks import CallbackSigner
from trader.api import auth
from trader.api.deps import ApiServices, CsrfUser, CurrentUser
from trader.api.errors import install_error_handlers
from trader.api.routers.auth import router as auth_router
from trader.bootstrap import Core
from trader.db import models as m
from trader.market.clock import FixedClock

# Mid-step (15:30:10 UTC) so a TOTP step boundary is never ambiguous.
NOW = datetime(2026, 10, 6, 15, 30, 10, tzinfo=UTC)
BASE_URL = "https://testserver"
PUBLIC = "https://trader.example.com"
USERNAME = "stephen"
PASSWORD = "Breaker-Right-8812"
WRONG = "Breaker-Wrong-4471"

# --- app, leak guard, helpers -------------------------------------------------------------------------------

protected = APIRouter()


@protected.get("/test/protected")
def protected_get(user: CurrentUser) -> dict[str, Any]:
    return {"username": user.username}


@protected.api_route("/test/protected", methods=["POST", "PUT", "PATCH", "DELETE"])
def protected_change(user: CsrfUser) -> dict[str, Any]:
    return {"username": user.username}


@dataclass
class Guard:
    secrets: list[str] = field(default_factory=list)
    bodies: list[tuple[str, str]] = field(default_factory=list)
    allowed: dict[str, str] = field(default_factory=dict)  # secret -> the one path allowed to carry it

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
    guard: Guard

    def client(self, ip: str = "10.1.0.1", app: Any = None) -> TestClient:
        c = TestClient(
            app if app is not None else self.app,
            base_url=BASE_URL,
            client=(ip, 50000),
            raise_server_exceptions=False,
        )
        c.event_hooks["response"].append(self.guard.hook)
        return c


@pytest.fixture
def guard(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> Iterator[Guard]:
    g = Guard()
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        yield g
    out = capsys.readouterr()
    text = repr(logs) + caplog.text + out.out + out.err
    for secret in g.secrets:
        assert secret not in text, "a secret reached a log line"
        for path, body in g.bodies:
            if g.allowed.get(secret) == path:
                continue
            assert secret not in body, f"a secret reached the response body of {path}"


@pytest.fixture
def env(db_factory: sessionmaker[Session], guard: Guard) -> Iterator[Env]:
    clock = FixedClock(NOW)
    core = test_core(db_factory, clock, public_base_url=PUBLIC, app_env="dev")
    services = make_services(core)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(auth_router, prefix="/api")
    app.include_router(protected, prefix="/api")
    app.state.services = services
    assert auth.ensure_admin(db_factory, clock, USERNAME, SecretStr(PASSWORD)) == "created"
    guard.add(PASSWORD, WRONG)
    yield Env(core, clock, services, app, guard)
    with db_factory() as s:
        audit = [(r.actor, r.action, r.before, r.after) for r in s.execute(select(m.AuditLog)).scalars()]
        events = [(r.message, r.data) for r in s.execute(select(m.EventLog)).scalars()]
    stored = repr(audit) + repr(events)
    for secret in guard.secrets:
        assert secret not in stored, "a secret reached audit_log or event_log"


def _login(
    c: TestClient,
    env: Env,
    password: str = PASSWORD,
    totp: str | None = None,
    username: str = USERNAME,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"username": username, "password": password}
    if totp is not None:
        body["totp"] = totp
        env.guard.add(totp)
    resp = c.post("/api/auth/login", json=body, headers=headers or {})
    cookie = resp.cookies.get(auth.COOKIE_NAME)
    if cookie:
        env.guard.add(cookie, cookie.partition(".")[0])
    return resp


def _cookie(resp: httpx.Response) -> str:
    value = resp.cookies.get(auth.COOKIE_NAME)
    assert value, resp.text
    return value


def _get(c: TestClient, cookie: str | None) -> httpx.Response:
    c.cookies.clear()
    headers = {"Cookie": f"{auth.COOKIE_NAME}={cookie}"} if cookie is not None else {}
    return c.get("/api/test/protected", headers=headers)


def _clears(resp: httpx.Response) -> bool:
    header = resp.headers.get("set-cookie", "").lower()
    return header.startswith(f"{auth.COOKIE_NAME}=") and "max-age=0" in header


def _user(env: Env) -> m.User:
    with env.core.factory() as s:
        return s.execute(select(m.User).where(m.User.username == USERNAME)).scalar_one()


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _state(env: Env) -> tuple[Any, ...]:
    """Everything a side effect could change, except `last_seen_at`."""
    with env.core.factory() as s:
        users = [
            (u.password_hash, u.totp_secret_enc, u.totp_pending_enc, u.failed_logins, u.locked_until)
            for u in s.execute(select(m.User).order_by(m.User.id)).scalars()
        ]
        sessions = [
            (w.id, w.revoked_at, w.expires_at, w.csrf_token)
            for w in s.execute(select(m.WebSession).order_by(m.WebSession.id)).scalars()
        ]
        audits = len(s.execute(select(m.AuditLog)).scalars().all())
    return users, sessions, audits


# --- cookies ------------------------------------------------------------------------------------------------


def test_forged_truncated_and_cross_domain_cookies_are_refused(env: Env) -> None:
    """A cookie is honoured only with this deployment's session MAC over a token that is in the database.
    A MAC made with the Telegram callback key (same SESSION_SECRET, other label) must not work."""
    c = env.client()
    cookie = _cookie(_login(c, env))
    token, _, mac = cookie.partition(".")
    secret = env.core.env.session_secret.get_secret_value()
    telegram_key = CallbackSigner.derive(secret)._secret
    raw_secret_mac = _b64(hmac.new(secret.encode(), token.encode(), hashlib.sha256).digest())
    other_deploy = auth.SessionSigner.derive("another-deployment-secret").issue()[0]
    valid_mac_unknown_token = auth.SessionSigner.derive(secret).issue()[0]  # right key, never stored
    forged = [
        token,  # truncated: no MAC
        f"{token}.",
        f".{mac}",
        cookie[:-4],  # truncated MAC
        cookie + "A",  # extended MAC
        f"{token}.{mac}.{mac}",
        f"{token}.{_b64(hmac.new(telegram_key, token.encode(), hashlib.sha256).digest())}",
        f"{token}.{raw_secret_mac}",  # the raw SESSION_SECRET instead of the derived key
        other_deploy,
        valid_mac_unknown_token,
        hashlib.sha256(token.encode()).hexdigest(),  # the stored hash itself
        f"{token}.{mac}" + "x" * 300,
        f'"{token}".{mac}',
    ]
    for value in forged:
        resp = _get(c, value)
        assert resp.status_code == 401, value
        assert resp.json()["error"]["code"] == "unauthorized"
        assert _clears(resp)
    assert _get(c, cookie).status_code == 200  # the real one still works


def test_login_cookie_is_fresh_flagged_and_cleared_with_the_same_flags(env: Env) -> None:
    """Session fixation: a cookie presented at login (valid or planted) is never adopted or reused.
    Login sets exactly one cookie with every flag; the clearing cookie (401 and logout) has the same name,
    Path and flags, or a browser would keep the real one."""
    c = env.client()
    first = _login(c, env)
    first_cookie = _cookie(first)
    planted = auth.SessionSigner.derive("attacker").issue()[0]
    second = _login(c, env, headers={"Cookie": f"{auth.COOKIE_NAME}={first_cookie}"})
    third = _login(c, env, headers={"Cookie": f"{auth.COOKIE_NAME}={planted}"})
    cookies = {first_cookie, _cookie(second), _cookie(third)}
    csrfs = {first.json()["csrf_token"], second.json()["csrf_token"], third.json()["csrf_token"]}
    assert len(cookies) == 3 and len(csrfs) == 3
    assert planted not in cookies
    assert _get(c, planted).status_code == 401
    with env.core.factory() as s:
        hashes = [w.token_hash for w in s.execute(select(m.WebSession)).scalars()]
    assert len(hashes) == len(set(hashes)) == 3

    c = env.client()
    resp = _login(c, env)
    set_cookies = resp.headers.get_list("set-cookie")
    assert len(set_cookies) == 1
    attrs = {p.strip().split("=")[0].lower(): p.strip() for p in set_cookies[0].split(";")}
    assert {"httponly", "secure", "samesite", "path", "max-age"} <= attrs.keys()
    assert attrs["samesite"].lower() == "samesite=strict" and attrs["path"].lower() == "path=/"
    assert "domain" not in attrs  # host-only
    csrf = resp.json()["csrf_token"]
    cookie = _cookie(resp)
    assert cookie not in resp.text and cookie.partition(".")[0] not in resp.text

    c.cookies.clear()
    logout = c.post(
        "/api/auth/logout", headers={"Cookie": f"{auth.COOKIE_NAME}={cookie}", auth.CSRF_HEADER: csrf}
    )
    assert logout.status_code == 200
    for cleared in (_get(c, "bogus"), logout):
        header = cleared.headers["set-cookie"]
        parts = {p.strip().lower() for p in header.split(";")}
        assert header.startswith(f"{auth.COOKIE_NAME}=")
        assert {"httponly", "secure", "samesite=strict", "path=/", "max-age=0"} <= parts, header


def test_every_way_a_session_ends_is_final(env: Env) -> None:
    """Logout, a password change from another session, and expiry end the session for good: a later
    request with the same cookie (or its CSRF token on a new session) never works again."""
    a, b = env.client("10.1.0.1"), env.client("10.1.0.2")
    ra, rb = _login(a, env), _login(b, env)
    cookie_a, cookie_b = _cookie(ra), _cookie(rb)
    csrf_a, csrf_b = ra.json()["csrf_token"], rb.json()["csrf_token"]

    new_pw = "Breaker-New-9090"
    env.guard.add(new_pw)
    changed = b.put(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": new_pw},
        headers={auth.CSRF_HEADER: csrf_b},
    )
    assert changed.status_code == 200
    assert _get(a, cookie_a).status_code == 401  # revoked by b's change
    # A revoked session cannot be revived by using it again later, or by presenting its CSRF token.
    env.clock.advance(timedelta(minutes=5))
    a.cookies.clear()
    revived = a.post(
        "/api/test/protected",
        headers={"Cookie": f"{auth.COOKIE_NAME}={cookie_a}", auth.CSRF_HEADER: csrf_a},
    )
    assert revived.status_code == 401

    # Log out b; its cookie is dead; a fresh login gets a new CSRF token and b's old one is refused.
    b.cookies.clear()
    out = b.post(
        "/api/auth/logout", headers={"Cookie": f"{auth.COOKIE_NAME}={cookie_b}", auth.CSRF_HEADER: csrf_b}
    )
    assert out.status_code == 200
    assert _get(b, cookie_b).status_code == 401
    # Logging out twice is refused, not an error.
    b.cookies.clear()
    again = b.post(
        "/api/auth/logout", headers={"Cookie": f"{auth.COOKIE_NAME}={cookie_b}", auth.CSRF_HEADER: csrf_b}
    )
    assert again.status_code == 401

    c = env.client("10.1.0.3")
    rc = _login(c, env, password=new_pw)
    assert rc.status_code == 200
    for stale in (csrf_a, csrf_b):
        resp = c.post("/api/test/protected", headers={auth.CSRF_HEADER: stale})
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "csrf"
    assert _login(env.client("10.1.0.4"), env).status_code == 401  # the old password is gone


# --- CSRF and Origin ----------------------------------------------------------------------------------------


def test_csrf_and_origin_edge_cases(env: Env) -> None:
    c = env.client()
    csrf = _login(c, env).json()["csrf_token"]
    h = {auth.CSRF_HEADER: csrf}

    # Every unsafe method needs the header; an empty, padded, case-changed or truncated token is refused.
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert c.request(method, "/api/test/protected").status_code == 403, method
        assert c.request(method, "/api/test/protected", headers=h).status_code == 200, method
    for bad in ("", " ", f" {csrf}", f"{csrf} ", csrf.swapcase(), csrf[:-1], csrf + csrf):
        if bad == csrf:
            continue
        resp = c.post("/api/test/protected", headers={auth.CSRF_HEADER: bad})
        assert resp.status_code == 403, repr(bad)

    # Foreign, null, look-alike, scheme-downgraded and other-port origins are refused.
    for origin in (
        "null",
        "https://evil.example",
        "https://testserver.evil.example",
        "https://evil.example/https://testserver",
        "http://testserver",
        "https://testserver:8443",
        "http://trader.example.com",
        "file://",
        "https://user@evil.example",
    ):
        resp = c.post("/api/test/protected", headers={**h, "Origin": origin})
        assert resp.status_code == 403, origin
        login = c.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers={"Origin": origin}
        )
        assert login.status_code == 403 and not login.cookies.get(auth.COOKIE_NAME), origin

    # A cross-site "simple" POST (text/plain, as an HTML form can send) is never a login.
    plain = c.post(
        "/api/auth/login",
        content=f'{{"username": "{USERNAME}", "password": "{PASSWORD}"}}',
        headers={"Content-Type": "text/plain"},
    )
    assert plain.status_code == 422 and not plain.cookies.get(auth.COOKIE_NAME)


def test_get_requests_have_no_side_effects(env: Env) -> None:
    """A GET (or HEAD) to any auth path changes nothing: no logout, no TOTP setup, no password change."""
    c = env.client()
    resp = _login(c, env)
    before = _state(env)
    for path in (
        "/api/auth/logout",
        "/api/auth/password",
        "/api/auth/totp/setup",
        "/api/auth/totp/confirm",
        "/api/auth/totp/disable",
        "/api/auth/login",
        "/api/auth/me",
    ):
        for method in ("GET", "HEAD"):
            r = c.request(method, path)
            assert r.status_code in (200, 405), (method, path, r.status_code)
            assert "secret" not in r.text
    assert _state(env) == before
    assert c.get("/api/test/protected").status_code == 200
    assert c.get("/api/auth/me").json()["csrf_token"] == resp.json()["csrf_token"]


# --- login limits -------------------------------------------------------------------------------------------


def _main_uvicorn_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import trader.api.__main__ as api_main

    captured: dict[str, Any] = {}
    monkeypatch.setattr(api_main, "create_app", lambda: object())
    monkeypatch.setattr(api_main.uvicorn, "run", lambda app, **kw: captured.update(kw))
    api_main.main()
    return captured


def test_x_forwarded_for_cannot_dodge_the_per_ip_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolved open question 1: X-Forwarded-For is trusted only from the proxy network. NPM APPENDS the
    real client to any X-Forwarded-For the client sent, so the limiter must key on the hop NPM added, and a
    peer outside the proxy network must not be able to choose its IP at all. Uses the exact uvicorn proxy
    settings `python -m trader.api` runs with."""
    kw = _main_uvicorn_kwargs(monkeypatch)
    assert kw.get("proxy_headers", True) is not False
    wrapped = ProxyHeadersMiddleware(env.app, trusted_hosts=kw.get("forwarded_allow_ips") or "127.0.0.1")

    # (a) Through NPM (a peer on the Docker proxy network), one LAN device rotating a spoofed first hop.
    npm = env.client("172.18.0.5", app=wrapped)
    statuses = []
    for i in range(11):
        r = _login(
            npm,
            env,
            username="nobody",
            password=WRONG,
            headers={"X-Forwarded-For": f"6.6.6.{i}, 192.168.68.50"},
        )
        statuses.append(r.status_code)
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429, (
        "a spoofed X-Forwarded-For first hop gave a LAN client a fresh rate-limit bucket"
    )

    # (b) A peer outside the proxy network sending its own X-Forwarded-For.
    direct = env.client("192.168.68.77", app=wrapped)
    statuses = [
        _login(
            direct, env, username="nobody", password=WRONG, headers={"X-Forwarded-For": f"7.7.7.{i}"}
        ).status_code
        for i in range(11)
    ]
    assert statuses[10] == 429, "X-Forwarded-For was trusted from outside the proxy network"


def test_concurrent_wrong_passwords_lock_exactly_once(env: Env) -> None:
    """A lockout race: 8 wrong passwords at once from 8 IPs. The row lock must serialise them: exactly 5
    count (401), the rest see the lock (429), one lockout audit row, and the right password is refused."""
    barrier = threading.Barrier(8)
    results: list[int] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        c = env.client(f"10.2.0.{i}")
        barrier.wait()
        r = c.post("/api/auth/login", json={"username": USERNAME, "password": WRONG})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert sorted(results) == [401] * 5 + [429] * 3, results
    user = _user(env)
    assert user.failed_logins == 5 and user.locked_until == NOW + timedelta(minutes=15)
    with env.core.factory() as s:
        lockouts = s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.lockout")).scalars().all()
    assert len(lockouts) == 1
    assert _login(env.client("10.2.1.1"), env).status_code == 429


def test_lock_boundary_and_a_fresh_count_after_expiry(env: Env) -> None:
    c = env.client()
    for _ in range(5):
        assert _login(c, env, password=WRONG).status_code == 401
    until = _user(env).locked_until
    assert until is not None

    env.clock.set(until - timedelta(seconds=30))
    near = _login(env.client("10.3.0.1"), env)
    assert near.status_code == 429
    assert near.headers["retry-after"] == "30"
    assert near.json()["error"]["message"] == "Too many failed attempts. Try again in 1 minute."
    env.clock.set(until - timedelta(microseconds=1))
    assert _login(env.client("10.3.0.2"), env).status_code == 429

    # The lock runs out exactly at locked_until; a wrong password then starts a new count (no instant relock).
    env.clock.set(until)
    assert _login(env.client("10.3.0.3"), env, password=WRONG).status_code == 401
    user = _user(env)
    assert user.failed_logins == 1 and user.locked_until is None
    assert _login(env.client("10.3.0.4"), env).status_code == 200
    assert _user(env).failed_logins == 0


def test_no_username_enumeration_by_message_parameters_or_timing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown user, wrong password, and (with TOTP on) right password without a code all look the same;
    the dummy hash costs what a real hash costs."""
    assert argon2.extract_parameters(auth._dummy_hash()) == argon2.extract_parameters(
        _user(env).password_hash
    )

    ip = iter(f"10.4.{i // 200}.{i % 200}" for i in range(10_000))

    def attempt(username: str, password: str) -> tuple[httpx.Response, float]:
        c = env.client(next(ip))
        start = time.perf_counter()
        r = _login(c, env, username=username, password=password)
        return r, time.perf_counter() - start

    unknown, _ = attempt("nobody-here", WRONG)
    wrong, _ = attempt(USERNAME, WRONG)

    def shape(r: httpx.Response) -> tuple[Any, ...]:
        headers = {k: v for k, v in r.headers.items() if k not in ("date", "x-request-id", "content-length")}
        return r.status_code, r.json()["error"]["code"], r.json()["error"]["message"], headers

    assert shape(unknown) == shape(wrong)

    # Coarse timing: Argon2 dominates both paths (medians within a factor of 3 on a loaded machine).
    _login(env.client(next(ip)), env)  # reset the failure count before timing
    t_unknown, t_wrong = [], []
    for _ in range(3):
        t_unknown.append(attempt("nobody-here", WRONG)[1])
        t_wrong.append(attempt(USERNAME, WRONG)[1])
    ratio = statistics.median(t_unknown) / statistics.median(t_wrong)
    assert 1 / 3 < ratio < 3, ratio


def test_username_case_whitespace_and_hostile_names(env: Env) -> None:
    for name in ("Stephen", "STEPHEN", "  stephen  ", "\tstephen\n", " stephen"):
        assert _login(env.client(f"10.5.0.{len(name)}"), env, username=name).status_code == 200, repr(name)
    too_long = "s" * 51
    r = _login(env.client("10.5.2.1"), env, username=too_long)
    assert r.status_code == 422 and too_long not in r.text
    # Not the same user: inner space, accent, a Cyrillic homoglyph, SQL-ish, and last a NUL byte (a plain
    # 401 like any unknown name, never a 500).
    for i, name in enumerate(("ste phen", "stéphen", "ѕtephen", "stephen'--", "stephen\x00", "\x00")):
        r = _login(env.client(f"10.5.1.{i}"), env, username=name)
        assert r.status_code == 401, (repr(name), r.status_code, r.text)
        assert r.json()["error"]["message"] == "Invalid username or password"


def test_password_length_unicode_and_nul(env: Env) -> None:
    """Minimum length is 8 code points (not bytes); 200 is the maximum; a NUL inside a password neither
    truncates it nor crashes; a password is compared exactly (no trimming)."""
    c = env.client()
    h = {auth.CSRF_HEADER: _login(c, env).json()["csrf_token"]}
    current = PASSWORD

    def change(new: str) -> httpx.Response:
        env.guard.add(new)
        return c.put("/api/auth/password", json={"current_password": current, "new_password": new}, headers=h)

    for seven in ("日本語のパスワ", "ääääääá", "🔑🔑🔑🔑🔑🔑🔑", "éééa"):
        assert len(seven) == 7
        r = change(seven)
        assert r.status_code == 422, seven
        assert seven not in r.text

    too_long = "L" * 201
    r = change(too_long)
    assert r.status_code == 422 and "L" * 50 not in r.text

    for new in ("日本語のパスワード", "x" * 200, "abcd\x00efgh"):
        assert len(new) >= 8
        assert change(new).status_code == 200, repr(new)
        current = new
        assert _login(env.client("10.6.0.1"), env, password=new).status_code == 200

    # The NUL password: its prefix, and the password with a trailing space, are both wrong.
    assert _login(env.client("10.6.0.2"), env, password="abcd").status_code == 401
    assert _login(env.client("10.6.0.3"), env, password="abcd\x00efgh ").status_code == 401
    assert _login(env.client("10.6.0.4"), env, password="abcd\x00efgh").status_code == 200
    # A 201-character login password is a 422 that does not echo it.
    r = _login(env.client("10.6.0.5"), env, password="p" * 201)
    assert r.status_code == 422 and "p" * 50 not in r.text


# --- TOTP ---------------------------------------------------------------------------------------------------


def _enable_totp(env: Env, c: TestClient, h: dict[str, str]) -> pyotp.TOTP:
    setup = c.post("/api/auth/totp/setup", json={"password": PASSWORD}, headers=h)
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    env.guard.add(secret)
    env.guard.allowed[secret] = "/api/auth/totp/setup"
    totp = pyotp.TOTP(secret)
    code = totp.at(env.clock.now())
    env.guard.add(code)
    assert c.post("/api/auth/totp/confirm", json={"code": code}, headers=h).status_code == 200
    return totp


def test_totp_setup_restart_window_and_monotonic_steps(env: Env) -> None:
    c = env.client()
    h = {auth.CSRF_HEADER: _login(c, env).json()["csrf_token"]}

    # Setup restarted mid-way: the first secret's codes stop working; only the latest one confirms.
    first = c.post("/api/auth/totp/setup", json={"password": PASSWORD}, headers=h).json()["secret"]
    env.guard.add(first)
    env.guard.allowed[first] = "/api/auth/totp/setup"
    second = c.post("/api/auth/totp/setup", json={"password": PASSWORD}, headers=h).json()["secret"]
    env.guard.add(second)
    env.guard.allowed[second] = "/api/auth/totp/setup"
    assert second != first
    first_code = pyotp.TOTP(first).at(env.clock.now())
    env.guard.add(first_code)
    if first_code != pyotp.TOTP(second).at(env.clock.now()):
        r = c.post("/api/auth/totp/confirm", json={"code": first_code}, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "bad_credentials"
    totp = _enable_totp(env, c, h)
    assert totp.secret not in (first, second)
    stale = pyotp.TOTP(first).at(env.clock.now() + timedelta(seconds=30))
    env.guard.add(stale)

    # Setup again while on: 409, and no new secret leaks or replaces the active one.
    before = _user(env).totp_secret_enc
    again = c.post("/api/auth/totp/setup", json={"password": PASSWORD}, headers=h)
    assert again.status_code == 409 and "secret" not in again.json()
    assert _user(env).totp_secret_enc == before

    env.clock.advance(timedelta(minutes=2))
    now = env.clock.now()
    plus2, minus2 = totp.at(now + timedelta(seconds=60)), totp.at(now - timedelta(seconds=60))
    plus1, current = totp.at(now + timedelta(seconds=30)), totp.at(now)
    env.guard.add(plus2, minus2, plus1, current)
    ip = iter(f"10.7.0.{i}" for i in range(1, 200))
    if plus2 not in (plus1, current):
        assert _login(env.client(next(ip)), env, totp=plus2).status_code == 401
    if minus2 not in (plus1, current):
        assert _login(env.client(next(ip)), env, totp=minus2).status_code == 401
    assert _login(env.client(next(ip)), env, totp=stale).status_code == 401
    assert _login(env.client(next(ip)), env, totp="١٢٣٤٥٦").status_code in (401, 422)  # Arabic-Indic digits
    # +1 step works once; after it, the current (earlier) step is refused too: steps only move forward.
    assert _login(env.client(next(ip)), env, totp=plus1).status_code == 200
    if current != plus1:
        assert _login(env.client(next(ip)), env, totp=current).status_code == 401
    assert _login(env.client(next(ip)), env, totp=plus1).status_code == 401

    # No response after setup carries the secret (checked by the guard), and /auth/me never does.
    me = c.get("/api/auth/me")
    assert me.json()["user"]["totp_enabled"] is True and totp.secret not in me.text


def test_totp_same_code_raced_from_two_clients_wins_once(env: Env) -> None:
    c = env.client()
    h = {auth.CSRF_HEADER: _login(c, env).json()["csrf_token"]}
    totp = _enable_totp(env, c, h)
    env.clock.advance(timedelta(seconds=30))
    code = totp.at(env.clock.now())
    env.guard.add(code)
    barrier = threading.Barrier(4)
    results: list[int] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        cl = env.client(f"10.8.0.{i}")
        barrier.wait()
        r = cl.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD, "totp": code})
        if r.cookies.get(auth.COOKIE_NAME):
            env.guard.add(r.cookies[auth.COOKIE_NAME])
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert sorted(results) == [200, 401, 401, 401], results


def test_totp_disable_needs_a_fresh_valid_code(env: Env) -> None:
    c = env.client()
    h = {auth.CSRF_HEADER: _login(c, env).json()["csrf_token"]}
    totp = _enable_totp(env, c, h)
    env.clock.advance(timedelta(seconds=30))
    used = totp.at(env.clock.now())
    env.guard.add(used)
    assert _login(env.client("10.9.0.1"), env, totp=used).status_code == 200

    missing = c.post("/api/auth/totp/disable", json={"password": PASSWORD}, headers=h)
    assert missing.status_code == 422
    for code in ("000000" if used != "000000" else "111111", used, "12345", "1234567", "12345a"):
        r = c.post("/api/auth/totp/disable", json={"password": PASSWORD, "code": code}, headers=h)
        assert r.status_code in (403, 422), (code, r.status_code)
        assert code not in r.text
    assert _user(env).totp_secret_enc is not None  # still on
    assert c.get("/api/test/protected").status_code == 200  # 403 bad_credentials kept the session
    assert _user(env).failed_logins == 0  # and did not count toward the login lockout


# --- ensure_admin -------------------------------------------------------------------------------------------


def test_ensure_admin_idempotent_rejects_and_never_prints_the_password(
    db_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FixedClock(NOW)
    caplog.set_level(logging.DEBUG)
    short, good = "Sh0rt-7", "Adm1n-Pw-Ok-8421"
    with structlog.testing.capture_logs() as logs:
        for name in ("Stephen", " stephen", "stephen ", "stephen\n", "st", "1stephen", "a" * 51, "stéphen"):
            assert auth.ensure_admin(db_factory, clock, name, SecretStr(good)) == "rejected", repr(name)
        assert auth.ensure_admin(db_factory, clock, USERNAME, SecretStr(short)) == "rejected"
        assert auth.ensure_admin(db_factory, clock, USERNAME, SecretStr("")) == "not_configured"
        assert auth.ensure_admin(db_factory, clock, "", SecretStr(good)) == "not_configured"
        with db_factory() as s:
            assert s.execute(select(m.User)).scalars().all() == []

        # Four processes start at once: exactly one creates the admin.
        results: list[str] = []
        barrier = threading.Barrier(4)

        def start() -> None:
            barrier.wait()
            results.append(auth.ensure_admin(db_factory, clock, USERNAME, SecretStr(good)))

        threads = [threading.Thread(target=start) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert sorted(results) == ["created", "exists", "exists", "exists"], results
        # Later starts with another password never overwrite it.
        assert auth.ensure_admin(db_factory, clock, USERNAME, SecretStr("Other-Pw-12345")) == "exists"
    with db_factory() as s:
        [user] = s.execute(select(m.User)).scalars().all()
        created = (
            s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.admin_created")).scalars().all()
        )
        stored = repr(
            [(a.actor, a.action, a.before, a.after) for a in s.execute(select(m.AuditLog)).scalars()]
        )
        stored += repr([(e.message, e.data) for e in s.execute(select(m.EventLog)).scalars()])
    assert auth.verify_password(user.password_hash, good)[0] is True
    assert len(created) == 1
    out = capsys.readouterr()
    text = repr(logs) + caplog.text + out.out + out.err + stored
    for secret in (short, good, "Other-Pw-12345"):
        assert secret not in text


# --- secrets in error bodies and logs; the 403 bad_credentials ruling ---------------------------------------


def test_no_secret_in_any_4xx_body_or_log_and_bad_credentials_keep_the_session(env: Env) -> None:
    c = env.client()
    login = _login(c, env)
    h = {auth.CSRF_HEADER: login.json()["csrf_token"]}
    marker_pw, marker_code = "Leaky-Pass-5150", "314159"
    env.guard.add(marker_pw, marker_code, "L3aky-Seven")

    bad_requests = [
        c.post("/api/auth/login", data={"username": USERNAME, "password": marker_pw}),
        c.post("/api/auth/login", json={"username": USERNAME, "password": marker_pw, "totp": "31415x"}),
        c.post("/api/auth/login", json={"username": marker_pw * 4, "password": marker_pw}),
        c.post("/api/auth/login", json={"username": USERNAME, "password": [marker_pw]}),
        c.post("/api/auth/login", content=b'{"username": "stephen", "password": "Leaky-Pass-5150"'),
        c.put(
            "/api/auth/password",
            json={"current_password": marker_pw, "new_password": "L3aky-Seven"},
            headers=h,
        ),
        c.post("/api/auth/totp/confirm", json={"code": marker_code + "0"}, headers=h),
        c.post("/api/auth/totp/disable", json={"password": marker_pw, "code": marker_code}, headers=h),
    ]
    for r in bad_requests:
        assert 400 <= r.status_code < 500, (r.request.url.path, r.status_code)
    env.guard.add(marker_code + "0", "31415x")

    # A signed-in user's wrong current password, many times: 403 bad_credentials, the session survives,
    # nothing is cleared, and it does not lock the login.
    for _ in range(6):
        r = c.put(
            "/api/auth/password",
            json={"current_password": marker_pw, "new_password": "Brand-New-7777"},
            headers=h,
        )
        assert r.status_code == 403 and r.json()["error"]["code"] == "bad_credentials"
        assert "set-cookie" not in r.headers
    assert c.get("/api/test/protected").status_code == 200
    assert _login(env.client("10.10.0.1"), env).status_code == 200
