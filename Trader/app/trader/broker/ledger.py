"""Cash ledger with T+1 settlement (SPEC §7.3, BR-21).

Rows are append-only: the database refuses UPDATE and DELETE (trigger cash_ledger_append_only, migration
0002) and TRUNCATE (trigger cash_ledger_no_truncate, migration 0003). Settled cash counts every debit at once but a credit only from its settle date, so money from a sale
can't be spent until it settles, and settled money can't be spent twice.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trader.db import models as m
from trader.market.calendar import SessionCalendar

LedgerKind = Literal["deposit", "buy", "sell", "fee"]
_POSITIVE: frozenset[str] = frozenset({"deposit", "sell"})


@dataclass(frozen=True, slots=True)
class CashBalances:
    total: Decimal
    settled: Decimal

    def buying_power(self, cash_account_mode: bool) -> Decimal:
        return self.settled if cash_account_mode else self.total


class Ledger:
    def __init__(self, calendar: SessionCalendar) -> None:
        self._cal = calendar

    def settle_date(self, trade_date: date) -> date:
        """T+1: the next trading session on the exchange calendar (skips weekends and holidays)."""
        return self._cal.next_session(trade_date)

    def record(
        self,
        session: Session,
        *,
        run_id: int,
        ts: datetime,
        trade_date: date,
        amount: Decimal,
        kind: LedgerKind,
        ref: str,
        currency: str = "USD",
    ) -> int:
        if (amount > 0) != (kind in _POSITIVE) or amount == 0:
            raise ValueError(
                f"wrong sign for a {kind} of {amount}: deposits and sells are > 0, buys and fees < 0"
            )
        settle = trade_date if kind == "deposit" else self.settle_date(trade_date)
        row = m.CashLedger(
            run_id=run_id,
            ts=ts,
            trade_date=trade_date,
            settle_date=settle,
            currency=currency,
            amount=amount,
            kind=kind,
            ref=ref,
        )
        session.add(row)
        session.flush()
        return row.id

    def balances(self, session: Session, run_id: int, today: date) -> CashBalances:
        amount = m.CashLedger.amount
        total, settled = session.execute(
            select(
                func.coalesce(func.sum(amount), 0),
                func.coalesce(
                    func.sum(amount).filter(or_(m.CashLedger.settle_date <= today, amount < 0)),
                    0,
                ),
            ).where(m.CashLedger.run_id == run_id)
        ).one()
        return CashBalances(total=Decimal(total), settled=Decimal(settled))
