"""DB-T5 acceptance tests 1-2 (db): the activity feed (plan S7) and the rejections panel (plan S8) on a real
test database.

`seed_session_day` builds one live-run session day with a row for every S7 source (also used by
`tests/api/test_live_route.py`); `seed_replay_noise` adds the same kinds of rows for a replay run and for
another day, none of which may appear.

The ET day bounds come from `periods.et_day_bounds` (DB-T3). Until DB-T3 lands that function is a stub, so
the `et_bounds` fixture substitutes the S5 definition only while it still raises `NotImplementedError`.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, insert
from sqlalchemy.orm import Session, sessionmaker

from tests.api.test_trading import _config, _order, _proposal, _signal, at_et, live_run
from tests.factories import add_run, add_symbol
from tests.live.test_contracts import _is_stub
from trader.api.livedata import activity, periods
from trader.api.livedata.activity import activity_feed, rejections
from trader.db import models as m
from trader.engine.proposals import AUTO_FLATTEN_ACTOR
from trader.market.clock import ET, FixedClock

pytestmark = pytest.mark.db

DAY = date(2026, 10, 6)  # a Tuesday session (EDT; MT = UTC-6)
NOW = at_et(DAY, 14, 0)
CANDIDATES, PASSED = 543, 12


def s5_day_bounds(d: date) -> tuple[datetime, datetime]:
    """Plan S5: [00:00 ET of d, 00:00 ET of d + 1 day) as UTC."""
    start = datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC)
    return start, datetime.combine(d + timedelta(days=1), time(0), tzinfo=ET).astimezone(UTC)


@pytest.fixture(autouse=True)
def et_bounds(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if _is_stub(periods.et_day_bounds):
        monkeypatch.setattr(periods, "et_day_bounds", s5_day_bounds)
    yield


# --- seeding ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Day:
    run_id: int
    position_id: int
    entry_order_id: int
    cancelled_order_id: int
    fill_id: int
    trade_id: int
    approved_id: int
    expired_id: int
    rejected_id: int
    ks_id: int
    job_ids: tuple[int, int]
    alert_id: int
    first_candidate_id: int


def _symbols(s: Session, prefix: str, n: int) -> list[int]:
    rows = [
        {"ticker": f"{prefix}{i:03d}", "exchange": "NYSE", "currency": "USD", "name": f"{prefix}{i}"}
        for i in range(n)
    ]
    return list(s.execute(insert(m.Symbol).returning(m.Symbol.id), rows).scalars())


def _fill(s: Session, run_id: int, order: m.Order, price: str, slippage: str, ts: datetime) -> int:
    f = m.Fill(
        run_id=run_id,
        order_id=order.id,
        ts=ts,
        qty=order.qty,
        price=Decimal(price),
        fees={"commission": "0.35", "ecn": "0", "sec": "0", "total": "0.35"},
        quote_snapshot={"bid": price, "ask": price},
        slippage=Decimal(slippage),
    )
    s.add(f)
    s.flush()
    return f.id


def _event(s: Session, run_id: int | None, level: str, source: str, message: str, ts: datetime) -> int:
    e = m.EventLog(ts=ts, level=level, source=source, run_id=run_id, message=message, data=None)
    s.add(e)
    s.flush()
    return e.id


def _job(s: Session, job: str, error: str | None, started: datetime, status: str = "failed") -> int:
    j = m.JobRun(
        job=job,
        session_date=DAY,
        started_at=started,
        finished_at=started + timedelta(minutes=1),
        status=status,
        error=error,
        detail=None,
    )
    s.add(j)
    s.flush()
    return j.id


def seed_session_day(s: Session, run_id: int, *, day: date = DAY) -> Day:
    """Every S7 source once, on `day`, for `run_id`."""
    aapl, msft = add_symbol(s, "AAPL"), add_symbol(s, "MSFT")
    cfg = _config(s, "orb_sip")
    sig = _signal(s, run_id, cfg, aapl, day, at_et(day, 9, 35))
    # entry proposal approved via telegram at 09:36:12 ET (07:36 MT), its order and fill, the position
    approved = _proposal(s, run_id, sig, aapl, kind="entry", created_at=at_et(day, 9, 36), qty=25)
    entry = _order(
        s,
        run_id,
        aapl,
        purpose="entry",
        side="buy",
        order_type="stop",
        status="filled",
        day=day,
        submitted_at=at_et(day, 9, 36) + timedelta(seconds=13),
        proposal_id=approved.id,
        stop_price="182.40",
        qty=25,
    )
    pos = m.Position(
        run_id=run_id,
        symbol_id=aapl,
        strategy_config_id=cfg,
        qty=25,
        avg_price=Decimal("182.46"),
        stop_loss=Decimal("181.96"),
        planned_risk=Decimal("25.00"),
        session_date=day,
        opened_at=at_et(day, 9, 37),
        closed_at=at_et(day, 10, 15),
        entry_order_id=entry.id,
        unprotected_seconds=0,
    )
    s.add(pos)
    s.flush()
    entry.position_id = approved.position_id = pos.id
    fill = _fill(s, run_id, entry, "182.46", "0.06", at_et(day, 9, 37))
    trade = m.Trade(
        run_id=run_id,
        position_id=pos.id,
        symbol_id=aapl,
        session_date=day,
        entry_price=Decimal("182.46"),
        exit_price=Decimal("181.96"),
        qty=25,
        pnl=Decimal("-12.5000"),
        pnl_r=Decimal("-0.5000"),
        planned_risk=Decimal("25.00"),
        exit_reason="stop",
        slippage_total=Decimal("0"),
        fees_total=Decimal("0.70"),
        opened_at=pos.opened_at,
        closed_at=at_et(day, 10, 15),
    )
    s.add(trade)
    # an exit proposal that expired and was executed by auto_flatten_on_expiry (created in manual mode)
    expired = _proposal(
        s,
        run_id,
        sig,
        aapl,
        kind="exit",
        created_at=at_et(day, 10, 58),
        status="submitted",
        side="sell",
        position_id=pos.id,
        qty=25,
    )
    expired.decided_at = expired.expired_at = at_et(day, 11, 0)
    expired.decided_via, expired.decided_by = "auto", AUTO_FLATTEN_ACTOR
    # an entry proposal rejected via the web by the kill-switch guard, then its order-less sibling cancelled
    msig = _signal(s, run_id, cfg, msft, day, at_et(day, 10, 39))
    rejected = _proposal(
        s, run_id, msig, msft, kind="entry", created_at=at_et(day, 10, 40), status="rejected"
    )
    rejected.decided_via, rejected.decided_at = "web", at_et(day, 10, 40) + timedelta(seconds=30)
    rejected.error = "entry blocked: kill switch daily_loss_pct"
    cancelled = _order(
        s,
        run_id,
        msft,
        purpose="entry",
        side="buy",
        order_type="stop",
        status="cancelled",
        day=day,
        submitted_at=at_et(day, 10, 20),
        stop_price="410.10",
    )
    cancelled.closed_at, cancelled.cancel_reason = at_et(day, 10, 41), "entry cutoff"
    ks = m.KillSwitchEvent(
        run_id=run_id,
        switch="daily_loss_pct",
        session_date=day,
        tripped_at=at_et(day, 12, 0),
        value=Decimal("0.052"),
        threshold=Decimal("0.05"),
        reset_at=at_et(day, 12, 30),
        reset_reason="reviewed the fills",
        reset_by="web:stephen",
    )
    s.add(ks)
    s.flush()
    jobs = (
        _job(s, "premarket", "timeout", at_et(day, 8, 0)),
        _job(s, "premarket", "db down password=hunter2 " + "z" * 300, at_et(day, 8, 5)),
    )
    _job(s, "preopen", None, at_et(day, 9, 20), status="succeeded")
    alert = _event(s, run_id, "error", "engine", "stop order rejected token=abc123secret", at_et(day, 13, 0))
    _event(s, None, "critical", "log.worker", "mirrored log line", at_et(day, 13, 1))
    _event(s, run_id, "warning", "engine", "just a warning", at_et(day, 13, 2))
    # the 9:35 scan: 543 candidates, 12 passed (ranked 1..12)
    ids = _symbols(s, "C", CANDIDATES)
    scan_at = at_et(day, 9, 35) + timedelta(seconds=5)
    rows: list[dict[str, Any]] = [
        {
            "run_id": run_id,
            "session_date": day,
            "strategy_key": "orb_sip",
            "symbol_id": sid,
            "rank": i + 1 if i < PASSED else None,
            "passed": i < PASSED,
            "reject_reason": None if i < PASSED else ("rvol_below_min" if i % 2 else "no_opening_bar"),
            "created_at": scan_at + timedelta(milliseconds=i),
        }
        for i, sid in enumerate(ids)
    ]
    first = min(s.execute(insert(m.Candidate).returning(m.Candidate.id), rows).scalars())
    s.flush()
    return Day(
        run_id,
        pos.id,
        entry.id,
        cancelled.id,
        fill,
        trade.id,
        approved.id,
        expired.id,
        rejected.id,
        ks.id,
        jobs,
        alert,
        first,
    )


def seed_replay_noise(s: Session, live_run_id: int, *, day: date = DAY) -> None:
    """A replay run's rows on `day` and the live run's rows on the day before: none may be shown."""
    replay = add_run(s, mode="replay", status="completed", started_at=at_et(day, 6, 0))
    rsym = add_symbol(s, "RPLY")
    cfg = _config(s, "orb_sip")
    sig = _signal(s, replay, cfg, rsym, day, at_et(day, 9, 35))
    p = _proposal(s, replay, sig, rsym, kind="entry", created_at=at_et(day, 9, 36))
    o = _order(
        s,
        replay,
        rsym,
        purpose="entry",
        side="buy",
        order_type="stop",
        status="cancelled",
        day=day,
        submitted_at=at_et(day, 9, 36),
        proposal_id=p.id,
        stop_price="5.00",
    )
    _fill(s, replay, o, "5.00", "0.01", at_et(day, 9, 40))
    _event(s, replay, "error", "engine", "replay error", at_et(day, 10, 0))
    s.add(
        m.KillSwitchEvent(
            run_id=replay,
            switch="expectancy",
            session_date=day,
            tripped_at=at_et(day, 11, 0),
            value=Decimal("-0.3"),
            threshold=Decimal("-0.2"),
        )
    )
    s.add(
        m.Candidate(
            run_id=replay,
            session_date=day,
            strategy_key="orb_sip",
            symbol_id=rsym,
            passed=True,
            rank=1,
            created_at=at_et(day, 9, 35),
        )
    )
    s.add(
        m.DecisionLog(
            run_id=replay,
            session_date=day,
            seq=1,
            stage="risk",
            symbol_id=rsym,
            ticker="RPLY",
            outcome="rejected",
            rule="max_positions",
            ts=at_et(day, 9, 36),
            recorded_at=at_et(day, 9, 40),
        )
    )
    # the live run, the evening before (23:59 ET): outside the day
    before = day - timedelta(days=1)
    lsym = add_symbol(s, "YDAY")
    _order(
        s,
        live_run_id,
        lsym,
        purpose="entry",
        side="buy",
        order_type="stop",
        status="working",
        day=before,
        submitted_at=at_et(before, 23, 59),
        stop_price="9.00",
    )
    _event(s, live_run_id, "error", "engine", "yesterday", at_et(before, 23, 59))
    s.flush()


def _run(factory: sessionmaker[Session]) -> int:
    return live_run(factory, FixedClock(NOW))


# --- 1. the activity feed -----------------------------------------------------------------------------------


def test_activity_items_of_every_kind_newest_first(db_factory: sessionmaker[Session]) -> None:
    run_id = _run(db_factory)
    with db_factory() as s:
        d = seed_session_day(s, run_id)
        seed_replay_noise(s, run_id)
        s.commit()
    items = activity_feed(db_factory, run_id, DAY)
    by_id = {i.id: i for i in items}
    got = {i.id: (i.kind, i.chip, i.tone, i.text, i.link) for i in items}
    pos_link, cancelled_link = f"/trades?position={d.position_id}", None
    expected = {
        f"scan:{d.first_candidate_id}": (
            "scan",
            "scan",
            "neutral",
            "07:35 MT scan: 543 → 12 passed → C000, C001, C002",
            "/reports?day=2026-10-06",
        ),
        f"proposal_created:{d.approved_id}": (
            "proposal_created",
            "proposals",
            "neutral",
            "Proposal entry AAPL 25 sh (manual)",
            f"/dashboard?proposal={d.approved_id}",
        ),
        f"proposal_approved:{d.approved_id}": (
            "proposal_approved",
            "proposals",
            "neutral",
            "Approved entry AAPL via telegram at 07:36 MT",
            f"/dashboard?proposal={d.approved_id}",
        ),
        f"order_placed:{d.entry_order_id}": (
            "order_placed",
            "trades",
            "neutral",
            "Buy stop 25 AAPL @ 182.40 (entry)",
            pos_link,
        ),
        f"fill:{d.fill_id}": (
            "fill",
            "trades",
            "neutral",
            "Filled buy 25 AAPL @ 182.46, slippage 0.06/share",
            pos_link,
        ),
        f"exit:{d.trade_id}": ("exit", "trades", "down", "Exit AAPL (stop): -12.50, -0.50 R", pos_link),
        f"order_placed:{d.cancelled_order_id}": (
            "order_placed",
            "trades",
            "neutral",
            "Buy stop 10 MSFT @ 410.10 (entry)",
            cancelled_link,
        ),
        f"proposal_created:{d.rejected_id}": (
            "proposal_created",
            "proposals",
            "neutral",
            "Proposal entry MSFT 10 sh (manual)",
            f"/dashboard?proposal={d.rejected_id}",
        ),
        f"proposal_rejected:{d.rejected_id}": (
            "proposal_rejected",
            "proposals",
            "warn",
            "Rejected entry MSFT via web at 08:40 MT: entry blocked: kill switch daily_loss_pct",
            f"/dashboard?proposal={d.rejected_id}",
        ),
        f"order_cancelled:{d.cancelled_order_id}": (
            "order_cancelled",
            "trades",
            "neutral",
            "Cancelled entry MSFT: entry cutoff",
            cancelled_link,
        ),
        f"proposal_created:{d.expired_id}": (
            "proposal_created",
            "proposals",
            "neutral",
            "Proposal exit AAPL 25 sh (manual)",
            f"/dashboard?proposal={d.expired_id}",
        ),
        f"proposal_expired:{d.expired_id}": (
            "proposal_expired",
            "proposals",
            "warn",
            "Expired exit AAPL (auto-flatten)",
            f"/dashboard?proposal={d.expired_id}",
        ),
        f"kill_switch_tripped:{d.ks_id}": (
            "kill_switch_tripped",
            "alerts",
            "warn",
            "Kill switch daily loss tripped: 5.2% vs 5.0%",
            "/control",
        ),
        f"kill_switch_reset:{d.ks_id}": (
            "kill_switch_reset",
            "alerts",
            "neutral",
            "Kill switch daily loss reset by web:stephen: reviewed the fills",
            "/control",
        ),
        f"job_failed:{d.job_ids[0]}": (
            "job_failed",
            "alerts",
            "warn",
            "premarket failed (attempt 1): timeout",
            "/control",
        ),
        f"job_failed:{d.job_ids[1]}": (
            "job_failed",
            "alerts",
            "warn",
            "premarket failed (attempt 2): " + ("db down password=[REDACTED] " + "z" * 300)[:120],
            "/control",
        ),
        f"alert:{d.alert_id}": (
            "alert",
            "alerts",
            "warn",
            "engine: stop order rejected token=[REDACTED]",
            "/control",
        ),
    }
    assert got == expected
    # every S7 kind appears; the log mirror, warnings, other days and the replay are absent
    assert {i.kind for i in items} == set(activity._CHIP)
    assert all("RPLY" not in i.text and "yesterday" not in i.text and "mirrored" not in i.text for i in items)
    # newest first; same-instant items in lifecycle order (the auto-flatten decision is not an approval)
    stamps = [i.ts for i in items]
    assert stamps == sorted(stamps, reverse=True)
    assert f"proposal_approved:{d.expired_id}" not in by_id
    # amounts: the fill price and the trade P&L only
    assert by_id[f"fill:{d.fill_id}"].amount == Decimal("182.46")
    assert by_id[f"exit:{d.trade_id}"].amount == Decimal("-12.50")
    assert by_id[f"exit:{d.trade_id}"].ticker == "AAPL"
    assert all(i.amount is None for i in items if i.kind not in ("fill", "exit"))


def test_activity_limit_keeps_the_newest(db_factory: sessionmaker[Session]) -> None:
    run_id = _run(db_factory)
    with db_factory() as s:
        seed_session_day(s, run_id)
        s.commit()
    everything = activity_feed(db_factory, run_id, DAY)
    assert [i.id for i in activity_feed(db_factory, run_id, DAY, limit=5)] == [i.id for i in everything[:5]]
    assert activity_feed(db_factory, run_id, DAY, limit=0) == []


def test_activity_of_an_empty_day_and_an_auto_mode_proposal(db_factory: sessionmaker[Session]) -> None:
    run_id = _run(db_factory)
    assert activity_feed(db_factory, run_id, DAY) == []
    with db_factory() as s:
        sym = add_symbol(s, "AUTO")
        sig = _signal(s, run_id, _config(s, "orb_sip"), sym, DAY, at_et(DAY, 9, 35))
        p = _proposal(s, run_id, sig, sym, kind="entry", created_at=at_et(DAY, 9, 36), status="auto_approved")
        p.decided_at, p.decided_via, p.decided_by = p.created_at, "auto", "auto"
        failed = _proposal(s, run_id, sig, sym, kind="entry", created_at=at_et(DAY, 9, 50), status="failed")
        failed.error = "order 7 is no longer working"
        s.commit()
        pid, fid = p.id, failed.id
    items = {i.id: i for i in activity_feed(db_factory, run_id, DAY)}
    assert items[f"proposal_created:{pid}"].text == "Proposal entry AUTO 10 sh (auto)"
    assert items[f"proposal_approved:{pid}"].text == "Approved entry AUTO via auto at 07:36 MT"
    # at the same instant the approval is listed above the creation (newest first, lifecycle order)
    order = [i.id for i in activity_feed(db_factory, run_id, DAY)]
    assert order.index(f"proposal_approved:{pid}") < order.index(f"proposal_created:{pid}")
    assert items[f"proposal_rejected:{fid}"].text == (
        "Failed entry AUTO via telegram at 07:50 MT: order 7 is no longer working"
    )
    assert items[f"proposal_rejected:{fid}"].tone == "warn"


def test_activity_day_is_the_et_day(db_factory: sessionmaker[Session]) -> None:
    """00:00 ET belongs to the new day; 23:59:59 ET to the old one (bounds of S5)."""
    run_id = _run(db_factory)
    with db_factory() as s:
        start, end = s5_day_bounds(DAY)
        first = _event(s, run_id, "error", "engine", "first", start)
        last = _event(s, run_id, "error", "engine", "last", end - timedelta(seconds=1))
        _event(s, run_id, "error", "engine", "next day", end)
        _event(s, run_id, "error", "engine", "day before", start - timedelta(seconds=1))
        s.commit()
    assert [i.id for i in activity_feed(db_factory, run_id, DAY)] == [f"alert:{last}", f"alert:{first}"]


# --- 2. rejections ------------------------------------------------------------------------------------------


def _decisions(s: Session, run_id: int, rows: list[dict[str, Any]], *, final: bool = False) -> None:
    recorded = at_et(DAY, 10, 0)
    s.execute(
        insert(m.DecisionLog),
        [
            {
                "run_id": run_id,
                "session_date": DAY,
                "seq": i + 1,
                "ts": at_et(DAY, 9, 35),
                "recorded_at": recorded,
                "final": final,
                "ref": {},
                "data": {},
                "rule": None,
                "ticker": None,
                "symbol_id": None,
                **r,
            }
            for i, r in enumerate(rows)
        ],
    )


def test_rejections_by_stage_and_rule_from_the_decision_log(db_factory: sessionmaker[Session]) -> None:
    run_id = _run(db_factory)
    with db_factory() as s:
        ids = _symbols(s, "R", 800)
        rules = ["rvol_below_min"] * 500 + ["no_opening_bar"] * 200 + ["outside_top_n"] * 100
        rows: list[dict[str, Any]] = [
            {"stage": "scan", "outcome": "rejected", "rule": rule, "symbol_id": sid, "ticker": f"R{i:03d}"}
            for i, (sid, rule) in enumerate(zip(ids, rules, strict=True))
        ]
        rows += [
            {"stage": "scan", "outcome": "passed", "symbol_id": ids[0], "ticker": "R000"},
            # the scan summary of an `all` scan: its counts are ignored (the rows are there)
            {
                "stage": "scan",
                "outcome": "info",
                "symbol_id": None,
                "data": {"scan_detail": "all", "counts": {"rejects_by_rule": {"rvol_below_min": 500}}},
            },
            {
                "stage": "risk",
                "outcome": "rejected",
                "rule": "max_positions",
                "symbol_id": ids[1],
                "ticker": "R001",
            },
            {"stage": "risk", "outcome": "rejected", "rule": None, "symbol_id": ids[2], "ticker": None},
            # the signal row repeating a risk rejection is not counted twice
            {"stage": "signal", "outcome": "rejected", "rule": "max_positions", "symbol_id": ids[1]},
        ]
        _decisions(s, run_id, rows, final=True)
        seed_replay_noise(s, run_id)
        s.commit()
    out = rejections(db_factory, run_id, DAY)
    assert (out.session_date, out.source, out.total, out.final) == (DAY, "decision_log", 802, True)
    assert out.recorded_at == at_et(DAY, 10, 0)
    summary = [(r.stage, r.rule, r.count, len(r.tickers), r.truncated) for r in out.rules]
    assert summary == [
        ("scan", "rvol_below_min", 500, 50, True),
        ("scan", "no_opening_bar", 200, 50, True),
        ("scan", "outside_top_n", 100, 50, True),
        ("risk", "max_positions", 1, 1, False),
        ("risk", "unknown", 1, 1, False),  # no rule stored; the ticker comes from `symbols`
    ]
    top = out.rules[0]
    assert top.tickers == sorted(top.tickers) and top.tickers[0] == "R000"
    assert top.link == "/reports?day=2026-10-06&stage=scan&outcome=rejected"
    assert out.rules[3].link == "/reports?day=2026-10-06&stage=risk&outcome=rejected"
    assert out.rules[3].tickers == ["R001"] and out.rules[4].tickers == ["R002"]


def test_rejections_of_a_summary_only_scan(db_factory: sessionmaker[Session]) -> None:
    run_id = _run(db_factory)
    with db_factory() as s:
        ids = _symbols(s, "S", 3)
        rows: list[dict[str, Any]] = [
            {
                "stage": "scan",
                "outcome": "info",
                "symbol_id": None,
                "data": {
                    "scan_detail": "ranked",
                    "counts": {"rejects_by_rule": {"rvol_below_min": 400, "outside_top_n": 30}},
                },
            },
            *(
                {
                    "stage": "scan",
                    "outcome": "rejected",
                    "rule": "outside_top_n",
                    "symbol_id": sid,
                    "ticker": f"S{i}",
                }
                for i, sid in enumerate(ids)
            ),
            {
                "stage": "risk",
                "outcome": "rejected",
                "rule": "max_open_risk",
                "symbol_id": ids[0],
                "ticker": "S0",
            },
        ]
        _decisions(s, run_id, rows)
        s.commit()
    out = rejections(db_factory, run_id, DAY)
    assert (out.source, out.total, out.final) == ("decision_log", 431, False)
    assert [(r.stage, r.rule, r.count, r.tickers, r.truncated) for r in out.rules] == [
        ("scan", "rvol_below_min", 400, [], False),
        ("scan", "outside_top_n", 30, [], False),
        ("risk", "max_open_risk", 1, ["S0"], False),
    ]


def test_rejections_from_candidates_before_the_decision_log_has_the_scan(
    db_factory: sessionmaker[Session],
) -> None:
    run_id = _run(db_factory)
    with db_factory() as s:
        seed_session_day(s, run_id)  # candidates only: no decision-log rows yet
        s.commit()
    out = rejections(db_factory, run_id, DAY)
    assert (out.source, out.total, out.final, out.recorded_at) == (
        "candidates",
        CANDIDATES - PASSED,
        False,
        None,
    )
    got = {(r.stage, r.rule): (r.count, len(r.tickers), r.truncated) for r in out.rules}
    odd = sum(1 for i in range(PASSED, CANDIDATES) if i % 2)
    assert got == {
        ("scan", "rvol_below_min"): (odd, 50, True),
        ("scan", "no_opening_bar"): (CANDIDATES - PASSED - odd, 50, True),
    }


def test_rejections_of_an_empty_day(db_factory: sessionmaker[Session]) -> None:
    run_id = _run(db_factory)
    with db_factory() as s:
        seed_replay_noise(s, run_id)  # a replay's decision log and candidates on the same day
        s.commit()
    out = rejections(db_factory, run_id, DAY)
    assert (out.source, out.total, out.rules, out.final, out.recorded_at) == ("none", 0, [], False, None)


# --- S14: a fixed number of read-only statements, whatever the row count ----------------------------------


def _statements(factory: sessionmaker[Session], fn: Callable[[], object]) -> list[str]:
    engine = factory.kw["bind"]
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


def test_feed_and_rejections_statements_do_not_grow_with_rows(db_factory: sessionmaker[Session]) -> None:
    run_id = _run(db_factory)
    empty_feed = _statements(db_factory, lambda: activity_feed(db_factory, run_id, DAY))
    empty_rej = _statements(db_factory, lambda: rejections(db_factory, run_id, DAY))
    with db_factory() as s:
        seed_session_day(s, run_id)
        s.commit()
    full_feed = _statements(db_factory, lambda: activity_feed(db_factory, run_id, DAY))
    full_rej = _statements(db_factory, lambda: rejections(db_factory, run_id, DAY))
    assert len(full_feed) == len(empty_feed) + 1  # the scan's top tickers are read only when there was a scan
    assert len(full_rej) == len(empty_rej) + 1  # the candidates are grouped only when there are some
    assert (len(full_feed), len(full_rej)) == (9, 4)  # 531 rejections, 17 items: still a fixed count
    print(f"activity_feed: {len(full_feed)} statements, rejections: {len(full_rej)}")
    assert all(st.lstrip().upper().startswith("SELECT") for st in full_feed + full_rej)
