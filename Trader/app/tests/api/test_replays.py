"""P5-T7 acceptance tests 1-6: the replay routes (`trader.api.routers.replays`) on the test database.

The runner functions (`create_replay`, `request_cancel`, `reconcile_abandoned`, P5-T6) and `compute_metrics`
(P5-T2) are monkeypatched in the router's namespace with small fakes that write what the real ones would, so
these tests pin the routes' own behaviour. The launcher is `FakeReplayLauncher`.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.factories import add_strategy_config, add_symbol
from tests.fakes_api import FakeReplayLauncher, make_services, test_core
from tests.reports import add_trade
from trader.api import auth
from trader.api.deps import ApiServices
from trader.api.errors import install_error_handlers
from trader.api.routers import replays
from trader.api.routers.auth import router as auth_router
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import ET, FixedClock
from trader.replay.types import (
    REPLAY_OVERRIDE_KEYS,
    PinnedStrategy,
    ReplayBusy,
    ReplayInvalid,
    ReplayProgress,
    ReplayRequest,
    StrategyOverride,
)
from trader.reports.metrics import OPEN_HIGH, OPEN_LOW, HistogramBin, Metrics
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 30, 10, 0, tzinfo=ET).astimezone(UTC)  # Monday 10:00 ET, a session
T0 = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
D_FROM, D_TO = date(2026, 11, 23), date(2026, 11, 27)
PASSWORD = "Right-Password-5531"  # noqa: S105 (a throwaway test password)
PINNED = PinnedStrategy("orb_sip", 5, 2, "1.0.0", "replay", True, {"top_n": 5})


# --- seed helpers -------------------------------------------------------------------------------------------


def replay_params(
    date_from: date = D_FROM, date_to: date = D_TO, *, data_mode: str = "offline", label: str | None = None
) -> dict[str, Any]:
    settings = RuntimeSettings.model_validate({"approval_mode": "auto"})
    return {
        "kind": "replay",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "label": label,
        "data_mode": data_mode,
        "catalyst_mode": "stored",
        "half_spread_bps": "5",
        "settings": settings.model_dump(mode="json", by_alias=True),
        "overrides": {"risk_pct": "0.01"},
        "strategies": [PINNED.to_json()],
        "code_version": "abc1234",
    }


def add_replay(
    s: Session,
    *,
    status: str = "completed",
    started_at: datetime = T0,
    label: str | None = "what if",
    progress: ReplayProgress | None = None,
    finished_at: datetime | None = None,
    error: str | None = None,
    cancel_requested: bool = False,
    data_mode: str = "offline",
    run_id: int | None = None,
) -> int:
    row = m.Run(
        mode="replay",
        started_at=started_at,
        params=replay_params(data_mode=data_mode, label=label),
        status=status,
        label=label,
        updated_at=started_at,
        progress=(progress or ReplayProgress(sessions_total=5)).to_json(),
        finished_at=finished_at,
        error=error,
        cancel_requested=cancel_requested,
    )
    if run_id is not None:
        row.id = run_id
    s.add(row)
    s.flush()
    return row.id


def add_event(s: Session, run_id: int | None, message: str, *, level: str = "info", ts: datetime = T0) -> int:
    ev = m.EventLog(ts=ts, level=level, source="replay", run_id=run_id, message=message, data=None)
    s.add(ev)
    s.flush()
    return ev.id


def live_run(factory: sessionmaker[Session]) -> int:
    return get_live_run(factory, FixedClock(NOW), RuntimeSettings()).id


def metrics_of(run_id: int, date_from: date | None, date_to: date | None) -> Metrics:
    bins = (HistogramBin(OPEN_LOW, Decimal("-3.0"), 0), HistogramBin(Decimal("5.0"), OPEN_HIGH, 1))
    return Metrics(
        run_id=run_id,
        date_from=date_from,
        date_to=date_to,
        trades=2,
        wins=1,
        losses=1,
        win_rate=Decimal("0.5"),
        avg_win_r=Decimal("2"),
        avg_loss_r=Decimal("-1"),
        expectancy_r=Decimal("0.5"),
        profit_factor=Decimal("2"),
        avg_slippage=Decimal("0.02"),
        avg_slippage_per_share=Decimal("0.002"),
        max_drawdown_pct=Decimal("0.01"),
        adherence_pct=None,
        total_pnl=Decimal("10"),
        total_fees=Decimal("0"),
        trades_without_r=0,
        r_histogram=bins,
    )


# --- fakes of the P5-T6 runner and the P5-T2 metrics --------------------------------------------------------


@dataclass
class FakeRunner:
    """Stands in for `trader.replay.runner`: `create` stores a queued replay and its `replay.start` audit
    row (as the real one does) unless `create_error` is set; `cancel` flags a queued/running replay and
    writes `replay.cancel`; `reconcile` settles every running replay in `abandoned` as failed."""

    factory: sessionmaker[Session]
    create_error: Exception | None = None
    created: list[tuple[ReplayRequest, str, str]] = field(default_factory=list)
    cancels: list[tuple[int, str]] = field(default_factory=list)
    reconciles: int = 0
    abandoned: set[int] = field(default_factory=set)
    metrics_calls: list[tuple[int, date | None, date | None]] = field(default_factory=list)

    def create(
        self,
        factory: sessionmaker[Session],
        wall: Any,
        calendar: Any,
        settings: Any,
        registry: Any,
        request: ReplayRequest,
        actor: str,
        *,
        config_writer: Any = None,
        app_version: str = "dev",
    ) -> int:
        self.created.append((request, actor, app_version))
        if self.create_error is not None:
            raise self.create_error
        with factory() as s:
            run_id = add_replay(s, status="queued", started_at=wall.now(), label=request.label)
            s.add(
                m.AuditLog(
                    ts=wall.now(), actor=actor, action="replay.start", before=None, after={"id": run_id}
                )
            )
            s.commit()
        return run_id

    def cancel(self, factory: sessionmaker[Session], wall: Any, run_id: int, actor: str) -> bool:
        self.cancels.append((run_id, actor))
        with factory() as s:
            row = s.get(m.Run, run_id)
            if row is None or row.mode != "replay" or row.status not in ("queued", "running"):
                return False
            row.cancel_requested = True
            s.add(m.AuditLog(ts=wall.now(), actor=actor, action="replay.cancel", before=None, after=None))
            s.commit()
        return True

    def reconcile(self, factory: sessionmaker[Session], wall: Any) -> list[int]:
        self.reconciles += 1
        ids = sorted(self.abandoned)
        with factory() as s:
            for run_id in ids:
                s.execute(update(m.Run).where(m.Run.id == run_id).values(status="failed", error="abandoned"))
            s.commit()
        self.abandoned.clear()
        return ids

    def metrics(
        self,
        factory: sessionmaker[Session],
        run_id: int,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> Metrics:
        self.metrics_calls.append((run_id, date_from, date_to))
        return metrics_of(run_id, date_from, date_to)


@pytest.fixture
def runner(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake = FakeRunner(db_factory)
    monkeypatch.setattr(replays, "create_replay", fake.create)
    monkeypatch.setattr(replays, "request_cancel", fake.cancel)
    monkeypatch.setattr(replays, "reconcile_abandoned", fake.reconcile)
    monkeypatch.setattr(replays, "compute_metrics", fake.metrics)
    return fake


def services_for(factory: sessionmaker[Session], now: datetime = NOW, **overrides: Any) -> ApiServices:
    return make_services(test_core(factory, FixedClock(now)), **overrides)


def client_for(services: ApiServices, **kw: Any) -> TestClient:
    return make_client(services, replays.router, **kw)


BODY: dict[str, Any] = {
    "date_from": "2026-11-23",
    "date_to": "2026-11-27",
    "label": "what if",
    "overrides": {"risk_pct": "0.01"},
    "strategies": {"orb_sip": {"params": {"top_n": 5}}},
    "offline": True,
}


# --- 1. start -----------------------------------------------------------------------------------------------


def test_1_start_queues_a_replay_launches_it_and_answers_202(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    launcher = FakeReplayLauncher()
    services = services_for(db_factory, replays=launcher)
    r = client_for(services).post("/api/replays", json=BODY)
    assert r.status_code == 202, r.text
    body = r.json()
    run_id = body["id"]
    assert body["status"] == "queued" and body["label"] == "what if"
    assert (body["date_from"], body["date_to"]) == ("2026-11-23", "2026-11-27")
    assert body["metrics"] is None and body["live_metrics"] is None
    assert launcher.launched == [run_id]
    [(request, who, version)] = runner.created
    assert request == ReplayRequest(
        date_from=D_FROM,
        date_to=D_TO,
        label="what if",
        overrides={"risk_pct": "0.01"},
        strategies={"orb_sip": StrategyOverride(enabled=None, params={"top_n": 5})},
        offline=True,
    )
    assert who == "web:stephen" and version == services.core.env.app_version
    with db_factory() as s:
        audits = s.execute(select(m.AuditLog.actor, m.AuditLog.action)).all()
        row = s.get(m.Run, run_id)
        assert row is not None and row.status == "queued"
    assert [tuple(a) for a in audits] == [("web:stephen", "replay.start")]


def test_1_start_needs_a_session(db_factory: sessionmaker[Session], runner: FakeRunner) -> None:
    launcher = FakeReplayLauncher()
    anon = client_for(services_for(db_factory, replays=launcher), user=None)
    assert anon.post("/api/replays", json=BODY).status_code == 401
    assert anon.get("/api/replays").status_code == 401
    assert anon.get("/api/replays/options").status_code == 401
    assert anon.get("/api/replays/1").status_code == 401
    assert anon.post("/api/replays/1/cancel").status_code == 401
    assert launcher.launched == [] and runner.created == []


def test_1_start_and_cancel_need_the_csrf_header(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    """The real session and CSRF dependencies (no overrides): a signed-in POST without the header is 403."""
    clock = FixedClock(NOW)
    core = test_core(db_factory, clock, public_base_url="https://testserver", app_env="dev")
    launcher = FakeReplayLauncher()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(auth_router, prefix="/api")
    app.include_router(replays.router, prefix="/api")
    app.state.services = make_services(core, replays=launcher)
    assert auth.ensure_admin(db_factory, clock, "stephen", SecretStr(PASSWORD)) == "created"
    c = TestClient(app, base_url="https://testserver")
    csrf = c.post("/api/auth/login", json={"username": "stephen", "password": PASSWORD}).json()["csrf_token"]

    missing = c.post("/api/replays", json=BODY)
    assert missing.status_code == 403 and missing.json()["error"]["code"] == "csrf"
    wrong = c.post("/api/replays", json=BODY, headers={auth.CSRF_HEADER: "wrong"})
    assert wrong.status_code == 403
    assert runner.created == [] and launcher.launched == []

    ok = c.post("/api/replays", json=BODY, headers={auth.CSRF_HEADER: csrf})
    assert ok.status_code == 202, ok.text
    run_id = ok.json()["id"]
    assert c.post(f"/api/replays/{run_id}/cancel").status_code == 403
    assert runner.cancels == []
    done = c.post(f"/api/replays/{run_id}/cancel", headers={auth.CSRF_HEADER: csrf})
    assert done.status_code == 200 and done.json()["cancel_requested"] is True
    assert runner.cancels == [(run_id, "web:stephen")]


def test_1_without_a_launcher_the_start_is_503_and_nothing_is_created(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    r = client_for(services_for(db_factory, replays=None)).post("/api/replays", json=BODY)
    assert r.status_code == 503 and r.json()["error"] == {
        "code": "unavailable",
        "message": "Replays are not available",
        "fields": None,
        "request_id": None,
    }
    assert runner.created == []


def test_1_a_spawn_failure_fails_the_run_and_is_500(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    launcher = FakeReplayLauncher()
    launcher.fail_with = OSError("[Errno 2] No such file or directory: 'trader' secret-ish detail")
    client = client_for(services_for(db_factory, replays=launcher), raise_server_exceptions=False)
    r = client.post("/api/replays", json=BODY)
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal"
    assert "No such file" not in r.text
    with db_factory() as s:
        [row] = s.execute(select(m.Run).where(m.Run.mode == "replay")).scalars().all()
    assert row.status == "failed" and row.error == "could not start: OSError"
    assert row.finished_at == NOW and row.updated_at == NOW


# --- 2. invalid and busy ------------------------------------------------------------------------------------


def test_2_invalid_requests_are_422_naming_the_fields_without_the_values(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    runner.create_error = ReplayInvalid(
        [
            ("date_to", "must be on or after date_from"),
            ("overrides.bogus_key", "is not a setting a replay may override"),
            ("strategies.orb_sip.params.top_n", "must be at least 1"),
        ]
    )
    launcher = FakeReplayLauncher()
    body = {
        **BODY,
        "date_from": "2026-11-27",
        "date_to": "2026-11-23",
        "overrides": {"bogus_key": "SENTINEL-9876"},
        "strategies": {"orb_sip": {"params": {"top_n": -424242}}},
    }
    r = client_for(services_for(db_factory, replays=launcher)).post("/api/replays", json=body)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation"
    assert [f["loc"] for f in err["fields"]] == [
        ["body", "date_to"],
        ["body", "overrides", "bogus_key"],
        ["body", "strategies", "orb_sip", "params", "top_n"],
    ]
    assert "SENTINEL-9876" not in r.text and "424242" not in r.text
    assert launcher.launched == []


def test_2_schema_errors_are_422_before_the_runner(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    client = client_for(services_for(db_factory))
    too_long = client.post("/api/replays", json={**BODY, "label": "x" * 201})
    assert too_long.status_code == 422
    bad_date = client.post("/api/replays", json={**BODY, "date_from": "not-a-date"})
    assert bad_date.status_code == 422 and "not-a-date" not in bad_date.text
    assert runner.created == []


def test_2_a_busy_system_is_409(db_factory: sessionmaker[Session], runner: FakeRunner) -> None:
    runner.create_error = ReplayBusy("replay 3 is running")
    launcher = FakeReplayLauncher()
    r = client_for(services_for(db_factory, replays=launcher)).post("/api/replays", json=BODY)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert r.json()["error"]["message"] == "A replay is already running."
    assert launcher.launched == []


# --- 3. list ------------------------------------------------------------------------------------------------


def test_3_list_shows_replays_only_newest_first_with_trade_totals(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    live = live_run(db_factory)
    with db_factory() as s:
        sym = add_symbol(s, "AAA")
        cfg = add_strategy_config(s)
        older = add_replay(s, started_at=T0, finished_at=T0 + timedelta(minutes=5))
        newer = add_replay(
            s,
            status="completed",
            started_at=T0 + timedelta(hours=1),
            label="biased one",
            progress=ReplayProgress(sessions_total=5, sessions_done=5, biased_days=(D_FROM,)),
            data_mode="full",
        )
        queued = add_replay(s, status="queued", started_at=T0 + timedelta(hours=2), label=None)
        add_trade(s, older, sym, D_FROM, "20.0000", "2.0000", config_id=cfg)
        add_trade(s, older, sym, D_TO, "-10.0000", "-1.0000", config_id=cfg)
        add_trade(s, older, sym, D_TO, "3.0000", None, config_id=cfg)  # no R: not in the expectancy
        add_trade(s, newer, sym, D_FROM, "1.0000", "0.3333", config_id=cfg)
        add_trade(s, newer, sym, D_TO, "1.0000", "0.3334", config_id=cfg)
        add_trade(s, newer, sym, D_TO, "1.0000", "0.3334", config_id=cfg)
        add_trade(s, live, sym, D_FROM, "99.0000", "9.0000", config_id=cfg)
        s.commit()
    client = client_for(services_for(db_factory))
    r = client.get("/api/replays")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [queued, newer, older]
    assert live not in [i["id"] for i in items]
    q, n, o = items
    assert (q["status"], q["trades"], q["expectancy_r"], q["total_pnl"], q["label"]) == (
        "queued",
        0,
        None,
        None,
        None,
    )
    assert (n["trades"], n["expectancy_r"], n["total_pnl"]) == (3, "0.3334", "3.0000")
    assert n["biased"] is True and n["data_mode"] == "full"
    assert (o["trades"], o["expectancy_r"], o["total_pnl"]) == (3, "0.5000", "13.0000")
    assert o["biased"] is False and o["data_mode"] == "offline"
    assert (o["date_from"], o["date_to"]) == ("2026-11-23", "2026-11-27")
    assert o["created_at"] == "2026-11-28T12:00:00Z" and o["finished_at"] == "2026-11-28T12:05:00Z"
    assert runner.reconciles == 1

    assert [i["id"] for i in client.get("/api/replays", params={"limit": 1}).json()["items"]] == [queued]
    assert client.get("/api/replays", params={"limit": 201}).status_code == 422
    assert client.get("/api/replays", params={"limit": 0}).status_code == 422


def test_3_list_settles_abandoned_runs_first(db_factory: sessionmaker[Session], runner: FakeRunner) -> None:
    with db_factory() as s:
        stuck = add_replay(s, status="running")
        s.commit()
    runner.abandoned.add(stuck)
    [item] = client_for(services_for(db_factory)).get("/api/replays").json()["items"]
    assert item["status"] == "failed"


# --- 4. get -------------------------------------------------------------------------------------------------


def test_4_get_a_completed_replay_with_metrics_live_metrics_and_events(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    live = live_run(db_factory)
    progress = ReplayProgress(
        sessions_total=5,
        sessions_done=5,
        current_date=D_TO,
        trades=2,
        forced_closes=1,
        biased_days=(D_FROM, date(2026, 11, 24)),
        missing_opening_bars=3,
        missing_minute_bars=4,
        questrade_requests=0,
    )
    with db_factory() as s:
        run_id = add_replay(s, progress=progress, finished_at=T0 + timedelta(minutes=9))
        for i in range(25):
            add_event(s, run_id, f"replay event {i}", ts=T0 + timedelta(seconds=i))
        add_event(s, live, "live event")
        add_event(s, None, "no run")
        s.commit()
    r = client_for(services_for(db_factory)).get(f"/api/replays/{run_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == run_id and body["status"] == "completed" and body["label"] == "what if"
    assert body["created_at"] == "2026-11-28T12:00:00Z" and body["finished_at"] == "2026-11-28T12:09:00Z"
    assert (body["data_mode"], body["catalyst_mode"], body["half_spread_bps"]) == ("offline", "stored", "5")
    assert body["overrides"] == {"risk_pct": "0.01"}
    assert body["strategies"] == [
        {
            "key": "orb_sip",
            "config_id": 5,
            "revision": 2,
            "version": "1.0.0",
            "scope": "replay",
            "enabled": True,
            "params": {"top_n": 5},
        }
    ]
    assert body["progress"] == {
        "sessions_total": 5,
        "sessions_done": 5,
        "current_date": "2026-11-27",
        "trades": 2,
        "forced_closes": 1,
        "biased_days": ["2026-11-23", "2026-11-24"],
        "missing_opening_bars": 3,
        "missing_minute_bars": 4,
        "questrade_requests": 0,
    }
    assert body["biased"] is True and body["cancel_requested"] is False and body["error"] is None
    assert body["metrics"]["run_id"] == run_id and body["metrics"]["expectancy_r"] == "0.5"
    assert body["metrics"]["from_date"] is None
    assert body["live_metrics"]["run_id"] == live
    assert (body["live_metrics"]["from_date"], body["live_metrics"]["to_date"]) == (
        "2026-11-23",
        "2026-11-27",
    )
    assert runner.metrics_calls == [(run_id, None, None), (live, D_FROM, D_TO)]
    events = body["events"]
    assert len(events) == 20
    assert [e["message"] for e in events] == [f"replay event {i}" for i in range(24, 4, -1)]


def test_4_a_running_replay_has_no_metrics_and_live_or_unknown_ids_are_404(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    live = live_run(db_factory)
    with db_factory() as s:
        running = add_replay(s, status="running", error=None)
        s.commit()
    client = client_for(services_for(db_factory))
    body = client.get(f"/api/replays/{running}").json()
    assert body["status"] == "running" and body["metrics"] is None and body["live_metrics"] is None
    assert body["events"] == []
    assert runner.metrics_calls == []
    for bad in (live, 987654):
        r = client.get(f"/api/replays/{bad}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert client.get("/api/replays/abc").status_code == 422
    assert client.get(f"/api/replays/{2**63}").status_code == 422


def test_4_a_failed_replay_shows_its_masked_error(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    with db_factory() as s:
        run_id = add_replay(s, status="failed", error="boom password=hunter2")
        s.commit()
    body = client_for(services_for(db_factory)).get(f"/api/replays/{run_id}").json()
    assert body["status"] == "failed" and body["metrics"] is None
    assert "hunter2" not in (body["error"] or "") and body["error"].startswith("boom")


# --- 5. cancel ----------------------------------------------------------------------------------------------


def test_5_cancel_a_running_replay_and_refuse_a_completed_one(
    db_factory: sessionmaker[Session], runner: FakeRunner
) -> None:
    live = live_run(db_factory)
    with db_factory() as s:
        running = add_replay(s, status="running")
        done = add_replay(s, status="completed")
        s.commit()
    client = client_for(services_for(db_factory))
    r = client.post(f"/api/replays/{running}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["cancel_requested"] is True and r.json()["status"] == "running"
    assert runner.cancels == [(running, "web:stephen")]

    refused = client.post(f"/api/replays/{done}/cancel")
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == "The replay is not running."
    assert client.post(f"/api/replays/{live}/cancel").status_code == 404
    assert client.post("/api/replays/987654/cancel").status_code == 404
    assert [c[0] for c in runner.cancels] == [running, done]


# --- 6. options ---------------------------------------------------------------------------------------------


def _seed_data(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        sym = add_symbol(s, "AAA")
        bar = {"open": Decimal(1), "high": Decimal(1), "low": Decimal(1), "close": Decimal(1), "volume": 1}
        # 1m bars from Tuesday 2026-09-01 09:30 ET; an earlier 5m bar does not count
        s.add(
            m.CandleArchive(
                symbol_id=sym, interval="1m", start_ts=datetime(2026, 9, 1, 13, 30, tzinfo=UTC), **bar
            )
        )
        s.add(
            m.CandleArchive(
                symbol_id=sym, interval="1m", start_ts=datetime(2026, 9, 2, 13, 30, tzinfo=UTC), **bar
            )
        )
        s.add(
            m.CandleArchive(
                symbol_id=sym, interval="5m", start_ts=datetime(2026, 8, 3, 13, 30, tzinfo=UTC), **bar
            )
        )
        for d in (date(2026, 10, 1), date(2026, 8, 20)):
            s.add(m.UniverseSnapshot(session_date=d, symbol_id=sym, source="finviz"))
        s.commit()


def test_6_options_during_market_hours(db_factory: sessionmaker[Session], runner: FakeRunner) -> None:
    _seed_data(db_factory)
    services = services_for(db_factory)
    services.core.settings.set("replay.max_sessions", 60, "test")
    r = client_for(services).get("/api/replays/options")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "override_keys": sorted(REPLAY_OVERRIDE_KEYS),
        "max_sessions": 60,
        "latest_allowed": "2026-11-27",  # today's session is not over
        "questrade_from": "2026-09-06",  # 85 days before 2026-11-30
        "archive_from": "2026-09-01",
        "snapshots_from": "2026-08-20",
        "busy": False,
        "offline_now": True,
    }
    assert runner.reconciles == 1


@pytest.mark.parametrize(
    ("now_et", "latest", "offline"),
    [
        (datetime(2026, 11, 30, 9, 14), "2026-11-27", False),
        (datetime(2026, 11, 30, 9, 15), "2026-11-27", True),
        (datetime(2026, 11, 30, 16, 14), "2026-11-27", True),
        (datetime(2026, 11, 30, 16, 15), "2026-11-30", True),
        (datetime(2026, 11, 30, 16, 30), "2026-11-30", False),
        (datetime(2026, 11, 30, 17, 0), "2026-11-30", False),
        (datetime(2026, 11, 27, 13, 20), "2026-11-27", True),  # early close 13:00: over at 13:15
        (datetime(2026, 12, 5, 10, 0), "2026-12-04", False),  # Saturday
    ],
)
def test_6_latest_allowed_and_offline_follow_the_clock(
    db_factory: sessionmaker[Session], runner: FakeRunner, now_et: datetime, latest: str, offline: bool
) -> None:
    now = now_et.replace(tzinfo=ET).astimezone(UTC)
    body = client_for(services_for(db_factory, now)).get("/api/replays/options").json()
    assert (body["latest_allowed"], body["offline_now"]) == (latest, offline)
    assert body["archive_from"] is None and body["snapshots_from"] is None


def test_6_busy_after_settling_abandoned_runs(db_factory: sessionmaker[Session], runner: FakeRunner) -> None:
    with db_factory() as s:
        stuck = add_replay(s, status="running")
        s.commit()
    client = client_for(services_for(db_factory))
    assert client.get("/api/replays/options").json()["busy"] is True
    runner.abandoned.add(stuck)
    assert client.get("/api/replays/options").json()["busy"] is False
    with db_factory() as s:
        add_replay(s, status="queued")
        s.commit()
    assert client.get("/api/replays/options").json()["busy"] is True
