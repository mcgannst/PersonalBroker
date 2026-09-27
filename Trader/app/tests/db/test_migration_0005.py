"""Migration 0005 (P4-T1): users, web_sessions and manual_watchlists. Acceptance tests 1 and 2.

The ORM-vs-schema comparison and the `alembic check` cover these tables through the P1 tests in
tests/db/test_migration.py (test_models_match_migrated_schema and test_alembic_check_*), which compare the
whole metadata.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from tests.db.test_migration_0004 import PHASE3_TABLES
from trader.db import models as m

pytestmark = pytest.mark.db

PHASE4_TABLES = {"users", "web_sessions", "manual_watchlists"}
T0 = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)

# table -> {column: (type text as the inspector renders it, nullable)}
COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "users": {
        "id": ("BIGINT", False),
        "username": ("VARCHAR(50)", False),
        "password_hash": ("TEXT", False),
        "totp_secret_enc": ("TEXT", True),
        "totp_pending_enc": ("TEXT", True),
        "totp_last_step": ("BIGINT", True),
        "failed_logins": ("INTEGER", False),
        "locked_until": ("TIMESTAMP", True),
        "created_at": ("TIMESTAMP", False),
        "updated_at": ("TIMESTAMP", False),
        "password_changed_at": ("TIMESTAMP", False),
        "last_login_at": ("TIMESTAMP", True),
    },
    "web_sessions": {
        "id": ("BIGINT", False),
        "user_id": ("BIGINT", False),
        "token_hash": ("VARCHAR(64)", False),
        "csrf_token": ("VARCHAR(64)", False),
        "created_at": ("TIMESTAMP", False),
        "last_seen_at": ("TIMESTAMP", False),
        "expires_at": ("TIMESTAMP", False),
        "revoked_at": ("TIMESTAMP", True),
        "ip": ("VARCHAR(45)", True),
        "user_agent": ("VARCHAR(200)", True),
    },
    "manual_watchlists": {
        "session_date": ("DATE", False),
        "tickers": ("JSONB", False),
        "filename": ("VARCHAR(200)", True),
        "uploaded_at": ("TIMESTAMP", False),
        "uploaded_by": ("VARCHAR(50)", False),
    },
}
PRIMARY_KEYS = {"users": ["id"], "web_sessions": ["id"], "manual_watchlists": ["session_date"]}
TIMESTAMP_COLUMNS = sum(1 for cols in COLUMNS.values() for t, _ in cols.values() if t == "TIMESTAMP")


def test_phase4_tables_exist(migrated_engine: Engine) -> None:
    assert PHASE4_TABLES <= set(inspect(migrated_engine).get_table_names(schema="trader"))


@pytest.mark.parametrize("table", sorted(PHASE4_TABLES))
def test_columns_and_primary_key(migrated_engine: Engine, table: str) -> None:
    insp = inspect(migrated_engine)
    got = {
        c["name"]: (str(c["type"]).split(" ")[0], c["nullable"])
        for c in insp.get_columns(table, schema="trader")
    }
    assert got == COLUMNS[table]
    assert insp.get_pk_constraint(table, schema="trader")["constrained_columns"] == PRIMARY_KEYS[table]


def test_timestamps_are_timestamptz(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        types = conn.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'trader' AND table_name = ANY(:t) AND data_type LIKE 'timestamp%'"
            ),
            {"t": sorted(PHASE4_TABLES)},
        ).all()
    assert len(types) == TIMESTAMP_COLUMNS
    assert {t.data_type for t in types} == {"timestamp with time zone"}


def test_ids_are_identity_and_failed_logins_default_zero(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rows = {
            (r[0], r[1]): r[2]
            for r in conn.execute(
                text(
                    "SELECT table_name, column_name, "
                    "coalesce(is_identity, 'NO') || '|' || coalesce(column_default, '') "
                    "FROM information_schema.columns WHERE table_schema = 'trader' "
                    "AND table_name IN ('users', 'web_sessions') "
                    "AND column_name IN ('id', 'failed_logins')"
                )
            ).all()
        }
    assert rows[("users", "id")].startswith("YES|")
    assert rows[("web_sessions", "id")].startswith("YES|")
    assert rows[("users", "failed_logins")] == "NO|0"


def test_keys_foreign_key_and_index(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    users_unique = [u["column_names"] for u in insp.get_unique_constraints("users", schema="trader")]
    sessions_unique = [
        u["column_names"] for u in insp.get_unique_constraints("web_sessions", schema="trader")
    ]
    assert ["username"] in users_unique
    assert ["token_hash"] in sessions_unique
    fks = insp.get_foreign_keys("web_sessions", schema="trader")
    assert len(fks) == 1
    fk = fks[0]
    assert (fk["constrained_columns"], fk["referred_table"], fk["referred_columns"]) == (
        ["user_id"],
        "users",
        ["id"],
    )
    assert fk["referred_schema"] == "trader"
    assert fk["options"].get("ondelete") == "CASCADE"
    indexes = {i["name"]: i for i in insp.get_indexes("web_sessions", schema="trader")}
    assert indexes["ix_web_sessions_user_id"]["column_names"] == ["user_id"]
    assert not indexes["ix_web_sessions_user_id"]["unique"]
    # Operational tables: no run_id anywhere (like job_runs).
    for table in PHASE4_TABLES:
        assert "run_id" not in {c["name"] for c in insp.get_columns(table, schema="trader")}
    assert insp.get_foreign_keys("users", schema="trader") == []
    assert insp.get_foreign_keys("manual_watchlists", schema="trader") == []


def _user(name: str = "stephen") -> m.User:
    return m.User(
        username=name,
        password_hash="$argon2id$not-a-real-hash",
        created_at=T0,
        updated_at=T0,
        password_changed_at=T0,
    )


def _session(user_id: int, token_hash: str) -> m.WebSession:
    return m.WebSession(
        user_id=user_id,
        token_hash=token_hash,
        csrf_token="c" * 43,
        created_at=T0,
        last_seen_at=T0,
        expires_at=T0 + timedelta(days=30),
        ip="203.0.113.7",
        user_agent="pytest",
    )


def test_orm_round_trip_and_defaults(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        user = _user()
        s.add(user)
        s.flush()
        s.add(_session(user.id, "a" * 64))
        s.add(
            m.ManualWatchlist(
                session_date=date(2026, 10, 7),
                tickers=["AAPL", "BF.B"],
                filename="list.csv",
                uploaded_at=T0,
                uploaded_by="web:stephen",
            )
        )
        s.commit()
        got = s.execute(select(m.User)).scalar_one()
        sess = s.execute(select(m.WebSession)).scalar_one()
        wl = s.get(m.ManualWatchlist, date(2026, 10, 7))
    assert got.failed_logins == 0 and got.totp_secret_enc is None and got.locked_until is None
    assert sess.user_id == got.id and sess.revoked_at is None and sess.expires_at == T0 + timedelta(days=30)
    assert wl is not None and wl.tickers == ["AAPL", "BF.B"]


def test_username_is_unique(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(_user())
        s.commit()
        s.add(_user())
        with pytest.raises(IntegrityError, match="username"):
            s.commit()


def test_token_hash_is_unique(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        user = _user()
        s.add(user)
        s.flush()
        s.add(_session(user.id, "b" * 64))
        s.commit()
        s.add(_session(user.id, "b" * 64))
        with pytest.raises(IntegrityError, match="token_hash"):
            s.commit()


def test_deleting_a_user_deletes_its_sessions(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        user = _user()
        s.add(user)
        s.flush()
        s.add_all([_session(user.id, "c" * 64), _session(user.id, "d" * 64)])
        s.commit()
        uid = user.id
    with db_factory() as s:
        # The database cascades (ON DELETE CASCADE), not the ORM: delete with plain SQL.
        s.execute(text("DELETE FROM trader.users WHERE id = :id"), {"id": uid})
        s.commit()
        left = s.execute(text("SELECT count(*) FROM trader.web_sessions")).scalar_one()
    assert left == 0


def test_downgrade_to_0004_drops_phase4_tables_and_keeps_phase3(pg_url: str, migrated_engine: Engine) -> None:
    cfg = alembic_config(pg_url)
    command.downgrade(cfg, "0004")
    try:
        tables = set(inspect(migrated_engine).get_table_names(schema="trader"))
        with migrated_engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM trader.alembic_version")).scalar_one()
        assert version == "0004"
        assert not (PHASE4_TABLES & tables)
        assert PHASE3_TABLES <= tables
    finally:
        command.upgrade(cfg, "head")
    assert PHASE4_TABLES <= set(inspect(migrated_engine).get_table_names(schema="trader"))
