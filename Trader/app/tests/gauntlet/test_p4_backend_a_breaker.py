"""P4-BA gauntlet (attempt 1): Breaker tests for Phase 4 backend group A.

P4-T3 (API core), P4-T5 (dashboard and trading reads), P4-T7 (performance, journal, CSV export), P4-T8
(settings and strategies) and P4-T11 (change feed and SSE). Real PostgreSQL (testcontainers) and TestClient;
a real uvicorn server only where the behaviour lives in the server (SSE headers through the full middleware
stack and shutdown).
"""

import asyncio
import csv
import io
import re
import signal
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
import uvicorn
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import DEFAULT_USER, make_client
from tests.api.test_trading import seed_closed_trade, seed_open_position
from tests.factories import add_run
from tests.fakes_api import FakeFeed, fake_candles, fake_quotes, make_services, test_core
from trader.api.deps import ApiServices, AuthUser, current_user
from trader.api.errors import ApiError
from trader.api.main import create_app
from trader.api.routers import dashboard, journal, performance, settings, strategies, stream, trading
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import FixedClock, RealClock
from trader.reports.export import TRADE_CSV_COLUMNS, trades_csv
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # Tuesday 10:00 ET
DAY = date(2026, 10, 6)
BASE = "https://testserver"
SECURITY = {
    "content-security-policy": "default-src 'self'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
}
PUBLIC = {("GET", "/api/health"), ("GET", "/api/meta"), ("POST", "/api/auth/login")}
SENTINEL = "ECHO-7f3a<script>"


def _services(factory: sessionmaker[Session], clock: Any = None, **overrides: Any) -> ApiServices:
    return make_services(test_core(factory, clock or FixedClock(NOW)), **overrides)


def _live(factory: sessionmaker[Session]) -> int:
    return get_live_run(factory, FixedClock(NOW), RuntimeSettings()).id


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>")
    (dist / "assets" / "app.123.js").write_text("console.log(1)")
    return dist


def _app(tmp_path: Path, services: ApiServices) -> Any:
    async def factory(stack: AsyncExitStack) -> ApiServices:
        return services

    return create_app(services_factory=factory, web_dist=_dist(tmp_path))


def _assert_security_headers(resp: httpx.Response, where: str) -> None:
    for name, value in SECURITY.items():
        assert value in resp.headers.get(name, ""), f"{where}: missing {name}"
    assert resp.headers.get("x-request-id"), f"{where}: no X-Request-ID"


# --- T3 / T18 sweep: every route needs a session ------------------------------------------------------------


def _fill_path(path: str) -> str:
    def value(match: re.Match[str]) -> str:
        name = match.group(1).split(":")[0]
        if "date" in name:
            return "2026-10-06"
        if name in ("key",):
            return "approval_mode"
        return "1"

    return re.sub(r"\{([^}]+)\}", value, path)


def _api_routes(app: Any) -> list[tuple[str, set[str]]]:
    """Every `/api` APIRoute with its full path. FastAPI 0.141 keeps an included router as one
    `_IncludedRouter` entry in `app.routes` (not its APIRoutes), so walk into it."""
    out: list[tuple[str, set[str]]] = []

    def walk(routes: Any, prefix: str) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                if (prefix + route.path).startswith("/api"):
                    out.append((prefix + route.path, set(route.methods)))
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes, prefix + route.include_context.prefix)

    walk(app.routes, "")
    return out


@pytest.mark.db
def test_every_api_route_without_a_session_is_401(db_factory: sessionmaker[Session], tmp_path: Path) -> None:
    """Walk the real app's route table: every /api route except health, meta and login answers 401 without a
    cookie (SPEC §11, §14; Global Constraints). Bodies are sent empty so no route can 422 first."""
    app = _app(tmp_path, _services(db_factory))
    checked: list[tuple[str, str]] = []
    failures: list[str] = []
    with TestClient(app, base_url=BASE, raise_server_exceptions=False) as client:
        for path, methods in _api_routes(app):
            for method in sorted(methods):
                if (method, path) in PUBLIC:
                    continue
                url = _fill_path(path)
                resp = client.request(method, url, json={} if method in ("POST", "PUT", "DELETE") else None)
                checked.append((method, path))
                if resp.status_code != 401:
                    failures.append(f"{method} {path} -> {resp.status_code}")
                else:
                    assert resp.json()["error"]["code"] == "unauthorized"
        # the public three really are public
        assert client.get("/api/meta").status_code == 200
        assert client.get("/api/health").status_code in (200, 503)
    assert not failures, failures
    # group A's routes are all in the sweep
    for must in [
        ("GET", "/api/dashboard"),
        ("GET", "/api/positions/{position_id}"),
        ("GET", "/api/trades"),
        ("GET", "/api/export/trades.csv"),
        ("PUT", "/api/journal/{session_date}"),
        ("PUT", "/api/settings/{key}"),
        ("PUT", "/api/strategies/{key}"),
        ("GET", "/api/stream"),
    ]:
        assert must in checked, must


# --- T3: the SPA never serves outside dist; HEAD behaves like GET ------------------------------------------


def test_spa_traversal_encodings_symlinks_hidden_files_and_head(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    services = make_services(test_core(_unreachable_factory(), FixedClock(NOW)))
    app = _app(tmp_path, services)
    dist = tmp_path / "dist"
    (dist / "leak.txt").symlink_to(secret)
    (dist / "assets" / "leak.js").symlink_to(secret)
    (dist / "assets" / "outside").symlink_to(tmp_path, target_is_directory=True)
    (dist / ".env").write_text("ENVSECRET")
    (dist / "assets" / ".env").write_text("ENVSECRET")
    paths = [
        "/%2e%2e/secret.txt",
        "/%2E%2E/%2E%2E/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/..%5csecret.txt",
        "/assets/..%5c..%5csecret.txt",
        "/%252e%252e/secret.txt",
        "/leak.txt",
        "/assets/leak.js",
        "/assets/outside/secret.txt",
        "/.env",
        "/assets/.env",
        "/assets/%2e%2e/.env",
        "/%00",
        "/assets/%00",
    ]
    with TestClient(app, base_url=BASE, raise_server_exceptions=False) as client:
        for path in paths:
            for method in ("GET", "HEAD"):
                resp = client.request(method, path)
                assert resp.status_code in (200, 404), (method, path, resp.status_code)
                assert "TOPSECRET" not in resp.text and "ENVSECRET" not in resp.text, (method, path)
                _assert_security_headers(resp, f"{method} {path}")
        # HEAD is GET without the body: same status and headers, no body
        for path in ("/dashboard?proposal=5", "/assets/app.123.js", "/trades"):
            get, head = client.get(path), client.head(path)
            assert get.status_code == head.status_code == 200, path
            assert head.content == b"", path
            assert head.headers.get("content-length") == get.headers.get("content-length"), path
            assert head.headers.get("cache-control") == get.headers.get("cache-control"), path
            _assert_security_headers(head, f"HEAD {path}")
        # HEAD of an unknown /api path is never the SPA
        head = client.head("/api/nope")
        assert head.status_code in (404, 405) and "text/html" not in head.headers.get("content-type", "")


def _unreachable_factory() -> sessionmaker[Session]:
    from trader.db.session import make_engine, make_session_factory

    return make_session_factory(make_engine("postgresql+psycopg://u:p@127.0.0.1:1/none"))


# --- T3: the request log and headers on error responses -------------------------------------------------


@pytest.mark.db
def test_request_log_has_no_query_cookie_or_auth_and_errors_carry_security_headers(
    db_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    app = _app(tmp_path, _services(db_factory))

    def boom() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/api/test-boom", boom, methods=["GET"])
    secret_headers = {"Authorization": "Bearer hdrsecret123", "Cookie": "trader_session=cookiesecret"}
    requests: list[tuple[str, str, Any, int]] = [
        ("GET", "/api/dashboard?token=querysecret", None, 401),
        ("GET", "/api/nope?token=querysecret", None, 404),
        ("POST", "/api/auth/login?x=querysecret", {"username": ["pwsecret"], "password": 5}, 422),
        ("POST", "/api/meta?x=querysecret", None, 405),
        ("GET", "/api/test-boom?token=querysecret", None, 500),
        ("GET", "/dashboard?token=querysecret", None, 200),
        ("GET", "/api/settings?token=querysecret", None, 401),
    ]
    with (
        structlog.testing.capture_logs() as logs,
        TestClient(app, base_url=BASE, raise_server_exceptions=False) as client,
    ):
        for method, path, body, status in requests:
            resp = client.request(method, path, json=body, headers=secret_headers)
            assert resp.status_code == status, (method, path, resp.status_code)
            _assert_security_headers(resp, f"{method} {path}")
            if path.startswith("/api"):
                assert resp.headers.get("cache-control") == "no-store", path
                for secret in ("pwsecret", "querysecret", "cookiesecret", "hdrsecret123"):
                    assert secret not in resp.text, (path, secret)
    lines = [e for e in logs if e.get("event") == "http.request"]
    assert len(lines) == len(requests)
    for line in lines:
        assert "?" not in line["path"] and "query" not in line
        assert set(line) <= {"event", "log_level", "method", "path", "status", "duration_ms", "request_id"}
    dump = repr(logs)
    for secret in ("querysecret", "cookiesecret", "hdrsecret123", "pwsecret", "token="):
        assert secret not in dump, secret


# --- T5: trading reads ----------------------------------------------------------------------------------


def _trading_client(factory: sessionmaker[Session], **overrides: Any) -> TestClient:
    services = _services(factory, **overrides)
    return make_client(
        services,
        dashboard.router,
        trading.router,
        performance.router,
        journal.router,
        raise_server_exceptions=False,
    )


@pytest.mark.db
def test_limit_and_offset_bounds_never_500(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        seed_closed_trade(s, run_id)
        s.commit()
    client = _trading_client(db_factory)
    for path in ("/api/orders", "/api/fills", "/api/trades"):
        assert client.get(path, params={"limit": 0}).status_code == 422, path
        assert client.get(path, params={"limit": 501}).status_code == 422, path
        assert client.get(path, params={"limit": 500, "date": "2026-10-06"}).status_code == 200, path
        assert client.get(path, params={"limit": "1e3"}).status_code == 422, path
    assert client.get("/api/trades", params={"offset": 10**6}).json()["items"] == []
    for offset in (2**63 - 1, 2**63, 10**20):
        resp = client.get("/api/trades", params={"offset": offset})
        assert resp.status_code in (200, 422), (offset, resp.status_code)


@pytest.mark.db
def test_run_ids_and_position_ids_of_other_runs(db_factory: sessionmaker[Session]) -> None:
    """`run` accepts `live` or an existing run id; anything else is a 404, never a 500. A replay run's
    position is not the live run's (404); huge or negative ids are 404."""
    live = _live(db_factory)
    with db_factory() as s:
        mine = seed_closed_trade(s, live, ticker="LIVE")
        replay = add_run(s, mode="replay", status="completed", label="replay 1")
        theirs = seed_closed_trade(s, replay, ticker="RPLY")
        s.commit()
    client = _trading_client(db_factory)
    live_ids = [t["id"] for t in client.get("/api/trades").json()["items"]]
    replay_ids = [t["id"] for t in client.get("/api/trades", params={"run": str(replay)}).json()["items"]]
    assert live_ids == [mine.trade_id] and replay_ids == [theirs.trade_id]
    assert client.get("/api/metrics", params={"run": str(replay)}).json()["trades"] == 1
    for bad in ("0", "999999", "-1", "1.0", " 1", "live ", "LIVE", "9" * 19, "²", "١٢"):
        for path in ("/api/trades", "/api/metrics", "/api/equity", "/api/export/trades.csv"):
            resp = client.get(path, params={"run": bad})
            assert resp.status_code == 404, (path, bad, resp.status_code)
    assert client.get(f"/api/positions/{mine.position_id}").status_code == 200
    for pid in (theirs.position_id, 0, -1, 2**63 - 1, 10**20):
        resp = client.get(f"/api/positions/{pid}")
        assert resp.status_code in (404, 422), (pid, resp.status_code)


@pytest.mark.db
def test_huge_and_edge_date_ranges_never_500(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        seed_closed_trade(s, run_id)
        s.commit()
    client = _trading_client(db_factory)
    wide = {"from": "0001-01-01", "to": "9999-12-31"}
    for path in ("/api/trades", "/api/metrics", "/api/equity", "/api/journal", "/api/export/trades.csv"):
        resp = client.get(path, params=wide)
        assert resp.status_code == 200, (path, resp.status_code)
    assert len(client.get("/api/trades", params=wide).json()["items"]) == 1
    edges = [
        ("/api/candidates", {"date": "0001-01-01"}),
        ("/api/candidates", {"date": "9999-12-31"}),
        ("/api/orders", {"date": "0001-01-01"}),
        ("/api/fills", {"date": "9999-12-31"}),
        ("/api/positions", {"status": "all", "date": "0001-01-01"}),
        ("/api/journal", {"to": "0001-01-02"}),  # no `from`: the default range starts before year 1
        ("/api/journal", {"to": "1900-01-02"}),
        ("/api/journal", {"to": "9999-12-31"}),
        ("/api/metrics", {"from": "9999-12-31"}),
        ("/api/equity", {"to": "0001-01-01"}),
    ]
    for path, params in edges:
        resp = client.get(path, params=params)
        assert resp.status_code in (200, 404, 422), (path, params, resp.status_code)


@pytest.mark.db
def test_chart_and_quote_failures_never_fail_the_detail_page(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        chain = seed_open_position(s, run_id)
        s.commit()

    class _BadCandle:  # a source that returns garbage instead of Candles
        start = "not a time"
        open = high = low = close = None
        volume = -1

    async def garbage(symbol_id: int, start: datetime, end: datetime) -> list[Any]:
        return [_BadCandle()]

    async def failing_quotes(ids: Any) -> Any:
        raise RuntimeError("quote source down: refresh_token=abc123secret")

    client = _trading_client(db_factory, candles=garbage, quotes=failing_quotes)
    resp = client.get(f"/api/positions/{chain.position_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["candles"] == [] and body["chart_error"]
    assert body["position"]["last"] is None
    assert "abc123secret" not in resp.text
    # a source that times out gives its exception type as chart_error
    raising = fake_candles([])
    raising.error = TimeoutError("slow")
    client2 = _trading_client(db_factory, candles=raising)
    assert client2.get(f"/api/positions/{chain.position_id}").json()["chart_error"] == "TimeoutError"
    assert client.get("/api/dashboard").status_code == 200


# --- T7: CSV export -----------------------------------------------------------------------------------------


@pytest.mark.db
def test_csv_guards_every_text_column_and_never_prefixes_numbers(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    cases = [
        ("=CMD", "orb_sip", "-1+1", "-10.0000"),
        ("@SUM", "+plus", "\tTAB", "5.0000"),
        ("-MINUS", "=eq", "\r=cr", "-0.5000"),
        ("+PLUS", "@at", "=HYPERLINK(1)", "0.0000"),
    ]
    with db_factory() as s:
        for ticker, strategy, reason, pnl in cases:
            chain = seed_closed_trade(s, run_id, ticker=ticker, strategy=strategy, pnl=pnl)
            s.execute(update(m.Trade).where(m.Trade.id == chain.trade_id).values(exit_reason=reason))
        s.commit()
    client = _trading_client(db_factory)
    resp = client.get("/api/export/trades.csv")
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.text, newline="")))
    assert tuple(rows[0]) == TRADE_CSV_COLUMNS and len(rows) == len(cases)
    by_ticker = {r["ticker"]: r for r in rows}
    for ticker, strategy, reason, pnl in cases:
        row = by_ticker.get("'" + ticker)
        assert row is not None, (ticker, list(by_ticker))
        assert row["strategy"] == "'" + strategy if strategy[0] in "=+-@" else row["strategy"] == strategy
        assert row["exit_reason"] == "'" + reason, (reason, row["exit_reason"])
        assert row["pnl"] == pnl  # numbers are never prefixed, even negative ones
        assert row["entry_price"] == "21.5600" and row["pnl_r"] == "2.4500"
        for col in ("qty", "entry_price", "exit_price", "pnl", "pnl_r", "fees_total", "slippage_total"):
            assert not row[col].startswith("'"), col


@pytest.mark.db
def test_csv_export_streams_from_a_server_side_cursor(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        for i in range(3):
            seed_closed_trade(s, run_id, ticker=f"T{i}")
        s.commit()
    engine = db_factory.kw["bind"]
    seen: list[bool] = []

    def spy(conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool) -> None:
        if "trader.trades" in statement:
            # psycopg 3 runs a server-side (named) cursor as a `ServerCursor`
            seen.append(
                bool(getattr(context, "is_server_side", False)) or "ServerCursor" in type(cursor).__name__
            )

    event.listen(engine, "before_cursor_execute", spy)
    lines = trades_csv(db_factory, run_id, None, None)
    try:
        assert next(lines).startswith("trade_id,")
        assert seen == []  # the header is sent before any query
        next(lines)
        assert seen == [True], "the export must read through a server-side cursor (yield_per)"
        rest = list(lines)
        assert len(rest) == 2
    finally:
        lines.close()  # an unfinished export must not keep its transaction (and locks) open
        event.remove(engine, "before_cursor_execute", spy)


# --- T7: journal PUT ----------------------------------------------------------------------------------------


@pytest.mark.db
def test_journal_put_changes_only_sent_fields_refuses_non_sessions_and_audits(
    db_factory: sessionmaker[Session],
) -> None:
    run_id = _live(db_factory)
    day = date(2026, 10, 5)
    with db_factory() as s:
        s.add(
            m.Journal(
                run_id=run_id,
                session_date=day,
                rules_followed=True,
                notes=None,
                answered_via="telegram",
                updated_at=NOW - timedelta(days=1),
            )
        )
        s.commit()
    client = _trading_client(db_factory)
    r1 = client.put(f"/api/journal/{day}", json={"notes": "late entry"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["rules_followed"] is True and r1.json()["answered_via"] == "telegram"
    r2 = client.put(f"/api/journal/{day}", json={"rules_followed": None})
    assert r2.status_code == 200
    assert r2.json()["rules_followed"] is None and r2.json()["notes"] == "late entry"
    assert r2.json()["answered_via"] == "web"
    for bad in ("2026-10-03", "2026-10-04", "2026-11-26", "2026-10-07", "1900-01-02", "0001-01-01"):
        resp = client.put(f"/api/journal/{bad}", json={"notes": "x"})
        assert resp.status_code == 422, (bad, resp.status_code)
    assert client.put(f"/api/journal/{day}", json={"notes": "n" * 5001}).status_code == 422
    assert client.put(f"/api/journal/{day}", json={"rules_followed": "maybe"}).status_code == 422
    with db_factory() as s:
        audits = (
            s.execute(select(m.AuditLog).where(m.AuditLog.action == "journal.update").order_by(m.AuditLog.id))
            .scalars()
            .all()
        )
        row = s.get(m.Journal, (run_id, day))
    assert len(audits) == 2
    assert all(a.actor == "web:stephen" for a in audits)
    assert audits[0].before["rules_followed"] is True and audits[0].before["answered_via"] == "telegram"
    assert audits[0].after["notes"] == "late entry" and audits[0].after["rules_followed"] is True
    assert audits[1].before == audits[0].after
    assert audits[1].after["rules_followed"] is None and audits[1].after["notes"] == "late entry"
    assert row is not None and row.notes == "late entry" and row.rules_followed is None


# --- T8: settings and strategies ----------------------------------------------------------------------------


@pytest.mark.db
def test_settings_put_audits_the_actor_refuses_bad_values_without_echo(
    db_factory: sessionmaker[Session],
) -> None:
    alice = AuthUser(id=2, username="alice", session_id=9, csrf_token="t")
    client = make_client(_services(db_factory), settings.router, user=alice, raise_server_exceptions=False)
    resp = client.put("/api/settings/approval_mode", json={"value": "auto"})
    assert resp.status_code == 200 and resp.json()["value"] == "auto"
    assert resp.json()["updated_by"] == "web:alice"
    with db_factory() as s:
        audits = s.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars().all()
    assert [(a.actor, a.action, a.before, a.after) for a in audits] == [
        ("web:alice", "settings.set:approval_mode", {"value": "manual"}, {"value": "auto"})
    ]
    # every key: a hostile value is refused (or, for free-text keys, stored) and never echoed in a 422
    keys = [item["key"] for item in client.get("/api/settings").json()["items"]]
    assert len(keys) == len(RuntimeSettings.model_fields)
    for key in keys:
        for value in (SENTINEL, [SENTINEL], {"k": SENTINEL}, 10**30):
            resp = client.put(f"/api/settings/{key}", json={"value": value})
            assert resp.status_code in (200, 422), (key, value, resp.status_code)
            if resp.status_code == 422:
                assert SENTINEL not in resp.text, (key, resp.text)
                assert resp.json()["error"]["code"] == "validation" and resp.json()["error"]["fields"]
    for unknown in ("nope", "approval_mode ", "killswitch_daily_loss_pct", "APPROVAL_MODE"):
        resp = client.put(f"/api/settings/{unknown}", json={"value": "auto"})
        assert resp.status_code == 404, (unknown, resp.status_code)
    assert client.put("/api/settings/approval_mode", json={}).status_code == 422
    assert client.put("/api/settings/approval_mode", json={"value": None}).status_code == 422
    with db_factory() as s:
        stored = s.get(m.Setting, "approval_mode")
    assert stored is not None and stored.value == "auto"


@pytest.mark.db
def test_strategy_put_validates_params_without_new_revisions(db_factory: sessionmaker[Session]) -> None:
    services = _services(db_factory)
    services.registry.ensure_defaults()
    client = make_client(services, strategies.router, raise_server_exceptions=False)

    def revision() -> int:
        return services.registry.current("orb_sip").revision

    def audit_count() -> int:
        with db_factory() as s:
            return int(s.execute(select(func.count()).select_from(m.AuditLog)).scalar_one())

    rev0, audits0 = revision(), audit_count()
    bad_bodies: list[Any] = [
        {"params": {"no_such_param": 1}},
        {"params": {"top_n": SENTINEL}},
        {"params": {"top_n": 10**30}},
        {"params": {"top_n": -1}},
        {"params": {"top_n": None}},
        {"params": [1, 2]},
        {"params": {}, "enabled": None},
        {},
        {"enabled": SENTINEL},
    ]
    for body in bad_bodies:
        resp = client.put("/api/strategies/orb_sip", json=body)
        assert resp.status_code == 422, (body, resp.status_code)
        assert SENTINEL not in resp.text, body
    assert revision() == rev0 and audit_count() == audits0
    assert client.put("/api/strategies/nope", json={"enabled": False}).status_code == 404
    ok = client.put("/api/strategies/orb_sip", json={"params": {"top_n": 7}})
    assert ok.status_code == 200 and ok.json()["revision"] == rev0 + 1
    assert ok.json()["params"]["top_n"] == 7 and ok.json()["updated_by"] == "web:stephen"
    assert audit_count() == audits0 + 1
    # the same change again is a no-op: no revision, no audit row
    assert client.put("/api/strategies/orb_sip", json={"params": {"top_n": 7}}).json()["revision"] == rev0 + 1
    assert audit_count() == audits0 + 1


# --- T11: SSE -------------------------------------------------------------------------------------------


@pytest.mark.db
async def test_stream_limit_holds_for_simultaneous_requests(db_factory: sessionmaker[Session]) -> None:
    """Eleven EventSource connections arriving together (every tab reconnecting after a restart): the limit
    is checked before the handler awaits and before the subscription exists, so all eleven get a stream."""
    feed = FakeFeed()
    services = _services(db_factory, RealClock(), feed=feed)
    results = await asyncio.gather(
        *(stream.stream(services, DEFAULT_USER) for _ in range(stream.MAX_STREAMS + 1)),
        return_exceptions=True,
    )
    for r in results:
        if not isinstance(r, BaseException):
            await r.body_iterator.aclose()  # type: ignore[attr-defined]
    refused = [r for r in results if isinstance(r, ApiError) and r.status == 429]
    assert len(refused) == 1, f"{len(refused)} of {len(results)} simultaneous streams refused"


@pytest.mark.db
async def test_revoked_session_ends_a_stream_opened_through_the_route(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route wires the real session check (the stream's own session id, the configured idle hours)."""
    monkeypatch.setattr(stream, "SESSION_RECHECK_SECONDS", 1)
    monkeypatch.setattr(stream, "TICK_SECONDS", 0.05)
    clock = FixedClock(NOW)
    with db_factory() as s:
        user = m.User(
            username="stephen",
            password_hash="x",
            failed_logins=0,
            created_at=NOW,
            updated_at=NOW,
            password_changed_at=NOW,
        )
        s.add(user)
        s.flush()
        mine = m.WebSession(
            user_id=user.id,
            token_hash="a" * 64,
            csrf_token="c" * 32,
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=NOW + timedelta(days=30),
        )
        other = replace_session(mine, "b" * 64)
        s.add_all([mine, other])
        s.flush()
        sid, other_id = mine.id, other.id
        s.commit()
    feed = FakeFeed()
    services = _services(db_factory, clock, feed=feed)
    who = AuthUser(id=1, username="stephen", session_id=sid, csrf_token="c")
    resp = await stream.stream(services, who)
    body: AsyncIterator[str] = resp.body_iterator  # type: ignore[assignment]
    first = [await asyncio.wait_for(body.__anext__(), 5) for _ in range(3)]
    assert first[0].startswith("retry:")
    # revoking ANOTHER session keeps this stream open
    await asyncio.to_thread(_revoke, db_factory, other_id)
    await asyncio.sleep(1.5)
    assert feed.subscriber_count() == 1
    await asyncio.to_thread(_revoke, db_factory, sid)
    t0 = time.monotonic()

    async def drain() -> None:
        async for _ in body:
            pass

    await asyncio.wait_for(drain(), 5)
    assert time.monotonic() - t0 < 3
    assert feed.subscriber_count() == 0


def replace_session(ws: m.WebSession, token_hash: str) -> m.WebSession:
    return m.WebSession(
        user_id=ws.user_id,
        token_hash=token_hash,
        csrf_token="d" * 32,
        created_at=ws.created_at,
        last_seen_at=ws.last_seen_at,
        expires_at=ws.expires_at,
    )


def _revoke(factory: sessionmaker[Session], sid: int) -> None:
    with factory() as s:
        s.execute(update(m.WebSession).where(m.WebSession.id == sid).values(revoked_at=NOW))
        s.commit()


class _LoopFeed(FakeFeed):
    """A FakeFeed that remembers the server's event loop (to call `handle_exit` on it like SIGTERM does)."""

    loop: asyncio.AbstractEventLoop | None = None

    async def run(self, stop: asyncio.Event) -> None:
        self.loop = asyncio.get_running_loop()
        await super().run(stop)


@contextmanager
def _serve(app: Any) -> Iterator[tuple[str, uvicorn.Server, threading.Thread]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=10
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", server, thread
    finally:
        if thread.is_alive():
            server.should_exit = True
            server.force_exit = True
            thread.join(15)
        sock.close()


@pytest.mark.db
def test_sse_through_the_full_app_has_security_headers_no_buffering_and_ends_on_shutdown(
    db_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    feed = _LoopFeed()
    app = _app(tmp_path, _services(db_factory, RealClock(), feed=feed))
    app.dependency_overrides[current_user] = lambda: DEFAULT_USER
    with _serve(app) as (url, server, thread), httpx.Client(base_url=url, timeout=10) as client:
        with client.stream("GET", "/api/stream") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["x-accel-buffering"] == "no"
            assert resp.headers["cache-control"] == "no-cache"  # the API's no-store must not replace it
            _assert_security_headers(resp, "GET /api/stream")
            lines = resp.iter_lines()
            assert next(lines) == "retry: 3000"
            assert feed.loop is not None
            feed.loop.call_soon_threadsafe(server.handle_exit, signal.SIGTERM, None)
            t0 = time.monotonic()
            for _ in lines:
                pass
            assert time.monotonic() - t0 < 5
        thread.join(10)
        assert not thread.is_alive()
    assert feed.run_stopped and feed.subscriber_count() == 0
    assert not stream.server_exiting()  # the exit mark is per event loop, never process-wide


# --- Decimal money on the wire --------------------------------------------------------------------------

FLOAT_OK = {"age_hours", "age_seconds", "token_age_hours", "worker_age_seconds", "duration_seconds"}
BLOBS = {
    "intent",
    "evidence",
    "candle",
    "data",
    "detail",
    "quote_snapshot",
    "fees",
    "params",
    "schema",
    "sizing",
}
MONEY = {
    "pnl", "price", "entry", "last", "stop", "unrealized_pnl", "equity", "cash", "settled_cash",
    "peak_equity",
    "realized_today", "week_to_date", "unrealized", "total_pnl", "entry_price", "exit_price", "planned_risk",
    "fees_total", "slippage_total", "slippage", "stop_price", "limit_price", "stop_loss", "realized_pnl",
    "risk_usd", "open", "high", "low", "close", "limit",
}  # fmt: skip


def _walk(value: Any, where: str, problems: list[str], key: str = "") -> None:
    if key in BLOBS:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _walk(v, f"{where}.{k}", problems, k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _walk(v, f"{where}[{i}]", problems, key)
    elif isinstance(value, float) and key not in FLOAT_OK:
        problems.append(f"{where} is a float ({value})")
    elif key in MONEY and value is not None and not isinstance(value, str):
        problems.append(f"{where} is {type(value).__name__}, not a string ({value!r})")


@pytest.mark.db
def test_decimal_money_is_a_json_string_everywhere(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        closed = seed_closed_trade(s, run_id)
        open_ = seed_open_position(s, run_id)
        s.add(
            m.EquitySnapshot(
                run_id=run_id,
                ts=NOW - timedelta(hours=1),
                equity=Decimal("25012.5000"),
                cash=Decimal("24812.5000"),
                settled_cash=Decimal("24812.5000"),
                peak_equity=Decimal("25012.5000"),
                drawdown_pct=Decimal("0.0000"),
            )
        )
        s.add(
            m.Journal(
                run_id=run_id, session_date=DAY, rules_followed=True, answered_via="web", updated_at=NOW
            )
        )
        s.commit()
    client = _trading_client(db_factory, quotes=fake_quotes({open_.symbol_id: Decimal("20.40")}))
    problems: list[str] = []
    for path in (
        "/api/dashboard",
        "/api/positions?status=all",
        f"/api/positions/{closed.position_id}",
        f"/api/positions/{open_.position_id}",
        "/api/trades",
        "/api/orders",
        "/api/fills",
        "/api/metrics",
        "/api/equity",
        "/api/journal",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.status_code)
        _walk(resp.json(), path, problems)
    assert not problems, problems
    dash = client.get("/api/dashboard").json()
    assert dash["positions"][0]["last"] == "20.40" and dash["pnl"]["unrealized"] == "4.0000"
