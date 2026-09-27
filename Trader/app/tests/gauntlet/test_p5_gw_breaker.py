"""P5-GW gauntlet (Breaker), attempt 1: the web/API group of Phase 5 (P5-T7 replay API and live views,
P5-T12 reports API and CSV columns).

Targets: replay rows never reaching a live route or a live SSE watermark (Review Focus 2), the replay start
and cancel paths with the REAL runner functions (busy, validation echo, the offline window, a cancel race),
the launcher never spawning for a run that is not queued, the weekly report lookup around DST and holiday
weeks, a report of a replay run never served as the live one, `telegram_status`, and the CSV formula guard on
every new text column. Real test database (testcontainers), TestClient, fakes for the launcher and quotes.
"""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.api.test_replays import add_replay
from tests.api.test_trading import _proposal, _signal, seed_closed_trade, seed_open_position
from tests.factories import add_symbol
from tests.fakes_api import FakeReplayLauncher, make_services, test_core
from trader.api import auth
from trader.api.deps import ApiServices
from trader.api.errors import install_error_handlers
from trader.api.feed import watermarks
from trader.api.replay_launcher import SubprocessReplayLauncher
from trader.api.routers import (
    dashboard,
    journal,
    killswitch,
    performance,
    proposals,
    replays,
    reports,
    strategies,
    system,
    trading,
)
from trader.api.routers.auth import router as auth_router
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import ET, FixedClock
from trader.replay.runner import REPLAY_LOCK_KEY
from trader.reports.metrics import OPEN_HIGH, OPEN_LOW, HistogramBin, Metrics
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

DAY = date(2026, 10, 6)  # a Tuesday session
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # 10:00 ET
MARK = "RPLY"  # every replay-owned text carries it
MARK_NUMBER = "777.77"  # and every replay-owned amount
PASSWORD = "Gauntlet-Password-8812"  # noqa: S105 (a throwaway test password)
LIVE_ROUTERS = (
    dashboard.router,
    trading.router,
    proposals.router,
    performance.router,
    journal.router,
    killswitch.router,
    system.router,
    strategies.router,
    reports.router,
)


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


def _metrics(run_id: int, date_from: date | None, date_to: date | None) -> Metrics:
    return Metrics(
        run_id=run_id,
        date_from=date_from,
        date_to=date_to,
        trades=0,
        wins=0,
        losses=0,
        win_rate=None,
        avg_win_r=None,
        avg_loss_r=None,
        expectancy_r=None,
        profit_factor=None,
        avg_slippage=None,
        avg_slippage_per_share=None,
        max_drawdown_pct=None,
        adherence_pct=None,
        total_pnl=Decimal("0"),
        total_fees=Decimal("0"),
        trades_without_r=0,
        r_histogram=(HistogramBin(OPEN_LOW, Decimal("-3.0"), 0), HistogramBin(Decimal("5.0"), OPEN_HIGH, 0)),
    )


@pytest.fixture(autouse=True)
def _replay_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """`compute_metrics` is P5-T2's and may still be a stub: the replay routes get a fixed one."""
    monkeypatch.setattr(
        replays, "compute_metrics", lambda factory, run_id, f=None, t=None: _metrics(run_id, f, t)
    )


def _replay_rows(s: Session, replay: int, cfg: int) -> dict[str, int]:
    """One row of every run-scoped table for the replay, on the same session day as the live rows: a closed
    trade, an open position with a working stop, a pending proposal, a candidate, a kill-switch trip, an
    event and a journal answer. Every text carries MARK and every amount MARK_NUMBER."""
    closed = seed_closed_trade(s, replay, ticker=f"{MARK}A", pnl="777.7700")
    opened = seed_open_position(s, replay, ticker=f"{MARK}B")
    sym = add_symbol(s, f"{MARK}C")
    sig = _signal(s, replay, cfg, sym, DAY, NOW - timedelta(seconds=30))
    pending = _proposal(
        s, replay, sig, sym, kind="entry", created_at=NOW - timedelta(seconds=10), status="pending"
    )
    pending.expires_at = NOW + timedelta(seconds=80)
    s.add(
        m.Candidate(
            run_id=replay,
            session_date=DAY,
            strategy_key="orb_sip",
            symbol_id=sym,
            rvol=Decimal("777.7700"),
            rank=1,
            candle=None,
            passed=True,
            reject_reason=None,
            data={"note": MARK},
            created_at=NOW - timedelta(minutes=20),
        )
    )
    s.add(
        m.KillSwitchEvent(
            run_id=replay,
            switch="daily_loss_pct",
            session_date=DAY,
            tripped_at=NOW - timedelta(minutes=5),
            value=Decimal("777.770000"),
            threshold=Decimal("0.050000"),
        )
    )
    s.add(
        m.EventLog(
            ts=NOW - timedelta(minutes=1),
            level="error",
            source="engine",
            run_id=replay,
            message=f"{MARK} event",
            data={"x": MARK},
        )
    )
    s.add(
        m.Journal(
            run_id=replay,
            session_date=DAY,
            rules_followed=False,
            notes=f"{MARK} journal",
            answered_via="web",
            updated_at=NOW,
        )
    )
    s.flush()
    return {
        "position_closed": closed.position_id,
        "position_open": opened.position_id,
        "proposal": pending.id,
    }


# --- isolation: every live route ---------------------------------------------------------------------------


def test_every_live_route_hides_a_replays_rows(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 2: with a running replay's rows beside the live run's on the same day, no live route
    (Dashboard, trading lists, proposals, performance, export, journal, kill switches, System, strategies)
    shows a replay row; a replay's position and proposal ids are 404 on the live detail routes."""
    clock = FixedClock(NOW)
    services = make_services(test_core(db_factory, clock))
    services.registry.ensure_defaults("test")
    live = get_live_run(db_factory, clock, RuntimeSettings()).id
    base = services.registry.current("orb_sip")
    services.registry.create_replay_config(
        "orb_sip", base=base, params={"top_n": 177}, enabled=True, created_by="replay:x"
    )
    with db_factory() as s:
        replay = add_replay(s, status="running", label=f"{MARK} run")
        seed_closed_trade(s, live, ticker="LIVEA")
        seed_open_position(s, live, ticker="LIVEB")
        ids = _replay_rows(s, replay, base.id)
        s.commit()
    seen: list[int] = []

    def fake_metrics(factory: Any, run_id: int, d_from: Any = None, d_to: Any = None) -> Metrics:
        seen.append(run_id)
        return _metrics(run_id, d_from, d_to)

    monkeypatch.setattr(performance, "compute_metrics", fake_metrics)
    client = make_client(services, *LIVE_ROUTERS)
    day = DAY.isoformat()
    paths = [
        "/api/dashboard",
        "/api/proposals",
        f"/api/candidates?date={day}",
        f"/api/orders?date={day}",
        f"/api/fills?date={day}",
        "/api/positions",
        f"/api/positions?status=all&date={day}",
        "/api/trades",
        "/api/trades?run=live",
        "/api/metrics",
        "/api/equity",
        "/api/export/trades.csv",
        f"/api/journal?from={day}&to={day}",
        "/api/killswitch",
        "/api/events",
        "/api/events?since=0",
        "/api/system",
        "/api/strategies",
    ]
    leaks = {}
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
        if MARK in r.text or MARK_NUMBER in r.text or '"top_n":177' in r.text:
            leaks[path] = r.text[:400]
    assert leaks == {}
    assert seen == [live]  # /api/metrics asked for the live run only
    for path in (
        f"/api/positions/{ids['position_closed']}",
        f"/api/positions/{ids['position_open']}",
        f"/api/proposals/{ids['proposal']}",
    ):
        assert client.get(path).status_code == 404, path
    approve = client.post(f"/api/proposals/{ids['proposal']}/approve")
    assert approve.status_code == 404, approve.text
    with db_factory() as s:
        assert s.get(m.Proposal, ids["proposal"]).status == "pending"  # type: ignore[union-attr]
    # the live dashboard's kill switches are untouched by the replay's trip
    ks = client.get("/api/killswitch").json()
    assert "777.77" not in str(ks)


def test_updates_to_a_replays_rows_move_no_trading_watermark(db_factory: sessionmaker[Session]) -> None:
    """Updates (not only inserts) of a replay's rows: a proposal decided, an order closed, a position closed
    and unprotected, a kill-switch reset and a new replay-scoped config leave every trading topic and
    `strategies` unchanged; only `replays` moves when the run row changes."""
    clock = FixedClock(NOW)
    services = make_services(test_core(db_factory, clock))
    services.registry.ensure_defaults("test")
    base = services.registry.current("orb_sip")
    get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        replay = add_replay(s, status="running")
        ids = _replay_rows(s, replay, base.id)
        s.commit()
    with db_factory() as s:
        before = watermarks(s)
    later = NOW + timedelta(minutes=3)
    with db_factory() as s:
        s.execute(
            update(m.Proposal)
            .where(m.Proposal.id == ids["proposal"])
            .values(status="expired", decided_at=later, expired_at=later)
        )
        s.execute(update(m.Order).where(m.Order.run_id == replay).values(status="cancelled", closed_at=later))
        s.execute(
            update(m.Position)
            .where(m.Position.id == ids["position_open"])
            .values(closed_at=later, unprotected_since=later)
        )
        s.execute(
            update(m.KillSwitchEvent)
            .where(m.KillSwitchEvent.run_id == replay)
            .values(reset_at=later, reset_reason="r", reset_by="web:stephen")
        )
        s.commit()
    services.registry.create_replay_config(
        "orb_sip", base=base, params={"top_n": 7}, enabled=True, created_by=f"replay:{replay}"
    )
    with db_factory() as s:
        after = watermarks(s)
    for topic in (
        "proposals",
        "orders",
        "fills",
        "positions",
        "trades",
        "candidates",
        "killswitch",
        "events",
        "strategies",
    ):
        assert after[topic] == before[topic], topic
    with db_factory() as s:
        s.execute(update(m.Run).where(m.Run.id == replay).values(updated_at=later))
        s.commit()
        assert watermarks(s)["replays"] != after["replays"]


# --- the replay API with the real runner -------------------------------------------------------------------


BODY: dict[str, Any] = {"date_from": "2026-11-23", "date_to": "2026-11-27", "offline": False}
MONDAY_10_ET = datetime(2026, 11, 30, 10, 0, tzinfo=ET).astimezone(UTC)


def _real_services(factory: sessionmaker[Session], now: datetime) -> tuple[ApiServices, FakeReplayLauncher]:
    launcher = FakeReplayLauncher()
    services = make_services(test_core(factory, FixedClock(now)), replays=launcher)
    services.registry.ensure_defaults("test")
    get_live_run(factory, FixedClock(now), RuntimeSettings())
    return services, launcher


def _replay_count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return int(s.scalar(select(func.count()).select_from(m.Run).where(m.Run.mode == "replay")) or 0)


def test_a_start_while_one_is_queued_or_the_lock_is_held_is_409(db_factory: sessionmaker[Session]) -> None:
    services, launcher = _real_services(db_factory, MONDAY_10_ET)
    client = make_client(services, replays.router)
    with db_factory() as s:
        add_replay(s, status="queued", started_at=MONDAY_10_ET - timedelta(seconds=30))
        s.commit()
    busy = client.post("/api/replays", json=BODY)
    assert busy.status_code == 409, busy.text
    assert busy.json()["error"]["message"] == "A replay is already running."
    assert _replay_count(db_factory) == 1 and launcher.launched == []

    with db_factory() as s:  # nothing queued any more, but another process holds the replay lock
        s.execute(update(m.Run).where(m.Run.mode == "replay").values(status="completed"))
        s.commit()
    engine = db_factory.kw["bind"]
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": REPLAY_LOCK_KEY})
        try:
            locked = client.post("/api/replays", json=BODY)
            assert locked.status_code == 409, locked.text
            assert client.get("/api/replays/options").json()["busy"] is False  # no queued/running row
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": REPLAY_LOCK_KEY})
            holder.commit()
    assert _replay_count(db_factory) == 1 and launcher.launched == []
    ok = client.post("/api/replays", json=BODY)
    assert ok.status_code == 202, ok.text
    assert launcher.launched == [ok.json()["id"]]


def test_a_malformed_body_never_echoes_a_value_in_the_422(db_factory: sessionmaker[Session]) -> None:
    """The plan: a 422 names the location and never echoes the value. Every bad value carries a sentinel,
    in the settings overrides and in the strategy params (whose validators format the value)."""
    services, launcher = _real_services(db_factory, MONDAY_10_ET)
    client = make_client(services, replays.router)
    body = {
        **BODY,
        "overrides": {
            "risk_pct": "SENTINEL-risk",
            "replay.catalyst_mode": "SENTINEL-mode",
            "cash_account_mode": "SENTINEL-cash",
            "killswitch.expectancy_min_trades": "SENTINEL-min",
            "fees.commission": {"SENTINEL-nested": "1"},
        },
        "strategies": {
            "orb_sip": {
                "params": {
                    "entry_cancel_at": "SENTINEL-cancel-at",
                    "exit_at": "SENTINEL-exit-at",
                    "stale_universe": "SENTINEL-stale",
                    "top_n": "SENTINEL-top",
                }
            },
        },
    }
    r = client.post("/api/replays", json=body)
    assert r.status_code == 422, r.text
    locs = [".".join(map(str, f["loc"])) for f in r.json()["error"]["fields"]]
    assert "body.overrides.risk_pct" in locs and "body.strategies.orb_sip.params.exit_at" in locs
    assert "SENTINEL" not in r.text
    assert launcher.launched == [] and _replay_count(db_factory) == 0


@pytest.mark.parametrize(
    ("now_et", "date_to", "expected"),
    [
        (datetime(2026, 11, 30, 10, 0), "2026-11-27", "offline"),  # market hours: offline even if asked full
        (datetime(2026, 11, 30, 9, 14), "2026-11-27", "full"),
        (datetime(2026, 11, 30, 16, 14), "2026-11-30", 422),  # today, before close + 15 min
        (datetime(2026, 11, 30, 16, 15), "2026-11-30", "offline"),  # today allowed; still the offline window
        (datetime(2026, 11, 30, 16, 30), "2026-11-30", "full"),
        (datetime(2026, 11, 27, 13, 20), "2026-11-27", "offline"),  # early close 13:00: allowed at 13:15
        (datetime(2026, 11, 28, 12, 0), "2026-11-28", 422),  # a Saturday alone: no session
    ],
)
def test_dates_and_the_offline_window(
    db_factory: sessionmaker[Session], now_et: datetime, date_to: str, expected: str | int
) -> None:
    now = now_et.replace(tzinfo=ET).astimezone(UTC)
    services, launcher = _real_services(db_factory, now)
    client = make_client(services, replays.router)
    date_from = "2026-11-23" if date_to != "2026-11-28" else "2026-11-28"
    r = client.post("/api/replays", json={**BODY, "date_from": date_from, "date_to": date_to})
    if expected == 422:
        assert r.status_code == 422, r.text
        assert [f["loc"] for f in r.json()["error"]["fields"]] == [["body", "date_to"]]
        assert launcher.launched == []
        return
    assert r.status_code == 202, r.text
    assert r.json()["data_mode"] == expected
    # the form's flag agrees with the mode the runner chose
    assert client.get("/api/replays/options").json()["offline_now"] is (expected == "offline")


def test_cancel_races_give_one_cancel_and_ids_that_are_not_replays_are_404(
    db_factory: sessionmaker[Session],
) -> None:
    services, launcher = _real_services(db_factory, MONDAY_10_ET)
    started = make_client(services, replays.router).post("/api/replays", json=BODY)
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    clients = [make_client(services, replays.router) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(lambda c: c.post(f"/api/replays/{run_id}/cancel").status_code, clients))
    assert codes == [200, 409, 409, 409]
    with db_factory() as s:
        cancels = s.scalars(select(m.AuditLog).where(m.AuditLog.action == "replay.cancel")).all()
        row = s.get(m.Run, run_id)
    assert len(cancels) == 1
    assert row is not None and row.status == "cancelled" and row.cancel_requested is True

    client = clients[0]
    live = get_live_run(db_factory, FixedClock(MONDAY_10_ET), RuntimeSettings()).id
    for bad in (live, 999_999):
        assert client.get(f"/api/replays/{bad}").status_code == 404
        assert client.post(f"/api/replays/{bad}/cancel").status_code == 404
    for bad_path in ("0", "-1", "1.5", "1e3", str(2**63)):
        assert client.get(f"/api/replays/{bad_path}").status_code == 422, bad_path
    # after the cancel a new replay may start
    again = client.post("/api/replays", json=BODY)
    assert again.status_code == 202, again.text
    assert launcher.launched == [run_id, again.json()["id"]]


async def test_the_launcher_never_spawns_for_a_run_that_is_not_queued(
    db_factory: sessionmaker[Session],
) -> None:
    spawned: list[tuple[str, ...]] = []

    async def spawn(*argv: str, **kw: Any) -> Any:
        spawned.append(argv)
        raise AssertionError("must not spawn")

    clock = FixedClock(MONDAY_10_ET)
    live = get_live_run(db_factory, clock, RuntimeSettings()).id
    with db_factory() as s:
        not_queued = [add_replay(s, status=st) for st in ("running", "completed", "failed", "cancelled")]
        s.commit()
    launcher = SubprocessReplayLauncher(db_factory, clock, executable="trader", spawn=spawn)
    for run_id in (*not_queued, live, 424_242):
        with pytest.raises(Exception) as info:
            await launcher.launch(run_id)
        assert not isinstance(info.value, AssertionError), run_id
        assert launcher.running() is False
    assert spawned == []
    with db_factory() as s:
        assert s.scalar(select(func.count()).select_from(m.EventLog)) == 0  # no exit event either


def _auth_app(factory: sessionmaker[Session]) -> tuple[TestClient, FakeReplayLauncher]:
    clock = FixedClock(MONDAY_10_ET)
    core = test_core(factory, clock, public_base_url="https://testserver", app_env="dev")
    launcher = FakeReplayLauncher()
    app = FastAPI()
    install_error_handlers(app)
    for router in (auth_router, replays.router, reports.router):
        app.include_router(router, prefix="/api")
    services = make_services(core, replays=launcher)
    services.registry.ensure_defaults("test")
    app.state.services = services
    assert auth.ensure_admin(factory, clock, "stephen", SecretStr(PASSWORD)) == "created"
    return TestClient(app, base_url="https://testserver"), launcher


def test_session_and_csrf_sweep_of_the_replay_and_report_routes(db_factory: sessionmaker[Session]) -> None:
    client, launcher = _auth_app(db_factory)
    with db_factory() as s:
        running = add_replay(s, status="running")
        s.commit()
    reads = [
        "/api/replays",
        "/api/replays/options",
        f"/api/replays/{running}",
        "/api/reports/weekly?week=2026-11-25",
    ]
    writes = ["/api/replays", f"/api/replays/{running}/cancel"]
    for path in reads:
        assert client.get(path).status_code == 401, path
    for path in writes:
        assert client.post(path, json=BODY).status_code == 401, path
    login = client.post("/api/auth/login", json={"username": "stephen", "password": PASSWORD})
    csrf = login.json()["csrf_token"]
    for path in reads:
        assert client.get(path).status_code in (200, 404), path
    for path in writes:
        for headers in ({}, {auth.CSRF_HEADER: "wrong"}, {auth.CSRF_HEADER: csrf + "x"}):
            r = client.post(path, json=BODY, headers=headers)
            assert r.status_code == 403, (path, headers, r.status_code)
    with db_factory() as s:
        row = s.get(m.Run, running)
        assert row is not None and row.cancel_requested is False
        replay_audits = select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action.like("replay.%"))
        assert s.scalar(replay_audits) == 0
    assert launcher.launched == []


# --- reports API -------------------------------------------------------------------------------------------


def _report(
    s: Session, week_start: date, week_ending: date, run_id: int, commentary: str = "ok text"
) -> None:
    s.add(
        m.WeeklyReport(
            week_ending=week_ending,
            week_start=week_start,
            run_id=run_id,
            facts={"week": {"start": week_start.isoformat(), "end": week_ending.isoformat()}},
            commentary=commentary,
            commentary_status="ok",
            commentary_error=None,
            model="claude-sonnet-5",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("0.010000"),
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _reports_client(factory: sessionmaker[Session]) -> tuple[TestClient, int]:
    clock = FixedClock(NOW)
    live = get_live_run(factory, clock, RuntimeSettings()).id
    services = make_services(test_core(factory, clock))
    return make_client(services, reports.router, raise_server_exceptions=False), live


@pytest.mark.parametrize(
    ("week", "ending"),
    [
        ("2026-10-26", "2026-10-30"),
        ("2026-11-01", "2026-10-30"),  # the Sunday DST ends: the week just ended
        ("2026-11-02", "2026-11-06"),  # the first Monday on standard time
        ("2026-11-08", "2026-11-06"),
        ("2026-09-07", "2026-09-11"),  # Labor Day Monday: the holiday week keeps its Monday
        ("2026-09-06", "2026-09-04"),  # the Sunday before it
        ("2026-04-03", "2026-04-02"),  # Good Friday: the week ends on Thursday
        ("2026-04-05", "2026-04-02"),
        ("2026-03-08", "2026-03-06"),  # the Sunday DST starts
        ("2026-03-09", None),  # a week with no stored report
        ("0001-01-01", None),  # the first date there is: no overflow, just none
        ("9999-12-31", None),
    ],
)
def test_weekly_report_week_edge_cases(
    db_factory: sessionmaker[Session], week: str, ending: str | None
) -> None:
    client, live = _reports_client(db_factory)
    with db_factory() as s:
        for start, end in (
            (date(2026, 10, 26), date(2026, 10, 30)),
            (date(2026, 11, 2), date(2026, 11, 6)),
            (date(2026, 8, 31), date(2026, 9, 4)),
            (date(2026, 9, 7), date(2026, 9, 11)),
            (date(2026, 3, 30), date(2026, 4, 2)),
            (date(2026, 3, 2), date(2026, 3, 6)),
        ):
            _report(s, start, end, live)
        s.commit()
    r = client.get("/api/reports/weekly", params={"week": week})
    if ending is None:
        assert r.status_code == 404, (week, r.status_code, r.text)
    else:
        assert r.status_code == 200, (week, r.text)
        assert r.json()["week_ending"] == ending
    for bad in ("2026-02-30", "", "2026-W48", "yesterday"):
        assert client.get("/api/reports/weekly", params={"week": bad}).status_code == 422, bad
    assert client.get("/api/reports/weekly").status_code == 422


def test_a_weekly_report_of_a_replay_run_is_never_served_as_live(db_factory: sessionmaker[Session]) -> None:
    """Weekly reports describe the live run; a row written for a replay run (a defect elsewhere, or a
    future replay report) must not be shown on the live Reports page."""
    client, live = _reports_client(db_factory)
    with db_factory() as s:
        replay = add_replay(s, status="completed")
        _report(s, date(2026, 11, 23), date(2026, 11, 27), replay, commentary=f"{MARK} commentary")
        s.commit()
    r = client.get("/api/reports/weekly", params={"week": "2026-11-25"})
    assert MARK not in r.text
    assert r.status_code == 404 or r.json()["run_id"] == live


def test_telegram_status_is_the_weeks_own_notification(db_factory: sessionmaker[Session]) -> None:
    client, live = _reports_client(db_factory)
    with db_factory() as s:
        _report(s, date(2026, 11, 23), date(2026, 11, 27), live)
        _report(s, date(2026, 11, 30), date(2026, 12, 4), live)
        for key, status in (
            ("weekly:2026-11-20", "sent"),  # the week before
            ("weekly:2026-11-27x", "sent"),  # a lookalike key
            ("event:2026-11-27", "sent"),
            ("weekly:2026-12-04", "failed"),
        ):
            s.add(
                m.Notification(kind="weekly_report", dedupe_key=key, text="t", created_at=NOW, status=status)
            )
        s.commit()
    first = client.get("/api/reports/weekly", params={"week": "2026-11-28"}).json()
    assert first["week_ending"] == "2026-11-27" and first["telegram_status"] is None
    second = client.get("/api/reports/weekly", params={"week": "2026-12-01"}).json()
    assert second["telegram_status"] == "failed"
    with db_factory() as s:
        s.add(
            m.Notification(
                kind="weekly_report",
                dedupe_key="weekly:2026-11-27",
                text="t",
                created_at=NOW,
                status="sending",
            )
        )
        s.commit()
    assert (
        client.get("/api/reports/weekly", params={"week": "2026-11-23"}).json()["telegram_status"]
        == "sending"
    )


# --- CSV ---------------------------------------------------------------------------------------------------


def _csv(client: TestClient, run: str) -> list[dict[str, str]]:
    r = client.get("/api/export/trades.csv", params={"run": run})
    assert r.status_code == 200, r.text
    return list(csv.DictReader(io.StringIO(r.text)))


def test_csv_guards_every_new_text_cell_and_keeps_replay_trades_to_their_run(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(NOW)
    services = make_services(test_core(db_factory, clock))
    services.registry.ensure_defaults("test")
    live = get_live_run(db_factory, clock, RuntimeSettings()).id
    base = services.registry.current("orb_sip")
    with db_factory() as s:
        s.execute(update(m.SimAccount).where(m.SimAccount.run_id == live).values(currency="@SU"))
        evil_cfg = m.StrategyConfig(
            strategy_key="+evil",
            version="=cmd|'/C calc'!A0",
            revision=1,
            params={},
            enabled=True,
            created_at=NOW,
            created_by="test",
        )
        s.add(evil_cfg)
        s.flush()
        chain = seed_closed_trade(s, live, ticker="-2+3", strategy="+evil")
        s.execute(update(m.Trade).where(m.Trade.id == chain.trade_id).values(exit_reason="@SUM(A1)"))
        s.execute(
            update(m.Position).where(m.Position.id == chain.position_id).values(stop_loss=Decimal("-1.5"))
        )
        odd = m.Run(mode="=HACK", started_at=NOW, params={}, status="done", label="odd")
        s.add(odd)
        s.flush()
        s.add(
            m.SimAccount(
                run_id=odd.id,
                currency="-1",
                starting_cash=Decimal("1"),
                source_amount=Decimal("1"),
                source_currency="USD",
                created_at=NOW,
            )
        )
        seed_closed_trade(s, odd.id, ticker="ODD")
        replay = add_replay(s, status="completed")
        s.commit()
    services.registry.create_replay_config(
        "orb_sip", base=base, params={"top_n": 9}, enabled=True, created_by="r"
    )
    with db_factory() as s:
        replay_cfg = s.scalar(select(m.StrategyConfig.id).where(m.StrategyConfig.scope == "replay"))
        chain_r = seed_closed_trade(s, replay, ticker=f"{MARK}Z")
        s.execute(
            update(m.Position)
            .where(m.Position.id == chain_r.position_id)
            .values(strategy_config_id=replay_cfg)
        )
        s.commit()
    client = make_client(services, performance.router)

    [row] = _csv(client, "live")
    assert row["ticker"] == "'-2+3" and row["strategy"] == "'+evil"
    assert row["exit_reason"] == "'@SUM(A1)"
    assert row["currency"] == "'@SU"
    assert row["strategy_version"] == "'=cmd|'/C calc'!A0"
    assert row["config_scope"] == "live" and row["run_mode"] == "live"
    assert row["stop_loss"] == "-1.5000"  # numbers are never changed
    for key, value in row.items():
        if value[:1] in ("=", "+", "@", "\t", "\r") or (value.startswith("-") and not _is_number(value)):
            raise AssertionError(f"unguarded cell {key}={value!r}")

    [odd_row] = _csv(client, str(odd.id))
    assert odd_row["run_mode"] == "'=HACK" and odd_row["currency"] == "'-1"

    [replay_row] = _csv(client, str(replay))
    assert replay_row["ticker"] == f"{MARK}Z"
    assert replay_row["config_scope"] == "replay" and replay_row["run_mode"] == "replay"
    assert replay_row["currency"] == ""  # a replay run without a sim account: empty, not the live one's


def _is_number(value: str) -> bool:
    try:
        Decimal(value)
    except ArithmeticError:
        return False
    return True
