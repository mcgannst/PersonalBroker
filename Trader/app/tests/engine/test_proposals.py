from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.strategies.fakes import FakeCatalysts
from trader.adapters.questrade.models import QtQuote
from trader.bootstrap import Core
from trader.broker.base import BrokerRejected
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import OrderSpec
from trader.config import EnvSettings
from trader.crypto import Crypto
from trader.db import models as m
from trader.engine.killswitch import KillSwitches, KillSwitchInputs
from trader.engine.orchestrator import build_engine
from trader.engine.proposals import DecisionResult, ProposalService
from trader.engine.risk import ProposalKind, SizedOrder
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import Cancel, EnterLong, Exit

pytestmark = pytest.mark.db
CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)
DAY = date(2026, 10, 6)


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
        mode, auto = s.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars().all()
    assert mode.action == "settings.set:approval_mode" and mode.after == {"value": "auto"}
    assert auto.action == "proposal.auto_approve" and auto.actor == "auto"
    assert auto.after == {"status": "submitted", "via": "auto", "order_id": p.order_id, "blocked": None}


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


# --- fix round 1 (attempt 2) ------------------------------------------------------------------------------
def guarded(env: Env) -> tuple[ProposalService, KillSwitches]:
    ks = KillSwitches(env.factory, env.clock)
    svc = ProposalService(
        env.factory, env.clock, env.store, env.broker, env.run_id, entry_blocked=ks.entry_guard()
    )
    return svc, ks


def test_an_entry_approved_while_paused_is_blocked(env: Env) -> None:
    """SPEC §6.3 / BR-41: /pause lands while an entry waits, so the later approval must not submit it."""
    svc, ks = guarded(env)
    p = svc.create(env.signal_id, entry_sized(env), "entry")
    ks.pause(env.run_id, DAY, actor="telegram")
    env.clock.set(T + timedelta(seconds=30))
    r = svc.decide(p.id, "approve", "telegram", "stephen")
    assert r == DecisionResult("rejected", False, None, "kill switch manual_pause is tripped")
    assert env.broker.working_orders() == []
    with env.factory() as s:
        row = s.get(m.Proposal, p.id)
        audit = s.execute(select(m.AuditLog).where(m.AuditLog.action == "proposal.approve")).scalar_one()
        msgs = s.execute(select(m.EventLog.message).where(m.EventLog.source == "proposals")).scalars().all()
    assert row is not None and row.status == "rejected" and row.decided_by == "stephen"
    assert row.error == "entry blocked: kill switch manual_pause is tripped"
    assert row.decision_latency_ms == 30_000
    assert audit.after["blocked"] == "kill switch manual_pause is tripped"
    assert audit.after["status"] == "rejected"
    assert any("approved via telegram: rejected" in msg for msg in msgs)


def test_the_decision_log_reads_approved_and_rejected(env: Env) -> None:
    a = env.svc.create(env.signal_id, entry_sized(env), "entry")
    b = env.svc.create(env.signal_id, entry_sized(env), "entry")
    env.svc.decide(a.id, "approve", "web", "stephen")
    env.svc.decide(b.id, "reject", "web", "stephen")
    with env.factory() as s:
        msgs = s.execute(select(m.EventLog.message).where(m.EventLog.source == "proposals")).scalars().all()
    assert f"proposal {a.id} approved via web: submitted" in msgs
    assert f"proposal {b.id} rejected via web: rejected" in msgs
    assert not any("rejectd" in msg for msg in msgs)


def test_an_entry_approved_after_the_daily_loss_trips_is_blocked(env: Env) -> None:
    svc, ks = guarded(env)
    p = svc.create(env.signal_id, entry_sized(env), "entry")
    losing = KillSwitchInputs(Decimal("720"), Decimal("680"), Decimal("720"), 0, None)
    assert ks.evaluate(env.run_id, DAY, losing, RuntimeSettings()) == ["daily_loss_pct"]
    r = svc.decide(p.id, "approve", "web", "stephen")
    assert (r.status, r.order_id, r.blocked) == ("rejected", None, "kill switch daily_loss_pct is tripped")
    assert env.broker.working_orders() == []


def test_exits_stops_and_cancels_are_never_blocked_by_the_guard(env: Env) -> None:
    """Review Focus 5: a pause must not trap an open position."""
    svc, ks = guarded(env)
    pid = open_position(env)
    entry_order = env.broker.submit(entry_sized(env).spec)  # type: ignore[arg-type]
    ks.pause(env.run_id, DAY, actor="telegram")
    stop = svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    assert svc.decide(stop.id, "approve", "telegram", "stephen").status == "submitted"
    cancel = SizedOrder(Cancel(entry_order, "x"), "cancel", 10, None, cancel_order_id=entry_order)
    c = svc.create(env.signal_id, cancel, "cancel")
    assert svc.decide(c.id, "approve", "telegram", "stephen").status == "submitted"
    flat = svc.create(env.signal_id, exit_sized(env, pid), "exit")
    r = svc.decide(flat.id, "approve", "telegram", "stephen")
    assert (r.status, r.blocked) == ("submitted", None) and r.order_id is not None


def test_auto_mode_does_not_submit_an_entry_while_paused(env: Env) -> None:
    svc, ks = guarded(env)
    env.svc.set_approval_mode("auto", actor="stephen")
    ks.pause(env.run_id, DAY, actor="telegram")
    p = svc.create(env.signal_id, entry_sized(env), "entry")
    assert p.status == "rejected" and p.order_id is None and p.decided_via == "auto"
    assert p.error == "entry blocked: kill switch manual_pause is tripped"
    assert env.broker.working_orders() == []
    with env.factory() as s:
        audit = s.execute(select(m.AuditLog).where(m.AuditLog.action == "proposal.auto_approve")).scalar_one()
    assert audit.after["blocked"] == "kill switch manual_pause is tripped"


async def test_build_engine_wires_the_entry_guard(
    env: Env, db_factory: sessionmaker[Session], migrated_engine: SqlEngine
) -> None:
    key = Fernet.generate_key().decode()
    cfg = EnvSettings(
        database_url="postgresql+psycopg://unused",
        migration_database_url="postgresql+psycopg://unused",
        app_encryption_key=key,
        session_secret="test-session-secret",
    )
    core = Core(cfg, migrated_engine, db_factory, Crypto(key), env.clock, CAL, env.store)
    engine = build_engine(core, FakeQuestrade(), FakeCatalysts())
    assert engine.run_id == env.run_id
    p = engine.proposals.create(env.signal_id, entry_sized(env), "entry")
    engine.killswitches.pause(env.run_id, DAY, actor="telegram")
    r = engine.proposals.decide(p.id, "approve", "web", "stephen")
    assert (r.status, r.blocked) == ("rejected", "kill switch manual_pause is tripped")


@pytest.mark.parametrize("kind", ["entry", "exit", "stop"])
def test_create_needs_an_order_spec_unless_it_is_a_cancel(env: Env, kind: ProposalKind) -> None:
    sized = SizedOrder(Exit(1, "market", None, "x"), kind, 10, None, position_id=1)
    with pytest.raises(ValueError, match="order spec"):
        env.svc.create(env.signal_id, sized, kind)


class RejectAfterWriting:
    """A broker that writes, then refuses: the savepoint must undo the write."""

    def __init__(self, inner: SimBroker) -> None:
        self.inner = inner

    def submit(self, spec: OrderSpec, session: Session | None = None) -> int:
        self.inner.submit(spec, session=session)
        raise BrokerRejected("refused after writing")

    def cancel(self, order_id: int, reason: str, session: Session | None = None) -> bool:
        self.inner.cancel(order_id, reason, session=session)
        raise BrokerRejected("refused after cancelling")


def test_a_broker_refusal_leaves_no_half_written_broker_rows(env: Env) -> None:
    broker: Any = RejectAfterWriting(env.broker)
    svc = ProposalService(env.factory, env.clock, env.store, broker, env.run_id)
    p = svc.create(env.signal_id, entry_sized(env), "entry")
    r = svc.decide(p.id, "approve", "web", "stephen")
    assert (r.status, r.order_id) == ("failed", None)
    order_id = env.broker.submit(entry_sized(env).spec)  # type: ignore[arg-type]
    sized = SizedOrder(Cancel(order_id, "x"), "cancel", 10, None, cancel_order_id=order_id)
    c = svc.create(env.signal_id, sized, "cancel")
    assert svc.decide(c.id, "approve", "web", "stephen").status == "failed"
    with env.factory() as s:
        orders = s.execute(select(m.Order)).scalars().all()
        row = s.get(m.Proposal, p.id)
        audits = s.execute(select(m.AuditLog.action)).scalars().all()
    assert [(o.id, o.status) for o in orders] == [(order_id, "working")]  # the cancel was undone too
    assert row is not None and row.status == "failed" and row.error == "refused after writing"
    assert audits == ["proposal.approve", "proposal.approve"]  # the decisions themselves were kept


def test_escalation_stops_for_a_protected_position(env: Env) -> None:
    pid = open_position(env)
    stop = env.svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    env.svc.expire_due(T + timedelta(seconds=180))
    env.broker.submit(
        OrderSpec(env.sym, "sell", "stop", 10, stop=Decimal("19.00"), purpose="stop", position_id=pid)
    )
    assert env.svc.escalate_unprotected(T + timedelta(seconds=240)) == []
    with env.factory() as s:
        row = s.get(m.Proposal, stop.id)
    assert row is not None and row.escalated_at is None  # cleared: never selected again
    assert env.svc.escalate_unprotected(T + timedelta(seconds=900)) == []


def test_escalation_alerts_once_per_position_per_tick(env: Env) -> None:
    pid = open_position(env)
    first = env.svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    env.clock.set(T + timedelta(seconds=20))
    second = env.svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    env.svc.expire_due(T + timedelta(seconds=200))  # both expire in the same sweep
    (alert,) = env.svc.escalate_unprotected(T + timedelta(seconds=260))
    assert alert.id == second.id != first.id and alert.escalations == 2
    assert env.svc.escalate_unprotected(T + timedelta(seconds=300)) == []  # the cadence is per position
    assert len(env.svc.escalate_unprotected(T + timedelta(seconds=320))) == 1
    with env.factory() as s:
        msgs = (
            s.execute(select(m.EventLog.message).where(m.EventLog.message.like("position % unprotected%")))
            .scalars()
            .all()
        )
    assert len(msgs) == 2


def test_an_auto_executed_expiry_is_audited(env: Env) -> None:
    pid = open_position(env)
    flat = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    with env.factory() as s:
        audit = s.execute(select(m.AuditLog)).scalar_one()
    assert audit.action == "proposal.auto_execute_on_expiry:exit"
    assert audit.actor == "auto_flatten_on_expiry"
    assert audit.before == {"proposal_id": flat.id, "status": "pending"}
    assert audit.after == {"status": "submitted", "via": "auto", "order_id": expired.order_id, "error": None}
    assert expired.decision_latency_ms is None  # nobody decided


def test_expire_due_locks_every_position_by_id_before_any_order(env: Env) -> None:
    """P2-REVIEW: the sweep must lock positions in id order up front (like SimBroker.on_quotes), not one
    by one in proposal-id order, or two auto-flattens could deadlock with a concurrent quote batch."""
    pid = open_position(env)
    env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    engine = env.factory.kw["bind"]
    seen: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "FOR UPDATE" in statement:
            seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert expired.status == "submitted"
    first_position = next(i for i, sql in enumerate(seen) if "FROM trader.positions" in sql)
    assert "ORDER BY trader.positions.id" in seen[first_position]
    assert all("FROM trader.orders" not in sql for sql in seen[:first_position])
