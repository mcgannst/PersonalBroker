"""GET /api/metrics, GET /api/equity, GET /api/export/trades.csv (BR-51, BR-52, BR-62; SPEC §11, §12).

Every route takes `run` (`live` or an existing run id in ASCII digits, else 404) and an optional inclusive
`from`/`to` date range (trades and journal days by session date, equity snapshots by their America/New_York
date). The CSV export is closed as soon as its response ends (`close_when_done`).

Metrics come from `trader.reports.metrics` (P5-T2), the one definition shared with the replay comparison,
the daily run-to-date line and the weekly report: computed in Python from the run's rows in range. For a
whole run they equal `v_trade_metrics`; the view stays for SQL users.

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
from trader.api.views import metrics_out
from trader.reports import metrics as metrics_module
from trader.reports.export import trades_csv

router = APIRouter(tags=["performance"])

MAX_EQUITY_POINTS = 5000

RunId = Annotated[int, Depends(resolve_run)]
DateFrom = Annotated[date | None, Query(alias="from")]
DateTo = Annotated[date | None, Query(alias="to")]

# Plain SQL literal (no string building). A range bound given as NULL is open; equity snapshots are ranged
# by their America/New_York date.
_EQUITY_SQL = """
SELECT ts, equity, cash, settled_cash, peak_equity, drawdown_pct
FROM trader.equity_snapshots
WHERE run_id = :run
      AND (CAST(:d_from AS date) IS NULL OR (ts AT TIME ZONE 'America/New_York')::date >= :d_from)
      AND (CAST(:d_to AS date) IS NULL OR (ts AT TIME ZONE 'America/New_York')::date <= :d_to)
ORDER BY ts
"""


def check_range(date_from: date | None, date_to: date | None) -> None:
    """422 when `from` is after `to` (shared with the journal router)."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ApiError(
            422, "validation", "from must be on or before to", [{"loc": ["query", "from"], "msg": "after to"}]
        )


def _ranged(sql: str) -> Any:
    return text(sql).bindparams(bindparam("d_from", type_=Date()), bindparam("d_to", type_=Date()))


def histogram_bins(counts: Mapping[int, int]) -> list[HistogramBinOut]:
    """All 18 bins from `width_bucket`-style counts (bucket 0 = below -3 R, 1..16 the 0.5 R bins, 17 = 5 R
    and above), as `trader.reports.metrics.histogram_from_counts`. The open ends are the Decimal sentinels
    `-Infinity`/`Infinity` (see the module docstring)."""
    return [
        HistogramBinOut(lo=b.lo, hi=b.hi, count=b.count) for b in metrics_module.histogram_from_counts(counts)
    ]


def compute_metrics(
    factory: sessionmaker[Session], run_id: int, date_from: date | None, date_to: date | None
) -> MetricsOut:
    """The run's metrics in range as the API model (`trader.reports.metrics.compute_metrics`)."""
    return metrics_out(metrics_module.compute_metrics(factory, run_id, date_from, date_to))


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
