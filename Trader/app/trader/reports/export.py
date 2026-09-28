"""The trades CSV export (BR-62, SPEC §11 `/export/trades.csv`).

`trades_csv` yields whole CSV lines (header first), so the API can stream them. One run only, optionally a
session-date range (inclusive). Times are UTC ISO (`...Z`), Decimals exactly as stored, a missing value is
an empty cell.

After the P4 fifteen columns, P5-T12 adds seven that make each row traceable: the run's sim account
`currency`, the position's strategy config `strategy_version`, `config_revision` and `config_scope`
(`live` or `replay`), the position's `stop_loss` and `unprotected_seconds`, and the run's `run_mode`
(`live` or `replay`). A trade without a config or a run without a sim account leaves those cells empty.

Text cells that a spreadsheet would read as a formula (starting with `=`, `+`, `-`, `@`, a tab or a carriage
return) get a leading `'` (CSV injection guard), the new text columns included; numbers are never changed.

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
    # P5-T12 (BR-62): traceability
    "currency",
    "strategy_version",
    "config_revision",
    "config_scope",
    "stop_loss",
    "unprotected_seconds",
    "run_mode",
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


# Public aliases (P6-T12): the decision log export (`trader.decisions.export`) shares this formula guard.
csv_cell = _cell
csv_line = _line


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
            m.SimAccount.currency,
            m.StrategyConfig.version,
            m.StrategyConfig.revision,
            m.StrategyConfig.scope,
            m.Position.stop_loss,
            m.Position.unprotected_seconds,
            m.Run.mode,
        )
        .join(m.Symbol, m.Symbol.id == t.symbol_id)
        .join(m.Position, m.Position.id == t.position_id)
        .join(m.Run, m.Run.id == t.run_id)
        .outerjoin(m.StrategyConfig, m.StrategyConfig.id == m.Position.strategy_config_id)
        .outerjoin(m.SimAccount, m.SimAccount.run_id == t.run_id)  # unique per run: at most one row
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
