"""FIX-DAY1: the timed quote captures of the opening bar, and the pre-market-free opening volume.

- `opening_quote_captures`: the raw quotes of the two timed captures per session and symbol: `open` (the
  volume at the open, read just before 09:30:00 ET) and `bar` (read from 09:35:00.0 ET), with the capture's
  start and end and when each quote arrived. The 9:35:05 ORB event builds its bars from the stored `bar`
  capture; the opening volume is the `bar` volume minus the `open` volume (the quote's day volume includes
  pre-market trades: CLDX Wed 09-30, 339,533 at 09:35 against an official 35,628).
- `opening_bar_quotes`: + `open_volume` (the `open` capture's volume used), `volume_basis` (`delta`),
  `capture_started_at`, `capture_ended_at` (NULL: the scan's own quotes pass, not a timed capture).
- `quote_volume_scale`: + `premarket_volume`, `premarket_source` (`snapshot` | `candles`): the factor is
  measured on regular-session volume only.

Market data, not run-scoped. Additive: new nullable columns, one new table.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

S = "trader"
TS = sa.DateTime(timezone=True)
MONEY = sa.Numeric(14, 4)


def upgrade() -> None:
    op.create_table(
        "opening_quote_captures",
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("kind", sa.String(10), primary_key=True),
        sa.Column("capture_started_at", TS, nullable=False),
        sa.Column("capture_ended_at", TS, nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("quote_time", TS),
        sa.Column("open", MONEY),
        sa.Column("high", MONEY),
        sa.Column("low", MONEY),
        sa.Column("last", MONEY),
        sa.Column("last_regular", MONEY),
        sa.Column("volume", sa.BigInteger, nullable=False),
        sa.Column("delay", sa.Integer),
        sa.CheckConstraint("kind IN ('open', 'bar')", name="ck_opening_quote_captures_kind"),
        schema=S,
    )
    op.add_column("opening_bar_quotes", sa.Column("open_volume", sa.BigInteger), schema=S)
    op.add_column("opening_bar_quotes", sa.Column("volume_basis", sa.String(20)), schema=S)
    op.add_column("opening_bar_quotes", sa.Column("capture_started_at", TS), schema=S)
    op.add_column("opening_bar_quotes", sa.Column("capture_ended_at", TS), schema=S)
    op.add_column("quote_volume_scale", sa.Column("premarket_volume", sa.BigInteger), schema=S)
    op.add_column("quote_volume_scale", sa.Column("premarket_source", sa.String(10)), schema=S)


def downgrade() -> None:
    op.drop_column("quote_volume_scale", "premarket_source", schema=S)
    op.drop_column("quote_volume_scale", "premarket_volume", schema=S)
    op.drop_column("opening_bar_quotes", "capture_ended_at", schema=S)
    op.drop_column("opening_bar_quotes", "capture_started_at", schema=S)
    op.drop_column("opening_bar_quotes", "volume_basis", schema=S)
    op.drop_column("opening_bar_quotes", "open_volume", schema=S)
    op.drop_table("opening_quote_captures", schema=S)
