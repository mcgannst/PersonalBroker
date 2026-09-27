from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.parser import UniverseRow
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtSymbol
from trader.db.models import DailyCandle, EventLog, IntradayCandle, OpenBarStat, Symbol, UniverseSnapshot
from trader.jobs.nightly import (
    NightlyDeps,
    earliest_run_time,
    run_nightly,
    sessions_between,
    target_session,
)
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
    assert detail["candle_error_tickers"] == ["AAPL"]
    assert detail["universe"] == 3  # AAPL, MSFT, SPY resolved


class StableMarket(FakeMarket):
    """Like FakeMarket, but a name keeps its Questrade id across runs (share one instance per test)."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, int] = {}

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        for n in names:
            self.ids.setdefault(n, 2000 + len(self.ids))
        return {n: QtSymbol(self.ids[n], n, "NASDAQ", "USD", n, True, True) for n in names}


def snapshot(factory: sessionmaker[Session], day: date) -> dict[str, tuple[str, Decimal | None]]:
    with factory() as s:
        rows = s.execute(
            select(Symbol.ticker, UniverseSnapshot.source, UniverseSnapshot.price)
            .join(UniverseSnapshot, UniverseSnapshot.symbol_id == Symbol.id)
            .where(UniverseSnapshot.session_date == day)
        ).all()
    return {t: (src, price) for t, src, price in rows}


def event_rows(factory: sessionmaker[Session], message: str) -> list[tuple[str, Any]]:
    with factory() as s:
        return [
            tuple(r)
            for r in s.execute(select(EventLog.level, EventLog.data).where(EventLog.message == message))
        ]


async def test_forced_rerun_replaces_the_day(db_factory: sessionmaker[Session]) -> None:
    """A fallback run, then a forced FinViz run for the same session: only the new tickers remain."""
    m = StableMarket()
    await run_nightly(deps(db_factory, FakeFinviz(["AAPL", "MSFT"]), m), date(2026, 9, 25))
    await run_nightly(deps(db_factory, FakeFinviz(None, FinvizBlocked("HTTP 403")), m), TARGET)
    assert set(snapshot(db_factory, TARGET)) == {"AAPL", "MSFT", "SPY"}
    await run_nightly(deps(db_factory, FakeFinviz(["NVDA"]), m), TARGET)
    snap = snapshot(db_factory, TARGET)
    assert set(snap) == {"NVDA", "SPY"}
    assert {src for src, _ in snap.values()} == {"finviz"}
    with db_factory() as s:
        stats = set(
            s.execute(
                select(Symbol.ticker)
                .join(OpenBarStat, OpenBarStat.symbol_id == Symbol.id)
                .where(OpenBarStat.session_date == TARGET)
            ).scalars()
        )
    assert stats == {"NVDA", "SPY"}
    assert set(snapshot(db_factory, date(2026, 9, 25))) == {"AAPL", "MSFT", "SPY"}  # other days untouched


async def test_reused_ticker_moves_old_row_aside_keeping_its_history(
    db_factory: sessionmaker[Session],
) -> None:
    """META (Questrade id 1, an old ETF) is taken by Meta (id 5000, stored as FB)."""
    with db_factory() as s:
        old = Symbol(ticker="META", exchange="NASDAQ", questrade_id=1, currency="USD", name="Old META ETF")
        s.add_all(
            [old, Symbol(ticker="FB", exchange="NASDAQ", questrade_id=5000, currency="USD", name="Meta")]
        )
        s.flush()
        s.add(
            DailyCandle(
                symbol_id=old.id,
                date=date(2021, 6, 1),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=1,
            )
        )
        s.commit()
        old_id = old.id

    class MetaMarket(FakeMarket):
        async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
            out = await super().symbols_by_names(names)
            out["META"] = QtSymbol(5000, "META", "NASDAQ", "USD", "Meta Platforms", True, True)
            return out

    detail = await run_nightly(deps(db_factory, FakeFinviz(["META"]), MetaMarket()), TARGET)
    assert detail["universe"] == 2
    with db_factory() as s:
        stale = s.get(Symbol, old_id)
        meta = s.execute(select(Symbol).where(Symbol.questrade_id == 5000)).scalar_one()
        old_candles = s.execute(
            select(func.count()).select_from(DailyCandle).where(DailyCandle.symbol_id == old_id)
        ).scalar_one()
    assert stale is not None and (stale.ticker, stale.exchange, stale.questrade_id) == ("META", "STALE-1", 1)
    assert (meta.ticker, meta.exchange) == ("META", "NASDAQ")
    assert old_candles == 1
    events = event_rows(db_factory, "ticker META reused; old symbol row marked stale")
    assert len(events) == 1 and events[0][1]["old_questrade_id"] == 1


def test_stale_exchange_fits_the_column(db_factory: sessionmaker[Session]) -> None:
    from trader.market import repository as repo

    with db_factory() as s:
        s.add(Symbol(ticker="X", exchange="NYSE", questrade_id=123456789012345678, currency="USD"))
        s.commit()
        repo.upsert_symbols(s, [QtSymbol(7, "X", "NYSE", "USD", "New X", True, True)])
        s.commit()
        exchanges = set(s.execute(select(Symbol.exchange)).scalars())
    assert exchanges == {"NYSE", "STALE-12345678901234"}


async def test_fallback_is_an_alert_priced_from_last_daily_close(db_factory: sessionmaker[Session]) -> None:
    m = StableMarket()
    await run_nightly(deps(db_factory, FakeFinviz(["AAPL"]), m), date(2026, 9, 25))
    detail = await run_nightly(deps(db_factory, FakeFinviz(None, FinvizBlocked("HTTP 403")), m), TARGET)
    assert detail["fallback_from"] == "2026-09-25"
    assert detail["fallback_age_sessions"] == 1
    assert detail["fallback_stale"] is False
    # the fallback rows carry no FinViz price: the last daily close (10) is used instead of NULL
    assert snapshot(db_factory, TARGET) == {
        "AAPL": ("fallback", Decimal("10.0000")),
        "SPY": ("fallback", Decimal("10.0000")),
    }
    events = event_rows(db_factory, "FinViz failed; using previous universe")
    assert len(events) == 1 and events[0][0] == "error"
    assert events[0][1]["age_sessions"] == 1
    assert event_rows(db_factory, "fallback universe too old") == []


async def test_fallback_of_a_fallback_reports_the_original_finviz_date(
    db_factory: sessionmaker[Session],
) -> None:
    blocked = FakeFinviz(None, FinvizBlocked("HTTP 403"))
    m = StableMarket()
    await run_nightly(deps(db_factory, FakeFinviz(["AAPL"]), m), date(2026, 9, 22))
    await run_nightly(deps(db_factory, blocked, m), date(2026, 9, 25))
    settings = RuntimeSettings.model_validate({"universe.fallback_stale_after_sessions": 3})
    d = NightlyDeps(db_factory, CLOCK, CAL, blocked, m, settings)
    detail = await run_nightly(d, TARGET)
    assert detail["fallback_from"] == "2026-09-22"
    assert detail["fallback_age_sessions"] == 4  # 23, 24, 25, 28 Sep
    assert detail["fallback_stale"] is True
    stale = event_rows(db_factory, "fallback universe too old")
    assert len(stale) == 1 and stale[0][0] == "error"
    assert stale[0][1] == {"from": "2026-09-22", "age_sessions": 4, "stale_after_sessions": 3}
    assert set(snapshot(db_factory, TARGET)) == {"AAPL", "SPY"}  # still used; Phase 2 decides


def test_sessions_between_counts_sessions_after_start_up_to_end() -> None:
    assert sessions_between(CAL, date(2026, 9, 25), TARGET) == 1  # Fri -> Mon
    assert sessions_between(CAL, TARGET, TARGET) == 0
    assert sessions_between(CAL, date(2026, 11, 25), date(2026, 11, 30)) == 2  # skips Thanksgiving


async def test_candles_are_fetched_in_chunks_of_50_symbols(db_factory: sessionmaker[Session]) -> None:
    class CountingMarket(FakeMarket):
        def __init__(self) -> None:
            super().__init__()
            self.batches: list[int] = []

        async def candles_many(
            self, reqs: Sequence[CandleRequest]
        ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
            self.batches.append(len(reqs))
            return await super().candles_many(reqs)

    market = CountingMarket()
    tickers = [f"T{i:03d}" for i in range(120)]
    detail = await run_nightly(deps(db_factory, FakeFinviz(tickers), market), TARGET)
    assert detail["universe"] == 121
    assert market.batches == [100, 100, 42]  # 50 symbols x (daily + 5-minute) per call


@pytest.mark.parametrize(("bars", "expected"), [(9, None), (10, Decimal("1000.00"))])
async def test_opening_average_needs_ten_bars(
    db_factory: sessionmaker[Session], bars: int, expected: Decimal | None
) -> None:
    class ThinMarket(FakeMarket):
        async def candles_many(
            self, reqs: Sequence[CandleRequest]
        ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
            out = await super().candles_many(reqs)
            for r, v in out.items():
                if r.interval == "FiveMinutes" and isinstance(v, list):
                    out[r] = v[-bars:]
            return out

    await run_nightly(deps(db_factory, FakeFinviz(["AAPL"]), ThinMarket()), TARGET)
    with db_factory() as s:
        avgs = set(s.execute(select(OpenBarStat.avg_open_vol_14d)).scalars())
    assert avgs == {expected}


def test_earliest_run_time_is_15_minutes_after_the_last_lookback_close() -> None:
    assert earliest_run_time(CAL, TARGET, 14) == datetime(2026, 9, 25, 20, 15, tzinfo=UTC)  # 16:15 EDT
    # Black Friday closes at 13:00 ET, so the Monday after may start from 13:15 ET
    assert earliest_run_time(CAL, date(2026, 11, 30), 14) == datetime(2026, 11, 27, 18, 15, tzinfo=UTC)
