"""Migration 0004 (P3-T1): the four Phase 3 operational tables. Acceptance tests 1 and 2.

The ORM-vs-schema comparison and the `alembic check` for these tables are the P1 tests in
tests/db/test_migration.py (test_models_match_migrated_schema and test_alembic_check_*), which cover the
whole metadata.
"""

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from tests.db.test_migration_0002 import TRADING_TABLES
from trader.db import models as m

pytestmark = pytest.mark.db

PHASE3_TABLES = {"worker_heartbeats", "telegram_callbacks", "notifications", "notify_cursors"}
T0 = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)

# table -> {column: (type text as the inspector renders it, nullable)}
COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "worker_heartbeats": {
        "process": ("VARCHAR(30)", False),
        "pid": ("INTEGER", False),
        "host": ("VARCHAR(100)", False),
        "started_at": ("TIMESTAMP", False),
        "beat_at": ("TIMESTAMP", False),
        "session_date": ("DATE", True),
        "phase": ("VARCHAR(20)", False),
        "detail": ("JSONB", True),
    },
    "telegram_callbacks": {
        "nonce": ("VARCHAR(16)", False),
        "kind": ("VARCHAR(20)", False),
        "ref": ("VARCHAR(50)", False),
        "chat_id": ("BIGINT", False),
        "message_id": ("BIGINT", True),
        "created_at": ("TIMESTAMP", False),
        "expires_at": ("TIMESTAMP", True),
        "used_at": ("TIMESTAMP", True),
        "used_action": ("VARCHAR(20)", True),
    },
    "notifications": {
        "id": ("BIGINT", False),
        "kind": ("VARCHAR(30)", False),
        "dedupe_key": ("VARCHAR(200)", True),
        "text": ("TEXT", False),
        "created_at": ("TIMESTAMP", False),
        "sent_at": ("TIMESTAMP", True),
        "status": ("VARCHAR(10)", False),
        "message_ids": ("JSONB", True),
        "attempts": ("INTEGER", False),
        "error": ("TEXT", True),
    },
    "notify_cursors": {
        "stream": ("VARCHAR(30)", False),
        "last_id": ("BIGINT", False),
        "updated_at": ("TIMESTAMP", False),
    },
}
PRIMARY_KEYS = {
    "worker_heartbeats": ["process"],
    "telegram_callbacks": ["nonce"],
    "notifications": ["id"],
    "notify_cursors": ["stream"],
}


def test_phase3_tables_exist(migrated_engine: Engine) -> None:
    assert PHASE3_TABLES <= set(inspect(migrated_engine).get_table_names(schema="trader"))


@pytest.mark.parametrize("table", sorted(PHASE3_TABLES))
def test_columns_and_primary_key(migrated_engine: Engine, table: str) -> None:
    insp = inspect(migrated_engine)
    got = {
        c["name"]: (str(c["type"]).split(" ")[0], c["nullable"])
        for c in insp.get_columns(table, schema="trader")
    }
    assert got == COLUMNS[table]
    assert insp.get_pk_constraint(table, schema="trader")["constrained_columns"] == PRIMARY_KEYS[table]
    assert insp.get_foreign_keys(table, schema="trader") == []  # operational tables: no run_id, no FKs


def test_timestamps_are_timestamptz(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        types = conn.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'trader' AND table_name = ANY(:t) AND data_type LIKE 'timestamp%'"
            ),
            {"t": sorted(PHASE3_TABLES)},
        ).all()
    assert len(types) == 8
    assert {t.data_type for t in types} == {"timestamp with time zone"}


def test_notifications_id_is_identity_and_attempts_default_zero(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rows = dict(
            conn.execute(
                text(
                    "SELECT column_name, coalesce(is_identity, 'NO') || '|' || coalesce(column_default, '') "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'trader' AND table_name = 'notifications' "
                    "AND column_name IN ('id', 'attempts')"
                )
            ).all()
        )
    assert rows["id"].startswith("YES|")
    assert rows["attempts"] == "NO|0"


def test_indexes(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    callbacks = {i["name"]: i for i in insp.get_indexes("telegram_callbacks", schema="trader")}
    assert callbacks["ix_telegram_callbacks_kind_ref"]["column_names"] == ["kind", "ref"]
    assert not callbacks["ix_telegram_callbacks_kind_ref"]["unique"]
    notes = {i["name"]: i for i in insp.get_indexes("notifications", schema="trader")}
    assert notes["uq_notifications_dedupe_key"]["column_names"] == ["dedupe_key"]
    assert notes["uq_notifications_dedupe_key"]["unique"]


def _note(key: str | None) -> m.Notification:
    return m.Notification(kind="alert", dedupe_key=key, text="hello", created_at=T0, status="sending")


def test_dedupe_key_is_unique(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(_note("fill:1"))
        s.commit()
        s.add(_note("fill:1"))
        with pytest.raises(IntegrityError, match="uq_notifications_dedupe_key"):
            s.commit()


def test_many_null_dedupe_keys_are_allowed(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add_all([_note(None), _note(None), _note(None)])
        s.commit()
        ids = s.execute(text("SELECT id, attempts FROM trader.notifications ORDER BY id")).all()
    assert [tuple(r) for r in ids] == [(1, 0), (2, 0), (3, 0)]


def test_orm_round_trip(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=123,
                host="trader-dev",
                started_at=T0,
                beat_at=T0,
                session_date=None,
                phase="starting",
                detail={"fills_today": 0},
            )
        )
        s.add(
            m.TelegramCallback(
                nonce="abcdefgh",
                kind="proposal",
                ref="1234567890",
                chat_id=-1001234567890,  # group chat ids are negative and wider than int32
                message_id=None,
                created_at=T0,
                expires_at=None,
                used_at=None,
                used_action=None,
            )
        )
        s.add(m.NotifyCursor(stream="fills", last_id=42, updated_at=T0))
        s.commit()
        cb = s.get(m.TelegramCallback, "abcdefgh")
        hb = s.get(m.WorkerHeartbeat, "worker")
        cur = s.get(m.NotifyCursor, "fills")
    assert cb is not None and cb.chat_id == -1001234567890
    assert hb is not None and hb.detail == {"fills_today": 0}
    assert cur is not None and cur.last_id == 42


def test_downgrade_to_0003_drops_phase3_tables_and_keeps_phase2(pg_url: str, migrated_engine: Engine) -> None:
    cfg = alembic_config(pg_url)
    command.downgrade(cfg, "0003")
    try:
        tables = set(inspect(migrated_engine).get_table_names(schema="trader"))
        with migrated_engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM trader.alembic_version")).scalar_one()
        assert version == "0003"
        assert not (PHASE3_TABLES & tables)
        assert TRADING_TABLES <= tables
    finally:
        command.upgrade(cfg, "head")
    assert PHASE3_TABLES <= set(inspect(migrated_engine).get_table_names(schema="trader"))
