"""P5-T4: Engine.on_candles, the candle twin of on_quotes, with the same-bar worst-case pass (SPEC §7.4).

The real orb_sip plug-in places a buy stop at 21.51 (the 09:30 bar's high 21.50 + 0.01) with a stop loss of
21.41 for 33 shares, in auto mode. A 1-minute bar that reaches the entry and also trades down to the stop must
count as entered and then stopped out in that same bar.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.broker.test_sim_broker_candles import FakeCandleModel
from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_replay import candle
from tests.strategies.fakes import FakeCatalyst, FakeCatalysts
from trader.broker.fill_model import FillParams
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import FillEvent, FillModel
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.orchestrator import Engine
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle
from trader.replay.candle_fill_model import CandleFillModel
from trader.settings_store import SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)
T_ORB = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET
ENTRY, STOP = Decimal("21.51"), Decimal("21.41")


def et(h: int, mi: int) -> datetime:
    return datetime(2026, 10, 6, h + 4, mi, tzinfo=UTC)


def _real_model_ready() -> bool:
    try:
        CandleFillModel(FillParams(), Decimal("5")).slip(Decimal("10"))
    except NotImplementedError:
        return False
    return True


@dataclass
class World:
    engine: Engine
    clock: FixedClock
    fq: FakeQuestrade
    ids: dict[str, int]
    run_id: int
    broker: SimBroker
    factory: sessionmaker[Session]


def build(factory: sessionmaker[Session], model: FillModel) -> World:
    clock = FixedClock(T_ORB)
    store = SettingsStore(factory, now=clock.now)
    store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    run = get_live_run(factory, clock, settings)
    ids: dict[str, int] = {}
    with factory() as s:
        for i, t in enumerate(("AAA", "BBB")):
            ids[t] = add_symbol(s, t, questrade_id=101 + i)
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=ids[t],
                    price=Decimal("21"),
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
        s.add(
            m.DailyCandle(
                symbol_id=ids["SPY"],
                date=date(2026, 10, 5),
                open=Decimal("499"),
                high=Decimal("501"),
                low=Decimal("498"),
                close=Decimal("500.00"),
                volume=1,
                vwap=None,
            )
        )
        s.commit()
    fq = FakeQuestrade()
    for t, q in (("AAA", 101), ("BBB", 102), ("SPY", 199)):
        fq.add_symbol(t, q)
    fq.add_bars(101, "FiveMinutes", [candle(OPEN, "21.00", "21.50", "20.90", "21.40", 5000, minutes=5)])
    fq.add_bars(102, "FiveMinutes", [candle(OPEN, "30.00", "30.60", "29.90", "30.50", 3000, minutes=5)])
    registry = StrategyRegistry(factory, clock)
    registry.ensure_defaults()
    broker = SimBroker(factory, clock, Ledger(CAL), model, run.id, calendar=CAL, settings=store.load)
    engine = Engine(
        factory=factory,
        clock=clock,
        calendar=CAL,
        settings=store,
        registry=registry,
        data=MarketDataService(factory, clock, CAL, fq),
        catalysts=FakeCatalysts({ids["AAA"]: FakeCatalyst()}),
        broker=broker,
        proposals=ProposalService(factory, clock, store, broker, run.id, audit_auto=False),
        risk=RiskManager(CAL),
        killswitches=KillSwitches(factory, clock),
        run_id=run.id,
    )
    return World(engine, clock, fq, ids, run.id, broker, factory)


async def place_entry(w: World) -> None:
    await w.engine.run_event("orb_open", DAY)
    (order,) = w.broker.working_orders()
    assert (order.purpose, order.stop, order.qty) == ("entry", ENTRY, 33)


async def candles_at(w: World, bars: dict[int, Candle], now: datetime) -> list[FillEvent]:
    """As the replay runner: the clock is set to `now` before the call, and a mark exists for the account."""
    w.clock.set(now)
    for sid, bar in bars.items():
        qt = {w.ids["AAA"]: 101, w.ids["BBB"]: 102}[sid]
        w.fq.set_quote(qt, str(bar.close), str(bar.close), str(bar.close), now)
    return await w.engine.on_candles(bars, now)


# --- 3. same bar: entry, then the protective stop --------------------------------------------------


async def test_same_bar_touching_entry_and_stop_is_entered_then_stopped(
    db_factory: sessionmaker[Session],
) -> None:
    w = build(db_factory, FakeCandleModel())
    await place_entry(w)
    aaa = w.ids["AAA"]
    bar = candle(et(9, 35), "21.45", "21.60", "21.30", "21.40")  # high >= 21.51 and low <= 21.41
    fills = await candles_at(w, {aaa: bar}, et(9, 36))
    assert [(f.purpose, f.side, f.price, f.ts) for f in fills] == [
        ("entry", "buy", ENTRY, et(9, 36)),
        ("stop", "sell", STOP, et(9, 36)),  # reopened at the entry's price: min(21.41, 21.51) = the stop
    ]
    assert fills[1].position_id == fills[0].position_id and fills[1].pnl is not None
    with db_factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
        stop_order = s.execute(select(m.Order).where(m.Order.purpose == "stop")).scalar_one()
    assert trade.exit_reason == "protective_stop" and trade.pnl < 0
    assert stop_order.submitted_at == et(9, 36) and stop_order.status == "filled"
    assert w.broker.open_positions() == [] and w.broker.working_orders() == []


@pytest.mark.skipif(not _real_model_ready(), reason="CandleFillModel is P5-T3's (still a stub)")
async def test_same_bar_with_the_real_candle_model_stops_at_stop_minus_slip_and_half_spread(
    db_factory: sessionmaker[Session],
) -> None:
    model = CandleFillModel(FillParams(), Decimal("5"))
    w = build(db_factory, model)
    await place_entry(w)
    bar = candle(et(9, 35), "21.45", "21.60", "21.30", "21.40")
    entry_fill, stop_fill = await candles_at(w, {w.ids["AAA"]: bar}, et(9, 36))
    assert entry_fill.purpose == "entry" and stop_fill.purpose == "stop"
    assert stop_fill.price == STOP - model.slip(STOP) - model.half_spread(STOP)
    with db_factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
    assert trade.exit_reason == "protective_stop" and trade.pnl < 0


# --- 4. same bar that stays above the stop -------------------------------------------------------


async def test_bar_above_the_stop_fills_only_the_entry_and_the_stop_works_on(
    db_factory: sessionmaker[Session],
) -> None:
    w = build(db_factory, FakeCandleModel())
    await place_entry(w)
    aaa = w.ids["AAA"]
    fills = await candles_at(w, {aaa: candle(et(9, 35), "21.45", "21.60", "21.45", "21.55")}, et(9, 36))
    assert [f.purpose for f in fills] == ["entry"]
    (stop,) = w.broker.working_orders()
    assert (stop.purpose, stop.stop, stop.submitted_at) == ("stop", STOP, et(9, 36))
    # the next bar (ending after the stop was submitted) reaches the stop: the normal path fills it
    (later,) = await candles_at(w, {aaa: candle(et(9, 36), "21.50", "21.52", "21.35", "21.38")}, et(9, 37))
    assert (later.purpose, later.order_id, later.price, later.ts) == ("stop", stop.id, STOP, et(9, 37))


async def test_no_entry_fill_means_no_same_bar_pass_and_no_bar_for_the_symbol_does_nothing(
    db_factory: sessionmaker[Session],
) -> None:
    w = build(db_factory, FakeCandleModel())
    await place_entry(w)
    assert await candles_at(w, {w.ids["BBB"]: candle(et(9, 35), "30", "31", "29", "30")}, et(9, 36)) == []
    assert (
        await candles_at(w, {w.ids["AAA"]: candle(et(9, 36), "21.00", "21.20", "20.90", "21.10")}, et(9, 37))
        == []
    )
    (entry,) = w.broker.working_orders()
    assert entry.purpose == "entry"


async def test_on_candles_writes_no_audit_rows_with_audit_auto_off(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, FakeCandleModel())
    await place_entry(w)
    await candles_at(w, {w.ids["AAA"]: candle(et(9, 35), "21.45", "21.60", "21.30", "21.40")}, et(9, 36))
    with db_factory() as s:
        actions = list(
            s.execute(select(m.AuditLog.action).where(m.AuditLog.action.like("proposal.%"))).scalars()
        )
        proposals = list(s.execute(select(m.Proposal.decided_via).order_by(m.Proposal.id)).scalars())
    assert actions == [] and proposals == ["auto", "auto"]  # the entry and its protective stop
