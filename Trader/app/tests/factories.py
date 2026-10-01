"""Tiny row builders for DB tests. Each flushes and returns the new primary key; the caller commits."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from trader.db import models as m

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
_ONE_SECOND = timedelta(seconds=1)


def add_symbol(
    s: Session,
    ticker: str = "AAA",
    *,
    questrade_id: int | None = None,
    exchange: str = "NASDAQ",
    currency: str = "USD",
    name: str | None = None,
) -> int:
    sym = m.Symbol(
        ticker=ticker,
        exchange=exchange,
        questrade_id=questrade_id,
        currency=currency,
        name=name or f"{ticker} Inc",
    )
    s.add(sym)
    s.flush()
    return sym.id


def add_run(
    s: Session,
    *,
    mode: str = "live",
    status: str = "active",
    started_at: datetime = T0,
    label: str | None = None,
) -> int:
    run = m.Run(mode=mode, started_at=started_at, params={}, status=status, label=label)
    s.add(run)
    s.flush()
    return run.id


def add_strategy_config(
    s: Session,
    key: str = "orb_sip",
    *,
    version: str = "1.0.0",
    revision: int = 1,
    params: dict[str, Any] | None = None,
    enabled: bool = True,
    created_at: datetime = T0,
) -> int:
    cfg = m.StrategyConfig(
        strategy_key=key,
        version=version,
        revision=revision,
        params=params or {},
        enabled=enabled,
        created_at=created_at,
        created_by="test",
    )
    s.add(cfg)
    s.flush()
    return cfg.id


def add_capture(
    s: Session,
    session_date: Any,
    symbol_id: int,
    kind: str,
    at: datetime,
    *,
    volume: int,
    quote_time: datetime | None = None,
    **prices: Any,
) -> None:
    """FIX-DAY1: one stored timed quote capture (`open`: the volume at the open, `bar`: the 09:35:00 one).
    `prices` sets open/high/low/last/last_regular (Decimals); `quote_time` defaults to `at` - 1 s."""
    s.add(
        m.OpeningQuoteCapture(
            session_date=session_date,
            symbol_id=symbol_id,
            kind=kind,
            capture_started_at=at,
            capture_ended_at=at,
            fetched_at=at,
            quote_time=quote_time if quote_time is not None else at - _ONE_SECOND,
            volume=volume,
            delay=prices.pop("delay", 0),
            **prices,
        )
    )
