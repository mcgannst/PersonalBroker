"""Market data types. Candle start and end times are UTC-aware datetimes."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Interval = Literal["OneMinute", "FiveMinutes", "FifteenMinutes", "OneHour", "OneDay"]
INTERVAL_CODES: dict[Interval, str] = {
    "OneMinute": "1m",
    "FiveMinutes": "5m",
    "FifteenMinutes": "15m",
    "OneHour": "1h",
    "OneDay": "1d",
}


@dataclass(frozen=True, slots=True)
class Candle:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One row of a session's universe (universe_snapshots joined to symbols); symbol_id is symbols.id."""

    symbol_id: int
    ticker: str
    name: str | None
    price: Decimal | None
    avg_volume: int | None
    atr14: Decimal | None
    source: str  # finviz | fallback | manual


@dataclass(frozen=True, slots=True)
class UniverseStatus:
    """Where a session's universe came from. `stale` is the nightly job's verdict on a fallback universe."""

    source: str | None  # None: no universe stored for the session
    fallback_from: date | None
    stale: bool
    age_sessions: int | None


@dataclass(frozen=True, slots=True)
class OpenBarStats:
    symbol_id: int
    avg_open_vol_14d: Decimal | None  # None with too few opening bars (P1-T9)
    atr14: Decimal | None


@dataclass(frozen=True, slots=True)
class OpeningBars:
    bars: dict[int, Candle]
    # symbol_id -> reason (Review Focus 4: reported, not raised): "no_questrade_id", "no_bar_at_open",
    # "bar_not_complete", "timeout" or "questrade_error: HTTP <status>"; from quotes (QUOTEBAR) also
    # "no_quote", "no_trade", "quote_incomplete", "quote_delayed".
    missing: dict[int, str]
    # QUOTEBAR: symbol_id -> "quotes" | "candles", where each bar came from. Filled only by the live scan's
    # quotes mode; empty (unknown: candles) everywhere else, so replay and the candle path are unchanged.
    sources: dict[int, str] = field(default_factory=dict)
