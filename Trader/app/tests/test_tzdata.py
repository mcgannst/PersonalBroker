"""P4-T1 acceptance test 6: the pinned pip `tzdata` has Alberta on UTC-6 all year (tzdata 2026c).

The image reads zoneinfo only from the pip package (PYTHONTZPATH empty, T17); here the search path is
emptied the same way, so the check never depends on the Mac's or the container's system zoneinfo.
"""

import zoneinfo
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
import tzdata


@contextmanager
def pip_tzdata_only() -> Iterator[None]:
    """zoneinfo reads the pip `tzdata` package only; the default search path is restored afterwards."""
    zoneinfo.reset_tzpath([])
    zoneinfo.ZoneInfo.clear_cache()
    try:
        yield
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()


def test_tzdata_is_2026c_or_later() -> None:
    assert tzdata.IANA_VERSION >= "2026c"


@pytest.mark.parametrize("day", [datetime(2026, 12, 1, 12), datetime(2027, 7, 1, 12)])
def test_edmonton_is_utc_minus_6_winter_and_summer(day: datetime) -> None:
    with pip_tzdata_only():
        assert zoneinfo.TZPATH == ()
        assert zoneinfo.ZoneInfo("America/Edmonton").utcoffset(day) == timedelta(hours=-6)


def test_the_default_search_path_is_restored() -> None:
    zoneinfo.reset_tzpath()
    default = zoneinfo.TZPATH
    with pip_tzdata_only():
        assert zoneinfo.TZPATH == ()
    assert zoneinfo.TZPATH == default
