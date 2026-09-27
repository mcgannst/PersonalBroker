"""P4-T3 acceptance tests 1, 2 and 7: the public `GET /api/health` and `GET /api/meta` (SPEC §11, §15)."""

import dataclasses
import zoneinfo
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import tzdata
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_api import make_services, test_core
from trader.api.deps import ApiServices
from trader.api.main import create_app
from trader.db import models as m
from trader.db.session import make_engine, make_session_factory
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)  # 11:30 ET, 09:30 MT
SECRET = "hunter2-db-password"


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>")
    (dist / "assets" / "app.123.js").write_text("console.log(1)")
    return dist


def _client(services: ApiServices, dist: Path) -> TestClient:
    async def factory(stack: AsyncExitStack) -> ApiServices:
        return services

    return TestClient(
        create_app(services_factory=factory, web_dist=dist),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )


def _seed_token(s: Session, *, last_refresh_at: datetime) -> None:
    s.add(
        m.ApiCredential(
            provider="questrade",
            refresh_token_enc="encrypted-not-a-real-token",
            expires_at=last_refresh_at + timedelta(minutes=30),
            last_refresh_at=last_refresh_at,
        )
    )
    s.commit()


def _beat(s: Session, *, beat_at: datetime, phase: str = "session") -> None:
    s.add(
        m.WorkerHeartbeat(
            process="worker",
            pid=42,
            host="trader-dev",
            started_at=beat_at - timedelta(hours=1),
            beat_at=beat_at,
            session_date=NOW.date(),
            phase=phase,
        )
    )
    s.commit()


class _BrokenFactory:
    """A session factory whose every use fails like an unreachable database (with a secret in the text)."""

    def __call__(self, *args: Any, **kwargs: Any) -> Session:
        raise OperationalError(f"SELECT 1 password={SECRET}", {}, Exception(f"password={SECRET}"))


def _unconnected_factory() -> sessionmaker[Session]:
    # Creating the engine does not connect; a use fails at once (nothing listens on port 1).
    return make_session_factory(make_engine("postgresql+psycopg://u:p@127.0.0.1:1/none"))


# --- test 1 -------------------------------------------------------------------------------------------------


@pytest.mark.db
def test_health_ok_without_a_session(db_factory: sessionmaker[Session], tmp_path: Path) -> None:
    with db_factory() as s:
        _seed_token(s, last_refresh_at=NOW - timedelta(hours=2))
        _beat(s, beat_at=NOW - timedelta(seconds=10))
    core = test_core(db_factory, FixedClock(NOW), app_version="1.2.3")
    with _client(make_services(core), _dist(tmp_path)) as client:
        resp = client.get("/api/health")  # no cookie: the route is public
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True and isinstance(body["db_latency_ms"], int)
    assert body["token_ok"] is True and body["token_age_hours"] == pytest.approx(2.0)
    assert body["worker_ok"] is True and body["worker_phase"] == "session"
    assert body["worker_age_seconds"] == pytest.approx(10.0)
    assert body["version"] == "1.2.3"
    assert body["time"] == "2026-10-06T15:30:00Z"


# --- test 2 -------------------------------------------------------------------------------------------------


def test_health_down_when_the_database_check_fails(tmp_path: Path) -> None:
    core = test_core(_unconnected_factory(), FixedClock(NOW))
    core = dataclasses.replace(core, factory=_BrokenFactory())  # type: ignore[arg-type]
    with _client(make_services(core), _dist(tmp_path)) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    assert body["db_ok"] is False and body["db_latency_ms"] is None
    assert body["token_ok"] is False and body["worker_ok"] is False
    assert SECRET not in resp.text
    assert "OperationalError" not in resp.text and "password" not in resp.text


@pytest.mark.db
def test_health_degraded_with_a_stale_heartbeat(db_factory: sessionmaker[Session], tmp_path: Path) -> None:
    with db_factory() as s:
        _seed_token(s, last_refresh_at=NOW - timedelta(hours=2))
        _beat(s, beat_at=NOW - timedelta(seconds=600))  # the default stale limit is 120 s
    core = test_core(db_factory, FixedClock(NOW))
    with _client(make_services(core), _dist(tmp_path)) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db_ok"] is True and body["token_ok"] is True
    assert body["worker_ok"] is False and body["worker_age_seconds"] == pytest.approx(600.0)


@pytest.mark.db
def test_health_degraded_without_a_token_or_worker(db_factory: sessionmaker[Session], tmp_path: Path) -> None:
    core = test_core(db_factory, FixedClock(NOW))
    with _client(make_services(core), _dist(tmp_path)) as client:
        body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["token_ok"] is False and body["worker_ok"] is False
    assert set(body) == {
        "status",
        "time",
        "version",
        "db_ok",
        "db_latency_ms",
        "token_ok",
        "token_age_hours",
        "worker_ok",
        "worker_phase",
        "worker_age_seconds",
    }  # no error text


@pytest.mark.db
def test_health_uses_the_stale_setting(db_factory: sessionmaker[Session], tmp_path: Path) -> None:
    with db_factory() as s:
        _seed_token(s, last_refresh_at=NOW - timedelta(hours=2))
        _beat(s, beat_at=NOW - timedelta(seconds=200))
    core = test_core(db_factory, FixedClock(NOW))
    core.settings.set("worker.heartbeat_stale_seconds", 300, "test")
    with _client(make_services(core), _dist(tmp_path)) as client:
        assert client.get("/api/health").json()["status"] == "ok"


# --- test 7 -------------------------------------------------------------------------------------------------


@pytest.fixture
def pip_tzdata_only() -> Iterator[None]:
    """zoneinfo reads the pip `tzdata` package only (as in the image); the default path is restored after."""
    zoneinfo.reset_tzpath([])
    zoneinfo.ZoneInfo.clear_cache()
    try:
        yield
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()


@pytest.mark.usefixtures("pip_tzdata_only")
def test_meta_without_a_session_uses_pinned_tzdata(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 12, 1, 17, 0, tzinfo=UTC))
    core = test_core(_unconnected_factory(), clock, app_env="dev", app_version="1.2.3")
    with _client(make_services(core), _dist(tmp_path)) as client:
        resp = client.get("/api/meta")
    assert resp.status_code == 200
    assert resp.json() == {
        "server_time": "2026-12-01T17:00:00Z",
        "app_env": "dev",
        "version": "1.2.3",
        "tz_display": "America/Edmonton",
        "tz_offset_minutes": -360,
        "tz_iana_version": tzdata.IANA_VERSION,
        "public_base_url": "https://testserver",
    }
    assert resp.headers["cache-control"] == "no-store"


def test_meta_offset_follows_the_display_zone(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 7, 1, 17, 0, tzinfo=UTC))
    core = test_core(_unconnected_factory(), clock, tz_display="America/New_York")
    with _client(make_services(core), _dist(tmp_path)) as client:
        assert client.get("/api/meta").json()["tz_offset_minutes"] == -240
