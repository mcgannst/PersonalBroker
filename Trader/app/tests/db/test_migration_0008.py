"""Migration 0008 (DB-T1, live dashboard plan): `quote_marks` and `mark_bars`. Acceptance test 1.

The ORM-vs-schema comparison and `alembic check` cover both tables through tests/db/test_migration.py
(acceptance test 2); CHECK constraints, which alembic does not compare, are checked here.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from tests.factories import add_symbol
from trader.db import models as m
from trader.db.session import make_engine

pytestmark = pytest.mark.db

T0 = datetime(2026, 9, 29, 13, 35, 2, tzinfo=UTC)
MINUTE = datetime(2026, 9, 29, 13, 35, tzinfo=UTC)

QUOTE_MARK_COLUMNS: dict[str, tuple[str, bool]] = {
    "run_id": ("BIGINT", False),
    "symbol_id": ("BIGINT", False),
    "bid": ("NUMERIC(14, 4)", True),
    "ask": ("NUMERIC(14, 4)", True),
    "last": ("NUMERIC(14, 4)", True),
    "quote_time": ("TIMESTAMP", True),
    "observed_at": ("TIMESTAMP", False),
    "written_at": ("TIMESTAMP", False),
    "is_halted": ("BOOLEAN", False),
}
MARK_BAR_COLUMNS: dict[str, tuple[str, bool]] = {
    "run_id": ("BIGINT", False),
    "symbol_id": ("BIGINT", False),
    "minute_start": ("TIMESTAMP", False),
    "open": ("NUMERIC(14, 4)", False),
    "high": ("NUMERIC(14, 4)", False),
    "low": ("NUMERIC(14, 4)", False),
    "close": ("NUMERIC(14, 4)", False),
    "samples": ("INTEGER", False),
    "updated_at": ("TIMESTAMP", False),
}
TIMESTAMP_COLUMNS = {
    "quote_marks": {"quote_time", "observed_at", "written_at"},
    "mark_bars": {"minute_start", "updated_at"},
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


def _run(s: Session, mode: str = "live") -> int:
    run = m.Run(mode=mode, started_at=T0, params={}, status="active", label=mode)
    s.add(run)
    s.flush()
    return run.id


def _bar(run_id: int, symbol_id: int, **kw: Any) -> m.MarkBar:
    values: dict[str, Any] = {
        "run_id": run_id,
        "symbol_id": symbol_id,
        "minute_start": MINUTE,
        "open": Decimal("20.00"),
        "high": Decimal("20.10"),
        "low": Decimal("19.95"),
        "close": Decimal("20.05"),
        "samples": 3,
        "updated_at": T0,
    }
    values.update(kw)
    return m.MarkBar(**values)


# 1: head, columns, keys ------------------------------------------------------------------------------------
def test_0008_is_below_the_head(pg_url: str) -> None:
    script = ScriptDirectory.from_config(alembic_config(pg_url))
    assert script.get_current_head() == "0011"  # QUOTEBAR added 0009, FIX-DAY1 0010, OPTSIM 0011 on top
    rev = script.get_revision("0008")
    assert rev is not None and rev.down_revision == "0007"


@pytest.mark.parametrize(
    ("table", "columns", "pk"),
    [
        ("quote_marks", QUOTE_MARK_COLUMNS, ["run_id", "symbol_id"]),
        ("mark_bars", MARK_BAR_COLUMNS, ["run_id", "symbol_id", "minute_start"]),
    ],
)
def test_columns_primary_key_and_foreign_keys(
    migrated_engine: Engine, table: str, columns: dict[str, tuple[str, bool]], pk: list[str]
) -> None:
    insp = inspect(migrated_engine)
    assert _columns(migrated_engine, table) == columns
    assert insp.get_pk_constraint(table, schema="trader")["constrained_columns"] == pk
    fks = sorted(
        (f["constrained_columns"], f["referred_table"]) for f in insp.get_foreign_keys(table, schema="trader")
    )
    assert fks == [(["run_id"], "runs"), (["symbol_id"], "symbols")]


@pytest.mark.parametrize("table", ["quote_marks", "mark_bars"])
def test_timestamps_are_timestamptz(migrated_engine: Engine, table: str) -> None:
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'trader' AND table_name = :t AND data_type LIKE 'timestamp%'"
            ),
            {"t": table},
        ).all()
    assert {str(r[0]): str(r[1]) for r in rows} == dict.fromkeys(
        TIMESTAMP_COLUMNS[table], "timestamp with time zone"
    )


def test_check_constraints(migrated_engine: Engine) -> None:
    checks = {c["name"] for c in inspect(migrated_engine).get_check_constraints("mark_bars", schema="trader")}
    assert {"ck_mark_bars_ohlc", "ck_mark_bars_samples"} <= checks


def test_quote_mark_round_trip_upsert_key_and_default(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id = _run(s)
        sym = add_symbol(s, "AAPL", questrade_id=8049)
        s.add(
            m.QuoteMark(
                run_id=run_id,
                symbol_id=sym,
                bid=Decimal("182.40"),
                ask=Decimal("182.46"),
                last=Decimal("182.43"),
                quote_time=T0,
                observed_at=T0,
                written_at=T0,
            )
        )
        s.commit()
        row = s.execute(select(m.QuoteMark)).scalar_one()
        assert row.is_halted is False and row.last == Decimal("182.4300") and row.observed_at == T0
        s.expunge(row)
        s.add(m.QuoteMark(run_id=run_id, symbol_id=sym, observed_at=T0, written_at=T0))
        with pytest.raises(IntegrityError, match="quote_marks_pkey"):
            s.commit()


def test_mark_bar_round_trip_and_minute_key(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id = _run(s)
        sym = add_symbol(s, "MSFT", questrade_id=27426)
        s.add(_bar(run_id, sym))
        s.add(_bar(run_id, sym, minute_start=datetime(2026, 9, 29, 13, 36, tzinfo=UTC)))
        s.commit()
        assert s.execute(select(m.MarkBar.samples).order_by(m.MarkBar.minute_start)).scalars().all() == [3, 3]
        s.add(_bar(run_id, sym))
        with pytest.raises(IntegrityError, match="mark_bars_pkey"):
            s.commit()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"low": Decimal("20.01")}, "ck_mark_bars_ohlc"),  # low > open
        ({"high": Decimal("20.04")}, "ck_mark_bars_ohlc"),  # high < close
        ({"low": Decimal("0"), "open": Decimal("0.01"), "close": Decimal("0.01")}, "ck_mark_bars_ohlc"),
        ({"samples": 0}, "ck_mark_bars_samples"),
    ],
)
def test_bad_bars_are_refused(
    db_factory: sessionmaker[Session], overrides: dict[str, Any], constraint: str
) -> None:
    with db_factory() as s:
        run_id = _run(s)
        sym = add_symbol(s, "NVDA", questrade_id=29814)
        s.add(_bar(run_id, sym, **overrides))
        with pytest.raises(IntegrityError, match=constraint):
            s.commit()


@pytest.mark.parametrize("model", [m.QuoteMark, m.MarkBar])
def test_run_and_symbol_must_exist(db_factory: sessionmaker[Session], model: type[m.Base]) -> None:
    with db_factory() as s:
        if model is m.QuoteMark:
            s.add(m.QuoteMark(run_id=999_999, symbol_id=999_999, observed_at=T0, written_at=T0))
        else:
            s.add(_bar(999_999, 999_999))
        with pytest.raises(IntegrityError, match="foreign key"):
            s.commit()


# 1 (downgrade) ---------------------------------------------------------------------------------------------
@pytest.fixture
def _restore_head(pg_url: str, migrated_engine: Engine) -> Iterator[None]:
    yield
    command.upgrade(alembic_config(pg_url), "head")


def test_downgrade_drops_both_tables_and_upgrade_restores_them(
    pg_url: str, migrated_engine: Engine, _restore_head: None
) -> None:
    command.downgrade(alembic_config(pg_url), "0007")
    assert _version(migrated_engine) == "0007"
    tables = set(inspect(migrated_engine).get_table_names(schema="trader"))
    assert not {"quote_marks", "mark_bars"} & tables
    assert "decision_log" in tables  # 0007 untouched
    command.upgrade(alembic_config(pg_url), "head")
    assert _version(migrated_engine) == "0011"
    assert {"quote_marks", "mark_bars"} <= set(inspect(migrated_engine).get_table_names(schema="trader"))


# 1 (app role) ----------------------------------------------------------------------------------------------
def _url(base: str, **parts: str) -> str:
    return make_url(base).set(**parts).render_as_string(hide_password=False)  # type: ignore[arg-type]


def _admin(url: str, *statements: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def app_role_url(pg_url: str) -> Iterator[str]:
    """The dev set-up: an owner role runs the migrations; the app role gets DML through default privileges."""
    _admin(
        pg_url,
        "DROP DATABASE IF EXISTS db1_lp",
        "DROP ROLE IF EXISTS db1_app",
        "DROP ROLE IF EXISTS db1_owner",
        "CREATE ROLE db1_owner LOGIN PASSWORD 'db1_owner_pw'",
        "CREATE ROLE db1_app LOGIN PASSWORD 'db1_app_pw'",
        "CREATE DATABASE db1_lp OWNER db1_owner",
    )
    _admin(
        _url(pg_url, database="db1_lp"),
        "CREATE SCHEMA trader AUTHORIZATION db1_owner",
        "GRANT USAGE ON SCHEMA trader TO db1_app",
        "ALTER DEFAULT PRIVILEGES FOR ROLE db1_owner IN SCHEMA trader "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO db1_app",
    )
    command.upgrade(
        alembic_config(_url(pg_url, database="db1_lp", username="db1_owner", password="db1_owner_pw")), "head"
    )
    yield _url(pg_url, database="db1_lp", username="db1_app", password="db1_app_pw")
    _admin(pg_url, "DROP DATABASE IF EXISTS db1_lp WITH (FORCE)")


def test_app_role_can_insert_update_delete_but_not_alter(app_role_url: str) -> None:
    engine = make_engine(app_role_url)
    try:
        with engine.begin() as conn:
            run_id = conn.execute(
                text(
                    "INSERT INTO trader.runs (mode, started_at, params, status) "
                    "VALUES ('live', :t, '{}', 'active') RETURNING id"
                ),
                {"t": T0},
            ).scalar_one()
            sym = conn.execute(
                text(
                    "INSERT INTO trader.symbols (ticker, exchange, questrade_id, currency) "
                    "VALUES ('AAPL', 'NASDAQ', 8049, 'USD') RETURNING id"
                ),
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO trader.quote_marks (run_id, symbol_id, last, observed_at, written_at) "
                    "VALUES (:r, :s, 182.43, :t, :t)"
                ),
                {"r": run_id, "s": sym, "t": T0},
            )
            conn.execute(
                text(
                    "INSERT INTO trader.mark_bars "
                    "(run_id, symbol_id, minute_start, open, high, low, close, samples, updated_at) "
                    "VALUES (:r, :s, :m, 20, 20.1, 19.9, 20.05, 2, :t)"
                ),
                {"r": run_id, "s": sym, "m": MINUTE, "t": T0},
            )
        with engine.begin() as conn:
            conn.execute(text("UPDATE trader.quote_marks SET last = 182.50"))
            conn.execute(text("UPDATE trader.mark_bars SET samples = samples + 1"))
            assert conn.execute(text("SELECT last FROM trader.quote_marks")).scalar_one() == Decimal(
                "182.5000"
            )
            assert conn.execute(text("SELECT samples FROM trader.mark_bars")).scalar_one() == 3
            conn.execute(text("DELETE FROM trader.quote_marks"))
            conn.execute(text("DELETE FROM trader.mark_bars"))
            assert conn.execute(text("SELECT count(*) FROM trader.quote_marks")).scalar_one() == 0
        for stmt in ("ALTER TABLE trader.mark_bars ADD COLUMN evil int", "DROP TABLE trader.quote_marks"):
            with engine.connect() as conn:
                with pytest.raises(ProgrammingError, match="permission denied|must be owner"):
                    conn.execute(text(stmt))
    finally:
        engine.dispose()
