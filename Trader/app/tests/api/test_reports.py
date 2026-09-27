"""P5-T12 acceptance tests 1-2: `GET /api/reports/weekly?week=YYYY-MM-DD` (`trader.api.routers.reports`)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.factories import add_run
from tests.fakes_api import make_services, test_core
from trader.api.routers import reports
from trader.api.schemas import WeeklyReportOut
from trader.db import models as m
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 28, 15, 0, tzinfo=UTC)  # Saturday
T_CREATED = datetime(2026, 11, 28, 14, 0, tzinfo=UTC)
WEEK_START, WEEK_ENDING = date(2026, 11, 23), date(2026, 11, 27)
FACTS: dict[str, Any] = {"trades": 3, "wins": 2, "win_rate": "0.6667", "total_pnl": "12.5000"}
COMMENTARY = "Three trades, two wins. <b>Keep</b> going."


def _client(factory: sessionmaker[Session], *, user_none: bool = False) -> TestClient:
    services = make_services(test_core(factory, FixedClock(NOW)))
    if user_none:
        return make_client(services, reports.router, user=None)
    return make_client(services, reports.router)


def _seed_report(
    s: Session, run_id: int, *, week_start: date = WEEK_START, week_ending: date = WEEK_ENDING
) -> None:
    s.add(
        m.WeeklyReport(
            week_ending=week_ending,
            week_start=week_start,
            run_id=run_id,
            facts=FACTS,
            commentary=COMMENTARY,
            commentary_status="ok",
            commentary_error=None,
            model="claude-sonnet-5",
            input_tokens=900,
            output_tokens=120,
            cost_usd=Decimal("0.004200"),
            created_at=T_CREATED,
            updated_at=T_CREATED,
        )
    )


def _seed_notification(s: Session, key: str, status: str) -> None:
    s.add(m.Notification(kind="weekly_report", dedupe_key=key, text="x", created_at=T_CREATED, status=status))


def test_weekly_by_any_day_of_the_week_including_the_weekend(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        _seed_report(s, run)
        # the week before and the week after, so the lookup must pick the right one
        _seed_report(s, run, week_start=date(2026, 11, 16), week_ending=date(2026, 11, 20))
        s.commit()
    client = _client(db_factory)
    for day in ("2026-11-23", "2026-11-25", "2026-11-27", "2026-11-28", "2026-11-29"):
        r = client.get("/api/reports/weekly", params={"week": day})
        assert r.status_code == 200, (day, r.text)
        body = r.json()
        WeeklyReportOut.model_validate(body)
        assert body["week_ending"] == "2026-11-27" and body["week_start"] == "2026-11-23", day
        assert body["run_id"] == run
        assert body["facts"] == FACTS
        assert body["commentary"] == COMMENTARY  # stored text, never re-generated or escaped
        assert body["commentary_status"] == "ok" and body["model"] == "claude-sonnet-5"
        assert body["cost_usd"] == "0.004200"  # money as a JSON string
        assert body["telegram_status"] is None
    prior = client.get("/api/reports/weekly", params={"week": "2026-11-22"}).json()
    assert prior["week_ending"] == "2026-11-20"


def test_weekly_without_a_report_is_404(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        _seed_report(s, add_run(s))
        s.commit()
    r = _client(db_factory).get("/api/reports/weekly", params={"week": "2026-11-30"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_weekly_needs_a_session(db_factory: sessionmaker[Session]) -> None:
    r = _client(db_factory, user_none=True).get("/api/reports/weekly", params={"week": "2026-11-25"})
    assert r.status_code == 401


@pytest.mark.parametrize("week", ["2026-13-01", "2026-02-30", "yesterday", "2026-11-25T10:30:00", ""])
def test_weekly_malformed_or_missing_date_is_422(db_factory: sessionmaker[Session], week: str) -> None:
    r = _client(db_factory).get("/api/reports/weekly", params={"week": week})
    assert r.status_code == 422, week


def test_weekly_missing_week_is_422(db_factory: sessionmaker[Session]) -> None:
    assert _client(db_factory).get("/api/reports/weekly").status_code == 422


@pytest.mark.parametrize("status", ["sent", "failed", "sending"])
def test_telegram_status_is_the_weekly_notification_status(
    db_factory: sessionmaker[Session], status: str
) -> None:
    with db_factory() as s:
        _seed_report(s, add_run(s))
        _seed_notification(s, "weekly:2026-11-27", status)
        _seed_notification(s, "weekly:2026-11-20", "failed")  # another week's message does not count
        s.commit()
    body = _client(db_factory).get("/api/reports/weekly", params={"week": "2026-11-25"}).json()
    assert body["telegram_status"] == status


def test_telegram_status_is_null_without_the_weekly_notification(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        _seed_report(s, add_run(s))
        _seed_notification(s, "weekly:2026-11-20", "sent")
        _seed_notification(s, "daily_summary:2026-11-27", "sent")
        s.commit()
    body = _client(db_factory).get("/api/reports/weekly", params={"week": "2026-11-27"}).json()
    assert body["telegram_status"] is None


# --- P5-GW fix round 1 --------------------------------------------------------------------------------------


@pytest.mark.parametrize("week", ["0001-01-01", "0001-01-06", "0001-01-07"])
def test_weekly_near_the_first_date_is_404_not_500(db_factory: sessionmaker[Session], week: str) -> None:
    """`week - 6 days` overflows below 0001-01-01: the lower bound is clamped, no report is found."""
    with db_factory() as s:
        _seed_report(s, add_run(s))
        s.commit()
    r = _client(db_factory).get("/api/reports/weekly", params={"week": week})
    assert r.status_code == 404, r.text


def test_weekly_serves_only_a_live_runs_report(db_factory: sessionmaker[Session]) -> None:
    """A report row written for a replay run is never served, even when it is the only one that week."""
    with db_factory() as s:
        replay = add_run(s, mode="replay", status="completed")
        _seed_report(s, replay)
        s.commit()
    client = _client(db_factory)
    assert client.get("/api/reports/weekly", params={"week": "2026-11-25"}).status_code == 404
    with db_factory() as s:
        live = add_run(s)
        _seed_report(s, live, week_start=date(2026, 11, 22), week_ending=date(2026, 11, 26))
        s.commit()
    body = client.get("/api/reports/weekly", params={"week": "2026-11-25"}).json()
    assert body["run_id"] == live and body["week_start"] == "2026-11-22"
