"""Open positions with marks, sparklines and bars (live dashboard plan S16, D2). DB-T1 stub with the final
signatures; DB-T4 implements it."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.types import LivePositions
from trader.api.schemas import MarkState


def mark_state(observed_at: datetime | None, now: datetime) -> MarkState:
    """`live` when observed at most `MARK_STALE_SECONDS` ago, `stale` when older, `missing` when None."""
    raise NotImplementedError("DB-T4")


def live_positions(
    factory: sessionmaker[Session], run_id: int, now: datetime, expand: Collection[int]
) -> LivePositions:
    raise NotImplementedError("DB-T4")
