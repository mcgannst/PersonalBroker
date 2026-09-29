"""Constants and internal types shared by the live data modules (DB-T1 contracts, final). Thresholds are
module constants, never settings keys (soak safety: no settings_store change)."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trader.api.schemas import LivePositionOut, PeriodKey

MARK_STALE_SECONDS = 30
HEARTBEAT_BADGE_SECONDS = 60
SPARK_POINTS = 60
EQUITY_MAX_POINTS = 500
FILL_MARKERS_MAX = 200
ACTIVITY_LIMIT = 100
REJECTION_TICKERS_MAX = 50
EXPAND_MAX = 3
NEAR_STOP_R = Decimal("0.25")
ERRORS_SHOWN = 200
SOAK_CACHE_SECONDS = 60.0
PART_MESSAGE_CHARS = 120
LIVE_MAX_STATEMENTS = 80  # S14 ceiling per GET /api/live (may only be lowered)


@dataclass(frozen=True, slots=True)
class PeriodWindow:
    key: PeriodKey
    date_from: date
    date_to: date
    start_at: datetime  # 00:00 ET of date_from, as UTC
    end_at: datetime  # 00:00 ET of date_to + 1 day, as UTC (exclusive)


@dataclass(frozen=True, slots=True)
class OpenValue:
    """One open position's contribution to P&L and equity."""

    position_id: int
    symbol_id: int
    qty: int
    avg_price: Decimal
    mark: Decimal | None
    entry_fees: Decimal  # round4 of its entry fill's fees.total


@dataclass(frozen=True, slots=True)
class LivePositions:
    positions: list[LivePositionOut]
    open_values: list[OpenValue]
    equity_at_marks: Decimal  # Σ cash_ledger + Σ (mark or avg_price) × qty
    all_marked: bool
