from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run
from trader.broker.ledger import CashBalances, Ledger
from trader.db import models as m
from trader.market.calendar import SessionCalendar

LEDGER = Ledger(SessionCalendar())
TS = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("trade", "settle"),
    [
        (date(2026, 10, 6), date(2026, 10, 7)),  # Tue -> Wed
        (date(2026, 10, 2), date(2026, 10, 5)),  # Fri -> Mon
        (date(2026, 11, 25), date(2026, 11, 27)),  # Wed before Thanksgiving -> Fri (an early-close session)
        (date(2026, 12, 24), date(2026, 12, 28)),  # Christmas Eve (early close) -> Mon, Christmas is Fri
        (date(2026, 12, 31), date(2027, 1, 4)),  # New Year's Eve Thu -> Mon (Jan 1 is a holiday)
    ],
)
def test_settle_date_skips_weekend_and_holidays(trade: date, settle: date) -> None:
    assert LEDGER.settle_date(trade) == settle


def test_buying_power_follows_cash_account_mode() -> None:
    bal = CashBalances(total=Decimal("1009.99"), settled=Decimal("499.99"))
    assert bal.buying_power(cash_account_mode=True) == Decimal("499.99")
    assert bal.buying_power(cash_account_mode=False) == Decimal("1009.99")


def _record(s: Session, run: int, day: date, amount: str, kind: str) -> int:
    return LEDGER.record(
        s, run_id=run, ts=TS, trade_date=day, amount=Decimal(amount), kind=kind, ref=f"test:{kind}"
    )


@pytest.mark.db
def test_sale_proceeds_settle_next_session(db_factory: sessionmaker[Session]) -> None:
    thu, fri, sat, mon = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5)
    with db_factory() as s:
        run = add_run(s)
        _record(s, run, thu, "1000", "deposit")
        _record(s, run, fri, "-500", "buy")
        _record(s, run, fri, "510", "sell")
        _record(s, run, fri, "-0.01", "fee")
        s.commit()
        on_fri = LEDGER.balances(s, run, fri)
        on_sat = LEDGER.balances(s, run, sat)
        on_mon = LEDGER.balances(s, run, mon)
    assert on_fri == CashBalances(total=Decimal("1009.9900"), settled=Decimal("499.9900"))
    assert on_sat.settled == Decimal("499.9900")  # a weekend doesn't settle anything
    assert on_mon == CashBalances(total=Decimal("1009.9900"), settled=Decimal("1009.9900"))


@pytest.mark.db
def test_deposit_settles_on_its_trade_date(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        row_id = _record(s, run, date(2026, 10, 3), "720", "deposit")  # a Saturday
        s.commit()
        row = s.get(m.CashLedger, row_id)
        assert row is not None and row.settle_date == date(2026, 10, 3)
        assert LEDGER.balances(s, run, date(2026, 10, 3)).settled == Decimal("720.0000")


@pytest.mark.db
def test_balances_are_per_run(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        a, b = add_run(s), add_run(s, mode="replay")
        _record(s, a, date(2026, 10, 1), "100", "deposit")
        _record(s, b, date(2026, 10, 1), "999", "deposit")
        s.commit()
        assert LEDGER.balances(s, a, date(2026, 10, 1)).total == Decimal("100.0000")


@pytest.mark.parametrize(
    ("amount", "kind"), [("-1", "deposit"), ("-1", "sell"), ("1", "buy"), ("1", "fee"), ("0", "buy")]
)
@pytest.mark.db
def test_record_rejects_the_wrong_sign(db_factory: sessionmaker[Session], amount: str, kind: str) -> None:
    with db_factory() as s:
        run = add_run(s)
        with pytest.raises(ValueError, match="sign"):
            _record(s, run, date(2026, 10, 1), amount, kind)


@pytest.mark.db
def test_ledger_rows_are_append_only(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        _record(s, run, date(2026, 10, 1), "100", "deposit")
        s.commit()
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(update(m.CashLedger).values(amount=Decimal("1000000")))
        s.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(delete(m.CashLedger))
        s.rollback()
        assert not hasattr(LEDGER, "update") and not hasattr(LEDGER, "delete")
