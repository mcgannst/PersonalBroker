"""ORM models (SPEC §10). Phase 1 tables, then the Phase 2 trading tables (migration 0002)."""

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
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))  # live | replay
    started_at: Mapped[datetime] = mapped_column(TS)
    params: Mapped[Any] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20))  # active | completed | failed
    label: Mapped[str | None] = mapped_column(String(200))


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
    __table_args__ = (UniqueConstraint("strategy_key", "revision", name="uq_strategy_configs_key_revision"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    params: Mapped[Any] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(String(50))


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
