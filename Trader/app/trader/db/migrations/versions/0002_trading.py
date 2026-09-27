"""Phase 2 trading tables, views and the cash-ledger append-only trigger.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

S = "trader"
MONEY = sa.Numeric(14, 4)
TS = sa.DateTime(timezone=True)


def _fk(table: str) -> sa.ForeignKey:
    return sa.ForeignKey(f"{S}.{table}.id")


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _run_id(unique: bool = False) -> sa.Column[int]:
    return sa.Column("run_id", sa.BigInteger, _fk("runs"), nullable=False, unique=unique)


V_TRADE_METRICS = """
CREATE VIEW trader.v_trade_metrics AS
WITH t AS (
    SELECT run_id,
           count(*) AS trades,
           count(*) FILTER (WHERE pnl > 0) AS wins,
           avg(pnl_r) FILTER (WHERE pnl > 0) AS avg_win_r,
           avg(pnl_r) FILTER (WHERE pnl <= 0) AS avg_loss_r,
           avg(pnl_r) AS expectancy_r,
           sum(pnl) FILTER (WHERE pnl > 0) AS gross_win,
           -sum(pnl) FILTER (WHERE pnl < 0) AS gross_loss,
           avg(slippage_total) AS avg_slippage
    FROM trader.trades
    GROUP BY run_id
), d AS (
    SELECT run_id, max(drawdown_pct) AS max_drawdown_pct FROM trader.equity_snapshots GROUP BY run_id
), j AS (
    SELECT run_id,
           count(*) FILTER (WHERE rules_followed) AS followed,
           count(rules_followed) AS answered
    FROM trader.journal
    GROUP BY run_id
)
SELECT r.id AS run_id,
       coalesce(t.trades, 0) AS trades,
       coalesce(t.wins, 0) AS wins,
       CASE WHEN t.trades > 0 THEN round(t.wins::numeric / t.trades, 4) END AS win_rate,
       round(t.avg_win_r, 4) AS avg_win_r,
       round(t.avg_loss_r, 4) AS avg_loss_r,
       round(t.expectancy_r, 4) AS expectancy_r,
       CASE WHEN t.gross_loss > 0 THEN round(coalesce(t.gross_win, 0) / t.gross_loss, 4) END AS profit_factor,
       round(t.avg_slippage, 4) AS avg_slippage,
       d.max_drawdown_pct,
       CASE WHEN j.answered > 0 THEN round(j.followed::numeric / j.answered, 4) END AS adherence_pct
FROM trader.runs r
LEFT JOIN t ON t.run_id = r.id
LEFT JOIN d ON d.run_id = r.id
LEFT JOIN j ON j.run_id = r.id
"""

V_DAILY_PNL = """
CREATE VIEW trader.v_daily_pnl AS
SELECT run_id, session_date, count(*) AS trades, sum(pnl) AS realized_pnl, sum(fees_total) AS fees
FROM trader.trades
GROUP BY run_id, session_date
"""

APPEND_ONLY_FN = f"""
CREATE FUNCTION {S}.cash_ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'trader.cash_ledger is append-only (% refused)', TG_OP;
END
$$
"""

APPEND_ONLY_TRIGGER = f"""
CREATE TRIGGER cash_ledger_append_only BEFORE UPDATE OR DELETE ON {S}.cash_ledger
FOR EACH ROW EXECUTE FUNCTION {S}.cash_ledger_append_only()
"""


def upgrade() -> None:
    op.create_table(
        "runs",
        _id(),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("label", sa.String(200)),
        schema=S,
    )
    op.create_index(
        "uq_runs_one_active_live",
        "runs",
        ["mode"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("mode = 'live' AND status = 'active'"),
    )
    op.create_table(
        "sim_accounts",
        _id(),
        _run_id(unique=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("starting_cash", MONEY, nullable=False),
        sa.Column("source_amount", MONEY, nullable=False),
        sa.Column("source_currency", sa.String(3), nullable=False),
        sa.Column("fx_rate", sa.Numeric(12, 6)),
        sa.Column("fx_fee", sa.Numeric(6, 4)),
        sa.Column("created_at", TS, nullable=False),
        schema=S,
    )
    op.create_table(
        "strategy_configs",
        _id(),
        sa.Column("strategy_key", sa.String(50), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("created_by", sa.String(50), nullable=False),
        sa.UniqueConstraint("strategy_key", "revision", name="uq_strategy_configs_key_revision"),
        schema=S,
    )
    op.create_table(
        "catalysts",
        _id(),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("headlines", JSONB, nullable=False),
        sa.Column("gap_pct", sa.Numeric(10, 4)),
        sa.Column("earnings_date", sa.Date),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("quality", sa.Integer),
        sa.Column("confirmed", sa.Boolean),
        sa.Column("reason", sa.Text),
        sa.Column("model", sa.String(60)),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("classified_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("symbol_id", "session_date", name="uq_catalysts_symbol_session"),
        schema=S,
    )
    op.create_table(
        "candidates",
        _id(),
        _run_id(),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("strategy_key", sa.String(50), nullable=False),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("rvol", sa.Numeric(12, 4)),
        sa.Column("rank", sa.Integer),
        sa.Column("candle", JSONB),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("reject_reason", sa.String(100)),
        sa.Column("data", JSONB),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "session_date",
            "strategy_key",
            "symbol_id",
            name="uq_candidates_run_session_strategy_symbol",
        ),
        schema=S,
    )
    op.create_table(
        "signals",
        _id(),
        _run_id(),
        sa.Column("strategy_config_id", sa.BigInteger, _fk("strategy_configs"), nullable=False),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols")),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("event_key", sa.String(50), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("intent", JSONB, nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        schema=S,
    )
    op.create_index("ix_signals_run_session", "signals", ["run_id", "session_date"], schema=S)
    op.create_table(
        "proposals",
        _id(),
        _run_id(),
        sa.Column("signal_id", sa.BigInteger, _fk("signals"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("order_spec", JSONB, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("decided_at", TS),
        sa.Column("decided_via", sa.String(10)),
        sa.Column("decided_by", sa.String(50)),
        sa.Column("decision_latency_ms", sa.BigInteger),
        sa.Column("position_id", sa.BigInteger),
        sa.Column("cancel_order_id", sa.BigInteger),
        sa.Column("order_id", sa.BigInteger),
        sa.Column("sizing", JSONB),
        sa.Column("expired_at", TS),
        sa.Column("escalated_at", TS),
        sa.Column("escalations", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
        schema=S,
    )
    op.create_index("ix_proposals_run_status", "proposals", ["run_id", "status"], schema=S)
    op.create_table(
        "orders",
        _id(),
        _run_id(),
        sa.Column("proposal_id", sa.BigInteger, _fk("proposals")),
        sa.Column("position_id", sa.BigInteger),
        sa.Column("strategy_config_id", sa.BigInteger, _fk("strategy_configs")),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("order_type", sa.String(12), nullable=False),
        sa.Column("purpose", sa.String(10), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("stop_price", MONEY),
        sa.Column("limit_price", MONEY),
        sa.Column("stop_loss", MONEY),
        sa.Column("tif", sa.String(3), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("submitted_at", TS, nullable=False),
        sa.Column("closed_at", TS),
        sa.Column("cancel_reason", sa.String(100)),
        sa.Column("stale_since", TS),
        sa.Column("stale_alerted", sa.Boolean, nullable=False),
        schema=S,
    )
    op.create_index("ix_orders_run_status", "orders", ["run_id", "status"], schema=S)
    op.create_table(
        "fills",
        _id(),
        _run_id(),
        sa.Column("order_id", sa.BigInteger, _fk("orders"), nullable=False, unique=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("fees", JSONB, nullable=False),
        sa.Column("quote_snapshot", JSONB, nullable=False),
        sa.Column("slippage", MONEY, nullable=False),
        schema=S,
    )
    op.create_table(
        "positions",
        _id(),
        _run_id(),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("strategy_config_id", sa.BigInteger, _fk("strategy_configs")),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("avg_price", MONEY, nullable=False),
        sa.Column("stop_loss", MONEY),
        sa.Column("planned_risk", MONEY),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("closed_at", TS),
        sa.Column("entry_order_id", sa.BigInteger, nullable=False),
        sa.Column("stop_order_id", sa.BigInteger),
        sa.Column("unprotected_since", TS),
        sa.Column("unprotected_seconds", sa.Integer, nullable=False),
        schema=S,
    )
    op.create_index("ix_positions_run_closed", "positions", ["run_id", "closed_at"], schema=S)
    op.create_table(
        "trades",
        _id(),
        _run_id(),
        sa.Column("position_id", sa.BigInteger, _fk("positions"), nullable=False, unique=True),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("entry_price", MONEY, nullable=False),
        sa.Column("exit_price", MONEY, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("pnl", MONEY, nullable=False),
        sa.Column("pnl_r", sa.Numeric(10, 4)),
        sa.Column("planned_risk", MONEY),
        sa.Column("exit_reason", sa.String(50), nullable=False),
        sa.Column("slippage_total", MONEY, nullable=False),
        sa.Column("fees_total", MONEY, nullable=False),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("closed_at", TS, nullable=False),
        schema=S,
    )
    op.create_table(
        "cash_ledger",
        _id(),
        _run_id(),
        sa.Column("ts", TS, nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("settle_date", sa.Date, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("ref", sa.String(100), nullable=False),
        schema=S,
    )
    op.create_index("ix_cash_ledger_run_settle", "cash_ledger", ["run_id", "settle_date"], schema=S)
    op.execute(APPEND_ONLY_FN)
    op.execute(APPEND_ONLY_TRIGGER)
    op.create_table(
        "equity_snapshots",
        sa.Column("run_id", sa.BigInteger, _fk("runs"), primary_key=True),
        sa.Column("ts", TS, primary_key=True),
        sa.Column("equity", MONEY, nullable=False),
        sa.Column("cash", MONEY, nullable=False),
        sa.Column("settled_cash", MONEY, nullable=False),
        sa.Column("peak_equity", MONEY, nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(8, 4), nullable=False),
        schema=S,
    )
    op.create_table(
        "journal",
        sa.Column("run_id", sa.BigInteger, _fk("runs"), primary_key=True),
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("rules_followed", sa.Boolean),
        sa.Column("notes", sa.Text),
        sa.Column("answered_via", sa.String(20)),
        sa.Column("updated_at", TS),
        schema=S,
    )
    op.create_table(
        "kill_switch_events",
        _id(),
        _run_id(),
        sa.Column("switch", sa.String(30), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("tripped_at", TS, nullable=False),
        sa.Column("value", sa.Numeric(14, 6)),
        sa.Column("threshold", sa.Numeric(14, 6)),
        sa.Column("reset_at", TS),
        sa.Column("reset_reason", sa.Text),
        sa.Column("reset_by", sa.String(50)),
        schema=S,
    )
    op.create_index("ix_kill_switch_events_run", "kill_switch_events", ["run_id", "switch"], schema=S)
    op.execute(V_TRADE_METRICS)
    op.execute(V_DAILY_PNL)


def downgrade() -> None:
    op.execute(f"DROP VIEW {S}.v_daily_pnl")
    op.execute(f"DROP VIEW {S}.v_trade_metrics")
    for table in (
        "kill_switch_events",
        "journal",
        "equity_snapshots",
        "cash_ledger",
        "trades",
        "positions",
        "fills",
        "orders",
        "proposals",
        "signals",
        "candidates",
        "catalysts",
        "strategy_configs",
        "sim_accounts",
        "runs",
    ):
        op.drop_table(table, schema=S)  # drops their indexes and the cash_ledger trigger too
    op.execute(f"DROP FUNCTION {S}.cash_ledger_append_only()")
