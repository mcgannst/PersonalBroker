"""FIX-401 (Tue 2026-09-29): the soak counted a 9:35 scan with 0 opening bars as a clean scan, and as proof
that the Questrade token works. Now an `event:orb_open` row whose detail shows `bars == 0`, or `missing` at
least half of the universe, is a failed scan for the day and never recovers the token check. Rows without
the counts (before FIX-401) are judged as before. Pure: no database."""

import dataclasses
from datetime import date, timedelta
from typing import Any

from tests.jobs.test_soak import CAL, LATER, TUE, check, clean_rows, et, evaluate, replace_job
from trader.jobs.soak import JobRunRow, build_report, report_json, scan_problem

ORB = "event:orb_open"


def with_scan(rows: list[JobRunRow], **detail: Any) -> list[JobRunRow]:
    [orb] = [r for r in rows if r.job == ORB]
    return replace_job(rows, ORB, dataclasses.replace(orb, detail={"strategies": ["orb_sip"], **detail}))


def scan(universe: int, bars: int, missing: int, reasons: dict[str, int] | None = None) -> dict[str, Any]:
    return {"universe": universe, "bars": bars, "missing": missing, "missing_reasons": reasons or {}}


def test_a_scan_with_its_bars_is_clean() -> None:
    day = evaluate(TUE, with_scan(clean_rows(TUE), **scan(542, 530, 12, {"timeout": 12})), LATER)
    assert day.verdict == "clean" and day.orb_open == "succeeded"


def test_a_scan_with_no_bars_fails_the_day() -> None:
    rows = with_scan(clean_rows(TUE), **scan(542, 0, 542, {"questrade_error: HTTP 401": 212, "timeout": 330}))
    day = evaluate(TUE, rows, LATER)
    assert day.verdict == "not_clean"
    orb = check(day, ORB)
    assert orb.status == "failed" and day.orb_open == "failed"
    assert orb.error == "9:35 scan: 542 of 542 opening bars missing (top reason timeout)"
    assert f"{ORB} failed" in day.failed
    assert day.orb_open_seconds is None


def test_a_scan_missing_half_the_universe_fails_the_day() -> None:
    day = evaluate(TUE, with_scan(clean_rows(TUE), **scan(100, 50, 50, {"timeout": 50})), LATER)
    assert check(day, ORB).status == "failed" and day.verdict == "not_clean"
    fine = evaluate(TUE, with_scan(clean_rows(TUE), **scan(100, 51, 49, {"timeout": 49})), LATER)
    assert check(fine, ORB).status == "succeeded" and fine.verdict == "clean"


def test_a_row_without_scan_counts_is_judged_by_its_status_as_before() -> None:
    day = evaluate(TUE, clean_rows(TUE), LATER)
    assert day.verdict == "clean"
    assert scan_problem({"strategies": ["orb_sip"], "outcomes": 0}) is None
    assert scan_problem(None) is None


def test_a_bad_scan_is_no_proof_the_token_works() -> None:
    """A token failure overnight, then premarket and postclose failed: only the 0-bar scan "succeeded"."""
    fail = (et(TUE, 2, 0),)
    rows = clean_rows(TUE)
    for job in ("premarket", "postclose"):
        [old] = [r for r in rows if r.job == job]
        rows = replace_job(rows, job, dataclasses.replace(old, status="failed", error="token"))
    good = evaluate(TUE, with_scan(rows, **scan(542, 530, 12)), LATER, token_failures=fail)
    assert check(good, "token-refresh").status == "succeeded"
    bad = evaluate(TUE, with_scan(rows, **scan(542, 0, 542, {"timeout": 542})), LATER, token_failures=fail)
    assert check(bad, "token-refresh").status == "failed"


def test_the_verdict_never_depends_on_now_once_final() -> None:
    rows = with_scan(clean_rows(TUE), **scan(542, 0, 542, {"timeout": 542}))
    assert evaluate(TUE, rows, LATER) == evaluate(TUE, rows, LATER + timedelta(days=7))


def test_scan_problem_reads_the_counts() -> None:
    assert scan_problem(scan(10, 0, 0)) == "9:35 scan: 0 opening bars (universe 10)"
    assert scan_problem(scan(10, 5, 5, {"a": 2, "b": 3})) == (
        "9:35 scan: 5 of 10 opening bars missing (top reason b)"
    )
    assert scan_problem(scan(10, 6, 4)) is None
    assert scan_problem({"bars": "x", "missing": 3}) is None  # unreadable counts: judged by status


def test_the_json_shows_the_failed_scan() -> None:
    day = evaluate(TUE, with_scan(clean_rows(TUE), **scan(4, 0, 4, {"timeout": 4})), LATER)
    report = build_report([day], cal=CAL, through=TUE, target=10, now=LATER, env="dev")
    (out,) = report_json(report)["days"]
    assert out["orb_open"] == "failed"
    assert any(c["job"] == ORB and c["status"] == "failed" for c in out["checks"])
    assert isinstance(date.fromisoformat(out["session_date"]), date)
