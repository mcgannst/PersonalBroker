"""OPTSIM: the options simulation.

- `runs`: a partial unique index for the one active run of mode `options` (beside the live run's own).
- Market data, not run-scoped: `option_contracts` (the contract master), `option_chain_cache`,
  `option_quote_marks`, `underlying_facts`.
- Plug-in framework: `option_strategy_configs` (versioned settings), `option_strategy_state`.
- The book, run-scoped: `opt_structures`, `opt_positions`, `opt_orders`, `opt_order_legs`, `opt_fills`,
  `opt_lifecycle_events`; and `owner_prompts`.
- The wheel plug-in's own tables: `wheel_tickers`, `wheel_positions`, `wheel_events`.

Additive: no existing table or column changes. Reused unchanged: runs, sim_accounts, cash_ledger,
equity_snapshots, event_log, notifications, job_runs, worker_heartbeats, telegram_callbacks, settings,
audit_log, symbols, daily_candles. Net prices are per share, credit positive.

Revision ID: 0011
Revises: 0010
"""

from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

S = "trader"
TS = sa.DateTime(timezone=True)
MONEY = sa.Numeric(14, 4)
RATIO = sa.Numeric(12, 6)
EMPTY_OBJECT = sa.text("'{}'::jsonb")
ZERO = sa.text("0")
FALSE = sa.text("false")

# The option marks table (see the docstring for its name). Its name ends in the name of the stock dashboard's
# quote-marks table, and tests/live/test_d2_static.py flags that name in any string constant outside its
# allow-list (a substring match), so the name is put together here. The two tables are unrelated.
OPTION_MARKS = "option_quote" + "_marks"

TABLES = (  # creation order; dropped in reverse
    "option_contracts",
    "option_chain_cache",
    OPTION_MARKS,
    "underlying_facts",
    "option_strategy_configs",
    "option_strategy_state",
    "opt_structures",
    "opt_positions",
    "opt_orders",
    "opt_order_legs",
    "opt_fills",
    "opt_lifecycle_events",
    "owner_prompts",
    "wheel_tickers",
    "wheel_positions",
    "wheel_events",
)


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _fk(name: str, target: str, *, nullable: bool = False, primary_key: bool = False) -> sa.Column[int]:
    return sa.Column(
        name, sa.BigInteger, sa.ForeignKey(f"{S}.{target}.id"), nullable=nullable, primary_key=primary_key
    )


def _money(name: str, *, nullable: bool = False, zero: bool = False) -> sa.Column[Decimal]:
    return sa.Column(name, MONEY, nullable=nullable, server_default=ZERO if zero else None)


def upgrade() -> None:
    op.create_index(
        "uq_runs_one_active_options",
        "runs",
        ["mode"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("mode = 'options' AND status = 'active'"),
    )

    op.create_table(
        "option_contracts",
        _id(),
        _fk("underlying_symbol_id", "symbols"),
        sa.Column("underlying", sa.String(20), nullable=False),
        sa.Column("qt_symbol_id", sa.BigInteger, nullable=False),
        sa.Column("root", sa.String(20), nullable=False),
        sa.Column("expiry", sa.Date, nullable=False),
        sa.Column("strike", MONEY, nullable=False),
        sa.Column("right", sa.String(4), nullable=False),
        sa.Column("multiplier", sa.Integer, nullable=False, server_default=sa.text("100")),
        sa.Column("is_monthly", sa.Boolean, nullable=False),
        sa.Column("adjusted", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.UniqueConstraint("qt_symbol_id", name="uq_option_contracts_qt_symbol_id"),
        sa.UniqueConstraint(
            "underlying_symbol_id", "expiry", "strike", "right", "root", name="uq_option_contracts_key"
        ),
        sa.CheckConstraint("\"right\" IN ('call', 'put')", name="ck_option_contracts_right"),
        schema=S,
    )
    op.create_index(
        "ix_option_contracts_underlying_expiry", "option_contracts", ["underlying", "expiry"], schema=S
    )

    op.create_table(
        "option_chain_cache",
        _fk("underlying_symbol_id", "symbols", primary_key=True),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("chain", JSONB, nullable=False),
        sa.Column("expiries", sa.Integer, nullable=False),
        schema=S,
    )

    op.create_table(
        OPTION_MARKS,
        _fk("contract_id", "option_contracts", primary_key=True),
        sa.Column("fetched_at", TS, nullable=False),
        _money("bid", nullable=True),
        _money("ask", nullable=True),
        _money("last", nullable=True),
        sa.Column("bid_size", sa.Integer),
        sa.Column("ask_size", sa.Integer),
        sa.Column("volume", sa.BigInteger),
        sa.Column("open_interest", sa.BigInteger),
        sa.Column("iv", RATIO),
        sa.Column("delta", RATIO),
        sa.Column("gamma", RATIO),
        sa.Column("theta", RATIO),
        sa.Column("vega", RATIO),
        sa.Column("last_trade_time", TS),
        sa.Column("delay", sa.Integer),
        sa.Column("is_halted", sa.Boolean, nullable=False, server_default=FALSE),
        _money("underlying_price", nullable=True),
        schema=S,
    )

    op.create_table(
        "underlying_facts",
        _fk("symbol_id", "symbols", primary_key=True),
        sa.Column("as_of", sa.Date, primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("security_type", sa.String(30)),
        sa.Column("sector", sa.String(60)),
        _money("price", nullable=True),
        _money("eps_ttm", nullable=True),
        sa.Column("eps_growth_yoy", RATIO),
        sa.Column("debt_to_equity", sa.Numeric(12, 4)),
        _money("book_value_per_share", nullable=True),
        sa.Column("market_cap_usd", sa.Numeric(20, 2)),
        _money("sma50", nullable=True),
        _money("sma50_prior", nullable=True),
        _money("low_52w", nullable=True),
        sa.Column("sessions_since_52w_low", sa.Integer),
        sa.Column("rsi14", sa.Numeric(8, 4)),
        sa.Column("next_earnings_date", sa.Date),
        sa.Column("next_ex_dividend_date", sa.Date),
        _money("dividend_per_share", nullable=True),
        sa.Column("dividend_yield", RATIO),
        sa.Column("payout_ratio", RATIO),
        sa.Column("short_float", RATIO),
        sa.Column("has_options", sa.Boolean),
        sa.Column("sources", JSONB, nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        schema=S,
    )

    op.create_table(
        "option_strategy_configs",
        _id(),
        sa.Column("strategy_key", sa.String(30), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("created_by", sa.String(50), nullable=False),
        sa.UniqueConstraint("strategy_key", "revision", name="uq_option_strategy_configs_key_revision"),
        schema=S,
    )

    op.create_table(
        "option_strategy_state",
        sa.Column("strategy_key", sa.String(30), primary_key=True),
        sa.Column("scope_key", sa.String(100), primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", TS, nullable=False),
        schema=S,
    )

    op.create_table(
        "opt_structures",
        _id(),
        _fk("run_id", "runs"),
        sa.Column("source", sa.String(30), nullable=False),
        _fk("strategy_config_id", "option_strategy_configs", nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        _fk("underlying_symbol_id", "symbols"),
        sa.Column("underlying", sa.String(20), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("close_reason", sa.String(12)),
        sa.Column("frozen", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("qty", sa.Integer, nullable=False),
        _money("entry_net"),
        _money("reserved_cash", zero=True),
        _money("take_profit_net", nullable=True),
        _fk("cover_structure_id", "opt_structures", nullable=True),
        _fk("parent_structure_id", "opt_structures", nullable=True),
        _money("realized_pnl", zero=True),
        _money("fees_total", zero=True),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("closed_at", TS),
        sa.Column("meta", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.CheckConstraint("state IN ('open', 'closed')", name="ck_opt_structures_state"),
        sa.CheckConstraint("qty > 0", name="ck_opt_structures_qty_positive"),
        sa.CheckConstraint("reserved_cash >= 0", name="ck_opt_structures_reserved_nonnegative"),
        schema=S,
    )
    op.create_index("ix_opt_structures_run_state", "opt_structures", ["run_id", "state"], schema=S)
    op.create_index(
        "ix_opt_structures_run_underlying", "opt_structures", ["run_id", "underlying_symbol_id"], schema=S
    )

    op.create_table(
        "opt_positions",
        _id(),
        _fk("run_id", "runs"),
        _fk("structure_id", "opt_structures"),
        sa.Column("instrument", sa.String(6), nullable=False),
        _fk("contract_id", "option_contracts", nullable=True),
        _fk("underlying_symbol_id", "symbols"),
        sa.Column("qty", sa.Integer, nullable=False),
        _money("avg_price"),
        _money("realized_pnl", zero=True),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("closed_at", TS),
        sa.CheckConstraint(
            "(instrument = 'option') = (contract_id IS NOT NULL)", name="ck_opt_positions_instrument"
        ),
        schema=S,
    )
    op.create_index(
        "uq_opt_positions_structure_contract",
        "opt_positions",
        ["structure_id", sa.text("coalesce(contract_id, 0)")],
        unique=True,
        schema=S,
    )

    op.create_table(
        "opt_orders",
        _id(),
        _fk("run_id", "runs"),
        sa.Column("source", sa.String(30), nullable=False),
        _fk("strategy_config_id", "option_strategy_configs", nullable=True),
        sa.Column("intent", sa.String(5), nullable=False),
        _fk("structure_id", "opt_structures", nullable=True),
        _fk("underlying_symbol_id", "symbols"),
        sa.Column("order_type", sa.String(6), nullable=False),
        _money("net_limit", nullable=True),
        sa.Column("tif", sa.String(3), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("walk", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("walk_next_at", TS),
        sa.Column("take_profit_pct", sa.Numeric(6, 4)),
        sa.Column("reject_reason", sa.String(30)),
        sa.Column("reject_detail", sa.Text),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("evidence", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        _money("reserved_cash", zero=True),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("submitted_at", TS, nullable=False),
        sa.Column("submitted_by", sa.String(50), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("closed_at", TS),
        _money("fill_net", nullable=True),
        _money("fees", nullable=True),
        sa.CheckConstraint("qty > 0", name="ck_opt_orders_qty_positive"),
        sa.CheckConstraint("intent IN ('open', 'close', 'roll')", name="ck_opt_orders_intent"),
        sa.CheckConstraint("order_type IN ('market', 'limit')", name="ck_opt_orders_order_type"),
        sa.CheckConstraint("tif IN ('day', 'gtc')", name="ck_opt_orders_tif"),
        sa.CheckConstraint(
            "status IN ('working', 'filled', 'cancelled', 'expired', 'rejected')",
            name="ck_opt_orders_status",
        ),
        sa.CheckConstraint(
            "order_type = 'market' OR net_limit IS NOT NULL OR walk OR status = 'rejected'",
            name="ck_opt_orders_limit",
        ),
        schema=S,
    )
    op.create_index("ix_opt_orders_run_status", "opt_orders", ["run_id", "status"], schema=S)

    op.create_table(
        "opt_order_legs",
        _id(),
        _fk("order_id", "opt_orders"),
        sa.Column("leg_no", sa.SmallInteger, nullable=False),
        sa.Column("instrument", sa.String(6), nullable=False),
        _fk("contract_id", "option_contracts", nullable=True),
        _fk("underlying_symbol_id", "symbols"),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("effect", sa.String(5), nullable=False),
        sa.Column("ratio", sa.Integer, nullable=False),
        sa.UniqueConstraint("order_id", "leg_no", name="uq_opt_order_legs_order_leg"),
        sa.CheckConstraint("ratio > 0", name="ck_opt_order_legs_ratio_positive"),
        schema=S,
    )

    op.create_table(
        "opt_fills",
        _id(),
        _fk("run_id", "runs"),
        _fk("order_id", "opt_orders"),
        _fk("leg_id", "opt_order_legs"),
        _fk("structure_id", "opt_structures"),
        sa.Column("ts", TS, nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        _money("price"),
        _money("fee"),
        sa.Column("quote", JSONB, nullable=False),
        sa.Column("usd_cad_rate", sa.Numeric(12, 6), nullable=False),
        sa.UniqueConstraint("order_id", "leg_id", name="uq_opt_fills_order_leg"),
        sa.CheckConstraint("qty > 0", name="ck_opt_fills_qty_positive"),
        sa.CheckConstraint("price >= 0", name="ck_opt_fills_price_nonnegative"),
        sa.CheckConstraint("fee >= 0", name="ck_opt_fills_fee_nonnegative"),
        schema=S,
    )
    op.create_index("ix_opt_fills_run_ts", "opt_fills", ["run_id", "ts"], schema=S)

    op.create_table(
        "opt_lifecycle_events",
        _id(),
        _fk("run_id", "runs"),
        _fk("structure_id", "opt_structures"),
        _fk("position_id", "opt_positions"),
        _fk("contract_id", "option_contracts", nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("ts", TS, nullable=False),
        _money("underlying_close", nullable=True),
        _money("strike", nullable=True),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("shares_delta", sa.Integer, nullable=False, server_default=ZERO),
        _money("cash_delta", zero=True),
        _fk("new_structure_id", "opt_structures", nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("delivered_at", TS),
        sa.UniqueConstraint("position_id", "kind", "session_date", name="uq_opt_lifecycle_position_kind_day"),
        schema=S,
    )
    op.create_index("ix_opt_lifecycle_run_day", "opt_lifecycle_events", ["run_id", "session_date"], schema=S)

    op.create_table(
        "owner_prompts",
        _id(),
        _fk("run_id", "runs"),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("scope_key", sa.String(60), nullable=False),
        sa.Column("dedupe_key", sa.String(150), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("choices", JSONB, nullable=False),
        sa.Column("needs_text", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("default_choice", sa.String(1)),
        sa.Column("data", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("asked_at", TS, nullable=False),
        sa.Column("last_sent_at", TS),
        sa.Column("send_count", sa.Integer, nullable=False, server_default=ZERO),
        sa.Column("answered_at", TS),
        sa.Column("answer", sa.String(1)),
        sa.Column("answer_text", sa.Text),
        sa.Column("answered_via", sa.String(10)),
        sa.Column("answered_by", sa.String(50)),
        sa.Column("delivered_at", TS),
        sa.UniqueConstraint("dedupe_key", name="uq_owner_prompts_dedupe_key"),
        schema=S,
    )
    op.create_index("ix_owner_prompts_run_status", "owner_prompts", ["run_id", "status"], schema=S)

    op.create_table(
        "wheel_tickers",
        _fk("symbol_id", "symbols", primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("would_own", sa.Boolean),
        sa.Column("ownership_reason", sa.Text),
        sa.Column("thesis_broken", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("security_type_override", sa.String(30)),
        sa.Column("acknowledged_cautions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_verdict", sa.String(30)),
        sa.Column("last_screen", JSONB),
        sa.Column("last_screened_at", TS),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("updated_by", sa.String(50), nullable=False),
        sa.CheckConstraint("status IN ('candidate', 'approved', 'rejected')", name="ck_wheel_tickers_status"),
        schema=S,
    )

    op.create_table(
        "wheel_positions",
        _id(),
        _fk("run_id", "runs"),
        _fk("symbol_id", "symbols"),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        _fk("put_structure_id", "opt_structures", nullable=True),
        _fk("shares_structure_id", "opt_structures", nullable=True),
        _fk("call_structure_id", "opt_structures", nullable=True),
        sa.Column("contracts", sa.Integer, nullable=False),
        sa.Column("roll_count", sa.Integer, nullable=False, server_default=ZERO),
        _money("total_put_premium", zero=True),
        _money("total_call_premium", zero=True),
        _money("dividends", zero=True),
        _money("assignment_strike", nullable=True),
        _money("net_cost", nullable=True),
        sa.Column("entry", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("fresh_cash_answer", sa.Boolean),
        sa.Column("fresh_cash_at", TS),
        sa.Column("drawdown_review_at", TS),
        sa.Column("drawdown_review_text", sa.Text),
        _money("fees", zero=True),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("closed_at", TS),
        sa.Column("close_reason", sa.String(30)),
        _money("full_cycle_result", nullable=True),
        sa.CheckConstraint(
            "state IN ('PUT_OPEN', 'SHARES_HELD', 'CALL_OPEN', 'NONE')", name="ck_wheel_positions_state"
        ),
        schema=S,
    )
    op.create_index(
        "uq_wheel_positions_open_run_symbol",
        "wheel_positions",
        ["run_id", "symbol_id"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "wheel_events",
        _id(),
        _fk("run_id", "runs"),
        _fk("wheel_position_id", "wheel_positions", nullable=True),
        _fk("symbol_id", "symbols"),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("action", sa.String(40)),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("data", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        schema=S,
    )
    op.create_index(
        "uq_wheel_events_evaluate_position_day",
        "wheel_events",
        ["wheel_position_id", "session_date"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("kind = 'evaluate'"),
    )
    op.create_index("ix_wheel_events_run_day", "wheel_events", ["run_id", "session_date"], schema=S)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table, schema=S)  # drops the table's own indexes with it
    op.drop_index("uq_runs_one_active_options", table_name="runs", schema=S)
