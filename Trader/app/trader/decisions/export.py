"""The decision log CSV export (stub from P6-T9; P6-T12 implements it).

`decisions_csv` streams one run's day like `trader.reports.export.trades_csv` (server-side cursor, closed on
early close), UTC ISO times, decimals as stored, the spreadsheet-formula guard on every text cell. `checks` is
`name value op threshold pass` per check, joined by `; `.
"""

from collections.abc import Generator
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

DECISION_CSV_COLUMNS: tuple[str, ...] = (
    "run_id",
    "run_mode",
    "session_date",
    "seq",
    "ts",
    "stage",
    "strategy",
    "ticker",
    "outcome",
    "rule",
    "reason",
    "checks",
    "rvol",
    "rank",
    "or_high",
    "or_low",
    "entry",
    "stop_loss",
    "target",
    "r_per_share",
    "qty",
    "risk_dollars",
    "est_cost",
    "catalyst_type",
    "catalyst_quality",
    "catalyst_reason",
    "decided_via",
    "decision_latency_ms",
    "planned_price",
    "fill_price",
    "diff_per_share",
    "slippage",
    "pnl",
    "pnl_r",
    "exit_category",
)


def decisions_csv(
    factory: sessionmaker[Session], run_id: int, session_date: date
) -> Generator[str, None, None]:
    raise NotImplementedError("P6-T12: decisions_csv")
