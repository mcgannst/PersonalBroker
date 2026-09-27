"""Phase 4 web tables: the web user, server-side web sessions, and manual watchlists (SPEC §4.2, §10, §14).

Operational tables, so they carry no run_id (like job_runs). The app role's default privileges on
trader_dev cover new tables.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

S = "trader"
TS = sa.DateTime(timezone=True)
TABLES = ("users", "web_sessions", "manual_watchlists")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("totp_secret_enc", sa.Text),
        sa.Column("totp_pending_enc", sa.Text),
        sa.Column("totp_last_step", sa.BigInteger),
        sa.Column("failed_logins", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("password_changed_at", TS, nullable=False),
        sa.Column("last_login_at", TS),
        schema=S,
    )
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey(f"{S}.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("ip", sa.String(45)),
        sa.Column("user_agent", sa.String(200)),
        schema=S,
    )
    op.create_index("ix_web_sessions_user_id", "web_sessions", ["user_id"], schema=S)
    op.create_table(
        "manual_watchlists",
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("tickers", JSONB, nullable=False),
        sa.Column("filename", sa.String(200)),
        sa.Column("uploaded_at", TS, nullable=False),
        sa.Column("uploaded_by", sa.String(50), nullable=False),
        schema=S,
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table, schema=S)  # drops their indexes and constraints too
