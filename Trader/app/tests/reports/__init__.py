"""Tests of `trader.reports` (P4-T7). `add_trade` is a small row builder shared with the P4-T7 API tests."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from trader.db import models as m


def add_trade(
    s: Session,
    run_id: int,
    symbol_id: int,
    session_date: date,
    pnl: str,
    pnl_r: str | None,
    *,
    config_id: int | None = None,
    exit_reason: str = "target",
    qty: int = 10,
    entry: str = "20.0000",
    slippage: str = "0.0200",
    fees: str = "0.0000",
    planned_risk: str | None = "10.0000",
) -> int:
    """A closed position and its trade (opened 10:00 ET, closed 11:00 ET on `session_date`); returns the
    trade id. The caller commits."""
    opened = datetime.combine(session_date, time(14, 0), tzinfo=UTC)
    pos = m.Position(
        run_id=run_id,
        symbol_id=symbol_id,
        strategy_config_id=config_id,
        qty=0,
        avg_price=Decimal(entry),
        stop_loss=None,
        planned_risk=Decimal(planned_risk) if planned_risk is not None else None,
        session_date=session_date,
        opened_at=opened,
        closed_at=opened + timedelta(hours=1),
        entry_order_id=1,
        stop_order_id=None,
        unprotected_since=None,
        unprotected_seconds=0,
    )
    s.add(pos)
    s.flush()
    exit_price = Decimal(entry) + Decimal(pnl) / qty
    trade = m.Trade(
        run_id=run_id,
        position_id=pos.id,
        symbol_id=symbol_id,
        session_date=session_date,
        entry_price=Decimal(entry),
        exit_price=exit_price,
        qty=qty,
        pnl=Decimal(pnl),
        pnl_r=Decimal(pnl_r) if pnl_r is not None else None,
        planned_risk=Decimal(planned_risk) if planned_risk is not None else None,
        exit_reason=exit_reason,
        slippage_total=Decimal(slippage),
        fees_total=Decimal(fees),
        opened_at=opened,
        closed_at=opened + timedelta(hours=1),
    )
    s.add(trade)
    s.flush()
    return trade.id
