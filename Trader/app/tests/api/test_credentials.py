"""P4-T9 acceptance test 5: `POST /api/credentials/questrade` (paste a new Questrade refresh token)."""

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import FakeCredentialStore, make_services, test_core
from trader.adapters.questrade.auth import QuestradeAuthError
from trader.api.routers import credentials
from trader.db import models as m
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)
TOKEN = "qtRefreshTOKENvalue0123456789abcdefXYZ"


def _client(factory: sessionmaker[Session], store: FakeCredentialStore) -> TestClient:
    services = make_services(test_core(factory, FixedClock(NOW)), credentials=store)
    return make_client(services, credentials.router)


def _rows(factory: sessionmaker[Session]) -> tuple[list[m.AuditLog], list[m.EventLog]]:
    with factory() as s:
        audits = list(s.scalars(select(m.AuditLog).order_by(m.AuditLog.id)))
        events = list(s.scalars(select(m.EventLog).order_by(m.EventLog.id)))
    return audits, events


def _everything(audits: list[m.AuditLog], events: list[m.EventLog]) -> str:
    parts: list[Any] = []
    for a in audits:
        parts += [a.actor, a.action, a.before, a.after]
    for e in events:
        parts += [e.level, e.source, e.message, e.data]
    return repr(parts)


@pytest.mark.db
def test_paste_ok(
    db_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeCredentialStore()
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        resp = _client(db_factory, store).post(
            "/api/credentials/questrade", json={"refresh_token": f"  {TOKEN} "}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["seeded"] is True
    assert store.seeded == [TOKEN] and store.access_calls == 1  # stripped, then checked at once

    audits, events = _rows(db_factory)
    assert [(a.action, a.actor, a.after) for a in audits] == [
        ("credentials.questrade.seed", "web:stephen", {"ok": True})
    ]
    assert [(e.level, e.source) for e in events] == [("info", "questrade.token")]
    captured = capsys.readouterr()
    assert TOKEN not in resp.text + _everything(audits, events)
    assert TOKEN not in repr(logs) + caplog.text + captured.out + captured.err


@pytest.mark.db
def test_paste_rejected(
    db_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeCredentialStore()
    store.access_error = QuestradeAuthError("The refresh token was rejected")
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        resp = _client(db_factory, store).post("/api/credentials/questrade", json={"refresh_token": TOKEN})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["message"] == "The refresh token was rejected"

    audits, events = _rows(db_factory)
    assert [(a.action, a.after) for a in audits] == [("credentials.questrade.seed", {"ok": False})]
    assert [(e.level, e.source) for e in events] == [("error", "questrade.token")]
    assert "The refresh token was rejected" in events[0].message
    captured = capsys.readouterr()
    assert TOKEN not in resp.text + _everything(audits, events)
    assert TOKEN not in repr(logs) + caplog.text + captured.out + captured.err


@pytest.mark.db
def test_an_error_message_quoting_the_token_is_masked(db_factory: sessionmaker[Session]) -> None:
    store = FakeCredentialStore()
    store.access_error = QuestradeAuthError(f"Questrade said no to {TOKEN}")
    resp = _client(db_factory, store).post("/api/credentials/questrade", json={"refresh_token": TOKEN})
    assert resp.status_code == 422
    audits, events = _rows(db_factory)
    assert TOKEN not in resp.text + _everything(audits, events)


@pytest.mark.db
def test_an_unexpected_failure_is_not_echoed(db_factory: sessionmaker[Session]) -> None:
    store = FakeCredentialStore()
    store.access_error = RuntimeError(f"connection reset while sending {TOKEN}")
    client = make_client(
        make_services(test_core(db_factory, FixedClock(NOW)), credentials=store),
        credentials.router,
        raise_server_exceptions=False,
    )
    resp = client.post("/api/credentials/questrade", json={"refresh_token": TOKEN})
    assert resp.status_code == 502
    audits, events = _rows(db_factory)
    assert [a.after for a in audits] == [{"ok": False}]
    assert TOKEN not in resp.text + _everything(audits, events)


@pytest.mark.db
@pytest.mark.parametrize("value", ["", "   ", "x" * 401])
def test_blank_or_long_token_is_422_without_echo(db_factory: sessionmaker[Session], value: str) -> None:
    store = FakeCredentialStore()
    resp = _client(db_factory, store).post("/api/credentials/questrade", json={"refresh_token": value})
    assert resp.status_code == 422
    assert store.seeded == []
    if value.strip():
        assert value not in resp.text


@pytest.mark.db
def test_a_short_token_is_not_cut_out_of_the_message(db_factory: sessionmaker[Session]) -> None:
    """Fix round 1: cutting a 1-7 character "token" out of Questrade's message would mangle it (every "e"
    replaced); only a token of at least 8 characters is cut."""
    store = FakeCredentialStore()
    store.access_error = QuestradeAuthError("The refresh token was rejected")
    resp = _client(db_factory, store).post("/api/credentials/questrade", json={"refresh_token": "e"})
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "The refresh token was rejected"


@pytest.mark.db
def test_paste_needs_a_session(db_factory: sessionmaker[Session]) -> None:
    services = make_services(test_core(db_factory, FixedClock(NOW)))
    client = make_client(services, credentials.router, user=None)
    assert client.post("/api/credentials/questrade", json={"refresh_token": TOKEN}).status_code == 401
