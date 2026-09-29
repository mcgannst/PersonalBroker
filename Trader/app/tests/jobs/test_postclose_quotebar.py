"""QUOTEBAR: the post-close job measures the day's candle-to-quote volume factor (for tomorrow's 9:35 bars)
and its daily summary carries the shadow check's one line."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.jobs.test_postclose import DAY, FakeData, World, post_close, world  # noqa: F401 (fixture)
from trader.db import models as m
from trader.jobs.postclose import run_postclose
from trader.market.calendar import SessionCalendar
from trader.notify.types import QuoteBarsLineView

pytestmark = pytest.mark.db
CAL = SessionCalendar()
OPEN = CAL.session_open(DAY)


class MeasuringData(FakeData):
    def __init__(self, base: FakeData, fail: bool = False) -> None:
        super().__init__(base.members)
        self.fail = fail
        self.measured: list[date] = []

    async def measure_volume_scale(self, session_date: date) -> dict[str, Any]:
        self.measured.append(session_date)
        if self.fail:
            raise RuntimeError("boom")
        return {"symbols": 3, "measured": 3, "median": "0.690000", "missing_reasons": {}}


def seed_quote_bars(factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    with factory() as s:
        for i, t in enumerate(("AAA", "BBB", "CCC")):
            row = m.OpeningBarQuote(
                session_date=DAY,
                symbol_id=ids[t],
                captured_at=OPEN + timedelta(minutes=5, seconds=5),
                quote_time=None,
                open=Decimal("20"),
                high=Decimal("21"),
                low=Decimal("19"),
                close=Decimal("20.5"),
                quote_volume=1_400,
                volume=1_000,
                vol_factor=Decimal("0.714300"),
                factor_source="default",
            )
            if i < 2:  # AAA and BBB compared: AAA exact within 10%, BBB high off and volume 20% off
                row.checked_at = OPEN + timedelta(minutes=17)
                row.check_status = "compared"
                row.official_open = Decimal("20")
                row.official_high = Decimal("21") if i == 0 else Decimal("20.95")
                row.official_low = Decimal("19")
                row.official_close = Decimal("20.5")
                row.official_volume = 1_000 if i == 0 else 1_250
                row.decision_differs = i == 1
            s.add(row)
        s.commit()


async def test_postclose_measures_the_volume_factor_and_sends_the_quote_bar_line(world: World) -> None:  # noqa: F811
    data = MeasuringData(world.data)
    world.data = data
    seed_quote_bars(world.factory, world.ids)
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    assert data.measured == [DAY]
    assert out["volume_scale"] == {"symbols": 3, "measured": 3, "median": "0.690000", "missing_reasons": {}}
    (view,) = world.summaries()
    assert view.quote_bars == QuoteBarsLineView(
        quote_bars=3, compared=2, prices_exact=1, volume_within=1, decision_differs=1
    )


async def test_a_failing_measurement_never_stops_the_summary(world: World) -> None:  # noqa: F811
    world.data = MeasuringData(world.data, fail=True)
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    assert out["volume_scale"] == {"error": "RuntimeError"}
    assert out["summary_sent"] is True
    (view,) = world.summaries()
    assert view.quote_bars is None  # no quote-built bars today: no line


async def test_data_without_the_measurement_keeps_the_old_result(world: World) -> None:  # noqa: F811
    out = await run_postclose(world.deps(post_close(DAY)), DAY)
    assert "volume_scale" not in out
