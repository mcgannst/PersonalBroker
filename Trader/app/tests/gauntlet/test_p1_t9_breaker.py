"""P1-T9 Breaker: job runner, market data repository, nightly job and `notify` CLI.

Fakes and the testcontainers database only: never trader_dev, FinViz, Questrade or Telegram.
"""

import asyncio
import threading
import time as time_mod
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from trader.adapters.finviz.parser import UniverseRow
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtSymbol
from trader.db.models import (
    DailyCandle,
    EventLog,
    IntradayCandle,
    JobRun,
    OpenBarStat,
    Symbol,
    UniverseSnapshot,
)
from trader.jobs.nightly import NightlyDeps, run_nightly, target_session
from trader.jobs.runner import JobOutcome, run_job
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock, et_date
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

CAL = SessionCalendar()
CLOCK = FixedClock(datetime(2026, 9, 28, 0, 0, tzinfo=UTC))  # Sun 27 Sep 2026, 20:00 ET
TARGET = date(2026, 9, 28)
MODELS = (Symbol, UniverseSnapshot, DailyCandle, IntradayCandle, OpenBarStat)


class FakeFinviz:
    def __init__(self, tickers: list[str] | None, error: Exception | None = None) -> None:
        self.tickers, self.error = tickers or [], error

    def universe(self, filters: str) -> list[UniverseRow]:
        if self.error:
            raise self.error
        return [
            UniverseRow(t, f"{t} Inc", "Tech", "Software", Decimal("20"), 2_000_000) for t in self.tickers
        ]


class CalendarMarket:
    """Calendar-aware fake: one daily bar per real session in the requested range (TR = 1) and one
    09:30 five-minute bar (volume 1000) per real session. Symbol ids are stable per name."""

    def __init__(
        self,
        ids: dict[str, int] | None = None,
        aliases: dict[str, str] | None = None,
        daily_limit: dict[int, int] | None = None,
    ) -> None:
        self.ids = dict(ids or {})
        self.aliases = aliases or {}
        self.daily_limit = daily_limit or {}

    def _sym(self, name: str) -> QtSymbol:
        real = self.aliases.get(name, name)
        if real not in self.ids:
            self.ids[real] = 1000 + len(self.ids)
        return QtSymbol(self.ids[real], real, "NASDAQ", "USD", f"{real} Corp", True, True)

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        return {n: self._sym(n) for n in names}

    @staticmethod
    def _sessions(start: date, end: date) -> list[date]:
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return [d for d in days if CAL.is_session(d)]

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        out: dict[CandleRequest, list[Candle] | QuestradeApiError] = {}
        for r in reqs:
            if r.interval == "OneDay":
                # end is exclusive (00:00 ET of the day after the last session)
                days = self._sessions(et_date(r.start), et_date(r.end) - timedelta(days=1))
                limit = self.daily_limit.get(r.symbol_id)
                if limit is not None:
                    days = days[-limit:] if limit else []
                out[r] = [
                    Candle(
                        datetime.combine(d, time(0), tzinfo=ET),
                        datetime.combine(d + timedelta(days=1), time(0), tzinfo=ET),
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
                days = self._sessions(et_date(r.start), et_date(r.end))
                out[r] = [
                    Candle(
                        CAL.session_open(d),
                        CAL.session_open(d) + timedelta(minutes=5),
                        Decimal("10"),
                        Decimal("11"),
                        Decimal("9"),
                        Decimal("10.5"),
                        1000,
                        None,
                    )
                    for d in days
                ]
        return out


def deps(
    factory: sessionmaker[Session],
    finviz: FakeFinviz,
    market: CalendarMarket | None = None,
    clock: FixedClock = CLOCK,
) -> NightlyDeps:
    return NightlyDeps(factory, clock, CAL, finviz, market or CalendarMarket(), RuntimeSettings())


def count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as s:
        return int(s.execute(select(func.count()).select_from(model)).scalar_one())


# --------------------------------------------------------------------------- Review Focus 5: runner


@pytest.mark.db
def test_concurrent_run_job_same_session_runs_once(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 5 / SPEC §9: two processes (cron + a manual `trader nightly`, or the cron backup)
    start the same (job, session_date) at once. Exactly one may run; the other must be skipped."""
    calls: list[int] = []
    barrier = threading.Barrier(2, timeout=10)
    outcomes: list[JobOutcome] = []
    lock = threading.Lock()

    def fn() -> dict[str, Any]:
        calls.append(1)
        time_mod.sleep(0.5)  # the job takes a while: the other caller arrives meanwhile
        return {"ok": True}

    def worker() -> None:
        barrier.wait()
        out = run_job(db_factory, CLOCK, "nightly", TARGET, fn)
        with lock:
            outcomes.append(out)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    with db_factory() as s:
        succeeded = s.execute(
            select(func.count()).select_from(JobRun).where(JobRun.status == "succeeded")
        ).scalar_one()
    assert len(calls) == 1, f"job body ran {len(calls)} times for one (job, session_date)"
    assert sorted(o.status for o in outcomes) == ["skipped", "succeeded"]
    assert succeeded == 1


@pytest.mark.db
def test_crash_halfway_then_rerun_completes_without_duplicates(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 5: (a) the nightly dies while writing (after symbols and candles were written in
    the transaction), (b) it dies after its data committed but before job_runs was marked, then
    (c) a clean re-run. The end state equals one clean run: no duplicates, one succeeded row."""
    d = deps(db_factory, FakeFinviz(["AAPL"]), CalendarMarket())

    def boom(*_: object, **__: object) -> int:
        raise RuntimeError("killed mid-write")

    with monkeypatch.context() as m:
        m.setattr(repo, "save_open_bar_stats", boom)
        first = run_job(db_factory, CLOCK, "nightly", TARGET, lambda: asyncio.run(run_nightly(d, TARGET)))
    assert first.status == "failed"
    assert [count(db_factory, m) for m in MODELS] == [0, 0, 0, 0, 0], "a failed run left partial rows"

    def commit_then_die() -> dict[str, Any]:
        asyncio.run(run_nightly(d, TARGET))
        raise RuntimeError("killed after commit")

    assert run_job(db_factory, CLOCK, "nightly", TARGET, commit_then_die).status == "failed"
    third = run_job(db_factory, CLOCK, "nightly", TARGET, lambda: asyncio.run(run_nightly(d, TARGET)))
    assert third.status == "succeeded"
    # AAPL + SPY: 2 symbols, 2 snapshot rows, 2 x 20 sessions of daily bars in the 30-day window,
    # 2 x 14 opening bars, 2 stats rows (the daily window is 27 Aug .. 25 Sep, Labor Day excluded)
    daily_sessions = len(CalendarMarket._sessions(date(2026, 8, 27), date(2026, 9, 25)))
    assert [count(db_factory, m) for m in MODELS] == [2, 2, 2 * daily_sessions, 28, 2]
    with db_factory() as s:
        statuses = s.execute(select(JobRun.status).order_by(JobRun.id)).scalars().all()
    assert statuses == ["failed", "failed", "succeeded"]


# ------------------------------------------------------------- Review Focus 2: weekends and holidays


@pytest.mark.db
async def test_friday_and_thanksgiving_week_targets_and_lookback(db_factory: sessionmaker[Session]) -> None:
    """Friday 20:00 ET prepares Monday. Wed 25 Nov 2026 (the day before Thanksgiving) prepares Fri 27
    Nov (an early close), and that night's 14-session opening-bar lookback must skip Thanksgiving."""
    friday = FixedClock(datetime(2026, 10, 3, 0, 0, tzinfo=UTC))  # Fri 2 Oct 20:00 ET
    wed_before_tg = FixedClock(datetime(2026, 11, 26, 1, 0, tzinfo=UTC))  # Wed 25 Nov 20:00 ET
    thanksgiving = FixedClock(datetime(2026, 11, 27, 1, 0, tzinfo=UTC))  # Thu 26 Nov 20:00 ET
    black_friday = FixedClock(datetime(2026, 11, 28, 1, 0, tzinfo=UTC))  # Fri 27 Nov 20:00 ET
    assert target_session(CAL, friday) == date(2026, 10, 5)
    assert target_session(CAL, wed_before_tg) == date(2026, 11, 27)
    assert target_session(CAL, thanksgiving) == date(2026, 11, 27)
    assert target_session(CAL, black_friday) == date(2026, 11, 30)

    # Friday night: Monday's stats come from sessions up to and including that Friday
    monday = target_session(CAL, friday)
    await run_nightly(deps(db_factory, FakeFinviz(["AAPL"]), clock=friday), monday)
    with db_factory() as s:
        last_daily = s.execute(select(func.max(DailyCandle.date))).scalar_one()
    assert last_daily == date(2026, 10, 2)

    # Thanksgiving week: target Black Friday
    bf = target_session(CAL, wed_before_tg)
    detail = await run_nightly(deps(db_factory, FakeFinviz(["AAPL"]), clock=wed_before_tg), bf)
    assert detail["universe"] == 2 and detail["candle_errors"] == 0
    with db_factory() as s:
        aapl = s.execute(select(Symbol.id).where(Symbol.ticker == "AAPL")).scalar_one()
        stat = s.execute(
            select(OpenBarStat).where(OpenBarStat.symbol_id == aapl, OpenBarStat.session_date == bf)
        ).scalar_one()
        bars = (
            s.execute(
                select(IntradayCandle.ts).where(
                    IntradayCandle.symbol_id == aapl,
                    IntradayCandle.ts >= datetime(2026, 11, 1, tzinfo=UTC),
                )
            )
            .scalars()
            .all()
        )
    assert stat.avg_open_vol_14d == Decimal("1000.00")
    assert stat.atr14 == Decimal("1.0000")
    assert date(2026, 11, 26) not in {et_date(t) for t in bars}
    expected = CAL.sessions_before(bf, 14)
    assert date(2026, 11, 26) not in expected
    assert {et_date(t) for t in bars} == {x for x in expected if x.month == 11}


# ----------------------------------------------------------------------------- symbol master edges


@pytest.mark.db
async def test_ticker_moves_to_new_questrade_id_and_old_holder_is_stale(
    db_factory: sessionmaker[Session],
) -> None:
    """Facebook (FB) became META in 2022, taking a ticker previously used by another security (the
    Roundhill metaverse ETF). The symbol master still holds (META, NASDAQ) for the old Questrade id
    and (FB, NASDAQ) for Meta's id. Tonight Questrade says META = Meta's id. The upsert keys on
    questrade_id only, so renaming FB -> META hits the (ticker, exchange) unique constraint and the
    whole nightly aborts: no universe for the next session."""
    with db_factory() as s:
        s.add_all(
            [
                Symbol(ticker="META", exchange="NASDAQ", questrade_id=1, currency="USD", name="Old META ETF"),
                Symbol(ticker="FB", exchange="NASDAQ", questrade_id=5000, currency="USD", name="Meta"),
            ]
        )
        s.commit()
    market = CalendarMarket(ids={"META": 5000})
    detail = await run_nightly(deps(db_factory, FakeFinviz(["META"]), market), TARGET)
    assert detail["universe"] == 2
    with db_factory() as s:
        qid = s.execute(
            select(Symbol.questrade_id)
            .join(UniverseSnapshot, UniverseSnapshot.symbol_id == Symbol.id)
            .where(Symbol.ticker == "META", UniverseSnapshot.session_date == TARGET)
        ).scalar_one()
    assert qid == 5000


@pytest.mark.db
async def test_two_finviz_tickers_resolving_to_one_questrade_symbol(
    db_factory: sessionmaker[Session],
) -> None:
    """Two universe names resolve to the same Questrade symbol (an alias or a share-class spelling
    the parser doesn't normalise). Expected: one symbol, one snapshot row, no crash. `upsert_symbols`
    keys its result by QtSymbol.symbol while run_nightly looks it up by the requested name, and the
    snapshot insert would touch the same (session_date, symbol_id) twice in one ON CONFLICT."""
    market = CalendarMarket(aliases={"GOOGL.OLD": "GOOGL"})
    detail = await run_nightly(deps(db_factory, FakeFinviz(["GOOGL", "GOOGL.OLD"]), market), TARGET)
    assert detail["universe"] == 2  # GOOGL once, plus SPY
    assert count(db_factory, Symbol) == 2
    assert count(db_factory, UniverseSnapshot) == 2
    assert count(db_factory, OpenBarStat) == 2


@pytest.mark.db
async def test_new_listing_with_ten_daily_bars_stores_null_atr(db_factory: sessionmaker[Session]) -> None:
    """A recent IPO has only 10 daily bars: ATR14 needs 15, so it is NULL in both open_bar_stats and
    universe_snapshots; the job carries on and other symbols are unaffected."""
    market = CalendarMarket(ids={"NEWCO": 1000, "AAPL": 1001, "SPY": 1002}, daily_limit={1000: 10})
    detail = await run_nightly(deps(db_factory, FakeFinviz(["NEWCO", "AAPL"]), market), TARGET)
    assert detail["universe"] == 3
    with db_factory() as s:
        rows = dict(
            s.execute(
                select(Symbol.ticker, OpenBarStat.atr14).join(OpenBarStat, OpenBarStat.symbol_id == Symbol.id)
            ).all()
        )
        snap = dict(
            s.execute(
                select(Symbol.ticker, UniverseSnapshot.atr14).join(
                    UniverseSnapshot, UniverseSnapshot.symbol_id == Symbol.id
                )
            ).all()
        )
        newco_daily = s.execute(
            select(func.count()).select_from(DailyCandle).join(Symbol).where(Symbol.ticker == "NEWCO")
        ).scalar_one()
    assert rows["NEWCO"] is None and snap["NEWCO"] is None
    assert rows["AAPL"] == Decimal("1.0000") and snap["AAPL"] == Decimal("1.0000")
    assert newco_daily == 10


# ------------------------------------------------------ Review Focus 3: fallback age, SPY in universe


@pytest.mark.db
async def test_spy_from_finviz_not_duplicated_and_month_old_fallback_is_flagged(
    db_factory: sessionmaker[Session],
) -> None:
    """FinViz returns SPY itself (it's also added as an extra): SPY appears once. A month later FinViz
    is blocked and the only stored universe is 31 days old: the fallback is used, reported with its
    real date (not passed off as last night's), and a warning-or-worse event records the age."""
    old = date(2026, 8, 28)
    market = CalendarMarket()  # Questrade ids are stable across nights
    d1 = await run_nightly(
        deps(
            db_factory,
            FakeFinviz(["SPY", "AAPL"]),
            market,
            clock=FixedClock(datetime(2026, 8, 28, 0, tzinfo=UTC)),
        ),
        old,
    )
    assert d1["universe"] == 2
    assert count(db_factory, Symbol) == 2 and count(db_factory, UniverseSnapshot) == 2

    d2 = await run_nightly(deps(db_factory, FakeFinviz(None, FinvizBlocked("HTTP 403")), market), TARGET)
    assert d2["source"] == "fallback"
    assert d2["fallback_from"] == "2026-08-28"
    assert d2["universe"] == 2
    with db_factory() as s:
        snap = s.execute(
            select(Symbol.ticker, UniverseSnapshot.source)
            .join(Symbol)
            .where(UniverseSnapshot.session_date == TARGET)
        ).all()
        events = s.execute(
            select(EventLog.level, EventLog.data).where(EventLog.message.like("%previous universe%"))
        ).all()
    assert sorted(snap) == [("AAPL", "fallback"), ("SPY", "fallback")]
    assert len(events) == 1
    level, data = events[0]
    assert level in {"warning", "error"}
    assert data["from"] == "2026-08-28"


# ---------------------------------------------------------------------------------- notify CLI


@pytest.mark.parametrize("mode", ["unset", "blank"])
def test_notify_without_telegram_config_exits_1_cleanly(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """No Telegram config (keys absent, or present but blank as in an env template) must exit 1 with a
    message, never a traceback and never an HTTP call."""
    import httpx

    from trader import config
    from trader.cli import app

    for key, value in {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:1/x",
        "MIGRATION_DATABASE_URL": "postgresql+psycopg://u:p@localhost:1/x",
        "APP_ENCRYPTION_KEY": "k" * 44,
        "SESSION_SECRET": "s" * 32,
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if mode == "unset":
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, "")

    def no_network(*_: object, **__: object) -> None:
        raise AssertionError("notify made an HTTP call without Telegram config")

    monkeypatch.setattr(httpx, "post", no_network)
    config.get_env.cache_clear()
    try:
        result = CliRunner().invoke(app, ["notify", "hello"])
    finally:
        config.get_env.cache_clear()
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
