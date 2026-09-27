import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import APP_DIR, alembic_config
from trader.db.models import SCHEMA, Base, IntradayCandle, Symbol

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
    cfg = alembic_config(pg_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


PARTITION = re.compile(r"^intraday_candles_(\d{6}|default)$")


def _include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    if type_ == "schema":  # the test container's default schema is public
        return name == SCHEMA
    return not (type_ == "table" and name and PARTITION.match(name))


def test_models_match_migrated_schema(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "include_name": _include_name,
                "version_table_schema": SCHEMA,
            },
        )
        diffs = compare_metadata(ctx, Base.metadata)
    assert diffs == []


def test_alembic_check_through_env_sees_no_changes(pg_url: str, migrated_engine: Engine) -> None:
    """env.py's own filters: autogenerate must not propose dropping the intraday partitions."""
    command.check(alembic_config(pg_url))


def test_alembic_check_when_trader_is_the_default_schema(pg_url: str) -> None:
    """On trader_dev the owner's search_path is `trader, public`. Reflected with that search_path,
    trader's tables and foreign keys lose their schema and autogenerate sees phantom differences;
    env.py pins search_path=public so it must still see none."""
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text("DROP DATABASE IF EXISTS search_path_trader"))
            conn.execute(text("CREATE DATABASE search_path_trader"))
            conn.execute(text("ALTER DATABASE search_path_trader SET search_path TO trader, public"))
    finally:
        admin.dispose()
    url = make_url(pg_url).set(database="search_path_trader").render_as_string(hide_password=False)
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_app_sessions_are_utc(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SHOW TimeZone")).scalar_one() == "UTC"


def test_partition_bounds_are_utc_midnight(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        bound = conn.execute(
            text(
                "SELECT pg_get_expr(relpartbound, oid) FROM pg_class "
                "WHERE oid = 'trader.intraday_candles_202610'::regclass"
            )
        ).scalar_one()
    assert bound == "FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00')"


def test_env_requires_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    cfg = Config(str(APP_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(APP_DIR / "trader" / "db" / "migrations"))
    with pytest.raises(RuntimeError, match="MIGRATION_DATABASE_URL"):
        command.current(cfg)
