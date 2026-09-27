"""P2-B1 fix round (attempt 2): one regression test per gauntlet finding (Breaker, Spec and Code review)."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import CashBalances, Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import NoFill, OrderSpec
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # Tue 09:40 ET
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)  # 09:30 ET
CLOSE = CAL.session_close(DAY)  # 16:00 ET


@dataclass
class Env:
    broker: SimBroker
    clock: FixedClock
    run_id: int
    sym: int
    cfg: int
    factory: sessionmaker[Session]
    settings: RuntimeSettings


def make_env(
    factory: sessionmaker[Session],
    *,
    settings: RuntimeSettings | None = None,
    ledger: Ledger | None = None,
    fill_model: Any = None,
) -> Env:
    st = settings if settings is not None else RuntimeSettings()
    clock = FixedClock(T)
    run = get_live_run(factory, clock, st)  # deposit 720 USD
    with factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        s.commit()
    broker = SimBroker(
        factory,
        clock,
        ledger if ledger is not None else Ledger(CAL),
        fill_model if fill_model is not None else QuoteFillModel(FillParams()),
        run.id,
        calendar=CAL,
        settings=lambda: st,
    )
    return Env(broker, clock, run.id, sym, cfg, factory, st)


def broker_for(env: Env, **kw: Any) -> SimBroker:
    return SimBroker(
        env.factory,
        env.clock,
        kw.get("ledger", Ledger(CAL)),
        kw.get("fill_model", QuoteFillModel(FillParams())),
        env.run_id,
        calendar=CAL,
        settings=kw.get("settings", lambda: env.settings),
    )


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


def up(env: Env) -> QtQuote:  # triggers the 10.00 buy stop: fills at 10.03
    return quote(env, "10.01", "10.02", "10.02")


def down(env: Env) -> QtQuote:  # triggers the 9.90 sell stop
    return quote(env, "9.85", "9.86", "9.86")


def flat(env: Env) -> QtQuote:  # triggers neither stop; a market exit fills at 9.99
    return quote(env, "10.00", "10.01", "10.00")


def entry(env: Env, qty: int = 50, reason: str = "orb_breakout", broker: SimBroker | None = None) -> int:
    return (broker or env.broker).submit(
        OrderSpec(
            env.sym,
            "buy",
            "stop",
            qty,
            stop=Decimal("10.00"),
            stop_loss=Decimal("9.90"),
            strategy_config_id=env.cfg,
            reason=reason,
        )
    )


def fill_entry(env: Env, qty: int = 50) -> int:
    entry(env, qty)
    (ev,) = env.broker.on_quotes([up(env)], env.clock.now())
    return ev.position_id


def stop_for(env: Env, pid: int, qty: int = 50, reason: str = "protective_stop") -> int:
    return env.broker.submit(
        OrderSpec(
            env.sym, "sell", "stop", qty, stop=Decimal("9.90"), purpose="stop", position_id=pid, reason=reason
        )
    )


def market_exit(env: Env, pid: int, qty: int = 50, reason: str = "flatten_close") -> int:
    return env.broker.submit(
        OrderSpec(env.sym, "sell", "market", qty, purpose="exit", position_id=pid, reason=reason)
    )


def order_row(env: Env, oid: int) -> m.Order:
    with env.factory() as s:
        row = s.get(m.Order, oid)
    assert row is not None
    return row


def events(env: Env, contains: str) -> list[tuple[str, dict[str, Any]]]:
    with env.factory() as s:
        rows = s.execute(
            select(m.EventLog.level, m.EventLog.data)
            .where(m.EventLog.message.contains(contains))
            .order_by(m.EventLog.id)
        ).all()
    return [(level, data) for level, data in rows]


# 1. MUST: an exit reason as long as orders.reason allows is written to trades.exit_reason ----------------
@pytest.mark.db
def test_a_100_character_exit_reason_fills_and_is_kept(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory)
    pid = fill_entry(env)
    reason = "x" * 100
    market_exit(env, pid, reason=reason)
    (ev,) = env.broker.on_quotes([flat(env)], T)
    assert ev.trade_id is not None
    with env.factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
    assert trade.exit_reason == reason


def test_order_spec_caps_the_reason_at_100_characters() -> None:
    OrderSpec(1, "buy", "market", 1, reason="r" * 100)
    with pytest.raises(ValueError, match="100"):
        OrderSpec(1, "buy", "market", 1, reason="r" * 101)
    with pytest.raises(ValueError, match="100"):
        OrderSpec.from_json(
            {"symbol_id": 1, "side": "buy", "order_type": "market", "qty": 1, "reason": "r" * 101}
        )


# 2. One failing order doesn't abort the batch: each order runs in its own savepoint -----------------------
class FailFirstRecord(Ledger):
    """A ledger whose first `record` call raises after the fill row was flushed."""

    def __init__(self) -> None:
        super().__init__(CAL)
        self.failed = False

    def record(self, session: Session, **kw: Any) -> int:
        if not self.failed:
            self.failed = True
            raise RuntimeError("ledger down")
        return super().record(session, **kw)


@pytest.mark.db
def test_an_exception_on_one_order_is_isolated(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory, ledger=FailFirstRecord())
    first = entry(env, qty=5)
    second = entry(env, qty=6)
    fills = env.broker.on_quotes([up(env)], T)
    assert [f.order_id for f in fills] == [second]
    assert order_row(env, first).status == "working"  # rolled back to its savepoint, not half-filled
    assert order_row(env, second).status == "filled"
    with env.factory() as s:
        fill_orders = s.execute(select(m.Fill.order_id)).scalars().all()
        buys = s.execute(select(m.CashLedger.amount).where(m.CashLedger.kind == "buy")).scalars().all()
    assert fill_orders == [second] and buys == [Decimal("-60.1800")]
    ((level, data),) = events(env, "failed")
    assert level == "error" and data["order_id"] == first and data["error_type"] == "RuntimeError"
    (again,) = env.broker.on_quotes([up(env)], T)  # the failed order is retried on the next batch
    assert again.order_id == first


# 3. Regular hours only: no order fills outside [session_open, session_close) ------------------------------
@pytest.mark.db
def test_nothing_fills_outside_regular_hours(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory)
    pid = fill_entry(env)
    stop_id = stop_for(env, pid)
    for at in (CLOSE + timedelta(minutes=5), CLOSE):  # 16:05 ET, and the close second itself
        env.clock.set(at)
        assert env.broker.on_quotes([down(env)], at) == []
        assert order_row(env, stop_id).status == "working"  # stays working, not cancelled
    env.clock.set(CLOSE - timedelta(seconds=1))
    (ev,) = env.broker.on_quotes([down(env)], env.clock.now())
    assert ev.order_id == stop_id


@pytest.mark.db
def test_an_exit_waits_for_the_open(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory)
    pid = fill_entry(env)
    exit_id = market_exit(env, pid)
    env.clock.set(OPEN + timedelta(days=1) - timedelta(seconds=1))  # 09:29:59 ET the next day
    assert env.broker.on_quotes([flat(env)], env.clock.now()) == []
    assert order_row(env, exit_id).status == "working"
    env.clock.set(OPEN + timedelta(days=1))
    (ev,) = env.broker.on_quotes([flat(env)], env.clock.now())
    assert ev.order_id == exit_id


@pytest.mark.db
def test_a_pre_market_entry_stays_working_then_fills_at_the_open(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory)
    env.clock.set(OPEN - timedelta(minutes=30))  # 09:00 ET
    oid = entry(env)
    assert env.broker.on_quotes([up(env)], env.clock.now()) == []
    assert order_row(env, oid).status == "working"
    env.clock.set(OPEN)
    (ev,) = env.broker.on_quotes([up(env)], OPEN)
    assert ev.order_id == oid


# 4. Crossed quotes are unusable --------------------------------------------------------------------------
def test_the_fill_model_calls_a_crossed_quote_crossed() -> None:
    model = QuoteFillModel(FillParams())
    now = T
    q = QtQuote(
        symbol_id=1,
        symbol="AAA",
        bid=Decimal("10.50"),
        ask=Decimal("10.00"),
        last=Decimal("10.25"),
        last_regular=None,
        volume=1,
        last_trade_time=now - timedelta(seconds=1),
        delay=0,
        is_halted=False,
        vwap=None,
    )
    for spec in (
        OrderSpec(1, "buy", "market", 1),
        OrderSpec(1, "sell", "market", 1, purpose="exit", position_id=7),
    ):
        out = model.assess(spec, q, now)
        assert out == NoFill("crossed_quote", "bid 10.50 > ask 10.00")
    locked = replace(q, bid=Decimal("10.00"))  # bid == ask is locked, not crossed
    assert not isinstance(model.assess(OrderSpec(1, "buy", "market", 1), locked, now), NoFill)


@pytest.mark.db
def test_the_broker_treats_a_crossed_quote_as_unusable(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory)
    oid = entry(env)
    assert env.broker.on_quotes([quote(env, "10.50", "10.02", "10.02")], T) == []
    row = order_row(env, oid)
    assert row.status == "working" and row.stale_since == T
    ((level, data),) = events(env, "unusable quote")
    assert level == "warning" and data["reason"] == "crossed_quote"


# 5. account_state reads cash and positions in one snapshot ---------------------------------------------
class HookLedger(Ledger):
    """Runs `hook` once, right after the balances were read (a fill commits between the two reads)."""

    def __init__(self) -> None:
        super().__init__(CAL)
        self.hook: Callable[[], None] | None = None

    def balances(self, session: Session, run_id: int, today: date) -> CashBalances:
        out = super().balances(session, run_id, today)
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        return out


@pytest.mark.db
def test_account_state_never_double_counts_a_fill_that_lands_between_reads(
    db_factory: sessionmaker[Session],
) -> None:
    ledger = HookLedger()
    env = make_env(db_factory, ledger=ledger)
    filler = broker_for(env)  # another process's broker, with a plain ledger
    entry(env)
    ledger.hook = lambda: filler.on_quotes([up(env)], T)
    acct = env.broker.account_state(DAY, {env.sym: Decimal("10.03")}, cash_account_mode=True)
    assert acct.equity == Decimal("720.0000")  # before the fill: 720 cash, no position
    assert (acct.total_cash, acct.positions_value) == (Decimal("720.0000"), Decimal("0"))
    after = env.broker.account_state(DAY, {env.sym: Decimal("10.03")}, cash_account_mode=True)
    assert after.equity == Decimal("720.0000")  # after it: 218.50 cash + 501.50 position
    assert after.total_cash == Decimal("218.5000")


# 6. Lock order: positions before orders, so cancel and on_quotes can't deadlock ------------------------
@pytest.mark.db
def test_cancel_and_on_quotes_race_without_a_deadlock(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory, settings=RuntimeSettings(cash_account_mode=False))
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(25):
            pid = fill_entry(env, qty=5)
            stop_id = stop_for(env, pid, qty=5)
            exit_id = market_exit(env, pid, qty=5)
            racing_cancel = pool.submit(env.broker.cancel, stop_id, "manual")
            racing_quotes = pool.submit(env.broker.on_quotes, [flat(env)], T)
            for fut in (racing_cancel, racing_quotes):
                exc = fut.exception()
                if exc is not None:
                    errors.append(exc)
            assert order_row(env, exit_id).status in ("filled", "working")
            stop = order_row(env, stop_id)
            assert stop.status == "cancelled" and stop.cancel_reason in ("manual", "position closed")
            if order_row(env, exit_id).status == "working":  # skipped this batch; the next one fills it
                env.broker.on_quotes([flat(env)], T)
    assert errors == []
    assert env.broker.open_positions() == [] and env.broker.working_orders() == []


# 7. A sell whose position is no longer open (or no longer the same size) is cancelled, not filled ---------
@pytest.mark.db
@pytest.mark.parametrize("damage", ["closed", "qty"])
def test_a_sell_for_a_position_that_is_no_longer_open_is_cancelled(
    db_factory: sessionmaker[Session], damage: str
) -> None:
    env = make_env(db_factory)
    pid = fill_entry(env)
    stop_id = stop_for(env, pid)
    with env.factory() as s:  # simulate a position changed behind the stop's back
        pos = s.get(m.Position, pid)
        assert pos is not None
        if damage == "closed":
            pos.closed_at = T
        else:
            pos.qty = 40
        s.commit()
    assert env.broker.on_quotes([down(env)], T) == []
    row = order_row(env, stop_id)
    assert (row.status, row.cancel_reason) == ("cancelled", "position no longer open")
    ((level, data),) = events(env, "position no longer open")
    assert level == "warning" and data["order_id"] == stop_id and data["position_id"] == pid
    with env.factory() as s:
        assert s.execute(select(m.Trade)).first() is None
        assert s.execute(select(m.CashLedger).where(m.CashLedger.kind == "sell")).first() is None


# 8. Buying-power backstop at the entry fill -------------------------------------------------------------
@pytest.mark.db
def test_an_entry_that_costs_more_than_buying_power_is_cancelled(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory)
    oid = entry(env, qty=72)  # 72 x 10.03 = 722.16 > 720
    ok = entry(env, qty=71)  # 71 x 10.03 = 712.13, placed after it: still fills
    fills = env.broker.on_quotes([up(env)], T)
    assert [f.order_id for f in fills] == [ok]
    row = order_row(env, oid)
    assert (row.status, row.cancel_reason) == ("cancelled", "insufficient buying power")
    ((level, data),) = events(env, "insufficient buying power")
    assert level == "warning" and data["order_id"] == oid
    assert data["cost"] == "722.1600" and data["buying_power"] == "720.0000"


@pytest.mark.db
@pytest.mark.parametrize(("cash_mode", "fills"), [(True, False), (False, True)])
def test_buying_power_is_settled_cash_in_cash_account_mode(
    db_factory: sessionmaker[Session], cash_mode: bool, fills: bool
) -> None:
    env = make_env(db_factory, settings=RuntimeSettings(cash_account_mode=cash_mode))
    pid = fill_entry(env)  # 720 - 501.50 = 218.50 settled
    market_exit(env, pid)
    env.broker.on_quotes([flat(env)], T)  # + 499.49 (unsettled until tomorrow)
    oid = entry(env, qty=30)  # 30 x 10.03 = 300.90: more than settled cash, less than total
    got = env.broker.on_quotes([up(env)], T)
    assert [f.order_id for f in got] == ([oid] if fills else [])
    assert order_row(env, oid).status == ("filled" if fills else "cancelled")


# 9. Nits --------------------------------------------------------------------------------------------------
@pytest.mark.db
def test_stale_alerted_resets_when_a_usable_quote_returns(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory)
    oid = entry(env)
    stale = quote(env, "9.90", "9.91", "9.90", age=30)
    env.broker.on_quotes([stale], T)
    env.broker.on_quotes([stale], T + timedelta(seconds=61))
    assert order_row(env, oid).stale_alerted is True
    env.clock.set(T + timedelta(seconds=70))
    env.broker.on_quotes([quote(env, "9.90", "9.91", "9.90")], env.clock.now())  # usable, not triggered
    row = order_row(env, oid)
    assert row.stale_since is None and row.stale_alerted is False
    env.clock.set(T + timedelta(seconds=80))
    later = quote(env, "9.90", "9.91", "9.90", age=30)
    env.broker.on_quotes([later], env.clock.now())
    env.broker.on_quotes([later], env.clock.now() + timedelta(seconds=61))
    levels = [level for level, _ in events(env, "quote")]
    assert levels == ["warning", "error", "warning", "error"]  # a second outage alerts again


def test_order_spec_refuses_a_buy_that_names_a_position() -> None:
    with pytest.raises(ValueError, match="position"):
        OrderSpec(1, "buy", "market", 1, position_id=3)


@pytest.mark.db
def test_marking_a_position_at_cost_logs_an_event(db_factory: sessionmaker[Session]) -> None:
    env = make_env(db_factory)
    pid = fill_entry(env)
    env.broker.account_state(DAY, {env.sym: Decimal("10.50")}, cash_account_mode=True)
    assert events(env, "no mark") == []
    acct = env.broker.account_state(DAY, {}, cash_account_mode=True)
    assert acct.positions_value == Decimal("501.5000")
    ((level, data),) = events(env, "no mark")
    assert level == "warning" and data["positions"] == [{"position_id": pid, "symbol_id": env.sym}]


def test_the_fill_model_never_returns_a_price_at_or_below_zero() -> None:
    model = QuoteFillModel(FillParams())
    q = QtQuote(
        symbol_id=1,
        symbol="AAA",
        bid=Decimal("0.01"),
        ask=Decimal("0.02"),
        last=Decimal("0.01"),
        last_regular=None,
        volume=1,
        last_trade_time=T - timedelta(seconds=1),
        delay=0,
        is_halted=False,
        vwap=None,
    )
    out = model.assess(OrderSpec(1, "sell", "market", 1, purpose="exit", position_id=7), q, T)
    assert isinstance(out, NoFill) and out.reason == "no_bid"
