"""P4-T6 acceptance tests 1-7: web approve/reject through the SAME guarded decider as Telegram
(`trader.runtime.build_decider`, wrapped by `tests.fakes_api.RecordingDeciderFor`), on a real database.

Review Focus 1: a web approval must never bypass the kill switches, and a web tap racing a Telegram tap (or
another web tap) must give exactly one decision and one order.
"""

import ast
import dataclasses
import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import trader.api.routers.proposals as proposals_module
from tests.api.conftest import make_client
from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_api import RecordingDeciderFor, make_services, test_core
from trader import runtime
from trader.adapters.questrade.models import QtQuote
from trader.api.deps import ApiServices
from trader.api.routers.proposals import router
from trader.bootstrap import Core
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.proposals import Decision, DecisionResult, ProposalService, Via
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.market.clock import ET, FixedClock
from trader.settings_store import RuntimeSettings
from trader.strategies.base import EnterLong, Exit

pytestmark = pytest.mark.db

DAY = date(2026, 10, 6)  # a Tuesday session


def et(h: int, mi: int, s: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, mi, s, tzinfo=ET).astimezone(UTC)


# --- a small trading world ----------------------------------------------------------------------------------


@dataclass
class World:
    core: Core
    clock: FixedClock
    run_id: int
    sym: int
    cfg: int
    signal_id: int
    svc: ProposalService  # the engine's own service (no guard needed to CREATE proposals)

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory


def _signal(s: Session, run_id: int, sym: int, cfg: int, clock: FixedClock, day: date = DAY) -> int:
    sig = m.Signal(
        run_id=run_id,
        strategy_config_id=cfg,
        symbol_id=sym,
        session_date=day,
        event_key="orb_open",
        ts=clock.now(),
        intent={"reason": "orb_breakout"},
        evidence={},
    )
    s.add(sig)
    s.flush()
    return sig.id


@pytest.fixture
def world(db_factory: sessionmaker[Session]) -> World:
    clock = FixedClock(et(9, 40))
    core = test_core(db_factory, clock)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        signal_id = _signal(s, run.id, sym, cfg, clock)
        s.commit()
    broker = runtime.build_sim_broker(core, run.id, RuntimeSettings())
    svc = ProposalService(db_factory, clock, core.settings, broker, run.id)
    return World(core, clock, run.id, sym, cfg, signal_id, svc)


def new_entry(w: World, svc: ProposalService | None = None, signal_id: int | None = None) -> int:
    intent = EnterLong(w.sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
    spec = OrderSpec(
        w.sym, "buy", "stop", 10, stop=Decimal("20.01"), stop_loss=Decimal("19.91"),
        strategy_config_id=w.cfg, reason="orb_breakout",
    )  # fmt: skip
    sized = SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10", "per_share_risk": "0.10"})
    return (svc or w.svc).create(signal_id or w.signal_id, sized, "entry").id


def open_position(w: World) -> int:
    broker = runtime.build_sim_broker(w.core, w.run_id, RuntimeSettings())
    broker.submit(OrderSpec(w.sym, "buy", "market", 10, stop_loss=Decimal("19.00"), strategy_config_id=w.cfg))
    now = w.clock.now()
    q = QtQuote(
        w.sym, "AAA", Decimal("20.00"), Decimal("20.01"), Decimal("20.00"), None, 1000,
        now - timedelta(seconds=1), 0, False, None,
    )  # fmt: skip
    (fill,) = broker.on_quotes([q], now)
    assert fill.position_id is not None
    return fill.position_id


def new_stop(w: World, position_id: int) -> int:
    spec = OrderSpec(
        w.sym, "sell", "stop", 10, stop=Decimal("19.00"), purpose="stop", position_id=position_id,
        reason="protective_stop",
    )  # fmt: skip
    sized = SizedOrder(
        Exit(position_id, "stop", Decimal("19.00"), "protective_stop"),
        "stop",
        10,
        spec,
        position_id=position_id,
    )
    return w.svc.create(w.signal_id, sized, "stop").id


def orders(w: World, proposal_id: int | None = None) -> list[m.Order]:
    q = select(m.Order).where(m.Order.run_id == w.run_id)
    if proposal_id is not None:
        q = q.where(m.Order.proposal_id == proposal_id)
    with w.factory() as s:
        return list(s.execute(q.order_by(m.Order.id)).scalars())


def proposal_row(w: World, proposal_id: int) -> m.Proposal:
    with w.factory() as s:
        return s.get_one(m.Proposal, proposal_id)


def trip(w: World, switch: str) -> None:
    with w.factory() as s:
        s.add(m.KillSwitchEvent(run_id=w.run_id, switch=switch, session_date=DAY, tripped_at=w.clock.now()))
        s.commit()


def client_for(w: World, **overrides: object) -> tuple[TestClient, ApiServices]:
    services = make_services(w.core, **overrides)
    return make_client(services, router), services


# --- 1. approve on the web ---------------------------------------------------------------------------------


def test_web_approve_submits_the_entry_with_the_web_actor(world: World) -> None:
    pid = new_entry(world)
    world.clock.advance(timedelta(seconds=30))
    client, services = client_for(world)

    r = client.post(f"/api/proposals/{pid}/approve")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_decided"] is False and body["blocked"] is None
    assert body["message"] == "Approved"
    out = body["proposal"]
    assert out["id"] == pid and out["status"] == "submitted"
    assert out["decided_via"] == "web" and out["decided_by"] == "web:stephen"
    assert out["decided_at"] == "2026-10-06T13:40:30Z" and out["decision_latency_ms"] == 30000
    (order,) = orders(world, pid)
    assert out["order_id"] == order.id and order.purpose == "entry"
    p = proposal_row(world, pid)
    assert (p.status, p.decided_via, p.decided_by) == ("submitted", "web", "web:stephen")
    with world.factory() as s:
        audits = list(s.execute(select(m.AuditLog).where(m.AuditLog.action.like("proposal.%"))).scalars())
    assert [(a.actor, a.action) for a in audits] == [("web:stephen", "proposal.approve")]
    assert audits[0].after["via"] == "web" and audits[0].after["order_id"] == order.id
    assert isinstance(services.decider_for, RecordingDeciderFor)
    assert services.decider_for.calls == [(world.run_id, pid, "approve", "web", "web:stephen")]


def test_web_reject_records_the_decision_and_places_no_order(world: World) -> None:
    pid = new_entry(world)
    client, _ = client_for(world)
    r = client.post(f"/api/proposals/{pid}/reject")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"] == "Rejected" and body["already_decided"] is False
    assert body["proposal"]["status"] == "rejected" and body["proposal"]["decided_by"] == "web:stephen"
    assert orders(world, pid) == []


# --- 2. safety: the kill switches ---------------------------------------------------------------------------


@pytest.mark.parametrize("switch", ["manual_pause", "max_drawdown_pct"])
def test_entry_approved_on_the_web_is_blocked_by_a_kill_switch(world: World, switch: str) -> None:
    position_id = open_position(world)
    entry = new_entry(world)
    stop = new_stop(world, position_id)
    if switch == "manual_pause":
        assert KillSwitches(world.factory, world.clock).pause(world.run_id, DAY, "telegram")
    else:
        trip(world, switch)
    before = len(orders(world))
    client, _ = client_for(world)

    r = client.post(f"/api/proposals/{entry}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    reason = f"kill switch {switch} is tripped"
    assert body["blocked"] == reason
    assert body["message"] == f"Entry blocked: {reason}"
    assert body["already_decided"] is False
    assert body["proposal"]["status"] == "rejected" and body["proposal"]["order_id"] is None
    assert body["proposal"]["error"] == f"entry blocked: {reason}"
    assert len(orders(world)) == before and orders(world, entry) == []

    # a protective stop still goes through while the switch is tripped
    r = client.post(f"/api/proposals/{stop}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "Approved" and r.json()["proposal"]["status"] == "submitted"
    (stop_order,) = orders(world, stop)
    assert stop_order.purpose == "stop" and stop_order.status == "working"


# --- 3. one decision only -----------------------------------------------------------------------------------


def test_web_approve_then_telegram_tap_is_already_decided(world: World) -> None:
    pid = new_entry(world)
    client, _ = client_for(world)
    assert client.post(f"/api/proposals/{pid}/approve").json()["message"] == "Approved"

    telegram = runtime.build_decider(world.core, world.run_id)  # the bot's decider
    result = telegram(pid, "approve", "telegram", "telegram:4242")
    assert result.already_decided is True and result.status == "submitted"
    assert len(orders(world, pid)) == 1
    assert proposal_row(world, pid).decided_via == "web"


def test_telegram_tap_then_web_approve_says_already_submitted(world: World) -> None:
    pid = new_entry(world)
    runtime.build_decider(world.core, world.run_id)(pid, "approve", "telegram", "telegram:4242")
    client, _ = client_for(world)
    r = client.post(f"/api/proposals/{pid}/reject")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_decided"] is True and body["message"] == "Already submitted"
    assert body["proposal"]["decided_via"] == "telegram"
    assert len(orders(world, pid)) == 1


def test_web_approve_racing_a_telegram_tap_gives_one_decision(world: World) -> None:
    """Both taps reach ProposalService.decide at the same moment (a barrier); the row lock lets one win."""
    pid = new_entry(world)
    barrier = threading.Barrier(2, timeout=10)
    recording = RecordingDeciderFor(world.core)

    def gated_for(run_id: int) -> Callable[[int, Decision, Via, str], DecisionResult]:
        inner = recording(run_id)

        def decide(proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
            barrier.wait()
            return inner(proposal_id, decision, via, actor)

        return decide

    client, _ = client_for(world, decider_for=gated_for)
    telegram = runtime.build_decider(world.core, world.run_id)
    results: dict[str, object] = {}

    def web() -> None:
        results["web"] = client.post(f"/api/proposals/{pid}/approve").json()

    def tap() -> None:
        barrier.wait()
        results["telegram"] = telegram(pid, "approve", "telegram", "telegram:4242")

    threads = [threading.Thread(target=web), threading.Thread(target=tap)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    web_body = results["web"]
    tg = results["telegram"]
    assert isinstance(web_body, dict) and isinstance(tg, DecisionResult)
    assert [web_body["already_decided"], tg.already_decided].count(True) == 1
    assert len(orders(world, pid)) == 1
    with world.factory() as s:
        n_audits = len(
            list(s.execute(select(m.AuditLog).where(m.AuditLog.action == "proposal.approve")).scalars())
        )
    assert n_audits == 1


def test_two_concurrent_web_approvals_give_one_order(world: World) -> None:
    pid = new_entry(world)
    barrier = threading.Barrier(2, timeout=10)
    recording = RecordingDeciderFor(world.core)

    def gated_for(run_id: int) -> Callable[[int, Decision, Via, str], DecisionResult]:
        inner = recording(run_id)

        def decide(proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
            barrier.wait()
            return inner(proposal_id, decision, via, actor)

        return decide

    client, _ = client_for(world, decider_for=gated_for)
    bodies: list[dict[str, object]] = []
    lock = threading.Lock()

    def approve() -> None:
        body = client.post(f"/api/proposals/{pid}/approve").json()
        with lock:
            bodies.append(body)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(bodies) == 2
    assert sorted(bool(b["already_decided"]) for b in bodies) == [False, True]
    messages = sorted(str(b["message"]) for b in bodies)
    assert messages == ["Already submitted", "Approved"]
    assert len(orders(world, pid)) == 1
    assert len(recording.calls) == 2


# --- 4. a stale page ----------------------------------------------------------------------------------------


def test_approving_after_expiry_is_already_expired(world: World) -> None:
    pid = new_entry(world)
    world.clock.set(proposal_row(world, pid).expires_at + timedelta(seconds=1))
    client, _ = client_for(world)
    r = client.post(f"/api/proposals/{pid}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_decided"] is True and body["message"] == "Already expired"
    assert body["proposal"]["status"] == "expired"
    assert orders(world, pid) == []


def test_auto_approved_is_reported_as_auto_approved(world: World) -> None:
    world.core.settings.set("approval_mode", "auto", "test")
    pid = new_entry(world)
    assert proposal_row(world, pid).status == "submitted"
    with world.factory() as s:  # an auto-approved row still waiting to be submitted
        s.get_one(m.Proposal, pid).status = "auto_approved"
        s.commit()
    client, _ = client_for(world)
    body = client.post(f"/api/proposals/{pid}/approve").json()
    assert body["already_decided"] is True and body["message"] == "Already auto-approved"


# --- 5. not found and not signed in -------------------------------------------------------------------------


def test_unknown_and_other_run_proposals_are_404(world: World) -> None:
    with world.factory() as s:
        other_run = add_run(s, mode="replay", status="completed")
        other_signal = _signal(s, other_run, world.sym, world.cfg, world.clock)
        s.commit()
    broker = runtime.build_sim_broker(world.core, other_run, RuntimeSettings())
    other_svc = ProposalService(world.factory, world.clock, world.core.settings, broker, other_run)
    other = new_entry(world, other_svc, other_signal)
    client, _ = client_for(world)
    for path in (
        "/api/proposals/999999/approve",
        "/api/proposals/999999/reject",
        f"/api/proposals/{other}/approve",
        f"/api/proposals/{other}/reject",
        "/api/proposals/999999",
        f"/api/proposals/{other}",
    ):
        r = client.post(path) if path.endswith(("approve", "reject")) else client.get(path)
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "not_found"
    assert proposal_row(world, other).status == "pending"


def test_without_a_session_every_route_is_401(world: World) -> None:
    pid = new_entry(world)
    client = make_client(make_services(world.core), router, user=None)
    assert client.get("/api/proposals").status_code == 401
    assert client.get(f"/api/proposals/{pid}").status_code == 401
    assert client.post(f"/api/proposals/{pid}/approve").status_code == 401
    assert client.post(f"/api/proposals/{pid}/reject").status_code == 401
    assert proposal_row(world, pid).status == "pending"


# --- 6. reading proposals -----------------------------------------------------------------------------------


def test_pending_list_is_the_live_runs_pending_oldest_first(world: World) -> None:
    first = new_entry(world)
    world.clock.advance(timedelta(seconds=5))
    second = new_entry(world)
    world.clock.advance(timedelta(seconds=5))
    decided = new_entry(world)
    runtime.build_decider(world.core, world.run_id)(decided, "reject", "telegram", "telegram:4242")
    with world.factory() as s:  # a pending proposal of another run is never listed
        other_run = add_run(s, mode="replay", status="completed")
        other_signal = _signal(s, other_run, world.sym, world.cfg, world.clock)
        s.commit()
    other_svc = ProposalService(
        world.factory,
        world.clock,
        world.core.settings,
        runtime.build_sim_broker(world.core, other_run, RuntimeSettings()),
        other_run,
    )
    new_entry(world, other_svc, other_signal)
    client, _ = client_for(world)

    r = client.get("/api/proposals", params={"status": "pending"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [p["id"] for p in items] == [first, second]
    assert all(p["status"] == "pending" and p["ticker"] == "AAA" for p in items)
    assert items[0]["stop"] == "20.01" and items[0]["stop_loss"] == "19.91"

    everything = client.get("/api/proposals", params={"status": "all"}).json()["items"]
    assert {p["id"] for p in everything} == {first, second, decided}
    assert everything[0]["id"] == decided  # history: newest first
    assert [
        p["id"] for p in client.get("/api/proposals", params={"status": "all", "limit": 1}).json()["items"]
    ] == [decided]
    assert client.get("/api/proposals", params={"status": "all", "date": "2026-10-07"}).json()["items"] == []
    assert (
        len(client.get("/api/proposals", params={"status": "all", "date": "2026-10-06"}).json()["items"]) == 3
    )
    assert client.get("/api/proposals", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/proposals", params={"limit": 0}).status_code == 422


def test_get_one_decided_proposal_has_its_decision(world: World) -> None:
    pid = new_entry(world)
    world.clock.advance(timedelta(seconds=12))
    client, _ = client_for(world)
    client.post(f"/api/proposals/{pid}/reject")
    r = client.get(f"/api/proposals/{pid}")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["decided_at"] == "2026-10-06T13:40:12Z" and out["decided_by"] == "web:stephen"
    assert out["decided_via"] == "web" and out["status"] == "rejected" and out["reason"] == "orb_breakout"


# --- 7. the only decision path ------------------------------------------------------------------------------


def test_router_module_never_builds_a_proposal_service() -> None:
    tree = ast.parse(inspect.getsource(proposals_module))
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("ProposalService", "build_decider", "build_sim_broker", "SimBroker"):
        assert forbidden not in imported and forbidden not in names and forbidden not in attrs, forbidden
    assert not hasattr(proposals_module, "ProposalService")


def test_decider_for_is_called_with_the_live_run_id(world: World) -> None:
    pid = new_entry(world)
    client, services = client_for(world)
    client.post(f"/api/proposals/{pid}/reject")
    assert isinstance(services.decider_for, RecordingDeciderFor)
    live = get_live_run(world.factory, world.clock, RuntimeSettings()).id
    assert services.decider_for.run_ids == [live] == [world.run_id]


def test_a_decider_error_is_not_swallowed(world: World) -> None:
    pid = new_entry(world)

    def broken_for(run_id: int) -> Callable[[int, Decision, Via, str], DecisionResult]:
        def decide(proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
            raise RuntimeError("database gone")

        return decide

    services = dataclasses.replace(make_services(world.core), decider_for=broken_for)
    client = make_client(services, router, raise_server_exceptions=False)
    r = client.post(f"/api/proposals/{pid}/approve")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal"
