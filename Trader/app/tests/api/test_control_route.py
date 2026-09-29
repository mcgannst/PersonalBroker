"""DB-T6 acceptance test 7: `GET /api/control`: part isolation, no Questrade, auth and read-only."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from trader.api.deps import ApiServices
from trader.api.livedata import control, health, positions, risk
from trader.api.livedata.types import LivePositions
from trader.api.routers import control as control_router
from trader.api.schemas import KillSwitchLightOut
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.runs import get_live_run
from trader.jobs import soak
from trader.market.clock import ET, FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

TUE = date(2026, 10, 6)
NOW = datetime.combine(TUE, time(10, 15), tzinfo=ET).astimezone(UTC)
SECRET = "boom password=hunter2"
PARTS = ("engine", "killswitches", "strategies", "schedule", "health", "soak", "errors")

LIGHT = KillSwitchLightOut(
    switch="daily_loss_pct",
    label="Daily loss",
    tripped=False,
    unit="pct",
    automatic=True,
    needs_web_reset=False,
    clears="at the next session",
)


def _no_questrade(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("GET /api/control called Questrade")


@pytest.fixture(autouse=True)
def _fresh_soak_cache() -> Iterator[None]:
    health.clear_soak_cache()
    yield
    health.clear_soak_cache()


@pytest.fixture(autouse=True)
def _db_t4_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB-T4's functions (stubs until it lands) replaced by fixtures."""
    monkeypatch.setattr(
        positions,
        "live_positions",
        lambda f, rid, now, expand: LivePositions([], [], Decimal("100000"), True),
    )
    monkeypatch.setattr(risk, "killswitch_lights", lambda *a: [LIGHT])
    monkeypatch.setattr(risk, "trading_state", lambda ks, rid, day: "running")


def _services(factory: sessionmaker[Session]) -> ApiServices:
    return make_services(test_core(factory, FixedClock(NOW)), quotes=_no_questrade, candles=_no_questrade)


def _seed(factory: sessionmaker[Session]) -> int:
    run_id = get_live_run(factory, FixedClock(NOW - timedelta(days=3)), RuntimeSettings()).id
    with session_scope(factory) as s:
        s.add(m.EventLog(ts=NOW, level="error", source="worker", message="e1", run_id=run_id))
        s.add(
            m.KillSwitchEvent(
                run_id=run_id,
                switch="daily_loss_pct",
                session_date=TUE,
                tripped_at=NOW - timedelta(hours=1),
                value=Decimal("0.06"),
                threshold=Decimal("0.05"),
                reset_at=NOW - timedelta(minutes=30),
                reset_reason="checked",
                reset_by="web:stephen",
            )
        )
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=7,
                host="trader-dev",
                started_at=NOW - timedelta(hours=3),
                beat_at=NOW - timedelta(seconds=5),
                session_date=TUE,
                phase="idle",
                detail={"rate_limit": {"market_remaining": 17}},
            )
        )
    return run_id


def test_the_whole_page(db_factory: sessionmaker[Session]) -> None:
    run_id = _seed(db_factory)
    resp = make_client(_services(db_factory), control_router.router).get("/api/control")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["part_errors"] == []
    assert body["server_time"] == "2026-10-06T14:15:00Z"
    assert body["session"]["date"] == "2026-10-06" and body["session"]["phase"] == "open"
    assert body["manual_jobs"] == ["nightly", "premarket", "preopen", "postclose", "token-refresh", "weekly"]
    assert body["engine"]["run_id"] == run_id and body["engine"]["trading"] == "running"
    assert body["killswitches"][0]["switch"] == "daily_loss_pct"
    history = body["killswitch_history"]
    assert len(history) == 1 and history[0]["reset_by"] == "web:stephen" and history[0]["value"] == "0.060000"
    assert isinstance(body["strategies"], list)
    assert [i["key"] for i in body["schedule"]][:2] == ["nightly", "premarket"]
    assert body["health"]["worker"]["phase"] == "idle" and body["health"]["worker_stale"] is False
    assert body["soak"]["target"] == 10
    assert [e["message"] for e in body["errors"]] == ["e1"]


def test_the_killswitch_equity_is_the_marked_equity(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(db_factory)
    seen: list[tuple[Any, ...]] = []

    def lights(*args: Any) -> list[KillSwitchLightOut]:
        seen.append(args)
        return [LIGHT]

    def live(f: Any, rid: int, now: datetime, expand: Any) -> LivePositions:
        seen.append(("positions", rid, now, tuple(expand)))
        return LivePositions([], [], Decimal("98765.4321"), True)

    monkeypatch.setattr(risk, "killswitch_lights", lights)
    monkeypatch.setattr(positions, "live_positions", live)
    resp = make_client(_services(db_factory), control_router.router).get("/api/control")
    assert resp.status_code == 200, resp.text
    assert seen[0] == ("positions", run_id, NOW, ())
    assert seen[1][5] == NOW and seen[1][6] == Decimal("98765.4321") and seen[1][4] == run_id


@pytest.mark.parametrize("part", PARTS)
def test_part_isolation(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, part: str
) -> None:
    _seed(db_factory)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    target = {
        "engine": (control, "engine_card"),
        "killswitches": (risk, "killswitch_lights"),
        "strategies": (control, "strategy_cards"),
        "schedule": (control, "schedule"),
        "health": (health, "health_panel"),
        "soak": (health, "soak_summary"),
        "errors": (control, "error_log"),
    }[part]
    monkeypatch.setattr(*target, boom)
    resp = make_client(_services(db_factory), control_router.router).get("/api/control")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["part_errors"] == [{"part": part, "message": "RuntimeError: boom password=[REDACTED]"}]
    assert "hunter2" not in resp.text
    fields = {p: [p] for p in PARTS}
    fields["killswitches"] = ["killswitches", "killswitch_history"]
    for other in PARTS:
        for field in fields[other]:
            if other == part:
                assert body[field] is None, field
            else:
                assert body[field] is not None, field


def test_every_part_failing_still_answers(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_factory)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("x" * 500)

    for mod, name in (
        (control, "engine_card"),
        (risk, "killswitch_lights"),
        (control, "strategy_cards"),
        (control, "schedule"),
        (health, "health_panel"),
        (health, "soak_summary"),
        (control, "error_log"),
    ):
        monkeypatch.setattr(mod, name, boom)
    resp = make_client(_services(db_factory), control_router.router).get("/api/control")
    assert resp.status_code == 200, resp.text
    errors = resp.json()["part_errors"]
    assert [e["part"] for e in errors] == list(PARTS)
    assert all(e["message"] == "ValueError: " + "x" * 120 for e in errors)


def test_the_live_run_lookup_failing_is_a_500(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(services: ApiServices) -> int:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(control_router, "_live_run", boom)
    client = make_client(_services(db_factory), control_router.router, raise_server_exceptions=False)
    resp = client.get("/api/control")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal"
    assert "hunter2" not in resp.text


def test_401_without_a_session(db_factory: sessionmaker[Session]) -> None:
    resp = make_client(_services(db_factory), control_router.router, user=None).get("/api/control")
    assert resp.status_code == 401


def test_the_route_is_read_only_and_never_calls_questrade(db_factory: sessionmaker[Session]) -> None:
    """Only SELECTs (the soak report included), with the real parts on a seeded database; the services'
    `quotes`/`candles` fail the test if called."""
    _seed(db_factory)
    services = _services(db_factory)
    client = make_client(services, control_router.router)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, sql: str, *args: Any) -> None:
        statements.append(sql)

    engine = db_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        resp = client.get("/api/control")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert resp.status_code == 200, resp.text
    assert resp.json()["part_errors"] == []
    assert statements
    writes = [sql for sql in statements if not sql.lstrip().upper().startswith("SELECT")]
    assert writes == []
    assert not [sql for sql in statements if "FOR UPDATE" in sql.upper() or "ADVISORY" in sql.upper()]


def test_no_live_run_yet_creates_it_as_every_process_does(db_factory: sessionmaker[Session]) -> None:
    resp = make_client(_services(db_factory), control_router.router).get("/api/control")
    assert resp.status_code == 200, resp.text
    with db_factory() as s:
        assert s.query(m.Run).filter(m.Run.mode == "live").count() == 1


def test_the_soak_report_failing_is_the_soak_part(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_factory)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(soak, "load_report", boom)
    resp = make_client(_services(db_factory), control_router.router).get("/api/control")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["soak"] is None
    assert body["part_errors"] == [{"part": "soak", "message": "RuntimeError: boom password=[REDACTED]"}]
