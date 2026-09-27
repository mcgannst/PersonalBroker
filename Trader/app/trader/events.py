"""The event log: the timeline the UI shows (SPEC §10 `event_log`)."""

from typing import Any

from sqlalchemy.orm import Session

from trader.db.models import EventLog
from trader.market.clock import Clock


def log_event(
    session: Session,
    clock: Clock,
    level: str,
    source: str,
    message: str,
    data: dict[str, Any] | None = None,
    run_id: int | None = None,
) -> None:
    session.add(
        EventLog(ts=clock.now(), level=level, source=source, run_id=run_id, message=message, data=data)
    )
