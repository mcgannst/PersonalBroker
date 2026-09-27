"""Where a moment falls relative to the trading session (Phase 3). Pure helpers on the calendar.

`now` is any timezone-aware datetime; the session day is its America/New_York date.
"""

from datetime import date, datetime
from typing import Literal

from trader.market.calendar import SessionCalendar
from trader.market.clock import et_date

SessionPhase = Literal["closed_day", "pre_market", "open", "after_close"]


def session_phase(cal: SessionCalendar, now: datetime) -> SessionPhase:
    """`closed_day` on a weekend or holiday; otherwise before the open, `open` for [open, close), or after
    the (possibly early) close."""
    day = et_date(now)
    if not cal.is_session(day):
        return "closed_day"
    if now < cal.session_open(day):
        return "pre_market"
    if now < cal.session_close(day):
        return "open"
    return "after_close"


def current_session(cal: SessionCalendar, now: datetime) -> date:
    """Today's ET date if it is a session (even after the close), else the next session."""
    day = et_date(now)
    return day if cal.is_session(day) else cal.next_session(day)
