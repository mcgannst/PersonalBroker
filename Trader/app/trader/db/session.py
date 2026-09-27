from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

UTC_SESSION = {"options": "-c timezone=UTC"}


def make_engine(url: str) -> Engine:
    """Every session runs with TimeZone=UTC, whatever the server's default, so timestamptz values
    come back in UTC and bare timestamp literals are read as UTC.

    hide_parameters keeps bound values (encrypted tokens, API keys) out of DB error messages
    and SQL logs."""
    return create_engine(url, pool_pre_ping=True, connect_args=UTC_SESSION, hide_parameters=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
