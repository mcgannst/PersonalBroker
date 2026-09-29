"""QUOTEBAR: `MarketDataService.opening_bars` with `opening_bar_source="quotes"` (the live 9:35 scan).

At 09:35:05 the Questrade package serves candles only ~10 minutes late (HTTP 401 code 1022), while quotes are
real time. The opening bar is built from ONE batched quotes pass (<= 100 ids a request): open/high/low are the
quote's session open/high/low, close its last regular-hours trade, volume the quote's consolidated volume
converted to candle scale. Quote-built bars are stored in `opening_bar_quotes`, never as candles."""

import dataclasses
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.quote_bars import DEFAULT_VOLUME_FACTOR
from trader.market.types import Candle

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
PREV = date(2026, 10, 5)
OPEN = CAL.session_open(DAY)  # 13:30Z
BAR_END = OPEN + timedelta(minutes=5)
T_ORB = BAR_END + timedelta(seconds=5)  # 09:35:05 ET
QT_BASE = "https://api05.iq.questrade.com/v1/"


def quote(qid: int, o: str, h: str, low: str, c: str, volume: int, **kw: Any) -> QtQuote:
    values: dict[str, Any] = {
        "symbol_id": qid,
        "symbol": f"Q{qid}",
        "bid": Decimal(c) - Decimal("0.01"),
        "ask": Decimal(c) + Decimal("0.01"),
        "last": Decimal(c),
        "last_regular": Decimal(c),
        "volume": volume,
        "last_trade_time": T_ORB - timedelta(seconds=1),
        "delay": 0,
        "is_halted": False,
        "vwap": None,
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(low),
    }
    values.update(kw)
    return QtQuote(**values)


def c5(o: str, h: str, low: str, c: str, v: int) -> Candle:
    return Candle(OPEN, BAR_END, Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None)


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
        s.commit()
    return out


def factors(factory: sessionmaker[Session], day: date, rows: dict[int, str | None]) -> None:
    with factory() as s:
        for sid, f in rows.items():
            s.add(
                m.QuoteVolumeScale(
                    session_date=day,
                    symbol_id=sid,
                    quote_volume=1_000_000,
                    candle_volume=None if f is None else int(Decimal(f) * 1_000_000),
                    factor=None if f is None else Decimal(f),
                    recorded_at=CAL.session_close(day) + timedelta(minutes=15),
                )
            )
        s.commit()


def svc(factory: sessionmaker[Session], client: Any, now: datetime = T_ORB, **kw: Any) -> MarketDataService:
    return MarketDataService(factory, FixedClock(now), CAL, client, opening_bar_source="quotes", **kw)


class RecordingQuotes(FakeQuestrade):
    """FakeQuestrade whose quotes() records each request's ids and can fail by call number."""

    def __init__(self) -> None:
        super().__init__()
        self.quote_requests: list[list[int]] = []
        self.quote_errors: dict[int, BaseException] = {}  # call index -> exception

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        n = len(self.quote_requests)
        self.quote_requests.append(list(ids))
        if n in self.quote_errors:
            raise self.quote_errors[n]
        return await super().quotes(ids)


# --- the mapping and what is stored -------------------------------------------------------------------------
async def test_opening_bars_come_from_one_quotes_pass(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    factors(db_factory, PREV, {ids["AAA"]: "0.6000", ids["BBB"]: "0.7000", ids["CCC"]: "0.8000"})
    fq = RecordingQuotes()
    fq.quote_map[101] = quote(101, "20.00", "20.60", "19.90", "20.50", 10_000)
    fq.quote_map[102] = quote(102, "30.00", "30.10", "29.00", "29.50", 20_000)
    service = svc(db_factory, fq)
    got = await service.opening_bars(DAY)
    assert fq.quote_requests == [[101, 102, 103]]  # one request, no candle call
    assert [c for c in fq.calls if c[0] != "quotes"] == []
    a = got.bars[ids["AAA"]]
    assert (a.start, a.end) == (OPEN, BAR_END)  # the 09:30-09:35 bar, complete at 09:35:05
    assert (a.open, a.high, a.low, a.close) == (
        Decimal("20.00"),
        Decimal("20.60"),
        Decimal("19.90"),
        Decimal("20.50"),
    )
    assert a.volume == 6_000  # 10,000 x the measured 0.60
    assert got.bars[ids["BBB"]].volume == 14_000
    assert got.missing == {ids["CCC"]: "no_quote", ids["DDD"]: "no_questrade_id"}
    assert got.sources == {ids["AAA"]: "quotes", ids["BBB"]: "quotes"}
    with db_factory() as s:
        assert (
            s.execute(select(func.count()).select_from(m.IntradayCandle)).scalar_one() == 0
        )  # never a candle
        rows = {r.symbol_id: r for r in s.execute(select(m.OpeningBarQuote)).scalars()}
    assert set(rows) == {ids["AAA"], ids["BBB"]}
    row = rows[ids["AAA"]]
    assert row.session_date == DAY and row.captured_at == T_ORB
    assert (row.quote_volume, row.volume, row.vol_factor, row.factor_source) == (
        10_000,
        6_000,
        Decimal("0.600000"),
        "symbol",
    )
    assert row.checked_at is None and row.official_volume is None
    scan = service.pop_opening_scan()
    assert scan is not None
    detail = scan.detail()
    assert detail["universe"] == 4 and detail["bars"] == 2 and detail["missing"] == 2
    assert detail["source"] == "quotes"
    assert detail["volume_factors"] == {"symbol": 2}
    assert detail["quote_lag_s"] == 5.0


async def test_a_symbol_without_a_trade_since_the_open_is_missing_no_trade(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    fq = RecordingQuotes()
    fq.quote_map[101] = quote(101, "20.00", "20.60", "19.90", "20.50", 10_000, open=None)
    fq.quote_map[102] = quote(
        102, "30.00", "30.10", "29.00", "29.50", 5_000, last_trade_time=OPEN - timedelta(minutes=10)
    )
    fq.quote_map[103] = quote(103, "10.00", "10.20", "9.90", "10.10", 7_000)
    got = await svc(db_factory, fq).opening_bars(DAY, [ids["AAA"], ids["BBB"], ids["CCC"]])
    assert got.missing == {ids["AAA"]: "no_trade", ids["BBB"]: "no_trade"}
    assert set(got.bars) == {ids["CCC"]}
    assert [c for c in fq.calls if c[0] != "quotes"] == []  # a no-trade symbol has no candle either


async def test_volume_factor_falls_back_to_the_median_then_the_default(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    other = [0]
    with db_factory() as s:
        other[0] = add_symbol(s, "OTH", questrade_id=900)
        s.commit()
    # AAA measured; BBB's factor unusable (NULL); the median of the session's usable factors is 0.65
    factors(db_factory, PREV, {ids["AAA"]: "0.6000", ids["BBB"]: None, other[0]: "0.7000"})
    fq = RecordingQuotes()
    for qid in (101, 102, 103):
        fq.quote_map[qid] = quote(qid, "20.00", "20.60", "19.90", "20.50", 10_000)
    service = svc(db_factory, fq)
    got = await service.opening_bars(DAY, [ids["AAA"], ids["BBB"], ids["CCC"]])
    assert got.bars[ids["AAA"]].volume == 6_000
    assert got.bars[ids["BBB"]].volume == 6_500
    assert got.bars[ids["CCC"]].volume == 6_500
    scan = service.pop_opening_scan()
    assert scan is not None and scan.detail()["volume_factors"] == {"symbol": 1, "median": 2}
    with db_factory() as s:
        src = dict(s.execute(select(m.OpeningBarQuote.symbol_id, m.OpeningBarQuote.factor_source)).all())
    assert src == {ids["AAA"]: "symbol", ids["BBB"]: "median", ids["CCC"]: "median"}


async def test_without_any_measured_factor_the_default_one_over_1_4_is_used(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    fq = RecordingQuotes()
    fq.quote_map[101] = quote(101, "20.00", "20.60", "19.90", "20.50", 14_000)
    service = svc(db_factory, fq)
    got = await service.opening_bars(DAY, [ids["AAA"]])
    assert got.bars[ids["AAA"]].volume == 10_000  # 14,000 x 0.7143 = 10,000.2
    assert DEFAULT_VOLUME_FACTOR == Decimal("0.7143")
    scan = service.pop_opening_scan()
    assert scan is not None and scan.detail()["volume_factors"] == {"default": 1}


async def test_factors_come_from_the_latest_earlier_session_only(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    factors(db_factory, date(2026, 10, 2), {ids["AAA"]: "0.5000"})  # older: ignored when PREV has rows
    factors(db_factory, PREV, {ids["AAA"]: "0.6000"})
    factors(db_factory, DAY, {ids["AAA"]: "0.9000"})  # the same session (a re-run after the close): never
    fq = RecordingQuotes()
    fq.quote_map[101] = quote(101, "20.00", "20.60", "19.90", "20.50", 10_000)
    got = await svc(db_factory, fq).opening_bars(DAY, [ids["AAA"]])
    assert got.bars[ids["AAA"]].volume == 6_000


# --- batching and pacing ------------------------------------------------------------------------------------
async def test_quotes_are_batched_at_most_100_ids_a_request(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sids = [add_symbol(s, f"S{i:03d}", questrade_id=1000 + i) for i in range(250)]
        s.commit()
    fq = RecordingQuotes()
    for i in range(250):
        fq.quote_map[1000 + i] = quote(1000 + i, "20.00", "20.60", "19.90", "20.50", 10_000)
    got = await svc(db_factory, fq).opening_bars(DAY, sids)
    assert [len(r) for r in fq.quote_requests] == [100, 100, 50]
    assert len(got.bars) == 250 and got.missing == {}


class FakeTokens:
    def access(self) -> AccessToken:
        return AccessToken("tok", QT_BASE, T_ORB + timedelta(minutes=30))

    def force_refresh(self, rejected: str | None = None) -> AccessToken:
        return self.access()


@respx.mock
async def test_quotes_go_through_the_paced_market_bucket(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sids = [add_symbol(s, f"S{i:03d}", questrade_id=1000 + i) for i in range(230)]
        s.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        asked = [int(x) for x in request.url.params["ids"].split(",")]
        return httpx.Response(
            200,
            json={
                "quotes": [
                    {
                        "symbol": f"S{i}",
                        "symbolId": i,
                        "lastTradePrice": 20.5,
                        "lastTradePriceTrHrs": 20.5,
                        "volume": 1000,
                        "lastTradeTime": "2026-10-06T09:35:04.000000-04:00",
                        "delay": 0,
                        "openPrice": 20.0,
                        "highPrice": 20.6,
                        "lowPrice": 19.9,
                    }
                    for i in asked
                ]
            },
        )

    route = respx.get(QT_BASE + "markets/quotes").mock(side_effect=handler)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    clock_t = [0.0]

    def monotonic() -> float:
        return clock_t[0]

    async with QuestradeClient(FakeTokens(), FixedClock(T_ORB), sleep=sleep, monotonic=monotonic) as client:
        got = await svc(db_factory, client).opening_bars(DAY, sids)
        assert client.stats["market"].requests == 3
    assert len(got.bars) == 230
    assert [len(c.request.url.params["ids"].split(",")) for c in route.calls] == [100, 100, 30]
    # the bucket spaces the three calls at the market rate (17/s): two waits of 1/17 s on a frozen clock
    assert len(slept) == 2 and all(abs(x - 2 * (1 / 17.0)) < 1e-9 or abs(x - 1 / 17.0) < 1e-9 for x in slept)


# --- failures: fail fast on 401, candle fallback ------------------------------------------------------------
async def test_a_401_fails_fast_and_the_rest_are_not_requested(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sids = [add_symbol(s, f"S{i:03d}", questrade_id=1000 + i) for i in range(250)]
        s.commit()
    fq = RecordingQuotes()
    fq.quote_errors[0] = QuestradeApiError(401, "x", code=1017, qt_message="Access token is invalid")
    for i in range(250):  # the candle fallback: Questrade refuses it as well
        fq.errors[1000 + i] = 401
    service = svc(db_factory, fq)
    got = await service.opening_bars(DAY, sids)
    assert len(fq.quote_requests) == 1  # the other two batches were never sent
    assert got.bars == {}
    assert set(got.missing) == set(sids)
    assert all(r.startswith("questrade_error: HTTP 401") for r in got.missing.values())
    scan = service.pop_opening_scan()
    assert scan is not None and scan.unhealthy and scan.top_reason == "questrade_error: HTTP 401"


async def test_symbols_whose_quotes_failed_fall_back_to_candles(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    fq = RecordingQuotes()
    fq.quote_errors[0] = QuestradeApiError(500, "boom")
    fq.add_bars(101, "FiveMinutes", [c5("21.00", "21.50", "20.90", "21.40", 5_000)])
    service = svc(db_factory, fq)
    got = await service.opening_bars(DAY, [ids["AAA"], ids["BBB"]])
    assert got.bars[ids["AAA"]].volume == 5_000  # an official candle: no scaling
    assert got.sources == {ids["AAA"]: "candles"}
    assert got.missing == {ids["BBB"]: "no_bar_at_open"}
    with db_factory() as s:  # an official candle is cached as before; nothing in opening_bar_quotes
        assert s.execute(select(func.count()).select_from(m.IntradayCandle)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(m.OpeningBarQuote)).scalar_one() == 0
    scan = service.pop_opening_scan()
    assert scan is not None and scan.detail()["source"] == "quotes+candles"


async def test_a_quotes_timeout_falls_back_to_candles(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    import asyncio

    class Slow(RecordingQuotes):
        async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
            await asyncio.sleep(5)
            return []

    fq = Slow()
    fq.add_bars(101, "FiveMinutes", [c5("21.00", "21.50", "20.90", "21.40", 5_000)])
    got = await svc(db_factory, fq, fetch_deadline_s=0.05).opening_bars(DAY, [ids["AAA"]])
    assert set(got.bars) == {ids["AAA"]} and got.sources == {ids["AAA"]: "candles"}


# --- when quotes are not used -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "now",
    [
        BAR_END + timedelta(minutes=5, seconds=1),  # too late: the quote's high/low are no longer the bar's
        BAR_END - timedelta(seconds=1),  # before the bar ends
    ],
)
async def test_outside_the_quote_window_the_candle_path_is_used(
    db_factory: sessionmaker[Session], ids: dict[str, int], now: datetime
) -> None:
    fq = RecordingQuotes()
    fq.quote_map[101] = quote(101, "20.00", "20.60", "19.90", "20.50", 10_000)
    fq.add_bars(101, "FiveMinutes", [c5("21.00", "21.50", "20.90", "21.40", 5_000)])
    got = await svc(db_factory, fq, now=now).opening_bars(DAY, [ids["AAA"]])
    assert fq.quote_requests == []
    if now > BAR_END:
        assert got.bars[ids["AAA"]].open == Decimal("21.00")
    else:
        assert got.missing == {ids["AAA"]: "bar_not_complete"}


async def test_the_default_source_is_candles(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    fq = RecordingQuotes()
    fq.quote_map[101] = quote(101, "20.00", "20.60", "19.90", "20.50", 10_000)
    fq.add_bars(101, "FiveMinutes", [c5("21.00", "21.50", "20.90", "21.40", 5_000)])
    service = MarketDataService(db_factory, FixedClock(T_ORB), CAL, fq)
    got = await service.opening_bars(DAY, [ids["AAA"]])
    assert fq.quote_requests == [] and got.sources == {}
    assert got.bars[ids["AAA"]].open == Decimal("21.00")
    scan = service.pop_opening_scan()
    assert scan is not None and "source" not in scan.detail()


async def test_a_cached_official_candle_wins_over_quotes(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    from trader.market import repository as repo

    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5("21.00", "21.50", "20.90", "21.40", 5_000)])
        s.commit()
    fq = RecordingQuotes()
    fq.quote_map[102] = quote(102, "20.00", "20.60", "19.90", "20.50", 10_000)
    got = await svc(db_factory, fq).opening_bars(DAY, [ids["AAA"], ids["BBB"]])
    assert fq.quote_requests == [[102]]
    assert got.bars[ids["AAA"]].volume == 5_000
    assert got.sources == {ids["AAA"]: "candles", ids["BBB"]: "quotes"}


async def test_a_second_call_replaces_the_stored_quote_bar(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    fq = RecordingQuotes()
    fq.quote_map[101] = quote(101, "20.00", "20.60", "19.90", "20.50", 10_000)
    await svc(db_factory, fq).opening_bars(DAY, [ids["AAA"]])
    fq.quote_map[101] = dataclasses.replace(fq.quote_map[101], high=Decimal("20.70"), volume=12_000)
    await svc(db_factory, fq, now=T_ORB + timedelta(seconds=30)).opening_bars(DAY, [ids["AAA"]])
    with db_factory() as s:
        (row,) = s.execute(select(m.OpeningBarQuote)).scalars().all()
    assert row.high == Decimal("20.7000") and row.quote_volume == 12_000
    assert row.captured_at == T_ORB + timedelta(seconds=30)


# --- the previous session's volume factor (post-close) ------------------------------------------------------
async def test_measure_volume_scale_stores_the_candle_to_quote_ratio(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    close = CAL.session_close(DAY)
    at = close + timedelta(minutes=15)
    fq = RecordingQuotes()
    fq.quote_map[101] = quote(101, "20", "21", "19", "20.5", 1_400_000, last_trade_time=close)
    fq.quote_map[102] = quote(102, "20", "21", "19", "20.5", 1_000_000, last_trade_time=close)
    # a stale quote (yesterday's last trade): not today's volume, never measured
    fq.quote_map[103] = quote(
        103, "20", "21", "19", "20.5", 900_000, last_trade_time=OPEN - timedelta(days=1)
    )
    day_bars = [
        Candle(
            OPEN + timedelta(minutes=5 * i),
            OPEN + timedelta(minutes=5 * (i + 1)),
            *(Decimal("20"),) * 4,
            12_500,
            None,
        )
        for i in range(78)
    ]  # 78 x 12,500 = 975,000 in the regular session
    pre = Candle(
        OPEN - timedelta(minutes=30), OPEN - timedelta(minutes=25), *(Decimal("20"),) * 4, 50_000, None
    )
    fq.add_bars(101, "FiveMinutes", [pre, *day_bars])
    fq.add_bars(102, "FiveMinutes", day_bars[:-1])  # the 15:55 bar is missing: incomplete, no factor
    service = MarketDataService(db_factory, FixedClock(at), CAL, fq)
    detail = await service.measure_volume_scale(DAY)
    assert detail["symbols"] == 4 and detail["measured"] == 1
    assert detail["missing_reasons"] == {"incomplete_day": 1, "stale_quote": 1, "no_questrade_id": 1}
    with db_factory() as s:
        rows = {r.symbol_id: r for r in s.execute(select(m.QuoteVolumeScale)).scalars()}
    a = rows[ids["AAA"]]
    assert (a.quote_volume, a.candle_volume, a.factor) == (1_400_000, 975_000, Decimal("0.696429"))
    assert a.recorded_at == at
    b = rows[ids["BBB"]]
    assert b.factor is None and b.quote_volume == 1_000_000
    assert ids["CCC"] not in rows and ids["DDD"] not in rows
    assert detail["median"] == "0.696429"


async def test_measure_volume_scale_survives_a_quotes_failure(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    fq = RecordingQuotes()
    fq.quote_errors[0] = QuestradeApiError(500, "boom")
    service = MarketDataService(
        db_factory, FixedClock(CAL.session_close(DAY) + timedelta(minutes=15)), CAL, fq
    )
    detail = await service.measure_volume_scale(DAY)
    assert detail["measured"] == 0 and detail["missing_reasons"]["questrade_error: HTTP 500"] == 3
