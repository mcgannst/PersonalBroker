"""P6-T10 acceptance tests 13 and 16 (and the replay half of 3): the golden replay gives the same results with
the decision log on and off; the journal is written under the replay's own run id with its pinned params; a
recorder that raises leaves the replay completed with one warning event."""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.replay.test_golden import (
    EXPECTED_PATH,
    FRI,
    MON,
    TUE,
    WALL,
    WED,
    create_golden_replay,
    golden_part,
    load_data,
    normalized,
    seed_from_data,
)
from trader.db import models as m
from trader.decisions.summary import summary_from_json
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.replay import runner
from trader.replay.types import ReplayRun
from trader.settings_store import SettingsStore

pytestmark = pytest.mark.db
CAL = SessionCalendar()
FIELDS = ("factory", "wall", "calendar", "market_factory", "catalysts_factory", "engine_factory")


def _core(factory: sessionmaker[Session], wall: FixedClock) -> Any:
    return cast(
        Any,
        SimpleNamespace(
            factory=factory,
            clock=wall,
            calendar=CAL,
            settings=SettingsStore(factory, now=wall.now),
            crypto=None,
        ),
    )


async def _run(factory: sessionmaker[Session], wall: FixedClock, label: str, *, enabled: bool) -> int:
    SettingsStore(factory, now=wall.now).set("reports.decisions_enabled", enabled, "test")
    run_id = create_golden_replay(factory, wall, label)
    async with runner.open_replay_deps(_core(factory, wall), data_mode="offline") as deps:
        assert deps.decisions is not None  # the real composition wires the hook
        final = await runner.run_replay(deps, run_id)
    assert final.status == "completed", final.error
    return run_id


def _rows(factory: sessionmaker[Session], run_id: int | None = None) -> list[m.DecisionLog]:
    with factory() as s:
        q = select(m.DecisionLog).order_by(
            m.DecisionLog.run_id, m.DecisionLog.session_date, m.DecisionLog.seq
        )
        if run_id is not None:
            q = q.where(m.DecisionLog.run_id == run_id)
        return list(s.execute(q).scalars())


async def test_13_decisions_unchanged_with_the_log_on_and_off(db_factory: sessionmaker[Session]) -> None:
    seed_from_data(db_factory, load_data())
    on_id = await _run(db_factory, FixedClock(WALL), "decisions on", enabled=True)
    off_id = await _run(db_factory, FixedClock(WALL + timedelta(hours=3)), "decisions off", enabled=False)
    on, off = normalized(db_factory, on_id), normalized(db_factory, off_id)

    expected = json.loads(EXPECTED_PATH.read_text())
    assert golden_part(on) == golden_part(off) == expected
    assert on["orders"] == off["orders"] and on["ledger"] == off["ledger"]

    rows = _rows(db_factory)
    assert rows and {r.run_id for r in rows} == {on_id}  # only the enabled run, all with its run id
    assert {r.session_date for r in rows} == {MON, TUE, WED, FRI}  # Thanksgiving is not a session
    assert all(r.final for r in rows)


async def test_3_16_the_replay_journal_uses_its_pinned_params_and_its_rows(
    db_factory: sessionmaker[Session],
) -> None:
    seed_from_data(db_factory, load_data())
    run_id = await _run(db_factory, FixedClock(WALL), "journal", enabled=True)
    rows = _rows(db_factory, run_id)
    by_day: dict[Any, list[m.DecisionLog]] = {}
    for r in rows:
        by_day.setdefault(r.session_date, []).append(r)

    mon = by_day[MON]
    assert [r.seq for r in mon] == list(range(1, len(mon) + 1))
    assert mon[-1].stage == "day"
    scan = [r for r in mon if r.stage == "scan" and r.symbol_id is not None]
    # the four ranked names with the strategy's own reasons (the golden hand checks)
    assert [(r.ticker, r.outcome, r.rule) for r in scan if r.data.get("rank") is not None] == [
        ("BBB", "passed", None),
        ("DDD", "rejected", "bearish_candle"),
        ("AAA", "rejected", "lower_rank"),
        ("CCC", "rejected", "catalyst_missing"),
    ]
    summary_row = next(r for r in mon if r.stage == "scan" and r.symbol_id is None)
    assert summary_row.data["params_in_effect"]["source"] == "replay_pin"
    assert summary_row.data["params"]["rvol_min"] == "1.00"
    bbb = next(r for r in scan if r.ticker == "BBB")
    d = {k: Decimal(bbb.data[k]) for k in ("or_high", "or_low", "entry", "stop_loss", "r_per_share")}
    assert d == {
        "or_high": Decimal("20.50"),
        "or_low": Decimal("19.95"),
        "entry": Decimal("20.51"),
        "stop_loss": Decimal("20.41"),
        "r_per_share": Decimal("0.10"),
    }
    assert bbb.data["target"] is None
    assert all(c["passed"] for c in bbb.data["checks"])
    # Monday's trade: a same-bar stop-out, recorded as a fill below the stop (worse) and a stop exit
    fills = [r for r in mon if r.stage == "fill"]
    assert [r.data["purpose"] for r in fills] == ["entry", "stop"]
    assert all(r.data["diff_per_share"] is not None and r.data["diff_per_share"][0] != "-" for r in fills)
    (exit_row,) = [r for r in mon if r.stage == "exit"]
    assert (exit_row.rule, exit_row.reason) == ("stop", "protective_stop")
    approvals = [r.outcome for r in mon if r.stage == "approval"]
    assert approvals and set(approvals) == {"auto_approved"}

    wed = by_day[WED]  # the biased day: the overlay exits AAA
    assert any(r.stage == "overlay" and r.rule == "exit" for r in wed)
    (wed_exit,) = [r for r in wed if r.stage == "exit"]
    assert wed_exit.rule == "overlay"
    fri_exit = next(r for r in by_day[FRI] if r.stage == "exit")
    assert fri_exit.rule == "flatten"

    day = summary_from_json(mon[-1].data)
    assert (day.run_id, day.session_date, day.final) == (run_id, MON, True)
    assert day.trades == 1 and day.losses == 1 and day.fills == 2
    assert dict(day.rejects_by_rule)["bearish_candle"] == 1
    assert mon[-1].data["text"] and len(mon[-1].data["text"]) <= 400


async def test_16_a_raising_recorder_leaves_the_replay_completed(db_factory: sessionmaker[Session]) -> None:
    seed_from_data(db_factory, load_data())
    wall = FixedClock(WALL)
    run_id = create_golden_replay(db_factory, wall, "raising")
    calls: list[Any] = []

    async def boom(run: ReplayRun, day: Any, scan: Any) -> None:
        calls.append(day)
        raise RuntimeError("recorder down password=hunter2")

    async with runner.open_replay_deps(_core(db_factory, wall), data_mode="offline") as deps:
        final = await runner.run_replay(runner.ReplayDeps(*[getattr(deps, f) for f in FIELDS], boom), run_id)
    assert final.status == "completed", final.error
    assert calls == [MON, TUE, WED, FRI]
    with db_factory() as s:
        events = list(
            s.execute(
                select(m.EventLog).where(m.EventLog.source == "decisions").order_by(m.EventLog.id)
            ).scalars()
        )
    assert len(events) == 4 and {e.run_id for e in events} == {run_id}
    assert {e.level for e in events} == {"warning"}
    assert all("hunter2" not in e.message and "hunter2" not in json.dumps(e.data) for e in events)
    assert golden_part(normalized(db_factory, run_id)) == json.loads(EXPECTED_PATH.read_text())
    assert _rows(db_factory) == []


async def test_16_a_snapshot_with_the_log_off_calls_no_hook(db_factory: sessionmaker[Session]) -> None:
    seed_from_data(db_factory, load_data())
    wall = FixedClock(WALL)
    SettingsStore(db_factory, now=wall.now).set("reports.decisions_enabled", False, "test")
    run_id = create_golden_replay(db_factory, wall, "off")
    # the live setting is turned back on after the snapshot: the replay follows its snapshot
    SettingsStore(db_factory, now=wall.now).set("reports.decisions_enabled", True, "test")
    calls: list[Any] = []

    async def hook(run: ReplayRun, day: Any, scan: Any) -> None:
        calls.append(day)

    async with runner.open_replay_deps(_core(db_factory, wall), data_mode="offline") as deps:
        final = await runner.run_replay(runner.ReplayDeps(*[getattr(deps, f) for f in FIELDS], hook), run_id)
    assert final.status == "completed" and calls == []


async def test_3_a_replay_shows_its_pinned_override_not_the_live_params(
    db_factory: sessionmaker[Session],
) -> None:
    from trader.replay.types import ReplayRequest, StrategyOverride
    from trader.strategies.registry import StrategyRegistry

    seed_from_data(db_factory, load_data())
    wall = FixedClock(WALL)
    request = ReplayRequest(
        MON,
        TUE,
        label="pinned",
        strategies={"orb_sip": StrategyOverride(params={"rvol_min": "1.5"})},
        offline=True,
    )
    run_id = runner.create_replay(
        db_factory,
        wall,
        CAL,
        SettingsStore(db_factory, now=wall.now),
        StrategyRegistry(db_factory, wall),
        request,
        "test",
    )
    async with runner.open_replay_deps(_core(db_factory, wall), data_mode="offline") as deps:
        final = await runner.run_replay(deps, run_id)
    assert final.status == "completed", final.error
    journal = _rows(db_factory, run_id)
    summaries = [r for r in journal if r.stage == "scan" and r.symbol_id is None]
    assert [r.session_date for r in summaries] == [MON, TUE]
    for r in summaries:
        assert r.data["params"]["rvol_min"] == "1.5"
        info = r.data["params_in_effect"]
        assert (info["source"], info["scope"]) == ("replay_pin", "replay")
    # Tuesday's DDD (rvol 0.5) is below the pinned minimum, shown with the pinned threshold
    ddd = next(r for r in journal if r.session_date == TUE and r.ticker == "DDD" and r.stage == "scan")
    assert ddd.rule == "rvol_below_min" and ddd.data["checks"][0]["threshold"] == "1.5"
