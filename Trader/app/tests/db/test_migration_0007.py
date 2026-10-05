"""Migration 0007 (P6-T9): the `decision_log` table. Acceptance tests 1 and 2.

The ORM-vs-schema comparison and `alembic check` cover the table through tests/db/test_migration.py, which
compares the whole metadata (columns, the unique key and the indexes; CHECK constraints are checked here).
"""

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import cast, get_args

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from trader.db import models as m
from trader.db.session import make_engine
from trader.decisions.types import DecisionOutcome, DecisionStage

pytestmark = pytest.mark.db

T0 = datetime(2026, 9, 28, 13, 35, 5, tzinfo=UTC)
D = date(2026, 9, 28)

COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "run_id": ("BIGINT", False),
    "session_date": ("DATE", False),
    "seq": ("INTEGER", False),
    "stage": ("VARCHAR(20)", False),
    "strategy_key": ("VARCHAR(50)", True),
    "symbol_id": ("BIGINT", True),
    "ticker": ("VARCHAR(20)", True),
    "outcome": ("VARCHAR(20)", False),
    "rule": ("VARCHAR(60)", True),
    "reason": ("TEXT", True),
    "ts": ("TIMESTAMP", False),
    "ref": ("JSONB", False),
    "data": ("JSONB", False),
    "recorded_at": ("TIMESTAMP", False),
    "final": ("BOOLEAN", False),
}


def _columns(engine: Engine) -> dict[str, tuple[str, bool]]:
    return {
        c["name"]: (str(c["type"]).split(" ")[0], c["nullable"])
        for c in inspect(engine).get_columns("decision_log", schema="trader")
    }


def _check_values(engine: Engine, name: str) -> set[str]:
    checks = {
        c["name"]: c["sqltext"]
        for c in inspect(engine).get_check_constraints("decision_log", schema="trader")
    }
    assert name in checks, checks
    return set(re.findall(r"'([^']+)'", checks[name]))


def _run(s: Session) -> int:
    run = m.Run(mode="live", started_at=T0, params={}, status="active", label="live")
    s.add(run)
    s.flush()
    return run.id


def _row(run_id: int, seq: int, **kw: object) -> m.DecisionLog:
    values: dict[str, object] = {
        "run_id": run_id,
        "session_date": D,
        "seq": seq,
        "stage": "scan",
        "outcome": "rejected",
        "ts": T0,
        "recorded_at": T0,
    }
    values.update(kw)
    return m.DecisionLog(**values)


def _head(pg_url: str) -> str:
    head = ScriptDirectory.from_config(alembic_config(pg_url)).get_current_head()
    assert head is not None
    return head


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM trader.alembic_version")).scalar_one())


# 1 ---------------------------------------------------------------------------------------------------------
def test_0007_is_in_the_chain_below_the_head(pg_url: str) -> None:
    assert _head(pg_url) == "0011"  # DB-T1 added 0008, QUOTEBAR 0009, FIX-DAY1 0010, OPTSIM 0011
    script = ScriptDirectory.from_config(alembic_config(pg_url)).get_revision("0007")
    assert script is not None and script.down_revision == "0006"


def test_columns_primary_key_and_foreign_keys(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert _columns(migrated_engine) == COLUMNS
    assert insp.get_pk_constraint("decision_log", schema="trader")["constrained_columns"] == ["id"]
    fks = sorted(
        (f["constrained_columns"], f["referred_table"])
        for f in insp.get_foreign_keys("decision_log", schema="trader")
    )
    assert fks == [(["run_id"], "runs"), (["symbol_id"], "symbols")]


def test_timestamps_are_timestamptz(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'trader' AND table_name = 'decision_log' "
                "AND column_name IN ('ts', 'recorded_at')"
            )
        ).all()
        types = {str(r[0]): str(r[1]) for r in rows}
    assert types == {"ts": "timestamp with time zone", "recorded_at": "timestamp with time zone"}


def test_unique_key_and_indexes(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    uniques = {
        u["name"]: u["column_names"] for u in insp.get_unique_constraints("decision_log", schema="trader")
    }
    assert uniques == {"uq_decision_log_run_day_seq": ["run_id", "session_date", "seq"]}
    indexes = {i["name"]: i for i in insp.get_indexes("decision_log", schema="trader")}
    assert indexes["ix_decision_log_run_day_stage"]["column_names"] == ["run_id", "session_date", "stage"]
    assert indexes["ix_decision_log_day"]["column_names"] == ["session_date"]
    assert not indexes["ix_decision_log_run_day_stage"]["unique"]
    assert not indexes["ix_decision_log_day"]["unique"]


def test_defaults_and_orm_round_trip(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id = _run(s)
        s.add(_row(run_id, 1, ticker="NVDA", rule="rvol_below_min", ref={"candidate_id": 5}))
        s.commit()
        # server defaults: ref and data '{}', final false
        s.execute(
            text(
                "INSERT INTO trader.decision_log "
                "(run_id, session_date, seq, stage, outcome, ts, recorded_at) "
                "VALUES (:r, :d, 2, 'day', 'info', :t, :t)"
            ),
            {"r": run_id, "d": D, "t": T0},
        )
        s.commit()
        rows = list(s.execute(select(m.DecisionLog).order_by(m.DecisionLog.seq)).scalars())
    assert [r.seq for r in rows] == [1, 2]
    assert rows[0].ref == {"candidate_id": 5} and rows[0].data == {} and rows[0].final is False
    assert rows[0].ts == T0 and rows[0].session_date == D and rows[0].ticker == "NVDA"
    assert rows[1].ref == {} and rows[1].data == {} and rows[1].final is False


def test_seq_is_unique_per_run_and_day(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id = _run(s)
        s.add(_row(run_id, 1))
        s.add(_row(run_id, 1, session_date=date(2026, 9, 29)))  # another day: fine
        s.commit()
        s.add(_row(run_id, 1))
        with pytest.raises(IntegrityError, match="uq_decision_log_run_day_seq"):
            s.commit()


def test_run_must_exist(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(_row(999_999, 1))
        with pytest.raises(IntegrityError, match="run_id"):
            s.commit()


# 2 ---------------------------------------------------------------------------------------------------------
def test_check_lists_equal_the_literals(migrated_engine: Engine) -> None:
    assert _check_values(migrated_engine, "ck_decision_log_stage") == set(get_args(DecisionStage))
    assert _check_values(migrated_engine, "ck_decision_log_outcome") == set(get_args(DecisionOutcome))


def test_orm_check_constraints_equal_the_literals() -> None:
    table = cast(Table, m.DecisionLog.__table__)
    checks = {c.name: str(getattr(c, "sqltext", "")) for c in table.constraints}
    assert set(re.findall(r"'([^']+)'", checks["ck_decision_log_stage"])) == set(get_args(DecisionStage))
    assert set(re.findall(r"'([^']+)'", checks["ck_decision_log_outcome"])) == set(get_args(DecisionOutcome))


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [("stage", "universe_x", "ck_decision_log_stage"), ("outcome", "maybe", "ck_decision_log_outcome")],
)
def test_values_outside_the_literals_are_refused(
    db_factory: sessionmaker[Session], column: str, value: str, constraint: str
) -> None:
    with db_factory() as s:
        run_id = _run(s)
        s.add(_row(run_id, 1, **{column: value}))
        with pytest.raises(IntegrityError, match=constraint):
            s.commit()


# 1 (downgrade) ---------------------------------------------------------------------------------------------
@pytest.fixture
def _restore_head(pg_url: str, migrated_engine: Engine) -> Iterator[None]:
    yield
    command.upgrade(alembic_config(pg_url), "head")


def test_downgrade_drops_the_table_and_upgrade_restores_it(
    pg_url: str, migrated_engine: Engine, _restore_head: None
) -> None:
    command.downgrade(alembic_config(pg_url), "0006")
    assert _version(migrated_engine) == "0006"
    assert "decision_log" not in inspect(migrated_engine).get_table_names(schema="trader")
    command.upgrade(alembic_config(pg_url), "0007")
    assert _version(migrated_engine) == "0007"
    assert _columns(migrated_engine) == COLUMNS


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
        "DROP DATABASE IF EXISTS t9_lp",
        "DROP ROLE IF EXISTS t9_app",
        "DROP ROLE IF EXISTS t9_owner",
        "CREATE ROLE t9_owner LOGIN PASSWORD 't9_owner_pw'",
        "CREATE ROLE t9_app LOGIN PASSWORD 't9_app_pw'",
        "CREATE DATABASE t9_lp OWNER t9_owner",
    )
    _admin(
        _url(pg_url, database="t9_lp"),
        "CREATE SCHEMA trader AUTHORIZATION t9_owner",
        "GRANT USAGE ON SCHEMA trader TO t9_app",
        "ALTER DEFAULT PRIVILEGES FOR ROLE t9_owner IN SCHEMA trader "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO t9_app",
    )
    command.upgrade(
        alembic_config(_url(pg_url, database="t9_lp", username="t9_owner", password="t9_owner_pw")), "head"
    )
    yield _url(pg_url, database="t9_lp", username="t9_app", password="t9_app_pw")
    _admin(pg_url, "DROP DATABASE IF EXISTS t9_lp WITH (FORCE)")


def test_app_role_can_insert_select_delete_but_not_alter(app_role_url: str) -> None:
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
            conn.execute(
                text(
                    "INSERT INTO trader.decision_log "
                    "(run_id, session_date, seq, stage, outcome, ts, recorded_at) "
                    "VALUES (:r, :d, 1, 'scan', 'passed', :t, :t)"
                ),
                {"r": run_id, "d": D, "t": T0},
            )
        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM trader.decision_log")).scalar_one() == 1
            conn.execute(text("DELETE FROM trader.decision_log"))
            assert conn.execute(text("SELECT count(*) FROM trader.decision_log")).scalar_one() == 0
        for stmt in (
            "ALTER TABLE trader.decision_log ADD COLUMN evil int",
            "DROP TABLE trader.decision_log",
        ):
            with engine.connect() as conn:
                with pytest.raises(ProgrammingError, match="permission denied|must be owner"):
                    conn.execute(text(stmt))
    finally:
        engine.dispose()
