from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.engine.runs import get_live_run, sim_account, starting_balance
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))  # Tue 08:00 ET


def test_starting_balance_same_currency_has_no_fx() -> None:
    bal = starting_balance(RuntimeSettings())
    assert bal.amount == Decimal("720.0000") and bal.fx_rate is None and bal.fx_fee is None


def test_starting_balance_cad_to_usd_applies_rate_and_fee() -> None:
    s = RuntimeSettings(
        starting_cash=Decimal("1000"), starting_cash_currency="CAD", fx_cad_usd_rate=Decimal("0.73")
    )
    bal = starting_balance(s)
    assert bal.amount == Decimal("719.0500")  # 1000 x 0.73 x (1 - 0.015)
    assert bal.fx_rate == Decimal("0.730000") and bal.fx_fee == Decimal("0.015")


def test_starting_balance_usd_to_cad_uses_the_inverse_rate() -> None:
    s = RuntimeSettings(
        starting_cash=Decimal("730"),
        account_currency="CAD",
        fx_cad_usd_rate=Decimal("0.73"),
        fx_fee_pct=Decimal("0"),
    )
    assert starting_balance(s).amount == Decimal("1000.0000")


@pytest.mark.db
def test_live_run_created_once_and_reused(db_factory: sessionmaker[Session]) -> None:
    first = get_live_run(db_factory, CLOCK, RuntimeSettings())
    second = get_live_run(db_factory, CLOCK, RuntimeSettings())
    assert first.id == second.id and first.mode == "live"
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Run)).scalar_one() == 1
        params = s.execute(select(m.Run.params)).scalar_one()
    assert params["settings"]["risk_pct"] == "0.02"


@pytest.mark.db
def test_concurrent_get_live_run_creates_one_run_and_one_deposit(db_factory: sessionmaker[Session]) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = set(pool.map(lambda _: get_live_run(db_factory, CLOCK, RuntimeSettings()).id, range(8)))
    assert len(ids) == 1
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.SimAccount)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(m.CashLedger)).scalar_one() == 1


@pytest.mark.db
def test_cad_account_deposit_applies_fx_once(db_factory: sessionmaker[Session]) -> None:
    s_cad = RuntimeSettings(
        starting_cash=Decimal("1000"), starting_cash_currency="CAD", fx_cad_usd_rate=Decimal("0.73")
    )
    run = get_live_run(db_factory, CLOCK, s_cad)
    get_live_run(db_factory, CLOCK, s_cad)
    acct = sim_account(db_factory, run.id)
    assert acct is not None
    assert acct.currency == "USD" and acct.starting_cash == Decimal("719.0500")
    assert acct.source_amount == Decimal("1000.0000") and acct.source_currency == "CAD"
    assert acct.fx_rate == Decimal("0.730000") and acct.fx_fee == Decimal("0.0150")
    with db_factory() as s:
        rows = s.execute(select(m.CashLedger)).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "deposit" and rows[0].amount == Decimal("719.0500")
    assert rows[0].trade_date == rows[0].settle_date == date(2026, 10, 6)
