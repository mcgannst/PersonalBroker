"""P4-REVIEW: the API's market data never does database work on the event loop (§7.1 Web API: sync DB work
stays off the event loop). `OffLoopMarketData` runs the symbol lookups and the candle cache read and write of
`quotes()` and `candles()` in a worker thread; the plain `MarketDataService` (worker, jobs) keeps them inline.
"""

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.api.services import OffLoopMarketData
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)  # 11:00 ET on a Tuesday session
START = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)


class RecordingFactory:
    """A session factory that records the thread of every session it opens."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.threads: list[int] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Session:
        self.threads.append(threading.get_ident())
        return self.factory(*args, **kwargs)


def _setup(db_factory: sessionmaker[Session]) -> tuple[int, FakeQuestrade]:
    with db_factory() as s:
        sid = add_symbol(s, "AAA", questrade_id=11)
        s.commit()
    qt = FakeQuestrade()
    qt.add_symbol("AAA", 11)
    qt.set_quote(11, "10.00", "10.02", "10.01", NOW)
    bars = [
        Candle(START + timedelta(minutes=5 * i), START + timedelta(minutes=5 * (i + 1)),
               Decimal("10"), Decimal("10.1"), Decimal("9.9"), Decimal("10"), 1000, None)
        for i in range(3)
    ]  # fmt: skip
    qt.add_bars(11, "FiveMinutes", bars)
    return sid, qt


async def test_the_api_market_data_reads_and_writes_the_database_off_the_loop(
    db_factory: sessionmaker[Session],
) -> None:
    sid, qt = _setup(db_factory)
    factory = RecordingFactory(db_factory)
    data = OffLoopMarketData(factory, FixedClock(NOW), SessionCalendar(), qt)  # type: ignore[arg-type]
    loop_thread = threading.get_ident()

    quotes = await data.quotes([sid])
    candles = await data.candles(sid, START, START + timedelta(minutes=15), "FiveMinutes")

    assert quotes[sid].symbol_id == sid
    assert len(candles) == 3
    assert len(factory.threads) >= 4  # the id lookups, the cache read and the cache write
    assert loop_thread not in factory.threads
    with db_factory() as s:  # the complete bars were cached
        assert len(s.execute(select(m.IntradayCandle).where(m.IntradayCandle.symbol_id == sid)).all()) == 3


async def test_the_plain_service_keeps_its_database_steps_inline(db_factory: sessionmaker[Session]) -> None:
    sid, qt = _setup(db_factory)
    factory = RecordingFactory(db_factory)
    data = MarketDataService(factory, FixedClock(NOW), SessionCalendar(), qt)  # type: ignore[arg-type]
    await data.quotes([sid])
    assert set(factory.threads) == {threading.get_ident()}
