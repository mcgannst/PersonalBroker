"""The equity series (live dashboard plan S6): snapshots, per-minute points from marks and candles (today
only) and a final `now` point, downsampled to at most 500 points. DB-T1 stub with the final signatures; DB-T3
implements it."""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.types import EQUITY_MAX_POINTS
from trader.api.schemas import EquityPointLiveOut, EquitySeriesOut, LiveRange
from trader.market.calendar import SessionCalendar


def downsample(
    points: Sequence[EquityPointLiveOut], max_points: int = EQUITY_MAX_POINTS
) -> tuple[list[EquityPointLiveOut], bool]:
    """At most `max_points` points keeping every bucket's extremes and the first and last point; the flag says
    whether anything was dropped."""
    raise NotImplementedError("DB-T3")


def equity_series(
    factory: sessionmaker[Session],
    calendar: SessionCalendar,
    run_id: int,
    now: datetime,
    range_: LiveRange,
    equity_now: Decimal | None,
) -> EquitySeriesOut:
    raise NotImplementedError("DB-T3")
