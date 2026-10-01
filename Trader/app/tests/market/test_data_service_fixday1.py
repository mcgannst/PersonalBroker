"""FIX-DAY1: MarketDataService's timed captures and the pre-market-free opening volume (test Postgres, fakes).

- `capture_quotes(day, kind)` stores one batched quotes pass in `opening_quote_captures` with the capture's
  start/end and each quote's last-trade and fetch time.
- `opening_bars` at 09:35:05 builds the bars from the stored 09:35:00 capture (no quotes request for them);
  volume = (09:35 volume - the open capture's volume) x factor; no usable open volume -> candle
  fallback, never the raw day volume; a quote > 2 s after 09:35:00 is `quote_late`.
- `measure_volume_scale` takes the pre-market volume out: the open capture when there is one.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_capture, add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
PREV = date(2026, 10, 5)
OPEN = CAL.session_open(DAY)
BAR_END = OPEN + timedelta(minutes=5)
T_ORB = BAR_END + timedelta(seconds=5)
T_OPEN_CAP = OPEN - timedelta(seconds=2)
T_BAR_CAP = BAR_END + timedelta(milliseconds=150)


class Quotes(FakeQuestrade):
    def __init__(self, clock: FixedClock) -> None:
        super().__init__()
        self.clock = clock
        self.requests: list[list[int]] = []

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        self.requests.append(list(ids))
        out = await super().quotes(ids)
        return [QtQuote(**{**_fields(q), "fetched_at": self.clock.now()}) for q in out]


def _fields(q: QtQuote) -> dict[str, Any]:
    return {f: getattr(q, f) for f in QtQuote.__slots__}


def quote(
    qid: int,
    volume: int,
    at: datetime,
    *,
    o: str | None = "20.00",
    h: str = "20.60",
    low: str = "19.90",
    c: str = "20.50",
    **kw: Any,
) -> QtQuote:
    values: dict[str, Any] = {
        "symbol_id": qid,
        "symbol": f"Q{qid}",
        "bid": Decimal(c) - Decimal("0.01"),
        "ask": Decimal(c) + Decimal("0.01"),
        "last": Decimal(c),
        "last_regular": Decimal(c),
        "volume": volume,
        "last_trade_time": at,
        "delay": 0,
        "is_halted": False,
        "vwap": None,
        "open": None if o is None else Decimal(o),
        "high": None if o is None else Decimal(h),
        "low": None if o is None else Decimal(low),
    }
    values.update(kw)
    return QtQuote(**values)


@pytest.fixture
def ids(db_factory: sessionmaker[Session]) -> dict[str, int]:
    out: dict[str, int] = {}
    with db_factory() as s:
        for i, t in enumerate(["CLDX", "NVTS", "QUIET"]):
            out[t] = add_symbol(s, t, questrade_id=201 + i)
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=out[t],
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1.0000"),
                    source="finviz",
                )
            )
        for sid in out.values():  # a measured factor of 0.7 for everyone
            s.add(
                m.QuoteVolumeScale(
                    session_date=PREV,
                    symbol_id=sid,
                    quote_volume=1_000_000,
                    candle_volume=700_000,
                    factor=Decimal("0.700000"),
                    recorded_at=CAL.session_close(PREV),
                )
            )
        s.commit()
    return out


def svc(factory: sessionmaker[Session], client: Any, clock: FixedClock) -> MarketDataService:
    return MarketDataService(factory, clock, CAL, client, opening_bar_source="quotes")


# --- capture_quotes -----------------------------------------------------------------------------------------
async def test_the_open_capture_stores_the_volume_at_the_open(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    clock = FixedClock(T_OPEN_CAP)
    fq = Quotes(clock)
    fq.quote_map[201] = quote(201, 303_905, OPEN - timedelta(seconds=40), o=None)  # pre-market only
    fq.quote_map[202] = quote(202, 50_000, OPEN - timedelta(minutes=3), o=None)
    detail = await svc(db_factory, fq, clock).capture_quotes(DAY, "open")
    assert fq.requests == [[201, 202, 203]]  # one batched request over the universe
    assert (detail["kind"], detail["symbols"], detail["quoted"], detail["after_open"]) == ("open", 3, 2, 0)
    assert detail["offset_s"] == -2.0  # two seconds before 09:30:00
    with db_factory() as s:
        rows = {r.symbol_id: r for r in s.execute(select(m.OpeningQuoteCapture)).scalars()}
    r = rows[ids["CLDX"]]
    assert (r.kind, r.volume, r.capture_started_at, r.fetched_at) == ("open", 303_905, T_OPEN_CAP, T_OPEN_CAP)
    assert r.quote_time == OPEN - timedelta(seconds=40)


async def test_the_bar_capture_records_start_end_and_counts_late_quotes(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    clock = FixedClock(T_BAR_CAP)
    fq = Quotes(clock)
    fq.quote_map[201] = quote(201, 339_533, BAR_END - timedelta(seconds=1))
    fq.quote_map[202] = quote(202, 900_000, BAR_END + timedelta(seconds=3))  # a print 3 s after 09:35:00
    detail = await svc(db_factory, fq, clock).capture_quotes(DAY, "bar")
    assert (detail["quoted"], detail["late"], detail["offset_s"]) == (2, 1, 0.15)
    assert detail["started_at"] == T_BAR_CAP.isoformat() and detail["ended_at"] == T_BAR_CAP.isoformat()


async def test_a_capture_rerun_replaces_its_rows(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    clock = FixedClock(T_OPEN_CAP)
    fq = Quotes(clock)
    fq.quote_map[201] = quote(201, 1, OPEN - timedelta(seconds=5), o=None)
    await svc(db_factory, fq, clock).capture_quotes(DAY, "open")
    fq.quote_map[201] = quote(201, 2, OPEN - timedelta(seconds=5), o=None)
    await svc(db_factory, fq, clock).capture_quotes(DAY, "open")
    with db_factory() as s:
        assert [r.volume for r in s.execute(select(m.OpeningQuoteCapture)).scalars()] == [2]


# --- the ORB event uses the stored capture ------------------------------------------------------------------
def seed(
    factory: sessionmaker[Session],
    ids: dict[str, int],
    *,
    opens: dict[str, int],
    bars: dict[str, dict[str, Any]],
) -> None:
    with factory() as s:
        for t, v in opens.items():
            add_capture(s, DAY, ids[t], "open", T_OPEN_CAP, volume=v, quote_time=OPEN - timedelta(seconds=30))
        for t, b in bars.items():
            last = b.pop("last", "20.50")
            add_capture(
                s,
                DAY,
                ids[t],
                "bar",
                b.pop("at", T_BAR_CAP),
                volume=b.pop("volume"),
                quote_time=b.pop("quote_time", BAR_END - timedelta(milliseconds=500)),
                open=Decimal(b.pop("open", "20.00")),
                high=Decimal(b.pop("high", "20.60")),
                low=Decimal(b.pop("low", "19.90")),
                last=Decimal(last),
                last_regular=Decimal(last),
            )
        s.commit()


async def test_the_orb_scan_reads_the_stored_0935_capture_and_subtracts_the_volume_at_the_open(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    """CLDX Wed 09-30: 339,533 at 09:35 of which 303,905 pre-market: the bar traded 35,628 (quote scale)."""
    seed(
        db_factory,
        ids,
        opens={"CLDX": 303_905, "NVTS": 0, "QUIET": 0},
        bars={
            "CLDX": {"volume": 339_533},
            "NVTS": {"volume": 100_000, "high": "12.30", "open": "11.80", "low": "11.75", "last": "12.20"},
        },
    )
    clock = FixedClock(T_ORB)
    fq = Quotes(clock)  # QUIET is not in the capture: one fresh request for it only, no quote back
    service = svc(db_factory, fq, clock)
    got = await service.opening_bars(DAY)
    assert fq.requests == [[203]]
    assert got.bars[ids["CLDX"]].volume == 24_940  # 35,628 x 0.7, never 339,533 x 0.7 = 237,673
    assert got.bars[ids["NVTS"]].high == Decimal("12.30")  # the 09:35:00 high
    assert got.missing == {ids["QUIET"]: "no_quote"}
    with db_factory() as s:
        row = s.get(m.OpeningBarQuote, (DAY, ids["CLDX"]))
    assert row is not None
    assert (row.quote_volume, row.open_volume, row.volume, row.volume_basis) == (
        339_533,
        303_905,
        24_940,
        "delta",
    )
    assert row.captured_at == T_BAR_CAP and row.capture_started_at == T_BAR_CAP
    assert row.quote_time == BAR_END - timedelta(milliseconds=500)
    detail = service.pop_opening_scan().detail()  # type: ignore[union-attr]
    assert detail["quote_lag_s"] == 0.15  # the capture's lag, not the event's 5 s
    assert detail["capture"]["symbols"] == 2 and detail["capture"]["started_at"] == T_BAR_CAP.isoformat()
    assert detail["volume_basis"] == {"delta": 2}


async def test_a_lagged_quote_in_the_capture_is_quote_late(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    seed(
        db_factory,
        ids,
        opens={"NVTS": 0},
        bars={
            "NVTS": {"volume": 100_000, "high": "12.3899", "quote_time": BAR_END + timedelta(seconds=6.32)}
        },
    )
    clock = FixedClock(T_ORB)
    got = await svc(db_factory, Quotes(clock), clock).opening_bars(DAY, [ids["NVTS"]])
    assert got.missing == {ids["NVTS"]: "quote_late"} and got.bars == {}


async def test_without_a_stored_capture_a_fresh_pass_at_0935_05_is_mostly_late(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    """The worker missed the 09:35:00 capture: the event's own pass reads quotes 5 s late; an active name's
    last trade is then past 09:35:02, so it is quote_late (not traded on a high that may be post-bar)."""
    seed(db_factory, ids, opens={"CLDX": 0, "QUIET": 0}, bars={})
    clock = FixedClock(T_ORB)
    fq = Quotes(clock)
    fq.quote_map[201] = quote(201, 50_000, T_ORB - timedelta(seconds=1))
    fq.quote_map[203] = quote(203, 1_000, OPEN + timedelta(minutes=3))  # quiet: last trade inside the bar
    service = svc(db_factory, fq, clock)
    got = await service.opening_bars(DAY, [ids["CLDX"], ids["QUIET"]])
    assert got.missing == {ids["CLDX"]: "quote_late"}
    assert got.bars[ids["QUIET"]].volume == 700
    detail = service.pop_opening_scan().detail()  # type: ignore[union-attr]
    assert "capture" not in detail and detail["quote_lag_s"] == 5.0


async def test_no_volume_at_the_open_falls_back_to_candles_never_the_raw_day_volume(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    seed(db_factory, ids, opens={}, bars={"CLDX": {"volume": 339_533}})
    clock = FixedClock(T_ORB)
    fq = Quotes(clock)
    fq.errors[201] = 401  # the official candle is not published yet (code 1022 in real life)
    service = svc(db_factory, fq, clock)
    got = await service.opening_bars(DAY, [ids["CLDX"]])
    assert got.bars == {} and got.missing[ids["CLDX"]].startswith("questrade_error: HTTP 401")
    with db_factory() as s:
        assert s.get(m.OpeningBarQuote, (DAY, ids["CLDX"])) is None  # no bar built on the day volume
    detail = service.pop_opening_scan().detail()  # type: ignore[union-attr]
    assert detail["volume_basis"] == {"no_open_snapshot": 1} and detail["source"] == "quotes+candles"


async def test_a_published_official_candle_serves_a_symbol_without_open_volume(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    seed(db_factory, ids, opens={}, bars={"CLDX": {"volume": 339_533}})
    clock = FixedClock(T_ORB)
    fq = Quotes(clock)
    official = Candle(OPEN, BAR_END, *(Decimal("20"),) * 4, 35_628, None)
    fq.add_bars(201, "FiveMinutes", [official])
    got = await svc(db_factory, fq, clock).opening_bars(DAY, [ids["CLDX"]])
    assert got.bars[ids["CLDX"]].volume == 35_628 and got.sources == {ids["CLDX"]: "candles"}


async def test_an_open_snapshot_that_already_traded_after_09_30_is_not_used(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    seed(db_factory, ids, opens={}, bars={"CLDX": {"volume": 339_533}})
    with db_factory() as s:
        add_capture(
            s, DAY, ids["CLDX"], "open", OPEN, volume=320_000, quote_time=OPEN + timedelta(milliseconds=5)
        )
        s.commit()
    clock = FixedClock(T_ORB)
    fq = Quotes(clock)
    fq.errors[201] = 401
    got = await svc(db_factory, fq, clock).opening_bars(DAY, [ids["CLDX"]])
    assert got.bars == {}


async def test_a_capture_started_outside_the_quote_window_is_ignored(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    seed(
        db_factory, ids, opens={"CLDX": 0}, bars={"CLDX": {"volume": 5, "at": BAR_END - timedelta(seconds=1)}}
    )
    clock = FixedClock(T_ORB)
    fq = Quotes(clock)
    fq.quote_map[201] = quote(201, 10_000, BAR_END + timedelta(seconds=1))
    got = await svc(db_factory, fq, clock).opening_bars(DAY, [ids["CLDX"]])
    assert fq.requests == [[201]] and got.bars[ids["CLDX"]].volume == 7_000


# --- the factor on regular-session volume -------------------------------------------------------------------
async def test_measure_takes_the_open_capture_out_of_the_day_volume(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    close = CAL.session_close(DAY)
    seed(db_factory, ids, opens={"CLDX": 400_000}, bars={})
    clock = FixedClock(close + timedelta(minutes=15))
    fq = Quotes(clock)
    fq.quote_map[201] = quote(201, 1_400_000, close)
    day_bars = [
        Candle(
            OPEN + timedelta(minutes=5 * i),
            OPEN + timedelta(minutes=5 * (i + 1)),
            *(Decimal("20"),) * 4,
            10_000,
            None,
        )
        for i in range(78)
    ]  # 780,000 in the regular session
    pre = Candle(OPEN - timedelta(hours=1), OPEN - timedelta(minutes=55), *(Decimal("20"),) * 4, 99_999, None)
    fq.add_bars(201, "FiveMinutes", [pre, *day_bars])
    detail = await svc(db_factory, fq, clock).measure_volume_scale(DAY, [ids["CLDX"]])
    assert detail["measured"] == 1
    with db_factory() as s:
        row = s.get(m.QuoteVolumeScale, (DAY, ids["CLDX"]))
    assert row is not None
    assert row.factor == Decimal("0.780000")  # 780,000 / (1,400,000 - 400,000); the candles are not used
    assert (row.premarket_volume, row.premarket_source) == (400_000, "snapshot")
