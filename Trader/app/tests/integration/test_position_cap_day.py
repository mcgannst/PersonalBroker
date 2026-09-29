"""SIZECAP integration: a simulated 9:35 scan through the real engine (orb_sip, risk, proposals, SimBroker)
with orb_sip at max_positions 10 and the default 10% per-stock cap on a $720 account. Six $20 names enter,
each costing at most 10% of equity; a $100 name can't buy one share under the $72 cap, so risk rejects it as
`position_cap`, and the decision log records that rejection like any other risk rejection."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.strategies.fakes import FakeCatalyst, FakeCatalysts
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
from trader.market.types import Candle
from trader.settings_store import SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)
T_ORB = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET
CHEAP = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
PRICEY = "ZZZ"


def five(o: str, h: str, low: str, c: str, v: int) -> Candle:
    return Candle(
        OPEN, OPEN + timedelta(minutes=5), Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None
    )


async def test_many_positions_each_within_the_cap_and_a_cap_rejection_logged(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(T_ORB)
    store = SettingsStore(db_factory, now=clock.now)
    store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    assert settings.max_position_pct == Decimal("0.10")  # the default, not set here
    run = get_live_run(db_factory, clock, settings)
    registry = StrategyRegistry(db_factory, clock)
    registry.ensure_defaults()
    # the deploy's config revision (max_positions 10); price_max lets the $100 name reach risk
    registry.update("orb_sip", params={"max_positions": 10, "price_max": "200"}, actor="test")

    fq = FakeQuestrade()
    ids: dict[str, int] = {}
    with db_factory() as s:
        for i, t in enumerate((*CHEAP, PRICEY)):
            ids[t] = add_symbol(s, t, questrade_id=101 + i)
            price = Decimal("100") if t == PRICEY else Decimal("20")
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=ids[t],
                    price=price,
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
        ids["SPY"] = add_symbol(s, "SPY", questrade_id=199, exchange="ARCA")
        s.commit()
    for i, t in enumerate(CHEAP):
        fq.add_symbol(t, 101 + i)
        fq.add_bars(101 + i, "FiveMinutes", [five("20.00", "20.40", "19.95", "20.30", 3000 + 100 * i)])
    fq.add_symbol(PRICEY, 101 + len(CHEAP))
    fq.add_bars(101 + len(CHEAP), "FiveMinutes", [five("100.00", "100.40", "99.95", "100.30", 9000)])
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
        data=MarketDataService(db_factory, clock, CAL, fq),
        catalysts=FakeCatalysts({ids[t]: FakeCatalyst() for t in (*CHEAP, PRICEY)}),
        broker=broker,
        proposals=ProposalService(db_factory, clock, store, broker, run.id),
        risk=RiskManager(CAL),
        killswitches=KillSwitches(db_factory, clock),
        run_id=run.id,
    )

    res = await engine.run_event("orb_open", DAY)
    by_status = sorted(o.status for o in res.outcomes)
    assert by_status == ["rejected_by_risk"] + ["submitted"] * len(CHEAP)
    (rejected,) = [o for o in res.outcomes if o.status == "rejected_by_risk"]
    assert rejected.rejection is not None and rejected.rejection.check == "position_cap"
    assert rejected.rejection.reason == "1 share of ZZZ costs $100.91, over the 10% cap $72.00"

    equity = Decimal("720")
    cap = equity * Decimal("0.10")
    entries = broker.working_orders()
    assert len(entries) == len(CHEAP) and 1 < len(entries) <= 10
    for o in entries:
        assert o.stop is not None and o.qty == 3  # 72 / (20.41 x 1.005) = 3.51 -> 3 (risk 144, cash 35)
        assert o.qty * o.stop * (1 + settings.slippage_buffer) <= cap

    clock.set(T_ORB + timedelta(seconds=55))
    for i in range(len(CHEAP)):
        fq.set_quote(101 + i, "20.42", "20.45", "20.43", clock.now())
    fills = await engine.poll_quotes()
    assert len(fills) == len(CHEAP)
    positions = broker.open_positions()
    assert len(positions) == len(CHEAP)
    for p in positions:
        assert p.qty * p.avg_price <= cap  # each position cost at most 10% of the equity at entry

    with db_factory() as s:
        sigs = s.execute(select(m.Signal).where(m.Signal.symbol_id == ids[PRICEY])).scalars().all()
    assert [x.evidence["rejection"]["check"] for x in sigs] == ["position_cap"]

    deps = RecorderDeps(db_factory, clock, CAL, store.load, LiveScanData(db_factory, CAL))
    await record_day(deps, run.id, DAY)
    with db_factory() as s:
        risk_rows = (
            s.execute(
                select(m.DecisionLog).where(
                    m.DecisionLog.run_id == run.id,
                    m.DecisionLog.stage == "risk",
                    m.DecisionLog.outcome == "rejected",
                )
            )
            .scalars()
            .all()
        )
        proposals = (
            s.execute(
                select(m.DecisionLog).where(m.DecisionLog.run_id == run.id, m.DecisionLog.stage == "proposal")
            )
            .scalars()
            .all()
        )
    assert [(r.ticker, r.rule) for r in risk_rows] == [(PRICEY, "position_cap")]
    assert risk_rows[0].reason == "1 share of ZZZ costs $100.91, over the 10% cap $72.00"
    entry_rows = [r for r in proposals if r.data.get("sizing", {}).get("limited_by") == "cap"]
    assert len(entry_rows) == len(CHEAP)
    assert {r.data["sizing"]["max_position_pct"] for r in entry_rows} == {"0.10"}
