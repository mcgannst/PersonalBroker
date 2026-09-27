"""The event log: the timeline the UI shows (SPEC §10 `event_log`)."""

from typing import Any, Literal, get_args

from sqlalchemy.orm import Session

from trader.db.models import EventLog
from trader.market.clock import Clock

# event_log.level has no DB constraint, and alerting matches these exact strings, so a typo'd level
# ("ERROR", "warn") would be stored but never alerted on. log_event refuses anything else.
Level = Literal["debug", "info", "warning", "error", "critical"]
LEVELS: frozenset[str] = frozenset(get_args(Level))


def log_event(
    session: Session,
    clock: Clock,
    level: str,
    source: str,
    message: str,
    data: dict[str, Any] | None = None,
    run_id: int | None = None,
) -> None:
    if level not in LEVELS:
        raise ValueError(f"event level {level!r} is not one of {sorted(LEVELS)}")
    session.add(
        EventLog(ts=clock.now(), level=level, source=source, run_id=run_id, message=message, data=data)
    )
