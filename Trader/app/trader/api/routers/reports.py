"""Report routes (BR-61 web side; P5-T12): `GET /api/reports/weekly?week=YYYY-MM-DD` -> `WeeklyReportOut`
(404 when the week has no stored report).

`week` is any day of the week: the report is the one whose `week_start` (its Monday) is at most six days
before it, so Monday-Friday find their own week and a Saturday or Sunday finds the week just ended (as
`trader.reports.weekly.week_window` and the web's `tradingWeek`). A holiday week keeps its Monday as
`week_start`, so this needs no session calendar. A malformed date is 422 (FastAPI's date parsing).

`telegram_status` is the status of the `notifications` row keyed `weekly:<week_ending>` (`sending`, `sent`,
`failed`), or null when none was queued. The commentary is returned as stored, never generated on read.
"""

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.deps import CurrentUser, Services
from trader.api.errors import ApiError
from trader.api.schemas import WeeklyReportOut
from trader.db import models as m

router = APIRouter(tags=["reports"])

WEEK_SPAN = timedelta(days=6)  # Monday .. Sunday


def weekly_notification_key(week_ending: date) -> str:
    """The `notifications.dedupe_key` of a weekly report's Telegram message."""
    return f"weekly:{week_ending.isoformat()}"


def load_weekly(factory: sessionmaker[Session], week: date) -> WeeklyReportOut | None:
    """The stored report of the week containing `week` with its Telegram status, or None."""
    wr = m.WeeklyReport
    with factory() as s:
        report = s.scalars(
            select(wr)
            .where(wr.week_start <= week, wr.week_start >= week - WEEK_SPAN)
            .order_by(wr.week_start.desc())
            .limit(1)
        ).one_or_none()
        if report is None:
            return None
        status = s.scalar(
            select(m.Notification.status).where(
                m.Notification.dedupe_key == weekly_notification_key(report.week_ending)
            )
        )
        return WeeklyReportOut(
            week_start=report.week_start,
            week_ending=report.week_ending,
            run_id=report.run_id,
            created_at=report.created_at,
            updated_at=report.updated_at,
            commentary=report.commentary,
            commentary_status=report.commentary_status,  # validated against CommentaryStatus by the model
            commentary_error=report.commentary_error,
            model=report.model,
            cost_usd=report.cost_usd,
            facts=report.facts,
            telegram_status=status,
        )


@router.get("/reports/weekly")
def weekly(_user: CurrentUser, services: Services, week: Annotated[date, Query()]) -> WeeklyReportOut:
    """Sync route: FastAPI runs it in a worker thread, so the database read is off the event loop."""
    out = load_weekly(services.core.factory, week)
    if out is None:
        raise ApiError(404, "not_found", "No weekly report for that week")
    return out
