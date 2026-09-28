"""P6-T12 acceptance tests 2, 3 and 5 (the API side): `GET /api/decisions/days`, `GET /api/decisions` and
`GET /api/export/decisions.csv` (`trader.api.routers.decisions`). Test 1 (cookie, GET only, the client
contract) is the sweep in `test_routes_sweep.py` and `test_web_client_contract.py` over the real app."""

import csv
import io
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.decisions.test_read import CHECKS, SUMMARY_TEXT, add_decision, seed_day
from tests.factories import add_run
from tests.fakes_api import make_services, test_core
from trader.api.routers import ROUTERS, decisions
from trader.api.schemas import DecisionDayOut, DecisionDaysOut
from trader.decisions.export import DECISION_CSV_COLUMNS
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)


def _client(factory: sessionmaker[Session], *, signed_in: bool = True) -> TestClient:
    services = make_services(test_core(factory, FixedClock(NOW)))
    if not signed_in:
        return make_client(services, decisions.router, user=None)
    return make_client(services, decisions.router)


def _seed(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as s:
        live = add_run(s)
        replay = add_run(s, mode="replay", status="completed")
        seed_day(s, live)
        seed_day(s, replay, prefix="R")
        s.commit()
    return live, replay


def test_the_router_is_registered_before_stream() -> None:
    assert decisions.router in ROUTERS and decisions.router.tags == ["decisions"]
    assert ROUTERS[-1].tags == ["stream"]


# --- /api/decisions -----------------------------------------------------------------------------------------
def test_day_rows_in_seq_order_with_checks_lifted_and_the_summary(db_factory: sessionmaker[Session]) -> None:
    live, _ = _seed(db_factory)
    r = _client(db_factory).get("/api/decisions", params={"date": "2026-10-06"})
    assert r.status_code == 200, r.text
    body = r.json()
    DecisionDayOut.model_validate(body)
    assert (body["run_id"], body["run_mode"], body["session_date"], body["final"]) == (
        live,
        "live",
        "2026-10-06",
        True,
    )
    assert body["recorded_at"] == "2026-10-06T20:30:00Z" and body["total"] == 9
    rows = body["rows"]
    assert [row["seq"] for row in rows] == list(range(1, 10))
    passed = rows[2]
    assert passed["checks"] == CHECKS and "checks" not in passed["data"]
    assert passed["data"]["rvol"] == "3.20" and passed["ref"] == {"candidate_id": 5}
    assert passed["ts"] == "2026-10-06T13:35:05Z"
    assert rows[0]["checks"] == []
    summary = body["summary"]
    assert summary["text"] == SUMMARY_TEXT
    assert (summary["scanned"], summary["ranked"], summary["passed"]) == (811, 20, 1)
    assert summary["rejects_by_rule"][0] == {"rule": "rvol_below_min", "count": 790}
    assert summary["pnl"] == "12.5000" and summary["pnl_r"] == "0.8000"  # money as JSON strings
    assert summary["avg_fill_diff_per_share"] == "0.0200"


def test_each_filter_narrows(db_factory: sessionmaker[Session]) -> None:
    live, _ = _seed(db_factory)
    client = _client(db_factory)

    def seqs(**params: str | int) -> list[int]:
        r = client.get("/api/decisions", params={"date": "2026-10-06", **params})
        assert r.status_code == 200, r.text
        return [row["seq"] for row in r.json()["rows"]]

    assert seqs(stage="scan") == [2, 3, 4, 5]
    assert seqs(outcome="rejected") == [4, 5]
    assert seqs(ticker="NVDA") == [3, 6, 7, 8]
    assert seqs(stage="scan", outcome="rejected", ticker="AMD") == [4]
    assert seqs(limit=2, offset=1) == [2, 3]
    assert seqs(run_id=live, stage="exit") == [8]
    empty = client.get("/api/decisions", params={"date": "2026-10-06", "stage": "kill_switch"}).json()
    assert empty["rows"] == [] and empty["total"] == 0 and empty["summary"] is not None


def test_an_empty_day_is_404(db_factory: sessionmaker[Session]) -> None:
    _seed(db_factory)
    r = _client(db_factory).get("/api/decisions", params={"date": "2026-10-05"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_an_unknown_run_is_404(db_factory: sessionmaker[Session]) -> None:
    _seed(db_factory)
    client = _client(db_factory)
    assert client.get("/api/decisions", params={"date": "2026-10-06", "run_id": 999_999}).status_code == 404
    assert client.get("/api/decisions/days", params={"run_id": 999_999}).status_code == 404
    csv_r = client.get("/api/export/decisions.csv", params={"date": "2026-10-06", "run_id": 999_999})
    assert csv_r.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"stage": "nope"},
        {"outcome": "maybe"},
        {"limit": 5001},
        {"limit": 0},
        {"offset": -1},
        {"offset": 2**31},  # fix round 1: capped (2**31 - 1), so past-bigint offsets are 422, never a 500
        {"offset": 10**19},
        {"run_id": "abc"},
    ],
)
def test_bad_query_values_are_422(db_factory: sessionmaker[Session], params: dict[str, object]) -> None:
    _seed(db_factory)
    r = _client(db_factory).get("/api/decisions", params={"date": "2026-10-06", **params})
    assert r.status_code == 422, (params, r.text)


@pytest.mark.parametrize("date", ["2026-13-01", "yesterday", ""])
def test_a_bad_or_missing_date_is_422(db_factory: sessionmaker[Session], date: str) -> None:
    client = _client(db_factory)
    assert client.get("/api/decisions", params={"date": date}).status_code == 422
    assert client.get("/api/export/decisions.csv", params={"date": date}).status_code == 422


def test_the_limit_of_5000_is_allowed(db_factory: sessionmaker[Session]) -> None:
    _seed(db_factory)
    r = _client(db_factory).get("/api/decisions", params={"date": "2026-10-06", "limit": 5000})
    assert r.status_code == 200


def test_malformed_checks_or_refs_never_fail_the_day(db_factory: sessionmaker[Session]) -> None:
    """A check with an unknown op or a non-integer ref value is dropped, not a 500."""
    with db_factory() as s:
        run = add_run(s)
        add_decision(
            s,
            run,
            datetime(2026, 10, 6).date(),
            1,
            "scan",
            "rejected",
            ref={"candidate_id": 5},
            data={"checks": [{"name": "x", "op": "~", "value": "1"}, CHECKS[0], "junk"]},
        )
        s.commit()
    body = _client(db_factory).get("/api/decisions", params={"date": "2026-10-06"}).json()
    assert body["rows"][0]["checks"] == [CHECKS[0]]


# --- /api/decisions/days ------------------------------------------------------------------------------------
def test_days_list(db_factory: sessionmaker[Session]) -> None:
    live, replay = _seed(db_factory)
    client = _client(db_factory)
    body = client.get("/api/decisions/days").json()
    DecisionDaysOut.model_validate(body)
    assert body["days"] == [
        {
            "run_id": live,
            "session_date": "2026-10-06",
            "final": True,
            "summary_text": SUMMARY_TEXT,
            "proposals": 1,
            "trades": 1,
        }
    ]
    theirs = client.get("/api/decisions/days", params={"run_id": replay}).json()["days"]
    assert [d["run_id"] for d in theirs] == [replay]
    assert client.get("/api/decisions/days", params={"limit": 0}).status_code == 422


# --- isolation (test 5) -------------------------------------------------------------------------------------
def test_a_replays_rows_need_its_run_id(db_factory: sessionmaker[Session]) -> None:
    live, replay = _seed(db_factory)
    with db_factory() as s:
        seed_day(s, replay, datetime(2026, 10, 7).date(), prefix="Q")  # a day only the replay has
        s.commit()
    client = _client(db_factory)
    mine = client.get("/api/decisions", params={"date": "2026-10-06"}).json()
    assert mine["run_id"] == live and all(not (r["ticker"] or "").startswith("R") for r in mine["rows"])
    assert client.get("/api/decisions", params={"date": "2026-10-07"}).status_code == 404
    assert client.get("/api/export/decisions.csv", params={"date": "2026-10-07"}).status_code == 404
    theirs = client.get("/api/decisions", params={"date": "2026-10-07", "run_id": replay}).json()
    assert theirs["run_id"] == replay and theirs["run_mode"] == "replay"
    assert {r["ticker"] for r in theirs["rows"] if r["ticker"]} == {"QNVDA", "QAMD", "QAAPL"}


# --- /api/export/decisions.csv (test 3) ---------------------------------------------------------------------
def test_csv_export(db_factory: sessionmaker[Session]) -> None:
    live, replay = _seed(db_factory)
    with db_factory() as s:
        add_decision(s, live, datetime(2026, 10, 6).date(), 10, "risk", "rejected", reason="=cmd()")
        s.commit()
    client = _client(db_factory)
    r = client.get("/api/export/decisions.csv", params={"date": "2026-10-06"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == f'attachment; filename="decisions-2026-10-06-run{live}.csv"'
    rows = list(csv.reader(io.StringIO(r.text)))
    assert tuple(rows[0]) == DECISION_CSV_COLUMNS and len(rows) == 1 + 10
    assert {row[0] for row in rows[1:]} == {str(live)}
    assert rows[-1][DECISION_CSV_COLUMNS.index("reason")] == "'=cmd()"
    theirs = client.get("/api/export/decisions.csv", params={"date": "2026-10-06", "run_id": replay})
    assert (
        theirs.headers["content-disposition"]
        == f'attachment; filename="decisions-2026-10-06-run{replay}.csv"'
    )
    assert {row[1] for row in list(csv.reader(io.StringIO(theirs.text)))[1:]} == {"replay"}


def test_every_route_needs_a_session(db_factory: sessionmaker[Session]) -> None:
    _seed(db_factory)
    client = _client(db_factory, signed_in=False)
    for path, params in (
        ("/api/decisions/days", {}),
        ("/api/decisions", {"date": "2026-10-06"}),
        ("/api/export/decisions.csv", {"date": "2026-10-06"}),
    ):
        assert client.get(path, params=params).status_code == 401, path
