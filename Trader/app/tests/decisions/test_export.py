"""P6-T12 acceptance tests 3 and 4: `trader.decisions.export.decisions_csv` (the decision log CSV)."""

import csv
import inspect
import io
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from tests.decisions.test_read import CHECKS, D1, D2, add_decision, seed_day
from tests.factories import add_run
from trader.decisions.export import DECISION_CSV_COLUMNS, decisions_csv
from trader.reports import export as trades_export

pytestmark = pytest.mark.db


def _rows(lines: list[str]) -> list[dict[str, str]]:
    parsed = list(csv.reader(io.StringIO("".join(lines))))
    assert tuple(parsed[0]) == DECISION_CSV_COLUMNS
    return [dict(zip(DECISION_CSV_COLUMNS, r, strict=True)) for r in parsed[1:]]


def test_the_csv_aliases_are_the_trades_export_guard() -> None:
    assert trades_export.csv_cell("=1+1") == "'=1+1"
    assert trades_export.csv_cell(None) == ""
    assert trades_export.csv_line(["a", "b,c"]) == 'a,"b,c"\r\n'


def test_header_rows_times_and_decimals(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        other = add_run(s, mode="replay", status="completed")
        seed_day(s, run, D2)
        seed_day(s, other, D2, prefix="R")  # another run: never in this run's export
        seed_day(s, run, D1, prefix="E")  # another day
        s.commit()
    rows = _rows(list(decisions_csv(db_factory, run, D2)))
    assert [r["seq"] for r in rows] == [str(i) for i in range(1, 10)]
    assert {r["run_id"] for r in rows} == {str(run)} and {r["run_mode"] for r in rows} == {"live"}
    assert {r["session_date"] for r in rows} == {"2026-10-06"}
    assert all(r["ts"].endswith("Z") for r in rows)
    assert rows[2]["ts"] == "2026-10-06T13:35:05Z"
    passed = rows[2]
    assert (passed["stage"], passed["strategy"], passed["ticker"], passed["outcome"]) == (
        "scan",
        "orb_sip",
        "NVDA",
        "passed",
    )
    assert passed["rvol"] == "3.20" and passed["rank"] == "1"  # decimals exactly as stored
    assert passed["entry"] == "10.2500" and passed["stop_loss"] == "9.8000"
    assert (
        passed["checks"] == "rvol 3.20 >= 1.00 pass; price 22.40 between 5-50 pass; atr14 n/a present n/a n/a"
    )
    assert rows[3]["rule"] == "rvol_below_min" and rows[3]["checks"] == "rvol 0.80 >= 1.00 fail"
    fill = rows[6]
    assert (fill["planned_price"], fill["fill_price"], fill["diff_per_share"]) == (
        "10.2500",
        "10.2700",
        "0.0200",
    )
    exit_ = rows[7]
    assert (exit_["exit_category"], exit_["reason"], exit_["pnl"], exit_["pnl_r"]) == (
        "flatten",
        "flatten_close",
        "12.5000",
        "0.8000",
    )
    assert rows[5]["qty"] == "10"


def test_every_text_cell_is_guarded_and_numbers_are_not(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        add_decision(
            s,
            run,
            D2,
            1,
            "premarket",
            "classified",
            ticker="@EVIL",
            reason='=HYPERLINK("http://x")',
            rule="+rule",
            strategy_key="-orb",
            data={
                "catalyst": {"type": "=earnings", "quality": 70, "reason": "@SUM(A1)"},
                "decided_via": "\tweb",
            },
        )
        add_decision(
            s,
            run,
            D2,
            2,
            "fill",
            "filled",
            reason="@cmd",
            data={
                "diff_per_share": "-0.0200",
                "slippage": -0.01,
                "fill_price": "=1+1",
                "checks": [{"name": "=x", "value": "1", "op": "==", "threshold": "1", "passed": True}],
            },
        )
        s.commit()
    first, second = _rows(list(decisions_csv(db_factory, run, D2)))
    assert first["reason"] == '\'=HYPERLINK("http://x")'
    assert first["ticker"] == "'@EVIL" and first["rule"] == "'+rule" and first["strategy"] == "'-orb"
    assert first["catalyst_type"] == "'=earnings" and first["catalyst_quality"] == "70"
    assert first["catalyst_reason"] == "'@SUM(A1)" and first["decided_via"] == "'\tweb"
    assert second["reason"] == "'@cmd"
    assert second["diff_per_share"] == "-0.0200"  # a negative decimal is a number, never prefixed
    assert second["slippage"] == "-0.01"
    assert second["fill_price"] == "'=1+1"  # a text value in a number column is still guarded
    assert second["checks"] == "'=x 1 == 1 pass"


def test_catalyst_fields_from_a_premarket_row_without_a_nested_catalyst(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        run = add_run(s)
        add_decision(
            s,
            run,
            D2,
            1,
            "premarket",
            "classified",
            ticker="NVDA",
            reason="Guidance raised",
            data={"type": "earnings", "quality": 80},
        )
        add_decision(
            s,
            run,
            D2,
            2,
            "scan",
            "passed",
            ticker="NVDA",
            data={"opening_range": {"high": "10.3000", "low": "10.0100"}, "catalyst": {"type": "fda"}},
        )
        s.commit()
    pre, scan = _rows(list(decisions_csv(db_factory, run, D2)))
    assert (pre["catalyst_type"], pre["catalyst_quality"], pre["catalyst_reason"]) == (
        "earnings",
        "80",
        "Guidance raised",
    )
    assert (scan["or_high"], scan["or_low"], scan["catalyst_type"]) == ("10.3000", "10.0100", "fda")
    assert scan["catalyst_reason"] == ""


def test_a_day_without_rows_is_the_header_only(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        s.commit()
    assert list(decisions_csv(db_factory, run, D2)) == [",".join(DECISION_CSV_COLUMNS) + "\r\n"]


def test_lines_are_whole_rows(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        add_decision(s, run, D2, 1, "exit", "exited", reason='stop, "hit"\nline')
        s.commit()
    lines = list(decisions_csv(db_factory, run, D2))
    assert len(lines) == 2 and all(line.endswith("\r\n") for line in lines)
    assert _rows(lines)[0]["reason"] == 'stop, "hit"\nline'


# --- test 4: the cursor -------------------------------------------------------------------------------------
def test_closing_the_stream_after_two_lines_closes_the_cursor(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        for i in range(1, 6):
            add_decision(s, run, D2, i, "scan", "rejected", ticker=f"T{i}", data={"checks": CHECKS})
        s.commit()
    engine = db_factory.kw["bind"]
    seen: list[bool] = []

    def spy(conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool) -> None:
        if "trader.decision_log" in statement:
            seen.append(
                bool(getattr(context, "is_server_side", False)) or "ServerCursor" in type(cursor).__name__
            )

    event.listen(engine, "before_cursor_execute", spy)
    lines = decisions_csv(db_factory, run, D2)
    try:
        assert next(lines).startswith("run_id,")
        assert seen == []  # the header is sent before any query
        next(lines)
        assert seen == [True], "the export must read through a server-side cursor (yield_per)"
        assert engine.pool.checkedout() == 1  # the cursor and its transaction are open
        lines.close()
        assert inspect.getgeneratorstate(lines) == inspect.GEN_CLOSED
        assert engine.pool.checkedout() == 0
    finally:
        lines.close()
        event.remove(engine, "before_cursor_execute", spy)


def test_an_aware_ts_is_utc_z(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        add_decision(s, run, D2, 1, "day", "info", ts=datetime(2026, 10, 6, 20, 0, 0, 123000, tzinfo=UTC))
        s.commit()
    assert _rows(list(decisions_csv(db_factory, run, D2)))[0]["ts"] == "2026-10-06T20:00:00.123000Z"
