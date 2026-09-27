"""Phase 1 core tables.

Revision ID: 0001
Revises:
"""

from datetime import date
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

S = "trader"
MONEY = sa.Numeric(14, 4)
TS = sa.DateTime(timezone=True)
FIRST_PARTITION = date(2026, 6, 1)
LAST_PARTITION = date(2028, 12, 1)


def _ohlcv() -> list[sa.Column[Any]]:
    return [
        sa.Column("open", MONEY, nullable=False),
        sa.Column("high", MONEY, nullable=False),
        sa.Column("low", MONEY, nullable=False),
        sa.Column("close", MONEY, nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
        sa.Column("vwap", MONEY),
    ]


def _months(first: date, last: date) -> list[date]:
    out, d = [], first
    while d <= last:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.String(50), nullable=False),
        schema=S,
    )
    op.create_table(
        "api_credentials",
        sa.Column("provider", sa.String(30), primary_key=True),
        sa.Column("refresh_token_enc", sa.Text),
        sa.Column("access_token_enc", sa.Text),
        sa.Column("api_server", sa.String(200)),
        sa.Column("expires_at", TS),
        sa.Column("last_refresh_at", TS),
        sa.Column("last_error", sa.Text),
        sa.Column("updated_at", TS),
        schema=S,
    )
    op.create_table(
        "symbols",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("questrade_id", sa.BigInteger, unique=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("name", sa.String(200)),
        sa.UniqueConstraint("ticker", "exchange"),
        schema=S,
    )
    sym_fk = sa.ForeignKey(f"{S}.symbols.id")
    op.create_table(
        "universe_snapshots",
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("symbol_id", sa.BigInteger, sym_fk, primary_key=True),
        sa.Column("price", MONEY),
        sa.Column("avg_volume", sa.BigInteger),
        sa.Column("atr14", MONEY),
        sa.Column("source", sa.String(20), nullable=False),
        schema=S,
    )
    op.create_table(
        "daily_candles",
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        *_ohlcv(),
        schema=S,
    )
    op.execute(f"""
        CREATE TABLE {S}.intraday_candles (
            symbol_id bigint NOT NULL,
            interval varchar(3) NOT NULL,
            ts timestamptz NOT NULL,
            open numeric(14,4) NOT NULL, high numeric(14,4) NOT NULL,
            low numeric(14,4) NOT NULL, close numeric(14,4) NOT NULL,
            volume bigint NOT NULL, vwap numeric(14,4),
            PRIMARY KEY (symbol_id, interval, ts)
        ) PARTITION BY RANGE (ts)
    """)
    months = _months(FIRST_PARTITION, LAST_PARTITION)
    for start in months:
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        op.execute(
            f"CREATE TABLE {S}.intraday_candles_{start:%Y%m} PARTITION OF {S}.intraday_candles "
            f"FOR VALUES FROM ('{start:%Y-%m-%d}') TO ('{end:%Y-%m-%d}')"
        )
    op.execute(f"CREATE TABLE {S}.intraday_candles_default PARTITION OF {S}.intraday_candles DEFAULT")
    op.create_table(
        "candle_archive",
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("interval", sa.String(3), primary_key=True),
        sa.Column("start_ts", TS, primary_key=True),
        *_ohlcv(),
        schema=S,
    )
    op.create_table(
        "open_bar_stats",
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("avg_open_vol_14d", sa.Numeric(18, 2)),
        sa.Column("atr14", MONEY),
        schema=S,
    )
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job", sa.String(50), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("detail", JSONB),
        schema=S,
    )
    op.create_index("ix_job_runs_job_session", "job_runs", ["job", "session_date"], schema=S)
    op.create_table(
        "event_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("run_id", sa.BigInteger),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("data", JSONB),
        schema=S,
    )
    op.create_index("ix_event_log_ts", "event_log", ["ts"], schema=S)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("actor", sa.String(50), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("before", JSONB),
        sa.Column("after", JSONB),
        schema=S,
    )


def downgrade() -> None:
    for table in ("audit_log", "event_log", "job_runs", "open_bar_stats", "candle_archive"):
        op.drop_table(table, schema=S)
    op.execute(f"DROP TABLE {S}.intraday_candles CASCADE")
    for table in ("daily_candles", "universe_snapshots", "symbols", "api_credentials", "settings"):
        op.drop_table(table, schema=S)
