"""QUOTEBAR: the 9:35 opening bar from live quotes.

- `opening_bar_quotes`: the bar the 9:35 scan built from a live quote per session and symbol (the Questrade
  market-data package serves intraday candles only ~10 minutes late), with the volume on candle scale and
  the factor used; the ~09:47 shadow check writes the official candle beside it. NOT a candle table: replay
  and the candle archive never read it.
- `quote_volume_scale`: the candle/quote volume factor per session and symbol, measured after each close, that
  converts the next session's quote volume to candle scale.

Market data, not run-scoped (like `intraday_candles`). Additive: nothing existing changes. The app role gets
DML through the schema's default privileges, like every table.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

S = "trader"
TS = sa.DateTime(timezone=True)
MONEY = sa.Numeric(14, 4)
FACTOR = sa.Numeric(10, 6)


def upgrade() -> None:
    op.create_table(
        "opening_bar_quotes",
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("captured_at", TS, nullable=False),
        sa.Column("quote_time", TS),
        sa.Column("open", MONEY, nullable=False),
        sa.Column("high", MONEY, nullable=False),
        sa.Column("low", MONEY, nullable=False),
        sa.Column("close", MONEY, nullable=False),
        sa.Column("quote_volume", sa.BigInteger, nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
        sa.Column("vol_factor", FACTOR, nullable=False),
        sa.Column("factor_source", sa.String(10), nullable=False),
        sa.Column("checked_at", TS),
        sa.Column("check_status", sa.String(200)),
        sa.Column("official_open", MONEY),
        sa.Column("official_high", MONEY),
        sa.Column("official_low", MONEY),
        sa.Column("official_close", MONEY),
        sa.Column("official_volume", sa.BigInteger),
        sa.Column("decision_differs", sa.Boolean),
        schema=S,
    )
    op.create_table(
        "quote_volume_scale",
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("quote_volume", sa.BigInteger, nullable=False),
        sa.Column("candle_volume", sa.BigInteger),
        sa.Column("factor", FACTOR),
        sa.Column("recorded_at", TS, nullable=False),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("quote_volume_scale", schema=S)
    op.drop_table("opening_bar_quotes", schema=S)
