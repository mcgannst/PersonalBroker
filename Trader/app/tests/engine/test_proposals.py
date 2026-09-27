from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.engine.proposals import ProposalService
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import Cancel, EnterLong, Exit

pytestmark = pytest.mark.db
CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)


@dataclass
class Env:
    svc: ProposalService
    broker: SimBroker
    store: SettingsStore
    clock: FixedClock
    factory: sessionmaker[Session]
    run_id: int
    sym: int
    cfg: int
    signal_id: int


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        sig = m.Signal(
            run_id=run.id,
            strategy_config_id=cfg,
            symbol_id=sym,
            session_date=T.date(),
            event_key="orb_open",
            ts=T,
            intent={},
            evidence={},
        )
        s.add(sig)
        s.commit()
        signal_id = sig.id
    store = SettingsStore(db_factory, now=clock.now)
    broker = SimBroker(db_factory, clock, Ledger(CAL), QuoteFillModel(FillParams()), run.id)
    return Env(
        ProposalService(db_factory, clock, store, broker, run.id),
        broker,
        store,
        clock,
        db_factory,
        run.id,
        sym,
        cfg,
        signal_id,
    )


def entry_sized(env: Env) -> SizedOrder:
    intent = EnterLong(env.sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
    spec = OrderSpec(
        env.sym,
        "buy",
        "stop",
        10,
        stop=Decimal("20.01"),
        stop_loss=Decimal("19.91"),
        strategy_config_id=env.cfg,
        reason="orb_breakout",
    )
    return SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10"})


def open_position(env: Env) -> int:
    env.broker.submit(
        OrderSpec(env.sym, "buy", "market", 10, stop_loss=Decimal("19.00"), strategy_config_id=env.cfg)
    )
    q = QtQuote(
        env.sym,
        "AAA",
        Decimal("20.00"),
        Decimal("20.01"),
        Decimal("20.00"),
        None,
        1000,
        env.clock.now() - timedelta(seconds=1),
        0,
        False,
        None,
    )
    (ev,) = env.broker.on_quotes([q], env.clock.now())
    return ev.position_id


def exit_sized(env: Env, pid: int, order_type: Literal["market", "stop"] = "market") -> SizedOrder:
    kind: Literal["stop", "exit"] = "stop" if order_type == "stop" else "exit"
    stop = Decimal("19.00") if order_type == "stop" else None
    spec = OrderSpec(
        env.sym,
        "sell",
        order_type,
        10,
        stop=stop,
        purpose=kind,
        position_id=pid,
        reason="protective_stop" if kind == "stop" else "flatten_close",
    )
    return SizedOrder(Exit(pid, order_type, stop, spec.reason), kind, 10, spec, position_id=pid)


def test_a_manual_entry_waits_as_pending(env: Env) -> None:
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    assert p.status == "pending" and p.expires_at == T + timedelta(minutes=5) and p.order_id is None
    assert p.order_spec["stop"] == "20.01" and p.sizing == {"shares": "10"}
    assert env.broker.working_orders() == []


def test_approve_submits_the_order_and_records_the_latency(env: Env) -> None:
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    env.clock.set(T + timedelta(seconds=42))
    r = env.svc.decide(p.id, "approve", "telegram", "stephen")
    assert (r.status, r.already_decided) == ("submitted", False) and r.order_id is not None
    (order,) = env.broker.working_orders()
    assert order.id == r.order_id and order.proposal_id == p.id
    with env.factory() as s:
        row = s.get(m.Proposal, p.id)
        audit = s.execute(select(m.AuditLog)).scalar_one()
    assert row is not None and row.decided_via == "telegram" and row.decided_by == "stephen"
    assert row.decision_latency_ms == 42_000 and row.decided_at == T + timedelta(seconds=42)
    assert audit.action == "proposal.approve" and audit.actor == "stephen"


def test_reject_places_nothing(env: Env) -> None:
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    r = env.svc.decide(p.id, "reject", "web", "stephen")
    assert (r.status, r.already_decided, r.order_id) == ("rejected", False, None)
    assert env.broker.working_orders() == []


def test_the_second_decision_gets_already_decided(env: Env) -> None:
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    env.svc.decide(p.id, "approve", "telegram", "stephen")
    again = env.svc.decide(p.id, "reject", "web", "stephen")
    assert (again.status, again.already_decided) == ("submitted", True)
    assert len(env.broker.working_orders()) == 1


def test_concurrent_decisions_first_wins(env: Env) -> None:
    """Review Focus 2: Telegram and the web at the same moment give one decision and at most one order."""
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    calls = [("approve", "telegram"), ("reject", "web"), ("approve", "web"), ("approve", "telegram")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda c: env.svc.decide(p.id, c[0], c[1], "stephen"), calls))  # type: ignore[arg-type]
    assert sum(not r.already_decided for r in results) == 1
    assert len({r.status for r in results}) == 1
    assert len(env.broker.working_orders()) == (1 if results[0].status == "submitted" else 0)


def test_decide_after_expiry_is_already_decided(env: Env) -> None:
    """Review Focus 2: a tap after the TTL expires the proposal instead of submitting it."""
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    env.clock.set(T + timedelta(minutes=5))
    r = env.svc.decide(p.id, "approve", "telegram", "stephen")
    assert (r.status, r.already_decided) == ("expired", True)
    assert env.broker.working_orders() == []


def test_expiry_follows_the_kind_and_ttl(env: Env) -> None:
    pid = open_position(env)
    entry = env.svc.create(env.signal_id, entry_sized(env), "entry")
    stop = env.svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    assert stop.expires_at == T + timedelta(minutes=3)
    assert env.svc.expire_due(T + timedelta(seconds=179)) == []
    assert [x.id for x in env.svc.expire_due(T + timedelta(seconds=180))] == [stop.id]
    assert [x.id for x in env.svc.expire_due(T + timedelta(seconds=300))] == [entry.id]
    with env.factory() as s:
        statuses = {r.id: r.status for r in s.execute(select(m.Proposal)).scalars()}
    assert statuses == {entry.id: "expired", stop.id: "expired"}


def test_auto_mode_approves_at_once_and_the_mode_change_is_audited(env: Env) -> None:
    env.svc.set_approval_mode("auto", actor="stephen")
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    assert p.status == "submitted" and p.decided_via == "auto" and p.decision_latency_ms == 0
    assert p.order_id is not None and len(env.broker.working_orders()) == 1
    with env.factory() as s:
        audit = s.execute(select(m.AuditLog)).scalar_one()
    assert audit.action == "settings.set:approval_mode" and audit.after == {"value": "auto"}


def test_an_expired_protective_stop_escalates_and_counts_unprotected_time(env: Env) -> None:
    pid = open_position(env)  # opened at T, unprotected from then
    stop = env.svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    env.svc.expire_due(T + timedelta(seconds=180))
    assert env.svc.escalate_unprotected(T + timedelta(seconds=200)) == []  # 60 s between alerts
    (again,) = env.svc.escalate_unprotected(T + timedelta(seconds=240))
    assert again.id == stop.id and again.escalations == 2
    with env.factory() as s:
        errors = s.execute(select(m.EventLog.message).where(m.EventLog.level == "error")).scalars().all()
    assert len(errors) == 2 and all("unprotected" in e or "no stop" in e for e in errors)
    env.clock.set(T + timedelta(seconds=600))
    flat = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    env.svc.decide(flat.id, "approve", "telegram", "stephen")
    q = QtQuote(
        env.sym,
        "AAA",
        Decimal("20.10"),
        Decimal("20.11"),
        Decimal("20.10"),
        None,
        1000,
        env.clock.now() - timedelta(seconds=1),
        0,
        False,
        None,
    )
    env.broker.on_quotes([q], env.clock.now())
    with env.factory() as s:
        pos = s.get(m.Position, pid)
    assert pos is not None and pos.closed_at is not None and pos.unprotected_seconds == 600
    assert env.svc.escalate_unprotected(T + timedelta(seconds=900)) == []  # closed: no more alerts


def test_expired_flatten_auto_submits(env: Env) -> None:
    """Review Focus 4: an unanswered flatten must not leave the position open overnight."""
    pid = open_position(env)
    flat = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    assert expired.id == flat.id and expired.status == "submitted"
    assert expired.decided_via == "auto" and expired.decided_by == "auto_flatten_on_expiry"
    assert expired.expired_at == T + timedelta(minutes=5) and expired.order_id is not None
    assert [o.id for o in env.broker.working_orders()] == [expired.order_id]


def test_expired_flatten_escalates_when_auto_flatten_is_off(env: Env) -> None:
    env.store.set("auto_flatten_on_expiry", False, actor="stephen")
    pid = open_position(env)
    env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    assert expired.status == "expired" and env.broker.working_orders() == []
    with env.factory() as s:
        levels = s.execute(select(m.EventLog.level).where(m.EventLog.source == "proposals")).scalars().all()
    assert "error" in levels


def test_an_entry_that_would_fill_late_is_cancelled_instead(env: Env) -> None:
    """Review Focus 4 (BR-42): an unanswered cancel of a working entry executes on expiry, like a flatten."""
    order_id = env.broker.submit(entry_sized(env).spec)  # type: ignore[arg-type]
    sized = SizedOrder(Cancel(order_id, "entry_cancel_at"), "cancel", 10, None, cancel_order_id=order_id)
    p = env.svc.create(env.signal_id, sized, "cancel")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    assert (
        expired.id == p.id
        and expired.status == "submitted"
        and expired.expired_at == T + timedelta(minutes=5)
    )
    assert expired.decided_via == "auto" and expired.decided_by == "auto_flatten_on_expiry"
    assert env.broker.working_orders() == []  # the entry can no longer fill late
    with env.factory() as s:
        order = s.get(m.Order, order_id)
    assert order is not None and order.status == "cancelled" and order.cancel_reason == "entry_cancel_at"


def test_an_expired_cancel_just_expires_when_auto_flatten_is_off(env: Env) -> None:
    env.store.set("auto_flatten_on_expiry", False, actor="stephen")
    order_id = env.broker.submit(entry_sized(env).spec)  # type: ignore[arg-type]
    sized = SizedOrder(Cancel(order_id, "entry_cancel_at"), "cancel", 10, None, cancel_order_id=order_id)
    env.svc.create(env.signal_id, sized, "cancel")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    assert expired.status == "expired" and [o.id for o in env.broker.working_orders()] == [order_id]


def test_a_cancel_proposal_cancels_the_order(env: Env) -> None:
    order_id = env.broker.submit(entry_sized(env).spec)  # type: ignore[arg-type]
    sized = SizedOrder(Cancel(order_id, "entry_cancel_at"), "cancel", 10, None, cancel_order_id=order_id)
    p = env.svc.create(env.signal_id, sized, "cancel")
    assert p.order_spec == {"cancel_order_id": order_id, "reason": "entry_cancel_at"}
    assert env.svc.decide(p.id, "approve", "web", "stephen").status == "submitted"
    assert env.broker.working_orders() == []


def test_a_broker_refusal_marks_the_proposal_failed(env: Env) -> None:
    pid = open_position(env)
    first = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    second = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    env.svc.decide(first.id, "approve", "web", "stephen")
    q = QtQuote(
        env.sym,
        "AAA",
        Decimal("20.10"),
        Decimal("20.11"),
        Decimal("20.10"),
        None,
        1000,
        env.clock.now() - timedelta(seconds=1),
        0,
        False,
        None,
    )
    env.broker.on_quotes([q], env.clock.now())  # the position is now closed
    r = env.svc.decide(second.id, "approve", "web", "stephen")
    assert (r.status, r.already_decided, r.order_id) == ("failed", False, None)
    with env.factory() as s:
        row = s.get(m.Proposal, second.id)
    assert row is not None and row.error is not None and "not open" in row.error


def test_kind_must_match_the_sized_order(env: Env) -> None:
    with pytest.raises(ValueError):
        env.svc.create(env.signal_id, entry_sized(env), "exit")
