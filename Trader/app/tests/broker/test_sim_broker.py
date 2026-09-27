from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.adapters.questrade.models import QtQuote
from trader.broker.base import BrokerRejected
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # Tue 09:40 ET
DAY = date(2026, 10, 6)


@dataclass
class Env:
    broker: SimBroker
    clock: FixedClock
    run_id: int
    sym: int
    cfg: int
    factory: sessionmaker[Session]


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T)
    run = get_live_run(db_factory, clock, RuntimeSettings())  # deposit 720 USD
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        s.commit()
    broker = SimBroker(db_factory, clock, Ledger(CAL), QuoteFillModel(FillParams()), run.id)
    return Env(broker, clock, run.id, sym, cfg, db_factory)


def quote(env: Env, bid: str, ask: str, last: str, age: float = 1.0) -> QtQuote:
    return QtQuote(
        symbol_id=env.sym,
        symbol="AAA",
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(last),
        last_regular=None,
        volume=1000,
        last_trade_time=env.clock.now() - timedelta(seconds=age),
        delay=0,
        is_halted=False,
        vwap=None,
    )


def entry(env: Env, qty: int = 50) -> int:
    spec = OrderSpec(
        env.sym,
        "buy",
        "stop",
        qty,
        stop=Decimal("10.00"),
        stop_loss=Decimal("9.90"),
        strategy_config_id=env.cfg,
        reason="orb_breakout",
    )
    return env.broker.submit(spec)


def fill_entry(env: Env) -> int:
    entry(env)
    (ev,) = env.broker.on_quotes([quote(env, "10.01", "10.02", "10.02")], env.clock.now())
    return ev.position_id


def stop_for(env: Env, position_id: int, qty: int = 50) -> int:
    return env.broker.submit(
        OrderSpec(
            env.sym,
            "sell",
            "stop",
            qty,
            stop=Decimal("9.90"),
            purpose="stop",
            position_id=position_id,
            reason="protective_stop",
        )
    )


def market_exit(env: Env, position_id: int, qty: int = 50) -> int:
    return env.broker.submit(
        OrderSpec(
            env.sym, "sell", "market", qty, purpose="exit", position_id=position_id, reason="flatten_close"
        )
    )


def test_submit_works_then_fills_and_opens_a_position(env: Env) -> None:
    oid = entry(env)
    assert [o.id for o in env.broker.working_orders()] == [oid]
    assert env.broker.working_symbol_ids() == [env.sym]
    assert env.broker.on_quotes([quote(env, "9.93", "9.95", "9.94")], T) == []
    (ev,) = env.broker.on_quotes([quote(env, "10.01", "10.02", "10.02")], T)
    assert (ev.order_id, ev.purpose, ev.qty, ev.price) == (oid, "entry", 50, Decimal("10.0300"))
    assert ev.stop_loss == Decimal("9.90") and ev.strategy_config_id == env.cfg and ev.trade_id is None
    (pos,) = env.broker.open_positions()
    assert (pos.id, pos.qty, pos.avg_price, pos.stop_loss) == (
        ev.position_id,
        50,
        Decimal("10.0300"),
        Decimal("9.90"),
    )
    assert pos.unprotected_since == T and pos.stop_order_id is None
    with env.factory() as s:
        fill = s.execute(select(m.Fill)).scalar_one()
        planned = s.get(m.Position, ev.position_id)
        ledger = s.execute(select(m.CashLedger.kind, m.CashLedger.amount).order_by(m.CashLedger.id)).all()
    assert fill.quote_snapshot["ask"] == "10.02" and fill.slippage == Decimal("0.0100")
    assert planned is not None and planned.planned_risk == Decimal("6.5000")  # (10.03 - 9.90) x 50
    assert ledger == [("deposit", Decimal("720.0000")), ("buy", Decimal("-501.5000"))]
    assert env.broker.working_orders() == []


def test_round_trip_writes_a_trade_with_pnl_and_r(env: Env) -> None:
    pid = fill_entry(env)
    stop_for(env, pid)
    (ev,) = env.broker.on_quotes([quote(env, "9.85", "9.86", "9.86")], T)
    # sell stop: min(9.90, 9.85) - 0.01 = 9.84; SEC fee 50 x 9.84 = 492 x 0.0000206 = 0.0101
    assert ev.price == Decimal("9.8400") and ev.purpose == "stop" and ev.trade_id is not None
    with env.factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
        pos = s.get(m.Position, pid)
    assert (trade.entry_price, trade.exit_price, trade.qty) == (Decimal("10.0300"), Decimal("9.8400"), 50)
    assert trade.pnl == Decimal("-9.5101")  # (9.84 - 10.03) x 50 - 0.0101
    assert trade.pnl_r == Decimal("-1.4631")  # -9.5101 / 6.5
    assert trade.fees_total == Decimal("0.0101")
    assert trade.slippage_total == Decimal("1.0000")  # (0.01 + 0.01) x 50
    assert trade.exit_reason == "protective_stop" and trade.session_date == DAY
    assert pos is not None and pos.closed_at == T
    assert ev.pnl == trade.pnl
    assert env.broker.open_positions() == []


def test_cancel_only_affects_working_orders(env: Env) -> None:
    oid = entry(env)
    assert env.broker.cancel(oid, "entry_cancel_at") is True
    assert env.broker.cancel(oid, "again") is False
    assert env.broker.cancel(999_999, "unknown") is False
    with env.factory() as s:
        order = s.get(m.Order, oid)
    assert order is not None and order.status == "cancelled" and order.cancel_reason == "entry_cancel_at"
    pid = fill_entry(env)
    with env.factory() as s:
        filled = s.execute(select(m.Order.id).where(m.Order.position_id == pid)).scalar_one()
    assert env.broker.cancel(filled, "too late") is False


def test_sells_must_match_the_position_long_only(env: Env) -> None:
    pid = fill_entry(env)
    with pytest.raises(BrokerRejected, match="long only"):
        market_exit(env, pid, qty=51)
    with pytest.raises(BrokerRejected, match="partial"):
        market_exit(env, pid, qty=49)
    with pytest.raises(BrokerRejected, match="not open"):
        market_exit(env, 999_999)
    market_exit(env, pid)
    env.broker.on_quotes([quote(env, "10.10", "10.11", "10.10")], T)
    with pytest.raises(BrokerRejected, match="not open"):
        market_exit(env, pid)


def test_end_of_session_cancels_every_working_order(env: Env) -> None:
    pid = fill_entry(env)
    stop_id = stop_for(env, pid)
    other = entry(env, qty=5)
    assert sorted(env.broker.end_of_session(DAY)) == sorted([stop_id, other])
    assert env.broker.working_orders() == []
    with env.factory() as s:
        reasons = set(s.execute(select(m.Order.cancel_reason).where(m.Order.status == "cancelled")).scalars())
    assert reasons == {"end_of_session"}


def test_stale_quote_keeps_order_working_and_logs_once(env: Env) -> None:
    """Review Focus 1: a stale quote never fills; one warning, then one error if it persists 60 s."""
    oid = entry(env)
    stale = quote(env, "10.50", "10.51", "10.50", age=30)
    assert env.broker.on_quotes([stale], T) == []
    assert env.broker.on_quotes([stale], T + timedelta(seconds=2)) == []
    with env.factory() as s:
        order = s.get(m.Order, oid)
        levels = (
            s.execute(select(m.EventLog.level).where(m.EventLog.message.contains("quote"))).scalars().all()
        )
    assert order is not None and order.status == "working" and order.stale_since == T
    assert levels == ["warning"]
    env.broker.on_quotes([stale], T + timedelta(seconds=61))
    env.broker.on_quotes([stale], T + timedelta(seconds=63))
    with env.factory() as s:
        levels = (
            s.execute(select(m.EventLog.level).where(m.EventLog.message.contains("quote"))).scalars().all()
        )
        order = s.get(m.Order, oid)
    assert levels == ["warning", "error"] and order is not None and order.stale_alerted is True
    env.clock.set(T + timedelta(seconds=70))
    assert env.broker.on_quotes([quote(env, "9.90", "9.91", "9.90")], env.clock.now()) == []
    with env.factory() as s:
        order = s.get(m.Order, oid)
    assert order is not None and order.stale_since is None and order.status == "working"


def test_an_entry_that_would_fill_late_is_cancelled_instead(env: Env) -> None:
    """Review Focus 4 (BR-42): from the no-entry cutoff on, an entry is cancelled instead of filled."""
    cutoff = CAL.session_close(DAY) - timedelta(minutes=30)  # 15:30 ET with the default 30 minutes
    assert (
        env.broker.entry_cutoff(DAY) == cutoff and env.broker.entry_cutoff(date(2026, 10, 4)) is None
    )  # Sunday
    env.clock.set(cutoff - timedelta(seconds=1))
    pid = fill_entry(env)  # one second before the cutoff an entry still fills
    late = entry(env, qty=5)
    env.clock.set(cutoff)
    market_exit(env, pid)  # exits are never refused
    fills = env.broker.on_quotes(
        [quote(env, "10.01", "10.02", "10.02")], cutoff
    )  # would trigger the buy stop
    assert [f.purpose for f in fills] == ["exit"]
    with env.factory() as s:
        order = s.get(m.Order, late)
        levels = (
            s.execute(select(m.EventLog.level).where(m.EventLog.message.contains("entry cutoff")))
            .scalars()
            .all()
        )
    assert order is not None and order.status == "cancelled" and order.cancel_reason == "entry cutoff"
    assert levels == ["warning"] and env.broker.open_positions() == [] and env.broker.working_orders() == []


def test_the_entry_cutoff_follows_the_settings(env: Env) -> None:
    settings = RuntimeSettings(no_entry_before_close_minutes=60)
    broker = SimBroker(
        env.factory,
        env.clock,
        Ledger(CAL),
        QuoteFillModel(FillParams()),
        env.run_id,
        calendar=CAL,
        settings=lambda: settings,
    )
    assert broker.entry_cutoff(DAY) == CAL.session_close(DAY) - timedelta(minutes=60)


def test_order_fills_only_once(env: Env) -> None:
    """Review Focus 2: repeated or concurrent quote batches fill an order once."""
    entry(env)
    q = quote(env, "10.01", "10.02", "10.02")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: env.broker.on_quotes([q], T), range(4)))
    assert sum(len(r) for r in results) == 1
    assert env.broker.on_quotes([q], T) == []
    with env.factory() as s:
        assert s.execute(select(func.count()).select_from(m.Fill)).scalar_one() == 1


def test_market_exit_fill_cancels_the_protective_stop(env: Env) -> None:
    pid = fill_entry(env)
    stop_id = stop_for(env, pid)
    market_exit(env, pid)
    (ev,) = env.broker.on_quotes([quote(env, "10.20", "10.21", "10.20")], T)
    assert ev.purpose == "exit" and ev.price == Decimal("10.1900")
    with env.factory() as s:
        stop = s.get(m.Order, stop_id)
    assert stop is not None and stop.status == "cancelled" and stop.cancel_reason == "position closed"


def test_unprotected_seconds_are_recorded(env: Env) -> None:
    pid = fill_entry(env)  # at T, no stop yet
    env.clock.set(T + timedelta(seconds=30))
    stop_id = stop_for(env, pid)
    (pos,) = env.broker.open_positions()
    assert pos.unprotected_seconds == 30 and pos.unprotected_since is None and pos.stop_order_id == stop_id
    env.clock.set(T + timedelta(seconds=40))
    env.broker.cancel(stop_id, "manual")
    (pos,) = env.broker.open_positions()
    assert pos.unprotected_since == T + timedelta(seconds=40) and pos.stop_order_id is None
    env.clock.set(T + timedelta(seconds=100))
    market_exit(env, pid)
    env.broker.on_quotes([quote(env, "10.20", "10.21", "10.20")], env.clock.now())
    with env.factory() as s:
        closed = s.get(m.Position, pid)
    assert closed is not None and closed.unprotected_seconds == 90  # 30 before the stop + 60 after it went


def test_a_new_stop_replaces_the_old_one(env: Env) -> None:
    pid = fill_entry(env)
    first = stop_for(env, pid)
    second = stop_for(env, pid)
    assert [o.id for o in env.broker.working_orders()] == [second]
    with env.factory() as s:
        old = s.get(m.Order, first)
    assert old is not None and old.cancel_reason == "replaced by a new stop"


def test_account_state_and_equity_snapshots(env: Env) -> None:
    fill_entry(env)  # 50 @ 10.03, cash 720 - 501.50
    acct = env.broker.account_state(DAY, {env.sym: Decimal("10.50")}, cash_account_mode=True)
    assert acct.total_cash == Decimal("218.5000") and acct.settled_cash == Decimal("218.5000")
    assert acct.positions_value == Decimal("525.0000") and acct.equity == Decimal("743.5000")
    assert acct.buying_power == Decimal("218.5000")
    no_mark = env.broker.account_state(DAY, {}, cash_account_mode=False)
    assert no_mark.positions_value == Decimal("501.5000")
    env.broker.snapshot_equity(T, acct)
    lower = env.broker.account_state(DAY, {env.sym: Decimal("9.00")}, cash_account_mode=True)
    env.broker.snapshot_equity(T + timedelta(minutes=1), lower)
    env.broker.snapshot_equity(T + timedelta(minutes=1), lower)  # same ts twice: one row
    with env.factory() as s:
        snaps = s.execute(select(m.EquitySnapshot).order_by(m.EquitySnapshot.ts)).scalars().all()
    assert [(x.equity, x.peak_equity, x.drawdown_pct) for x in snaps] == [
        (Decimal("743.5000"), Decimal("743.5000"), Decimal("0.0000")),
        (Decimal("668.5000"), Decimal("743.5000"), Decimal("0.1009")),  # 75 / 743.5
    ]
