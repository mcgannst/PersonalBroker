"""The activity feed and the rejections panel (live dashboard plan S7, S8). DB-T1 stub with the final
signatures; DB-T5 implements it."""

from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.types import ACTIVITY_LIMIT
from trader.api.schemas import ActivityItemOut, RejectionsOut


def activity_feed(
    factory: sessionmaker[Session], run_id: int, day: date, *, limit: int = ACTIVITY_LIMIT
) -> list[ActivityItemOut]:
    """Newest first, at most `limit` items whose time falls in the ET day of `day`."""
    raise NotImplementedError("DB-T5")


def rejections(factory: sessionmaker[Session], run_id: int, day: date) -> RejectionsOut:
    raise NotImplementedError("DB-T5")
