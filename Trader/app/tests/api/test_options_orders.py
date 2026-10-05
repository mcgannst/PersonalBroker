"""OPTSIM T14: `/orders` (working and history, with each leg's fill), cancel and reprice, and `/activity`."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from tests.options.factories import (
    EXPIRY,
    SESSION,
    T0,
    add_contract,
    add_options_run,
    add_order,
    add_position,
    add_structure,
    add_underlying,
    make_leg,
    make_request,
)
from tests.options.fakes import OptionFakes, make_option_services
from trader.api.options_views import event_kind
from trader.api.routers.options import router
from trader.db import models as m
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

TICKET: dict[str, Any] = {
    "underlying": "F",
    "intent": "open",
    "legs": [{"instrument": "option", "contract_id": 1, "side": "sell", "effect": "open", "ratio": 1}],
    "qty": 1,
    "order_type": "limit",
    "net_limit": "0.45",
    "tif": "day",
}


def _setup(factory: sessionmaker[Session]) -> tuple[TestClient, OptionFakes, FixedClock]:
    clock = FixedClock(T0)
    options, fakes = make_option_services(factory, clock)
    fakes.market.add_underlying("F", "15.20")
    fakes.market.add_contract("F", EXPIRY, "14.50", "put", bid="0.45", ask="0.50")
    services = make_services(test_core(factory, clock), options=options)
    return make_client(services, router), fakes, clock


def _submit(client: TestClient, clock: FixedClock) -> int:
    clock.advance(timedelta(seconds=10))
    r = client.post("/api/options/orders", json=TICKET)
    assert r.status_code == 200, r.text
    return int(r.json()["id"])


def _db_fill(factory: sessionmaker[Session]) -> None:
    """The rows the real broker writes for order 1's fill (the fake broker keeps its orders in memory)."""
    with factory() as s:
        run_id = add_options_run(s)
        symbol_id = add_underlying(s, "F")
        contract_id = add_contract(s, symbol_id)
        structure_id = add_structure(s, run_id, symbol_id)
        order_id = add_order(s, run_id, symbol_id, legs=[make_leg(contract_id=contract_id)], status="filled")
        leg_id = s.query(m.OptOrderLeg.id).filter_by(order_id=order_id).scalar()
        s.add(
            m.OptFill(
                run_id=run_id,
                order_id=order_id,
                leg_id=leg_id,
                structure_id=structure_id,
                ts=T0,
                side="sell",
                qty=1,
                price=Decimal("0.45"),
                fee=Decimal("0.99"),
                quote={"bid": "0.45", "ask": "0.50"},
                usd_cad_rate=Decimal("1.37"),
            )
        )
        s.commit()
        assert order_id == 1


def test_orders_working_and_history(db_factory: sessionmaker[Session]) -> None:
    client, fakes, clock = _setup(db_factory)
    first, second, third = (_submit(client, clock) for _ in range(3))
    clock.advance(timedelta(seconds=10))
    fakes.broker.fill(first, {1: Decimal("0.45")})
    _db_fill(db_factory)
    clock.advance(timedelta(seconds=10))
    assert client.post(f"/api/options/orders/{second}/cancel").status_code == 200

    working = client.get("/api/options/orders").json()["items"]
    assert [(o["id"], o["status"]) for o in working] == [(third, "working")]
    assert working[0]["legs"][0]["fill_price"] is None

    r = client.get("/api/options/orders", params={"status": "history"})
    assert r.status_code == 200, r.text
    history = r.json()["items"]
    assert [(o["id"], o["status"]) for o in history] == [(second, "cancelled"), (first, "filled")]
    filled = history[1]
    assert (filled["fill_net"], filled["fees"]) == ("0.45", "0.99")
    assert (filled["legs"][0]["fill_price"], filled["legs"][0]["fill_quote"]) == (
        "0.4500",
        {"bid": "0.45", "ask": "0.50"},
    )
    assert history[0]["legs"][0]["fill_quote"] is None
    assert (
        len(client.get("/api/options/orders", params={"status": "history", "limit": 1}).json()["items"]) == 1
    )
    assert client.get("/api/options/orders", params={"status": "filled"}).status_code == 422


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("working", 200, None),
        ("filled", 409, "conflict"),
        ("strategy", 409, "conflict"),
        ("unknown", 404, "not_found"),
    ],
)
def test_cancel_and_reprice_rules(
    db_factory: sessionmaker[Session], case: str, status: int, code: str | None
) -> None:
    for action, body in (("cancel", None), ("reprice", {"net_limit": "0.40"})):
        client, fakes, clock = _setup(db_factory)
        if case == "strategy":
            order_id = asyncio.run(fakes.broker.submit(make_request(source="toy_call"))).order.id
        elif case == "unknown":
            order_id = 99
        else:
            order_id = _submit(client, clock)
            if case == "filled":
                fakes.broker.fill(order_id, {1: Decimal("0.45")})
        r = client.post(f"/api/options/orders/{order_id}/{action}", json=body)
        assert r.status_code == status, (action, r.text)
        if code is not None:
            assert r.json()["error"]["code"] == code
            assert fakes.broker.cancels == [] and fakes.broker.reprices == []
        elif action == "cancel":
            assert (r.json()["status"], r.json()["closed_at"]) == ("cancelled", "2026-10-06T14:00:10Z")
            assert fakes.broker.cancels == [(order_id, "cancelled from the web", "web:stephen")]
        else:
            assert (r.json()["status"], r.json()["net_limit"], r.json()["walk"]) == ("working", "0.40", False)
            assert fakes.broker.reprices == [(order_id, Decimal("0.40"), "web:stephen")]
    assert client.post("/api/options/orders/1/reprice", json={}).status_code == 422


def test_activity_merges_and_orders_newest_first(db_factory: sessionmaker[Session]) -> None:
    client, _, _ = _setup(db_factory)
    _db_fill(db_factory)  # the fill at T0: run 1, structure 1, order 1

    def at(minutes: int) -> Any:
        return T0 + timedelta(minutes=minutes)

    with db_factory() as s:
        other_run = add_options_run(s, status="completed")
        position_id = add_position(s, 1, 1, 1, contract_id=1)
        s.add(
            m.OptLifecycleEvent(
                run_id=1,
                structure_id=1,
                position_id=position_id,
                contract_id=1,
                kind="assigned",
                session_date=SESSION,
                ts=at(1),
                underlying_close=Decimal("14.10"),
                strike=Decimal("14.50"),
                qty=1,
                shares_delta=100,
                cash_delta=Decimal("-1450"),
                detail={},
            )
        )
        s.add(
            m.OwnerPrompt(
                run_id=1,
                source="toy_call",
                kind="fresh_cash",
                scope_key="F",
                dedupe_key="toy:F:1",
                title="Keep the shares?",
                body="...",
                choices=[{"code": "y", "label": "Yes"}],
                data={},
                status="pending",
                asked_at=at(2),
            )
        )
        for minute, run_id, level, source, message, data in (
            (3, 1, "info", "options.strategy.toy_call", "No candidate today", {"underlying": "F"}),
            (4, 1, "warning", "options.strategy.toy_call", "Strike touched", {"structure_id": 1}),
            (5, 1, "info", "options.broker", "Order 1 cancelled", None),
            (6, other_run, "info", "options.broker", "another run's event", None),
            (7, 1, "info", "worker", "a stock event", None),
        ):
            s.add(
                m.EventLog(
                    ts=at(minute), level=level, source=source, run_id=run_id, message=message, data=data
                )
            )
        s.commit()

    r = client.get("/api/options/activity")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [(i["id"], i["kind"], i["source"]) for i in items] == [
        ("event:3", "order", "broker"),
        ("event:2", "alert", "toy_call"),
        ("event:1", "decision", "toy_call"),
        ("prompt:1", "prompt", "toy_call"),
        ("lifecycle:1", "lifecycle", "manual"),
        ("fill:1", "fill", "manual"),
    ]
    alert, decision, prompt, lifecycle, fill = items[1:]
    assert (alert["level"], alert["structure_id"], alert["title"]) == ("warning", 1, "Strike touched")
    assert (decision["underlying"], decision["structure_id"]) == ("F", None)
    assert (prompt["title"], prompt["detail"]) == ("Keep the shares?", "pending")
    assert (lifecycle["title"], lifecycle["underlying"], lifecycle["structure_id"]) == ("Assigned: F", "F", 1)
    assert lifecycle["detail"] == "1 x F 2026-11-20 P 14.50; close 14.10; shares +100; cash -1450.00"
    assert (fill["title"], fill["underlying"], fill["structure_id"]) == ("Filled: open F x1", "F", 1)
    assert fill["detail"] == "sell 1 F 2026-11-20 P 14.50 at 0.45"

    older = client.get("/api/options/activity", params={"before": at(2).isoformat(), "limit": 1})
    assert [i["id"] for i in older.json()["items"]] == ["lifecycle:1"]
    assert client.get("/api/options/activity", params={"before": "2026-10-06T14:00:00"}).status_code == 422
    assert event_kind("options.orders", "info") == "order" and event_kind("options.jobs", "error") == "alert"
