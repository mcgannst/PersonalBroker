"""One definition of every performance number (BR-52, O4; SPEC §10 `v_trade_metrics`, §11 `/metrics`).

Computed in Python from the rows, per run and inclusive date range; ratios rounded to 4 dp half-up, money
summed exactly. Used by `/api/metrics`, the replay comparison, the daily run-to-date line and the weekly
report. The dataclasses are the P5-T1 contract; the functions are implemented by P5-T2.

R histogram (the P4-T7 wire format): 18 bins, an open-ended first bin below `R_LOW` (its `lo` is
`Decimal("-Infinity")`), sixteen `[lo, hi)` bins of 0.5 R, and an open-ended last bin from `R_HIGH` (its `hi`
is `Decimal("Infinity")`).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

R_LOW = Decimal("-3.0")
R_HIGH = Decimal("5.0")
R_BINS = 16
OPEN_LOW = Decimal("-Infinity")
OPEN_HIGH = Decimal("Infinity")


@dataclass(frozen=True, slots=True)
class TradeRow:
    pnl: Decimal
    pnl_r: Decimal | None
    qty: int
    slippage_total: Decimal
    fees_total: Decimal


@dataclass(frozen=True, slots=True)
class HistogramBin:
    lo: Decimal  # OPEN_LOW for the first bin
    hi: Decimal  # OPEN_HIGH for the last bin
    count: int


@dataclass(frozen=True, slots=True)
class Metrics:
    run_id: int
    date_from: date | None
    date_to: date | None
    trades: int
    wins: int
    losses: int
    win_rate: Decimal | None
    avg_win_r: Decimal | None
    avg_loss_r: Decimal | None
    expectancy_r: Decimal | None
    profit_factor: Decimal | None
    avg_slippage: Decimal | None  # dollars per trade, as v_trade_metrics
    avg_slippage_per_share: Decimal | None
    max_drawdown_pct: Decimal | None
    adherence_pct: Decimal | None
    total_pnl: Decimal
    total_fees: Decimal
    trades_without_r: int
    r_histogram: tuple[HistogramBin, ...]


def metrics_from_rows(
    run_id: int,
    date_from: date | None,
    date_to: date | None,
    trades: Sequence[TradeRow],
    equity: Sequence[Decimal],
    answers: Sequence[bool | None],
) -> Metrics:
    """Pure: every metric from the rows (trades ordered by close, equity by time, journal answers)."""
    raise NotImplementedError("P5-T2")


def r_histogram(values: Iterable[Decimal]) -> tuple[HistogramBin, ...]:
    """Pure: the 18 bins of the P4-T7 wire format over the given R values."""
    raise NotImplementedError("P5-T2")


def max_drawdown(equity: Sequence[Decimal]) -> Decimal | None:
    """Pure: the largest (peak - equity) / peak with a running peak from the first value; None when empty."""
    raise NotImplementedError("P5-T2")


def compute_metrics(
    factory: sessionmaker[Session], run_id: int, date_from: date | None = None, date_to: date | None = None
) -> Metrics:
    """The run's metrics over the inclusive range (trades and journal by session date, equity snapshots by
    their America/New_York date)."""
    raise NotImplementedError("P5-T2")
