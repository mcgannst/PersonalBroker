"""Phase 5: replay run state, replay-scoped strategy configs and the weekly reports table (SPEC §8, §10).

- `runs` gains the replay lifecycle columns (`finished_at`, `updated_at`, `progress`, `error`,
  `cancel_requested`) and an index on (mode, status). The live run keeps them null / false.
- `strategy_configs` gains `scope` (`live` | `replay`). A replay's parameter override is written as a
  `replay` row with the base live revision, so the unique (strategy_key, revision) constraint becomes a
  partial unique index over `live` rows only.
- `weekly_reports`: one row per Monday-Friday week, keyed by the week's last session date.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

S = "trader"
TS = sa.DateTime(timezone=True)
RUN_COLUMNS = ("finished_at", "updated_at", "progress", "error", "cancel_requested")
DOWNGRADE_REFUSED = (
    "cannot downgrade 0006: trader.strategy_configs has replay-scoped rows; delete the replay runs first"
)


def upgrade() -> None:
    op.add_column("runs", sa.Column("finished_at", TS), schema=S)
    op.add_column("runs", sa.Column("updated_at", TS), schema=S)
    op.add_column("runs", sa.Column("progress", JSONB), schema=S)
    op.add_column("runs", sa.Column("error", sa.Text), schema=S)
    op.add_column(
        "runs",
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.text("false")),
        schema=S,
    )
    op.create_index("ix_runs_mode_status", "runs", ["mode", "status"], schema=S)

    op.add_column(
        "strategy_configs",
        sa.Column("scope", sa.String(10), nullable=False, server_default=sa.text("'live'")),
        schema=S,
    )
    op.create_check_constraint(
        "ck_strategy_configs_scope", "strategy_configs", "scope IN ('live', 'replay')", schema=S
    )
    op.drop_constraint("uq_strategy_configs_key_revision", "strategy_configs", type_="unique", schema=S)
    op.create_index(
        "uq_strategy_configs_key_revision_live",
        "strategy_configs",
        ["strategy_key", "revision"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("scope = 'live'"),
    )

    op.create_table(
        "weekly_reports",
        sa.Column("week_ending", sa.Date, primary_key=True),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("run_id", sa.BigInteger, sa.ForeignKey(f"{S}.runs.id"), nullable=False),
        sa.Column("facts", JSONB, nullable=False),
        sa.Column("commentary", sa.Text),
        sa.Column("commentary_status", sa.String(20), nullable=False),
        sa.Column("commentary_error", sa.Text),
        sa.Column("model", sa.String(60)),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        schema=S,
    )


def downgrade() -> None:
    replay_rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM trader.strategy_configs WHERE scope = 'replay'"))
        .scalar_one()
    )
    if replay_rows:
        raise RuntimeError(DOWNGRADE_REFUSED)
    op.drop_table("weekly_reports", schema=S)
    op.drop_index("uq_strategy_configs_key_revision_live", table_name="strategy_configs", schema=S)
    op.create_unique_constraint(
        "uq_strategy_configs_key_revision", "strategy_configs", ["strategy_key", "revision"], schema=S
    )
    op.drop_constraint("ck_strategy_configs_scope", "strategy_configs", type_="check", schema=S)
    op.drop_column("strategy_configs", "scope", schema=S)
    op.drop_index("ix_runs_mode_status", table_name="runs", schema=S)
    for column in reversed(RUN_COLUMNS):
        op.drop_column("runs", column, schema=S)
