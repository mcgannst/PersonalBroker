"""Phase 3 operational tables: worker heartbeat, Telegram callbacks, sent notifications, relay cursors.

They are not trading data, so they carry no run_id (like job_runs).

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

S = "trader"
TS = sa.DateTime(timezone=True)
TABLES = ("worker_heartbeats", "telegram_callbacks", "notifications", "notify_cursors")


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("process", sa.String(30), primary_key=True),
        sa.Column("pid", sa.Integer, nullable=False),
        sa.Column("host", sa.String(100), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("beat_at", TS, nullable=False),
        sa.Column("session_date", sa.Date),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("detail", JSONB),
        schema=S,
    )
    op.create_table(
        "telegram_callbacks",
        sa.Column("nonce", sa.String(16), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("ref", sa.String(50), nullable=False),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS),
        sa.Column("used_at", TS),
        sa.Column("used_action", sa.String(20)),
        schema=S,
    )
    op.create_index("ix_telegram_callbacks_kind_ref", "telegram_callbacks", ["kind", "ref"], schema=S)
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("dedupe_key", sa.String(200)),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("sent_at", TS),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("message_ids", JSONB),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text),
        schema=S,
    )
    # Unique where not NULL: PostgreSQL treats NULLs as distinct, so any number of rows may have no key.
    op.create_index("uq_notifications_dedupe_key", "notifications", ["dedupe_key"], unique=True, schema=S)
    op.create_table(
        "notify_cursors",
        sa.Column("stream", sa.String(30), primary_key=True),
        sa.Column("last_id", sa.BigInteger, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        schema=S,
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table, schema=S)  # drops their indexes too
