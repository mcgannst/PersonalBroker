"""Tiny row builders for DB tests. Each flushes and returns the new primary key; the caller commits."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from trader.db import models as m

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


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
