"""Live dashboard: the worker's quote marks and the 1-minute bars built from them (DB-T1, live dashboard plan
S1/S2).

- `quote_marks`: the latest quote the worker's mark publisher observed per run and symbol (upserted).
- `mark_bars`: 1-minute OHLC bars of observed prices, per run, symbol and minute. They are NOT candles: only
  the publisher writes them and only the API reads them (never a strategy, report, replay or the archive).

Both are run-scoped like every trading table (SPEC §3a), so a later live run starts clean; a replay never
writes them. Additive: nothing existing changes. The app role gets DML through the schema's default
privileges, like every table.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

S = "trader"
TS = sa.DateTime(timezone=True)
MONEY = sa.Numeric(14, 4)

BAR_OHLC_CHECK = "low <= open AND low <= close AND high >= open AND high >= close AND low > 0"
BAR_SAMPLES_CHECK = "samples > 0"


def upgrade() -> None:
    op.create_table(
        "quote_marks",
        sa.Column("run_id", sa.BigInteger, sa.ForeignKey(f"{S}.runs.id"), primary_key=True),
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("bid", MONEY),
        sa.Column("ask", MONEY),
        sa.Column("last", MONEY),
        sa.Column("quote_time", TS),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("written_at", TS, nullable=False),
        sa.Column("is_halted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        schema=S,
    )
    op.create_table(
        "mark_bars",
        sa.Column("run_id", sa.BigInteger, sa.ForeignKey(f"{S}.runs.id"), primary_key=True),
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("minute_start", TS, primary_key=True),
        sa.Column("open", MONEY, nullable=False),
        sa.Column("high", MONEY, nullable=False),
        sa.Column("low", MONEY, nullable=False),
        sa.Column("close", MONEY, nullable=False),
        sa.Column("samples", sa.Integer, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(BAR_OHLC_CHECK, name="ck_mark_bars_ohlc"),
        sa.CheckConstraint(BAR_SAMPLES_CHECK, name="ck_mark_bars_samples"),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("mark_bars", schema=S)
    op.drop_table("quote_marks", schema=S)
