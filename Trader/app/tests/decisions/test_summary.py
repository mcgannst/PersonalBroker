"""P6-T10 acceptance test 9: `summarize` gives exact counts over a seeded day and `summary_text` stays within
3 lines and 400 characters with the top three reject rules. The day row's JSON round-trips."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from trader.decisions.summary import summarize, summary_from_json, summary_text, summary_to_json
from trader.decisions.types import DecisionOutcome, DecisionRowView, DecisionStage

D = date(2026, 9, 28)
T = datetime(2026, 9, 28, 13, 35, tzinfo=UTC)


def row(
    stage: DecisionStage,
    outcome: DecisionOutcome,
    *,
    symbol: int | None = None,
    rule: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> DecisionRowView:
    return DecisionRowView(
        id=0,
        run_id=7,
        session_date=D,
        seq=0,
        stage=stage,
        strategy_key=None,
        symbol_id=symbol,
        ticker=None,
        outcome=outcome,
        rule=rule,
        reason=None,
        ts=T,
        ref={},
        data=data or {},
        recorded_at=T,
        final=False,
    )


def seeded_day() -> list[DecisionRowView]:
    rows = [
        row("universe", "info", data={"size": 812, "source": "finviz"}),
        row("premarket", "info", data={"summary_note": "1 FinViz screen error(s) in the pre-market"}),
        row("premarket", "classified", symbol=1),
        row("premarket", "classified", symbol=2),
        row("premarket", "listed", symbol=3, rule="over_cap"),
        row(
            "scan",
            "info",
            data={
                "counts": {
                    "scanned": 812,
                    "rvol_passed": 20,
                    "ranked": 14,
                    "passed": 1,
                    "rejects_by_rule": {
                        "rvol_below_min": 790,
                        "catalyst_low_quality": 6,
                        "doji": 3,
                        "outside_top_n": 6,
                        "lower_rank": 1,
                        "no_baseline": 2,
                    },
                }
            },
        ),
        row("signal", "proposed", symbol=1),
        row("signal", "rejected", symbol=2, rule="zero_shares"),
        row("risk", "rejected", symbol=2, rule="zero_shares"),
        row("proposal", "proposed", symbol=1),
        row("proposal", "proposed", symbol=1),
        row("proposal", "proposed", symbol=4),
        row("approval", "approved", symbol=1, rule="manual", data={"decision_latency_ms": 42_000}),
        row("approval", "declined", symbol=4, data={"decision_latency_ms": 10_000}),
        row("approval", "auto_approved", symbol=1, data={"decision_latency_ms": 0}),
        row("fill", "filled", symbol=1, data={"diff_per_share": "0.02"}),
        row("fill", "filled", symbol=1, data={"diff_per_share": "0.0100"}),
        row("exit", "exited", symbol=1, rule="flatten", data={"pnl": "12.50", "pnl_r": "0.8"}),
        row("exit", "exited", symbol=5, rule="stop", data={"pnl": "-4.00", "pnl_r": "-1.0"}),
        row("exit", "exited", symbol=6, rule="flatten", data={"pnl": "1.00", "pnl_r": None}),
    ]
    return rows


def test_9_summarize_counts_a_seeded_day_exactly() -> None:
    s = summarize(seeded_day(), run_id=7, session_date=D, final=True)
    assert (s.run_id, s.session_date, s.final) == (7, D, True)
    assert (s.universe_size, s.universe_source) == (812, "finviz")
    assert (s.premarket_listed, s.premarket_classified) == (3, 2)
    assert (s.scanned, s.rvol_passed, s.ranked, s.passed) == (812, 20, 14, 1)
    assert s.rejects_by_rule == (
        ("rvol_below_min", 790),
        ("catalyst_low_quality", 6),
        ("outside_top_n", 6),
        ("doji", 3),
        ("no_baseline", 2),
        ("lower_rank", 1),
    )
    assert s.signals == 2 and s.risk_rejections == (("zero_shares", 1),)
    assert s.proposals == 3
    assert dict(s.approvals) == {"manual": 1, "auto": 1, "declined": 1, "expired": 0, "blocked": 0}
    assert s.median_decision_seconds == 26.0  # manual decisions only: 42 s and 10 s
    assert s.fills == 2 and s.avg_fill_diff_per_share == Decimal("0.0150")
    assert (s.trades, s.wins, s.losses) == (3, 2, 1)
    assert s.pnl == Decimal("9.50") and s.pnl_r == Decimal("-0.2")
    assert s.exits_by_reason == (("flatten", 2), ("stop", 1))
    assert s.notes == ("1 FinViz screen error(s) in the pre-market",)


def test_9_without_a_counts_row_the_scan_rows_are_counted() -> None:
    rows = [
        row("scan", "passed", symbol=1, data={"rank": 1}),
        row("scan", "rejected", symbol=2, rule="doji", data={"rank": 2}),
        row("scan", "rejected", symbol=3, rule="outside_top_n", data={"rank": 21}),
        row("scan", "rejected", symbol=4, rule="rvol_below_min"),
        row("scan", "rejected", symbol=5, rule="no_opening_bar"),
    ]
    s = summarize(rows, run_id=1, session_date=D, final=False)
    assert (s.scanned, s.rvol_passed, s.ranked, s.passed) == (5, 3, 2, 1)
    assert s.rejects_by_rule == (
        ("doji", 1),
        ("no_opening_bar", 1),
        ("outside_top_n", 1),
        ("rvol_below_min", 1),
    )
    assert s.pnl == Decimal(0) and s.pnl_r is None and s.median_decision_seconds is None


def test_9_summary_text_is_short_and_names_the_top_three_rejects() -> None:
    s = summarize(seeded_day(), run_id=7, session_date=D, final=True)
    t = summary_text(s, link=None)
    lines = t.split("\n")
    assert len(lines) <= 3 and len(t) <= 400
    assert lines[0] == "universe 812 · 812 scanned · 14 ranked · 1 passed"
    assert "3 proposals (1 manual, 1 auto, 1 declined, median 26 s)" in lines[1]
    assert "2 fills (avg +0.015 vs planned)" in lines[1]
    assert "3 trades 2W/1L P&L +9.50 (-0.20R)" in lines[1] and "exits flatten 2, stop 1" in lines[1]
    assert lines[2].startswith("top rejects: rvol_below_min 790, catalyst_low_quality 6, outside_top_n 6")
    assert "doji" not in lines[2]
    link = "https://trader-dev.example/reports?day=2026-09-28"
    with_link = summary_text(s, link=link)
    assert with_link.endswith(link) and len(with_link) <= 400 and with_link.count("\n") <= 2


def test_9_summary_text_caps_a_huge_day() -> None:
    s = summarize(seeded_day(), run_id=7, session_date=D, final=True)
    from dataclasses import replace

    many = replace(
        s,
        risk_rejections=tuple((f"check_{i}_" + "x" * 20, i) for i in range(1, 40)),
        notes=("n" * 500,),
    )
    link = "https://trader.example/reports?day=2026-09-28"
    for lk in (None, link):
        t = summary_text(many, link=lk)
        assert len(t) <= 400 and t.count("\n") <= 2
    assert summary_text(many, link=link).endswith(link)


def test_9_an_empty_day() -> None:
    s = summarize([], run_id=1, session_date=D, final=False)
    assert s.scanned == 0 and s.trades == 0 and s.universe_size is None
    assert summary_text(s, link=None) == "no 9:35 scan recorded\nno proposals"


def test_the_day_json_round_trips() -> None:
    s = summarize(seeded_day(), run_id=7, session_date=D, final=True)
    assert summary_from_json(summary_to_json(s)) == s
