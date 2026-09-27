from datetime import UTC, date, datetime, timedelta

import pytest

from trader.market.clock import FixedClock, RealClock, et_date


def test_real_clock_is_utc_aware() -> None:
    assert RealClock().now().tzinfo is not None


def test_fixed_clock_advance_and_set() -> None:
    c = FixedClock(datetime(2026, 9, 28, 13, 35, tzinfo=UTC))
    c.advance(timedelta(seconds=5))
    assert c.now() == datetime(2026, 9, 28, 13, 35, 5, tzinfo=UTC)
    c.set(datetime(2026, 9, 29, 0, 0, tzinfo=UTC))
    assert c.now().day == 29


def test_fixed_clock_rejects_naive() -> None:
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 9, 28, 13, 35))


def test_et_date_crosses_midnight_utc() -> None:
    # 01:00 UTC on the 29th is still the 28th in New York
    assert et_date(datetime(2026, 9, 29, 1, 0, tzinfo=UTC)) == date(2026, 9, 28)
