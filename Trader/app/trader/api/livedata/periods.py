"""Period P&L and costs (live dashboard plan S4, S5): today, week and run of the live run, in America/New_York
dates. DB-T1 stub with the final signatures; DB-T3 implements it."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.types import OpenValue, PeriodWindow
from trader.api.schemas import ClaudeTodayOut, PeriodPnlOut
from trader.market.calendar import SessionCalendar
from trader.settings_store import RuntimeSettings


def et_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[00:00 ET of `d`, 00:00 ET of `d` + 1 day) as UTC datetimes (a DST-change day is 23 or 25 hours)."""
    raise NotImplementedError("DB-T3")


def session_day(calendar: SessionCalendar, now: datetime) -> date:
    """Today's ET date when it is a session, else the latest session before it."""
    raise NotImplementedError("DB-T3")


def period_windows(
    calendar: SessionCalendar, now: datetime, run_started_at: datetime
) -> tuple[PeriodWindow, PeriodWindow, PeriodWindow]:
    """The (today, week, run) windows of S5."""
    raise NotImplementedError("DB-T3")


def claude_spent_between(factory: sessionmaker[Session], window: PeriodWindow) -> Decimal:
    raise NotImplementedError("DB-T3")


def period_blocks(
    factory: sessionmaker[Session],
    run_id: int,
    windows: Sequence[PeriodWindow],
    open_values: Sequence[OpenValue],
) -> list[PeriodPnlOut]:
    raise NotImplementedError("DB-T3")


def claude_today(factory: sessionmaker[Session], now: datetime, settings: RuntimeSettings) -> ClaudeTodayOut:
    raise NotImplementedError("DB-T3")
