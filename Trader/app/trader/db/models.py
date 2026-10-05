"""ORM models (SPEC §10). Phase 1 tables, the Phase 2 trading tables (migration 0002), the Phase 3
worker and Telegram tables (migration 0004), the Phase 4 web tables (migration 0005), the Phase 5
replay columns and weekly reports (migration 0006), then the Phase 6 decision log (migration 0007) and the
live dashboard's quote marks and mark bars (migration 0008)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "trader"
Money = Numeric(14, 4)
TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_by: Mapped[str] = mapped_column(String(50))


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    api_server: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    last_refresh_at: Mapped[datetime | None] = mapped_column(TS)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(TS)


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (UniqueConstraint("ticker", "exchange"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20))
    exchange: Mapped[str] = mapped_column(String(20))
    questrade_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    name: Mapped[str | None] = mapped_column(String(200))


class UniverseSnapshot(Base):
    __tablename__ = "universe_snapshots"
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("trader.symbols.id"), primary_key=True)
    price: Mapped[Decimal | None] = mapped_column(Money)
    avg_volume: Mapped[int | None] = mapped_column(BigInteger)
    atr14: Mapped[Decimal | None] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(20))  # finviz | fallback | manual


class DailyCandle(Base):
    __tablename__ = "daily_candles"
    symbol_id: Mapped[int] = mapped_column(ForeignKey("trader.symbols.id"), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[int] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Money)


class IntradayCandle(Base):
    __tablename__ = "intraday_candles"
    __table_args__ = {"postgresql_partition_by": "RANGE (ts)"}
    symbol_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    interval: Mapped[str] = mapped_column(String(3), primary_key=True)  # 1m | 5m
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[int] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Money)


class CandleArchive(Base):
    __tablename__ = "candle_archive"
    symbol_id: Mapped[int] = mapped_column(ForeignKey("trader.symbols.id"), primary_key=True)
    interval: Mapped[str] = mapped_column(String(3), primary_key=True)
    start_ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[int] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Money)


class OpenBarStat(Base):
    __tablename__ = "open_bar_stats"
    symbol_id: Mapped[int] = mapped_column(ForeignKey("trader.symbols.id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    avg_open_vol_14d: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    atr14: Mapped[Decimal | None] = mapped_column(Money)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_job_session", "job", "session_date"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job: Mapped[str] = mapped_column(String(50))
    session_date: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(20))  # running | succeeded | failed
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[Any] = mapped_column(JSONB, nullable=True)


class EventLog(Base):
    __tablename__ = "event_log"
    __table_args__ = (Index("ix_event_log_ts", "ts"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS)
    level: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(50))
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Any] = mapped_column(JSONB, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS)
    actor: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(100))
    before: Mapped[Any] = mapped_column(JSONB, nullable=True)
    after: Mapped[Any] = mapped_column(JSONB, nullable=True)


# --- Phase 2: trading (migration 0002). Every trading row carries run_id (SPEC §3a). -------------------
RUN_FK = "trader.runs.id"
SYMBOL_FK = "trader.symbols.id"
CONFIG_FK = "trader.strategy_configs.id"


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index(
            "uq_runs_one_active_live",
            "mode",
            unique=True,
            postgresql_where=text("mode = 'live' AND status = 'active'"),
        ),
        Index("ix_runs_mode_status", "mode", "status"),  # migration 0006
        Index(  # OPTSIM (migration 0011): one active options run, beside the live run
            "uq_runs_one_active_options",
            "mode",
            unique=True,
            postgresql_where=text("mode = 'options' AND status = 'active'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))  # live | replay
    started_at: Mapped[datetime] = mapped_column(TS)
    params: Mapped[Any] = mapped_column(JSONB, nullable=False)
    # live: active | completed | failed; replay: queued | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(20))
    label: Mapped[str | None] = mapped_column(String(200))
    # Replay lifecycle (migration 0006); the live run leaves them null / false.
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    updated_at: Mapped[datetime | None] = mapped_column(TS)
    progress: Mapped[Any] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class SimAccount(Base):
    __tablename__ = "sim_accounts"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK), unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    starting_cash: Mapped[Decimal] = mapped_column(Money)  # in the account currency, after FX and fee
    source_amount: Mapped[Decimal] = mapped_column(Money)
    source_currency: Mapped[str] = mapped_column(String(3))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    fx_fee: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    created_at: Mapped[datetime] = mapped_column(TS)


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"
    # Migration 0006: a replay override is a `replay` row with the base live revision, so revisions are
    # unique among `live` rows only.
    __table_args__ = (
        CheckConstraint("scope IN ('live', 'replay')", name="ck_strategy_configs_scope"),
        Index(
            "uq_strategy_configs_key_revision_live",
            "strategy_key",
            "revision",
            unique=True,
            postgresql_where=text("scope = 'live'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    params: Mapped[Any] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(String(50))
    scope: Mapped[str] = mapped_column(
        String(10), default="live", server_default=text("'live'")
    )  # live | replay


class Catalyst(Base):
    __tablename__ = "catalysts"
    __table_args__ = (UniqueConstraint("symbol_id", "session_date", name="uq_catalysts_symbol_session"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    session_date: Mapped[date] = mapped_column(Date)
    headlines: Mapped[Any] = mapped_column(JSONB, nullable=False)
    gap_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    earnings_date: Mapped[date | None] = mapped_column(Date)
    catalyst_type: Mapped[str] = mapped_column("type", String(20))
    direction: Mapped[str] = mapped_column(String(10))
    quality: Mapped[int | None] = mapped_column(Integer)
    confirmed: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(60))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    classified_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "session_date",
            "strategy_key",
            "symbol_id",
            name="uq_candidates_run_session_strategy_symbol",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    session_date: Mapped[date] = mapped_column(Date)
    strategy_key: Mapped[str] = mapped_column(String(50))
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    rvol: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    rank: Mapped[int | None] = mapped_column(Integer)
    candle: Mapped[Any] = mapped_column(JSONB, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    reject_reason: Mapped[str | None] = mapped_column(String(100))
    data: Mapped[Any] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_run_session", "run_id", "session_date"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    strategy_config_id: Mapped[int] = mapped_column(ForeignKey(CONFIG_FK))
    symbol_id: Mapped[int | None] = mapped_column(ForeignKey(SYMBOL_FK))
    session_date: Mapped[date] = mapped_column(Date)
    event_key: Mapped[str] = mapped_column(String(50))
    ts: Mapped[datetime] = mapped_column(TS)
    intent: Mapped[Any] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[Any] = mapped_column(JSONB, nullable=False)


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (Index("ix_proposals_run_status", "run_id", "status"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    signal_id: Mapped[int] = mapped_column(ForeignKey("trader.signals.id"))
    kind: Mapped[str] = mapped_column(String(10))  # entry | stop | exit | cancel
    order_spec: Mapped[Any] = mapped_column(JSONB, nullable=False)
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS)
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    decided_via: Mapped[str | None] = mapped_column(String(10))  # telegram | web | auto
    decided_by: Mapped[str | None] = mapped_column(String(50))
    decision_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    cancel_order_id: Mapped[int | None] = mapped_column(BigInteger)
    order_id: Mapped[int | None] = mapped_column(BigInteger)
    sizing: Mapped[Any] = mapped_column(JSONB, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(TS)
    escalated_at: Mapped[datetime | None] = mapped_column(TS)
    escalations: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_run_status", "run_id", "status"),
        CheckConstraint("qty > 0", name="ck_orders_qty_positive"),  # migration 0003
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("trader.proposals.id"))
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    strategy_config_id: Mapped[int | None] = mapped_column(ForeignKey(CONFIG_FK))
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    order_type: Mapped[str] = mapped_column(String(12))  # market | limit | stop | stop_limit
    purpose: Mapped[str] = mapped_column(String(10))  # entry | stop | exit
    qty: Mapped[int] = mapped_column(Integer)
    stop_price: Mapped[Decimal | None] = mapped_column(Money)
    limit_price: Mapped[Decimal | None] = mapped_column(Money)
    stop_loss: Mapped[Decimal | None] = mapped_column(Money)
    tif: Mapped[str] = mapped_column(String(3))  # day | gtc
    status: Mapped[str] = mapped_column(String(12))  # working | filled | cancelled
    reason: Mapped[str] = mapped_column(String(100))
    session_date: Mapped[date] = mapped_column(Date)
    submitted_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    cancel_reason: Mapped[str | None] = mapped_column(String(100))
    stale_since: Mapped[datetime | None] = mapped_column(TS)
    stale_alerted: Mapped[bool] = mapped_column(Boolean)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (  # migration 0003
        CheckConstraint("qty > 0", name="ck_fills_qty_positive"),
        CheckConstraint("price > 0", name="ck_fills_price_positive"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    order_id: Mapped[int] = mapped_column(ForeignKey("trader.orders.id"), unique=True)
    ts: Mapped[datetime] = mapped_column(TS)
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Money)
    fees: Mapped[Any] = mapped_column(JSONB, nullable=False)
    quote_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    slippage: Mapped[Decimal] = mapped_column(Money)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_run_closed", "run_id", "closed_at"),
        CheckConstraint("qty >= 0", name="ck_positions_qty_nonnegative"),  # migration 0003
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    strategy_config_id: Mapped[int | None] = mapped_column(ForeignKey(CONFIG_FK))
    qty: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[Decimal] = mapped_column(Money)
    stop_loss: Mapped[Decimal | None] = mapped_column(Money)
    planned_risk: Mapped[Decimal | None] = mapped_column(Money)
    session_date: Mapped[date] = mapped_column(Date)
    opened_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    entry_order_id: Mapped[int] = mapped_column(BigInteger)
    stop_order_id: Mapped[int | None] = mapped_column(BigInteger)
    unprotected_since: Mapped[datetime | None] = mapped_column(TS)
    unprotected_seconds: Mapped[int] = mapped_column(Integer)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    position_id: Mapped[int] = mapped_column(ForeignKey("trader.positions.id"), unique=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    session_date: Mapped[date] = mapped_column(Date)
    entry_price: Mapped[Decimal] = mapped_column(Money)
    exit_price: Mapped[Decimal] = mapped_column(Money)
    qty: Mapped[int] = mapped_column(Integer)
    pnl: Mapped[Decimal] = mapped_column(Money)
    pnl_r: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    planned_risk: Mapped[Decimal | None] = mapped_column(Money)
    exit_reason: Mapped[str] = mapped_column(String(100))  # = orders.reason (migration 0003)
    slippage_total: Mapped[Decimal] = mapped_column(Money)
    fees_total: Mapped[Decimal] = mapped_column(Money)
    opened_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime] = mapped_column(TS)


class CashLedger(Base):
    __tablename__ = "cash_ledger"
    __table_args__ = (Index("ix_cash_ledger_run_settle", "run_id", "settle_date"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    ts: Mapped[datetime] = mapped_column(TS)
    trade_date: Mapped[date] = mapped_column(Date)
    settle_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Money)
    kind: Mapped[str] = mapped_column(String(10))  # deposit | buy | sell | fee
    ref: Mapped[str] = mapped_column(String(100))


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    equity: Mapped[Decimal] = mapped_column(Money)
    cash: Mapped[Decimal] = mapped_column(Money)
    settled_cash: Mapped[Decimal] = mapped_column(Money)
    peak_equity: Mapped[Decimal] = mapped_column(Money)
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4))


class Journal(Base):
    __tablename__ = "journal"
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rules_followed: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    answered_via: Mapped[str | None] = mapped_column(String(20))
    updated_at: Mapped[datetime | None] = mapped_column(TS)


class KillSwitchEvent(Base):
    __tablename__ = "kill_switch_events"
    __table_args__ = (Index("ix_kill_switch_events_run", "run_id", "switch"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    switch: Mapped[str] = mapped_column(String(30))
    session_date: Mapped[date] = mapped_column(Date)
    tripped_at: Mapped[datetime] = mapped_column(TS)
    value: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    reset_at: Mapped[datetime | None] = mapped_column(TS)
    reset_reason: Mapped[str | None] = mapped_column(Text)
    reset_by: Mapped[str | None] = mapped_column(String(50))


# --- Phase 3: worker and Telegram (migration 0004). Operational tables, so no run_id (like job_runs). ------
class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    process: Mapped[str] = mapped_column(String(30), primary_key=True)  # "worker"
    pid: Mapped[int] = mapped_column(Integer)
    host: Mapped[str] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(TS)
    beat_at: Mapped[datetime] = mapped_column(TS)
    session_date: Mapped[date | None] = mapped_column(Date)
    phase: Mapped[str] = mapped_column(String(20))  # starting | idle | session | stopping | stopped
    detail: Mapped[Any] = mapped_column(JSONB, nullable=True)


class TelegramCallback(Base):
    __tablename__ = "telegram_callbacks"
    __table_args__ = (Index("ix_telegram_callbacks_kind_ref", "kind", "ref"),)
    nonce: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # proposal | pause | journal
    ref: Mapped[str] = mapped_column(String(50))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    used_at: Mapped[datetime | None] = mapped_column(TS)
    used_action: Mapped[str | None] = mapped_column(String(20))


class Notification(Base):
    __tablename__ = "notifications"
    # Unique where set: PostgreSQL treats NULLs as distinct, so many rows may have no key.
    __table_args__ = (Index("uq_notifications_dedupe_key", "dedupe_key", unique=True),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS)
    sent_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(10))  # sending | sent | failed
    message_ids: Mapped[Any] = mapped_column(JSONB, nullable=True)
    # server_default as a plain string: `text` is a column name in this class body.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)


class NotifyCursor(Base):
    __tablename__ = "notify_cursors"
    stream: Mapped[str] = mapped_column(String(30), primary_key=True)  # proposals | fills | events
    last_id: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(TS)


# --- Phase 4: web app (migration 0005). Operational tables, so no run_id (like job_runs). -------------------
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)  # Argon2id; never leaves the server
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)  # Crypto-encrypted base32 secret
    totp_pending_enc: Mapped[str | None] = mapped_column(Text)  # set up but not yet confirmed
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)  # the last accepted step (no replay)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)
    password_changed_at: Mapped[datetime] = mapped_column(TS)
    last_login_at: Mapped[datetime | None] = mapped_column(TS)


class WebSession(Base):
    __tablename__ = "web_sessions"
    __table_args__ = (Index("ix_web_sessions_user_id", "user_id"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("trader.users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # SHA-256 hex of the cookie token
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)
    last_seen_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(200))


class ManualWatchlist(Base):
    __tablename__ = "manual_watchlists"
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    tickers: Mapped[Any] = mapped_column(JSONB, nullable=False)  # list of Questrade-style tickers
    filename: Mapped[str | None] = mapped_column(String(200))
    uploaded_at: Mapped[datetime] = mapped_column(TS)
    uploaded_by: Mapped[str] = mapped_column(String(50))


# --- Phase 5: weekly reports (migration 0006). Operational data about the live run, not a trading row. -----
class WeeklyReport(Base):
    __tablename__ = "weekly_reports"
    week_ending: Mapped[date] = mapped_column(Date, primary_key=True)  # the week's last session date
    week_start: Mapped[date] = mapped_column(Date)  # the Monday
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))  # the live run it describes
    facts: Mapped[Any] = mapped_column(JSONB, nullable=False)
    commentary: Mapped[str | None] = mapped_column(Text)
    commentary_status: Mapped[str] = mapped_column(String(20))  # ok | disabled | budget | rejected | error
    commentary_error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(60))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal(0), server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


# --- Phase 6: the decision log (migration 0007). A derived journal, rebuilt per (run, session) by the -------
# recorder; the CHECK lists equal trader.decisions.types DecisionStage / DecisionOutcome (a test reads both).
DECISION_STAGES_SQL = (
    "stage IN ('universe', 'premarket', 'scan', 'signal', 'risk', 'proposal', 'approval', 'order', 'fill', "
    "'exit', 'overlay', 'kill_switch', 'day')"
)
DECISION_OUTCOMES_SQL = (
    "outcome IN ('info', 'listed', 'classified', 'passed', 'rejected', 'proposed', 'approved', "
    "'auto_approved', 'declined', 'expired', 'blocked', 'submitted', 'filled', 'cancelled', 'exited', "
    "'tripped', 'reset', 'error')"
)


class DecisionLog(Base):
    __tablename__ = "decision_log"
    __table_args__ = (
        UniqueConstraint("run_id", "session_date", "seq", name="uq_decision_log_run_day_seq"),
        CheckConstraint(DECISION_STAGES_SQL, name="ck_decision_log_stage"),
        CheckConstraint(DECISION_OUTCOMES_SQL, name="ck_decision_log_outcome"),
        Index("ix_decision_log_run_day_stage", "run_id", "session_date", "stage"),
        Index("ix_decision_log_day", "session_date"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    session_date: Mapped[date] = mapped_column(Date)
    seq: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(20))  # DecisionStage
    strategy_key: Mapped[str | None] = mapped_column(String(50))
    symbol_id: Mapped[int | None] = mapped_column(ForeignKey(SYMBOL_FK))
    ticker: Mapped[str | None] = mapped_column(String(20))
    outcome: Mapped[str] = mapped_column(String(20))  # DecisionOutcome
    rule: Mapped[str | None] = mapped_column(String(60))
    reason: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(TS)  # when the decision happened (from the source row)
    ref: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    data: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    recorded_at: Mapped[datetime] = mapped_column(TS)
    final: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


# --- Phase 6: live marks (migration 0008). Written only by the worker's mark publisher from quotes it already
# received, read only by the API (live dashboard plan S1/S2). Run-scoped. Bars from quotes, never candles.
MARK_BAR_OHLC_SQL = "low <= open AND low <= close AND high >= open AND high >= close AND low > 0"


class QuoteMark(Base):
    __tablename__ = "quote_marks"
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK), primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK), primary_key=True)
    bid: Mapped[Decimal | None] = mapped_column(Money)
    ask: Mapped[Decimal | None] = mapped_column(Money)
    last: Mapped[Decimal | None] = mapped_column(Money)
    quote_time: Mapped[datetime | None] = mapped_column(TS)  # Questrade's lastTradeTime
    observed_at: Mapped[datetime] = mapped_column(TS)  # when the worker received it
    written_at: Mapped[datetime] = mapped_column(TS)
    is_halted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class MarkBar(Base):
    __tablename__ = "mark_bars"
    __table_args__ = (
        CheckConstraint(MARK_BAR_OHLC_SQL, name="ck_mark_bars_ohlc"),
        CheckConstraint("samples > 0", name="ck_mark_bars_samples"),
    )
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK), primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK), primary_key=True)
    minute_start: Mapped[datetime] = mapped_column(TS, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    samples: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(TS)


# --- QUOTEBAR (migration 0009): the 9:35 opening bar from live quotes. Market data, not run-scoped. ---------
class OpeningBarQuote(Base):
    """One symbol's 09:30-09:35 bar as the 9:35 scan built it from a live quote (open/high/low: the quote's
    session open/high/low; close: its last regular-hours trade; `volume`: `quote_volume` x `vol_factor`, the
    candle scale). NOT a candle: nothing that reads candles (replay, the archive) ever reads it. The
    `official_*` columns are the delayed official candle the ~09:47 shadow check compared it with."""

    __tablename__ = "opening_bar_quotes"
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(TS)  # when the scan read the quote
    quote_time: Mapped[datetime | None] = mapped_column(TS)  # Questrade's lastTradeTime
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    quote_volume: Mapped[int] = mapped_column(BigInteger)  # the quote's consolidated volume, as read
    volume: Mapped[int] = mapped_column(BigInteger)  # candle scale: what rvol used
    vol_factor: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    factor_source: Mapped[str] = mapped_column(String(10))  # symbol | median | default
    checked_at: Mapped[datetime | None] = mapped_column(TS)
    check_status: Mapped[str | None] = mapped_column(String(200))  # "compared" or the missing reason
    official_open: Mapped[Decimal | None] = mapped_column(Money)
    official_high: Mapped[Decimal | None] = mapped_column(Money)
    official_low: Mapped[Decimal | None] = mapped_column(Money)
    official_close: Mapped[Decimal | None] = mapped_column(Money)
    official_volume: Mapped[int | None] = mapped_column(BigInteger)
    decision_differs: Mapped[bool | None] = mapped_column(Boolean)
    # FIX-DAY1 (migration 0010): `volume` = (quote_volume - open_volume) x vol_factor, volume_basis "delta";
    # the capture's start and end (NULL: the scan's own quotes pass, not the 09:35:00 capture).
    open_volume: Mapped[int | None] = mapped_column(BigInteger)
    volume_basis: Mapped[str | None] = mapped_column(String(20))
    capture_started_at: Mapped[datetime | None] = mapped_column(TS)
    capture_ended_at: Mapped[datetime | None] = mapped_column(TS)


class OpeningQuoteCapture(Base):
    """FIX-DAY1 (migration 0010): one symbol's raw quote from a timed capture: `open` (the volume at the open,
    read just before 09:30:00 ET) or `bar` (read from 09:35:00.0 ET, what the 9:35:05 ORB event builds its bar
    from). `quote_time` is Questrade's lastTradeTime, `fetched_at` when the app received the quote."""

    __tablename__ = "opening_quote_captures"
    __table_args__ = (CheckConstraint("kind IN ('open', 'bar')", name="ck_opening_quote_captures_kind"),)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    capture_started_at: Mapped[datetime] = mapped_column(TS)
    capture_ended_at: Mapped[datetime] = mapped_column(TS)
    fetched_at: Mapped[datetime] = mapped_column(TS)
    quote_time: Mapped[datetime | None] = mapped_column(TS)
    open: Mapped[Decimal | None] = mapped_column(Money)
    high: Mapped[Decimal | None] = mapped_column(Money)
    low: Mapped[Decimal | None] = mapped_column(Money)
    last: Mapped[Decimal | None] = mapped_column(Money)
    last_regular: Mapped[Decimal | None] = mapped_column(Money)
    volume: Mapped[int] = mapped_column(BigInteger)
    delay: Mapped[int | None] = mapped_column(Integer)


class QuoteVolumeScale(Base):
    """A session's volume factor per symbol, measured after the close: `candle_volume` (the regular-session
    5-minute candles summed) / `quote_volume` (the quote's day volume). NULL factor: not measurable."""

    __tablename__ = "quote_volume_scale"
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK), primary_key=True)
    quote_volume: Mapped[int] = mapped_column(BigInteger)
    candle_volume: Mapped[int | None] = mapped_column(BigInteger)
    factor: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    recorded_at: Mapped[datetime] = mapped_column(TS)
    # FIX-DAY1 (migration 0010): the pre-market volume taken out of `quote_volume` before the ratio;
    # `snapshot` (the open capture, quote scale) or `candles` (that day's pre-market candles, candle scale).
    premarket_volume: Mapped[int | None] = mapped_column(BigInteger)
    premarket_source: Mapped[str | None] = mapped_column(String(10))


# --- OPTSIM: the options simulation (migration 0011). The options run is a `runs` row with mode `options`; ---
# it reuses sim_accounts, cash_ledger, equity_snapshots, event_log and job_runs. Nothing above reads these.
Ratio = Numeric(12, 6)  # greeks, IV and other ratios
CONTRACT_FK = "trader.option_contracts.id"
OPT_CONFIG_FK = "trader.option_strategy_configs.id"
STRUCTURE_FK = "trader.opt_structures.id"
EMPTY_OBJECT = text("'{}'::jsonb")
ZERO_DEFAULT = text("0")
FALSE_DEFAULT = text("false")

OPT_RIGHTS_SQL = "\"right\" IN ('call', 'put')"
OPT_STRUCTURE_STATES_SQL = "state IN ('open', 'closed')"
OPT_ORDER_INTENTS_SQL = "intent IN ('open', 'close', 'roll')"
OPT_ORDER_TYPES_SQL = "order_type IN ('market', 'limit')"
OPT_ORDER_TIFS_SQL = "tif IN ('day', 'gtc')"
OPT_ORDER_STATUSES_SQL = "status IN ('working', 'filled', 'cancelled', 'expired', 'rejected')"
# A limit order needs a limit or the walk (which picks one). A rejected order is stored as it was asked.
OPT_ORDER_LIMIT_SQL = "order_type = 'market' OR net_limit IS NOT NULL OR walk OR status = 'rejected'"
OPT_POSITION_INSTRUMENT_SQL = "(instrument = 'option') = (contract_id IS NOT NULL)"
WHEEL_TICKER_STATUSES_SQL = "status IN ('candidate', 'approved', 'rejected')"
WHEEL_POSITION_STATES_SQL = "state IN ('PUT_OPEN', 'SHARES_HELD', 'CALL_OPEN', 'NONE')"


class OptionContract(Base):
    """The contract master: one row per listed contract ever seen in a chain. `id` is the contract id used
    everywhere in the options tables; `qt_symbol_id` is Questrade's."""

    __tablename__ = "option_contracts"
    __table_args__ = (
        UniqueConstraint("qt_symbol_id", name="uq_option_contracts_qt_symbol_id"),
        UniqueConstraint(
            "underlying_symbol_id", "expiry", "strike", "right", "root", name="uq_option_contracts_key"
        ),
        CheckConstraint(OPT_RIGHTS_SQL, name="ck_option_contracts_right"),
        Index("ix_option_contracts_underlying_expiry", "underlying", "expiry"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    underlying_symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    underlying: Mapped[str] = mapped_column(String(20))
    qt_symbol_id: Mapped[int] = mapped_column(BigInteger)
    root: Mapped[str] = mapped_column(String(20))
    expiry: Mapped[date] = mapped_column(Date)
    strike: Mapped[Decimal] = mapped_column(Money)
    right: Mapped[str] = mapped_column(String(4))  # call | put
    multiplier: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))
    is_monthly: Mapped[bool] = mapped_column(Boolean)
    adjusted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=FALSE_DEFAULT)
    first_seen_at: Mapped[datetime] = mapped_column(TS)


class OptionChainCache(Base):
    """An underlying's chain structure as Questrade last sent it: a list of
    `{expiry, root, multiplier, strikes: [{strike, call_id, put_id}]}` with Questrade ids."""

    __tablename__ = "option_chain_cache"
    underlying_symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(TS)
    chain: Mapped[Any] = mapped_column(JSONB, nullable=False)
    expiries: Mapped[int] = mapped_column(Integer)


class OptionQuoteMark(Base):
    """The last quote recorded for a contract (the fallback mark when no live quote is at hand). `iv` is a
    decimal fraction (0.35 = 35%); `delta` is signed."""

    __tablename__ = "option_quote_marks"
    contract_id: Mapped[int] = mapped_column(ForeignKey(CONTRACT_FK), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(TS)
    bid: Mapped[Decimal | None] = mapped_column(Money)
    ask: Mapped[Decimal | None] = mapped_column(Money)
    last: Mapped[Decimal | None] = mapped_column(Money)
    bid_size: Mapped[int | None] = mapped_column(Integer)
    ask_size: Mapped[int | None] = mapped_column(Integer)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    iv: Mapped[Decimal | None] = mapped_column(Ratio)
    delta: Mapped[Decimal | None] = mapped_column(Ratio)
    gamma: Mapped[Decimal | None] = mapped_column(Ratio)
    theta: Mapped[Decimal | None] = mapped_column(Ratio)
    vega: Mapped[Decimal | None] = mapped_column(Ratio)
    last_trade_time: Mapped[datetime | None] = mapped_column(TS)
    delay: Mapped[int | None] = mapped_column(Integer)
    is_halted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=FALSE_DEFAULT)
    underlying_price: Mapped[Decimal | None] = mapped_column(Money)


class UnderlyingFacts(Base):
    """One underlying's facts on one day (wheel rules spec §3.1). Every fact is nullable (unknown);
    `sources` maps a field to `[source, fetched at]`. Its value type is `trader.options.types.UnderlyingFacts`."""

    __tablename__ = "underlying_facts"
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20))
    security_type: Mapped[str | None] = mapped_column(String(30))
    sector: Mapped[str | None] = mapped_column(String(60))
    price: Mapped[Decimal | None] = mapped_column(Money)
    eps_ttm: Mapped[Decimal | None] = mapped_column(Money)
    eps_growth_yoy: Mapped[Decimal | None] = mapped_column(Ratio)
    debt_to_equity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    book_value_per_share: Mapped[Decimal | None] = mapped_column(Money)
    market_cap_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    sma50: Mapped[Decimal | None] = mapped_column(Money)
    sma50_prior: Mapped[Decimal | None] = mapped_column(Money)
    low_52w: Mapped[Decimal | None] = mapped_column(Money)
    sessions_since_52w_low: Mapped[int | None] = mapped_column(Integer)
    rsi14: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    next_earnings_date: Mapped[date | None] = mapped_column(Date)
    next_ex_dividend_date: Mapped[date | None] = mapped_column(Date)
    dividend_per_share: Mapped[Decimal | None] = mapped_column(Money)
    dividend_yield: Mapped[Decimal | None] = mapped_column(Ratio)
    payout_ratio: Mapped[Decimal | None] = mapped_column(Ratio)
    short_float: Mapped[Decimal | None] = mapped_column(Ratio)
    has_options: Mapped[bool | None] = mapped_column(Boolean)
    sources: Mapped[Any] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TS)


class OptionStrategyConfig(Base):
    """A plug-in's versioned settings: every change is a new revision (no replay scope here)."""

    __tablename__ = "option_strategy_configs"
    __table_args__ = (
        UniqueConstraint("strategy_key", "revision", name="uq_option_strategy_configs_key_revision"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    params: Mapped[Any] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(String(50))


class OptionStrategyState(Base):
    __tablename__ = "option_strategy_state"
    strategy_key: Mapped[str] = mapped_column(String(30), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(TS)


class OptStructure(Base):
    """A group of positions opened together (a put, a spread, shares, ...). `source` is `manual` or a
    strategy key. `entry_net` and `take_profit_net` are per share, credit positive. `reserved_cash` is the
    cash this structure holds back; `cover_structure_id` is the shares structure covering a short call."""

    __tablename__ = "opt_structures"
    __table_args__ = (
        CheckConstraint(OPT_STRUCTURE_STATES_SQL, name="ck_opt_structures_state"),
        CheckConstraint("qty > 0", name="ck_opt_structures_qty_positive"),
        CheckConstraint("reserved_cash >= 0", name="ck_opt_structures_reserved_nonnegative"),
        Index("ix_opt_structures_run_state", "run_id", "state"),
        Index("ix_opt_structures_run_underlying", "run_id", "underlying_symbol_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    source: Mapped[str] = mapped_column(String(30))
    strategy_config_id: Mapped[int | None] = mapped_column(ForeignKey(OPT_CONFIG_FK))
    kind: Mapped[str] = mapped_column(String(20))  # StructureKind
    underlying_symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    underlying: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(8))  # open | closed
    close_reason: Mapped[str | None] = mapped_column(String(12))  # CloseReason
    frozen: Mapped[bool] = mapped_column(Boolean, default=False, server_default=FALSE_DEFAULT)
    qty: Mapped[int] = mapped_column(Integer)
    entry_net: Mapped[Decimal] = mapped_column(Money)
    reserved_cash: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default=ZERO_DEFAULT)
    take_profit_net: Mapped[Decimal | None] = mapped_column(Money)
    cover_structure_id: Mapped[int | None] = mapped_column(ForeignKey(STRUCTURE_FK))
    parent_structure_id: Mapped[int | None] = mapped_column(ForeignKey(STRUCTURE_FK))
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default=ZERO_DEFAULT)
    fees_total: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default=ZERO_DEFAULT)
    opened_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    meta: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=EMPTY_OBJECT)


class OptPosition(Base):
    """One instrument held inside a structure: an option contract, or shares (`contract_id` NULL). `qty` is
    signed (short < 0), in contracts or shares. One row per structure and contract."""

    __tablename__ = "opt_positions"
    __table_args__ = (
        CheckConstraint(OPT_POSITION_INSTRUMENT_SQL, name="ck_opt_positions_instrument"),
        Index(
            "uq_opt_positions_structure_contract",
            "structure_id",
            text("coalesce(contract_id, 0)"),
            unique=True,
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    structure_id: Mapped[int] = mapped_column(ForeignKey(STRUCTURE_FK))
    instrument: Mapped[str] = mapped_column(String(6))  # option | shares
    contract_id: Mapped[int | None] = mapped_column(ForeignKey(CONTRACT_FK))
    underlying_symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    qty: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default=ZERO_DEFAULT)
    opened_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)


class OptOrder(Base):
    """A multi-leg order; all legs fill together or not at all. `net_limit` and `fill_net` are per share,
    credit positive. `reserved_cash` is what the order holds back while it works."""

    __tablename__ = "opt_orders"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_opt_orders_qty_positive"),
        CheckConstraint(OPT_ORDER_INTENTS_SQL, name="ck_opt_orders_intent"),
        CheckConstraint(OPT_ORDER_TYPES_SQL, name="ck_opt_orders_order_type"),
        CheckConstraint(OPT_ORDER_TIFS_SQL, name="ck_opt_orders_tif"),
        CheckConstraint(OPT_ORDER_STATUSES_SQL, name="ck_opt_orders_status"),
        CheckConstraint(OPT_ORDER_LIMIT_SQL, name="ck_opt_orders_limit"),
        Index("ix_opt_orders_run_status", "run_id", "status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    source: Mapped[str] = mapped_column(String(30))
    strategy_config_id: Mapped[int | None] = mapped_column(ForeignKey(OPT_CONFIG_FK))
    intent: Mapped[str] = mapped_column(String(5))  # open | close | roll
    structure_id: Mapped[int | None] = mapped_column(ForeignKey(STRUCTURE_FK))
    underlying_symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    order_type: Mapped[str] = mapped_column(String(6))  # market | limit
    net_limit: Mapped[Decimal | None] = mapped_column(Money)
    tif: Mapped[str] = mapped_column(String(3))  # day | gtc
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))  # OrderStatus
    walk: Mapped[bool] = mapped_column(Boolean, default=False, server_default=FALSE_DEFAULT)
    walk_next_at: Mapped[datetime | None] = mapped_column(TS)
    take_profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    reject_reason: Mapped[str | None] = mapped_column(String(30))  # RejectReason
    reject_detail: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(100))
    evidence: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=EMPTY_OBJECT)
    reserved_cash: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default=ZERO_DEFAULT)
    session_date: Mapped[date] = mapped_column(Date)
    submitted_at: Mapped[datetime] = mapped_column(TS)
    submitted_by: Mapped[str] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    fill_net: Mapped[Decimal | None] = mapped_column(Money)
    fees: Mapped[Decimal | None] = mapped_column(Money)


class OptOrderLeg(Base):
    __tablename__ = "opt_order_legs"
    __table_args__ = (
        UniqueConstraint("order_id", "leg_no", name="uq_opt_order_legs_order_leg"),
        CheckConstraint("ratio > 0", name="ck_opt_order_legs_ratio_positive"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("trader.opt_orders.id"))
    leg_no: Mapped[int] = mapped_column(SmallInteger)
    instrument: Mapped[str] = mapped_column(String(6))  # option | shares
    contract_id: Mapped[int | None] = mapped_column(ForeignKey(CONTRACT_FK))
    underlying_symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    effect: Mapped[str] = mapped_column(String(5))  # open | close
    ratio: Mapped[int] = mapped_column(Integer)


class OptFill(Base):
    """One leg's fill (an order fills all its legs in one transaction). `qty` is contracts or shares;
    `quote` is the quote the fill was priced from."""

    __tablename__ = "opt_fills"
    __table_args__ = (
        UniqueConstraint("order_id", "leg_id", name="uq_opt_fills_order_leg"),
        CheckConstraint("qty > 0", name="ck_opt_fills_qty_positive"),
        CheckConstraint("price >= 0", name="ck_opt_fills_price_nonnegative"),
        CheckConstraint("fee >= 0", name="ck_opt_fills_fee_nonnegative"),
        Index("ix_opt_fills_run_ts", "run_id", "ts"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    order_id: Mapped[int] = mapped_column(ForeignKey("trader.opt_orders.id"))
    leg_id: Mapped[int] = mapped_column(ForeignKey("trader.opt_order_legs.id"))
    structure_id: Mapped[int] = mapped_column(ForeignKey(STRUCTURE_FK))
    ts: Mapped[datetime] = mapped_column(TS)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Money)
    fee: Mapped[Decimal] = mapped_column(Money)
    quote: Mapped[Any] = mapped_column(JSONB, nullable=False)
    usd_cad_rate: Mapped[Decimal] = mapped_column(Numeric(12, 6))


class OptLifecycleEvent(Base):
    """What happened to a position at expiry or on early assignment; unique per position, kind and session,
    which is what makes the post-close job safe to repeat."""

    __tablename__ = "opt_lifecycle_events"
    __table_args__ = (
        UniqueConstraint("position_id", "kind", "session_date", name="uq_opt_lifecycle_position_kind_day"),
        Index("ix_opt_lifecycle_run_day", "run_id", "session_date"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    structure_id: Mapped[int] = mapped_column(ForeignKey(STRUCTURE_FK))
    position_id: Mapped[int] = mapped_column(ForeignKey("trader.opt_positions.id"))
    contract_id: Mapped[int | None] = mapped_column(ForeignKey(CONTRACT_FK))
    kind: Mapped[str] = mapped_column(String(16))  # LifecycleKind
    session_date: Mapped[date] = mapped_column(Date)
    ts: Mapped[datetime] = mapped_column(TS)
    underlying_close: Mapped[Decimal | None] = mapped_column(Money)
    strike: Mapped[Decimal | None] = mapped_column(Money)
    qty: Mapped[int] = mapped_column(Integer)
    shares_delta: Mapped[int] = mapped_column(Integer, default=0, server_default=ZERO_DEFAULT)
    cash_delta: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default=ZERO_DEFAULT)
    new_structure_id: Mapped[int | None] = mapped_column(ForeignKey(STRUCTURE_FK))
    detail: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=EMPTY_OBJECT)
    delivered_at: Mapped[datetime | None] = mapped_column(TS)


class OwnerPrompt(Base):
    """A question for the owner, answered from Telegram or the web. `dedupe_key` makes asking idempotent;
    `delivered_at` is when the answer reached the plug-in that asked."""

    __tablename__ = "owner_prompts"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_owner_prompts_dedupe_key"),
        Index("ix_owner_prompts_run_status", "run_id", "status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    source: Mapped[str] = mapped_column(String(30))
    kind: Mapped[str] = mapped_column(String(30))
    scope_key: Mapped[str] = mapped_column(String(60))
    dedupe_key: Mapped[str] = mapped_column(String(150))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    choices: Mapped[Any] = mapped_column(JSONB, nullable=False)
    needs_text: Mapped[bool] = mapped_column(Boolean, default=False, server_default=FALSE_DEFAULT)
    default_choice: Mapped[str | None] = mapped_column(String(1))
    data: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=EMPTY_OBJECT)
    status: Mapped[str] = mapped_column(String(10))  # PromptStatus
    asked_at: Mapped[datetime] = mapped_column(TS)
    last_sent_at: Mapped[datetime | None] = mapped_column(TS)
    send_count: Mapped[int] = mapped_column(Integer, default=0, server_default=ZERO_DEFAULT)
    answered_at: Mapped[datetime | None] = mapped_column(TS)
    answer: Mapped[str | None] = mapped_column(String(1))
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_via: Mapped[str | None] = mapped_column(String(10))  # telegram | web
    answered_by: Mapped[str | None] = mapped_column(String(50))
    delivered_at: Mapped[datetime | None] = mapped_column(TS)


# The wheel plug-in's own tables (only `trader.option_strategies.wheel` reads or writes them).
class WheelTicker(Base):
    __tablename__ = "wheel_tickers"
    __table_args__ = (CheckConstraint(WHEEL_TICKER_STATUSES_SQL, name="ck_wheel_tickers_status"),)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10))  # candidate | approved | rejected
    would_own: Mapped[bool | None] = mapped_column(Boolean)
    ownership_reason: Mapped[str | None] = mapped_column(Text)
    thesis_broken: Mapped[bool] = mapped_column(Boolean, default=False, server_default=FALSE_DEFAULT)
    security_type_override: Mapped[str | None] = mapped_column(String(30))
    acknowledged_cautions: Mapped[Any] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    last_verdict: Mapped[str | None] = mapped_column(String(30))
    last_screen: Mapped[Any] = mapped_column(JSONB, nullable=True)
    last_screened_at: Mapped[datetime | None] = mapped_column(TS)
    origin: Mapped[str] = mapped_column(String(10))  # screen | manual
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)
    updated_by: Mapped[str] = mapped_column(String(50))


class WheelPosition(Base):
    """One wheel cycle on one ticker. Premiums and dividends are per share. At most one open cycle per run
    and ticker."""

    __tablename__ = "wheel_positions"
    __table_args__ = (
        CheckConstraint(WHEEL_POSITION_STATES_SQL, name="ck_wheel_positions_state"),
        Index(
            "uq_wheel_positions_open_run_symbol",
            "run_id",
            "symbol_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    ticker: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(12))  # PUT_OPEN | SHARES_HELD | CALL_OPEN | NONE
    put_structure_id: Mapped[int | None] = mapped_column(ForeignKey(STRUCTURE_FK))
    shares_structure_id: Mapped[int | None] = mapped_column(ForeignKey(STRUCTURE_FK))
    call_structure_id: Mapped[int | None] = mapped_column(ForeignKey(STRUCTURE_FK))
    contracts: Mapped[int] = mapped_column(Integer)
    roll_count: Mapped[int] = mapped_column(Integer, default=0, server_default=ZERO_DEFAULT)
    total_put_premium: Mapped[Decimal] = mapped_column(
        Money, default=Decimal(0), server_default=ZERO_DEFAULT
    )
    total_call_premium: Mapped[Decimal] = mapped_column(
        Money, default=Decimal(0), server_default=ZERO_DEFAULT
    )
    dividends: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default=ZERO_DEFAULT)
    assignment_strike: Mapped[Decimal | None] = mapped_column(Money)
    net_cost: Mapped[Decimal | None] = mapped_column(Money)
    entry: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=EMPTY_OBJECT)
    fresh_cash_answer: Mapped[bool | None] = mapped_column(Boolean)
    fresh_cash_at: Mapped[datetime | None] = mapped_column(TS)
    drawdown_review_at: Mapped[datetime | None] = mapped_column(TS)
    drawdown_review_text: Mapped[str | None] = mapped_column(Text)
    fees: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default=ZERO_DEFAULT)
    opened_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    close_reason: Mapped[str | None] = mapped_column(String(30))
    full_cycle_result: Mapped[Decimal | None] = mapped_column(Money)


class WheelEvent(Base):
    """The wheel's journal: screens, daily evaluations (one per position and session), actions, alerts,
    answers, lifecycle events and reviews."""

    __tablename__ = "wheel_events"
    __table_args__ = (
        Index(
            "uq_wheel_events_evaluate_position_day",
            "wheel_position_id",
            "session_date",
            unique=True,
            postgresql_where=text("kind = 'evaluate'"),
        ),
        Index("ix_wheel_events_run_day", "run_id", "session_date"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    wheel_position_id: Mapped[int | None] = mapped_column(ForeignKey("trader.wheel_positions.id"))
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    session_date: Mapped[date] = mapped_column(Date)
    ts: Mapped[datetime] = mapped_column(TS)
    kind: Mapped[str] = mapped_column(String(10))  # screen|evaluate|action|alert|answer|lifecycle|review
    action: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    data: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default=EMPTY_OBJECT)
