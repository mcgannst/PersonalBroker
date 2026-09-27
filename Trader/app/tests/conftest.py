"""Shared fixtures. Database tests use a throwaway PostgreSQL 14 container, never trader_dev."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer

from trader.db.models import Base
from trader.db.session import make_engine, make_session_factory

APP_DIR = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> Config:
    cfg = Config(str(APP_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(APP_DIR / "trader" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:14-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_engine(pg_url: str) -> Iterator[Engine]:
    from alembic import command

    command.upgrade(alembic_config(pg_url), "head")
    engine = make_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db_factory(migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield make_session_factory(migrated_engine)
    names = ", ".join(t.fullname for t in Base.metadata.sorted_tables)
    with migrated_engine.begin() as conn:
        # cash_ledger refuses TRUNCATE (trigger cash_ledger_no_truncate, migration 0003); replica mode
        # skips triggers for this cleanup transaction only (the test container user is a superuser).
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
