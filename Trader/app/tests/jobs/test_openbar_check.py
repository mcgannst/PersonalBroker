"""QUOTEBAR: the ~09:47 ET shadow check. Once the delayed official 09:30-09:35 candle is available, it is
fetched for a sample (at most 100: every symbol that passed or entered first) and compared with the bar the
9:35 scan built from quotes. The result is stored on `opening_bar_quotes` and summarised in the job detail and
in the post-close summary line."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest
from trader.db import models as m
from trader.jobs.openbar_check import (
    MAX_SAMPLE,
    OpenbarCheckDeps,
    OpenbarNotReady,
    quote_bars_line,
    run_openbar_check,
    sample_ids,
)
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle
from trader.notify.types import QuoteBarsLineView
from trader.strategies.orb_sip import OrbSipParams

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)
BAR_END = OPEN + timedelta(minutes=5)
T_CHECK = OPEN + timedelta(minutes=17)  # 09:47 ET


def c5(o: str, h: str, low: str, c: str, v: int) -> Candle:
    return Candle(OPEN, BAR_END, Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None)


def quote_row(sid: int, o: str, h: str, low: str, c: str, volume: int) -> m.OpeningBarQuote:
    return m.OpeningBarQuote(
        session_date=DAY,
        symbol_id=sid,
        captured_at=BAR_END + timedelta(seconds=5),
        quote_time=BAR_END + timedelta(seconds=4),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        quote_volume=int(volume * 1.4),
        volume=volume,
        vol_factor=Decimal("0.714300"),
        factor_source="default",
    )


def world(factory: sessionmaker[Session], n: int = 3) -> tuple[int, dict[str, int], FakeQuestrade]:
    fq = FakeQuestrade()
    ids: dict[str, int] = {}
    with factory() as s:
        run_id = add_run(s)
        for i in range(n):
            t = f"T{i:03d}"
            ids[t] = add_symbol(s, t, questrade_id=500 + i)
            s.add(
                m.OpenBarStat(
                    symbol_id=ids[t],
                    session_date=DAY,
                    avg_open_vol_14d=Decimal("1000.00"),
                    atr14=Decimal("1"),
                )
            )
        s.commit()
    return run_id, ids, fq


def deps(factory: sessionmaker[Session], fq: Any, run_id: int | None) -> OpenbarCheckDeps:
    clock = FixedClock(T_CHECK)
    return OpenbarCheckDeps(
        factory=factory,
        clock=clock,
        calendar=CAL,
        data=MarketDataService(factory, clock, CAL, fq),
        run_id=run_id,
        params=OrbSipParams,
    )


async def test_the_check_compares_quote_bars_with_the_official_candles(
    db_factory: sessionmaker[Session],
) -> None:
    run_id, ids, fq = world(db_factory)
    a, b, c = ids["T000"], ids["T001"], ids["T002"]
    with db_factory() as s:
        # A: exact prices, volume 5% high. B: the quote's high is 0.09 above the candle's and its volume 1,300
        # vs 900: rvol 1.3 passes on the quote bar, 0.9 fails on the official one (the decision differs)
        s.add(quote_row(a, "20.00", "20.50", "19.90", "20.40", 1_050))
        s.add(quote_row(b, "20.00", "20.59", "19.90", "20.40", 1_300))
        s.add(quote_row(c, "20.00", "20.50", "19.90", "20.40", 1_000))
        s.commit()
    fq.add_bars(500, "FiveMinutes", [c5("20.00", "20.50", "19.90", "20.38", 1_000)])
    fq.add_bars(501, "FiveMinutes", [c5("20.00", "20.50", "19.90", "20.40", 900)])
    fq.errors[502] = 500  # C: no official candle
    detail = await run_openbar_check(deps(db_factory, fq, run_id), DAY)
    assert detail["sample"] == 3 and detail["compared"] == 2
    assert detail["prices_exact"] == 1 and detail["volume_within_10pct"] == 1
    assert detail["decision_differs"] == 1 and detail["differing"] == ["T001"]
    assert detail["missing"] == 1 and detail["missing_reasons"] == {"questrade_error: HTTP 500": 1}
    assert detail["source"] == "quotes"
    with db_factory() as s:
        rows = {r.symbol_id: r for r in s.execute(select(m.OpeningBarQuote)).scalars()}
        cached = s.execute(select(func.count()).select_from(m.IntradayCandle)).scalar_one()
    ra = rows[a]
    assert ra.checked_at == T_CHECK and ra.check_status == "compared"
    assert (ra.official_open, ra.official_high, ra.official_low, ra.official_close, ra.official_volume) == (
        Decimal("20.0000"),
        Decimal("20.5000"),
        Decimal("19.9000"),
        Decimal("20.3800"),
        1_000,
    )
    assert ra.decision_differs is False
    assert rows[b].decision_differs is True
    assert rows[c].check_status == "questrade_error: HTTP 500" and rows[c].official_volume is None
    assert cached == 2  # the official candles are cached as candles (they are official)
    line = quote_bars_line(db_factory, DAY)
    assert line == QuoteBarsLineView(
        quote_bars=3, compared=2, prices_exact=1, volume_within=1, decision_differs=1
    )


async def test_the_sample_takes_passed_and_entered_symbols_first(db_factory: sessionmaker[Session]) -> None:
    run_id, ids, _ = world(db_factory, n=150)
    tickers = sorted(ids)
    with db_factory() as s:
        for i, t in enumerate(tickers):  # T000 has the largest volume, T149 the smallest
            s.add(quote_row(ids[t], "20", "21", "19", "20.5", 10_000 - i))
        for rank, t in enumerate(["T149", "T148"], start=1):
            s.add(
                m.Candidate(
                    run_id=run_id,
                    session_date=DAY,
                    strategy_key="orb_sip",
                    symbol_id=ids[t],
                    rvol=Decimal("3"),
                    rank=rank,
                    candle={},
                    passed=t == "T149",
                    reject_reason=None if t == "T149" else "catalyst_missing",
                    data={},
                    created_at=BAR_END,
                )
            )
        s.add(
            m.Position(
                run_id=run_id,
                symbol_id=ids["T147"],
                qty=1,
                avg_price=Decimal("20.5"),
                session_date=DAY,
                opened_at=BAR_END + timedelta(minutes=1),
                entry_order_id=1,
                unprotected_seconds=0,
            )
        )
        s.commit()
    got = sample_ids(db_factory, run_id, DAY)
    assert len(got) == MAX_SAMPLE == 100
    assert got[:3] == [ids["T149"], ids["T147"], ids["T148"]]  # passed, entered, then ranked
    assert got[3:6] == [ids["T000"], ids["T001"], ids["T002"]]  # then by volume


async def test_all_401_means_the_candle_is_not_published_yet(db_factory: sessionmaker[Session]) -> None:
    run_id, ids, fq = world(db_factory, n=2)
    with db_factory() as s:
        for sid in ids.values():
            s.add(quote_row(sid, "20", "21", "19", "20.5", 1_000))
        s.commit()

    class Package401(FakeQuestrade):
        async def candles_many(
            self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None
        ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
            return {r: QuestradeApiError(401, "x", code=1022, qt_message="market data package") for r in reqs}

    with pytest.raises(OpenbarNotReady, match="2 of 2"):
        await run_openbar_check(deps(db_factory, Package401(), run_id), DAY)
    with db_factory() as s:  # nothing half-written: the retry starts clean
        assert (
            s.execute(select(func.count()).where(m.OpeningBarQuote.checked_at.is_not(None))).scalar_one() == 0
        )


async def test_a_day_without_quote_bars_is_skipped(db_factory: sessionmaker[Session]) -> None:
    run_id, _, fq = world(db_factory, n=1)
    detail = await run_openbar_check(deps(db_factory, fq, run_id), DAY)
    assert detail == {"session_date": DAY.isoformat(), "skipped": "no quote-built opening bars"}
    assert fq.calls == []
    assert quote_bars_line(db_factory, DAY) is None


def test_the_line_counts_only_this_session(db_factory: sessionmaker[Session]) -> None:
    _, ids, _ = world(db_factory, n=1)
    with db_factory() as s:
        row = quote_row(ids["T000"], "20", "21", "19", "20.5", 1_000)
        row.session_date = date(2026, 10, 5)
        s.add(row)
        s.commit()
    assert quote_bars_line(db_factory, DAY) is None
    line = quote_bars_line(db_factory, date(2026, 10, 5))
    assert line == QuoteBarsLineView(
        quote_bars=1, compared=0, prices_exact=0, volume_within=0, decision_differs=0
    )
