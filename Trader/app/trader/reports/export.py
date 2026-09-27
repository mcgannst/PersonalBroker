"""The trades CSV export (BR-62, SPEC §11 `/export/trades.csv`). P5-T6 extends this module.

`trades_csv` yields whole CSV lines (header first), so the API can stream them. One run only, optionally a
session-date range (inclusive). Times are UTC ISO (`...Z`), Decimals exactly as stored, a missing value is
an empty cell. Text cells that a spreadsheet would read as a formula (starting with `=`, `+`, `-`, `@`, a tab
or a carriage return) get a leading `'` (CSV injection guard); numbers are never changed.

Rows are read through a server-side cursor. Closing the generator early (the API closes it when a client
disconnects) closes the cursor and ends the read transaction at once.
"""

import csv
import io
from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m

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

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_BATCH = 500


def safe_cell(text: str) -> str:
    """A text cell a spreadsheet will not evaluate: `'` before a leading formula character."""
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, (date, Decimal, int)):
        return str(value)
    return safe_cell(str(value))


def _line(values: tuple[object, ...] | list[object]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerow(values)
    return buf.getvalue()


def trades_csv(
    factory: sessionmaker[Session], run_id: int, date_from: date | None, date_to: date | None
) -> Generator[str, None, None]:
    """CSV lines, the header first; UTC ISO times; Decimals as stored."""
    yield _line(TRADE_CSV_COLUMNS)
    t = m.Trade
    stmt = (
        select(
            t.id,
            t.session_date,
            m.Symbol.ticker,
            m.StrategyConfig.strategy_key,
            t.qty,
            t.entry_price,
            t.exit_price,
            t.pnl,
            t.pnl_r,
            t.planned_risk,
            t.fees_total,
            t.slippage_total,
            t.exit_reason,
            t.opened_at,
            t.closed_at,
        )
        .join(m.Symbol, m.Symbol.id == t.symbol_id)
        .join(m.Position, m.Position.id == t.position_id)
        .outerjoin(m.StrategyConfig, m.StrategyConfig.id == m.Position.strategy_config_id)
        .where(t.run_id == run_id)
        .order_by(t.closed_at, t.id)
        .execution_options(yield_per=_BATCH)
    )
    if date_from is not None:
        stmt = stmt.where(t.session_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(t.session_date <= date_to)
    with factory() as s:
        result = s.execute(stmt)
        try:
            for row in result:
                yield _line([_cell(v) for v in row])
        finally:  # also on close() of an unfinished export: the cursor, then (with) the transaction
            result.close()
