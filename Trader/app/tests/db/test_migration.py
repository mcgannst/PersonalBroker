from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import IntradayCandle, Symbol

pytestmark = pytest.mark.db

PHASE1_TABLES = {
    "settings",
    "api_credentials",
    "symbols",
    "universe_snapshots",
    "daily_candles",
    "intraday_candles",
    "candle_archive",
    "open_bar_stats",
    "job_runs",
    "event_log",
    "audit_log",
    "alembic_version",
}


def test_all_phase1_tables_exist(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names(schema="trader"))
    assert PHASE1_TABLES <= tables


def test_intraday_candles_route_to_monthly_partition(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = Symbol(ticker="AAPL", exchange="NASDAQ", questrade_id=8049, currency="USD", name="Apple")
        s.add(sym)
        s.flush()
        s.add(
            IntradayCandle(
                symbol_id=sym.id,
                interval="5m",
                ts=datetime(2026, 9, 25, 13, 30, tzinfo=UTC),
                open=Decimal("336.04"),
                high=Decimal("336.80"),
                low=Decimal("334.53"),
                close=Decimal("334.92"),
                volume=403790,
                vwap=Decimal("335.5643"),
            )
        )
        s.commit()
        part = s.execute(
            text("SELECT tableoid::regclass::text FROM trader.intraday_candles LIMIT 1")
        ).scalar_one()
    assert part == "trader.intraday_candles_202609"


def test_money_keeps_four_decimals(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = Symbol(ticker="X", exchange="NYSE", questrade_id=1, currency="USD", name=None)
        s.add(sym)
        s.commit()
        s.add(
            IntradayCandle(
                symbol_id=sym.id,
                interval="1m",
                ts=datetime(2026, 9, 25, 14, 0, tzinfo=UTC),
                open=Decimal("1.2345"),
                high=Decimal("1.2345"),
                low=Decimal("1.2345"),
                close=Decimal("1.2345"),
                volume=1,
                vwap=None,
            )
        )
        s.commit()
        got = s.execute(text("SELECT close FROM trader.intraday_candles")).scalar_one()
    assert got == Decimal("1.2345")


def test_downgrade_and_upgrade_again(pg_url: str) -> None:
    from alembic import command

    from tests.conftest import alembic_config

    cfg = alembic_config(pg_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
