from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.db import models as m
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle, UniverseStatus

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
PREV = date(2026, 10, 5)
OPEN = CAL.session_open(DAY)  # 13:30Z
AFTER_BAR = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)


def c5(start: datetime, volume: int = 5000, close: str = "21.40") -> Candle:
    return Candle(
        start,
        start + timedelta(minutes=5),
        Decimal("21.00"),
        Decimal("21.50"),
        Decimal("20.90"),
        Decimal(close),
        volume,
        None,
    )


@pytest.fixture
def ids(db_factory: sessionmaker[Session]) -> dict[str, int]:
    """AAA/BBB/CCC have Questrade ids 101-103; DDD has none."""
    out: dict[str, int] = {}
    with db_factory() as s:
        for i, t in enumerate(["AAA", "BBB", "CCC"]):
            out[t] = add_symbol(s, t, questrade_id=101 + i)
        out["DDD"] = add_symbol(s, "DDD")
        for sid in out.values():
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=sid,
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1.0000"),
                    source="finviz",
                )
            )
            s.add(
                m.OpenBarStat(
                    symbol_id=sid,
                    session_date=DAY,
                    avg_open_vol_14d=Decimal("1000.00"),
                    atr14=Decimal("1.0000"),
                )
            )
        s.commit()
    return out


def service(
    factory: sessionmaker[Session], qt: FakeQuestrade, now: datetime = AFTER_BAR
) -> MarketDataService:
    return MarketDataService(factory, FixedClock(now), CAL, qt)


async def test_universe_and_stats_come_from_the_cache(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    svc = service(db_factory, qt)
    members = await svc.universe(DAY)
    assert [x.ticker for x in members] == ["AAA", "BBB", "CCC", "DDD"]
    assert members[0].atr14 == Decimal("1.0000") and members[0].source == "finviz"
    stats = await svc.open_bar_stats(DAY)
    assert stats[ids["AAA"]].avg_open_vol_14d == Decimal("1000.00")
    assert await svc.universe(date(2026, 10, 7)) == []
    assert await svc.symbol_ids(["AAA", "ZZZ"]) == {"AAA": ids["AAA"]}
    assert qt.calls == []


async def test_universe_status_reads_the_nightly_detail(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    svc = service(db_factory, FakeQuestrade())
    assert await svc.universe_status(DAY) == UniverseStatus("finviz", None, False, None)
    with db_factory() as s:
        s.add(
            m.JobRun(
                job="nightly",
                session_date=DAY,
                started_at=OPEN,
                finished_at=OPEN,
                status="succeeded",
                error=None,
                detail={
                    "source": "fallback",
                    "fallback_from": "2026-09-30",
                    "fallback_stale": True,
                    "fallback_age_sessions": 4,
                },
            )
        )
        s.commit()
    assert await svc.universe_status(DAY) == UniverseStatus("fallback", date(2026, 9, 30), True, 4)
    assert await svc.universe_status(date(2026, 10, 7)) == UniverseStatus(None, None, False, None)


async def test_opening_bars_use_the_cache_first(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5(OPEN)])
        s.commit()
    qt = FakeQuestrade()
    got = await service(db_factory, qt).opening_bars(DAY, [ids["AAA"]])
    assert got.bars[ids["AAA"]].volume == 5000 and got.missing == {}
    assert qt.calls == []


async def test_opening_bars_fetch_the_rest_in_one_batch_and_cache_them(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5(OPEN)])
        s.commit()
    qt = FakeQuestrade()
    qt.add_bars(102, "FiveMinutes", [c5(OPEN, volume=7000), c5(OPEN + timedelta(minutes=5))])
    got = await service(db_factory, qt).opening_bars(DAY)  # the whole universe
    assert set(got.bars) == {ids["AAA"], ids["BBB"]}
    assert got.bars[ids["BBB"]].volume == 7000
    assert got.missing == {ids["CCC"]: "no_bar_at_open", ids["DDD"]: "no_questrade_id"}
    assert qt.calls == [("candles_many", 2)]  # BBB and CCC only, in one batch
    with db_factory() as s:
        cached = (
            s.execute(select(m.IntradayCandle.volume).where(m.IntradayCandle.symbol_id == ids["BBB"]))
            .scalars()
            .all()
        )
    assert cached == [7000]


async def test_opening_bars_report_api_errors_per_symbol(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    """Review Focus 4: a failing symbol is reported with a reason, never raised."""
    qt = FakeQuestrade()
    qt.errors[103] = 500
    qt.add_bars(101, "FiveMinutes", [c5(OPEN)])
    got = await service(db_factory, qt).opening_bars(DAY, [ids["AAA"], ids["CCC"]])
    assert set(got.bars) == {ids["AAA"]}
    assert got.missing == {ids["CCC"]: "questrade_error: HTTP 500"}


async def test_an_incomplete_opening_bar_is_neither_used_nor_cached(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    qt.add_bars(101, "FiveMinutes", [c5(OPEN)])
    early = datetime(2026, 10, 6, 13, 34, 0, tzinfo=UTC)
    got = await service(db_factory, qt, now=early).opening_bars(DAY, [ids["AAA"]])
    assert got.bars == {} and got.missing == {ids["AAA"]: "bar_not_complete"}
    with db_factory() as s:
        assert s.execute(select(m.IntradayCandle)).first() is None


async def test_quotes_are_keyed_by_database_id(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    qt.add_symbol("AAA", 101)
    qt.set_quote(101, "21.00", "21.02", "21.01", AFTER_BAR)
    got = await service(db_factory, qt).quotes([ids["AAA"], ids["DDD"]])
    assert list(got) == [ids["AAA"]]
    assert got[ids["AAA"]].symbol_id == ids["AAA"] and got[ids["AAA"]].ask == Decimal("21.02")
    assert qt.calls == [("quotes", 1)]


async def test_prior_close_from_the_cache_then_questrade(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    with db_factory() as s:
        s.add(
            m.DailyCandle(
                symbol_id=ids["AAA"],
                date=PREV,
                open=Decimal("20"),
                high=Decimal("21"),
                low=Decimal("19"),
                close=Decimal("20.50"),
                volume=1,
                vwap=None,
            )
        )
        s.commit()
    qt = FakeQuestrade()
    prev_start = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 00:00 ET
    qt.add_bars(
        102,
        "OneDay",
        [
            Candle(
                prev_start,
                prev_start + timedelta(days=1),
                Decimal("30"),
                Decimal("31"),
                Decimal("29"),
                Decimal("30.25"),
                1,
                None,
            )
        ],
    )
    svc = service(db_factory, qt)
    assert await svc.prior_close(ids["AAA"], DAY) == Decimal("20.50")
    assert qt.calls == []
    assert await svc.prior_close(ids["BBB"], DAY) == Decimal("30.25")
    assert await svc.prior_close(ids["CCC"], DAY) is None
    assert await svc.prior_closes([ids["AAA"], ids["BBB"], ids["CCC"]], DAY) == {
        ids["AAA"]: Decimal("20.50"),
        ids["BBB"]: Decimal("30.25"),  # cached by the fallback above
    }


async def test_candles_come_from_the_cache_when_complete(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    bars = [c5(OPEN + timedelta(minutes=5 * i)) for i in range(3)]
    qt.add_bars(101, "FiveMinutes", bars)
    svc = service(db_factory, qt)
    end = OPEN + timedelta(minutes=15)
    first = await svc.candles(ids["AAA"], OPEN, end, "FiveMinutes")
    second = await svc.candles(ids["AAA"], OPEN, end, "FiveMinutes")
    assert [c.start for c in first] == [c.start for c in second] == [b.start for b in bars]
    assert qt.calls == [("candles", 1)]  # the second read was served from the cache
