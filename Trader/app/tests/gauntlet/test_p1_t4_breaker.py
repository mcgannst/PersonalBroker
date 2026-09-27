"""P1-T4 gauntlet (Breaker): clock, et_date and the NYSE session calendar.

Calendar facts checked against NYSE's published 2026 schedule: holidays Jan 1, Jan 19, Feb 16,
Apr 3 (Good Friday), May 25, Jun 19, Jul 3 (Independence Day observed, Jul 4 is a Saturday),
Sep 7, Nov 26, Dec 25; early (13:00 ET) closes Nov 27 and Dec 24 only. Jan 1 2027 is a Friday
holiday. US DST 2026: starts Sun Mar 8, ends Sun Nov 1.
"""

import ast
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock, et_date

CAL = SessionCalendar()


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_session_times_across_dst_spring_forward_and_fall_back() -> None:
    # Fri before spring-forward: EST (-5) -> 09:30 ET = 14:30 UTC, 16:00 ET = 21:00 UTC
    assert CAL.session_open(date(2026, 3, 6)) == _utc(2026, 3, 6, 14, 30)
    assert CAL.session_close(date(2026, 3, 6)) == _utc(2026, 3, 6, 21)
    # Mon after spring-forward: EDT (-4)
    assert CAL.session_open(date(2026, 3, 9)) == _utc(2026, 3, 9, 13, 30)
    assert CAL.session_close(date(2026, 3, 9)) == _utc(2026, 3, 9, 20)
    # Fri before fall-back: EDT, Mon after: EST
    assert CAL.session_close(date(2026, 10, 30)) == _utc(2026, 10, 30, 20)
    assert CAL.session_close(date(2026, 11, 2)) == _utc(2026, 11, 2, 21)
    # Always 09:30-16:00 wall time in New York on a regular day, and UTC-aware
    for d in (date(2026, 3, 6), date(2026, 3, 9), date(2026, 10, 30), date(2026, 11, 2)):
        o, c = CAL.session_open(d), CAL.session_close(d)
        assert o.utcoffset() == timedelta(0) and c.utcoffset() == timedelta(0)
        assert (o.astimezone(ET).hour, o.astimezone(ET).minute) == (9, 30)
        assert (c.astimezone(ET).hour, c.astimezone(ET).minute) == (16, 0)


def test_2026_holidays_and_early_closes() -> None:
    holidays = [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day observed
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ]
    for d in holidays:
        assert not CAL.is_session(d), d
        with pytest.raises(ValueError):
            CAL.session_close(d)
    # July 2 2026 is a normal full day (the holiday itself is the 3rd)
    assert CAL.session_close(date(2026, 7, 2)) == _utc(2026, 7, 2, 20)
    # Early closes at 13:00 ET: Nov 27 and Dec 24 (both EST -> 18:00 UTC), normal open
    assert CAL.session_open(date(2026, 12, 24)) == _utc(2026, 12, 24, 14, 30)
    assert CAL.session_close(date(2026, 12, 24)) == _utc(2026, 12, 24, 18)
    assert CAL.session_close(date(2026, 11, 27)) == _utc(2026, 11, 27, 18)
    # Dec 31 is a normal full day
    assert CAL.session_close(date(2026, 12, 31)) == _utc(2026, 12, 31, 21)


def test_navigation_across_long_weekends_and_year_boundary() -> None:
    # Good Friday long weekend
    assert CAL.next_session(date(2026, 4, 2)) == date(2026, 4, 6)
    assert CAL.previous_session(date(2026, 4, 6)) == date(2026, 4, 2)
    assert CAL.next_session(date(2026, 4, 3)) == date(2026, 4, 6)  # from the holiday itself
    # July 3 observed long weekend
    assert CAL.next_session(date(2026, 7, 2)) == date(2026, 7, 6)
    assert CAL.previous_session(date(2026, 7, 4)) == date(2026, 7, 2)
    # Christmas long weekend
    assert CAL.next_session(date(2026, 12, 24)) == date(2026, 12, 28)
    # Year boundary: Jan 1 2027 is a Friday holiday
    assert CAL.next_session(date(2026, 12, 31)) == date(2027, 1, 4)
    assert CAL.previous_session(date(2027, 1, 4)) == date(2026, 12, 31)
    assert CAL.previous_session(date(2027, 1, 1)) == date(2026, 12, 31)
    # sessions_before with n larger than a week, across Christmas and New Year
    assert CAL.sessions_before(date(2027, 1, 5), 10) == [
        date(2026, 12, 18),
        date(2026, 12, 21),
        date(2026, 12, 22),
        date(2026, 12, 23),
        date(2026, 12, 24),
        date(2026, 12, 28),
        date(2026, 12, 29),
        date(2026, 12, 30),
        date(2026, 12, 31),
        date(2027, 1, 4),
    ]
    # From a holiday, 14 sessions (the opening-bar history window) spanning Thanksgiving
    got = CAL.sessions_before(date(2026, 11, 26), 14)
    assert len(got) == 14
    assert got[-1] == date(2026, 11, 25)
    assert got[0] == date(2026, 11, 6)  # Veterans Day (Nov 11) is a session
    assert all(CAL.is_session(d) for d in got)
    assert got == sorted(set(got))


def test_sessions_before_zero_is_empty() -> None:
    # "the n sessions strictly before d": n = 0 must be [], not an exception from pandas
    assert CAL.sessions_before(date(2026, 9, 28), 0) == []


def test_et_date_around_midnight_in_both_dst_regimes() -> None:
    # Winter (EST, -5): midnight ET is 05:00 UTC
    assert et_date(_utc(2026, 1, 15, 4, 59)) == date(2026, 1, 14)
    assert et_date(_utc(2026, 1, 15, 5, 0)) == date(2026, 1, 15)
    # Summer (EDT, -4): midnight ET is 04:00 UTC
    assert et_date(_utc(2026, 7, 15, 3, 59)) == date(2026, 7, 14)
    assert et_date(_utc(2026, 7, 15, 4, 0)) == date(2026, 7, 15)
    # Spring-forward night: 00:00 ET on Mar 8 is still EST
    assert et_date(_utc(2026, 3, 8, 4, 59)) == date(2026, 3, 7)
    assert et_date(_utc(2026, 3, 8, 5, 0)) == date(2026, 3, 8)
    # Fall-back: 00:00 ET Nov 1 is EDT (04:00 UTC), 00:00 ET Nov 2 is EST (05:00 UTC)
    assert et_date(_utc(2026, 11, 1, 3, 59)) == date(2026, 10, 31)
    assert et_date(_utc(2026, 11, 1, 4, 0)) == date(2026, 11, 1)
    assert et_date(_utc(2026, 11, 2, 4, 59)) == date(2026, 11, 1)
    assert et_date(_utc(2026, 11, 2, 5, 0)) == date(2026, 11, 2)
    # Non-UTC input is handled by instant, not by its wall time
    edmonton = timezone(timedelta(hours=-6))
    assert et_date(datetime(2026, 9, 28, 22, 30, tzinfo=edmonton)) == date(2026, 9, 29)


def test_fixed_clock_with_non_utc_input() -> None:
    # 01:30 ET on spring-forward day is EST -> 06:30 UTC
    c = FixedClock(datetime(2026, 3, 8, 1, 30, tzinfo=ET))
    assert c.now() == _utc(2026, 3, 8, 6, 30)
    assert c.now().utcoffset() == timedelta(0)
    # Advancing an hour crosses the gap: 03:30 EDT, not 02:30
    c.advance(timedelta(hours=1))
    assert c.now() == _utc(2026, 3, 8, 7, 30)
    assert c.now().astimezone(ET).hour == 3
    # Ambiguous fall-back hour: fold picks EDT (0) vs EST (1)
    c.set(datetime(2026, 11, 1, 1, 30, tzinfo=ET, fold=0))
    assert c.now() == _utc(2026, 11, 1, 5, 30)
    c.set(datetime(2026, 11, 1, 1, 30, tzinfo=ET, fold=1))
    assert c.now() == _utc(2026, 11, 1, 6, 30)
    # Fixed-offset tz (Edmonton MDT, -6)
    c.set(datetime(2026, 9, 28, 7, 35, tzinfo=timezone(timedelta(hours=-6))))
    assert c.now() == _utc(2026, 9, 28, 13, 35)
    assert c.now().utcoffset() == timedelta(0)
    # set() also rejects naive datetimes, and a rejected set leaves the time unchanged
    with pytest.raises(ValueError):
        c.set(datetime(2026, 9, 28, 13, 35))
    assert c.now() == _utc(2026, 9, 28, 13, 35)


def test_dates_outside_calendar_range_raise_value_error() -> None:
    # The calendar starts in 2020; asking before that (or far in the future) must fail loudly
    # with ValueError (the interface's error), never return a wrong time.
    with pytest.raises(ValueError):
        CAL.session_open(date(2019, 12, 31))
    with pytest.raises(ValueError):
        CAL.session_close(date(2035, 1, 2))
    with pytest.raises(ValueError):
        CAL.next_session(date(2035, 1, 2))


_WALL_ATTRS = {"now", "today", "utcnow", "time", "time_ns", "monotonic"}
_WALL_TARGETS = {
    "datetime.datetime",
    "datetime.date",
    "pandas.Timestamp",
    "time",
}


def _wall_clock_calls(src: str) -> list[int]:
    tree = ast.parse(src)
    alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                alias[a.asname or a.name] = f"{node.module}.{a.name}"
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _WALL_ATTRS:
            continue
        parts: list[str] = []
        recv: ast.expr = node.func.value
        while isinstance(recv, ast.Attribute):
            parts.insert(0, recv.attr)
            recv = recv.value
        if not isinstance(recv, ast.Name):
            continue
        full = ".".join([alias.get(recv.id, recv.id), *parts])
        if full in _WALL_TARGETS:
            hits.append(node.lineno)
    return hits


def test_no_wall_clock_reads_outside_clock_module_ast() -> None:
    # Stricter than tests/test_no_wall_clock.py: catches aliases (`from datetime import datetime
    # as dt`), datetime.today(), pd.Timestamp.now(), time.time() calls, and exempts only the
    # exact path trader/market/clock.py rather than any file named clock.py.
    pkg = Path(__file__).resolve().parents[2] / "trader"
    allowed = pkg / "market" / "clock.py"
    offenders = {
        str(p.relative_to(pkg)): lines
        for p in pkg.rglob("*.py")
        if p != allowed and (lines := _wall_clock_calls(p.read_text()))
    }
    assert offenders == {}
    # Self-check that the detector actually catches the forms it claims to.
    probe = (
        "import time\nimport pandas as pd\nfrom datetime import datetime as dt, date\n"
        "dt.now()\ndt.today()\ndate.today()\npd.Timestamp.now()\ntime.time()\n"
    )
    assert len(_wall_clock_calls(probe)) == 5
