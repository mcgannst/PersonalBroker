"""P4-T8 acceptance tests 4-5: `GET /api/strategies` and `PUT /api/strategies/{key}` (BR-10, BR-22, SPEC §5,
§11). Changes go through `StrategyRegistry.update`: a new versioned, audited revision."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from trader.api.deps import ApiServices
from trader.api.routers.strategies import router
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
DAY = date(2026, 10, 6)

pytestmark = pytest.mark.db


def _setup(factory: sessionmaker[Session]) -> tuple[ApiServices, TestClient]:
    services = make_services(test_core(factory, FixedClock(NOW)))
    services.registry.ensure_defaults()
    return services, make_client(services, router)


def _revisions(factory: sessionmaker[Session], key: str) -> int:
    with factory() as s:
        return s.execute(
            select(func.count()).select_from(m.StrategyConfig).where(m.StrategyConfig.strategy_key == key)
        ).scalar_one()


def _open_position(factory: sessionmaker[Session], config_id: int, *, closed: bool = False) -> None:
    run_id = get_live_run(factory, FixedClock(NOW), RuntimeSettings()).id
    with factory() as s:
        sym = m.Symbol(ticker="ABCD", exchange="NASDAQ", currency="USD")
        s.add(sym)
        s.flush()
        s.add(
            m.Position(
                run_id=run_id,
                symbol_id=sym.id,
                strategy_config_id=config_id,
                qty=0 if closed else 10,
                avg_price=Decimal("12.50"),
                session_date=DAY,
                opened_at=NOW,
                closed_at=NOW if closed else None,
                entry_order_id=1,
                unprotected_seconds=0,
            )
        )
        s.commit()


def test_get_lists_both_plugins_with_schema_and_fields(db_factory: sessionmaker[Session]) -> None:
    services, client = _setup(db_factory)
    r = client.get("/api/strategies")
    assert r.status_code == 200, r.text
    items = {i["key"]: i for i in r.json()["items"]}
    assert sorted(items) == ["orb_sip", "spy_overlay"]

    orb = items["orb_sip"]
    assert (orb["version"], orb["kind"], orb["revision"], orb["enabled"]) == ("1.0.0", "entry", 1, True)
    assert orb["schema"] == services.registry.json_schema("orb_sip")
    assert orb["params"]["top_n"] == 20
    assert orb["updated_at"] == "2026-10-06T15:30:00Z" and orb["updated_by"] == "system"
    assert orb["owns_open_positions"] is False
    fields = {f["name"]: f for f in orb["fields"]}
    assert (fields["entry_cancel_at"]["kind"], fields["entry_cancel_at"]["nullable"]) == ("string", True)
    assert (fields["stale_universe"]["kind"], fields["stale_universe"]["enum"]) == ("enum", ["skip", "trade"])
    assert set(fields) == set(orb["params"])

    overlay = items["spy_overlay"]
    assert (overlay["version"], overlay["kind"], overlay["revision"]) == ("1.0.0", "overlay", 1)
    assert {f["name"] for f in overlay["fields"]} == set(overlay["params"])


def test_put_params_creates_an_audited_revision(db_factory: sessionmaker[Session]) -> None:
    _, client = _setup(db_factory)
    before = client.get("/api/strategies").json()["items"][0]
    assert before["key"] == "orb_sip"

    r = client.put("/api/strategies/orb_sip", json={"params": {"top_n": 10}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["revision"] == before["revision"] + 1 and out["updated_by"] == "web:stephen"
    assert out["params"] == {**before["params"], "top_n": 10}
    assert out["enabled"] is True
    with db_factory() as s:
        audit = s.execute(
            select(m.AuditLog).where(m.AuditLog.action == "strategy.update:orb_sip")
        ).scalar_one()
        assert audit.actor == "web:stephen"
        assert audit.before["params"]["top_n"] == 20 and audit.after["params"]["top_n"] == 10


def test_put_invalid_params_is_422_without_a_revision(db_factory: sessionmaker[Session]) -> None:
    _, client = _setup(db_factory)
    r = client.put("/api/strategies/orb_sip", json={"params": {"top_n": -1}})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation"
    assert [f["loc"] for f in err["fields"]] == [["params", "top_n"]]
    assert err["fields"][0]["msg"]
    assert _revisions(db_factory, "orb_sip") == 1
    # An unknown parameter is refused too (the params models forbid extras).
    assert client.put("/api/strategies/orb_sip", json={"params": {"nope": 1}}).status_code == 422
    assert _revisions(db_factory, "orb_sip") == 1


def test_put_422_never_echoes_a_value(db_factory: sessionmaker[Session]) -> None:
    """P5-GW fix round 1: the plug-in validators leave the value out, pydantic's "Value error, " prefix is
    dropped, and a validator that still formats the value (spy_overlay's) is scrubbed by the router."""
    _, client = _setup(db_factory)
    bad = {"entry_cancel_at": "SENTINEL-cancel", "exit_at": "open+45m", "stale_universe": "SENTINEL-stale"}
    r = client.put("/api/strategies/orb_sip", json={"params": bad})
    assert r.status_code == 422, r.text
    assert "SENTINEL" not in r.text and "open+45m" not in r.text
    msgs = {f["loc"][-1]: f["msg"] for f in r.json()["error"]["fields"]}
    assert msgs["entry_cancel_at"] == "not a session offset (e.g. 'open+5m', 'close-30m', 'open+5m5s')"
    assert msgs["exit_at"] == "exit_at must be a negative offset from the close (e.g. 'close-10m')"
    overlay = client.put("/api/strategies/spy_overlay", json={"params": {"decision_at": "open+45m"}})
    assert overlay.status_code == 422, overlay.text
    assert "open+45m" not in overlay.text
    assert [f["msg"] for f in overlay.json()["error"]["fields"]] == ["invalid value"]
    assert _revisions(db_factory, "orb_sip") == 1


def test_put_unknown_key_and_empty_body(db_factory: sessionmaker[Session]) -> None:
    _, client = _setup(db_factory)
    r = client.put("/api/strategies/no_such", json={"enabled": False})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    bodies: list[dict[str, object]] = [{}, {"params": None, "enabled": None}, {"params": {}}]
    for body in bodies:
        r = client.put("/api/strategies/orb_sip", json=body)
        assert r.status_code == 422, body
        assert r.json()["error"]["code"] == "validation"
    assert _revisions(db_factory, "orb_sip") == 1


def test_disable_with_an_open_position_is_allowed_and_flagged(db_factory: sessionmaker[Session]) -> None:
    services, client = _setup(db_factory)
    _open_position(db_factory, services.registry.current("orb_sip").id)
    r = client.put("/api/strategies/orb_sip", json={"enabled": False})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["enabled"], out["revision"], out["owns_open_positions"]) == (False, 2, True)
    items = {i["key"]: i for i in client.get("/api/strategies").json()["items"]}
    assert items["orb_sip"]["owns_open_positions"] is True  # config ids of every revision count
    assert items["spy_overlay"]["owns_open_positions"] is False


def test_closed_position_and_working_order(db_factory: sessionmaker[Session]) -> None:
    services, client = _setup(db_factory)
    config_id = services.registry.current("orb_sip").id
    _open_position(db_factory, config_id, closed=True)
    items = {i["key"]: i for i in client.get("/api/strategies").json()["items"]}
    assert items["orb_sip"]["owns_open_positions"] is False

    run_id = get_live_run(db_factory, FixedClock(NOW), RuntimeSettings()).id
    with db_factory() as s:
        sym_id = s.execute(select(m.Symbol.id)).scalar_one()
        s.add(
            m.Order(
                run_id=run_id,
                strategy_config_id=config_id,
                symbol_id=sym_id,
                side="buy",
                order_type="stop",
                purpose="entry",
                qty=5,
                stop_price=Decimal("13"),
                tif="day",
                status="working",
                reason="orb entry",
                session_date=DAY,
                submitted_at=NOW,
                stale_alerted=False,
            )
        )
        s.commit()
    items = {i["key"]: i for i in client.get("/api/strategies").json()["items"]}
    assert items["orb_sip"]["owns_open_positions"] is True
