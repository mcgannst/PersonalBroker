"""Alembic environment. Runs as the owner role (MIGRATION_DATABASE_URL), or the URL tests set."""

import os
import re
from typing import Any

from alembic import context
from sqlalchemy import create_engine, text

from trader.db.models import SCHEMA, Base

# Like make_engine, the session is pinned to UTC. search_path is pinned to `public` as well: the owner
# role on trader_dev has `trader, public`, and with `trader` on the search_path PostgreSQL reflects
# trader's tables and foreign keys without their schema, so autogenerate would see phantom differences.
# Every migration names the schema explicitly, so nothing relies on the search_path.
CONNECT_ARGS = {"options": "-c timezone=UTC -c search_path=public"}

# Monthly and default partitions of intraday_candles are created by migrations with raw DDL and have no
# model. Autogenerate must never see them, or it would propose dropping them.
PARTITION = re.compile(r"^intraday_candles_(\d{6}|default)$")


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    """Autogenerate compares only schema `trader`, without the intraday partitions."""
    if type_ == "schema":
        return name == SCHEMA
    if type_ == "table":
        return not PARTITION.match(name or "")
    return True


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table":
        return obj.schema == SCHEMA and not PARTITION.match(name or "")
    return True


config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ.get("MIGRATION_DATABASE_URL")
if not url:
    raise RuntimeError(
        "No database URL for migrations: set MIGRATION_DATABASE_URL (the owner role's URL) "
        "or sqlalchemy.url in the Alembic config"
    )

engine = create_engine(url, connect_args=CONNECT_ARGS)
try:
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema=SCHEMA,
            include_schemas=True,
            include_name=include_name,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
