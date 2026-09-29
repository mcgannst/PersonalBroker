"""DB-T3 acceptance tests 10-14 (testcontainer): the books check (live dashboard plan S3).

cash + positions at cost = starting cash + realised gross - fees paid, exactly, on books the real `SimBroker`
wrote; each corruption (a missing trade row, a duplicated ledger row, a wrong position cost) flips it to ✗
with the difference shown; the check only reads.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.adapters.questrade.models import QtQuote
from trader.api.livedata.books import books_check
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import Fees, OrderSpec
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # Tue 09:40 ET
Q4 = Decimal("0.0001")
# Odd fee fractions: a commission with 5 dp, ECN per share, and the SEC fee rate to 7 dp on every sale.
PARAMS = FillParams(
    commission=Decimal("0.01337"),
    direct_route=True,
    ecn_per_share=Decimal("0.0035"),
    sec_fee_rate=Decimal("0.0000206"),
)


@dataclass
class Books:
    factory: sessionmaker[Session]
    broker: SimBroker
    clock: FixedClock
    run_id: int
    starting_cash: Decimal
    closed: list[int]  # position ids
    open: list[int]


def _quote(sym_qt: int, bid: Decimal, ask: Decimal, clock: FixedClock) -> QtQuote:
    return QtQuote(
        symbol_id=sym_qt,
        symbol="AAA",
        bid=bid,
        ask=ask,
        last=ask,
        last_regular=None,
        volume=1000,
        last_trade_time=clock.now() - timedelta(seconds=1),
        delay=0,
        is_halted=False,
        vwap=None,
    )


def _build(factory: sessionmaker[Session], trips: int, still_open: int) -> Books:
    clock = FixedClock(T)
    settings = RuntimeSettings().model_copy(update={"cash_account_mode": False})
    run = get_live_run(factory, clock, settings)
    with factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        starting = s.execute(
            select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == run.id)
        ).scalar_one()
        s.commit()
    broker = SimBroker(factory, clock, Ledger(CAL), QuoteFillModel(PARAMS), run.id, settings=lambda: settings)
    closed: list[int] = []
    opened: list[int] = []
    for i in range(trips + still_open):
        price = Decimal("3.0000") + Decimal("0.0731") * (i % 17)
        qty = 1 + i % 3
        broker.submit(
            OrderSpec(
                sym,
                "buy",
                "stop",
                qty,
                stop=price,
                stop_loss=price - Decimal("0.25"),
                strategy_config_id=cfg,
                reason="orb_breakout",
            )
        )
        clock.advance(timedelta(seconds=1))
        (ev,) = broker.on_quotes([_quote(sym, price, price + Decimal("0.0100"), clock)], clock.now())
        if i >= trips:
            opened.append(ev.position_id)
            continue
        broker.submit(
            OrderSpec(
                sym, "sell", "market", qty, purpose="exit", position_id=ev.position_id, reason="flatten_close"
            )
        )
        clock.advance(timedelta(seconds=1))
        exit_bid = price + Decimal("0.0137") * ((i % 7) - 3)
        (out,) = broker.on_quotes([_quote(sym, exit_bid, exit_bid + Decimal("0.02"), clock)], clock.now())
        assert out.trade_id is not None
        closed.append(ev.position_id)
    return Books(factory, broker, clock, run.id, starting, closed, opened)


def _sum_fees(factory: sessionmaker[Session], run_id: int) -> Decimal:
    with factory() as s:
        rows = s.execute(select(m.Fill.fees).where(m.Fill.run_id == run_id)).scalars().all()
    return sum((Fees.from_json(f).total.quantize(Q4, ROUND_HALF_UP) for f in rows), Decimal(0))


# --- 10. ✓ after many trades --------------------------------------------------------------------------------


def test_books_balance_exactly_after_150_round_trips(db_factory: sessionmaker[Session]) -> None:
    books = _build(db_factory, 150, 0)
    out = books_check(db_factory, books.run_id)
    assert out.ok is True
    assert out.difference == Decimal("0.0000")
    assert out.actual == out.expected
    assert out.open_positions == 0 and out.positions_at_cost == 0
    assert out.starting_cash == books.starting_cash
    assert out.fees_paid == _sum_fees(db_factory, books.run_id) and out.fees_paid > 0
    with db_factory() as s:
        cash = s.execute(
            select(func.sum(m.CashLedger.amount)).where(m.CashLedger.run_id == books.run_id)
        ).scalar_one()
        gross = s.execute(
            select(func.sum((m.Trade.exit_price - m.Trade.entry_price) * m.Trade.qty)).where(
                m.Trade.run_id == books.run_id
            )
        ).scalar_one()
        recorded = s.execute(select(func.sum(m.Trade.pnl)).where(m.Trade.run_id == books.run_id)).scalar_one()
    assert out.cash == cash and out.realized_gross == gross and out.realized_recorded == recorded
    for v in (out.cash, out.actual, out.expected, out.difference, out.fees_paid, out.realized_gross):
        assert v.as_tuple().exponent == -4


# --- 11. ✗ on each corruption -------------------------------------------------------------------------------


def test_a_missing_trade_row_flips_to_cross_by_its_gross(db_factory: sessionmaker[Session]) -> None:
    books = _build(db_factory, 5, 1)
    assert books_check(db_factory, books.run_id).ok is True
    with db_factory() as s:
        trade = s.execute(select(m.Trade).where(m.Trade.position_id == books.closed[2])).scalar_one()
        gross = (trade.exit_price - trade.entry_price) * trade.qty
        s.delete(trade)
        s.commit()
    assert gross != 0
    out = books_check(db_factory, books.run_id)
    assert out.ok is False and out.difference == gross


def test_a_duplicated_fee_row_flips_to_cross_by_that_fee(db_factory: sessionmaker[Session]) -> None:
    books = _build(db_factory, 5, 1)
    with db_factory() as s:
        row = s.execute(
            select(m.CashLedger)
            .where(m.CashLedger.run_id == books.run_id, m.CashLedger.kind == "fee")
            .limit(1)
        ).scalar_one()
        s.add(
            m.CashLedger(
                run_id=row.run_id,
                ts=row.ts,
                trade_date=row.trade_date,
                settle_date=row.settle_date,
                currency=row.currency,
                amount=row.amount,
                kind="fee",
                ref=row.ref + ":dup",
            )
        )
        s.commit()
        fee = row.amount
    out = books_check(db_factory, books.run_id)
    assert out.ok is False and out.difference == fee  # the ledger has one more (negative) fee row


def test_a_wrong_position_cost_flips_to_cross(db_factory: sessionmaker[Session]) -> None:
    books = _build(db_factory, 5, 1)
    with db_factory() as s:
        pos = s.get(m.Position, books.open[0])
        assert pos is not None
        pos.avg_price = pos.avg_price + Decimal("0.0100")
        qty = pos.qty
        s.commit()
    out = books_check(db_factory, books.run_id)
    assert out.ok is False and out.difference == Decimal("0.0100") * qty


def test_a_cent_is_enough_to_flip_but_rounding_dust_is_not(db_factory: sessionmaker[Session]) -> None:
    books = _build(db_factory, 2, 0)
    with db_factory() as s:
        row = s.execute(select(m.CashLedger).where(m.CashLedger.run_id == books.run_id).limit(1)).scalar_one()
        s.add(
            m.CashLedger(
                run_id=row.run_id,
                ts=row.ts,
                trade_date=row.trade_date,
                settle_date=row.settle_date,
                currency=row.currency,
                amount=Decimal("0.0049"),
                kind="fee",
                ref="dust",
            )
        )
        s.commit()
    assert books_check(db_factory, books.run_id).ok is True
    with db_factory() as s:
        s.add(
            m.CashLedger(
                run_id=row.run_id,
                ts=row.ts,
                trade_date=row.trade_date,
                settle_date=row.settle_date,
                currency=row.currency,
                amount=Decimal("0.0001"),
                kind="fee",
                ref="dust2",
            )
        )
        s.commit()
    assert books_check(db_factory, books.run_id).ok is False


# --- 12. ✓ with open positions ------------------------------------------------------------------------------


def test_books_balance_with_two_open_positions(db_factory: sessionmaker[Session]) -> None:
    books = _build(db_factory, 3, 2)
    out = books_check(db_factory, books.run_id)
    assert out.ok is True and out.difference == 0
    assert out.open_positions == 2
    with db_factory() as s:
        rows = s.execute(
            select(m.Position.avg_price, m.Position.qty).where(m.Position.id.in_(books.open))
        ).all()
        recorded = s.execute(select(func.sum(m.Trade.pnl)).where(m.Trade.run_id == books.run_id)).scalar_one()
    assert out.positions_at_cost == sum(((a * q).quantize(Q4, ROUND_HALF_UP) for a, q in rows), Decimal(0))
    assert out.realized_recorded == recorded
    assert out.actual == out.cash + out.positions_at_cost
    assert out.expected == out.starting_cash + out.realized_gross - out.fees_paid


# --- 13. a brand-new run ------------------------------------------------------------------------------------


def test_a_new_run_with_only_its_deposit_balances(db_factory: sessionmaker[Session]) -> None:
    run = get_live_run(db_factory, FixedClock(T), RuntimeSettings())
    out = books_check(db_factory, run.id)
    assert out.ok is True
    assert out.cash == out.starting_cash == out.actual == out.expected
    assert out.cash > 0
    zero = (out.positions_at_cost, out.realized_gross, out.fees_paid, out.difference, out.realized_recorded)
    assert zero == (0, 0, 0, 0, 0) and out.open_positions == 0


def test_other_runs_never_enter_the_check(db_factory: sessionmaker[Session]) -> None:
    books = _build(db_factory, 2, 1)
    with db_factory() as s:
        replay = m.Run(mode="replay", started_at=T, params={}, status="completed", label="r")
        s.add(replay)
        s.flush()
        s.add(
            m.CashLedger(
                run_id=replay.id,
                ts=T,
                trade_date=T.date(),
                settle_date=T.date(),
                currency="USD",
                amount=Decimal("123.4500"),
                kind="deposit",
                ref="deposit",
            )
        )
        s.commit()
    assert books_check(db_factory, books.run_id).ok is True


# --- 14. read only ------------------------------------------------------------------------------------------


def test_books_check_only_selects(db_factory: sessionmaker[Session]) -> None:
    books = _build(db_factory, 2, 1)
    statements: list[str] = []
    engine = db_factory.kw["bind"]

    def record(*args: Any) -> None:
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", record)
    try:
        books_check(db_factory, books.run_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements
    assert all(st.lstrip().upper().startswith("SELECT") for st in statements), statements
