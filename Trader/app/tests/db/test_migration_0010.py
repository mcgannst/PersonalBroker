"""Migration 0010 (FIX-DAY1): `opening_quote_captures` (the timed `open` and `bar` quote captures) and the
new nullable columns on `opening_bar_quotes` and `quote_volume_scale`. Additive. The ORM-vs-schema comparison
is in test_migration.py."""

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from tests.factories import add_symbol
from trader.db import models as m

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 6, 13, 29, 58, tzinfo=UTC)
DAY = date(2026, 10, 6)

CAPTURE_COLUMNS: dict[str, tuple[str, bool]] = {
    "session_date": ("DATE", False),
    "symbol_id": ("BIGINT", False),
    "kind": ("VARCHAR(10)", False),
    "capture_started_at": ("TIMESTAMP", False),
    "capture_ended_at": ("TIMESTAMP", False),
    "fetched_at": ("TIMESTAMP", False),
    "quote_time": ("TIMESTAMP", True),
    "open": ("NUMERIC(14, 4)", True),
    "high": ("NUMERIC(14, 4)", True),
    "low": ("NUMERIC(14, 4)", True),
    "last": ("NUMERIC(14, 4)", True),
    "last_regular": ("NUMERIC(14, 4)", True),
    "volume": ("BIGINT", False),
    "delay": ("INTEGER", True),
}
NEW_BAR_COLUMNS = {
    "open_volume": ("BIGINT", True),
    "volume_basis": ("VARCHAR(20)", True),
    "capture_started_at": ("TIMESTAMP", True),
    "capture_ended_at": ("TIMESTAMP", True),
}
NEW_SCALE_COLUMNS = {"premarket_volume": ("BIGINT", True), "premarket_source": ("VARCHAR(10)", True)}


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    out: dict[str, tuple[str, bool]] = {}
    for c in inspect(engine).get_columns(table, schema="trader"):
        ty = str(c["type"])
        out[c["name"]] = ("TIMESTAMP" if ty.startswith("TIMESTAMP") else ty, c["nullable"])
    return out


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM trader.alembic_version")).scalar_one())


def test_captures_table_columns_key_and_foreign_key(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert _columns(migrated_engine, "opening_quote_captures") == CAPTURE_COLUMNS
    assert insp.get_pk_constraint("opening_quote_captures", schema="trader")["constrained_columns"] == [
        "session_date",
        "symbol_id",
        "kind",
    ]
    fks = [
        (f["constrained_columns"], f["referred_table"])
        for f in insp.get_foreign_keys("opening_quote_captures", schema="trader")
    ]
    assert fks == [(["symbol_id"], "symbols")]


def test_new_columns_are_nullable(migrated_engine: Engine) -> None:
    bars = _columns(migrated_engine, "opening_bar_quotes")
    assert {k: bars[k] for k in NEW_BAR_COLUMNS} == NEW_BAR_COLUMNS
    scale = _columns(migrated_engine, "quote_volume_scale")
    assert {k: scale[k] for k in NEW_SCALE_COLUMNS} == NEW_SCALE_COLUMNS


def test_kind_is_checked_and_keyed(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = add_symbol(s, "CLDX", questrade_id=9)
        s.commit()
        row = {"session_date": DAY, "symbol_id": sym, "capture_started_at": T0, "capture_ended_at": T0,
               "fetched_at": T0, "volume": 303_905}  # fmt: skip
        s.add(m.OpeningQuoteCapture(kind="open", **row))
        s.add(m.OpeningQuoteCapture(kind="bar", **row))
        s.commit()
        s.add(m.OpeningQuoteCapture(kind="close", **row))
        with pytest.raises(IntegrityError, match="ck_opening_quote_captures_kind"):
            s.commit()
        s.rollback()
        s.add(m.OpeningQuoteCapture(kind="open", **row))
        with pytest.raises(IntegrityError, match="opening_quote_captures_pkey"):
            s.commit()


@pytest.fixture
def _restore_head(pg_url: str, migrated_engine: Engine) -> Iterator[None]:
    yield
    command.upgrade(alembic_config(pg_url), "head")


def test_downgrade_to_0009_and_back(pg_url: str, migrated_engine: Engine, _restore_head: None) -> None:
    command.downgrade(alembic_config(pg_url), "0009")
    assert _version(migrated_engine) == "0009"
    assert "opening_quote_captures" not in inspect(migrated_engine).get_table_names(schema="trader")
    assert not set(NEW_BAR_COLUMNS) & set(_columns(migrated_engine, "opening_bar_quotes"))
    assert not set(NEW_SCALE_COLUMNS) & set(_columns(migrated_engine, "quote_volume_scale"))
    command.upgrade(alembic_config(pg_url), "head")
    assert _version(migrated_engine) == "0011"
