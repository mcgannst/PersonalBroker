"""OPTSIM T14: `/strategies` (list and update through the registry) and the generic panel and action routes
(through the strategy host). The toy plug-in stands in for any plug-in: the routes name none."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from tests.options.factories import T0
from tests.options.fakes import OptionFakes, make_option_services
from tests.options.toy_plugin import ToyCallBuyer
from trader.api.routers.options import router
from trader.market.clock import FixedClock
from trader.option_strategies.base import (
    KeyValue,
    PanelAction,
    PanelActionRequest,
    PanelActionResult,
    PanelColumn,
    PanelRow,
    PanelTable,
    StrategyPanel,
)

pytestmark = pytest.mark.db


def _setup(factory: sessionmaker[Session]) -> tuple[TestClient, OptionFakes]:
    clock = FixedClock(T0)
    options, fakes = make_option_services(factory, clock, ToyCallBuyer)
    return make_client(make_services(test_core(factory, clock), options=options), router), fakes


def test_strategies_list_update_and_validation(db_factory: sessionmaker[Session]) -> None:
    client, fakes = _setup(db_factory)
    fakes.book.add_structure(
        kind="long_call",
        source="toy_call",
        strategy_config_id=1,
        underlying="F",
        qty=1,
        entry_net=Decimal("-1.00"),
        reserved_cash=Decimal("0"),
        cover_structure_id=None,
        parent_structure_id=None,
        take_profit_net=None,
        meta={},
        ts=T0,
    )
    r = client.get("/api/options/strategies")
    assert r.status_code == 200, r.text
    (toy,) = r.json()["items"]
    assert (toy["key"], toy["version"], toy["enabled"], toy["revision"]) == ("toy_call", "0.1.0", True, 1)
    assert toy["params"] == {"underlying": "F", "min_dte": 30, "at": "open+5m"}
    assert (toy["open_structures"], toy["manual_events"], toy["updated_by"]) == (1, ["toy_buy"], "system")
    assert [f["name"] for f in toy["fields"]] == ["underlying", "min_dte", "at"]
    assert toy["fields"][1]["kind"] == "integer" and toy["schema"]["properties"]["min_dte"]["maximum"] == 365

    r = client.put("/api/options/strategies/toy_call", json={"params": {"min_dte": 45}, "enabled": False})
    assert r.status_code == 200, r.text
    updated = r.json()
    assert (updated["revision"], updated["enabled"], updated["params"]["min_dte"]) == (2, False, 45)
    assert (updated["updated_by"], updated["open_structures"]) == ("web:stephen", 1)
    assert fakes.registry.updates == [("toy_call", {"min_dte": 45}, False, "web:stephen")]

    r = client.put("/api/options/strategies/toy_call", json={"params": {"min_dte": 0}})
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "validation" and error["fields"][0]["loc"] == ["params", "min_dte"]
    assert client.put("/api/options/strategies/toy_call", json={}).status_code == 422
    r = client.put("/api/options/strategies/nope", json={"enabled": True})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")
    assert fakes.registry.current("toy_call").revision == 2


def test_panel_and_action_pass_through(db_factory: sessionmaker[Session]) -> None:
    client, fakes = _setup(db_factory)
    fakes.host.panels["toy_call"] = StrategyPanel(
        summary=(KeyValue("Calls held", "1", "ok"),),
        tables=(
            PanelTable(
                key="holdings",
                title="Holdings",
                columns=(PanelColumn("contract", "Contract"), PanelColumn("cost", "Cost", "money")),
                rows=(
                    PanelRow(
                        id="7",
                        cells={"contract": "F 2026-11-20 C 16.00", "cost": "1.00"},
                        actions=("note",),
                        detail=(KeyValue("Opened", "today"),),
                    ),
                ),
                empty_text="No call held",
            ),
        ),
        actions=(
            PanelAction("note", "Leave a note", "text"),
            PanelAction("mode", "Mode", "choice", True, ("a", "b")),
        ),
    )
    r = client.get("/api/options/strategies/toy_call/panel")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "strategy_key": "toy_call",
        "summary": [{"label": "Calls held", "value": "1", "tone": "ok"}],
        "tables": [
            {
                "key": "holdings",
                "title": "Holdings",
                "columns": [
                    {"key": "contract", "label": "Contract", "kind": "text"},
                    {"key": "cost", "label": "Cost", "kind": "money"},
                ],
                "rows": [
                    {
                        "id": "7",
                        "cells": {"contract": "F 2026-11-20 C 16.00", "cost": "1.00"},
                        "actions": ["note"],
                        "detail": [{"label": "Opened", "value": "today", "tone": None}],
                    }
                ],
                "empty_text": "No call held",
            }
        ],
        "actions": [
            {"key": "note", "label": "Leave a note", "kind": "text", "confirm": False, "choices": []},
            {"key": "mode", "label": "Mode", "kind": "choice", "confirm": True, "choices": ["a", "b"]},
        ],
    }

    fakes.host.action_result = PanelActionResult(False, "A note needs some text")
    body = {"action": "note", "row_id": "7", "value": "watch earnings"}
    r = client.post("/api/options/strategies/toy_call/actions", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "message": "A note needs some text"}  # the plug-in's own verdict
    assert fakes.host.actions == [
        ("toy_call", PanelActionRequest("note", "7", "watch earnings"), "web:stephen")
    ]
    r = client.post("/api/options/strategies/toy_call/actions", json={"action": "pause", "value": True})
    assert r.status_code == 200 and fakes.host.actions[-1][1] == PanelActionRequest("pause", None, True)
    assert client.post("/api/options/strategies/toy_call/actions", json={"value": "x"}).status_code == 422

    for method, path in (("GET", "/strategies/nope/panel"), ("POST", "/strategies/nope/actions")):
        r = client.request(method, f"/api/options{path}", json={"action": "note"})
        assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found"), path
    # A strategy the registry knows but the host can't serve (not loaded there) is a 404 too.
    del fakes.host.panels["toy_call"]
    assert client.get("/api/options/strategies/toy_call/panel").status_code == 404
    assert client.post("/api/options/strategies/toy_call/actions", json={"action": "note"}).status_code == 404
