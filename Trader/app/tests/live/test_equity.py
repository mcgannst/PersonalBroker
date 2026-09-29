"""DB-T3 acceptance test 15 (pure): downsampling the equity series to at most 500 points keeping every
bucket's extremes and the first and last point (live dashboard plan S6).
"""

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.api.livedata.equity import downsample
from trader.api.schemas import EquityPointLiveOut

T0 = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)


def _series(values: list[Decimal], step_s: float = 5.0) -> list[EquityPointLiveOut]:
    return [
        EquityPointLiveOut(ts=T0 + timedelta(seconds=i * step_s), equity=v, source="marks")
        for i, v in enumerate(values)
    ]


def _wave(n: int, seed: int = 7) -> list[Decimal]:
    rnd = random.Random(seed)
    return [Decimal(str(round(10000 + 40 * math.sin(i / 37) + rnd.uniform(-5, 5), 4))) for i in range(n)]


@pytest.mark.parametrize("n", [0, 1, 2, 499, 500])
def test_short_series_are_unchanged(n: int) -> None:
    points = _series(_wave(n))
    out, flag = downsample(points)
    assert out == points and flag is False


def test_ten_thousand_points_keep_the_extremes_ends_and_order() -> None:
    values = _wave(10_000)
    values[4321] = Decimal("9000.0000")  # the global minimum, a deep drawdown in the middle
    values[8765] = Decimal("11000.0000")  # the global maximum
    points = _series(values)
    out, flag = downsample(points)
    assert flag is True
    assert 2 < len(out) <= 500
    assert out[0] == points[0] and out[-1] == points[-1]
    assert points[4321] in out and points[8765] in out
    assert [p.ts for p in out] == sorted(p.ts for p in out)
    assert len({p.ts for p in out}) == len(out)
    assert all(p in points for p in out)
    assert downsample(points) == (out, True)  # deterministic


def test_extremes_at_the_ends_still_fit_the_cap() -> None:
    values = _wave(5_000)
    values[1] = Decimal("1.0000")  # both global extremes inside the first bucket, next to the first point
    values[2] = Decimal("99999.0000")
    values[-2] = Decimal("2.0000")  # and the last bucket's extremes next to the last point
    values[-3] = Decimal("88888.0000")
    points = _series(values)
    out, flag = downsample(points)
    assert flag is True and len(out) <= 500
    for i in (0, 1, 2, len(points) - 1):
        assert points[i] in out
    assert [p.ts for p in out] == sorted(p.ts for p in out)


def test_a_smaller_cap_is_honoured() -> None:
    points = _series(_wave(1_000))
    out, flag = downsample(points, max_points=50)
    assert flag is True and len(out) <= 50
    assert out[0] == points[0] and out[-1] == points[-1]
    lo = min(points, key=lambda p: p.equity)
    hi = max(points, key=lambda p: p.equity)
    assert lo in out and hi in out


def test_points_at_one_instant_do_not_divide_by_zero() -> None:
    points = [EquityPointLiveOut(ts=T0, equity=Decimal(i), source="marks") for i in range(600)]
    out, flag = downsample(points)
    assert flag is True and len(out) <= 500
    assert out[0] == points[0] and out[-1] == points[-1]
    assert points[0] in out and points[-1] in out
