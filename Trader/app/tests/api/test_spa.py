"""P4-T3 acceptance tests 3 and 9: the built web app is served with SPA deep links (contract refinement 1:
every Telegram link path returns the SPA with 200), assets are cached forever, `/api/...` stays JSON, and no
path escapes `dist`."""

from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_api import make_services, test_core
from trader.api.deps import ApiServices
from trader.api.main import create_app
from trader.db.session import make_engine, make_session_factory
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
INDEX = "<!doctype html><title>Trader</title><div id='root'></div>"
IMMUTABLE = "public, max-age=31536000, immutable"


def _dist(tmp_path: Path, *, index: bool = True) -> Path:
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    if index:
        (dist / "index.html").write_text(INDEX)
    (dist / "assets" / "app.123.js").write_text("console.log('app')")
    (dist / "assets" / ".hidden.js").write_text("hidden")
    (dist / ".env").write_text("SECRET=nope")
    (tmp_path / "web" / "outside.txt").write_text("outside dist")
    (tmp_path / "x").write_text("outside dist")
    return dist


def _client(
    tmp_path: Path, factory: sessionmaker[Session] | None = None, *, index: bool = True
) -> TestClient:
    factory = factory or make_session_factory(make_engine("postgresql+psycopg://u:p@127.0.0.1:1/none"))
    services = make_services(test_core(factory, FixedClock(NOW)))

    async def services_factory(stack: AsyncExitStack) -> ApiServices:
        return services

    return TestClient(
        create_app(services_factory=services_factory, web_dist=_dist(tmp_path, index=index)),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )


# The five Telegram link paths (P3 contract 4) and the other SPA pages, with or without a query string.
DEEP_LINKS = [
    "/dashboard?proposal=5",
    "/trades?position=3",
    "/journal?date=2026-10-06",
    "/system",
    "/reports?week=2026-10-09",
    "/candidates",
    "/performance",
    "/settings",
    "/login?next=%2Fsystem",
    "/",
    "/dashboard/",
    "/some/unknown/page",
]


@pytest.mark.parametrize("path", DEEP_LINKS)
def test_deep_links_return_the_spa(tmp_path: Path, path: str) -> None:
    with _client(tmp_path) as client:
        resp = client.get(path)  # no session: the SPA itself sends a logged-out visitor to /login
    assert resp.status_code == 200
    assert resp.text == INDEX
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"


def test_head_of_a_deep_link(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.head("/dashboard?proposal=5")
    assert resp.status_code == 200 and resp.content == b""


def test_assets_are_served_immutable(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/assets/app.123.js")
        missing = client.get("/assets/nope.js")
    assert resp.status_code == 200
    assert resp.text == "console.log('app')"
    assert resp.headers["cache-control"] == IMMUTABLE
    assert "javascript" in resp.headers["content-type"]
    assert missing.status_code == 404  # never index.html for a missing asset
    assert missing.headers.get("cache-control") != IMMUTABLE


@pytest.mark.parametrize("path", ["/api/nope", "/api", "/api/", "/api/dashboard/extra/segments"])
def test_unknown_api_paths_are_json_404(tmp_path: Path, path: str) -> None:
    with _client(tmp_path) as client:
        resp = client.get(path)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert INDEX not in resp.text


def test_unknown_api_path_with_another_method_is_json_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post("/api/nope")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_wrong_method_on_a_known_api_route_is_405(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post("/api/meta")
    assert resp.status_code == 405 and resp.json()["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize(
    "raw_path",
    [
        "/../etc/passwd",
        "/../x",
        "/assets/../../x",
        "/assets/%2e%2e/%2e%2e/x",
        "/assets/..%2f..%2fx",
        "/%2e%2e/outside.txt",
        "/assets/../../outside.txt",
        "/.env",
        "/assets/.hidden.js",
        "/assets/../.env",
        "/assets/%00.js",
    ],
)
def test_no_path_escapes_dist_or_serves_a_hidden_file(tmp_path: Path, raw_path: str) -> None:
    with _client(tmp_path) as client:
        # Send the path as written: httpx would otherwise normalise the dot segments away.
        resp = client.request("GET", "https://testserver" + raw_path)
        raw = client.get(raw_path)
    for r in (resp, raw):
        assert "outside dist" not in r.text
        assert "SECRET" not in r.text and "hidden" not in r.text
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert r.text == INDEX


def test_a_root_file_of_dist_is_served(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        (tmp_path / "web" / "dist" / "favicon.svg").write_text("<svg/>")
        resp = client.get("/favicon.svg")
    assert resp.status_code == 200 and resp.text == "<svg/>"
    assert resp.headers["cache-control"] == "no-cache"


def test_a_directory_is_never_listed(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/assets")
        slash = client.get("/assets/")
    for r in (resp, slash):
        assert "app.123.js" not in r.text


def test_non_get_on_an_spa_path_is_not_the_spa(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post("/dashboard")
    assert resp.status_code in (404, 405)
    assert INDEX not in resp.text


# --- test 9 -------------------------------------------------------------------------------------------------


@pytest.mark.db
def test_without_index_html_pages_are_503_and_health_still_works(
    tmp_path: Path, db_factory: sessionmaker[Session]
) -> None:
    with _client(tmp_path, db_factory, index=False) as client:
        page = client.get("/dashboard")
        health = client.get("/api/health")
    assert page.status_code == 503
    assert page.headers["content-type"].startswith("text/plain")
    assert "web app not built" in page.text
    assert health.status_code == 200
    assert health.json()["db_ok"] is True


def test_index_built_after_start_is_served(tmp_path: Path) -> None:
    with _client(tmp_path, index=False) as client:
        assert client.get("/system").status_code == 503
        (tmp_path / "web" / "dist" / "index.html").write_text(INDEX)
        resp = client.get("/system")
    assert resp.status_code == 200 and resp.text == INDEX
