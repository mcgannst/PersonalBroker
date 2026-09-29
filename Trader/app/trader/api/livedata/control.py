"""The Control page's engine, strategies, schedule and error-log parts (live dashboard design §4). DB-T1 stub
with the final signatures; DB-T6 implements it."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.api.deps import ApiServices
from trader.api.livedata.types import ERRORS_SHOWN
from trader.api.schemas import EngineOut, EventOut, OpeningBarsOut, ScheduleItemOut, StrategyCardOut


def engine_card(services: ApiServices, run_id: int, now: datetime) -> EngineOut:
    raise NotImplementedError("DB-T6")


def strategy_cards(services: ApiServices, run_id: int) -> list[StrategyCardOut]:
    raise NotImplementedError("DB-T6")


def schedule(
    services: ApiServices, now: datetime, heartbeat_detail: Mapping[str, Any] | None
) -> list[ScheduleItemOut]:
    raise NotImplementedError("DB-T6")


def job_summary(job: str, detail: Any, batch: OpeningBarsOut | None) -> str | None:
    """A job's key detail in a few words (e.g. "543 bars in 31 s"), or None."""
    raise NotImplementedError("DB-T6")


def error_log(factory: sessionmaker[Session], limit: int = ERRORS_SHOWN) -> list[EventOut]:
    """The newest warning-and-above `event_log` rows (live or unscoped), masked."""
    raise NotImplementedError("DB-T6")
