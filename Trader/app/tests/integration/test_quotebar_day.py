"""QUOTEBAR integration: the live 9:35 scan through the real engine (orb_sip at max_positions 10, the default
10% per-stock cap on a $720 account) with opening bars built from live quotes. No candle is served at 09:35:05
(the package delays them ~10 minutes), yet six names enter under the cap. The candidates and the decision log
say the bars came from quotes, the volumes are on candle scale (yesterday's measured factor), and nothing
quote-built lands in the candle tables."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.strategies.fakes import FakeCatalyst, FakeCatalysts
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.db import models as m
from trader.decisions.recorder import LiveScanData, record_day
from trader.decisions.types import RecorderDeps
from trader.engine.killswitch import KillSwitches
from trader.engine.orchestrator import Engine
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.settings_store import SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
PREV = date(2026, 10, 5)
OPEN = CAL.session_open(DAY)
T_ORB = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET
CHEAP = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
QUIET = "QQQQ"  # traded, but its candle-scale volume is under the 14-day average: rvol < 1


def open_quote(qid: int, ticker: str, volume: int) -> QtQuote:
    return QtQuote(
        symbol_id=qid,
        symbol=ticker,
        bid=Decimal("20.29"),
        ask=Decimal("20.31"),
        last=Decimal("20.30"),
        last_regular=Decimal("20.30"),
        volume=volume,
        last_trade_time=T_ORB - timedelta(seconds=1),
        delay=0,
        is_halted=False,
        vwap=None,
        open=Decimal("20.00"),
        high=Decimal("20.40"),
        low=Decimal("19.95"),
    )


async def test_the_935_scan_enters_on_quote_bars_under_the_cap(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(T_ORB)
    store = SettingsStore(db_factory, now=clock.now)
    store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    run = get_live_run(db_factory, clock, settings)
    registry = StrategyRegistry(db_factory, clock)
    registry.ensure_defaults()
    registry.update("orb_sip", params={"max_positions": 10}, actor="test")

    fq = FakeQuestrade()  # serves no candles at all: the delayed package has none yet
    ids: dict[str, int] = {}
    with db_factory() as s:
        for i, t in enumerate((*CHEAP, QUIET)):
            ids[t] = add_symbol(s, t, questrade_id=101 + i)
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=ids[t],
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1.0000"),
                    source="finviz",
                )
            )
            s.add(
                m.OpenBarStat(
                    symbol_id=ids[t],
                    session_date=DAY,
                    avg_open_vol_14d=Decimal("1000.00"),
                    atr14=Decimal("1.0000"),
                )
            )
            # yesterday's measured candle/quote factor: 0.70 for every name
            s.add(
                m.QuoteVolumeScale(
                    session_date=PREV,
                    symbol_id=ids[t],
                    quote_volume=1_000_000,
                    candle_volume=700_000,
                    factor=Decimal("0.700000"),
                    recorded_at=CAL.session_close(PREV) + timedelta(minutes=15),
                )
            )
        ids["SPY"] = add_symbol(s, "SPY", questrade_id=199, exchange="ARCA")
        s.commit()
    for i, t in enumerate(CHEAP):
        fq.add_symbol(t, 101 + i)
        fq.quote_map[101 + i] = open_quote(101 + i, t, 4_000 + 100 * i)  # x 0.70 = 2,800.. -> rvol 2.8..
    fq.add_symbol(QUIET, 101 + len(CHEAP))
    fq.quote_map[101 + len(CHEAP)] = open_quote(101 + len(CHEAP), QUIET, 1_000)  # 700 -> rvol 0.7
    fq.add_symbol("SPY", 199)

    broker = SimBroker(
        db_factory,
        clock,
        Ledger(CAL),
        QuoteFillModel(FillParams.from_settings(settings)),
        run.id,
        calendar=CAL,
        settings=store.load,
    )
    engine = Engine(
        factory=db_factory,
        clock=clock,
        calendar=CAL,
        settings=store,
        registry=registry,
        data=MarketDataService(db_factory, clock, CAL, fq, opening_bar_source="quotes"),
        catalysts=FakeCatalysts({ids[t]: FakeCatalyst() for t in (*CHEAP, QUIET)}),
        broker=broker,
        proposals=ProposalService(db_factory, clock, store, broker, run.id),
        risk=RiskManager(CAL),
        killswitches=KillSwitches(db_factory, clock),
        run_id=run.id,
    )

    res = await engine.run_event("orb_open", DAY)
    assert sorted(o.status for o in res.outcomes) == ["submitted"] * len(CHEAP)
    assert res.scan is not None and res.scan["source"] == "quotes" and res.scan["bars"] == 7
    assert res.scan["volume_factors"] == {"symbol": 7}
    assert not any(c[0].startswith("candles") for c in fq.calls)  # no candle request at 09:35

    equity, cap = Decimal("720"), Decimal("72")
    entries = broker.working_orders()
    assert len(entries) == len(CHEAP) <= 10
    for o in entries:
        assert o.stop == Decimal("20.41")  # the quote's session high + 0.01
        assert o.qty * o.stop * (1 + settings.slippage_buffer) <= cap

    with db_factory() as s:
        cands = {
            c.symbol_id: c
            for c in s.execute(select(m.Candidate).where(m.Candidate.run_id == run.id)).scalars()
        }
        assert s.execute(select(func.count()).select_from(m.IntradayCandle)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(m.CandleArchive)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(m.OpeningBarQuote)).scalar_one() == 7
    a = cands[ids["AAA"]]
    assert a.data["bar_source"] == "quotes"
    assert a.candle["volume"] == 2_800 and a.rvol == Decimal("2.8000")

    clock.set(T_ORB + timedelta(seconds=55))
    for i in range(len(CHEAP)):
        fq.set_quote(101 + i, "20.42", "20.45", "20.43", clock.now())
    fills = await engine.poll_quotes()
    assert len(fills) == len(CHEAP)
    for p in broker.open_positions():
        assert p.qty * p.avg_price <= equity * Decimal("0.10")

    deps = RecorderDeps(db_factory, clock, CAL, store.load, LiveScanData(db_factory, CAL))
    await record_day(deps, run.id, DAY)
    with db_factory() as s:
        scan_rows = (
            s.execute(
                select(m.DecisionLog).where(m.DecisionLog.run_id == run.id, m.DecisionLog.stage == "scan")
            )
            .scalars()
            .all()
        )
    by_ticker = {r.ticker: r for r in scan_rows if r.ticker}
    assert by_ticker["AAA"].data["bar_source"] == "quotes"
    # the non-candidate is explained with the quote-built bar the scan used, not "no stored opening bar"
    quiet = by_ticker[QUIET]
    assert quiet.rule == "rvol_below_min" and quiet.data["candle"]["volume"] == 700
