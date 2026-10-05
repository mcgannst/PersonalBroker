"""OPTSIM T14: the Trade tab's routes: `/chain`, `/chain/quotes`, the ticket preview and submit."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from tests.options.factories import EXPIRY, T0
from tests.options.fakes import CannedCollateral, OptionFakes, accept, make_option_services, reject
from trader.api.routers.options import router
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

WEEKLY = date(2026, 10, 16)


def _setup(factory: sessionmaker[Session]) -> tuple[TestClient, OptionFakes, FixedClock]:
    """F at 15.20 with a 14.50 put and call and a 15.00 put on the monthly, and one weekly put."""
    clock = FixedClock(T0)
    options, fakes = make_option_services(factory, clock)
    market = fakes.market
    market.add_underlying("F", "15.20")
    market.add_contract("F", EXPIRY, "14.50", "put", bid="0.40", ask="0.45", delta="-0.30", iv="0.35")
    market.add_contract("F", EXPIRY, "14.50", "call", bid="1.10", ask="1.15")
    market.add_contract("F", EXPIRY, "15", "put", bid="0.60", ask="0.66")
    market.add_contract("F", WEEKLY, "15", "put", bid="0.20", ask="0.24")
    services = make_services(test_core(factory, clock), options=options)
    return make_client(services, router), fakes, clock


def _ticket(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "underlying": "F",
        "intent": "open",
        "legs": [{"instrument": "option", "contract_id": 1, "side": "sell", "effect": "open", "ratio": 1}],
        "qty": 1,
        "order_type": "limit",
        "net_limit": "0.42",
        "tif": "day",
    }
    return {**body, **changes}


def test_chain_expiries(db_factory: sessionmaker[Session]) -> None:
    client, _, _ = _setup(db_factory)
    r = client.get("/api/options/chain", params={"underlying": "F"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "underlying": "F",
        "underlying_price": "15.20",
        "price_time": "2026-10-06T14:00:00Z",
        "market_open": True,
        "expiries": [
            {"expiry": "2026-10-16", "dte": 10, "is_monthly": True, "strikes": 1},
            {"expiry": "2026-11-20", "dte": 45, "is_monthly": True, "strikes": 2},
        ],
    }


def test_chain_quotes_rows_cache_and_stale_flag(db_factory: sessionmaker[Session]) -> None:
    client, fakes, clock = _setup(db_factory)
    fakes.market.set_quote(3, "0.60", "0.66", fetched_at=T0 - timedelta(seconds=60))  # an old quote
    query = {"underlying": "F", "expiry": str(EXPIRY)}
    r = client.get("/api/options/chain/quotes", params=query)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["underlying_price"], body["fetched_at"], body["market_open"]) == (
        "15.20",
        "2026-10-06T14:00:00Z",
        True,
    )
    low, high = body["rows"]
    assert (low["strike"], low["call_contract_id"], low["put_contract_id"]) == ("14.50", 2, 1)
    assert (low["put"]["bid"], low["put"]["ask"], low["put"]["delta"], low["put"]["iv"]) == (
        "0.40",
        "0.45",
        "-0.30",
        "0.35",
    )
    assert (low["put"]["stale"], low["call"]["bid"]) == (False, "1.10")
    assert (high["strike"], high["call_contract_id"], high["call"]) == ("15", None, None)
    assert high["put"]["stale"] is True  # older than options.stale_quote_seconds

    # Cached for options.web_quote_cache_seconds (5): a new quote is not seen until the cache has aged.
    fakes.market.set_quote(1, "0.50", "0.55")
    clock.advance(timedelta(seconds=4))
    assert client.get("/api/options/chain/quotes", params=query).json()["rows"][0]["put"]["bid"] == "0.40"
    clock.advance(timedelta(seconds=2))
    fresh = client.get("/api/options/chain/quotes", params=query).json()
    assert (fresh["rows"][0]["put"]["bid"], fresh["rows"][0]["put"]["stale"]) == ("0.50", False)

    # A closed market makes every quote stale, also the cached ones.
    fakes.market.open = False
    closed = client.get("/api/options/chain/quotes", params=query).json()
    assert closed["market_open"] is False and closed["rows"][0]["put"]["stale"] is True


def test_unknown_underlying_404(db_factory: sessionmaker[Session]) -> None:
    client, _, _ = _setup(db_factory)
    for path, query in (
        ("/chain", {"underlying": "ZZZ"}),
        ("/chain/quotes", {"underlying": "ZZZ", "expiry": str(EXPIRY)}),
        ("/chain/quotes", {"underlying": "F", "expiry": "2027-01-15"}),  # no such expiry
    ):
        r = client.get(f"/api/options{path}", params=query)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found"), (path, query)
        assert "ZZZ" not in r.text
    assert client.get("/api/options/chain", params={"underlying": "not a ticker"}).status_code == 422
    assert client.get("/api/options/chain").status_code == 422


def test_preview_shows_the_collateral_verdict(db_factory: sessionmaker[Session]) -> None:
    client, fakes, _ = _setup(db_factory)
    fakes.broker.collateral = CannedCollateral(reject("naked_short", "A short call needs cover"))
    r = client.post("/api/options/orders/preview", json=_ticket())
    assert r.status_code == 200, r.text  # a rejection is a verdict, not an HTTP error
    body = r.json()
    assert (body["accepted"], body["reject_reason"], body["detail"]) == (
        False,
        "naked_short",
        "A short call needs cover",
    )

    fakes.broker.collateral = CannedCollateral(
        accept(kind="csp", reserve_cash="1450", cash="5000", free_cash="3590.01")
    )
    body = client.post("/api/options/orders/preview", json=_ticket()).json()
    assert (body["accepted"], body["reject_reason"], body["kind"]) == (True, None, "csp")
    assert (body["reserve_cash"], body["cash_after"], body["free_cash_after"]) == (
        "1450",
        "5000",
        "3590.01",
    )
    assert body["breakevens"] == [] and body["max_loss"] is None

    # The engine was asked about a manual order of the signed-in user; nothing was submitted.
    req = fakes.broker.previews[-1]
    assert (req.source, req.submitted_by, req.intent, req.qty) == ("manual", "web:stephen", "open", 1)
    assert (req.order_type, req.net_limit, req.tif, req.walk) == ("limit", Decimal("0.42"), "day", False)
    (leg,) = req.legs
    assert (leg.leg_no, leg.instrument, leg.side, leg.effect, leg.ratio, leg.underlying, leg.contract_id) == (
        1,
        "option",
        "sell",
        "open",
        1,
        "F",
        1,
    )
    assert fakes.broker.submitted == []


def test_submit_working_and_rejected_orders(db_factory: sessionmaker[Session]) -> None:
    client, fakes, _ = _setup(db_factory)
    fakes.broker.collateral = CannedCollateral(accept(kind="csp", reserve_cash="1450"))
    r = client.post("/api/options/orders", json=_ticket())
    assert r.status_code == 200, r.text
    order = r.json()
    assert (order["id"], order["status"], order["source"], order["reason"]) == (
        1,
        "working",
        "manual",
        "manual",
    )
    assert (order["net_limit"], order["reserved_cash"], order["fill_net"]) == ("0.42", "1450", None)
    (leg,) = order["legs"]
    assert (leg["contract"]["label"], leg["side"], leg["fill_price"]) == (
        "F 2026-11-20 P 14.50",
        "sell",
        None,
    )
    # Submitting never fills: the worker does.
    assert fakes.broker.fills == [] and "poll" not in fakes.broker.calls

    fakes.broker.collateral = CannedCollateral(reject("insufficient_cash", "Short by 12.00"))
    r = client.post("/api/options/orders", json=_ticket(order_type="market", net_limit=None))
    assert r.status_code == 200, r.text
    rejected = r.json()
    assert (rejected["status"], rejected["reject_reason"], rejected["reject_detail"]) == (
        "rejected",
        "insufficient_cash",
        "Short by 12.00",
    )
    assert [req.submitted_by for req in fakes.broker.submitted] == ["web:stephen", "web:stephen"]


SHARES_LEG = {"instrument": "shares", "contract_id": None, "side": "buy", "effect": "open", "ratio": 100}
OPTION_LEG = {"instrument": "option", "contract_id": 1, "side": "sell", "effect": "open", "ratio": 1}


@pytest.mark.parametrize(
    ("changes", "loc"),
    [
        ({"qty": 0}, ["body", "qty"]),
        ({"legs": []}, ["body", "legs"]),
        ({"legs": [OPTION_LEG] * 5}, ["body", "legs"]),
        ({"underlying": "f"}, ["body", "underlying"]),
        ({"net_limit": "0.12345"}, ["body", "net_limit"]),
        ({"legs": [{**OPTION_LEG, "contract_id": None}]}, ["body", "legs", 0, "contract_id"]),
        ({"legs": [{**SHARES_LEG, "contract_id": 1}]}, ["body", "legs", 0, "contract_id"]),
        ({"order_type": "market"}, ["body", "net_limit"]),
        ({"order_type": "market", "net_limit": None, "walk": True}, ["body", "walk"]),
        ({"net_limit": None}, ["body", "net_limit"]),
        ({"intent": "close"}, ["body", "structure_id"]),
    ],
)
def test_submit_validation_422(
    db_factory: sessionmaker[Session], changes: dict[str, Any], loc: list[str | int]
) -> None:
    client, fakes, _ = _setup(db_factory)
    for path in ("/api/options/orders/preview", "/api/options/orders"):
        r = client.post(path, json=_ticket(**changes))
        assert r.status_code == 422, r.text
        error = r.json()["error"]
        assert error["code"] == "validation" and error["fields"][0]["loc"] == loc
    assert fakes.broker.previews == [] and fakes.broker.submitted == []
    # A walking limit order needs no limit: the broker starts it at the midpoint.
    assert client.post("/api/options/orders", json=_ticket(net_limit=None, walk=True)).status_code == 200
