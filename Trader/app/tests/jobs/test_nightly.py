from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.parser import UniverseRow
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtSymbol
from trader.db.models import DailyCandle, IntradayCandle, OpenBarStat, Symbol, UniverseSnapshot
from trader.jobs.nightly import NightlyDeps, run_nightly, target_session
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
CLOCK = FixedClock(datetime(2026, 9, 28, 0, 0, tzinfo=UTC))  # Sun 27 Sep, 20:00 ET
TARGET = date(2026, 9, 28)


class FakeFinviz:
    def __init__(self, tickers: list[str] | None, error: Exception | None = None) -> None:
        self.tickers, self.error, self.calls = tickers or [], error, 0

    def universe(self, filters: str) -> list[UniverseRow]:
        self.calls += 1
        if self.error:
            raise self.error
        return [
            UniverseRow(t, f"{t} Inc", "Tech", "Software", Decimal("20"), 2_000_000) for t in self.tickers
        ]


class FakeMarket:
    """Every symbol gets 20 daily bars (TR = 1) and a 9:30 bar of volume 1000 per session."""

    def __init__(self, unknown: frozenset[str] = frozenset(), failing: frozenset[int] = frozenset()) -> None:
        self.unknown, self.failing = unknown, failing

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        return {
            n: QtSymbol(1000 + i, n, "NASDAQ", "USD", n, True, True)
            for i, n in enumerate(names)
            if n not in self.unknown
        }

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        out: dict[CandleRequest, list[Candle] | QuestradeApiError] = {}
        for r in reqs:
            if r.symbol_id in self.failing:
                out[r] = QuestradeApiError(400, "bad")
            elif r.interval == "OneDay":
                days = [r.end - timedelta(days=i) for i in range(20, 0, -1)]
                out[r] = [
                    Candle(
                        d,
                        d + timedelta(days=1),
                        Decimal("10"),
                        Decimal("10.5"),
                        Decimal("9.5"),
                        Decimal("10"),
                        1_500_000,
                        None,
                    )
                    for d in days
                ]
            else:
                sessions = CAL.sessions_before(TARGET, 14)
                out[r] = [
                    Candle(
                        CAL.session_open(s),
                        CAL.session_open(s) + timedelta(minutes=5),
                        Decimal("10"),
                        Decimal("11"),
                        Decimal("9"),
                        Decimal("10.5"),
                        1000,
                        None,
                    )
                    for s in sessions
                ]
        return out


def deps(factory: sessionmaker[Session], finviz: FakeFinviz, market: FakeMarket | None = None) -> NightlyDeps:
    return NightlyDeps(factory, CLOCK, CAL, finviz, market or FakeMarket(), RuntimeSettings())


def count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as s:
        return int(s.execute(select(func.count()).select_from(model)).scalar_one())


def test_target_session_is_next_trading_day() -> None:
    assert target_session(CAL, CLOCK) == TARGET  # Sunday evening -> Monday


async def test_nightly_builds_universe_stats_and_candles(db_factory: sessionmaker[Session]) -> None:
    detail = await run_nightly(deps(db_factory, FakeFinviz(["AAPL", "BF-B"])), TARGET)
    assert detail["source"] == "finviz"
    assert detail["universe"] == 3  # AAPL, BF.B, plus SPY
    with db_factory() as s:
        tickers = set(s.execute(select(Symbol.ticker)).scalars())
        stat = s.execute(
            select(OpenBarStat)
            .join(Symbol, Symbol.id == OpenBarStat.symbol_id)
            .where(Symbol.ticker == "AAPL")
        ).scalar_one()
    assert tickers == {"AAPL", "BF.B", "SPY"}
    assert stat.session_date == TARGET
    assert stat.avg_open_vol_14d == Decimal("1000.00")
    assert stat.atr14 == Decimal("1.0000")
    assert count(db_factory, UniverseSnapshot) == 3
    assert count(db_factory, DailyCandle) == 60
    assert count(db_factory, IntradayCandle) == 3 * 14  # opening bars kept


async def test_nightly_is_idempotent(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 5: running twice gives the same rows, no duplicates."""
    d = deps(db_factory, FakeFinviz(["AAPL"]))
    await run_nightly(d, TARGET)
    before = [
        count(db_factory, m) for m in (Symbol, UniverseSnapshot, DailyCandle, IntradayCandle, OpenBarStat)
    ]
    await run_nightly(d, TARGET)
    after = [
        count(db_factory, m) for m in (Symbol, UniverseSnapshot, DailyCandle, IntradayCandle, OpenBarStat)
    ]
    assert before == after


async def test_nightly_falls_back_to_previous_universe(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 3: FinViz blocked -> use the last stored universe, marked as a fallback."""
    await run_nightly(deps(db_factory, FakeFinviz(["AAPL", "MSFT"])), date(2026, 9, 25))
    detail = await run_nightly(deps(db_factory, FakeFinviz(None, FinvizBlocked("HTTP 403"))), TARGET)
    assert detail["source"] == "fallback"
    assert detail["fallback_from"] == "2026-09-25"
    with db_factory() as s:
        sources = set(
            s.execute(
                select(UniverseSnapshot.source).where(UniverseSnapshot.session_date == TARGET)
            ).scalars()
        )
    assert sources == {"fallback"}


async def test_daily_candles_unsorted_or_duplicated_are_normalised(db_factory: sessionmaker[Session]) -> None:
    """A reversed, duplicated daily list from the API must not abort the job (P1-T8 contract)."""

    class MessyMarket(FakeMarket):
        async def candles_many(
            self, reqs: Sequence[CandleRequest]
        ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
            out = await super().candles_many(reqs)
            for r, v in out.items():
                if r.interval == "OneDay" and isinstance(v, list):
                    out[r] = list(reversed(v)) + v[:2]  # newest-first plus two duplicates
            return out

    detail = await run_nightly(deps(db_factory, FakeFinviz(["AAPL"]), MessyMarket()), TARGET)
    assert detail["universe"] == 2
    assert count(db_factory, DailyCandle) == 40
    with db_factory() as s:
        atrs = set(s.execute(select(OpenBarStat.atr14)).scalars())
    assert atrs == {Decimal("1.0000")}


async def test_nightly_without_any_universe_raises(db_factory: sessionmaker[Session]) -> None:
    with pytest.raises(FinvizBlocked):
        await run_nightly(deps(db_factory, FakeFinviz(None, FinvizBlocked("HTTP 403"))), TARGET)


async def test_unknown_and_failing_symbols_are_reported_not_fatal(db_factory: sessionmaker[Session]) -> None:
    market = FakeMarket(unknown=frozenset({"ZZZZ"}), failing=frozenset({1000}))
    detail = await run_nightly(deps(db_factory, FakeFinviz(["AAPL", "ZZZZ", "MSFT"]), market), TARGET)
    assert detail["unresolved"] == ["ZZZZ"]
    assert detail["candle_errors"] == 2  # AAPL's daily and 5-minute requests both failed
    assert detail["universe"] == 3  # AAPL, MSFT, SPY resolved
