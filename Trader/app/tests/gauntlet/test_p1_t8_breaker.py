"""P1-T8 gauntlet: Breaker tests for trader.market.indicators."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trader.market.calendar import SessionCalendar
from trader.market.indicators import (
    atr,
    average_volume,
    is_doji,
    opening_bar,
    regular_hours,
    rvol,
)
from trader.market.types import Candle

D = Decimal
CAL = SessionCalendar()
NY = ZoneInfo("America/New_York")


def bar(start: datetime, o: str = "10", h: str = "11", low: str = "9", c: str = "10", v: int = 100) -> Candle:
    return Candle(start, start + timedelta(minutes=5), D(o), D(h), D(low), D(c), v, None)


def day(n: int, h: str, low: str, c: str, o: str | None = None) -> Candle:
    start = datetime(2026, 8, 3, tzinfo=UTC) + timedelta(days=n)
    return Candle(start, start + timedelta(days=1), D(o or c), D(h), D(low), D(c), 1000, None)


def test_atr_gap_true_range_uses_previous_close() -> None:
    # Gap up: prev close 10, bar 14-15 -> TR = |15 - 10| = 5 (not H-L = 1).
    # Gap down: prev close 14.5, bar 8-9 -> TR = |8 - 14.5| = 6.5 (not H-L = 1).
    daily = [day(0, "10.5", "9.5", "10"), day(1, "15", "14", "14.5"), day(2, "9", "8", "8.5")]
    assert atr(daily[:2], period=1) == D("5.0000")
    assert atr(daily, period=2) == D("5.7500")


def test_atr_exactly_period_plus_one_vs_longer_history() -> None:
    # TRs: 2, 2, 0.5, 3
    daily = [
        day(0, "10.5", "9.5", "10"),
        day(1, "11", "9", "10"),
        day(2, "12", "10", "11"),
        day(3, "11.5", "11", "11.2"),
        day(4, "14", "11", "13"),
    ]
    # Exactly period + 1 candles: simple mean of the period TRs.
    assert atr(daily[:3], period=2) == D("2.0000")
    assert atr(daily[2:], period=2) == D("1.7500")
    # More than period + 1: seeded with the first mean, then Wilder smoothing.
    # 2 -> (2*1 + 0.5)/2 = 1.25 -> (1.25*1 + 3)/2 = 2.125
    assert atr(daily, period=2) == D("2.1250")
    # Exactly period candles (one short) and none at all give None (not a crash).
    assert atr(daily[:2], period=2) is None
    assert atr([], period=14) is None


def test_atr_unsorted_input_is_not_silently_wrong() -> None:
    # The interface takes an unconstrained Sequence and says nothing about order.
    # A caller that passes newest-first daily bars must not get a plausible but
    # wrong ATR (it feeds min_atr filtering and stop_loss sizing, SPEC 5.2).
    # Acceptable: sort by start internally (same answer) or raise ValueError.
    daily = [
        day(0, "10.5", "9.5", "10"),
        day(1, "11", "9", "10"),
        day(2, "12", "10", "11"),
        day(3, "11.5", "11", "11.2"),
        day(4, "14", "11", "13"),
    ]
    expected = atr(daily, period=2)
    try:
        got = atr(list(reversed(daily)), period=2)
    except ValueError:
        return
    assert got == expected, f"unsorted input gave ATR {got}, sorted gives {expected}"


def test_regular_hours_on_early_close_day() -> None:
    # 2026-11-27 (day after Thanksgiving) closes at 13:00 ET = 18:00 UTC.
    session = date(2026, 11, 27)
    bars = [
        bar(datetime(2026, 11, 27, 9, 25, tzinfo=NY)),
        bar(datetime(2026, 11, 27, 9, 30, tzinfo=NY)),
        bar(datetime(2026, 11, 27, 12, 55, tzinfo=NY)),
        bar(datetime(2026, 11, 27, 13, 0, tzinfo=NY)),
        bar(datetime(2026, 11, 27, 15, 55, tzinfo=NY)),
    ]
    rth = regular_hours(bars, CAL, session)
    assert [b.start.astimezone(NY).time().isoformat() for b in rth] == ["09:30:00", "12:55:00"]


def test_non_session_date_raises() -> None:
    bars = [bar(datetime(2026, 11, 26, 9, 30, tzinfo=NY))]
    for d in (date(2026, 11, 26), date(2026, 9, 26)):  # Thanksgiving, Saturday
        with pytest.raises(ValueError):
            regular_hours(bars, CAL, d)
        with pytest.raises(ValueError):
            opening_bar(bars, CAL, d)


def test_opening_bar_needs_exact_0930_bar() -> None:
    session = date(2026, 9, 25)
    # Missing 09:30 bar (first bar at 09:31): no opening bar, no fallback.
    late = [bar(datetime(2026, 9, 25, 9, 31, tzinfo=NY)), bar(datetime(2026, 9, 25, 9, 35, tzinfo=NY))]
    assert opening_bar(late, CAL, session) is None
    # Yesterday's 09:30 bar must not be mistaken for today's.
    stale = [bar(datetime(2026, 9, 24, 9, 30, tzinfo=NY))]
    assert opening_bar(stale, CAL, session) is None
    # An aware 09:30 ET timestamp (not UTC) is still the same instant.
    ny = bar(datetime(2026, 9, 25, 9, 30, tzinfo=NY), v=777)
    ob = opening_bar([*late, ny], CAL, session)
    assert ob is not None and ob.volume == 777


def test_average_volume_rounding_rvol_zero_and_large_volumes() -> None:
    t = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    # 1/8 = 0.125 -> 0.13 with ROUND_HALF_UP (banker's rounding would give 0.12).
    assert average_volume([bar(t, v=1)] + [bar(t, v=0)] * 7) == D("0.13")
    # 4/3 -> 1.33, 5/3 -> 1.67
    assert average_volume([bar(t, v=1), bar(t, v=1), bar(t, v=2)]) == D("1.33")
    assert average_volume([bar(t, v=1), bar(t, v=2), bar(t, v=2)]) == D("1.67")
    # rvol with a zero average in any Decimal spelling.
    assert rvol(100, D("0.00")) is None
    assert rvol(0, D("0.00")) is None
    assert rvol(0, D("200.00")) == D("0.0000")
    # Very large volumes stay exact (no float, no overflow).
    big = [bar(t, v=9_000_000_000_000), bar(t, v=9_000_000_000_001)]
    assert average_volume(big) == D("9000000000000.50")
    assert rvol(18_000_000_000_001, D("9000000000000.50")) == D("2.0000")


def test_is_doji_boundary_and_negative_body() -> None:
    t = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    # Exactly 10% counts as a doji (SPEC 5.2: body/range <= 10%), bullish or bearish.
    assert is_doji(bar(t, "10", "11", "9", "10.2"))
    assert is_doji(bar(t, "10.2", "11", "9", "10"))
    # Awkward range where 0.1 * range is exact but not a round number.
    assert is_doji(bar(t, "20", "20.7", "20", "20.07"))
    assert is_doji(bar(t, "20.07", "20.7", "20", "20"))
    # Just over the line, in both directions.
    assert not is_doji(bar(t, "10", "11", "9", "10.2001"))
    assert not is_doji(bar(t, "10.2001", "11", "9", "10"))
    # Big bearish body is not a doji (abs of a negative body is used).
    assert not is_doji(bar(t, "11", "11", "9", "9"))
