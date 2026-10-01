"""CATWIDE: with extend_past_top_n the decision log's scan stage records the ranks actually evaluated as
candidates and only the ranks never evaluated as `outside_top_n`, with the evaluated rank as the threshold."""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.decisions.test_recorder import SCAN_AT, D, deps, make_world, rows
from trader.decisions.recorder import record_day

pytestmark = pytest.mark.db


async def test_extended_scan_outside_top_n_is_past_the_last_evaluated_rank(
    db_factory: sessionmaker[Session],
) -> None:
    w = make_world(db_factory, orb_params={"top_n": 5, "extend_past_top_n": True, "max_rank": 100})
    names = [f"R{i:02d}" for i in range(1, 15)]  # rvol 5.00, 4.90, ...
    for i, t in enumerate(names):
        vol = 5000 - i * 100
        w.member(t, bar=("21.00", "21.50", "20.90", "21.40", vol))
        if i < 10:  # chunks 1 (ranks 1..5) and 2 (6..10) evaluated; R08 passed and filled the slot
            w.candidate(t, i + 1, f"{Decimal(vol) / 1000:.4f}", reject=None if i == 7 else "catalyst_missing")
    w.event("strategy.orb_sip", "orb: ranked", {"ranked": 10, "selected": ["R08"]}, SCAN_AT)
    w.job("event:orb_open", {}, SCAN_AT)
    await record_day(deps(db_factory), w.run_id, D)
    scan = [r for r in rows(db_factory, w.run_id, "scan") if r.symbol_id is not None]
    by = {r.ticker: r for r in scan}
    outside = sorted((r.data["rank"], r.ticker) for r in scan if r.rule == "outside_top_n")
    assert outside == [(11, "R11"), (12, "R12"), (13, "R13"), (14, "R14")]
    rank_check = next(c for c in by["R11"].data["checks"] if c["name"] == "rank")
    assert (rank_check["threshold"], rank_check["passed"]) == ("10", False)
    # an evaluated candidate past top_n passes the rank check (its threshold is max_rank)
    r08 = next(c for c in by["R08"].data["checks"] if c["name"] == "rank")
    assert (r08["value"], r08["threshold"], r08["passed"]) == ("8", "100", True)
    assert by["R08"].outcome == "passed"
    (summary,) = [r for r in rows(db_factory, w.run_id, "scan") if r.symbol_id is None]
    counts = summary.data["counts"]
    assert (counts["rvol_passed"], counts["ranked"], counts["passed"]) == (14, 10, 1)
    assert counts["rejects_by_rule"]["outside_top_n"] == 4


async def test_extended_but_filled_in_chunk_one_keeps_top_n_as_the_threshold(
    db_factory: sessionmaker[Session],
) -> None:
    w = make_world(db_factory, orb_params={"top_n": 5, "extend_past_top_n": True})
    for i in range(8):
        vol = 5000 - i * 100
        t = f"R{i + 1:02d}"
        w.member(t, bar=("21.00", "21.50", "20.90", "21.40", vol))
        if i < 5:
            w.candidate(t, i + 1, f"{Decimal(vol) / 1000:.4f}", reject=None if i == 0 else "lower_rank")
    w.event("strategy.orb_sip", "orb: ranked", {"ranked": 5, "selected": ["R01"]}, SCAN_AT)
    w.job("event:orb_open", {}, SCAN_AT)
    await record_day(deps(db_factory), w.run_id, D)
    scan = [r for r in rows(db_factory, w.run_id, "scan") if r.symbol_id is not None]
    outside = [r for r in scan if r.rule == "outside_top_n"]
    assert sorted(r.data["rank"] for r in outside) == [6, 7, 8]
    assert {next(c for c in r.data["checks"] if c["name"] == "rank")["threshold"] for r in outside} == {"5"}
