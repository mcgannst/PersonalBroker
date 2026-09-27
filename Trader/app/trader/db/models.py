"""ORM models (SPEC §10). Phase 1 tables only; later phases add theirs in new migrations."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
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
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job: Mapped[str] = mapped_column(String(50), index=True)
    session_date: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(20))  # running | succeeded | failed
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[Any] = mapped_column(JSONB, nullable=True)


class EventLog(Base):
    __tablename__ = "event_log"
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
