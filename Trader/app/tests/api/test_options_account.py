"""OPTSIM T14: the rules every `/api/options` route follows (login, CSRF, 503 without option services, 409
without an options run), then `/account` and `/positions` over the fakes."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import BASE_URL, DEFAULT_USER, make_client
from tests.fakes_api import make_services, test_core
from tests.options.factories import EXPIRY, T0, add_options_run
from tests.options.fakes import OptionFakes, make_option_services
from tests.options.toy_plugin import ToyCallBuyer
from trader.api.deps import AuthUser, current_user
from trader.api.errors import install_error_handlers
from trader.api.options_views import WORKER_PROCESS
from trader.api.routers.options import router
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.market.types import Candle

pytestmark = pytest.mark.db

STARTED = T0 - timedelta(days=5)  # when the options run of `_book` started
ORDER = {
    "underlying": "F",
    "intent": "open",
    "legs": [{"instrument": "option", "contract_id": 1, "side": "sell", "effect": "open", "ratio": 1}],
    "qty": 1,
    "order_type": "limit",
    "net_limit": "0.45",
    "tif": "day",
}
# The 18 routes of the task plan §3.9: (method, path, JSON body of a write).
ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/account", None),
    ("GET", "/positions", None),
    ("GET", "/chain?underlying=F", None),
    ("GET", f"/chain/quotes?underlying=F&expiry={EXPIRY}", None),
    ("POST", "/orders/preview", ORDER),
    ("POST", "/orders", ORDER),
    ("GET", "/orders", None),
    ("POST", "/orders/1/cancel", {}),
    ("POST", "/orders/1/reprice", {"net_limit": "0.40"}),
    ("GET", "/activity", None),
    ("GET", "/prompts", None),
    ("POST", "/prompts/1/answer", {"choice": "y"}),
    ("GET", "/strategies", None),
    ("PUT", "/strategies/toy_call", {"enabled": False}),
    ("GET", "/strategies/toy_call/panel", None),
    ("POST", "/strategies/toy_call/actions", {"action": "note", "value": "hello"}),
    ("GET", "/settings", None),
    ("PUT", "/settings/options.max_legs", {"value": 2}),
]
NO_RUN_NEEDED = {
    ("GET", "/strategies"),
    ("PUT", "/strategies/toy_call"),
    ("GET", "/settings"),
    ("PUT", "/settings/options.max_legs"),
}


def _setup(
    factory: sessionmaker[Session], *, run_id: int | None = 1, user: AuthUser | None = DEFAULT_USER
) -> tuple[TestClient, OptionFakes]:
    clock = FixedClock(T0)
    options, fakes = make_option_services(factory, clock, ToyCallBuyer, run_id=run_id)
    services = make_services(test_core(factory, clock), options=options)
    return make_client(services, router, user=user), fakes


def _call(client: TestClient, method: str, path: str, body: dict[str, Any] | None, **kw: Any) -> Any:
    return client.request(method, f"/api/options{path}", json=body, **kw)


def test_every_route_needs_login_and_writes_need_csrf(db_factory: sessionmaker[Session]) -> None:
    declared = {(method, r.path) for r in router.routes if isinstance(r, APIRoute) for method in r.methods}
    assert len(declared) == len(ROUTES) == 18
    anonymous, _ = _setup(db_factory, user=None)
    for method, path, body in ROUTES:
        r = _call(anonymous, method, path, body)
        assert (r.status_code, r.json()["error"]["code"]) == (401, "unauthorized"), (method, path)

    # Signed in, with the real CSRF check: a write without the header is refused, with it it gets through.
    clock = FixedClock(T0)
    options, _ = make_option_services(db_factory, clock, ToyCallBuyer)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router, prefix="/api")
    app.state.services = make_services(test_core(db_factory, clock), options=options)
    app.dependency_overrides[current_user] = lambda: DEFAULT_USER
    client = TestClient(app, base_url=BASE_URL)
    token = {"X-CSRF-Token": DEFAULT_USER.csrf_token}
    for method, path, body in ROUTES:
        r = _call(client, method, path, body)
        if method == "GET":
            assert r.status_code != 403, (method, path)
        else:
            assert (r.status_code, r.json()["error"]["code"]) == (403, "csrf"), (method, path)
            assert _call(client, method, path, body, headers=token).status_code != 403, (method, path)


def test_503_without_option_services(db_factory: sessionmaker[Session]) -> None:
    client = make_client(make_services(test_core(db_factory, FixedClock(T0))), router)
    for method, path, body in ROUTES:
        r = _call(client, method, path, body)
        assert (r.status_code, r.json()["error"]["code"]) == (503, "unavailable"), (method, path)


def test_409_without_an_options_run_except_settings_and_strategies(
    db_factory: sessionmaker[Session],
) -> None:
    client, _ = _setup(db_factory, run_id=None)
    for method, path, body in ROUTES:
        r = _call(client, method, path, body)
        if (method, path) in NO_RUN_NEEDED:
            assert r.status_code == 200, (method, path, r.text)
        else:
            assert (r.status_code, r.json()["error"]["code"]) == (409, "no_options_run"), (method, path)


def _book(factory: sessionmaker[Session]) -> tuple[TestClient, OptionFakes]:
    """A real options run row with 5,000 cash, and in the fake book: a manual short put (open), a toy_call
    long call (open) and a manual long call closed for a 50.00 gain."""
    with factory() as s:
        run_id = add_options_run(s, 5000, started_at=STARTED)
        s.add(
            m.WorkerHeartbeat(
                process=WORKER_PROCESS, pid=1, host="test", started_at=T0, beat_at=T0, phase="session"
            )
        )
        s.commit()
    client, fakes = _setup(factory, run_id=run_id)
    market, book = fakes.market, fakes.book
    market.add_underlying("F", "15.20")
    put = market.add_contract("F", EXPIRY, "14.50", "put", bid="0.40", ask="0.45", delta="-0.30")
    call = market.add_contract("F", EXPIRY, "16", "call", bid="1.20", ask="1.25", delta="0.40")

    def add(source: str, kind: str, entry: str, reserve: str = "0") -> int:
        return book.add_structure(
            kind=kind,  # type: ignore[arg-type]
            source=source,
            strategy_config_id=None,
            underlying="F",
            qty=1,
            entry_net=Decimal(entry),
            reserved_cash=Decimal(reserve),
            cover_structure_id=None,
            parent_structure_id=None,
            take_profit_net=None,
            meta={},
            ts=T0,
        )

    csp = add("manual", "csp", "0.50", "1450")
    book.apply(csp, "option", put.id, -1, Decimal("0.50"), T0)
    book.move_cash(Decimal("50"), "sell", "opt_fill:1", T0, structure_id=csp)
    book.move_cash(Decimal("-0.99"), "fee", "opt_fill:1", T0, structure_id=csp)
    toy = add("toy_call", "long_call", "-1.00")
    book.apply(toy, "option", call.id, 1, Decimal("1.00"), T0)
    book.move_cash(Decimal("-100"), "buy", "opt_fill:2", T0, structure_id=toy)
    done = add("manual", "long_call", "-1.00")
    book.apply(done, "option", call.id, 1, Decimal("1.00"), T0)
    book.apply(done, "option", call.id, -1, Decimal("1.50"), T0 + timedelta(minutes=5))
    book.close_structure(done, "closed", T0 + timedelta(minutes=5))
    fakes.broker.premium_collected = Decimal("50")
    return client, fakes


def test_account_numbers_and_by_source(db_factory: sessionmaker[Session]) -> None:
    client, fakes = _book(db_factory)
    fakes.market.add_underlying("SOFI", "11")
    fakes.market.set_bars(
        "SOFI",
        [
            Candle(at, at + timedelta(hours=6), Decimal(c), Decimal(c), Decimal(c), Decimal(c), 1, None)
            for at, c in ((STARTED - timedelta(days=1), "8"), (STARTED, "10"), (T0, "11"))
        ],
    )
    r = client.get("/api/options/account")
    assert r.status_code == 200, r.text
    body = r.json()
    cash = Decimal("5000") + Decimal("50") - Decimal("0.99") - Decimal("100")
    value = Decimal("-45") + Decimal("120")  # the short put at the ask, the long call at the bid
    assert Decimal(body["cash"]) == cash
    assert Decimal(body["reserved"]) == Decimal("1450")
    assert Decimal(body["free_cash"]) == cash - Decimal("1450")
    assert Decimal(body["positions_value"]) == value
    assert Decimal(body["account_value"]) == cash + value
    assert Decimal(body["premium_collected"]) == Decimal("50")
    assert Decimal(body["realized_pnl"]) == Decimal("50")
    assert Decimal(body["unrealized_pnl"]) == Decimal("25")  # put +5, call +20
    assert Decimal(body["fees_total"]) == Decimal("0.99")
    assert (body["run_id"], body["started_at"], body["worker_beat_at"]) == (
        1,
        "2026-10-01T14:00:00Z",
        "2026-10-06T14:00:00Z",
    )
    assert Decimal(body["starting_cash"]) == Decimal("5000")
    assert (body["max_position_pct"], body["marks_complete"]) == ("0.50", True)

    by_source = {row["source"]: row for row in body["by_source"]}
    assert list(by_source) == ["manual", "toy_call"]
    manual, toy = by_source["manual"], by_source["toy_call"]
    assert (manual["open_structures"], toy["open_structures"]) == (1, 1)
    assert [
        Decimal(manual[k]) for k in ("reserved", "realized_pnl", "unrealized_pnl", "premium_collected")
    ] == [
        Decimal("1450"),
        Decimal("50"),
        Decimal("5"),
        Decimal("50"),
    ]
    assert [Decimal(toy[k]) for k in ("reserved", "realized_pnl", "unrealized_pnl", "premium_collected")] == [
        Decimal("0"),
        Decimal("0"),
        Decimal("20"),
        Decimal("0"),
    ]
    assert body["benchmark"] == {
        "ticker": "SOFI",
        "since": "2026-10-01",
        "benchmark_return": "0.100000",  # 10 on the day the run started, 11 today
        "account_return": str(((cash + value) / Decimal("5000") - 1).quantize(Decimal("0.000001"))),
    }


def test_positions_open_and_closed(db_factory: sessionmaker[Session]) -> None:
    client, fakes = _book(db_factory)
    r = client.get("/api/options/positions")
    assert r.status_code == 200, r.text
    csp, toy = r.json()["items"]
    assert (csp["source"], csp["kind"], csp["state"], csp["dte"]) == ("manual", "csp", "open", 45)
    assert Decimal(csp["unrealized_pnl"]) == Decimal("5") and Decimal(csp["reserved_cash"]) == Decimal("1450")
    (leg,) = csp["positions"]
    assert leg["contract"]["label"] == "F 2026-11-20 P 14.50" and leg["contract"]["dte"] == 45
    assert (leg["qty"], Decimal(leg["mark"]), Decimal(leg["delta"])) == (-1, Decimal("0.45"), Decimal("30"))
    assert (toy["source"], Decimal(toy["unrealized_pnl"])) == ("toy_call", Decimal("20"))

    # A position without a quote has no mark, and its structure no unrealized P&L.
    fakes.market.clear_quote(1)
    csp = client.get("/api/options/positions").json()["items"][0]
    assert csp["unrealized_pnl"] is None and csp["positions"][0]["mark"] is None

    r = client.get("/api/options/positions", params={"state": "closed"})
    (done,) = r.json()["items"]
    assert (done["state"], done["close_reason"], done["dte"], done["unrealized_pnl"]) == (
        "closed",
        "closed",
        None,
        None,
    )
    assert Decimal(done["realized_pnl"]) == Decimal("50") and done["positions"][0]["qty"] == 0
    assert client.get("/api/options/positions", params={"state": "all"}).status_code == 422
