from datetime import UTC, date, datetime

import pytest

from trader.market.calendar import SessionCalendar

CAL = SessionCalendar()


def test_regular_session_times_utc_during_dst() -> None:
    assert CAL.session_open(date(2026, 10, 30)) == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert CAL.session_close(date(2026, 10, 30)) == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def test_session_times_after_dst_ends() -> None:
    assert CAL.session_open(date(2026, 11, 2)) == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def test_holiday_is_not_a_session() -> None:
    assert not CAL.is_session(date(2026, 11, 26))  # Thanksgiving
    assert not CAL.is_session(date(2026, 9, 26))  # Saturday
    with pytest.raises(ValueError):
        CAL.session_open(date(2026, 11, 26))


def test_early_close_day() -> None:
    # Day after Thanksgiving closes at 13:00 ET = 18:00 UTC
    assert CAL.session_close(date(2026, 11, 27)) == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_next_and_previous_session_skip_weekends_and_holidays() -> None:
    assert CAL.next_session(date(2026, 9, 25)) == date(2026, 9, 28)  # Fri -> Mon
    assert CAL.next_session(date(2026, 11, 25)) == date(2026, 11, 27)  # skips Thanksgiving
    assert CAL.next_session(date(2026, 9, 26)) == date(2026, 9, 28)  # from a Saturday
    assert CAL.previous_session(date(2026, 9, 28)) == date(2026, 9, 25)


def test_sessions_before() -> None:
    got = CAL.sessions_before(date(2026, 9, 28), 3)
    assert got == [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
