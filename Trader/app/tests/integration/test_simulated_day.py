"""One full simulated day, nightly job to flatten, with a fake clock and fake data (SPEC §16, BR-42)."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_questrade import FakeQuestrade
from trader.adapters.claude.catalyst import CatalystClassifier, CatalystService, CatalystStore
from trader.adapters.finviz.parser import Headline, ScreenerPage, UniverseRow
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.orchestrator import Engine
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.engine.runs import get_live_run
from trader.jobs.nightly import NightlyDeps, run_nightly
from trader.jobs.premarket import PremarketDeps, run_premarket
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle
from trader.settings_store import SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
NEXT = date(2026, 10, 7)
QT = {"AAA": 101, "BBB": 102, "SPY": 199}


def et(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


def daily(d: date, low: str, high: str, close: str) -> Candle:
    start = datetime.combine(d, time(0), tzinfo=ET)
    return Candle(
        start,
        start + timedelta(days=1),
        Decimal(close),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        1_500_000,
        None,
    )


def opening(d: date, o: str, h: str, low: str, c: str, v: int) -> Candle:
    start = CAL.session_open(d)
    return Candle(
        start, start + timedelta(minutes=5), Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None
    )


class FakeFinviz:
    """Universe, screens and headlines."""

    def universe(self, filters: str) -> list[UniverseRow]:
        return [
            UniverseRow(t, f"{t} Inc", "Tech", "Software", Decimal("20"), 2_000_000) for t in ("AAA", "BBB")
        ]

    def screen(
        self, filters: str, view: int = 111, signal: str | None = None, *, columns: str | None = None
    ) -> ScreenerPage:
        tickers = ["AAA"] if "news_date_today" in filters else []
        return ScreenerPage(len(tickers), ["Ticker"], [{"Ticker": t} for t in tickers])

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        return [Headline(et(7, 0), f"{ticker} news", "Reuters", "https://example.com")]


class FakeClaude:
    """AAA: a strong bullish catalyst. Anything else: no catalyst."""

    def __init__(self) -> None:
        self.messages = self
        self.tickers: list[str] = []

    async def create(self, **kwargs: Any) -> Any:
        prompt = kwargs["messages"][0]["content"]
        ticker = prompt.splitlines()[0].removeprefix("Ticker: ")
        self.tickers.append(ticker)
        good = ticker == "AAA"
        payload = {
            "catalyst_type": "earnings_beat" if good else "none",
            "direction": "bullish" if good else "neutral",
            "quality": 82 if good else 10,
            "is_confirmed": good,
            "reason": "Beat and raise." if good else "No company news.",
        }
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=500, output_tokens=50),
            stop_reason="end_turn",
        )


@dataclass
class Day:
    clock: FixedClock
    fq: FakeQuestrade
    engine: Engine
    finviz: FakeFinviz
    claude: FakeClaude
    catalysts: CatalystService
    data: MarketDataService
    store: SettingsStore
    run_id: int
    factory: sessionmaker[Session]


def setup_day(factory: sessionmaker[Session]) -> Day:
    clock = FixedClock(et(20, 0, day=date(2026, 10, 5)))  # Monday evening: the nightly job
    store = SettingsStore(factory, now=clock.now)
    store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    fq = FakeQuestrade()
    for t, q in QT.items():
        fq.add_symbol(t, q)
    for d in CAL.sessions_before(DAY, 20):
        fq.add_bars(QT["AAA"], "OneDay", [daily(d, "19.50", "20.50", "20.00")])
        fq.add_bars(QT["BBB"], "OneDay", [daily(d, "19.50", "20.50", "20.00")])
        fq.add_bars(QT["SPY"], "OneDay", [daily(d, "499.50", "500.50", "500.00")])
    for d in CAL.sessions_before(DAY, 14):
        for t in ("AAA", "BBB"):
            fq.add_bars(QT[t], "FiveMinutes", [opening(d, "20.00", "20.10", "19.90", "20.05", 1000)])
    run = get_live_run(factory, clock, settings)
    finviz, claude = FakeFinviz(), FakeClaude()
    data = MarketDataService(factory, clock, CAL, fq)
    catalysts = CatalystService(
        factory, clock, CatalystStore(factory, clock), CatalystClassifier(claude, store.load), finviz
    )
    registry = StrategyRegistry(factory, clock)
    registry.ensure_defaults()
    broker = SimBroker(
        factory,
        clock,
        Ledger(CAL),
        QuoteFillModel(FillParams.from_settings(settings)),
        run.id,
        calendar=CAL,
        settings=store.load,
    )
    engine = Engine(
        factory=factory,
        clock=clock,
        calendar=CAL,
        settings=store,
        registry=registry,
        data=data,
        catalysts=catalysts,
        broker=broker,
        proposals=ProposalService(factory, clock, store, broker, run.id),
        risk=RiskManager(CAL),
        killswitches=KillSwitches(factory, clock),
        run_id=run.id,
    )
    return Day(clock, fq, engine, finviz, claude, catalysts, data, store, run.id, factory)


async def morning(d: Day) -> None:
    """Nightly, pre-market, the 9:35 scan and the entry fill."""
    detail = await run_nightly(NightlyDeps(d.factory, d.clock, CAL, d.finviz, d.fq, d.store.load()), DAY)
    assert detail["source"] == "finviz" and detail["universe"] == 3

    d.clock.set(et(8, 0))
    d.fq.set_quote(QT["AAA"], "20.99", "21.01", "21.00", d.clock.now())  # +5% gap
    d.fq.set_quote(QT["BBB"], "20.09", "20.11", "20.10", d.clock.now())
    brief = await run_premarket(
        PremarketDeps(d.factory, d.clock, d.finviz, d.data, d.catalysts, d.store.load()), DAY
    )
    assert brief["candidates"] == 1 and brief["classified"] == 1 and d.claude.tickers == ["AAA"]

    d.clock.set(et(9, 35, 5))
    d.fq.add_bars(QT["AAA"], "FiveMinutes", [opening(DAY, "21.00", "21.50", "20.90", "21.40", 5000)])
    d.fq.add_bars(QT["BBB"], "FiveMinutes", [opening(DAY, "20.00", "20.40", "19.95", "20.30", 3000)])
    res = await d.engine.run_event("orb_open", DAY)
    (out,) = res.outcomes
    assert out.status == "submitted" and d.claude.tickers == ["AAA", "BBB"]  # BBB classified at 9:35
    (entry,) = d.engine.broker.working_orders()
    assert (entry.stop, entry.qty) == (Decimal("21.5100"), 33)

    d.clock.set(et(9, 36))
    d.fq.set_quote(QT["AAA"], "21.52", "21.55", "21.53", d.clock.now())
    (fill,) = await d.engine.poll_quotes()
    assert fill.price == Decimal("21.5608")
    (stop,) = d.engine.broker.working_orders()
    assert (stop.purpose, stop.stop) == ("stop", Decimal("21.4100"))
    await d.engine.tick(d.clock.now())


async def test_full_day_ends_flat(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 4 and BR-42: signal, auto-approval, fill, stop, overlay hold, flatten, nothing held."""
    d = setup_day(db_factory)
    await morning(d)

    d.clock.set(et(15, 30))
    d.fq.set_quote(QT["SPY"], "501.99", "502.01", "502.00", d.clock.now())  # +0.4%: hold
    d.fq.set_quote(QT["AAA"], "21.70", "21.72", "21.71", d.clock.now())
    overlay = await d.engine.run_event("overlay_decision", DAY)
    assert overlay.strategies == ["spy_overlay"] and overlay.outcomes == []

    d.clock.set(et(15, 50))
    flatten = await d.engine.run_event("flatten", DAY)
    assert [o.status for o in flatten.outcomes] == ["submitted"]
    d.clock.set(et(15, 50, 2))
    d.fq.set_quote(QT["AAA"], "21.90", "21.92", "21.91", d.clock.now())
    (exit_fill,) = await d.engine.poll_quotes()
    assert exit_fill.price == Decimal("21.8890") and exit_fill.trade_id is not None

    d.clock.set(et(16, 0))
    assert await d.engine.end_of_session(DAY) == []
    assert d.engine.broker.open_positions() == [] and d.engine.broker.working_orders() == []

    with db_factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
        kinds = [
            (p.kind, p.status, p.decided_via)
            for p in s.execute(select(m.Proposal).order_by(m.Proposal.id)).scalars()
        ]
        stop = s.execute(select(m.Order).where(m.Order.purpose == "stop")).scalar_one()
        cands = {c.reject_reason for c in s.execute(select(m.Candidate)).scalars()}
        ledger = [
            (r.kind, r.amount) for r in s.execute(select(m.CashLedger).order_by(m.CashLedger.id)).scalars()
        ]
        decision = s.execute(
            select(m.EventLog).where(
                m.EventLog.source == "strategy.spy_overlay", m.EventLog.message == "overlay: decision"
            )
        ).scalar_one()
        metrics = (
            s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": d.run_id})
            .mappings()
            .one()
        )
        on_day = Ledger(CAL).balances(s, d.run_id, DAY)
        next_day = Ledger(CAL).balances(s, d.run_id, NEXT)
        fills = s.execute(select(m.Fill).order_by(m.Fill.id)).scalars().all()
    assert (trade.entry_price, trade.exit_price, trade.qty) == (Decimal("21.5608"), Decimal("21.8890"), 33)
    assert trade.pnl == Decimal("10.8157") and trade.pnl_r == Decimal("2.1734")
    assert trade.planned_risk == Decimal("4.9764") and trade.slippage_total == Decimal("0.7194")
    assert trade.fees_total == Decimal("0.0149") and trade.exit_reason == "flatten_close"
    assert kinds == [
        ("entry", "submitted", "auto"),
        ("stop", "submitted", "auto"),
        ("exit", "submitted", "auto"),
    ]
    assert stop.status == "cancelled" and stop.cancel_reason == "position closed"
    assert cands == {None, "catalyst_missing"}
    assert decision.data["decision"] == "hold" and decision.data["spy_return"] == "0.004000"
    assert ledger == [
        ("deposit", Decimal("720.0000")),
        ("buy", Decimal("-711.5064")),
        ("sell", Decimal("722.3370")),
        ("fee", Decimal("-0.0149")),
    ]
    assert on_day.total == Decimal("730.8157") and on_day.settled == Decimal("8.4787")
    assert next_day.settled == Decimal("730.8157")
    assert metrics["trades"] == 1 and metrics["win_rate"] == Decimal("1.0000")
    assert metrics["expectancy_r"] == Decimal("2.1734")
    assert fills[0].quote_snapshot["ask"] == "21.55" and fills[1].quote_snapshot["bid"] == "21.90"


async def test_stop_out_day_ends_flat(db_factory: sessionmaker[Session]) -> None:
    d = setup_day(db_factory)
    await morning(d)

    d.clock.set(et(10, 5))
    d.fq.set_quote(QT["AAA"], "21.38", "21.40", "21.39", d.clock.now())
    (stopped,) = await d.engine.poll_quotes()
    assert stopped.purpose == "stop" and stopped.price == Decimal("21.3693")  # min(21.41, 21.38) - 0.0107

    d.clock.set(et(15, 50))
    assert (await d.engine.run_event("flatten", DAY)).outcomes == []  # nothing left to flatten
    d.clock.set(et(16, 0))
    assert await d.engine.end_of_session(DAY) == []
    with db_factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
        positions = s.execute(select(m.Position)).scalars().all()
    assert trade.exit_reason == "protective_stop" and trade.pnl < 0
    assert all(p.closed_at is not None for p in positions)
