"""Migration 0003 (P2-B1 fix round): exit_reason length, CHECK constraints, no TRUNCATE on cash_ledger."""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0, add_run, add_symbol
from trader.db import models as m

pytestmark = pytest.mark.db


def test_trades_exit_reason_holds_what_orders_reason_holds(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)

    def length(table: str, column: str) -> int:
        (col,) = [c for c in insp.get_columns(table, schema="trader") if c["name"] == column]
        return int(col["type"].length)

    assert length("trades", "exit_reason") == length("orders", "reason") == 100


def test_check_constraints_exist(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    names = {
        c["name"]
        for t in ("orders", "fills", "positions")
        for c in insp.get_check_constraints(t, schema="trader")
    }
    assert {
        "ck_orders_qty_positive",
        "ck_fills_qty_positive",
        "ck_fills_price_positive",
        "ck_positions_qty_nonnegative",
    } <= names


def _order(run_id: int, sym: int, qty: int) -> m.Order:
    return m.Order(
        run_id=run_id,
        symbol_id=sym,
        side="buy",
        order_type="market",
        purpose="entry",
        qty=qty,
        tif="day",
        status="working",
        reason="",
        session_date=date(2026, 10, 6),
        submitted_at=T0,
        stale_alerted=False,
    )


def _fill(run_id: int, order_id: int, qty: int, price: str) -> m.Fill:
    return m.Fill(
        run_id=run_id,
        order_id=order_id,
        ts=T0,
        qty=qty,
        price=Decimal(price),
        fees={},
        quote_snapshot={},
        slippage=Decimal("0"),
    )


def _position(run_id: int, sym: int, qty: int) -> m.Position:
    return m.Position(
        run_id=run_id,
        symbol_id=sym,
        qty=qty,
        avg_price=Decimal("10"),
        session_date=date(2026, 10, 6),
        opened_at=T0,
        entry_order_id=1,
        unprotected_seconds=0,
    )


BAD: dict[str, Callable[[int, int, int], object]] = {
    "order qty 0": lambda run, sym, oid: _order(run, sym, 0),
    "fill qty 0": lambda run, sym, oid: _fill(run, oid, 0, "10"),
    "fill price 0": lambda run, sym, oid: _fill(run, oid, 1, "0"),
    "position qty -1": lambda run, sym, oid: _position(run, sym, -1),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_check_constraints_refuse_bad_rows(db_factory: sessionmaker[Session], case: str) -> None:
    with db_factory() as s:
        run = add_run(s)
        sym = add_symbol(s)
        good = _order(run, sym, 1)
        s.add(good)
        s.add(_position(run, sym, 0))  # a zero-size position is allowed (>= 0)
        s.commit()
        s.add(BAD[case](run, sym, good.id))
        with pytest.raises(IntegrityError, match="ck_"):
            s.commit()


def test_cash_ledger_refuses_truncate(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(text("TRUNCATE trader.cash_ledger"))
