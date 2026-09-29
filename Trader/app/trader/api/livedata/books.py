"""The books check (live dashboard plan S3): cash + positions at cost = starting cash + realised - fees, to
the cent.

    actual   = Σ cash_ledger.amount (run) + Σ round4(avg_price x qty) over open positions
    expected = sim_accounts.starting_cash
               + Σ over the run's trades of (exit_price - entry_price) x qty      (exact at 4 dp)
               - Σ over the run's fills of round4_half_up(fees.total)             (as the ledger stores it)
    ok       = |actual - expected| < 0.005

Realised gross comes from the trades' prices and the fees from the fills, rounded the way the ledger's `fee`
column rounds them, so the identity is exact on correct books after any number of trades (no rounding drift).
A fill without its ledger rows, a duplicated ledger row, a position whose cost disagrees with its entry
fill or a closed position without its `trades` row each break it. `realized_recorded` (Σ `trades.pnl`) is
information only. One read-only session; only the given run's rows are read.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.periods import ZERO, fee_total, q4
from trader.api.schemas import BooksCheckOut
from trader.db import models as m

TOLERANCE = Decimal("0.005")


def books_check(factory: sessionmaker[Session], run_id: int) -> BooksCheckOut:
    t = m.Trade
    with factory() as s:
        starting = s.execute(
            select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == run_id)
        ).scalar_one_or_none()
        cash = s.execute(
            select(func.coalesce(func.sum(m.CashLedger.amount), 0)).where(m.CashLedger.run_id == run_id)
        ).scalar_one()
        open_rows = s.execute(
            select(m.Position.avg_price, m.Position.qty).where(
                m.Position.run_id == run_id, m.Position.closed_at.is_(None)
            )
        ).all()
        gross, recorded = s.execute(
            select(
                func.coalesce(func.sum((t.exit_price - t.entry_price) * t.qty), 0),
                func.coalesce(func.sum(t.pnl), 0),
            ).where(t.run_id == run_id)
        ).one()
        fills = s.execute(select(m.Fill.fees).where(m.Fill.run_id == run_id)).scalars().all()
    cash_d = q4(Decimal(cash))
    at_cost = q4(sum((q4(avg * qty) for avg, qty in open_rows), ZERO))
    starting_d = q4(starting if starting is not None else ZERO)
    gross_d = q4(Decimal(gross))
    fees_paid = q4(sum((fee_total(f) for f in fills), ZERO))
    actual = cash_d + at_cost
    expected = starting_d + gross_d - fees_paid
    difference = actual - expected
    return BooksCheckOut(
        ok=abs(difference) < TOLERANCE,
        cash=cash_d,
        positions_at_cost=at_cost,
        actual=actual,
        starting_cash=starting_d,
        realized_gross=gross_d,
        fees_paid=fees_paid,
        expected=expected,
        difference=difference,
        realized_recorded=q4(Decimal(recorded)),
        open_positions=len(open_rows),
    )
