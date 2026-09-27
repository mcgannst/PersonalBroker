"""P4-T7 acceptance tests 6 and 7: `GET /api/journal` and `PUT /api/journal/{date}`."""

import threading
import time
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.factories import add_run, add_symbol
from tests.fakes_api import make_services, test_core
from tests.reports import add_trade
from trader.api.routers import journal
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # Tuesday 17:00 ET
TODAY = date(2026, 10, 6)


def _client(factory: sessionmaker[Session], now: datetime = NOW) -> TestClient:
    return make_client(make_services(test_core(factory, FixedClock(now))), journal.router)


def _live(factory: sessionmaker[Session]) -> int:
    return get_live_run(factory, FixedClock(NOW), RuntimeSettings()).id


def _audits(factory: sessionmaker[Session]) -> list[m.AuditLog]:
    with factory() as s:
        return list(
            s.scalars(select(m.AuditLog).where(m.AuditLog.action == "journal.update").order_by(m.AuditLog.id))
        )


# --- PUT ---------------------------------------------------------------------------------------------------


@pytest.mark.db
def test_put_changes_only_the_fields_sent(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    client = _client(db_factory)
    first = client.put("/api/journal/2026-10-06", json={"rules_followed": True})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["session_date"] == "2026-10-06" and body["rules_followed"] is True
    assert body["answered_via"] == "web" and body["notes"] is None
    assert body["updated_at"] == "2026-10-06T21:00:00Z" and body["trades"] == 0

    second = client.put("/api/journal/2026-10-06", json={"notes": "late entry"})
    assert second.status_code == 200
    assert second.json()["rules_followed"] is True and second.json()["notes"] == "late entry"
    with db_factory() as s:
        row = s.get(m.Journal, (run_id, TODAY))
        assert row is not None
        assert (row.rules_followed, row.notes, row.answered_via) == (True, "late entry", "web")

    audits = _audits(db_factory)
    assert len(audits) == 2 and all(a.actor == "web:stephen" for a in audits)
    assert audits[0].before is None and audits[0].after["rules_followed"] is True
    assert audits[1].before["rules_followed"] is True and audits[1].before["notes"] is None
    assert audits[1].after["notes"] == "late entry" and audits[1].after["session_date"] == "2026-10-06"


@pytest.mark.db
def test_put_notes_keeps_a_telegram_answer(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        s.add(
            m.Journal(
                run_id=run_id,
                session_date=date(2026, 10, 5),
                rules_followed=False,
                answered_via="telegram",
                updated_at=datetime(2026, 10, 5, 21, 0, tzinfo=UTC),
            )
        )
        s.commit()
    body = _client(db_factory).put("/api/journal/2026-10-05", json={"notes": "chased"}).json()
    assert (
        body["rules_followed"] is False and body["answered_via"] == "telegram" and body["notes"] == "chased"
    )


@pytest.mark.db
def test_put_null_clears_an_answer(db_factory: sessionmaker[Session]) -> None:
    _live(db_factory)
    client = _client(db_factory)
    client.put("/api/journal/2026-10-06", json={"rules_followed": True, "notes": "ok"})
    body = client.put("/api/journal/2026-10-06", json={"rules_followed": None}).json()
    assert body["rules_followed"] is None and body["notes"] == "ok"


@pytest.mark.db
@pytest.mark.parametrize(
    "day",
    [
        "2026-10-07",  # tomorrow (future)
        "2026-09-07",  # Labor Day (holiday)
        "2026-10-03",  # a Saturday
        "2040-01-02",  # beyond the calendar
    ],
)
def test_put_refuses_a_future_or_non_session_date(db_factory: sessionmaker[Session], day: str) -> None:
    _live(db_factory)
    resp = _client(db_factory).put(f"/api/journal/{day}", json={"rules_followed": True})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation"
    assert _audits(db_factory) == []


@pytest.mark.db
def test_put_refuses_long_notes_and_empty_body(db_factory: sessionmaker[Session]) -> None:
    _live(db_factory)
    client = _client(db_factory)
    long = client.put("/api/journal/2026-10-06", json={"notes": "x" * 5001})
    assert long.status_code == 422
    assert "xxxxx" not in long.text  # the input is never echoed
    assert client.put("/api/journal/2026-10-06", json={"notes": "x" * 5000}).status_code == 200
    assert client.put("/api/journal/2026-10-06", json={}).status_code == 422
    assert client.put("/api/journal/not-a-date", json={"notes": "a"}).status_code == 422


@pytest.mark.db
def test_put_needs_a_session(db_factory: sessionmaker[Session]) -> None:
    client = make_client(make_services(test_core(db_factory, FixedClock(NOW))), journal.router, user=None)
    assert client.put("/api/journal/2026-10-06", json={"notes": "a"}).status_code == 401
    assert client.get("/api/journal").status_code == 401


# --- GET ---------------------------------------------------------------------------------------------------


@pytest.mark.db
def test_get_lists_trade_days_and_telegram_answers_newest_first(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        sym = add_symbol(s)
        other = add_run(s, mode="replay", status="completed")
        add_trade(s, run_id, sym, date(2026, 10, 2), "20.0000", "2.0000")
        add_trade(s, run_id, sym, date(2026, 10, 2), "-5.0000", "-0.5000")
        add_trade(s, other, sym, date(2026, 10, 1), "1.0000", "0.1000")  # another run: not listed
        s.add(
            m.Journal(
                run_id=run_id,
                session_date=date(2026, 10, 5),
                rules_followed=True,
                answered_via="telegram",
                updated_at=datetime(2026, 10, 5, 21, 0, tzinfo=UTC),
            )
        )
        s.add(m.Journal(run_id=other, session_date=date(2026, 9, 30), rules_followed=False))
        s.commit()
    items = _client(db_factory).get("/api/journal").json()["items"]
    assert [i["session_date"] for i in items] == ["2026-10-05", "2026-10-02"]
    answered, traded = items
    assert answered["answered_via"] == "telegram" and answered["rules_followed"] is True
    assert answered["trades"] == 0 and answered["realized_pnl"] is None
    assert traded["rules_followed"] is None and traded["answered_via"] is None
    assert traded["trades"] == 2 and traded["realized_pnl"] == "15.0000"


@pytest.mark.db
def test_get_range_and_default_last_30_sessions(db_factory: sessionmaker[Session]) -> None:
    run_id = _live(db_factory)
    with db_factory() as s:
        sym = add_symbol(s)
        add_trade(s, run_id, sym, date(2026, 8, 24), "1.0000", "0.1000")  # 31 sessions back: outside
        add_trade(s, run_id, sym, date(2026, 8, 25), "2.0000", "0.2000")  # 30th session back (incl. today)
        add_trade(s, run_id, sym, date(2026, 9, 15), "3.0000", "0.3000")
        s.commit()
    client = _client(db_factory)
    default = [i["session_date"] for i in client.get("/api/journal").json()["items"]]
    assert default == ["2026-09-15", "2026-08-25"]
    ranged = client.get("/api/journal", params={"from": "2026-08-01", "to": "2026-08-31"}).json()["items"]
    assert [i["session_date"] for i in ranged] == ["2026-08-25", "2026-08-24"]
    assert client.get("/api/journal", params={"from": "2026-09-01", "to": "2026-08-01"}).status_code == 422


# --- fix round 1 ------------------------------------------------------------------------------------------


@pytest.mark.db
def test_dates_outside_the_calendar_are_never_a_500(db_factory: sessionmaker[Session]) -> None:
    """The calendar covers 2020-2030; pandas overflows on year 1 or 9999 (OverflowError, not ValueError)."""
    _live(db_factory)
    client = _client(db_factory)
    for to in ("0001-01-01", "0001-01-02", "1900-01-02", "2019-12-31", "9999-12-31"):
        assert client.get("/api/journal", params={"to": to}).status_code == 200, to
    for day in ("0001-01-01", "1900-01-02", "2019-12-31"):
        assert client.put(f"/api/journal/{day}", json={"notes": "x"}).status_code == 422, day


def _lock_waiters(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
            ).scalar_one()
        )


@pytest.mark.db
def test_first_put_racing_another_writer_audits_the_true_before(db_factory: sessionmaker[Session]) -> None:
    """Another writer (a racing PUT, Telegram) inserts the day's row but has not committed: the PUT waits for
    it, then audits that row as its "before" instead of null."""
    run_id = _live(db_factory)
    day = date(2026, 10, 5)
    client = _client(db_factory)
    writer = db_factory()
    writer.add(
        m.Journal(
            run_id=run_id, session_date=day, rules_followed=True, answered_via="telegram", updated_at=NOW
        )
    )
    writer.flush()
    result: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: result.update(resp=client.put(f"/api/journal/{day}", json={"notes": "web note"}))
    )
    thread.start()
    deadline = time.monotonic() + 10
    while _lock_waiters(db_factory) == 0 and thread.is_alive():
        assert time.monotonic() < deadline, "the PUT never waited for the uncommitted row"
        time.sleep(0.05)
    writer.commit()
    writer.close()
    thread.join(10)
    assert result["resp"].status_code == 200, result["resp"].text
    (audit,) = _audits(db_factory)
    assert audit.before is not None
    assert audit.before["rules_followed"] is True and audit.before["answered_via"] == "telegram"
    assert audit.after["notes"] == "web note" and audit.after["rules_followed"] is True
