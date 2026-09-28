"""P4-T9 acceptance tests 1, 2 and 6: `GET /api/system`, `GET /api/events` and the Telegram test."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import FakeCredentialStore, make_services, test_core
from tests.fakes_telegram import RecordingNotifier
from trader.adapters.questrade.auth import TokenHealth
from trader.api.routers import system
from trader.db import models as m
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # Tuesday 17:00 ET, 15:00 MT
BOT_TOKEN = "123456789:AAHfakeTokenValueForTestsOnlyXYZ12345"


def _client(factory: sessionmaker[Session], **overrides: Any) -> TestClient:
    services = make_services(test_core(factory, FixedClock(NOW)), **overrides)
    return make_client(services, system.router)


def _event(s: Session, level: str, message: str, ts: datetime, source: str = "worker") -> None:
    s.add(m.EventLog(ts=ts, level=level, source=source, message=message, data=None))


def _run(s: Session, job: str, started: datetime, status: str = "succeeded") -> None:
    s.add(
        m.JobRun(
            job=job,
            session_date=date(2026, 10, 6),
            started_at=started,
            finished_at=started + timedelta(seconds=30),
            status=status,
        )
    )


def _notification(s: Session, status: str, created: datetime, kind: str = "fill") -> None:
    s.add(
        m.Notification(
            kind=kind,
            text=f"secret message text {status}",
            created_at=created,
            status=status,
            attempts=2,
            error="429 Too Many Requests" if status != "sent" else None,
        )
    )


# --- GET /api/system ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_system_on_seeded_data(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        for i in range(3):  # three nightly runs: only the newest is listed
            _run(s, "nightly", NOW - timedelta(days=i, hours=20))
        _run(s, "premarket", NOW - timedelta(hours=10), status="failed")
        _run(s, "event:entry_0935", NOW - timedelta(hours=7))
        _run(s, "checkin@10:00", NOW - timedelta(hours=6))
        _run(s, "postclose", NOW - timedelta(minutes=30))
        _run(s, "session_end", NOW - timedelta(minutes=50))
        _run(s, "preopen", NOW - timedelta(days=8))  # older than 7 days: not listed
        for i in range(55):
            _event(s, "error", f"error {i}", NOW - timedelta(minutes=120 - i))
        _event(s, "critical", "critical one", NOW - timedelta(seconds=10))
        _event(s, "warning", "just a warning", NOW)
        _notification(s, "failed", NOW - timedelta(minutes=3))
        _notification(s, "unknown", NOW - timedelta(minutes=2))
        _notification(s, "sent", NOW - timedelta(minutes=1))
        _notification(s, "sending", NOW)
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=7,
                host="trader-dev",
                started_at=NOW - timedelta(hours=3),
                beat_at=NOW - timedelta(seconds=5),
                session_date=date(2026, 10, 6),
                phase="idle",
                detail={"rate_limit": {"market_remaining": 17, "account_remaining": 29}},
            )
        )
        s.commit()

    resp = _client(db_factory, telegram_configured=False).get("/api/system")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    runs = {r["job"]: r for r in body["last_runs"]}
    assert set(runs) == {
        "nightly",
        "premarket",
        "event:entry_0935",
        "checkin@10:00",
        "postclose",
        "session_end",
    }
    assert runs["nightly"]["started_at"] == "2026-10-06T01:00:00Z"  # the newest of the three
    assert runs["premarket"]["status"] == "failed"
    starts = [r["started_at"] for r in body["last_runs"]]
    assert starts == sorted(starts, reverse=True)

    errors = body["errors"]
    assert len(errors) == 50
    assert errors[0]["message"] == "critical one" and errors[0]["level"] == "critical"
    assert errors[1]["message"] == "error 54"
    assert all(e["level"] in ("error", "critical") for e in errors)
    assert [e["id"] for e in errors] == sorted((e["id"] for e in errors), reverse=True)

    failed = body["notifications_failed"]
    assert [n["status"] for n in failed] == ["unknown", "failed"]
    assert failed[0]["attempts"] == 2 and failed[0]["error"] == "429 Too Many Requests"
    assert "text" not in failed[0]
    assert "secret message text" not in resp.text

    assert body["rate_limit"] == {"market_remaining": 17, "account_remaining": 29}
    assert body["telegram_configured"] is False
    assert body["alembic_revision"] == "0006"  # the head (P5-T1 added 0006)
    assert body["worker"]["ok"] is True and body["worker"]["phase"] == "idle"
    assert body["token"]["ok"] is True
    assert body["manual_jobs"] == [
        "nightly",
        "premarket",
        "preopen",
        "postclose",
        "token-refresh",
        "weekly",  # P5-T17
    ]
    assert body["server_time"] == "2026-10-06T21:00:00Z"
    assert body["app_env"] == "dev" and body["version"] == "dev"


@pytest.mark.db
def test_system_with_nothing_recorded(db_factory: sessionmaker[Session]) -> None:
    store = FakeCredentialStore(TokenHealth(False, None, None, None))
    body = _client(db_factory, credentials=store).get("/api/system").json()
    assert body["last_runs"] == [] and body["errors"] == [] and body["notifications_failed"] == []
    assert body["rate_limit"] is None and body["worker"]["ok"] is False
    assert body["token"]["ok"] is False and body["token"]["seeded"] is False
    assert body["telegram_configured"] is True


@pytest.mark.db
def test_system_ignores_a_malformed_rate_limit(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=7,
                host="h",
                started_at=NOW,
                beat_at=NOW,
                session_date=None,
                phase="idle",
                detail={"rate_limit": "lots"},
            )
        )
        s.commit()
    body = _client(db_factory).get("/api/system").json()
    assert body["rate_limit"] is None


@pytest.mark.db
def test_system_needs_a_session(db_factory: sessionmaker[Session]) -> None:
    services = make_services(test_core(db_factory, FixedClock(NOW)))
    client = make_client(services, system.router, user=None)
    assert client.get("/api/system").status_code == 401
    assert client.get("/api/events").status_code == 401
    assert client.post("/api/system/telegram-test").status_code == 401


# --- GET /api/events ---------------------------------------------------------------------------------------


def _seed_events(factory: sessionmaker[Session], n: int) -> None:
    levels = ["info", "warning", "error", "debug", "critical"]
    with factory() as s:
        for i in range(1, n + 1):
            _event(s, levels[i % 5], f"event {i}", NOW - timedelta(minutes=n - i), source=f"src{i % 2}")
        s.commit()


@pytest.mark.db
def test_events_since_returns_newer_ids_oldest_first(db_factory: sessionmaker[Session]) -> None:
    _seed_events(db_factory, 15)
    client = _client(db_factory)
    items = client.get("/api/events", params={"since": 10}).json()["items"]
    assert [i["id"] for i in items] == [11, 12, 13, 14, 15]

    newest = client.get("/api/events").json()["items"]
    assert [i["id"] for i in newest] == list(range(15, 0, -1))
    page = client.get("/api/events", params={"before": 6, "limit": 3}).json()["items"]
    assert [i["id"] for i in page] == [5, 4, 3]
    assert [i["id"] for i in client.get("/api/events", params={"limit": 2}).json()["items"]] == [15, 14]


@pytest.mark.db
def test_events_level_and_source_filters(db_factory: sessionmaker[Session]) -> None:
    _seed_events(db_factory, 15)
    client = _client(db_factory)
    errors = client.get("/api/events", params={"level": "error"}).json()["items"]
    assert errors and {i["level"] for i in errors} == {"error", "critical"}
    warnings = client.get("/api/events", params={"level": "warning"}).json()["items"]
    assert {i["level"] for i in warnings} == {"warning", "error", "critical"}
    src = client.get("/api/events", params={"source": "src1"}).json()["items"]
    assert src and all(i["source"] == "src1" for i in src)
    assert client.get("/api/events", params={"level": "loud"}).status_code == 422
    assert client.get("/api/events", params={"limit": 0}).status_code == 422
    assert client.get("/api/events", params={"limit": 501}).status_code == 422
    assert client.get("/api/events", params={"since": -1}).status_code == 422
    # fix round 1: an id past a bigint is a 422, never a database error; the largest bigint is fine
    for name in ("since", "before"):
        assert client.get("/api/events", params={name: str(2**63)}).status_code == 422, name
        assert client.get("/api/events", params={name: str(2**63 - 1)}).status_code == 200, name


@pytest.mark.db
def test_events_mask_a_bot_token_url(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        _event(s, "error", f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage failed", NOW)
        s.commit()
    resp = _client(db_factory).get("/api/events")
    assert resp.status_code == 200
    assert BOT_TOKEN not in resp.text
    assert "sendMessage failed" in resp.json()["items"][0]["message"]


# --- POST /api/system/telegram-test ------------------------------------------------------------------------


@pytest.mark.db
def test_telegram_test_not_configured_is_409(db_factory: sessionmaker[Session]) -> None:
    notifier = RecordingNotifier()
    resp = _client(db_factory, telegram_configured=False, notifier=notifier).post("/api/system/telegram-test")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "conflict"
    assert resp.json()["error"]["message"] == "Telegram is not configured"
    assert notifier.sent == []


@pytest.mark.db
def test_telegram_test_sends_one_reply(db_factory: sessionmaker[Session]) -> None:
    notifier = RecordingNotifier()
    resp = _client(db_factory, notifier=notifier).post("/api/system/telegram-test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sent"] is True and "check your Telegram" in body["message"]
    assert len(notifier.sent) == 1
    msg = notifier.sent[0]
    assert msg.kind == "reply" and msg.buttons == ()
    assert "web app" in msg.text and "trader telegram-test" not in msg.text
    assert "15:00 MT" in msg.text
    with db_factory() as s:
        audits = list(s.scalars(select(m.AuditLog).where(m.AuditLog.action == "telegram.test")))
    assert len(audits) == 1 and audits[0].actor == "web:stephen"
