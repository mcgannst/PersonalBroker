"""P4-T8 acceptance tests 1-3: `GET /api/settings` and `PUT /api/settings/{key}` (BR-30, BR-53, SPEC §6.2,
§13). Changes go through `SettingsStore.set`, which validates and writes the audit row."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from trader.api.forms import SETTING_GROUPS
from trader.api.routers.settings import router
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
DB_KEYS = [(f.alias or n) for n, f in RuntimeSettings.model_fields.items()]

pytestmark = pytest.mark.db


def _client(factory: sessionmaker[Session]) -> TestClient:
    return make_client(make_services(test_core(factory, FixedClock(NOW))), router)


def _items(client: TestClient) -> dict[str, dict]:  # type: ignore[type-arg]
    r = client.get("/api/settings")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    keys = [i["key"] for i in items]
    assert len(keys) == len(set(keys))
    return {i["key"]: i for i in items}


def test_get_lists_every_setting_once_with_descriptors(db_factory: sessionmaker[Session]) -> None:
    items = _items(_client(db_factory))
    assert sorted(items) == sorted(DB_KEYS)

    approval = items["approval_mode"]
    assert approval["field"]["kind"] == "enum" and approval["field"]["enum"] == ["manual", "auto"]
    assert (approval["value"], approval["default"], approval["is_default"]) == ("manual", "manual", True)
    assert approval["group"] == "Approvals"
    assert approval["updated_at"] is None and approval["updated_by"] is None

    risk = items["risk_pct"]
    assert risk["field"]["kind"] == "decimal" and risk["field"]["maximum"] == "0.10"
    assert risk["field"]["exclusive_minimum"] is True and risk["value"] == "0.02"
    assert risk["group"] == "Risk"
    assert (items["quote_poll_seconds"]["field"]["kind"], items["quote_poll_seconds"]["group"]) == (
        "number",
        "Fill model",
    )
    assert items["scheduler.always_fire_late"]["field"]["kind"] == "string_list"
    assert items["scheduler.always_fire_late"]["group"] == "Worker and Telegram"
    assert items["markets_enabled"]["field"]["kind"] == "enum_list"
    assert items["markets_enabled"]["field"]["item_enum"] == ["US", "TSX"]
    assert items["markets_enabled"]["group"] == "Account"
    assert (items["cash_account_mode"]["field"]["kind"], items["cash_account_mode"]["group"]) == (
        "boolean",
        "Account",
    )
    for key, item in items.items():
        assert item["field"]["name"] == key
        assert item["group"] in set(SETTING_GROUPS.values()), key


def test_get_is_sorted_by_group_then_key(db_factory: sessionmaker[Session]) -> None:
    r = _client(db_factory).get("/api/settings")
    items = r.json()["items"]
    order = list(dict.fromkeys(SETTING_GROUPS.values()))
    pairs = [(order.index(i["group"]), i["key"]) for i in items]
    assert pairs == sorted(pairs)


def test_put_approval_mode_is_stored_and_audited(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    r = client.put("/api/settings/approval_mode", json={"value": "auto"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["key"], body["value"], body["is_default"], body["updated_by"]) == (
        "approval_mode",
        "auto",
        False,
        "web:stephen",
    )
    with db_factory() as s:
        audit = s.execute(
            select(m.AuditLog).where(m.AuditLog.action == "settings.set:approval_mode")
        ).scalar_one()
        assert audit.actor == "web:stephen"
        assert audit.before == {"value": "manual"} and audit.after == {"value": "auto"}
        assert s.get(m.Setting, "approval_mode").value == "auto"  # type: ignore[union-attr]

    after = _items(client)["approval_mode"]
    assert (after["value"], after["is_default"], after["updated_by"]) == ("auto", False, "web:stephen")
    assert after["updated_at"] == "2026-10-06T15:30:00Z"


def test_put_decimal_and_list_values(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    r = client.put("/api/settings/risk_pct", json={"value": "0.03"})
    assert r.status_code == 200 and r.json()["value"] == "0.03"
    r = client.put("/api/settings/markets_enabled", json={"value": ["US", "TSX"]})
    assert r.status_code == 200 and r.json()["value"] == ["US", "TSX"]


def test_put_out_of_range_is_422_and_stores_nothing(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    r = client.put("/api/settings/risk_pct", json={"value": "0.5"})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation"
    assert err["fields"] and all(f["msg"] for f in err["fields"])
    assert "0.5" not in str(err["fields"])  # never echoes the input
    with db_factory() as s:
        assert s.get(m.Setting, "risk_pct") is None
        assert s.execute(select(m.AuditLog)).first() is None


def test_put_unknown_key_is_404(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    r = client.put("/api/settings/no_such_key", json={"value": 1})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    # A field NAME that is not its DB key is unknown too (keys are aliases).
    assert client.put("/api/settings/fx_fee_pct", json={"value": "0.01"}).status_code == 404


def test_put_without_a_value_is_422(db_factory: sessionmaker[Session]) -> None:
    assert _client(db_factory).put("/api/settings/approval_mode", json={}).status_code == 422


def test_get_with_a_corrupt_stored_row_still_lists_it(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(m.Setting(key="risk_pct", value="lots", updated_at=NOW, updated_by="manual"))
        s.commit()
    items = _items(_client(db_factory))
    assert items["risk_pct"]["value"] == "lots" and items["risk_pct"]["is_default"] is False
    assert items["approval_mode"]["value"] == "manual"
