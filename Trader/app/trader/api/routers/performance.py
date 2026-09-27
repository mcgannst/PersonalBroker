"""GET /api/metrics, GET /api/equity, GET /api/export/trades.csv (BR-51, BR-52, BR-62; SPEC §11, §12).

Every route takes `run` (`live` or an existing run id in ASCII digits, else 404) and an optional inclusive
`from`/`to` date range (trades and journal days by session date, equity snapshots by their America/New_York
date). The CSV export is closed as soon as its response ends (`close_when_done`).

Metrics are simple SQL for now (P5-T1 may move them into `trader/reports/metrics.py` behind this route):
- Without a range: the run's `v_trade_metrics` row, plus `total_pnl` and the R histogram.
- With a range: one query with the view's formulas over the run's trades, journal and snapshots in range.

**R histogram wire format.** 18 bins, always all of them (zero counts included), ordered: an open-ended first
bin (below -3 R), sixteen bins of 0.5 R covering [-3, 5), and an open-ended last bin (5 R and above). Each
closed bin is [lo, hi). `HistogramBinOut.lo`/`hi` are Decimals, so the open ends are the Decimal sentinels
`-Infinity` (first bin's `lo`) and `Infinity` (last bin's `hi`), serialised as the JSON strings
`"-Infinity"` and `"Infinity"`. The web (`pages/performance/RHistogram.tsx`, `histogramLabel`) parses a
non-finite bound as open and labels those bins `< -3.0` and `≥ 5.0`. `HistogramBinOut.lo`/`hi` allow the
two sentinels (`allow_inf_nan`, P4-T18), so the metrics route returns a normal `MetricsOut` and a client can
validate it again from JSON.
"""

from collections.abc import AsyncGenerator, Generator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

import anyio
import anyio.to_thread
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Date, bindparam, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import iterate_in_threadpool

from trader.api.deps import CurrentUser, Services, resolve_run
from trader.api.errors import ApiError
from trader.api.schemas import EquityOut, EquityPointOut, HistogramBinOut, MetricsOut
from trader.reports.export import trades_csv

router = APIRouter(tags=["performance"])

MAX_EQUITY_POINTS = 5000

# The histogram: 16 bins of 0.5 R over [-3, 5) plus the open-ended ends (SQL width_bucket 0 and 17).
R_LOW, R_HIGH, R_BINS = Decimal("-3.0"), Decimal("5.0"), 16
R_WIDTH = (R_HIGH - R_LOW) / R_BINS
OPEN_LOW, OPEN_HIGH = Decimal("-Infinity"), Decimal("Infinity")

RunId = Annotated[int, Depends(resolve_run)]
DateFrom = Annotated[date | None, Query(alias="from")]
DateTo = Annotated[date | None, Query(alias="to")]

# Plain SQL literals (no string building). A range bound given as NULL is open. Trades and journal days are
# ranged by session date, equity snapshots by their America/New_York date.
_VIEW_SQL = """
SELECT v.*, (SELECT coalesce(sum(pnl), 0) FROM trader.trades WHERE run_id = :run) AS total_pnl
FROM trader.v_trade_metrics v WHERE v.run_id = :run
"""

# The view's formulas (migration 0002, v_trade_metrics) over one run and a date range.
_RANGED_SQL = """
WITH t AS (
    SELECT count(*) AS trades,
           count(*) FILTER (WHERE pnl > 0) AS wins,
           avg(pnl_r) FILTER (WHERE pnl > 0) AS avg_win_r,
           avg(pnl_r) FILTER (WHERE pnl <= 0) AS avg_loss_r,
           avg(pnl_r) AS expectancy_r,
           sum(pnl) FILTER (WHERE pnl > 0) AS gross_win,
           -sum(pnl) FILTER (WHERE pnl < 0) AS gross_loss,
           avg(slippage_total) AS avg_slippage,
           coalesce(sum(pnl), 0) AS total_pnl
    FROM trader.trades
    WHERE run_id = :run
      AND (CAST(:d_from AS date) IS NULL OR session_date >= :d_from)
      AND (CAST(:d_to AS date) IS NULL OR session_date <= :d_to)
), d AS (
    SELECT max(drawdown_pct) AS max_drawdown_pct
    FROM trader.equity_snapshots
    WHERE run_id = :run
      AND (CAST(:d_from AS date) IS NULL OR (ts AT TIME ZONE 'America/New_York')::date >= :d_from)
      AND (CAST(:d_to AS date) IS NULL OR (ts AT TIME ZONE 'America/New_York')::date <= :d_to)
), j AS (
    SELECT count(*) FILTER (WHERE rules_followed) AS followed, count(rules_followed) AS answered
    FROM trader.journal
    WHERE run_id = :run
      AND (CAST(:d_from AS date) IS NULL OR session_date >= :d_from)
      AND (CAST(:d_to AS date) IS NULL OR session_date <= :d_to)
)
SELECT t.trades,
       t.wins,
       CASE WHEN t.trades > 0 THEN round(t.wins::numeric / t.trades, 4) END AS win_rate,
       round(t.avg_win_r, 4) AS avg_win_r,
       round(t.avg_loss_r, 4) AS avg_loss_r,
       round(t.expectancy_r, 4) AS expectancy_r,
       CASE WHEN t.gross_loss > 0 THEN round(coalesce(t.gross_win, 0) / t.gross_loss, 4) END AS profit_factor,
       round(t.avg_slippage, 4) AS avg_slippage,
       d.max_drawdown_pct,
       CASE WHEN j.answered > 0 THEN round(j.followed::numeric / j.answered, 4) END AS adherence_pct,
       t.total_pnl
FROM t, d, j
"""

# width_bucket's bounds are R_LOW, R_HIGH and R_BINS: bucket 0 is below -3 R, 17 is 5 R and above.
_HISTOGRAM_SQL = """
SELECT width_bucket(pnl_r, -3.0, 5.0, 16) AS bucket, count(*) AS n
FROM trader.trades
WHERE run_id = :run AND pnl_r IS NOT NULL
      AND (CAST(:d_from AS date) IS NULL OR session_date >= :d_from)
      AND (CAST(:d_to AS date) IS NULL OR session_date <= :d_to)
GROUP BY bucket
"""

_EQUITY_SQL = """
SELECT ts, equity, cash, settled_cash, peak_equity, drawdown_pct
FROM trader.equity_snapshots
WHERE run_id = :run
      AND (CAST(:d_from AS date) IS NULL OR (ts AT TIME ZONE 'America/New_York')::date >= :d_from)
      AND (CAST(:d_to AS date) IS NULL OR (ts AT TIME ZONE 'America/New_York')::date <= :d_to)
ORDER BY ts
"""

_METRIC_FIELDS = (
    "trades",
    "wins",
    "win_rate",
    "avg_win_r",
    "avg_loss_r",
    "expectancy_r",
    "profit_factor",
    "avg_slippage",
    "max_drawdown_pct",
    "adherence_pct",
    "total_pnl",
)


def check_range(date_from: date | None, date_to: date | None) -> None:
    """422 when `from` is after `to` (shared with the journal router)."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ApiError(
            422, "validation", "from must be on or before to", [{"loc": ["query", "from"], "msg": "after to"}]
        )


def _ranged(sql: str) -> Any:
    return text(sql).bindparams(bindparam("d_from", type_=Date()), bindparam("d_to", type_=Date()))


def histogram_bins(counts: Mapping[int, int]) -> list[HistogramBinOut]:
    """All 18 bins from `width_bucket` counts (bucket 0 = below -3 R, 1..16 the 0.5 R bins, 17 = 5 R and
    above). The open ends are the Decimal sentinels `-Infinity`/`Infinity` (see the module docstring)."""
    bins = [HistogramBinOut(lo=OPEN_LOW, hi=R_LOW, count=counts.get(0, 0))]
    for b in range(1, R_BINS + 1):
        lo = R_LOW + R_WIDTH * (b - 1)
        bins.append(HistogramBinOut(lo=lo, hi=lo + R_WIDTH, count=counts.get(b, 0)))
    bins.append(HistogramBinOut(lo=R_HIGH, hi=OPEN_HIGH, count=counts.get(R_BINS + 1, 0)))
    return bins


def compute_metrics(
    factory: sessionmaker[Session], run_id: int, date_from: date | None, date_to: date | None
) -> MetricsOut:
    """The run's metrics (the view without a range, the ranged query with one) and its R histogram."""
    params = {"run": run_id, "d_from": date_from, "d_to": date_to}
    with factory() as s:
        if date_from is None and date_to is None:
            row = s.execute(text(_VIEW_SQL), {"run": run_id}).mappings().one()
        else:
            row = s.execute(_ranged(_RANGED_SQL), params).mappings().one()
        counts = {int(r.bucket): int(r.n) for r in s.execute(_ranged(_HISTOGRAM_SQL), params)}
    return MetricsOut(
        run_id=run_id,
        from_date=date_from,
        to_date=date_to,
        r_histogram=histogram_bins(counts),
        **{k: row[k] for k in _METRIC_FIELDS},
    )


def thin[T](items: Sequence[T], limit: int) -> list[T]:
    """At most `limit` items, evenly spaced, keeping the first and the last."""
    n = len(items)
    if n <= limit:
        return list(items)
    if limit < 2:
        return list(items[:limit])
    return [items[(i * (n - 1) + (limit - 1) // 2) // (limit - 1)] for i in range(limit)]


@router.get("/metrics")
def metrics(
    _user: CurrentUser, services: Services, run_id: RunId, date_from: DateFrom = None, date_to: DateTo = None
) -> MetricsOut:
    check_range(date_from, date_to)
    return compute_metrics(services.core.factory, run_id, date_from, date_to)


@router.get("/equity")
def equity(
    _user: CurrentUser, services: Services, run_id: RunId, date_from: DateFrom = None, date_to: DateTo = None
) -> EquityOut:
    check_range(date_from, date_to)
    params = {"run": run_id, "d_from": date_from, "d_to": date_to}
    with services.core.factory() as s:
        rows = list(s.execute(_ranged(_EQUITY_SQL), params).mappings())
    points = [EquityPointOut(**r) for r in thin(rows, MAX_EQUITY_POINTS)]
    return EquityOut(run_id=run_id, points=points)


async def close_when_done(lines: Generator[str, None, None]) -> AsyncGenerator[str, None]:
    """Stream a blocking line generator from worker threads, and close it on every exit (the end, an error,
    a client disconnect), so an unfinished export releases its cursor and transaction at once instead of
    whenever the generator is garbage-collected."""
    try:
        async for line in iterate_in_threadpool(lines):
            yield line
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(lines.close)


@router.get("/export/trades.csv", response_class=StreamingResponse)
def export_trades(
    _user: CurrentUser, services: Services, run_id: RunId, date_from: DateFrom = None, date_to: DateTo = None
) -> StreamingResponse:
    check_range(date_from, date_to)
    name = f"trades-{run_id}-{date_from or 'all'}-{date_to or 'all'}.csv"
    return StreamingResponse(
        close_when_done(trades_csv(services.core.factory, run_id, date_from, date_to)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
