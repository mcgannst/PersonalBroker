"""P5-T18 acceptance test 3: replay isolation in the database (Review Focus 2).

A live run with real activity (a trade, a journal answer, an event, a job run, a sent notification, a stored
setting, relay cursors), then an OFFLINE replay of the golden week through the real composition with a
settings override and a strategy override. Counting and comparing every table before and after:
- only the tables of the decision "Replay isolation" changed, and every new row with a run id carries the
  replay's (no new `event_log` row without a run id);
- `audit_log` gained exactly `replay.start`; `strategy_configs` gained only one `replay`-scoped row;
- `job_runs`, `notifications`, `catalysts`, `universe_snapshots`, `open_bar_stats`, `candle_archive`,
  `intraday_candles`, `daily_candles`, `settings`, `notify_cursors` (and every other table) are unchanged;
- `compute_metrics(live)` and `GET /api/dashboard` are identical before and after;
- a kill switch trips inside the replay with the replay's run id, and one relay pump sends nothing.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Table, select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from tests.fakes_telegram import FakeMessenger, RecordingNotifier
from tests.replay.test_golden import FRI, MON, TUE, WALL, load_data, seed_from_data
from trader.api.routers import dashboard
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.notify.messages import MessageRenderer
from trader.notify.relay import NotificationRelay
from trader.replay import runner
from trader.replay.types import ReplayRequest, StrategyOverride
from trader.reports.metrics import compute_metrics
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db

CAL = SessionCalendar()
LIVE_DAY = date(2026, 11, 20)  # the live run's last session before the replayed week
# Tables a replay may change (decision "Replay isolation"): its runs row, its sim account, rows carrying its
# run id, its replay-scoped strategy config, and its `replay.start` audit row.
MAY_CHANGE = frozenset(
    {
        "runs",
        "sim_accounts",
        "candidates",
        "signals",
        "proposals",
        "orders",
        "fills",
        "positions",
        "trades",
        "cash_ledger",
        "equity_snapshots",
        "kill_switch_events",
        "event_log",
        "strategy_configs",
        "audit_log",
        "decision_log",
    }
)
MUST_NOT_CHANGE = (
    "job_runs",
    "notifications",
    "notify_cursors",
    "catalysts",
    "universe_snapshots",
    "open_bar_stats",
    "candle_archive",
    "intraday_candles",
    "daily_candles",
    "settings",
    "symbols",
    "telegram_callbacks",
    "worker_heartbeats",
    "journal",
    "weekly_reports",
)


def _tables() -> list[Table]:
    return list(m.Base.metadata.sorted_tables)


def _pk(table: Table, row: Any) -> tuple[Any, ...]:
    return tuple(row[c.name] for c in table.primary_key.columns)


def dump(factory: sessionmaker[Session]) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    """Every row of every table, by primary key."""
    out: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    with factory() as s:
        for t in _tables():
            cols = list(t.primary_key.columns) or list(t.c)
            rows = s.execute(select(t).order_by(*cols)).mappings().all()
            out[t.name] = {_pk(t, r): dict(r) for r in rows}
    return out


def seed_live(factory: sessionmaker[Session], symbols: dict[str, int]) -> int:
    """The live run's own activity before the replay (all committed); returns the live run id."""
    clock = FixedClock(WALL - timedelta(days=7))
    SettingsStore(factory, now=clock.now).set("approval_mode", "manual", "web:stephen")
    run = get_live_run(factory, clock, SettingsStore(factory, now=clock.now).load())
    at = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
    with session_scope(factory) as s:
        pos = m.Position(
            run_id=run.id,
            symbol_id=symbols["AAA"],
            strategy_config_id=None,
            qty=0,
            avg_price=Decimal("10"),
            stop_loss=Decimal("9.90"),
            planned_risk=Decimal("5"),
            session_date=LIVE_DAY,
            opened_at=at,
            closed_at=at + timedelta(hours=2),
            entry_order_id=1,
            stop_order_id=None,
            unprotected_since=None,
            unprotected_seconds=0,
        )
        s.add(pos)
        s.flush()
        s.add(
            m.Trade(
                run_id=run.id,
                position_id=pos.id,
                symbol_id=symbols["AAA"],
                session_date=LIVE_DAY,
                entry_price=Decimal("10"),
                exit_price=Decimal("10.25"),
                qty=50,
                pnl=Decimal("12.5000"),
                pnl_r=Decimal("2.5000"),
                planned_risk=Decimal("5"),
                exit_reason="flatten_close",
                slippage_total=Decimal("0.02"),
                fees_total=Decimal("0.01"),
                opened_at=at,
                closed_at=at + timedelta(hours=2),
            )
        )
        s.add(
            m.EquitySnapshot(
                run_id=run.id,
                ts=at + timedelta(hours=3),
                equity=Decimal("732.50"),
                cash=Decimal("732.50"),
                settled_cash=Decimal("720.00"),
                peak_equity=Decimal("732.50"),
                drawdown_pct=Decimal("0"),
            )
        )
        s.add(m.Journal(run_id=run.id, session_date=LIVE_DAY, rules_followed=True, answered_via="web"))
        s.add(
            m.EventLog(
                ts=at, level="warning", source="engine", run_id=run.id, message="live warning", data={}
            )
        )
        s.add(
            m.JobRun(
                job="postclose",
                session_date=LIVE_DAY,
                started_at=at + timedelta(hours=6),
                finished_at=at + timedelta(hours=6, minutes=1),
                status="succeeded",
                error=None,
                detail={},
            )
        )
        s.add(
            m.Notification(
                kind="daily_summary",
                dedupe_key=f"daily:{LIVE_DAY.isoformat()}",
                text="daily summary",
                created_at=at + timedelta(hours=6),
                sent_at=at + timedelta(hours=6),
                status="sent",
                message_ids=[1],
                attempts=1,
                error=None,
            )
        )
    return run.id


def relay_for(
    factory: sessionmaker[Session], clock: FixedClock, run_id: int
) -> tuple[NotificationRelay, Any]:
    notifier = RecordingNotifier()
    relay = NotificationRelay(
        factory,
        clock,
        notifier,
        MessageRenderer("https://trader.test", ZoneInfo("America/Edmonton"), clock=clock),
        FakeMessenger(),
        run_id,
        settings=RuntimeSettings,
    )
    return relay, notifier


def _metrics(factory: sessionmaker[Session], run_id: int) -> Any:
    return compute_metrics(factory, run_id)


async def test_3_an_offline_replay_changes_only_its_own_rows(db_factory: sessionmaker[Session]) -> None:
    symbols = seed_from_data(db_factory, load_data())
    live_id = seed_live(db_factory, symbols)
    wall = FixedClock(WALL)

    relay, notifier = relay_for(db_factory, wall, live_id)
    await relay.pump()  # the relay has caught up with the live run's history
    notifier.sent.clear()

    client = make_client(make_services(test_core(db_factory, wall)), dashboard.router)
    dash_before = client.get("/api/dashboard")
    assert dash_before.status_code == 200
    metrics_before = _metrics(db_factory, live_id)
    live_settings_before = SettingsStore(db_factory, now=wall.now).load()
    live_configs_before = {
        k: StrategyRegistry(db_factory, wall).current(k) for k in StrategyRegistry(db_factory, wall).keys()
    }
    before = dump(db_factory)

    # a settings override (a tight daily-loss switch, so a kill switch trips inside the replay) and a
    # strategy override (a replay-scoped config row)
    replay_id = runner.create_replay(
        db_factory,
        wall,
        CAL,
        SettingsStore(db_factory, now=wall.now),
        StrategyRegistry(db_factory, wall),
        ReplayRequest(
            MON,
            FRI,
            label="isolation",
            overrides={"killswitch.daily_loss_pct": "0.01"},
            strategies={"orb_sip": StrategyOverride(params={"catalyst_min_quality": 60})},
            offline=True,
        ),
        "web:stephen",
    )
    core = SimpleNamespace(
        factory=db_factory,
        clock=wall,
        calendar=CAL,
        settings=SettingsStore(db_factory, now=wall.now),
        crypto=None,
    )
    async with runner.open_replay_deps(cast(Any, core), data_mode="offline") as deps:
        final = await runner.run_replay(deps, replay_id)
    assert final.status == "completed", final.error
    assert final.data_mode == "offline"
    after = dump(db_factory)

    # 1. only the allowed tables changed; every other table is identical, row for row
    changed = {name for name in before if before[name] != after[name]}
    assert changed <= MAY_CHANGE, changed - MAY_CHANGE
    for name in MUST_NOT_CHANGE:
        assert before[name] == after[name], name
    # rows that existed before are untouched in every table except `runs` (only the replay's own row is new)
    for name in MAY_CHANGE:
        for key, row in before[name].items():
            assert after[name].get(key) == row, (name, key)

    # 2. every new row with a run id carries the replay's; no new event without a run id
    new: dict[str, list[dict[str, Any]]] = {
        name: [row for key, row in after[name].items() if key not in before[name]] for name in after
    }
    for t in _tables():
        if "run_id" in t.c and t.name != "runs":
            assert all(r["run_id"] == replay_id for r in new[t.name]), t.name
    assert [r["id"] for r in new["runs"]] == [replay_id]
    assert new["event_log"] and all(r["run_id"] == replay_id for r in new["event_log"])
    assert new["trades"] and new["candidates"] and new["sim_accounts"]

    # 3. the audit log gained exactly replay.start; strategy_configs only one replay-scoped row
    assert [(r["action"], r["actor"]) for r in new["audit_log"]] == [("replay.start", "web:stephen")]
    assert [(r["scope"], r["strategy_key"], r["created_by"]) for r in new["strategy_configs"]] == [
        ("replay", "orb_sip", f"replay:{replay_id}")
    ]
    assert StrategyRegistry(db_factory, wall).current("orb_sip") == live_configs_before["orb_sip"]
    assert SettingsStore(db_factory, now=wall.now).load() == live_settings_before
    assert live_settings_before.approval_mode == "manual"  # the replay's automatic approvals never touched it

    # 4. a kill switch tripped inside the replay, with the replay's run id
    trips = new["kill_switch_events"]
    assert trips and all(t["run_id"] == replay_id for t in trips)
    assert {t["switch"] for t in trips} == {"daily_loss_pct"}
    assert {t["session_date"] for t in trips} <= {MON, TUE}

    # 5. the live views and metrics are identical
    assert _metrics(db_factory, live_id) == metrics_before
    dash_after = client.get("/api/dashboard")
    assert dash_after.status_code == 200 and dash_after.json() == dash_before.json()

    # 6. nothing is relayed
    await relay.pump()
    assert notifier.sent == []
