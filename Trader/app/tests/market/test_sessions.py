"""P3-T1 acceptance test 4: session_phase and current_session (pure helpers on the calendar)."""

from datetime import date, datetime

import pytest

from trader.market.calendar import SessionCalendar
from trader.market.clock import ET
from trader.market.sessions import SessionPhase, current_session, session_phase

CAL = SessionCalendar()


def et(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=ET)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (et(2026, 10, 3, 11, 0), "closed_day"),  # Saturday
        (et(2026, 11, 26, 11, 0), "closed_day"),  # Thanksgiving
        (et(2026, 10, 6, 9, 29, 59), "pre_market"),
        (et(2026, 10, 6, 0, 0, 0), "pre_market"),  # ET midnight of a session day
        (et(2026, 10, 6, 9, 30, 0), "open"),
        (et(2026, 10, 6, 15, 59), "open"),
        (et(2026, 10, 6, 16, 0), "after_close"),  # open is [open, close)
        (et(2026, 11, 27, 12, 59, 59), "open"),  # early close at 13:00 ET
        (et(2026, 11, 27, 13, 0, 0), "after_close"),
        (et(2026, 11, 2, 9, 30), "open"),  # first session after the November clock change (EST)
    ],
)
def test_session_phase(now: datetime, expected: SessionPhase) -> None:
    assert session_phase(CAL, now) == expected


def test_session_phase_reads_the_et_date_not_the_utc_date() -> None:
    # 21:00 ET on Friday 2026-10-02 is 01:00 UTC on Saturday: still Friday's session, after the close.
    assert session_phase(CAL, et(2026, 10, 2, 21, 0)) == "after_close"


def test_current_session() -> None:
    assert current_session(CAL, et(2026, 10, 3, 12, 0)) == date(2026, 10, 5)  # Saturday -> Monday
    assert current_session(CAL, et(2026, 10, 6, 8, 0)) == date(2026, 10, 6)
    assert current_session(CAL, et(2026, 10, 6, 17, 0)) == date(2026, 10, 6)  # today, even after close
    assert current_session(CAL, et(2026, 11, 26, 10, 0)) == date(2026, 11, 27)  # Thanksgiving
