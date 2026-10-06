"""OPTSIM T13: the options morning refresh (`trader.jobs.options_refresh`), over the T1 fakes and a fixed
clock. The job runner needs `job_runs`, so every test uses the database."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_telegram import RecordingNotifier
from tests.options.factories import EXPIRY, add_options_run
from tests.options.fakes import FakeFacts, FakeOptionBroker, FakeOptionMarket, RecordingHost
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs.options_refresh import BAR_SESSIONS, REFRESH_JOB, SOURCE, RefreshDeps, refresh_job
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.types import Candle
from trader.options.messages import OptionMessages
from trader.options.settings import OptionSettings
from trader.options.types import SOURCE_MANUAL, LifecycleEvent, UnderlyingFacts

pytestmark = pytest.mark.db

CAL = SessionCalendar()
SESSION = date(2026, 10, 6)  # a Tuesday
NOW = datetime(2026, 10, 6, 12, 15, tzinfo=UTC)  # 08:15 ET
D = Decimal


class Market(FakeOptionMarket):
    """The fake market plus the two service methods the jobs use; records what was asked."""

    def __init__(self, clock: FixedClock) -> None:
        super().__init__(clock)
        self.chains: list[tuple[str, bool]] = []
        self.bar_calls: list[tuple[str, date, date]] = []
        self.broken: set[str] = set()

    async def refresh_chain(self, underlying: str, *, force: bool = False) -> int:
        if underlying in self.broken:
            raise RuntimeError("chain down")
        self.chains.append((underlying, force))
        return 1

    async def record_marks(self, contract_ids: Sequence[int]) -> int:
        return 0

    async def daily_bars(self, underlying: str, start: date, end: date) -> list[Candle]:
        if underlying in self.broken:
            raise RuntimeError("bars down")
        self.bar_calls.append((underlying, start, end))
        return []


class Adjustments:
    """A lifecycle engine that only answers the adjustment check."""

    def __init__(self) -> None:
        self.missing_closes: list[Any] = []
        self.frozen: list[LifecycleEvent] = []
        self.checks = 0

    async def run_expiry(self, session_date: date) -> list[LifecycleEvent]:
        return []

    async def run_early_assignment(self, session_date: date) -> list[LifecycleEvent]:
        return []

    async def check_adjustments(self) -> list[LifecycleEvent]:
        self.checks += 1
        return list(self.frozen)


class World:
    def __init__(self, factory: sessionmaker[Session], *, run: bool = True, **settings: Any) -> None:
        self.factory = factory
        self.clock = FixedClock(NOW)
        self.run_id: int | None = None
        if run:
            with session_scope(factory) as s:
                self.run_id = add_options_run(s)
        self.market = Market(self.clock)
        self.broker = FakeOptionBroker(self.market)
        self.host = RecordingHost()
        self.facts = FakeFacts()
        self.notifier = RecordingNotifier()
        self.engine = Adjustments()
        self.deps = RefreshDeps(
            factory=factory,
            clock=self.clock,
            calendar=CAL,
            settings=lambda: OptionSettings(**settings),
            run_id=lambda: self.run_id,
            broker=self.broker,
            market=self.market,
            facts=self.facts,
            host=self.host,
            notifier=self.notifier,
            renderer=OptionMessages("https://trader.example"),
            lifecycle=lambda run_id, settings, close: self.engine,
        )

    def known(self, *tickers: str) -> None:
        for ticker in tickers:
            self.facts.facts[ticker] = UnderlyingFacts(symbol_id=1, as_of=SESSION, ticker=ticker)

    def hold(self, ticker: str) -> int:
        return self.broker.book.add_structure(
            kind="shares",
            source=SOURCE_MANUAL,
            strategy_config_id=None,
            underlying=ticker,
            qty=1,
            entry_net=D(0),
            reserved_cash=D(0),
            cover_structure_id=None,
            parent_structure_id=None,
            take_profit_net=None,
            meta={},
            ts=NOW,
        )

    def job_rows(self) -> list[tuple[str, str]]:
        with self.factory() as s:
            return [(r.job, r.status) for r in s.execute(select(m.JobRun).order_by(m.JobRun.id)).scalars()]


async def test_refresh_union_of_underlyings(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory, watchlist=["AAPL"], benchmark_ticker="SOFI")
    w.host.watch = {"T", "F"}  # F is also held: once only
    w.hold("F")
    w.known("AAPL", "F", "SOFI", "T")

    outcome = await refresh_job(w.deps, SESSION, force=False)

    everyone = ("AAPL", "F", "SOFI", "T")
    assert outcome.status == "succeeded"
    assert outcome.detail == {"underlyings": 4, "ok": 4, "failed": {}, "frozen": 0}
    assert w.facts.refreshed == [everyone]
    assert w.market.chains == [(t, True) for t in everyone]  # a fresh chain, not the cached one
    start = CAL.sessions_before(SESSION, BAR_SESSIONS)[0]
    assert w.market.bar_calls == [(t, start, SESSION) for t in everyone]
    assert w.job_rows() == [(REFRESH_JOB, "succeeded")]
    assert (await refresh_job(w.deps, SESSION, force=False)).detail == {"reason": "already succeeded"}


async def test_refresh_counts_failures_and_fails_only_when_all_fail(
    db_factory: sessionmaker[Session],
) -> None:
    w = World(db_factory, watchlist=["AAPL", "F"], benchmark_ticker="SOFI")
    w.known("AAPL", "SOFI")  # F has no facts
    w.market.broken = {"SOFI"}  # no chain and no bars

    outcome = await refresh_job(w.deps, SESSION, force=False)

    assert outcome.status == "succeeded"
    assert outcome.detail["ok"] == 1
    assert outcome.detail["failed"] == {
        "F": {"facts": "no facts"},
        "SOFI": {"chain": "RuntimeError: chain down", "bars": "RuntimeError: bars down"},
    }
    with db_factory() as s:
        warning = s.execute(select(m.EventLog).where(m.EventLog.source == SOURCE)).scalar_one()
    assert (warning.level, warning.run_id) == ("warning", w.run_id)
    assert "2 of 3" in warning.message

    w.facts.facts.clear()  # now every ticker fails
    failed = await refresh_job(w.deps, SESSION, force=True)
    assert failed.status == "failed"
    assert failed.error == "RefreshFailed: every underlying failed: AAPL, F, SOFI"
    assert w.engine.checks == 2  # the adjustment check ran both times
    assert w.job_rows() == [(REFRESH_JOB, "succeeded"), (REFRESH_JOB, "failed")]


async def test_refresh_runs_adjustment_check(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory, benchmark_ticker="F")
    w.known("F")
    w.market.add_underlying("F", "15")
    put = w.market.add_contract("F", EXPIRY, "14.50", "put")
    frozen = LifecycleEvent(
        id=7,
        structure_id=3,
        source="toy_call",
        strategy_config_id=1,
        kind="frozen",
        session_date=SESSION,
        ts=NOW,
        contract=put,
        qty=1,
        strike=put.strike,
        underlying_close=None,
        shares_delta=0,
        cash_delta=D(0),
        new_structure_id=None,
    )
    w.engine.frozen = [frozen]

    outcome = await refresh_job(w.deps, SESSION, force=False)

    assert outcome.status == "succeeded"
    assert outcome.detail["frozen"] == 1
    assert w.engine.checks == 1
    assert w.host.lifecycle == [frozen]  # the owning plug-in is told
    (alert,) = w.notifier.sent  # and so is the owner
    assert alert.dedupe_key == "opt:life:7"
    assert "FROZEN" in alert.text


async def test_not_a_session_and_no_run_are_skips(db_factory: sessionmaker[Session]) -> None:
    w = World(db_factory)
    saturday = await refresh_job(w.deps, date(2026, 10, 10), force=True)
    assert (saturday.status, saturday.detail) == ("skipped", {"reason": "not a session"})

    no_run = World(db_factory, run=False)
    outcome = await refresh_job(no_run.deps, SESSION, force=False)
    assert (outcome.status, outcome.detail) == ("skipped", {"reason": "no options run"})

    assert w.job_rows() == []  # a skip records nothing, so the job can still run later
    assert w.facts.refreshed == [] and no_run.facts.refreshed == []
