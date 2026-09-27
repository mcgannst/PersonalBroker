"""P4-T9 acceptance test 3: `GET /api/jobs` and `POST /api/jobs/{job}/run` (manual runs)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.api.test_launcher import FakeSpawn
from tests.fakes_api import FakeJobLauncher, make_services, test_core
from trader.api.launcher import SubprocessJobLauncher
from trader.api.routers import jobs
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # Tuesday 17:00 ET


def _client(factory: sessionmaker[Session], spawn: FakeSpawn) -> TestClient:
    core = test_core(factory, FixedClock(NOW))
    launcher = SubprocessJobLauncher(factory, core.clock, SessionCalendar(), spawn=spawn)
    return make_client(make_services(core, jobs=launcher), jobs.router)


def _audits(factory: sessionmaker[Session]) -> list[m.AuditLog]:
    with factory() as s:
        return list(
            s.scalars(select(m.AuditLog).where(m.AuditLog.action == "job.run_manual").order_by(m.AuditLog.id))
        )


@pytest.mark.db
def test_run_spawns_once_audits_and_refuses_a_second_while_running(db_factory: sessionmaker[Session]) -> None:
    spawn = FakeSpawn()
    with _client(db_factory, spawn) as client:
        resp = client.post("/api/jobs/nightly/run", json={"force": True})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["job"] == "nightly" and body["launched"] is True and body["message"]
        assert [argv for argv, _ in spawn.calls] == [("trader", "nightly", "--force")]

        again = client.post("/api/jobs/nightly/run", json={})
        assert again.status_code == 409 and again.json()["error"]["code"] == "conflict"
        assert "already running" in again.json()["error"]["message"]
        assert len(spawn.calls) == 1

    audits = _audits(db_factory)
    assert len(audits) == 1
    assert audits[0].actor == "web:stephen"
    assert audits[0].after == {"job": "nightly", "date": None, "force": True}


@pytest.mark.db
def test_run_with_a_date(db_factory: sessionmaker[Session]) -> None:
    spawn = FakeSpawn()
    with _client(db_factory, spawn) as client:
        resp = client.post("/api/jobs/postclose/run", json={"date": "2026-10-05"})
        assert resp.status_code == 202, resp.text
        assert resp.json()["session_date"] == "2026-10-05"
    assert spawn.calls[0][0] == ("trader", "postclose", "--date", "2026-10-05")
    assert _audits(db_factory)[0].after == {"job": "postclose", "date": "2026-10-05", "force": False}


@pytest.mark.db
@pytest.mark.parametrize("job", ["event", "checkin", "session_end", "nope", "..%2F..%2Fbin%2Fsh"])
def test_unknown_job_is_404(db_factory: sessionmaker[Session], job: str) -> None:
    spawn = FakeSpawn()
    with _client(db_factory, spawn) as client:
        resp = client.post(f"/api/jobs/{job}/run", json={})
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"
    assert spawn.calls == [] and _audits(db_factory) == []


@pytest.mark.db
@pytest.mark.parametrize(
    "day",
    [
        "2026-09-07",  # Labor Day
        "2026-10-03",  # a Saturday
        "2040-01-02",  # beyond the calendar
    ],
)
def test_a_non_session_date_is_422(db_factory: sessionmaker[Session], day: str) -> None:
    spawn = FakeSpawn()
    with _client(db_factory, spawn) as client:
        resp = client.post("/api/jobs/premarket/run", json={"date": day})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation"
    assert spawn.calls == [] and _audits(db_factory) == []


@pytest.mark.db
def test_token_refresh_takes_no_options(db_factory: sessionmaker[Session]) -> None:
    spawn = FakeSpawn()
    with _client(db_factory, spawn) as client:
        forced = client.post("/api/jobs/token-refresh/run", json={"force": True})
        dated = client.post("/api/jobs/token-refresh/run", json={"date": "2026-10-06"})
        assert forced.status_code == 422 and dated.status_code == 422
        assert spawn.calls == []
        plain = client.post("/api/jobs/token-refresh/run", json={})
        assert plain.status_code == 202, plain.text
        assert plain.json()["session_date"] is None
    assert [argv for argv, _ in spawn.calls] == [("trader", "token-refresh")]


@pytest.mark.db
def test_run_without_a_body_and_a_bad_body(db_factory: sessionmaker[Session]) -> None:
    spawn = FakeSpawn()
    with _client(db_factory, spawn) as client:
        assert client.post("/api/jobs/preopen/run").status_code == 202
        assert client.post("/api/jobs/postclose/run", json={"date": "not-a-date"}).status_code == 422
        for force in ("true", 1, "yes", 0):  # fix round 1: a JSON boolean only
            assert client.post("/api/jobs/postclose/run", json={"force": force}).status_code == 422, force
    assert [argv for argv, _ in spawn.calls] == [("trader", "preopen")]


@pytest.mark.db
def test_run_uses_the_services_launcher(db_factory: sessionmaker[Session]) -> None:
    fake = FakeJobLauncher()
    fake.running_jobs.add("premarket")
    client = make_client(make_services(test_core(db_factory, FixedClock(NOW)), jobs=fake), jobs.router)
    assert client.post("/api/jobs/premarket/run", json={}).status_code == 409
    assert client.post("/api/jobs/preopen/run", json={"force": True}).status_code == 202
    assert fake.launches == [("preopen", None, True, "web:stephen")]


@pytest.mark.db
def test_run_needs_a_session(db_factory: sessionmaker[Session]) -> None:
    client = make_client(make_services(test_core(db_factory, FixedClock(NOW))), jobs.router, user=None)
    assert client.post("/api/jobs/nightly/run", json={}).status_code == 401
    assert client.get("/api/jobs").status_code == 401


# --- history -----------------------------------------------------------------------------------------------


def _run(s: Session, job: str, started: datetime, status: str = "succeeded", **kw: object) -> None:
    s.add(
        m.JobRun(
            job=job,
            session_date=date(2026, 10, 6),
            started_at=started,
            finished_at=kw.get("finished_at", started + timedelta(seconds=90)),
            status=status,
            error=kw.get("error"),
            detail=kw.get("detail"),
        )
    )


@pytest.mark.db
def test_history_newest_first_filtered_and_limited(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        for i in range(5):
            _run(s, "nightly", NOW - timedelta(days=i))
        _run(s, "premarket", NOW - timedelta(hours=1), status="failed", error="boom")
        _run(s, "postclose", NOW - timedelta(minutes=5), status="running", finished_at=None)
        s.commit()
    client = make_client(make_services(test_core(db_factory, FixedClock(NOW))), jobs.router)
    items = client.get("/api/jobs").json()["items"]
    assert [i["job"] for i in items][:3] == ["nightly", "postclose", "premarket"]
    assert len(items) == 7
    running = items[1]
    assert running["status"] == "running" and running["finished_at"] is None
    assert running["duration_seconds"] is None
    assert items[0]["duration_seconds"] == 90.0 and items[0]["started_at"] == "2026-10-06T21:00:00Z"
    assert items[2]["error"] == "boom"

    nightly = client.get("/api/jobs", params={"job": "nightly", "limit": 2}).json()["items"]
    assert [i["job"] for i in nightly] == ["nightly", "nightly"]
    assert nightly[0]["started_at"] > nightly[1]["started_at"]
    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/api/jobs", params={"limit": 501}).status_code == 422
