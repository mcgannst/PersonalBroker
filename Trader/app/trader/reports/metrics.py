"""One definition of every performance number (BR-52, O4; SPEC §10 `v_trade_metrics`, §11 `/metrics`).

Computed in Python from the rows, per run and inclusive date range; ratios rounded to 4 dp half-up, money
summed exactly. Used by `/api/metrics`, the replay comparison, the daily run-to-date line and the weekly
report. The dataclasses are the P5-T1 contract; the functions are P5-T2's.

Definitions (the view's where both define a value, so for a whole run they equal `v_trade_metrics`):
- wins are trades with `pnl > 0`, losses `pnl <= 0`; `win_rate` = wins / trades.
- `expectancy_r`, `avg_win_r`, `avg_loss_r` average `pnl_r` over the trades that have one;
  `trades_without_r` counts the others, so a missing planned risk is visible.
- `profit_factor` = gross win / gross loss, None without losing P&L.
- `avg_slippage` = mean `slippage_total` (dollars per trade); `avg_slippage_per_share` = sum / sum qty.
- `max_drawdown_pct`: the largest (peak - equity) / peak with the running peak starting at the first
  snapshot in range (for a whole run the stored `drawdown_pct` maximum, which uses the same peak).
- `adherence_pct` = followed / answered journal days.
Every ratio is rounded to 4 dp half-up (away from zero, as Postgres `round`); money is summed exactly.

R histogram (the P4-T7 wire format): 18 bins, an open-ended first bin below `R_LOW` (its `lo` is
`Decimal("-Infinity")`), sixteen `[lo, hi)` bins of 0.5 R, and an open-ended last bin from `R_HIGH` (its `hi`
is `Decimal("Infinity")`).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m

R_LOW = Decimal("-3.0")
R_HIGH = Decimal("5.0")
R_BINS = 16
OPEN_LOW = Decimal("-Infinity")
OPEN_HIGH = Decimal("Infinity")
R_WIDTH = (R_HIGH - R_LOW) / R_BINS  # 0.5
Q4 = Decimal("0.0001")
ZERO = Decimal(0)
ET_ZONE = "America/New_York"


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


def _q4(value: Decimal) -> Decimal:
    return value.quantize(Q4, ROUND_HALF_UP)


def _ratio(num: Decimal, den: Decimal) -> Decimal | None:
    return _q4(num / den) if den else None


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return _q4(sum(values, ZERO) / len(values)) if values else None


def metrics_from_rows(
    run_id: int,
    date_from: date | None,
    date_to: date | None,
    trades: Sequence[TradeRow],
    equity: Sequence[Decimal],
    answers: Sequence[bool | None],
) -> Metrics:
    """Pure: every metric from the rows (trades ordered by close, equity by time, journal answers)."""
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    with_r = [t.pnl_r for t in trades if t.pnl_r is not None]
    gross_win = sum((t.pnl for t in trades if t.pnl > 0), ZERO)
    gross_loss = -sum((t.pnl for t in trades if t.pnl < 0), ZERO)
    answered = [a for a in answers if a is not None]
    qty = sum(t.qty for t in trades)
    slippage = sum((t.slippage_total for t in trades), ZERO)
    return Metrics(
        run_id=run_id,
        date_from=date_from,
        date_to=date_to,
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=_ratio(Decimal(len(wins)), Decimal(len(trades))),
        avg_win_r=_mean([t.pnl_r for t in wins if t.pnl_r is not None]),
        avg_loss_r=_mean([t.pnl_r for t in losses if t.pnl_r is not None]),
        expectancy_r=_mean(with_r),
        profit_factor=_ratio(gross_win, gross_loss) if gross_loss > 0 else None,
        avg_slippage=_mean([t.slippage_total for t in trades]),
        avg_slippage_per_share=_ratio(slippage, Decimal(qty)) if qty > 0 else None,
        max_drawdown_pct=max_drawdown(equity),
        adherence_pct=_ratio(Decimal(sum(1 for a in answered if a)), Decimal(len(answered))),
        total_pnl=sum((t.pnl for t in trades), ZERO),
        total_fees=sum((t.fees_total for t in trades), ZERO),
        trades_without_r=len(trades) - len(with_r),
        r_histogram=r_histogram(with_r),
    )


def _bucket(value: Decimal) -> int:
    """Postgres `width_bucket(value, R_LOW, R_HIGH, R_BINS)`: 0 below R_LOW, R_BINS + 1 from R_HIGH."""
    if value < R_LOW:
        return 0
    if value >= R_HIGH:
        return R_BINS + 1
    return int(((value - R_LOW) / R_WIDTH).to_integral_value(ROUND_FLOOR)) + 1


def histogram_from_counts(counts: Mapping[int, int]) -> tuple[HistogramBin, ...]:
    """All 18 bins from bucket counts (0 = below R_LOW, 1..R_BINS the 0.5 R bins, R_BINS + 1 = R_HIGH up)."""
    bins = [HistogramBin(lo=OPEN_LOW, hi=R_LOW, count=counts.get(0, 0))]
    for b in range(1, R_BINS + 1):
        lo = R_LOW + R_WIDTH * (b - 1)
        bins.append(HistogramBin(lo=lo, hi=lo + R_WIDTH, count=counts.get(b, 0)))
    bins.append(HistogramBin(lo=R_HIGH, hi=OPEN_HIGH, count=counts.get(R_BINS + 1, 0)))
    return tuple(bins)


def r_histogram(values: Iterable[Decimal]) -> tuple[HistogramBin, ...]:
    """Pure: the 18 bins of the P4-T7 wire format over the given R values."""
    counts: dict[int, int] = {}
    for v in values:
        b = _bucket(v)
        counts[b] = counts.get(b, 0) + 1
    return histogram_from_counts(counts)


def max_drawdown(equity: Sequence[Decimal]) -> Decimal | None:
    """Pure: the largest (peak - equity) / peak with a running peak from the first value; None when empty."""
    if not equity:
        return None
    peak = equity[0]
    worst = ZERO
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return _q4(worst)


def compute_metrics(
    factory: sessionmaker[Session], run_id: int, date_from: date | None = None, date_to: date | None = None
) -> Metrics:
    """The run's metrics over the inclusive range (trades and journal by session date, equity snapshots by
    their America/New_York date)."""
    t, j, e = m.Trade, m.Journal, m.EquitySnapshot
    trade_q = (
        select(t.pnl, t.pnl_r, t.qty, t.slippage_total, t.fees_total)
        .where(t.run_id == run_id)
        .order_by(t.closed_at, t.id)
    )
    journal_q = select(j.rules_followed).where(j.run_id == run_id).order_by(j.session_date)
    et_day = cast(func.timezone(ET_ZONE, e.ts), Date)
    equity_q = select(e.equity).where(e.run_id == run_id).order_by(e.ts)
    if date_from is not None:
        trade_q = trade_q.where(t.session_date >= date_from)
        journal_q = journal_q.where(j.session_date >= date_from)
        equity_q = equity_q.where(et_day >= date_from)
    if date_to is not None:
        trade_q = trade_q.where(t.session_date <= date_to)
        journal_q = journal_q.where(j.session_date <= date_to)
        equity_q = equity_q.where(et_day <= date_to)
    with factory() as s:
        trades = [
            TradeRow(
                pnl=r.pnl, pnl_r=r.pnl_r, qty=r.qty, slippage_total=r.slippage_total, fees_total=r.fees_total
            )
            for r in s.execute(trade_q)
        ]
        equity = list(s.execute(equity_q).scalars())
        answers = list(s.execute(journal_q).scalars())
    return metrics_from_rows(run_id, date_from, date_to, trades, equity, answers)
