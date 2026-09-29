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


@pytest.fixture(autouse=True)
def _process_logging_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI commands and `python -m trader.worker` call `logging_setup.configure_logging` first (P3-T12).
    It configures the whole process once (a root handler on the stdout of that moment), so in tests it would
    leak into every later test and into CliRunner output. Tests that need it call the function they imported
    by name (tests/test_logging_setup.py), or patch this attribute with a recorder."""
    import trader.logging_setup

    monkeypatch.setattr(trader.logging_setup, "configure_logging", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _day_jobs_single_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """P5-T17: the day-level jobs (nightly, premarket, preopen, postclose, weekly) retry a failure in-process
    with real sleeps of minutes, taking their policy from `trader.runtime.RetryPolicy.from_settings`. Tests
    that make such a job fail would sleep through them, so by default that name gives one attempt (the P3
    behaviour those tests pin). Tests of the wiring restore the real class (tests/test_cli_phase5.py); the
    retry mechanics are tested on `trader.jobs.runner` directly (tests/jobs/test_runner_retry.py)."""
    from datetime import datetime

    import trader.runtime
    from trader.jobs.runner import RetryPolicy
    from trader.settings_store import RuntimeSettings

    class SingleAttempt(RetryPolicy):
        @classmethod
        def from_settings(cls, s: RuntimeSettings, *, deadline: datetime | None = None) -> RetryPolicy:
            return RetryPolicy(attempts=1, deadline=deadline)

    monkeypatch.setattr(trader.runtime, "RetryPolicy", SingleAttempt)


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """One throwaway cluster per test process. Under pytest-xdist (scripts/check.sh runs `-n 4`) every worker
    is its own pytest session, so each worker starts its own container: roles, databases, advisory locks and
    pg_terminate_backend stay private to that worker, exactly as in a serial run.

    Durability is switched off (fsync, synchronous_commit, full_page_writes): it only matters if the container
    itself crashes, which no test does (the crash tests kill the worker process, not PostgreSQL). Visibility,
    locking, isolation and triggers are unchanged. Tests that pin server settings use their own containers
    (tests/integration/test_prod_db_setup.py, tests/gauntlet/test_p6_t6_breaker.py)."""
    container = PostgresContainer("postgres:14-alpine", driver="psycopg").with_command(
        "postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    )
    with container as pg:
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
