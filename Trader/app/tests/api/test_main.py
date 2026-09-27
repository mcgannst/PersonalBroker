"""P4-T3 acceptance tests 4, 5, 6 and 8: the app factory's security headers, request log, error ids and
lifespan, and `python -m trader.api` (uvicorn logging through `configure_logging`)."""

import json
import logging
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import trader.api.__main__ as api_main
import trader.logging_setup
from tests.fakes_api import FakeFeed, make_services, test_core
from trader.api import main
from trader.api.deps import ApiServices
from trader.api.main import SECURITY_HEADERS, create_app, web_dist_dir
from trader.config import EnvSettings
from trader.db.session import make_engine, make_session_factory
from trader.logging_setup import configure_logging
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
INDEX = "<!doctype html><div id='root'></div>"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _dist(tmp_path: Path, *, index: bool = True) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    if index:
        (dist / "index.html").write_text(INDEX)
    (dist / "assets" / "app.123.js").write_text("console.log(1)")
    return dist


def _factory() -> sessionmaker[Session]:
    return make_session_factory(make_engine("postgresql+psycopg://u:p@127.0.0.1:1/none"))


def _services(**overrides: Any) -> ApiServices:
    return make_services(test_core(_factory(), FixedClock(NOW)), **overrides)


def _app(tmp_path: Path, services: ApiServices | None = None, *, index: bool = True) -> Any:
    svc = services or _services()

    async def factory(stack: AsyncExitStack) -> ApiServices:
        return svc

    app = create_app(services_factory=factory, web_dist=_dist(tmp_path, index=index))

    def boom() -> None:
        raise RuntimeError("refresh_token=abc123secret")

    def ok() -> dict[str, str]:
        return {"ok": "yes"}

    app.add_api_route("/api/test-boom", boom, methods=["GET"])
    app.add_api_route("/api/test-ok", ok, methods=["GET"])
    return app


def _client(tmp_path: Path, services: ApiServices | None = None, *, index: bool = True) -> TestClient:
    return TestClient(
        _app(tmp_path, services, index=index), base_url="https://testserver", raise_server_exceptions=False
    )


# --- test 4 -------------------------------------------------------------------------------------------------


def test_security_headers_constant_is_the_planned_policy() -> None:
    assert SECURITY_HEADERS == {
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY",
    }


@pytest.mark.parametrize(
    ("path", "status", "api"),
    [
        ("/api/test-ok", 200, True),
        ("/api/meta", 200, True),
        ("/api/nope", 404, True),
        ("/api/test-boom", 500, True),
        ("/dashboard?proposal=5", 200, False),
        ("/assets/app.123.js", 200, False),
        ("/assets/missing.js", 404, False),
    ],
)
def test_every_response_carries_the_security_headers(
    tmp_path: Path, path: str, status: int, api: bool
) -> None:
    with _client(tmp_path) as client:
        resp = client.get(path)
    assert resp.status_code == status
    assert resp.headers["content-security-policy"] == CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    if api:
        assert resp.headers["cache-control"] == "no-store"
    else:
        assert resp.headers.get("cache-control") != "no-store"


def test_the_not_built_page_carries_the_security_headers(tmp_path: Path) -> None:
    with _client(tmp_path, index=False) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 503
    assert resp.headers["content-security-policy"] == CSP and resp.headers["x-frame-options"] == "DENY"


def test_api_validation_error_carries_no_store(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post("/api/meta")
    assert resp.status_code == 405 and resp.headers["cache-control"] == "no-store"


# --- test 5 -------------------------------------------------------------------------------------------------


@pytest.fixture
def clean_logging() -> Iterator[None]:
    """Undo configure_logging: its handler, the logger levels and the structlog configuration."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    names = ("", "httpx", "httpcore", "telegram", "anthropic", "sqlalchemy")
    levels = {name: logging.getLogger(name).level for name in names}
    structlog.reset_defaults()
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


def _json_lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@pytest.mark.usefixtures("clean_logging")
def test_request_log_is_one_json_line_without_query_or_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("api")  # the real function (imported by name before conftest patches the module)
    with _client(tmp_path) as client:
        capsys.readouterr()
        resp = client.get(
            "/api/test-ok?token=querysecret",
            headers={"Authorization": "Bearer hdrsecret123", "Cookie": "trader_session=cookiesecret"},
        )
    # The test client's own request log (logger httpx/httpx2, the client side) is not the server's.
    server = [x for x in _json_lines(capsys.readouterr().out) if not str(x.get("logger")).startswith("httpx")]
    out = json.dumps(server)
    lines = [line for line in server if line.get("event") == "http.request"]
    assert len(lines) == 1
    line = lines[0]
    assert line["method"] == "GET"
    assert line["path"] == "/api/test-ok"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], int | float)
    assert line["request_id"] == resp.headers["x-request-id"]
    assert line["process"] == "api" and line["level"] == "info"
    for secret in ("querysecret", "hdrsecret123", "cookiesecret", "token="):
        assert secret not in out


@pytest.mark.usefixtures("clean_logging")
def test_health_requests_log_at_debug(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api")
    with _client(tmp_path) as client:
        capsys.readouterr()
        client.get("/api/health")
        client.get("/api/meta")
    events = [(x.get("event"), x.get("path")) for x in _json_lines(capsys.readouterr().out)]
    assert ("http.request", "/api/meta") in events
    assert ("http.request", "/api/health") not in events  # DEBUG, below the INFO root level


def test_request_log_levels() -> None:
    assert main.request_log_level("/api/health") == logging.DEBUG
    assert main.request_log_level("/api/stream") == logging.DEBUG
    assert main.request_log_level("/api/dashboard") == logging.INFO
    assert main.request_log_level("/dashboard") == logging.INFO


@pytest.mark.usefixtures("clean_logging")
def test_uvicorn_records_go_through_configure_logging(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api")
    logging.getLogger("uvicorn.error").info("x")
    logging.getLogger("uvicorn").warning("password=hunter2 in a uvicorn line")
    lines = _json_lines(capsys.readouterr().out)
    assert [(x["event"], x["logger"], x["process"]) for x in lines][:1] == [("x", "uvicorn.error", "api")]
    assert len(lines) == 2 and "hunter2" not in json.dumps(lines)


# --- test 6 -------------------------------------------------------------------------------------------------


def test_unhandled_exception_is_500_with_the_request_id(tmp_path: Path) -> None:
    with structlog.testing.capture_logs() as logs, _client(tmp_path) as client:
        resp = client.get("/api/test-boom")
    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["code"] == "internal" and error["message"] == "Internal error"
    rid = resp.headers["x-request-id"]
    assert rid and error["request_id"] == rid
    assert "abc123secret" not in resp.text
    assert "abc123secret" not in repr(logs)
    req = [e for e in logs if e.get("event") == "http.request"]
    assert len(req) == 1 and req[0]["status"] == 500


def test_every_response_has_its_own_request_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a = client.get("/api/nope")
        b = client.get("/api/nope", headers={"X-Request-ID": "spoofed"})
    assert a.headers["x-request-id"] != b.headers["x-request-id"]
    assert b.headers["x-request-id"] != "spoofed"
    assert a.json()["error"]["request_id"] == a.headers["x-request-id"]


# --- test 8 -------------------------------------------------------------------------------------------------


def test_lifespan_starts_and_stops_the_feed(tmp_path: Path) -> None:
    feed = FakeFeed()
    services = _services(feed=feed)
    app = _app(tmp_path, services)
    assert not feed.run_started
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/test-ok").status_code == 200
        assert feed.run_started and not feed.run_stopped
        assert app.state.services is services
    assert feed.run_stopped


def test_lifespan_closes_the_stack_on_shutdown(tmp_path: Path) -> None:
    closed: list[str] = []
    services = _services()

    async def factory(stack: AsyncExitStack) -> ApiServices:
        stack.callback(closed.append, "closed")
        return services

    app = create_app(services_factory=factory, web_dist=_dist(tmp_path))
    with TestClient(app, base_url="https://testserver"):
        assert closed == []
    assert closed == ["closed"]


def test_a_services_build_failure_is_logged_and_raised(tmp_path: Path) -> None:
    async def factory(stack: AsyncExitStack) -> ApiServices:
        raise RuntimeError("cannot build: password=hunter2")

    app = create_app(services_factory=factory, web_dist=_dist(tmp_path))
    with structlog.testing.capture_logs() as logs, pytest.raises(RuntimeError, match="cannot build"):
        with TestClient(app, base_url="https://testserver"):
            pass
    failed = [e for e in logs if e.get("event") == "api.services_failed"]
    assert len(failed) == 1 and failed[0]["error_type"] == "RuntimeError"
    assert "hunter2" not in repr(logs)


class _StuckFeed(FakeFeed):
    """A feed whose run ignores `stop`: shutdown must not wait for it forever."""

    async def run(self, stop: Any) -> None:
        import asyncio

        self.run_started = True
        try:
            await asyncio.Event().wait()
        finally:
            self.run_stopped = True


def test_a_stuck_feed_is_cancelled_after_the_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "FEED_STOP_SECONDS", 0.05)
    feed = _StuckFeed()
    with TestClient(_app(tmp_path, _services(feed=feed)), base_url="https://testserver"):
        pass
    assert feed.run_started and feed.run_stopped


# --- web_dist_dir and python -m trader.api ------------------------------------------------------------------


def _env(**kw: Any) -> EnvSettings:
    return EnvSettings(
        _env_file=None,
        database_url="postgresql+psycopg://unused",
        app_encryption_key="k",
        session_secret="s",
        **kw,
    )


def test_web_dist_dir_from_env_or_the_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_DIST_DIR", raising=False)
    assert web_dist_dir(_env(web_dist_dir=str(tmp_path))) == tmp_path
    repo = Path(main.__file__).resolve().parents[3]  # Trader/app/trader/api/main.py -> Trader
    assert web_dist_dir(_env()) == repo / "web" / "dist"


def test_main_runs_uvicorn_with_logging_through_configure_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    sentinel = object()

    def configure(process: str) -> None:
        calls.append(("logging", process))

    def make_app() -> object:
        calls.append(("app", None))
        return sentinel

    def run(app: Any, **kwargs: Any) -> None:
        calls.append(("run", (app, kwargs)))

    monkeypatch.setattr(trader.logging_setup, "configure_logging", configure)
    monkeypatch.setattr(api_main, "create_app", make_app)
    monkeypatch.setattr(uvicorn, "run", run)
    api_main.main()
    assert [c[0] for c in calls] == ["logging", "app", "run"]
    assert calls[0][1] == "api"
    app, kwargs = calls[2][1]
    assert app is sentinel
    assert kwargs == {
        "host": "0.0.0.0",
        "port": 8000,
        "log_config": None,
        "access_log": False,
        "proxy_headers": True,
        "forwarded_allow_ips": "*",
        "timeout_graceful_shutdown": 10,
    }
