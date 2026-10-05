"""OPTSIM T14: `/settings` (the `options.*` keys, through `OptionSettingsStore`), and the rule that no
error response of any options route repeats what was sent."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from tests.options.factories import T0
from tests.options.fakes import make_option_services
from tests.options.toy_plugin import ToyCallBuyer
from trader.api.routers.options import router
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.options.settings import OPTION_SETTING_KEYS
from trader.options.types import OwnerPromptRequest, PromptChoice

pytestmark = pytest.mark.db


def _client(factory: sessionmaker[Session]) -> TestClient:
    clock = FixedClock(T0)
    options, fakes = make_option_services(factory, clock, ToyCallBuyer)
    fakes.prompts.ensure(
        1, "toy_call", OwnerPromptRequest("k", "F", "k1", "Why?", "Body", (PromptChoice("a", "Answer"),))
    )
    return make_client(make_services(test_core(factory, clock), options=options), router)


def test_settings_list_put_and_bounds(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:  # a stock setting of the same field name is not an option setting
        s.add(m.Setting(key="max_position_pct", value="0.10", updated_at=T0, updated_by="manual"))
        s.commit()
    client = _client(db_factory)
    r = client.get("/api/options/settings")
    assert r.status_code == 200, r.text
    items = {i["key"]: i for i in r.json()["items"]}
    assert list(items) == list(OPTION_SETTING_KEYS)
    assert {i["group"] for i in items.values()} == {"Options"}
    cap = items["options.max_position_pct"]
    assert (cap["value"], cap["default"], cap["is_default"], cap["updated_by"]) == (
        "0.50",
        "0.50",
        True,
        None,
    )
    assert (cap["field"]["name"], cap["field"]["kind"], cap["field"]["maximum"]) == (
        "options.max_position_pct",
        "decimal",
        "1",
    )
    assert items["options.watchlist"]["field"]["kind"] == "string_list"
    assert "ASSUMPTION" in items["options.tick_size"]["field"]["description"]

    r = client.put("/api/options/settings/options.max_position_pct", json={"value": "0.25"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["value"], body["is_default"], body["updated_by"], body["updated_at"]) == (
        "0.25",
        False,
        "web:stephen",
        "2026-10-06T14:00:00Z",
    )
    assert client.put("/api/options/settings/options.watchlist", json={"value": ["F", "SOFI"]}).json()[
        "value"
    ] == ["F", "SOFI"]

    r = client.put("/api/options/settings/options.max_position_pct", json={"value": "1.5"})
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "validation" and error["fields"] and "1.5" not in str(error["fields"])
    assert client.put("/api/options/settings/options.max_legs", json={}).status_code == 422

    with db_factory() as s:
        audit = s.execute(
            select(m.AuditLog).where(m.AuditLog.action == "settings.set:options.max_position_pct")
        ).scalar_one()
        assert (audit.actor, audit.after) == ("web:stephen", {"value": "0.25"})
        assert s.get(m.Setting, "options.max_position_pct").value == "0.25"  # type: ignore[union-attr]
        assert s.get(m.Setting, "max_position_pct").value == "0.10"  # type: ignore[union-attr]
    assert client.get("/api/options/settings").json()["items"][1]["value"] == "0.25"


def test_unknown_setting_404(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    for key in ("options.no_such_key", "max_position_pct", "risk_pct"):  # a field name, a stock key
        r = client.put(f"/api/options/settings/{key}", json={"value": "0.25"})
        assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found"), key
    with db_factory() as s:
        assert s.execute(select(m.Setting)).first() is None


MARK = "Zq7secretZq7"  # appears in every rejected request below, never in a response
ORDER: dict[str, Any] = {
    "underlying": "F",
    "intent": "open",
    "legs": [{"instrument": "option", "contract_id": 1, "side": "sell", "effect": "open", "ratio": 1}],
    "qty": 1,
    "order_type": "limit",
    "net_limit": "0.45",
    "tif": "day",
}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", f"/chain?underlying={MARK}", None),
        ("GET", "/chain?underlying=ZQSEVEN", None),
        ("GET", f"/chain/quotes?underlying=F&expiry={MARK}", None),
        ("GET", f"/positions?state={MARK}", None),
        ("GET", f"/activity?before={MARK}", None),
        ("POST", "/orders", {**ORDER, "underlying": MARK}),
        ("POST", "/orders/preview", {**ORDER, "tif": MARK}),
        ("POST", "/orders", {**ORDER, "net_limit": "777.123456"}),
        ("POST", "/orders/1/reprice", {"net_limit": MARK}),
        ("POST", f"/orders/{MARK}/cancel", None),
        ("POST", "/prompts/1/answer", {"choice": "z", "text": MARK}),
        ("POST", "/prompts/1/answer", {"choice": MARK}),
        ("PUT", "/strategies/toy_call", {"params": {"underlying": MARK.lower()}}),
        ("PUT", "/strategies/toy_call", {"params": {"min_dte": 777123456}}),
        ("PUT", f"/strategies/{MARK}", {"enabled": True}),
        ("GET", f"/strategies/{MARK}/panel", None),
        ("POST", "/strategies/toy_call/actions", {"action": "", "value": MARK}),
        ("PUT", "/settings/options.benchmark_ticker", {"value": MARK}),
        ("PUT", "/settings/options.max_legs", {"value": 777123456}),
        ("PUT", f"/settings/{MARK}", {"value": 1}),
    ],
)
def test_no_response_echoes_an_invalid_input(
    db_factory: sessionmaker[Session], method: str, path: str, body: dict[str, Any] | None
) -> None:
    r = _client(db_factory).request(method, f"/api/options{path}", json=body)
    assert 400 <= r.status_code < 500, r.text
    assert r.json()["error"]["code"] in {"validation", "not_found"}
    for sent in (MARK, MARK.lower(), "ZQSEVEN", "777123456", "777.123456"):
        assert sent not in r.text, r.text
