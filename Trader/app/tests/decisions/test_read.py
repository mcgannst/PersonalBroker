"""P6-T12 acceptance tests 2 and 5 (the read side): `trader.decisions.read` over seeded `decision_log` rows.

The rows are seeded directly (the recorder is P6-T10's), in the shape the recorder writes: one row per
decision with `checks` inside `data`, and one `day` row whose `data` holds the `DaySummary` fields
(Decimals as strings, rule counts as `[rule, count]` pairs) plus `text`.
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from trader.db import models as m
from trader.decisions import read

pytestmark = pytest.mark.db

D1, D2, D3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
T_SCAN = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET = 07:35:05 MT
T_REC = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)

CHECKS: list[dict[str, Any]] = [
    {"name": "rvol", "value": "3.20", "op": ">=", "threshold": "1.00", "passed": True},
    {"name": "price", "value": "22.40", "op": "between", "threshold": "5-50", "passed": True},
    {"name": "atr14", "value": None, "op": "present", "threshold": None, "passed": None},
]

SUMMARY: dict[str, Any] = {
    "run_id": 0,  # overwritten per seed
    "session_date": "2026-10-06",
    "final": True,
    "universe_size": 812,
    "universe_source": "finviz",
    "premarket_listed": 12,
    "premarket_classified": 10,
    "scanned": 811,
    "rvol_passed": 21,
    "ranked": 20,
    "passed": 1,
    "rejects_by_rule": [["rvol_below_min", 790], ["catalyst_low_quality", 6], ["doji", 3]],
    "signals": 1,
    "risk_rejections": [],
    "proposals": 1,
    "approvals": {"manual": 1, "auto": 0, "declined": 0, "expired": 0, "blocked": 0},
    "median_decision_seconds": 42.0,
    "fills": 2,
    "avg_fill_diff_per_share": "0.0200",
    "trades": 1,
    "wins": 1,
    "losses": 0,
    "pnl": "12.5000",
    "pnl_r": "0.8000",
    "exits_by_reason": [["flatten", 1]],
    "notes": ["no baseline for 2 symbols"],
}
SUMMARY_TEXT = "811 scanned · 20 ranked · 1 passed (NVDA) · 1 proposal, approved by you in 42 s"


def add_decision(
    s: Session,
    run_id: int,
    session_date: date,
    seq: int,
    stage: str,
    outcome: str,
    *,
    ticker: str | None = None,
    symbol_id: int | None = None,
    rule: str | None = None,
    reason: str | None = None,
    strategy_key: str | None = None,
    ts: datetime = T_SCAN,
    ref: Mapping[str, int] | None = None,
    data: Mapping[str, Any] | None = None,
    final: bool = False,
    recorded_at: datetime = T_REC,
) -> int:
    row = m.DecisionLog(
        run_id=run_id,
        session_date=session_date,
        seq=seq,
        stage=stage,
        strategy_key=strategy_key,
        symbol_id=symbol_id,
        ticker=ticker,
        outcome=outcome,
        rule=rule,
        reason=reason,
        ts=ts,
        ref=dict(ref or {}),
        data=dict(data or {}),
        recorded_at=recorded_at,
        final=final,
    )
    s.add(row)
    s.flush()
    return row.id


def seed_day(
    s: Session,
    run_id: int,
    session_date: date = D2,
    *,
    final: bool = True,
    with_day_row: bool = True,
    proposals: int = 1,
    trades: int = 1,
    prefix: str = "",
) -> None:
    """A small day: a universe row, three scan rows (one passed, two rejected), a proposal, a fill, an exit
    and the `day` row."""
    sym = add_symbol(s, f"{prefix}NVDA")
    add_decision(s, run_id, session_date, 1, "universe", "info", data={"count": 812}, final=final)
    add_decision(
        s,
        run_id,
        session_date,
        2,
        "scan",
        "info",
        strategy_key="orb_sip",
        data={"params": {"rvol_min": "1.00"}},
        final=final,
    )
    add_decision(
        s,
        run_id,
        session_date,
        3,
        "scan",
        "passed",
        strategy_key="orb_sip",
        ticker=f"{prefix}NVDA",
        symbol_id=sym,
        ref={"candidate_id": 5},
        data={"rvol": "3.20", "rank": 1, "checks": CHECKS, "entry": "10.2500", "stop_loss": "9.8000"},
        final=final,
    )
    add_decision(
        s,
        run_id,
        session_date,
        4,
        "scan",
        "rejected",
        strategy_key="orb_sip",
        ticker=f"{prefix}AMD",
        rule="rvol_below_min",
        data={"rvol": "0.80", "checks": [CHECKS[0] | {"value": "0.80", "passed": False}]},
        final=final,
    )
    add_decision(
        s,
        run_id,
        session_date,
        5,
        "scan",
        "rejected",
        strategy_key="orb_sip",
        ticker=f"{prefix}AAPL",
        rule="doji",
        final=final,
    )
    add_decision(
        s,
        run_id,
        session_date,
        6,
        "proposal",
        "proposed",
        ticker=f"{prefix}NVDA",
        ts=T_SCAN + timedelta(seconds=2),
        data={"qty": 10, "entry": "10.2500"},
        final=final,
    )
    add_decision(
        s,
        run_id,
        session_date,
        7,
        "fill",
        "filled",
        ticker=f"{prefix}NVDA",
        ts=T_SCAN + timedelta(minutes=3),
        data={"planned_price": "10.2500", "fill_price": "10.2700", "diff_per_share": "0.0200"},
        final=final,
    )
    add_decision(
        s,
        run_id,
        session_date,
        8,
        "exit",
        "exited",
        ticker=f"{prefix}NVDA",
        rule="flatten",
        reason="flatten_close",
        ts=T_SCAN + timedelta(hours=6),
        data={"pnl": "12.5000", "pnl_r": "0.8000"},
        final=final,
    )
    if with_day_row:
        summary = SUMMARY | {
            "run_id": run_id,
            "session_date": session_date.isoformat(),
            "final": final,
            "proposals": proposals,
            "trades": trades,
        }
        add_decision(
            s,
            run_id,
            session_date,
            9,
            "day",
            "info",
            ts=T_SCAN + timedelta(hours=7),
            data=summary | {"text": SUMMARY_TEXT, "fingerprint": "abc"},
            final=final,
        )


# --- resolve_run --------------------------------------------------------------------------------------------
def test_resolve_run_prefers_the_active_live_run_then_the_latest_live_run(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        assert read.resolve_run(db_factory, None) is None  # no run at all
        old = add_run(s, status="completed")
        replay = add_run(s, mode="replay", status="completed")
        s.commit()
    assert read.resolve_run(db_factory, None) == (old, "live")  # never the (newer) replay
    with db_factory() as s:
        active = add_run(s)
        add_run(s, status="completed")  # a newer, inactive live run: the active one still wins
        s.commit()
    assert read.resolve_run(db_factory, None) == (active, "live")
    assert read.resolve_run(db_factory, replay) == (replay, "replay")
    assert read.resolve_run(db_factory, old) == (old, "live")
    assert read.resolve_run(db_factory, 999_999) is None


# --- load_day (test 2) --------------------------------------------------------------------------------------
def test_load_day_rows_in_seq_order_with_the_summary(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        seed_day(s, run)
        s.commit()
    view = read.load_day(db_factory, None, D2)
    assert view is not None
    assert (view.run_id, view.run_mode, view.session_date, view.final) == (run, "live", D2, True)
    assert view.recorded_at == T_REC
    assert [r.seq for r in view.rows] == list(range(1, 10)) and view.total == 9
    assert view.rows[2].data["checks"] == CHECKS  # the read keeps data whole; the API lifts `checks`
    assert view.summary_text == SUMMARY_TEXT
    s_ = view.summary
    assert s_ is not None
    assert (s_.run_id, s_.session_date, s_.final) == (run, D2, True)
    assert (s_.scanned, s_.ranked, s_.passed, s_.universe_size) == (811, 20, 1, 812)
    assert s_.rejects_by_rule == (("rvol_below_min", 790), ("catalyst_low_quality", 6), ("doji", 3))
    assert s_.approvals["manual"] == 1 and s_.median_decision_seconds == 42.0
    assert s_.pnl == Decimal("12.5000") and s_.pnl_r == Decimal("0.8000")
    assert s_.avg_fill_diff_per_share == Decimal("0.0200")
    assert s_.exits_by_reason == (("flatten", 1),) and s_.notes == ("no baseline for 2 symbols",)


def test_load_day_filters_narrow(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        seed_day(s, run)
        s.commit()
    scan = read.load_day(db_factory, run, D2, stage="scan")
    assert scan is not None and [r.seq for r in scan.rows] == [2, 3, 4, 5] and scan.total == 4
    rejected = read.load_day(db_factory, run, D2, stage="scan", outcome="rejected")
    assert rejected is not None and [r.ticker for r in rejected.rows] == ["AMD", "AAPL"]
    nvda = read.load_day(db_factory, run, D2, ticker="nvda")  # case-insensitive
    assert nvda is not None and [r.seq for r in nvda.rows] == [3, 6, 7, 8]
    none = read.load_day(db_factory, run, D2, stage="kill_switch")
    assert none is not None and none.rows == () and none.total == 0
    assert none.summary is not None  # the summary comes from the day row, whatever the filters
    page = read.load_day(db_factory, run, D2, limit=3, offset=3)
    assert page is not None and [r.seq for r in page.rows] == [4, 5, 6] and page.total == 9


def test_load_day_without_rows_is_none(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        seed_day(s, run)
        s.commit()
    assert read.load_day(db_factory, None, D1) is None
    assert read.load_day(db_factory, 999_999, D2) is None


def test_a_day_without_its_day_row_has_no_summary_and_final_from_its_rows(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        run = add_run(s)
        seed_day(s, run, final=False, with_day_row=False)
        s.commit()
    view = read.load_day(db_factory, run, D2)
    assert view is not None and view.summary is None and view.summary_text is None and view.final is False


def test_an_unreadable_day_row_gives_no_summary_but_keeps_the_text(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        add_decision(s, run, D2, 1, "day", "info", data={"text": "partial", "scanned": "lots"})
        s.commit()
    view = read.load_day(db_factory, run, D2)
    assert view is not None and view.summary is None and view.summary_text == "partial"


# --- list_days ----------------------------------------------------------------------------------------------
def test_list_days_newest_first_with_counts(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        seed_day(s, run, D1, proposals=0, trades=0, prefix="A")
        seed_day(s, run, D2, prefix="B")
        seed_day(s, run, D3, final=False, with_day_row=False, prefix="C")
        s.commit()
    days = read.list_days(db_factory, run_id=None)
    assert [(d.session_date, d.final) for d in days] == [(D3, False), (D2, True), (D1, True)]
    assert (days[1].proposals, days[1].trades, days[1].summary_text) == (1, 1, SUMMARY_TEXT)
    assert (days[2].proposals, days[2].trades) == (0, 0)
    assert (days[0].proposals, days[0].trades, days[0].summary_text) == (0, 0, None)
    assert [d.session_date for d in read.list_days(db_factory, run_id=None, limit=2)] == [D3, D2]


# --- isolation (test 5) -------------------------------------------------------------------------------------
def test_a_replays_rows_never_appear_without_its_run_id(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        live = add_run(s)
        replay = add_run(s, mode="replay", status="completed")
        seed_day(s, live, D2, prefix="L")
        seed_day(s, replay, D2, prefix="R")
        seed_day(s, replay, D3, prefix="Q")  # a day only the replay has
        s.commit()
    view = read.load_day(db_factory, None, D2)
    assert view is not None and view.run_id == live
    assert all(r.run_id == live for r in view.rows)
    assert read.load_day(db_factory, None, D3) is None
    assert [(d.run_id, d.session_date) for d in read.list_days(db_factory, run_id=None)] == [(live, D2)]

    theirs = read.load_day(db_factory, replay, D3)
    assert theirs is not None and theirs.run_mode == "replay"
    assert {r.run_id for r in theirs.rows} == {replay}
    assert [(d.run_id, d.session_date) for d in read.list_days(db_factory, run_id=replay)] == [
        (replay, D3),
        (replay, D2),
    ]


def test_an_older_live_runs_days_still_show(db_factory: sessionmaker[Session]) -> None:
    """Rows of an older, non-active live run are live rows (`live_or_unscoped`), so its days still show."""
    with db_factory() as s:
        old = add_run(s, status="completed")
        seed_day(s, old, D1, prefix="O")
        new = add_run(s)
        seed_day(s, new, D2, prefix="N")
        s.commit()
    assert [(d.run_id, d.session_date) for d in read.list_days(db_factory, run_id=None)] == [
        (new, D2),
        (old, D1),
    ]
    view = read.load_day(db_factory, None, D1)
    assert view is not None and view.run_id == old
