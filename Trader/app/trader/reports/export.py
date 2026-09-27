"""The trades CSV export (BR-62, SPEC §11 `/export/trades.csv`). P5-T6 extends this module.

Stub (P4-T1): T7 implements `trades_csv`; `TRADE_CSV_COLUMNS` is the plan's column list.
"""

from collections.abc import Iterator
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

TRADE_CSV_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "session_date",
    "ticker",
    "strategy",
    "qty",
    "entry_price",
    "exit_price",
    "pnl",
    "pnl_r",
    "planned_risk",
    "fees_total",
    "slippage_total",
    "exit_reason",
    "opened_at",
    "closed_at",
)


def trades_csv(
    factory: sessionmaker[Session], run_id: int, date_from: date | None, date_to: date | None
) -> Iterator[str]:
    """CSV lines, the header first; UTC ISO times; Decimals as stored."""
    raise NotImplementedError("P4-T7")
