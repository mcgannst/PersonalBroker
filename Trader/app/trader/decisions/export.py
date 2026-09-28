"""The decision log CSV export (P6-T12, BR-62 extended to decisions).

`decisions_csv` streams one run's day like `trader.reports.export.trades_csv`: the header first (before any
query), then one line per `decision_log` row in `seq` order, read through a server-side cursor that is closed
at once when the generator is closed early (the API closes it when a client disconnects). Times are UTC ISO
(`...Z`); numbers are written exactly as stored (a JSON number or a decimal string such as `"-0.0200"`, never
prefixed); every other cell is text and gets the spreadsheet-formula guard of the trades export
(`trader.reports.export.csv_cell`: a leading `'` before `=`, `+`, `-`, `@`, a tab or a carriage return), the
number columns included when they hold something that is not a number.

`checks` is `name value op threshold pass` per check (`pass`/`fail`, `n/a` for a missing value), joined by
`; `. The other columns come from the row's `data`, by these keys (the recorder, P6-T10, writes them):
`rvol`, `rank`, `or_high`/`or_low` (or `opening_range.high`/`.low`), `entry`, `stop_loss`, `target`,
`r_per_share`, `qty`, `risk_dollars`, `est_cost`, the catalyst from `catalyst.{type,quality,reason}` (or
`catalyst_type`/`catalyst_quality`/`catalyst_reason`; a `premarket` row's own `type`, `quality` and its
`reason` column), `decided_via`, `decision_latency_ms`, `planned_price`, `fill_price`, `diff_per_share`,
`slippage`, `pnl`, `pnl_r`; `exit_category` is an `exit` row's `rule` (else `data.exit_category`).
"""

import json
import re
from collections.abc import Generator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.reports.export import csv_cell, csv_line, safe_cell

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

# The `data` keys copied as they are (numbers when they are numbers, else guarded text).
_NUMBER_KEYS = (
    "rvol",
    "rank",
    "entry",
    "stop_loss",
    "target",
    "r_per_share",
    "qty",
    "risk_dollars",
    "est_cost",
)
_FILL_KEYS = ("planned_price", "fill_price", "diff_per_share", "slippage", "pnl", "pnl_r")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")
_BATCH = 500
NA = "n/a"


def number_cell(value: Any) -> str:
    """A number exactly as stored; anything else as a guarded text cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, str) and _NUMBER.match(value):
        return value
    return text_cell(value)


def text_cell(value: Any) -> str:
    """A guarded text cell; a JSON object or list is written as compact JSON (guarded too)."""
    if value is None:
        return ""
    if isinstance(value, (Mapping, list)):
        return safe_cell(json.dumps(value, separators=(",", ":"), sort_keys=True, default=str))
    if isinstance(value, bool):
        return "true" if value else "false"
    return csv_cell(value)


def checks_cell(checks: Any) -> str:
    """`name value op threshold pass` per check, joined by `; ` (then guarded as text)."""
    if not isinstance(checks, Sequence) or isinstance(checks, str):
        return ""
    parts: list[str] = []
    for c in checks:
        if not isinstance(c, Mapping):
            continue
        passed = c.get("passed")
        verdict = NA if passed is None else ("pass" if passed else "fail")
        fields = (c.get("name"), c.get("value"), c.get("op"), c.get("threshold"))
        parts.append(" ".join([*(NA if f is None else str(f) for f in fields), verdict]))
    return text_cell("; ".join(parts)) if parts else ""


def _sub(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def _first(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


def row_cells(
    run_id: int,
    run_mode: str,
    session_date: date,
    seq: int,
    ts: Any,
    stage: str,
    strategy: str | None,
    ticker: str | None,
    outcome: str,
    rule: str | None,
    reason: str | None,
    data: Any,
) -> list[object]:
    """One CSV row's cells, in `DECISION_CSV_COLUMNS` order."""
    d: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
    opening = _sub(d, "opening_range")
    catalyst = _sub(d, "catalyst")
    premarket = stage == "premarket"
    cat_type = _first(catalyst.get("type"), d.get("catalyst_type"), d.get("type") if premarket else None)
    cat_quality = _first(
        catalyst.get("quality"), d.get("catalyst_quality"), d.get("quality") if premarket else None
    )
    cat_reason = _first(catalyst.get("reason"), d.get("catalyst_reason"), reason if premarket else None)
    exit_category = rule if stage == "exit" else d.get("exit_category")
    return [
        csv_cell(run_id),
        text_cell(run_mode),
        csv_cell(session_date),
        csv_cell(seq),
        csv_cell(ts),
        text_cell(stage),
        text_cell(strategy),
        text_cell(ticker),
        text_cell(outcome),
        text_cell(rule),
        text_cell(reason),
        checks_cell(d.get("checks")),
        number_cell(d.get("rvol")),
        number_cell(d.get("rank")),
        number_cell(_first(d.get("or_high"), opening.get("high"))),
        number_cell(_first(d.get("or_low"), opening.get("low"))),
        *(number_cell(d.get(k)) for k in _NUMBER_KEYS[2:]),
        text_cell(cat_type),
        number_cell(cat_quality),
        text_cell(cat_reason),
        text_cell(d.get("decided_via")),
        number_cell(d.get("decision_latency_ms")),
        *(number_cell(d.get(k)) for k in _FILL_KEYS),
        text_cell(exit_category),
    ]


def decisions_csv(
    factory: sessionmaker[Session], run_id: int, session_date: date
) -> Generator[str, None, None]:
    """CSV lines of one run's day, the header first; UTC ISO times; numbers as stored; text guarded."""
    yield csv_line(DECISION_CSV_COLUMNS)
    d = m.DecisionLog
    stmt = (
        select(
            d.run_id,
            m.Run.mode,
            d.session_date,
            d.seq,
            d.ts,
            d.stage,
            d.strategy_key,
            d.ticker,
            d.outcome,
            d.rule,
            d.reason,
            d.data,
        )
        .join(m.Run, m.Run.id == d.run_id)
        .where(d.run_id == run_id, d.session_date == session_date)
        .order_by(d.seq)
        .execution_options(yield_per=_BATCH)
    )
    with factory() as s:
        result = s.execute(stmt)
        try:
            for row in result:
                yield csv_line(row_cells(*row))
        finally:  # also on close() of an unfinished export: the cursor, then (with) the transaction
            result.close()
