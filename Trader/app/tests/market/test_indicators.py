from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trader.market.calendar import SessionCalendar
from trader.market.indicators import (
    atr,
    average_volume,
    is_bearish,
    is_doji,
    opening_bar,
    regular_hours,
    rvol,
)
from trader.market.types import Candle

D = Decimal
CAL = SessionCalendar()


def bar(start: datetime, o: str, h: str, low: str, c: str, v: int = 100, minutes: int = 5) -> Candle:
    return Candle(start, start + timedelta(minutes=minutes), D(o), D(h), D(low), D(c), v, None)


def day(n: int, h: str, low: str, c: str) -> Candle:
    start = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=n)
    return Candle(start, start + timedelta(days=1), D(c), D(h), D(low), D(c), 1000, None)


WILDER = [
    day(0, "10", "9", "9.5"),
    day(1, "11", "9.5", "10.5"),
    day(2, "10.8", "10", "10.2"),
    day(3, "12", "10.1", "11.8"),
    day(4, "12.2", "11", "11.1"),
]


def test_atr_first_value_is_simple_average_of_true_ranges() -> None:
    # TRs: 1.5, 0.8, 1.9 -> 1.4
    assert atr(WILDER[:4], period=3) == D("1.4000")


def test_atr_then_wilder_smoothing() -> None:
    # next TR 1.2 -> (1.4*2 + 1.2)/3 = 1.3333
    assert atr(WILDER, period=3) == D("1.3333")


def test_atr_needs_period_plus_one_candles() -> None:
    assert atr(WILDER[:3], period=3) is None


def test_regular_hours_and_opening_bar() -> None:
    session = date(2026, 9, 25)
    open_ = CAL.session_open(session)
    bars = [
        bar(open_ - timedelta(minutes=5), "1", "1", "1", "1"),  # 09:25 pre-market
        bar(open_, "10", "11", "9", "10.5", v=500),
        bar(CAL.session_close(session), "1", "1", "1", "1"),
    ]  # 16:00 after-hours
    rth = regular_hours(bars, CAL, session)
    assert [b.start for b in rth] == [open_]
    ob = opening_bar(bars, CAL, session)
    assert ob is not None and ob.volume == 500


def test_opening_bar_missing() -> None:
    assert opening_bar([], CAL, date(2026, 9, 25)) is None


def test_average_volume_and_rvol() -> None:
    t = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    assert average_volume([bar(t, "1", "1", "1", "1", v=100), bar(t, "1", "1", "1", "1", v=301)]) == D(
        "200.50"
    )
    assert average_volume([]) is None
    assert rvol(403790, D("200000")) == D("2.0190")
    assert rvol(100, None) is None
    assert rvol(100, D("0")) is None


def test_doji_and_bearish() -> None:
    t = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    assert is_doji(bar(t, "10", "11", "9", "10.2"))  # body 0.2 / range 2 = 10%
    assert not is_doji(bar(t, "10", "11", "9", "10.3"))
    assert is_doji(bar(t, "10", "10", "10", "10"))  # zero range
    assert is_bearish(bar(t, "10", "11", "9", "9.5"))
    assert not is_bearish(bar(t, "10", "11", "9", "10"))


def test_atr_rejects_non_positive_period() -> None:
    for period in (0, -1):
        with pytest.raises(ValueError, match="period must be >= 1"):
            atr(WILDER, period=period)


def test_atr_rejects_newest_first_and_duplicate_starts() -> None:
    with pytest.raises(ValueError):
        atr(list(reversed(WILDER)), period=3)
    with pytest.raises(ValueError):
        atr([WILDER[0], WILDER[1], WILDER[1], WILDER[2]], period=3)


def test_malformed_bar_high_below_low_raises() -> None:
    t = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    broken = bar(t, "10", "9", "11", "10")  # high 9 < low 11
    with pytest.raises(ValueError):
        is_doji(broken)
    with pytest.raises(ValueError):
        is_bearish(broken)


def test_rounding_is_round_half_up_not_half_even() -> None:
    # 1/32 = 0.03125 -> 0.0313 (HALF_EVEN would give 0.0312).
    assert rvol(1, D("32")) == D("0.0313")
    # TRs 0.0001 and 0 -> mean 0.00005 -> 0.0001 (HALF_EVEN would give 0.0000).
    tiny = [day(0, "10", "10", "10"), day(1, "10.0001", "10", "10"), day(2, "10", "10", "10")]
    assert atr(tiny, period=2) == D("0.0001")
    # 5/8 = 0.625 -> 0.63 (HALF_EVEN would give 0.62).
    t = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    vols = [5, 0, 0, 0, 0, 0, 0, 0]
    assert average_volume([bar(t, "1", "1", "1", "1", v=v) for v in vols]) == D("0.63")


def test_session_helpers_raise_for_non_session_dates() -> None:
    saturday = date(2026, 9, 26)
    with pytest.raises(ValueError):
        regular_hours([], CAL, saturday)
    with pytest.raises(ValueError):
        opening_bar([], CAL, saturday)
