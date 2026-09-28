"""Phase 6 amendment: the decision log (SPEC §10 `decision_log`, P6-T9).

A derived journal of every trading decision of a run's session, rebuilt by the recorder (P6-T10) from the rows
the engine already writes; `seq` orders a day's rows. The CHECK lists equal `trader.decisions.types`
`DecisionStage` and `DecisionOutcome` (tests/db/test_migration_0007.py reads both). The app role gets DML
through the schema's default privileges, like every table.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

S = "trader"
TS = sa.DateTime(timezone=True)

STAGES = (
    "universe",
    "premarket",
    "scan",
    "signal",
    "risk",
    "proposal",
    "approval",
    "order",
    "fill",
    "exit",
    "overlay",
    "kill_switch",
    "day",
)
OUTCOMES = (
    "info",
    "listed",
    "classified",
    "passed",
    "rejected",
    "proposed",
    "approved",
    "auto_approved",
    "declined",
    "expired",
    "blocked",
    "submitted",
    "filled",
    "cancelled",
    "exited",
    "tripped",
    "reset",
    "error",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "decision_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.BigInteger, sa.ForeignKey(f"{S}.runs.id"), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("strategy_key", sa.String(50)),
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id")),
        sa.Column("ticker", sa.String(20)),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("rule", sa.String(60)),
        sa.Column("reason", sa.Text),
        sa.Column("ts", TS, nullable=False),
        sa.Column("ref", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("final", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("run_id", "session_date", "seq", name="uq_decision_log_run_day_seq"),
        sa.CheckConstraint(_in("stage", STAGES), name="ck_decision_log_stage"),
        sa.CheckConstraint(_in("outcome", OUTCOMES), name="ck_decision_log_outcome"),
        schema=S,
    )
    op.create_index(
        "ix_decision_log_run_day_stage", "decision_log", ["run_id", "session_date", "stage"], schema=S
    )
    op.create_index("ix_decision_log_day", "decision_log", ["session_date"], schema=S)


def downgrade() -> None:
    op.drop_table("decision_log", schema=S)
