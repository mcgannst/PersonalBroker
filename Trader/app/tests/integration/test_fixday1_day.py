"""FIX-DAY1 integration: Wednesday 2026-09-30 replayed through the real engine with the timed captures.

- CLDX (news, pre-market heavy): day volume 339,533 at 09:35 of which 303,905 traded pre-market; the official
  09:30-09:35 candle had 35,628. Wednesday's scan scaled the whole 339,533 (rvol ~3.6) and traded it. Now the
  open capture is subtracted: 35,628 x 0.7 = 24,940 against a 40,000 average -> rvol 0.62, not passed.
- NVTS (breakout): the bar's high at 09:35:00 was 12.30; by the 09:35:05 event the quote's day high was
  12.3899. The ORB reads the stored 09:35:00 capture, so the OR high (and the entry stop) is 12.30 (+0.01).
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.strategies.fakes import FakeCatalyst, FakeCatalysts
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.db import models as m
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
DAY = date(2026, 9, 30)
PREV = date(2026, 9, 29)
OPEN = CAL.session_open(DAY)
BAR_END = OPEN + timedelta(minutes=5)
QT = {"CLDX": 301, "NVTS": 302}
AVG = {"CLDX": Decimal("40000.00"), "NVTS": Decimal("100000.00")}


def qt(ticker: str, volume: int, last_trade: datetime, **kw: Any) -> QtQuote:
    values: dict[str, Any] = {
        "symbol_id": QT[ticker],
        "symbol": ticker,
        "bid": None,
        "ask": None,
        "last": None,
        "last_regular": None,
        "volume": volume,
        "last_trade_time": last_trade,
        "delay": 0,
        "is_halted": False,
        "vwap": None,
    }
    values.update(kw)
    return QtQuote(**values)


def d(x: str) -> Decimal:
    return Decimal(x)


async def test_wednesday_cldx_is_not_passed_and_nvts_enters_on_the_0935_00_high(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(OPEN - timedelta(seconds=2))
    store = SettingsStore(db_factory, now=clock.now)
    store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    run = get_live_run(db_factory, clock, settings)
    registry = StrategyRegistry(db_factory, clock)
    registry.ensure_defaults()
    registry.update("orb_sip", params={"max_positions": 10}, actor="test")
    fq = FakeQuestrade()  # no candles: the package delays them ~10 minutes
    ids: dict[str, int] = {}
    with db_factory() as s:
        for t, qid in QT.items():
            ids[t] = add_symbol(s, t, questrade_id=qid)
            fq.add_symbol(t, qid)
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=ids[t],
                    price=d("12"),
                    avg_volume=2_000_000,
                    atr14=d("1.0000"),
                    source="finviz",
                )
            )
            s.add(
                m.OpenBarStat(symbol_id=ids[t], session_date=DAY, avg_open_vol_14d=AVG[t], atr14=d("1.0000"))
            )
            s.add(
                m.QuoteVolumeScale(
                    session_date=PREV,
                    symbol_id=ids[t],
                    quote_volume=1_000_000,
                    candle_volume=700_000,
                    factor=d("0.700000"),
                    recorded_at=CAL.session_close(PREV),
                )
            )
        ids["SPY"] = add_symbol(s, "SPY", questrade_id=399, exchange="ARCA")
        s.commit()
    fq.add_symbol("SPY", 399)
    data = MarketDataService(db_factory, clock, CAL, fq, opening_bar_source="quotes")

    # 09:29:58: the volume at the open (pre-market only, no regular-session open yet)
    fq.quote_map[QT["CLDX"]] = qt("CLDX", 303_905, OPEN - timedelta(seconds=20))
    fq.quote_map[QT["NVTS"]] = qt("NVTS", 20_000, OPEN - timedelta(minutes=2))
    cap_open = await data.capture_quotes(DAY, "open")
    assert cap_open["quoted"] == 2 and cap_open["after_open"] == 0

    # 09:35:00.2: the bar capture
    clock.set(BAR_END + timedelta(milliseconds=200))
    fq.quote_map[QT["CLDX"]] = qt(
        "CLDX", 339_533, BAR_END - timedelta(milliseconds=300),
        open=d("9.00"), high=d("9.60"), low=d("8.95"), last=d("9.50"), last_regular=d("9.50"),
        bid=d("9.49"), ask=d("9.51"),
    )  # fmt: skip
    fq.quote_map[QT["NVTS"]] = qt(
        "NVTS", 420_000, BAR_END + timedelta(milliseconds=100),
        open=d("11.80"), high=d("12.30"), low=d("11.75"), last=d("12.20"), last_regular=d("12.20"),
        bid=d("12.19"), ask=d("12.21"),
    )  # fmt: skip
    cap_bar = await data.capture_quotes(DAY, "bar")
    assert cap_bar["late"] == 0 and cap_bar["offset_s"] == 0.2

    # 09:35:05: the breakout ran on; the live quote's day high is 12.3899 now
    clock.set(BAR_END + timedelta(seconds=5))
    fq.quote_map[QT["NVTS"]] = qt(
        "NVTS", 520_000, BAR_END + timedelta(seconds=4),
        open=d("11.80"), high=d("12.3899"), low=d("11.75"), last=d("12.38"), last_regular=d("12.38"),
        bid=d("12.37"), ask=d("12.39"),
    )  # fmt: skip
    calls_before = len(fq.calls)

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
        data=data,
        catalysts=FakeCatalysts({ids[t]: FakeCatalyst() for t in QT}),
        broker=broker,
        proposals=ProposalService(db_factory, clock, store, broker, run.id),
        risk=RiskManager(CAL),
        killswitches=KillSwitches(db_factory, clock),
        run_id=run.id,
    )
    res = await engine.run_event("orb_open", DAY)
    assert [c for c in fq.calls[calls_before:] if c[0] == "quotes"] == []  # the stored capture, no new read
    assert res.scan is not None
    assert res.scan["capture"]["symbols"] == 2 and res.scan["quote_lag_s"] == 0.2
    assert res.scan["volume_basis"] == {"delta": 2}

    with db_factory() as s:
        cands = {
            c.symbol_id: c
            for c in s.execute(select(m.Candidate).where(m.Candidate.run_id == run.id)).scalars()
        }
        bars = {r.symbol_id: r for r in s.execute(select(m.OpeningBarQuote)).scalars()}
    # CLDX: 35,628 traded in the bar (quote scale) x 0.7 = 24,940 -> rvol 0.62: not passed, not traded
    assert bars[ids["CLDX"]].volume == 24_940 and bars[ids["CLDX"]].open_volume == 303_905
    cldx = cands.get(ids["CLDX"])
    assert cldx is None or not cldx.passed
    # Wednesday's way (the raw day volume x 0.7 = 237,673, rvol 5.9) would have passed it
    assert Decimal(339_533) * d("0.7") / AVG["CLDX"] > 1
    # NVTS: the OR high from the 09:35:00 capture, not the 09:35:05 quote
    assert bars[ids["NVTS"]].high == d("12.3000")
    entries = broker.working_orders()
    assert [(o.symbol_id, o.stop) for o in entries] == [(ids["NVTS"], d("12.31"))]
