"""DB-T4 acceptance test 1 and the pure parts of the positions panel (live dashboard plan S16, DB-T4).

`mark_state` (live up to `MARK_STALE_SECONDS`, stale after, missing without a mark) and the sparkline
reduction (at most `SPARK_POINTS`: every k-th bar close, always keeping the last, then the mark).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.api.livedata.positions import mark_state, spark_from
from trader.api.livedata.types import MARK_STALE_SECONDS, SPARK_POINTS
from trader.api.schemas import BarOut

NOW = datetime(2026, 10, 7, 18, 30, tzinfo=UTC)


def _bars(n: int) -> list[BarOut]:
    start = NOW - timedelta(minutes=n)
    return [
        BarOut(
            start=start + timedelta(minutes=i),
            open=Decimal(10),
            high=Decimal(11),
            low=Decimal(9),
            close=Decimal(10) + Decimal(i) / 100,
            source="marks",
        )
        for i in range(n)
    ]


def test_a_mark_30_seconds_old_is_live_and_one_a_millisecond_older_is_stale() -> None:
    assert MARK_STALE_SECONDS == 30
    assert mark_state(NOW - timedelta(seconds=30), NOW) == "live"
    assert mark_state(NOW - timedelta(seconds=30, milliseconds=1), NOW) == "stale"
    assert mark_state(NOW, NOW) == "live"
    assert mark_state(None, NOW) == "missing"


def test_the_spark_keeps_every_bar_close_when_they_fit_then_the_mark() -> None:
    bars = _bars(10)
    spark = spark_from(bars, Decimal("10.50"), NOW)
    assert [p.price for p in spark[:-1]] == [b.close for b in bars]
    assert (spark[-1].ts, spark[-1].price) == (NOW, Decimal("10.50"))


def test_a_long_spark_is_reduced_to_at_most_60_points_keeping_the_last_bar() -> None:
    for n in (59, 60, 61, 118, 119, 390):
        bars = _bars(n)
        spark = spark_from(bars, Decimal("12"), NOW)
        assert len(spark) <= SPARK_POINTS, n
        assert spark[-2].ts == bars[-1].start  # the last bar is always kept
        assert spark[-1].price == Decimal("12")  # and the spark ends at the mark
        times = [p.ts for p in spark]
        assert times == sorted(times)


def test_without_a_mark_the_spark_ends_at_the_last_bar() -> None:
    bars = _bars(5)
    spark = spark_from(bars, None, None)
    assert len(spark) == 5 and spark[-1].ts == bars[-1].start
    assert spark_from([], None, None) == []
    only_mark = spark_from([], Decimal("3"), NOW)
    assert [(p.ts, p.price) for p in only_mark] == [(NOW, Decimal("3"))]
