"""Migration 0006 (P5-T1): replay run columns, strategy config scope, weekly reports. Acceptance test 1.

The ORM-vs-schema comparison and `alembic check` cover these through tests/db/test_migration.py, which
compares the whole metadata.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from trader.db import models as m

pytestmark = pytest.mark.db

T0 = datetime(2026, 11, 27, 21, 0, tzinfo=UTC)

RUN_COLUMNS = {
    "finished_at": ("TIMESTAMP", True),
    "updated_at": ("TIMESTAMP", True),
    "progress": ("JSONB", True),
    "error": ("TEXT", True),
    "cancel_requested": ("BOOLEAN", False),
}
WEEKLY_COLUMNS = {
    "week_ending": ("DATE", False),
    "week_start": ("DATE", False),
    "run_id": ("BIGINT", False),
    "facts": ("JSONB", False),
    "commentary": ("TEXT", True),
    "commentary_status": ("VARCHAR(20)", False),
    "commentary_error": ("TEXT", True),
    "model": ("VARCHAR(60)", True),
    "input_tokens": ("INTEGER", True),
    "output_tokens": ("INTEGER", True),
    "cost_usd": ("NUMERIC(10, 6)", False),
    "created_at": ("TIMESTAMP", False),
    "updated_at": ("TIMESTAMP", False),
}


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    return {
        c["name"]: (str(c["type"]).split(" W")[0], c["nullable"])
        for c in inspect(engine).get_columns(table, schema="trader")
    }


def _config(key: str, revision: int, scope: str = "live") -> m.StrategyConfig:
    return m.StrategyConfig(
        strategy_key=key,
        version="1.0.0",
        revision=revision,
        params={},
        enabled=True,
        created_at=T0,
        created_by="test",
        scope=scope,
    )


def _live_run() -> m.Run:
    return m.Run(mode="live", started_at=T0, params={}, status="active", label="live")


def test_runs_gain_the_replay_columns(migrated_engine: Engine) -> None:
    got = _columns(migrated_engine, "runs")
    for name, spec in RUN_COLUMNS.items():
        assert got[name] == spec
    indexes = {i["name"]: i for i in inspect(migrated_engine).get_indexes("runs", schema="trader")}
    assert indexes["ix_runs_mode_status"]["column_names"] == ["mode", "status"]
    assert not indexes["ix_runs_mode_status"]["unique"]


def test_strategy_configs_scope_check_and_partial_index(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert _columns(migrated_engine, "strategy_configs")["scope"] == ("VARCHAR(10)", False)
    checks = {
        c["name"]: c["sqltext"] for c in insp.get_check_constraints("strategy_configs", schema="trader")
    }
    assert "ck_strategy_configs_scope" in checks
    uniques = {u["name"] for u in insp.get_unique_constraints("strategy_configs", schema="trader")}
    assert "uq_strategy_configs_key_revision" not in uniques
    indexes = {i["name"]: i for i in insp.get_indexes("strategy_configs", schema="trader")}
    live = indexes["uq_strategy_configs_key_revision_live"]
    assert live["unique"] and live["column_names"] == ["strategy_key", "revision"]
    assert "scope" in live["dialect_options"]["postgresql_where"]


def test_weekly_reports_table(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert _columns(migrated_engine, "weekly_reports") == WEEKLY_COLUMNS
    assert insp.get_pk_constraint("weekly_reports", schema="trader")["constrained_columns"] == ["week_ending"]
    fks = insp.get_foreign_keys("weekly_reports", schema="trader")
    assert [(f["constrained_columns"], f["referred_table"]) for f in fks] == [(["run_id"], "runs")]


def test_scope_defaults_to_live_and_live_revisions_are_unique(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.execute(
            text(
                "INSERT INTO trader.strategy_configs "
                "(strategy_key, version, revision, params, enabled, created_at, created_by) "
                "VALUES ('orb_sip', '1.0.0', 1, '{}', true, now(), 'test')"
            )
        )
        s.commit()
        assert s.execute(select(m.StrategyConfig.scope)).scalar_one() == "live"
        s.add(_config("orb_sip", 1))
        with pytest.raises(IntegrityError, match="uq_strategy_configs_key_revision_live"):
            s.commit()


def test_a_replay_row_may_repeat_a_live_revision(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(_config("orb_sip", 1))
        s.commit()
        s.add_all([_config("orb_sip", 1, "replay"), _config("orb_sip", 1, "replay")])
        s.commit()
        scopes = sorted(s.execute(select(m.StrategyConfig.scope)).scalars())
    assert scopes == ["live", "replay", "replay"]


def test_scope_check_refuses_other_values(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(_config("orb_sip", 1, "paper"))
        with pytest.raises(IntegrityError, match="ck_strategy_configs_scope"):
            s.commit()


def test_orm_round_trip(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = _live_run()
        s.add(run)
        s.flush()
        s.add(
            m.WeeklyReport(
                week_ending=date(2026, 11, 27),
                week_start=date(2026, 11, 23),
                run_id=run.id,
                facts={"week": {"sessions": 4}},
                commentary_status="ok",
                created_at=T0,
                updated_at=T0,
            )
        )
        s.commit()
        got = s.get(m.WeeklyReport, date(2026, 11, 27))
        run_row = s.get(m.Run, run.id)
    assert got is not None and got.cost_usd == Decimal(0) and got.commentary is None
    assert run_row is not None and run_row.cancel_requested is False and run_row.progress is None


@pytest.fixture
def _restore_head(pg_url: str, migrated_engine: Engine) -> Iterator[None]:
    yield
    command.upgrade(alembic_config(pg_url), "head")
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM trader.strategy_configs"))


def _head(pg_url: str) -> str:
    return str(ScriptDirectory.from_config(alembic_config(pg_url)).get_current_head())


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM trader.alembic_version")).scalar_one())


def test_downgrade_refuses_with_replay_rows(
    pg_url: str, migrated_engine: Engine, _restore_head: None
) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO trader.strategy_configs "
                "(strategy_key, version, revision, params, enabled, created_at, created_by, scope) "
                "VALUES ('orb_sip', '1.0.0', 1, '{}', true, now(), 'replay:1', 'replay')"
            )
        )
    with pytest.raises(RuntimeError, match="delete the replay runs first"):
        command.downgrade(alembic_config(pg_url), "0005")
    assert _version(migrated_engine) == _head(pg_url)  # nothing downgraded (P6-T9: head is now 0007)


def test_downgrade_to_0005_and_back(pg_url: str, migrated_engine: Engine, _restore_head: None) -> None:
    command.downgrade(alembic_config(pg_url), "0005")
    insp = inspect(migrated_engine)
    assert _version(migrated_engine) == "0005"
    assert "weekly_reports" not in insp.get_table_names(schema="trader")
    assert "scope" not in {c["name"] for c in insp.get_columns("strategy_configs", schema="trader")}
    assert not set(RUN_COLUMNS) & {c["name"] for c in insp.get_columns("runs", schema="trader")}
    uniques = {u["name"] for u in insp.get_unique_constraints("strategy_configs", schema="trader")}
    assert "uq_strategy_configs_key_revision" in uniques
    command.upgrade(alembic_config(pg_url), "head")
    assert _version(migrated_engine) == _head(pg_url)
