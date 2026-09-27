"""Alembic environment. Runs as the owner role (MIGRATION_DATABASE_URL), or the URL tests set."""

import os

from alembic import context
from sqlalchemy import create_engine, text

from trader.db.models import SCHEMA, Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ["MIGRATION_DATABASE_URL"]

engine = create_engine(url)
with engine.connect() as connection:
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
