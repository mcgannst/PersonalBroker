"""Migration 0009 (QUOTEBAR): `opening_bar_quotes` (the 9:35 bars built from live quotes, with the shadow
check's official candle beside them) and `quote_volume_scale` (the candle/quote volume factor measured after
each close). Market data, not run-scoped; additive. The ORM-vs-schema comparison is in test_migration.py."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from tests.factories import add_symbol
from trader.db import models as m

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)
DAY = date(2026, 10, 6)

OPENING_BAR_QUOTE_COLUMNS: dict[str, tuple[str, bool]] = {
    "session_date": ("DATE", False),
    "symbol_id": ("BIGINT", False),
    "captured_at": ("TIMESTAMP", False),
    "quote_time": ("TIMESTAMP", True),
    "open": ("NUMERIC(14, 4)", False),
    "high": ("NUMERIC(14, 4)", False),
    "low": ("NUMERIC(14, 4)", False),
    "close": ("NUMERIC(14, 4)", False),
    "quote_volume": ("BIGINT", False),
    "volume": ("BIGINT", False),
    "vol_factor": ("NUMERIC(10, 6)", False),
    "factor_source": ("VARCHAR(10)", False),
    "checked_at": ("TIMESTAMP", True),
    "check_status": ("VARCHAR(200)", True),
    "official_open": ("NUMERIC(14, 4)", True),
    "official_high": ("NUMERIC(14, 4)", True),
    "official_low": ("NUMERIC(14, 4)", True),
    "official_close": ("NUMERIC(14, 4)", True),
    "official_volume": ("BIGINT", True),
    "decision_differs": ("BOOLEAN", True),
}
QUOTE_VOLUME_SCALE_COLUMNS: dict[str, tuple[str, bool]] = {
    "session_date": ("DATE", False),
    "symbol_id": ("BIGINT", False),
    "quote_volume": ("BIGINT", False),
    "candle_volume": ("BIGINT", True),
    "factor": ("NUMERIC(10, 6)", True),
    "recorded_at": ("TIMESTAMP", False),
}


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    out: dict[str, tuple[str, bool]] = {}
    for c in inspect(engine).get_columns(table, schema="trader"):
        ty = str(c["type"])
        out[c["name"]] = ("TIMESTAMP" if ty.startswith("TIMESTAMP") else ty, c["nullable"])
    return out


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM trader.alembic_version")).scalar_one())


def test_head_is_0009(pg_url: str) -> None:
    assert ScriptDirectory.from_config(alembic_config(pg_url)).get_current_head() == "0009"


@pytest.mark.parametrize(
    ("table", "columns"),
    [("opening_bar_quotes", OPENING_BAR_QUOTE_COLUMNS), ("quote_volume_scale", QUOTE_VOLUME_SCALE_COLUMNS)],
)
def test_columns_keys_and_foreign_keys(
    migrated_engine: Engine, table: str, columns: dict[str, tuple[str, bool]]
) -> None:
    insp = inspect(migrated_engine)
    assert _columns(migrated_engine, table) == columns
    assert insp.get_pk_constraint(table, schema="trader")["constrained_columns"] == [
        "session_date",
        "symbol_id",
    ]
    fks = [
        (f["constrained_columns"], f["referred_table"]) for f in insp.get_foreign_keys(table, schema="trader")
    ]
    assert fks == [(["symbol_id"], "symbols")]


def test_round_trip_and_key(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = add_symbol(s, "SMMT", questrade_id=7)
        s.add(
            m.QuoteVolumeScale(
                session_date=DAY,
                symbol_id=sym,
                quote_volume=1_400,
                candle_volume=1_000,
                factor=Decimal("0.714286"),
                recorded_at=T0,
            )
        )
        s.commit()
        assert s.get(m.QuoteVolumeScale, (DAY, sym)).factor == Decimal("0.714286")  # type: ignore[union-attr]
        s.add(m.QuoteVolumeScale(session_date=DAY, symbol_id=sym, quote_volume=1, recorded_at=T0))
        with pytest.raises(IntegrityError, match="quote_volume_scale_pkey"):
            s.commit()


@pytest.fixture
def _restore_head(pg_url: str, migrated_engine: Engine) -> Iterator[None]:
    yield
    command.upgrade(alembic_config(pg_url), "head")


def test_downgrade_drops_both_tables_and_upgrade_restores_them(
    pg_url: str, migrated_engine: Engine, _restore_head: None
) -> None:
    command.downgrade(alembic_config(pg_url), "0008")
    assert _version(migrated_engine) == "0008"
    tables = set(inspect(migrated_engine).get_table_names(schema="trader"))
    assert not {"opening_bar_quotes", "quote_volume_scale"} & tables
    assert {"quote_marks", "mark_bars"} <= tables  # 0008 untouched
    command.upgrade(alembic_config(pg_url), "head")
    assert _version(migrated_engine) == "0009"
