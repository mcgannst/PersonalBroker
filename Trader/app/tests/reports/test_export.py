"""P4-T7 acceptance test 5 (the export side): `trader.reports.export.trades_csv`."""

import csv
import io
from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from tests.reports import add_trade
from trader.reports.export import TRADE_CSV_COLUMNS, safe_cell, trades_csv

D1, D2, D3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)


def _rows(lines: list[str]) -> list[list[str]]:
    return list(csv.reader(io.StringIO("".join(lines))))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=cmd()", "'=cmd()"),
        ("+1", "'+1"),
        ("-2", "'-2"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
        ("target", "target"),
        ("", ""),
    ],
)
def test_safe_cell_guards_formula_starts(value: str, expected: str) -> None:
    assert safe_cell(value) == expected


@pytest.mark.db
def test_trades_csv_header_rows_range_and_run(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        other = add_run(s, mode="replay", status="completed")
        sym = add_symbol(s, "AAA")
        cfg = add_strategy_config(s, "orb_sip")
        t1 = add_trade(s, run, sym, D1, "20.0000", "2.0000", config_id=cfg)
        t2 = add_trade(s, run, sym, D2, "-10.0000", "-1.0000", config_id=cfg, exit_reason="=cmd()")
        add_trade(s, run, sym, D3, "5.0000", None, config_id=None, planned_risk=None)
        add_trade(s, other, sym, D2, "99.0000", "9.9000", config_id=cfg)
        s.commit()

    everything = _rows(list(trades_csv(db_factory, run, None, None)))
    assert tuple(everything[0]) == TRADE_CSV_COLUMNS
    assert len(everything) == 4  # header + the run's three trades, never the other run's
    by = {r[0]: dict(zip(TRADE_CSV_COLUMNS, r, strict=True)) for r in everything[1:]}
    first = by[str(t1)]
    assert (
        first["session_date"] == "2026-10-05" and first["ticker"] == "AAA" and first["strategy"] == "orb_sip"
    )
    assert (first["pnl"], first["pnl_r"], first["entry_price"]) == ("20.0000", "2.0000", "20.0000")
    assert first["opened_at"] == "2026-10-05T14:00:00Z" and first["closed_at"] == "2026-10-05T15:00:00Z"
    second = by[str(t2)]
    assert second["exit_reason"] == "'=cmd()"
    assert second["pnl"] == "-10.0000"  # numbers are never prefixed
    third = [r for r in by.values() if r["session_date"] == "2026-10-07"][0]
    assert third["pnl_r"] == "" and third["planned_risk"] == "" and third["strategy"] == ""

    ranged = _rows(list(trades_csv(db_factory, run, D2, D2)))
    assert [r[0] for r in ranged[1:]] == [str(t2)]
    assert _rows(list(trades_csv(db_factory, run, date(2026, 11, 1), None))) == [list(TRADE_CSV_COLUMNS)]


@pytest.mark.db
def test_trades_csv_lines_are_whole_rows(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        sym = add_symbol(s, "BBB")
        add_trade(s, run, sym, D1, "1.0000", "0.1000", exit_reason='stop, "hit"\nline')
        s.commit()
    lines = list(trades_csv(db_factory, run, None, None))
    assert len(lines) == 2 and all(line.endswith("\r\n") for line in lines)
    assert _rows(lines)[1][TRADE_CSV_COLUMNS.index("exit_reason")] == 'stop, "hit"\nline'
