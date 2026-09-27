"""Broker hardening (P2-B1 fix round): exit_reason length, CHECK constraints, no TRUNCATE on the ledger.

- trades.exit_reason becomes varchar(100), the same as orders.reason it is copied from.
- CHECK constraints: orders.qty > 0, fills.qty > 0, fills.price > 0, positions.qty >= 0.
- cash_ledger also refuses TRUNCATE (a statement-level trigger on the same append-only function).

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

S = "trader"

CHECKS = (
    ("ck_orders_qty_positive", "orders", "qty > 0"),
    ("ck_fills_qty_positive", "fills", "qty > 0"),
    ("ck_fills_price_positive", "fills", "price > 0"),
    ("ck_positions_qty_nonnegative", "positions", "qty >= 0"),
)

NO_TRUNCATE_TRIGGER = f"""
CREATE TRIGGER cash_ledger_no_truncate BEFORE TRUNCATE ON {S}.cash_ledger
FOR EACH STATEMENT EXECUTE FUNCTION {S}.cash_ledger_append_only()
"""


def upgrade() -> None:
    op.alter_column(
        "trades",
        "exit_reason",
        type_=sa.String(100),
        existing_type=sa.String(50),
        existing_nullable=False,
        schema=S,
    )
    for name, table, condition in CHECKS:
        op.create_check_constraint(name, table, condition, schema=S)
    op.execute(NO_TRUNCATE_TRIGGER)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER cash_ledger_no_truncate ON {S}.cash_ledger")
    for name, table, _ in reversed(CHECKS):
        op.drop_constraint(name, table, type_="check", schema=S)
    op.alter_column(
        "trades",
        "exit_reason",
        type_=sa.String(50),
        existing_type=sa.String(100),
        existing_nullable=False,
        schema=S,
    )
