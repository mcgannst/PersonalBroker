"""DB-T3 acceptance tests 1-3 and 7 (pure): period windows, ET day bounds across DST, the session day on
weekends and holidays, and the unrealized part of a period block (live dashboard plan S4, S5).
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from trader.api.livedata.periods import et_day_bounds, open_pnl, period_windows, session_day
from trader.api.livedata.types import OpenValue, PeriodWindow
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET

CAL = SessionCalendar()
RUN_START = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)  # Tue 09-29 09:00 ET


def _et(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=ET).astimezone(UTC)


def _contains(w: PeriodWindow, ts: datetime) -> bool:
    return w.start_at <= ts < w.end_at


# --- 1. period windows --------------------------------------------------------------------------------------


def test_windows_on_a_wednesday() -> None:
    today, week, run = period_windows(CAL, _et(2026, 10, 7, 14), RUN_START)
    assert (today.key, today.date_from, today.date_to) == ("today", date(2026, 10, 7), date(2026, 10, 7))
    assert (week.key, week.date_from, week.date_to) == ("week", date(2026, 10, 5), date(2026, 10, 7))
    assert (run.key, run.date_from, run.date_to) == ("run", date(2026, 9, 29), date(2026, 10, 7))
    assert today.start_at == _et(2026, 10, 7, 0) and today.end_at == _et(2026, 10, 8, 0)
    assert week.start_at == _et(2026, 10, 5, 0) and week.end_at == _et(2026, 10, 8, 0)
    assert run.start_at == _et(2026, 9, 29, 0)
    for w in (today, week, run):
        assert w.start_at.tzinfo is UTC and w.end_at.tzinfo is UTC


def test_windows_on_a_saturday_show_the_last_session_and_the_week_just_ended() -> None:
    today, week, run = period_windows(CAL, _et(2026, 10, 10, 12), RUN_START)
    assert (today.date_from, today.date_to) == (date(2026, 10, 9), date(2026, 10, 9))
    assert (week.date_from, week.date_to) == (date(2026, 10, 5), date(2026, 10, 10))
    assert (run.date_from, run.date_to) == (date(2026, 9, 29), date(2026, 10, 10))


def test_run_window_starts_at_the_et_date_of_the_run_start() -> None:
    late = datetime(2026, 9, 30, 3, 30, tzinfo=UTC)  # Tue 09-29 23:30 ET
    *_, run = period_windows(CAL, _et(2026, 10, 7, 14), late)
    assert run.date_from == date(2026, 9, 29)


def test_a_sunday_belongs_to_the_week_that_began_the_monday_before() -> None:
    _, week, _ = period_windows(CAL, _et(2026, 10, 11, 20), RUN_START)
    assert (week.date_from, week.date_to) == (date(2026, 10, 5), date(2026, 10, 11))


# --- 2. DST -------------------------------------------------------------------------------------------------


def test_et_day_bounds_on_the_dst_change_days() -> None:
    start, end = et_day_bounds(date(2026, 11, 1))
    assert (start, end) == (datetime(2026, 11, 1, 4, tzinfo=UTC), datetime(2026, 11, 2, 5, tzinfo=UTC))
    assert (end - start).total_seconds() == 25 * 3600
    start, end = et_day_bounds(date(2026, 3, 8))
    assert (start, end) == (datetime(2026, 3, 8, 5, tzinfo=UTC), datetime(2026, 3, 9, 4, tzinfo=UTC))
    assert (end - start).total_seconds() == 23 * 3600
    assert start.tzinfo is UTC and end.tzinfo is UTC


def test_the_pinned_dst_fill_cases_land_in_the_stated_weeks() -> None:
    # Sunday 2026-11-01 (DST ends) belongs to the week of Monday 2026-10-26.
    _, week_oct26, _ = period_windows(CAL, _et(2026, 11, 1, 12), RUN_START)
    assert week_oct26.date_from == date(2026, 10, 26)
    _, week_nov2, _ = period_windows(CAL, _et(2026, 11, 4, 12), RUN_START)
    assert week_nov2.date_from == date(2026, 11, 2)
    last_edt_sunday = datetime(2026, 11, 2, 4, 59, 59, tzinfo=UTC)  # Sun 23:59:59 EST
    first_monday = datetime(2026, 11, 2, 5, 0, 0, tzinfo=UTC)  # Mon 00:00:00 EST
    assert _contains(week_oct26, last_edt_sunday) and not _contains(week_nov2, last_edt_sunday)
    assert _contains(week_nov2, first_monday) and not _contains(week_oct26, first_monday)
    # Sunday 2026-03-08 23:59:59 EDT is in the week of 2026-03-02.
    early = datetime(2025, 12, 1, 14, tzinfo=UTC)
    _, week_mar2, _ = period_windows(CAL, _et(2026, 3, 8, 22), early)
    _, week_mar9, _ = period_windows(CAL, _et(2026, 3, 10, 12), early)
    assert week_mar2.date_from == date(2026, 3, 2) and week_mar9.date_from == date(2026, 3, 9)
    spring_sunday_end = datetime(2026, 3, 9, 3, 59, 59, tzinfo=UTC)
    assert _contains(week_mar2, spring_sunday_end) and not _contains(week_mar9, spring_sunday_end)
    assert _contains(week_mar9, datetime(2026, 3, 9, 4, tzinfo=UTC))


# --- 3. holidays --------------------------------------------------------------------------------------------


def test_session_day_on_thanksgiving_is_the_day_before() -> None:
    assert session_day(CAL, _et(2026, 11, 26, 12)) == date(2026, 11, 25)
    today, _, _ = period_windows(CAL, _et(2026, 11, 26, 12), RUN_START)
    assert today.date_from == today.date_to == date(2026, 11, 25)


def test_session_day_on_a_session_morning_before_the_open_is_that_day() -> None:
    assert session_day(CAL, _et(2026, 10, 12, 7)) == date(2026, 10, 12)


def test_session_day_uses_the_et_date_not_the_utc_date() -> None:
    # Mon 2026-10-12 21:00 ET is already Tuesday in UTC.
    assert session_day(CAL, datetime(2026, 10, 13, 1, tzinfo=UTC)) == date(2026, 10, 12)
    assert session_day(CAL, _et(2026, 10, 11, 12)) == date(2026, 10, 9)  # Sunday


# --- 7. unrealized ------------------------------------------------------------------------------------------


def _ov(pid: int, qty: int, avg: str, mark: str | None, fees: str = "0.35") -> OpenValue:
    return OpenValue(
        position_id=pid,
        symbol_id=pid,
        qty=qty,
        avg_price=Decimal(avg),
        mark=Decimal(mark) if mark is not None else None,
        entry_fees=Decimal(fees),
    )


def test_unrealized_is_mark_minus_avg_times_qty_less_entry_fees() -> None:
    value, partial = open_pnl([_ov(1, 25, "182.40", "183.10"), _ov(2, 10, "20.00", "19.50", "0.0035")])
    assert value == Decimal("17.1500") + Decimal("-5.0035")
    assert partial is False


def test_a_position_without_a_mark_makes_the_value_partial() -> None:
    value, partial = open_pnl([_ov(1, 25, "182.40", "183.10"), _ov(2, 10, "20.00", None)])
    assert value == Decimal("17.1500") and partial is True


def test_no_marked_position_gives_none() -> None:
    assert open_pnl([_ov(1, 25, "182.40", None)]) == (None, True)
    assert open_pnl([]) == (None, False)
