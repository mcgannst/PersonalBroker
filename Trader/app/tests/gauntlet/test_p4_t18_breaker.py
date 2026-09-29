"""P4-T18 Breaker (gauntlet attempt 1): the wiring under stress.

- build_services: nothing touches the network before first use (sockets guarded, real PTB client built),
  a half-configured Telegram builds no client, the stack closes what was opened (one Questrade client per
  API process), a bad settings row neither stops the API nor alerts once per click.
- create-admin: never prints or logs the password, idempotent, exit codes.
- user-password: a mismatched pair re-prompts, the 8-character boundary counts characters, every session
  (the browser's own included) ends, the lockout is cleared, the audit actor is `cli`.
- Heartbeat extra: a non-JSON value (NaN) or a clash never loses the beat or the P3 keys; `rate_limit`
  appears only once the worker's one shared Questrade client is open.
- Notifier via to_thread: exactly once across the API's and the worker's notifiers; a slow database never
  stalls the event loop (nor does the API's quote-cache settings read).
- The sweeps are not vacuous: a route without a session or CSRF dependency, a nullability change in
  types.ts, and a ProposalService construction (direct or aliased) are each caught.
"""

import asyncio
import dataclasses
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs
from typer.testing import CliRunner

import tests.api.test_routes_sweep as sweep
import tests.api.test_ts_contract as tc
import trader.bootstrap
import trader.runtime as rt
from tests.api.test_routes_sweep import (  # noqa: F401 (the fixture)
    BASE,
    PASSWORD,
    USER,
    Wired,
    api_routes,
    fill_path,
    login,
    seed_entries,
    wired,
)
from tests.factories import add_symbol
from tests.fakes_api import test_core
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_telegram import FakeTelegramApi
from tests.test_runtime import World, world  # noqa: F401 (the fixture)
from tests.test_worker import SAT, Harness, _heartbeat, et
from trader.adapters.telegram.api import PtbTelegramApi
from trader.api import auth, schemas
from trader.api.deps import ApiServices, CurrentUser
from trader.api.main import create_app
from trader.api.services import build_services
from trader.cli import app as cli_app
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.notify.notifier import TelegramNotifier
from trader.notify.types import OutboundMessage
from trader.settings_store import RuntimeSettings
from trader.worker import Worker

NOW = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)
runner = CliRunner()


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>")
    return dist


def _app(core: Any, dist: Path) -> Any:
    async def factory(stack: AsyncExitStack) -> ApiServices:
        return await build_services(core, stack)

    return create_app(services_factory=factory, web_dist=dist)


class _RefusingQt:
    """A Questrade client context that must never be entered (it would connect)."""

    def __init__(self) -> None:
        self.attempts = 0

    async def __aenter__(self) -> Any:
        self.attempts += 1
        raise AssertionError("the Questrade client was opened")

    async def __aexit__(self, *exc: object) -> None:
        return None


class _TrackedQt:
    def __init__(self, qt: FakeQuestrade) -> None:
        self.qt = qt
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> FakeQuestrade:
        self.entered += 1
        return self.qt

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1


class _TrackedTelegram(FakeTelegramApi):
    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


# --- build_services -----------------------------------------------------------------------------------------


@pytest.mark.db
def test_build_services_opens_no_socket_until_first_use_even_with_the_real_telegram_client(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real PTB client is built and entered (Telegram configured), Questrade is a context that raises
    on entry, and every Python socket connect or DNS lookup is refused: start-up, a few requests and the
    shutdown must not try one. A half-configured Telegram (token, no chat id) builds no client at all."""
    core = test_core(
        db_factory,
        FixedClock(NOW),
        telegram_bot_token=SecretStr("123456:TEST-TOKEN-NOT-REAL"),
        telegram_chat_id=1,
    )
    qt = _RefusingQt()
    monkeypatch.setattr(rt, "questrade_client", lambda core: qt)
    attempts: list[Any] = []
    local = {"localhost", "127.0.0.1", "::1"}  # the test database container (psycopg resolves in Python)
    real_connect, real_connect_ex, real_dns = (
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.getaddrinfo,
    )

    def remote(address: Any) -> bool:
        return isinstance(address, tuple) and str(address[0]) not in local

    def guard_connect(self: socket.socket, address: Any) -> Any:
        if remote(address):
            attempts.append(address)
            raise OSError("network disabled in this test")
        return real_connect(self, address)

    def guard_connect_ex(self: socket.socket, address: Any) -> Any:
        if remote(address):
            attempts.append(address)
            raise OSError("network disabled in this test")
        return real_connect_ex(self, address)

    def guard_dns(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and str(host) not in local:
            attempts.append(host)
            raise OSError("DNS disabled in this test")
        return real_dns(host, *args, **kwargs)

    app = _app(core, _dist(tmp_path))
    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guard_dns)
    with TestClient(app, base_url=BASE) as client:
        services: ApiServices = app.state.services
        assert isinstance(services.notifier, TelegramNotifier)
        assert isinstance(services.notifier.api, PtbTelegramApi)  # the real client path was exercised
        assert client.get("/api/meta").status_code == 200
        assert client.get("/api/health").status_code in (200, 503)
        assert client.get("/api/auth/me").status_code == 401
    monkeypatch.undo()
    assert attempts == [], attempts
    assert qt.attempts == 0

    def no_client(env: Any) -> Any:
        raise AssertionError("a Telegram client was built without TELEGRAM_CHAT_ID")

    monkeypatch.setattr(rt, "build_telegram_api", no_client)
    monkeypatch.setattr(rt, "questrade_client", lambda core: qt)
    half = test_core(db_factory, FixedClock(NOW), telegram_bot_token=SecretStr("123456:TEST-TOKEN-NOT-REAL"))
    auth.ensure_admin(db_factory, half.clock, USER, SecretStr(PASSWORD))
    app = _app(half, _dist(tmp_path))
    with TestClient(app, base_url=BASE) as client:
        assert app.state.services.telegram_configured is False
        assert not isinstance(app.state.services.notifier, TelegramNotifier)
        csrf = login(client)
        r = client.post("/api/system/telegram-test", headers={"X-CSRF-Token": csrf})
        assert r.status_code == 409, r.text


@pytest.mark.db
async def test_one_questrade_client_per_api_process_and_the_stack_closes_it_and_telegram(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    core = test_core(db_factory, FixedClock(NOW), telegram_bot_token=SecretStr("1:x"), telegram_chat_id=7)
    tg = _TrackedTelegram()
    fake = FakeQuestrade()
    qt = _TrackedQt(fake)
    monkeypatch.setattr(rt, "build_telegram_api", lambda env: tg)
    monkeypatch.setattr(rt, "questrade_client", lambda core: qt)
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        s.commit()
    async with AsyncExitStack() as stack:
        services = await build_services(core, stack)
        assert qt.entered == 0 and tg.closed == 0
        assert services.quotes is not None and services.candles is not None
        await asyncio.gather(services.quotes([sym]), services.quotes([sym]), services.quotes([sym, sym]))
        await services.candles(sym, NOW - timedelta(days=3), NOW - timedelta(days=2))
        assert qt.entered == 1  # quotes and candles share ONE lazily opened client
    assert qt.exited == 1, "the Questrade client was not closed with the stack"
    assert tg.closed == 1, "the Telegram client was not closed with the stack"


@pytest.mark.db
def test_a_bad_settings_row_keeps_the_api_up_and_does_not_alert_on_every_click(wired: Wired) -> None:  # noqa: F811
    """With an unusable settings row, the API still starts and serves; web approvals must not each write a
    relayed `settings` error event (the worker's GuardedSettings already alerts once per streak)."""
    auth.ensure_admin(wired.factory, wired.clock, USER, SecretStr(PASSWORD))
    _, (first, second) = seed_entries(wired.core, ("AAA", "BBB"))
    with wired.factory() as s:
        s.add(m.Setting(key="web.sse_poll_seconds", value=999, updated_by="test"))
        s.commit()
    app = wired.app()
    with TestClient(app, base_url=BASE, raise_server_exceptions=False) as client:
        csrf = login(client)
        for pid in (first, second, first):
            r = client.post(f"/api/proposals/{pid}/approve", headers={"X-CSRF-Token": csrf})
            assert r.status_code != 401
        assert client.get("/api/meta").status_code == 200
        assert client.get("/api/auth/me").status_code == 200
    with wired.factory() as s:
        alerts = s.execute(select(m.EventLog).where(m.EventLog.source == rt.SETTINGS_SOURCE)).scalars().all()
    assert len(alerts) <= 1, f"{len(alerts)} relayed settings alerts from 3 web clicks"


# --- create-admin -------------------------------------------------------------------------------------------


def _use_core(monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session], **env: Any) -> Any:
    core = test_core(factory, FixedClock(NOW), **env)
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    return core


def _all_output(result: Any) -> str:
    out = result.output
    try:
        out += result.stderr
    except ValueError:  # an older Click that mixes stderr into output
        pass
    return out


@pytest.mark.db
def test_create_admin_never_prints_or_logs_the_password(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "Zq9-unique-initial-pw-5531"  # noqa: S105 (a throwaway test password)
    short = "Zq9-7ch"  # 7 characters
    seen: list[str] = []
    with capture_logs() as logs:
        _use_core(monkeypatch, db_factory, admin_username="stephen", admin_password_initial=SecretStr(short))
        rejected = runner.invoke(cli_app, ["create-admin"])
        _use_core(
            monkeypatch, db_factory, admin_username="Bad Name!", admin_password_initial=SecretStr(secret)
        )
        bad_name = runner.invoke(cli_app, ["create-admin"])
        _use_core(monkeypatch, db_factory, admin_username="stephen", admin_password_initial=SecretStr(secret))
        created = runner.invoke(cli_app, ["create-admin"])
        exists = runner.invoke(cli_app, ["create-admin"])
    for result in (rejected, bad_name, created, exists):
        seen.append(_all_output(result))
    assert (rejected.exit_code, bad_name.exit_code, created.exit_code, exists.exit_code) == (1, 1, 0, 0)
    text = "\n".join(seen) + "\n" + repr(logs)
    with db_factory() as s:
        events = " ".join(e.message + repr(e.data) for e in s.execute(select(m.EventLog)).scalars())
        audits = " ".join(repr(a.before) + repr(a.after) for a in s.execute(select(m.AuditLog)).scalars())
    for pw in (secret, short):
        assert pw not in text, "a password reached stdout, stderr or a log line"
        assert pw not in events + audits, "a password reached event_log or audit_log"


@pytest.mark.db
def test_create_admin_is_idempotent_and_its_exit_codes_never_block_a_restart(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    first_pw, second_pw = "first-initial-password", "second-initial-password"
    _use_core(monkeypatch, db_factory, admin_username="stephen", admin_password_initial=SecretStr(first_pw))
    assert runner.invoke(cli_app, ["create-admin"]).exit_code == 0
    # a later start with a changed env password, and one with an env username that is now invalid
    _use_core(monkeypatch, db_factory, admin_username="stephen", admin_password_initial=SecretStr(second_pw))
    again = runner.invoke(cli_app, ["create-admin"])
    _use_core(monkeypatch, db_factory, admin_username="NOT VALID", admin_password_initial=SecretStr("x"))
    bad_later = runner.invoke(cli_app, ["create-admin"])
    assert again.exit_code == 0 and again.output.strip().splitlines()[-1] == "exists"
    assert bad_later.exit_code == 0 and bad_later.output.strip().splitlines()[-1] == "exists"
    with db_factory() as s:
        users = s.execute(select(m.User)).scalars().all()
        created = (
            s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.admin_created")).scalars().all()
        )
    assert len(users) == 1 and len(created) == 1
    assert auth.verify_password(users[0].password_hash, first_pw)[0]  # the env never overwrites a password
    assert not auth.verify_password(users[0].password_hash, second_pw)[0]

    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(trader.bootstrap, "build_core", broken)
    down = runner.invoke(cli_app, ["create-admin"])
    assert down.exit_code == 1 and "create-admin" in _all_output(down)


# --- user-password ------------------------------------------------------------------------------------------


@pytest.mark.db
def test_user_password_reprompts_on_a_mismatch_and_counts_characters_not_bytes(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_core(monkeypatch, db_factory)
    assert (
        auth.ensure_admin(db_factory, FixedClock(NOW), "stephen", SecretStr("initial-password-1"))
        == "created"
    )
    a, b, c = "mismatch-one-AAA", "mismatch-two-BBB", "the-final-one-CCC"
    result = runner.invoke(cli_app, ["user-password"], input=f"{a}\n{b}\n{c}\n{c}\n")
    assert result.exit_code == 0, result.output
    for pw in (a, b, c):
        assert pw not in _all_output(result)
    with db_factory() as s:
        user = s.execute(select(m.User)).scalar_one()
        assert auth.verify_password(user.password_hash, c)[0]
        assert not auth.verify_password(user.password_hash, a)[0]
    seven, eight = "éééé€€€", "éééé€€€€"  # 7 and 8 characters (14+ bytes each)
    refused = runner.invoke(cli_app, ["user-password"], input=f"{seven}\n{seven}\n")
    assert refused.exit_code == 1 and "8 characters" in _all_output(refused)
    accepted = runner.invoke(cli_app, ["user-password"], input=f"{eight}\n{eight}\n")
    assert accepted.exit_code == 0, accepted.output
    with db_factory() as s:
        user = s.execute(select(m.User)).scalar_one()
        assert auth.verify_password(user.password_hash, eight)[0]


@pytest.mark.db
def test_user_password_ends_the_browsers_own_session_clears_the_lockout_and_audits_cli(
    wired: Wired,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new = "reset-from-the-cli-9"
    auth.ensure_admin(wired.factory, wired.clock, USER, SecretStr(PASSWORD))
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: wired.core)
    app = wired.app()
    with TestClient(app, base_url=BASE) as phone, TestClient(app, base_url=BASE) as laptop:
        login(phone)
        login(laptop)
        assert phone.get("/api/auth/me").status_code == 200
        with wired.factory() as s:
            user = s.execute(select(m.User)).scalar_one()
            user.failed_logins, user.locked_until = 5, NOW + timedelta(minutes=15)
            s.commit()
        result = runner.invoke(cli_app, ["user-password"], input=f"{new}\n{new}\n")
        assert result.exit_code == 0, result.output
        assert "2 sessions signed out" in result.output
        assert phone.get("/api/auth/me").status_code == 401  # the session making requests ended too
        assert laptop.get("/api/auth/me").status_code == 401
        r = phone.post("/api/auth/login", json={"username": USER, "password": new})
        assert r.status_code == 200, r.text  # the lockout was cleared
        assert phone.post("/api/auth/login", json={"username": USER, "password": PASSWORD}).status_code != 200
    with wired.factory() as s:
        audit = s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.password_reset")).scalar_one()
    assert audit.actor == "cli"
    assert audit.after is not None and audit.after.get("sessions_revoked") == 2
    assert new not in repr(audit.before) + repr(audit.after)


# --- the heartbeat's extra detail ---------------------------------------------------------------------------


@pytest.mark.db
def test_heartbeat_extra_nan_or_a_clash_never_loses_the_beat_or_the_p3_keys(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(et(SAT, 11, 0))
    extra: dict[str, Callable[[], Any]] = {"fn": lambda: {"last_event": "spoofed", "fills_today": 99}}
    deps = dataclasses.replace(Harness(db_factory, clock).deps(), heartbeat_extra=lambda: extra["fn"]())
    w = Worker(deps)
    w._beat("idle")
    hb = _heartbeat(db_factory)
    assert hb is not None and hb.detail["fills_today"] == 0 and hb.detail["last_event"] != "spoofed"

    # json.dumps accepts NaN, PostgreSQL jsonb does not: the beat itself must still be written
    extra["fn"] = lambda: {"rate_limit": {"market_data": float("nan")}}
    clock.advance(timedelta(seconds=30))
    w._beat("idle")
    hb = _heartbeat(db_factory)
    assert hb is not None and hb.beat_at == clock.now(), "a NaN in heartbeat_extra lost the heartbeat"
    assert "rate_limit" not in hb.detail

    def boom() -> dict[str, Any]:
        raise ZeroDivisionError

    extra["fn"] = boom
    clock.advance(timedelta(seconds=30))
    w._beat("stopping")
    hb = _heartbeat(db_factory)
    assert hb is not None and hb.phase == "stopping" and hb.beat_at == clock.now()


@pytest.mark.db
async def test_rate_limit_appears_only_once_the_one_shared_client_is_open(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lazies: list[rt.LazyQuestrade] = []
    real_lazy = rt.LazyQuestrade

    class Recording(real_lazy):  # type: ignore[valid-type,misc]
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            lazies.append(self)

    monkeypatch.setattr(rt, "LazyQuestrade", Recording)
    world.qt.qt.rate_limit_remaining = {"market_data": 19, "account": 28}  # type: ignore[attr-defined]
    seen: list[Any] = []

    async def capture(self: Worker, stop: Any, *, once: bool = False) -> None:
        fn = self.deps.heartbeat_extra
        assert fn is not None
        seen.append(dict(fn()))
        await lazies[0].client()
        seen.append(dict(fn()))

    monkeypatch.setattr(Worker, "run", capture)
    assert await rt.run_worker(once=True) == 0
    assert len(lazies) == 1, "the worker built more than one Questrade client"
    assert world.qt.entered == 1
    # DB-T10 adds the tap's and the publisher's health keys beside rate_limit (S10)
    rate_limits = [{k: v for k, v in s.items() if k == "rate_limit"} for s in seen]
    assert rate_limits == [{}, {"rate_limit": {"market_data": 19, "account": 28}}]


# --- the notifier through to_thread -------------------------------------------------------------------------


@pytest.mark.db
async def test_the_api_and_the_worker_notifiers_send_a_key_exactly_once_concurrently(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(NOW)
    api_side, worker_side = FakeTelegramApi(), FakeTelegramApi()
    notifiers = [
        TelegramNotifier(api_side, 42, db_factory, clock),
        TelegramNotifier(worker_side, 42, db_factory, clock),
    ]
    msg = OutboundMessage(kind="reply", text="proposal 1", dedupe_key="proposal:1")
    await asyncio.gather(*(notifiers[i % 2].send(msg) for i in range(12)))
    sends = len(api_side.calls_of("send_message")) + len(worker_side.calls_of("send_message"))
    assert sends == 1
    with db_factory() as s:
        rows = (
            s.execute(select(m.Notification).where(m.Notification.dedupe_key == "proposal:1")).scalars().all()
        )
    assert [r.status for r in rows] == ["sent"]


async def _max_gap_while(work: Awaitable[Any]) -> float:
    """Run `work` beside a 10 ms ticker; return the longest gap between ticks (a blocked loop shows up)."""
    ticks: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        while not done.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    try:
        await work
    finally:
        done.set()
        await t
    return max(b - a for a, b in zip(ticks, ticks[1:], strict=False))


@pytest.mark.db
async def test_a_slow_database_never_stalls_the_event_loop(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notifier's DB steps run in a thread (P4-T18); so must the API's own settings reads (the quote
    cache's TTL, read on every quote request, through `QuietSettings`)."""
    notifier = TelegramNotifier(FakeTelegramApi(), 42, db_factory, FixedClock(NOW))

    def slow_claim(msg: OutboundMessage) -> tuple[int, int] | None:
        time.sleep(0.4)
        return (1, 1)

    notifier._claim = slow_claim  # type: ignore[method-assign]
    notifier._record_success = lambda *a: time.sleep(0.4)  # type: ignore[method-assign]
    gap = await _max_gap_while(notifier.send(OutboundMessage(kind="reply", text="x", dedupe_key="slow")))
    assert gap < 0.2, f"the notifier blocked the loop for {gap:.2f}s"

    core = test_core(db_factory, FixedClock(NOW))
    monkeypatch.setattr(rt, "questrade_client", lambda core: _TrackedQt(FakeQuestrade()))
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        s.commit()
    async with AsyncExitStack() as stack:
        services = await build_services(core, stack)
        assert services.quotes is not None
        real_load = core.settings.load

        def slow_load() -> RuntimeSettings:
            time.sleep(0.4)
            return real_load()

        monkeypatch.setattr(core.settings, "load", slow_load)
        gap = await _max_gap_while(services.quotes([sym]))
    assert gap < 0.2, f"the API's quote path read settings on the event loop ({gap:.2f}s stall)"


# --- the sweeps are not vacuous -----------------------------------------------------------------------------


@pytest.mark.db
def test_the_route_sweeps_catch_a_route_without_a_session_or_without_csrf(wired: Wired) -> None:  # noqa: F811
    auth.ensure_admin(wired.factory, wired.clock, USER, SecretStr(PASSWORD))
    app = wired.app()
    hits = {"open": 0, "nocsrf": 0}

    def leaky() -> dict[str, str]:
        hits["open"] += 1
        return {"ok": "yes"}

    def no_csrf(user: CurrentUser) -> dict[str, str]:
        hits["nocsrf"] += 1
        return {"ok": "yes"}

    app.add_api_route("/api/zz-leak/{proposal_id}", leaky, methods=["GET"])
    app.add_api_route("/api/zz-change", no_csrf, methods=["POST"])
    for _ in range(2):  # first in the table, so no catch-all answers for them
        app.router.routes.insert(0, app.router.routes.pop())

    unauth: list[str] = []
    nocsrf: list[str] = []
    with TestClient(app, base_url=BASE, raise_server_exceptions=False) as client:
        for path, methods in api_routes(app):  # the sweep of test 1, as written in test_routes_sweep.py
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                if (method, path) in sweep.PUBLIC:
                    continue
                body = {} if method in ("POST", "PUT", "DELETE") else None
                r = client.request(method, fill_path(path), json=body)
                if r.status_code != 401:
                    unauth.append(f"{method} {path}")
        login(client)
        for path, methods in api_routes(app):  # the sweep of test 2
            for method in sorted(methods & {"POST", "PUT", "DELETE"}):
                if (method, path) in sweep.PUBLIC:
                    continue
                r = client.request(method, fill_path(path), json={})
                if r.status_code != 403:
                    nocsrf.append(f"{method} {path}")
    assert hits["open"] >= 1 and hits["nocsrf"] >= 1, hits  # the dummy handlers really ran
    assert unauth == ["GET /api/zz-leak/{proposal_id}"], unauth
    assert nocsrf == ["POST /api/zz-change"], nocsrf


def test_the_ts_mirror_catches_nullability_and_optionality_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tc.TYPES_TS.read_text(encoding="utf-8")
    check = tc.test_every_schema_model_is_mirrored_with_the_same_fields_and_nullability
    mutations = [
        (
            schemas.JobLaunchOut,
            "(token-refresh, or the command's own default session). */\n  session_date: IsoDate | null;",
            "(token-refresh, or the command's own default session). */\n  session_date: IsoDate;",
        ),
        (
            schemas.ProposalOut,
            "export interface ProposalOut {\n  id: number;",
            "export interface ProposalOut {\n  id: number | null;",
        ),
        (
            schemas.ProposalOut,
            "export interface ProposalOut {\n  id: number;",
            "export interface ProposalOut {\n  id?: number;",
        ),
    ]
    copy = tmp_path / "types.ts"
    for model, before, after in mutations:
        assert original.count(before) == 1, before
        copy.write_text(original.replace(before, after), encoding="utf-8")
        monkeypatch.setattr(tc, "TYPES_TS", copy)
        with pytest.raises(AssertionError):
            check(model)
    copy.write_text(original, encoding="utf-8")  # the unmutated copy passes
    for model in (schemas.JobLaunchOut, schemas.ProposalOut):
        check(model)


def test_the_proposal_service_scan_catches_direct_and_aliased_constructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_api = tmp_path / "api"
    fake_api.mkdir()
    (fake_api / "services.py").write_text("from trader.runtime import build_decider\n")
    monkeypatch.setattr(sweep, "API_DIR", fake_api)
    scan = sweep.test_no_proposal_service_is_constructed_in_the_api
    scan()  # a clean package passes
    synthetic = {
        "direct.py": "from trader.engine.proposals import ProposalService\nsvc = ProposalService(1, 2)\n",
        "attribute.py": "import trader.engine.proposals as p\nsvc = p.ProposalService (1, 2)\n",
        "aliased.py": "from trader.engine.proposals import ProposalService as Decider\nsvc = Decider(1, 2)\n",
    }
    missed: list[str] = []
    for name, source in synthetic.items():
        bad = fake_api / name
        bad.write_text(source)
        try:
            scan()
        except AssertionError:
            pass
        else:
            missed.append(name)
        bad.unlink()
    assert not missed, f"the ProposalService scan missed: {missed}"
