"""P4-T18 acceptance tests 1, 2 and 4, plus the wiring checks from the Phase 4 build notes.

The real app (`create_app`) over the real `build_services` on a test Core: Questrade and Telegram are the
fakes, entered through the monkeypatched `trader.runtime` builders, so nothing touches the network.

- 1: every `/api` route except login, health and meta answers 401 without a session (Review Focus 2).
- 2: with a real session, every POST/PUT/DELETE except login answers 403 without `X-CSRF-Token`; a signed-in
  user's wrong password or code on `/auth/password` and `/auth/totp/*` is never a 401 (the web client treats
  a 401 outside login as a lost session).
- 4: a web approval goes through the real `runtime.build_decider` (the kill-switch entry guard).
- No `ProposalService` is constructed anywhere in `trader/api` (contract refinement 3).
"""

import ast
import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import String, select
from sqlalchemy.orm import Session, sessionmaker

import trader.runtime as rt
from tests.factories import add_strategy_config, add_symbol
from tests.fakes_api import test_core
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_telegram import FakeTelegramApi
from trader.adapters.questrade.auth import QuestradeAuth
from trader.api import auth
from trader.api.deps import ApiServices
from trader.api.feed import PollingChangeFeed
from trader.api.launcher import SubprocessJobLauncher
from trader.api.main import create_app
from trader.api.quotes import CachedQuotes
from trader.api.routers import meta, performance
from trader.api.schemas import MetricsOut
from trader.api.services import build_services
from trader.bootstrap import Core
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.db.session import make_engine, make_session_factory
from trader.engine.killswitch import KillSwitches
from trader.engine.proposals import ProposalService
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.market.clock import ET, FixedClock
from trader.notify.notifier import TelegramNotifier
from trader.settings_store import RuntimeSettings
from trader.strategies.base import EnterLong

pytestmark = pytest.mark.db

DAY = date(2026, 10, 6)  # a Tuesday session
NOW = datetime(2026, 10, 6, 9, 40, tzinfo=ET).astimezone(UTC)
BASE = "https://testserver"
USER = "stephen"
PASSWORD = "correct-horse-battery"  # noqa: S105 (a throwaway test password)
PUBLIC = {("GET", "/api/health"), ("GET", "/api/meta"), ("POST", "/api/auth/login")}
API_DIR = Path(__file__).resolve().parents[2] / "trader" / "api"


class _QtContext:
    def __init__(self, qt: FakeQuestrade) -> None:
        self.qt = qt
        self.entered = 0

    async def __aenter__(self) -> FakeQuestrade:
        self.entered += 1
        return self.qt

    async def __aexit__(self, *exc: object) -> None:
        return None


@dataclass
class Wired:
    core: Core
    clock: FixedClock
    api: FakeTelegramApi
    qt: _QtContext
    dist: Path

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory

    def app(self) -> Any:
        async def factory(stack: AsyncExitStack) -> ApiServices:
            return await build_services(self.core, stack)

        return create_app(services_factory=factory, web_dist=self.dist)


@pytest.fixture
def wired(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Wired:
    clock = FixedClock(NOW)
    core = test_core(
        db_factory,
        clock,
        telegram_bot_token=SecretStr("123456:TEST-TOKEN-NOT-REAL"),
        telegram_chat_id=4242,
        admin_username=USER,
        admin_password_initial=SecretStr(PASSWORD),
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>")
    w = Wired(core, clock, FakeTelegramApi(), _QtContext(FakeQuestrade()), dist)
    monkeypatch.setattr(rt, "build_telegram_api", lambda env: w.api)
    monkeypatch.setattr(rt, "questrade_client", lambda core: w.qt)
    return w


def api_routes(app: Any) -> list[tuple[str, set[str]]]:
    """Every `/api` APIRoute with its full path. FastAPI 0.141 keeps an included router as one
    `_IncludedRouter` entry in `app.routes`, so walk into `original_router` under its include prefix."""
    out: list[tuple[str, set[str]]] = []

    def walk(routes: Any, prefix: str) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                if (prefix + route.path).startswith("/api"):
                    out.append((prefix + route.path, set(route.methods or ())))
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes, prefix + route.include_context.prefix)

    walk(app.routes, "")
    return out


def fill_path(path: str) -> str:
    """Path parameters filled with valid-looking values."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1).split(":")[0]
        if "date" in name:
            return DAY.isoformat()
        if name == "key":
            return "approval_mode"
        if name == "switch":
            return "max_drawdown_pct"
        if name == "job":
            return "nightly"
        return "1"

    return re.sub(r"\{([^}]+)\}", value, path)


def login(client: TestClient) -> str:
    """Log in as the env admin; the TestClient keeps the cookie. Returns the CSRF token."""
    r = client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token: str = r.json()["csrf_token"]
    return token


# --- the composition ----------------------------------------------------------------------------------------


def test_build_services_wires_the_real_pieces_and_opens_nothing_on_the_network(wired: Wired) -> None:
    app = wired.app()
    with TestClient(app, base_url=BASE):
        services: ApiServices = app.state.services
        assert isinstance(services.credentials, QuestradeAuth)
        assert isinstance(services.notifier, TelegramNotifier)
        assert services.telegram_configured is True
        assert isinstance(services.quotes, CachedQuotes)
        assert services.candles is not None
        assert isinstance(services.jobs, SubprocessJobLauncher)
        assert isinstance(services.feed, PollingChangeFeed)
        assert services.plan(DAY).session_date == DAY
        assert services.fired(DAY) == set()
        with wired.factory() as s:  # the strategy defaults were ensured once
            assert s.execute(select(m.StrategyConfig)).first() is not None
        assert wired.qt.entered == 0  # the Questrade client connects on the first quote only


async def test_the_questrade_client_opens_on_the_first_quote(wired: Wired) -> None:
    async with AsyncExitStack() as stack:
        services = await build_services(wired.core, stack)
        assert wired.qt.entered == 0
        assert services.quotes is not None
        await services.quotes([])
        with wired.factory() as s:
            sym = add_symbol(s, "AAA", questrade_id=11)
            s.commit()
        await services.quotes([sym])
        assert wired.qt.entered == 1


async def test_without_telegram_the_notifier_is_null_and_no_client_is_built(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_client(env: Any) -> Any:
        raise AssertionError("no Telegram client without configuration")

    monkeypatch.setattr(rt, "build_telegram_api", no_client)
    core = test_core(db_factory, FixedClock(NOW))
    async with AsyncExitStack() as stack:
        services = await build_services(core, stack)
    assert services.telegram_configured is False
    assert not isinstance(services.notifier, TelegramNotifier)


# --- 1. every route needs a session -------------------------------------------------------------------------


def test_every_api_route_except_the_public_three_is_401_without_a_session(wired: Wired) -> None:
    app = wired.app()
    checked: list[tuple[str, str]] = []
    failures: list[str] = []
    with TestClient(app, base_url=BASE, raise_server_exceptions=False) as client:
        routes = api_routes(app)
        for path, methods in routes:
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                if (method, path) in PUBLIC:
                    continue
                body: dict[str, Any] | None = {} if method in ("POST", "PUT", "DELETE") else None
                r = client.request(method, fill_path(path), json=body)
                checked.append((method, path))
                if r.status_code != 401 or r.json()["error"]["code"] != "unauthorized":
                    failures.append(f"{method} {path} -> {r.status_code}")
        assert client.get("/api/meta").status_code == 200
        assert client.get("/api/health").status_code in (200, 503)
    assert not failures, failures
    assert len(checked) >= 30, checked  # the whole table was walked, not an empty one
    public = {(meth, p) for p, methods in routes for meth in methods} & PUBLIC
    assert public == PUBLIC


# --- 2. CSRF on every change --------------------------------------------------------------------------------


def test_every_change_route_needs_the_csrf_header(wired: Wired) -> None:
    auth.ensure_admin(wired.factory, wired.clock, USER, SecretStr(PASSWORD))
    app = wired.app()
    failures: list[str] = []
    checked = 0
    with TestClient(app, base_url=BASE, raise_server_exceptions=False) as client:
        login(client)
        for path, methods in api_routes(app):
            for method in sorted(methods & {"POST", "PUT", "DELETE"}):
                if (method, path) in PUBLIC:
                    continue
                r = client.request(method, fill_path(path), json={})
                checked += 1
                if r.status_code != 403 or r.json()["error"]["code"] != "csrf":
                    failures.append(f"{method} {path} -> {r.status_code} {r.text[:80]}")
                # a wrong token is refused the same way
                r = client.request(method, fill_path(path), json={}, headers={"X-CSRF-Token": "wrong"})
                if r.status_code != 403:
                    failures.append(f"{method} {path} (wrong token) -> {r.status_code}")
        assert client.get("/api/auth/me").status_code == 200  # the session survived the sweep
    assert not failures, failures
    assert checked >= 15


def test_a_wrong_password_or_code_of_a_signed_in_user_is_never_401(wired: Wired) -> None:
    auth.ensure_admin(wired.factory, wired.clock, USER, SecretStr(PASSWORD))
    app = wired.app()
    wrong = "not-the-password-1"
    calls = [
        ("PUT", "/api/auth/password", {"current_password": wrong, "new_password": "another-password-2"}),
        ("POST", "/api/auth/totp/setup", {"password": wrong}),
        ("POST", "/api/auth/totp/confirm", {"code": "123456"}),
        ("POST", "/api/auth/totp/disable", {"password": wrong, "code": "123456"}),
    ]
    with TestClient(app, base_url=BASE, raise_server_exceptions=False) as client:
        csrf = login(client)
        routes = {p for p, methods in api_routes(app) if p.startswith("/api/auth/")}
        assert {"/api/auth/password", "/api/auth/totp/setup", "/api/auth/totp/confirm"} <= routes
        for method, path, body in calls:
            r = client.request(method, path, json=body, headers={"X-CSRF-Token": csrf})
            assert r.status_code in (403, 409, 422), (path, r.status_code)
            assert r.status_code != 401
            assert wrong not in r.text
        assert client.get("/api/auth/me").status_code == 200


# --- 4. the web approve path end to end ---------------------------------------------------------------------


def seed_entries(core: Core, tickers: tuple[str, ...]) -> tuple[int, list[int]]:
    """One pending entry proposal of the live run per ticker, created by the engine's own (unguarded)
    service: creating is never guarded, only deciding. Returns (run id, proposal ids)."""
    run = get_live_run(core.factory, core.clock, RuntimeSettings())
    broker = rt.build_sim_broker(core, run.id, RuntimeSettings())
    svc = ProposalService(core.factory, core.clock, core.settings, broker, run.id)
    ids: list[int] = []
    with core.factory() as s:
        cfg = add_strategy_config(s)
        s.commit()
    for n, ticker in enumerate(tickers):
        with core.factory() as s:
            sym = add_symbol(s, ticker, questrade_id=11 + n)
            sig = m.Signal(
                run_id=run.id,
                strategy_config_id=cfg,
                symbol_id=sym,
                session_date=DAY,
                event_key="orb_open",
                ts=core.clock.now(),
                intent={"reason": "orb_breakout"},
                evidence={},
            )
            s.add(sig)
            s.commit()
            signal_id = sig.id
        intent = EnterLong(sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
        spec = OrderSpec(
            sym, "buy", "stop", 10, stop=Decimal("20.01"), stop_loss=Decimal("19.91"),
            strategy_config_id=cfg, reason="orb_breakout",
        )  # fmt: skip
        sized = SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10", "per_share_risk": "0.10"})
        ids.append(svc.create(signal_id, sized, "entry").id)
    return run.id, ids


def test_a_web_approval_submits_and_a_paused_one_is_blocked(wired: Wired) -> None:
    auth.ensure_admin(wired.factory, wired.clock, USER, SecretStr(PASSWORD))
    run_id, (first, second) = seed_entries(wired.core, ("AAA", "BBB"))
    app = wired.app()
    with TestClient(app, base_url=BASE) as client:
        csrf = login(client)
        r = client.post(f"/api/proposals/{first}/approve", headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["proposal"]["status"] == "submitted"
        assert body["proposal"]["decided_via"] == "web"
        assert body["proposal"]["decided_by"] == f"web:{USER}"
        assert body["blocked"] is None

        KillSwitches(wired.factory, wired.clock).pause(run_id, DAY, "test")
        r = client.post(f"/api/proposals/{second}/approve", headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["blocked"] == "kill switch manual_pause is tripped"
        assert body["message"] == "Entry blocked: kill switch manual_pause is tripped"
        assert body["proposal"]["status"] == "rejected"
    with wired.factory() as s:
        orders = s.execute(select(m.Order).where(m.Order.run_id == run_id)).scalars().all()
    assert [o.proposal_id for o in orders] == [first]  # the blocked entry placed nothing


def test_the_services_decider_is_the_guarded_runtime_decider(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[int] = []
    real = rt.build_decider

    def spy(core: Core, run_id: int, **kw: Any) -> Any:
        built.append(run_id)
        return real(core, run_id, **kw)

    monkeypatch.setattr(rt, "build_decider", spy)
    app = wired.app()
    with TestClient(app, base_url=BASE):
        app.state.services.decider_for(7)
    assert built == [7]


# --- the grep checks ----------------------------------------------------------------------------------------


def test_no_proposal_service_is_constructed_in_the_api() -> None:
    """Every ProposalService of the API comes from `runtime.build_decider` (the kill-switch entry guard)."""
    offenders: list[str] = []
    for path in sorted(API_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "ProposalService(" in source:
            offenders.append(str(path.relative_to(API_DIR)))
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == (
                "ProposalService"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders
    assert "build_decider" in (API_DIR / "services.py").read_text(encoding="utf-8")


# --- build notes: username length, histogram, health timeout ------------------------------------------------


def test_the_longest_allowed_username_fits_the_audit_actor_column() -> None:
    longest = "a" * 46
    assert re.fullmatch(auth.USERNAME_PATTERN, longest)
    assert not re.fullmatch(auth.USERNAME_PATTERN, "a" * 47)
    actor_type = m.AuditLog.__table__.c.actor.type
    uploaded_by_type = m.ManualWatchlist.__table__.c.uploaded_by.type
    assert isinstance(actor_type, String) and isinstance(uploaded_by_type, String)
    assert actor_type.length is not None and uploaded_by_type.length is not None
    assert len(f"web:{longest}") <= min(actor_type.length, uploaded_by_type.length) == 50


def test_ensure_admin_rejects_a_47_character_username(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    assert auth.ensure_admin(db_factory, clock, "a" * 47, SecretStr(PASSWORD)) == "rejected"
    assert auth.ensure_admin(db_factory, clock, "a" * 46, SecretStr(PASSWORD)) == "created"


def test_metrics_round_trip_with_the_open_histogram_bins(db_factory: sessionmaker[Session]) -> None:
    run_id = get_live_run(db_factory, FixedClock(NOW), RuntimeSettings()).id
    out = performance.compute_metrics(db_factory, run_id, None, None)
    first, last = out.r_histogram[0], out.r_histogram[-1]
    assert first.lo == Decimal("-Infinity") and last.hi == Decimal("Infinity")
    again = MetricsOut.model_validate_json(out.model_dump_json())
    assert again == out
    dumped = out.model_dump(mode="json")
    assert dumped["r_histogram"][0]["lo"] == "-Infinity"
    assert dumped["r_histogram"][-1]["hi"] == "Infinity"
    source = Path(performance.__file__).read_text(encoding="utf-8")
    assert "model_construct" not in source  # T7's workaround is gone


def test_the_metrics_route_returns_its_response_model(wired: Wired) -> None:
    auth.ensure_admin(wired.factory, wired.clock, USER, SecretStr(PASSWORD))
    app = wired.app()
    with TestClient(app, base_url=BASE) as client:
        login(client)
        r = client.get("/api/metrics")
    assert r.status_code == 200, r.text
    bins = r.json()["r_histogram"]
    assert len(bins) == 18
    assert bins[0]["lo"] == "-Infinity" and bins[-1]["hi"] == "Infinity"
    MetricsOut.model_validate(r.json())


@pytest.fixture
def silent_server() -> Iterator[int]:
    """A TCP port that accepts connections but never answers (a hung database)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    try:
        yield int(server.getsockname()[1])
    finally:
        server.close()


def test_the_health_database_check_fails_fast_on_a_database_that_never_answers(silent_server: int) -> None:
    engine = make_engine(f"postgresql+psycopg://nobody:nothing@127.0.0.1:{silent_server}/nowhere")
    factory = make_session_factory(engine)
    outcome: list[BaseException | int] = []

    def run() -> None:
        try:
            outcome.append(meta.db_check(factory))
        except BaseException as exc:
            outcome.append(exc)

    t0 = time.monotonic()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=15)
    elapsed = time.monotonic() - t0
    engine.dispose()
    assert not worker.is_alive(), "the health check hung on an unanswering database"
    assert outcome and isinstance(outcome[0], Exception)
    assert elapsed < meta.DB_CONNECT_TIMEOUT_S + 5
