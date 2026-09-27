"""P5-T4: ProposalService(audit_auto=False) skips only the automatic audit rows (replay)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from trader.strategies.base import EnterLong, Exit

pytestmark = pytest.mark.db
CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)


class Env:
    def __init__(self, factory: sessionmaker[Session], *, audit_auto: bool, mode: str) -> None:
        self.factory = factory
        self.clock = FixedClock(T)
        run = get_live_run(factory, self.clock, RuntimeSettings())
        self.run_id = run.id
        with factory() as s:
            self.sym = add_symbol(s, "AAA", questrade_id=11)
            self.cfg = add_strategy_config(s)
            sig = m.Signal(
                run_id=run.id,
                strategy_config_id=self.cfg,
                symbol_id=self.sym,
                session_date=T.date(),
                event_key="orb_open",
                ts=T,
                intent={},
                evidence={},
            )
            s.add(sig)
            s.commit()
            self.signal_id = sig.id
        self.store = SettingsStore(factory, now=self.clock.now)
        self.store.set("approval_mode", mode, actor="test")
        self.broker = SimBroker(factory, self.clock, Ledger(CAL), QuoteFillModel(FillParams()), run.id)
        self.svc = ProposalService(
            factory, self.clock, self.store, self.broker, run.id, audit_auto=audit_auto
        )

    def entry(self) -> SizedOrder:
        intent = EnterLong(self.sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
        spec = OrderSpec(
            self.sym,
            "buy",
            "stop",
            10,
            stop=Decimal("20.01"),
            stop_loss=Decimal("19.91"),
            strategy_config_id=self.cfg,
            reason="orb_breakout",
        )
        return SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10"})

    def proposal_audits(self) -> list[str]:
        with self.factory() as s:
            return list(
                s.execute(
                    select(m.AuditLog.action)
                    .where(m.AuditLog.action.like("proposal.%"))
                    .order_by(m.AuditLog.id)
                ).scalars()
            )


def test_auto_mode_without_audit_writes_no_audit_row(db_factory: sessionmaker[Session]) -> None:
    env = Env(db_factory, audit_auto=False, mode="auto")
    p = env.svc.create(env.signal_id, env.entry(), "entry")
    assert (p.status, p.decided_via, p.decided_by, p.order_id is not None) == (
        "submitted",
        "auto",
        "auto",
        True,
    )
    assert env.proposal_audits() == []
    with db_factory() as s:
        ev = s.execute(select(m.EventLog).where(m.EventLog.source == "proposals")).scalars().all()
    assert ev and all(e.run_id == env.run_id for e in ev)  # its event is written as before


def test_default_still_writes_the_auto_approve_audit_row(db_factory: sessionmaker[Session]) -> None:
    env = Env(db_factory, audit_auto=True, mode="auto")
    p = env.svc.create(env.signal_id, env.entry(), "entry")
    assert p.status == "submitted"
    assert env.proposal_audits() == ["proposal.auto_approve"]


def test_human_decisions_are_audited_even_with_audit_auto_off(db_factory: sessionmaker[Session]) -> None:
    env = Env(db_factory, audit_auto=False, mode="manual")
    p = env.svc.create(env.signal_id, env.entry(), "entry")
    assert p.status == "pending" and env.proposal_audits() == []
    env.svc.decide(p.id, "approve", "web", "stephen")
    assert env.proposal_audits() == ["proposal.approve"]


def test_auto_execute_on_expiry_is_not_audited_with_audit_auto_off(db_factory: sessionmaker[Session]) -> None:
    env = Env(db_factory, audit_auto=False, mode="manual")
    env.broker.submit(
        OrderSpec(env.sym, "buy", "market", 10, stop_loss=Decimal("19.00"), strategy_config_id=env.cfg)
    )
    quote = QtQuote(
        env.sym,
        "AAA",
        Decimal("20.00"),
        Decimal("20.01"),
        Decimal("20.00"),
        None,
        1000,
        T - timedelta(seconds=1),
        0,
        False,
        None,
    )
    (fill,) = env.broker.on_quotes([quote], T)
    spec = OrderSpec(
        env.sym, "sell", "market", 10, purpose="exit", position_id=fill.position_id, reason="flatten"
    )
    sized = SizedOrder(Exit(fill.position_id, "market", None, "flatten"), "exit", 10, spec)
    p = env.svc.create(env.signal_id, sized, "exit")
    assert p.status == "pending"
    (expired,) = env.svc.expire_due(p.expires_at)
    assert (expired.status, expired.decided_via) == ("submitted", "auto")  # BR-42: it executed by itself
    assert env.proposal_audits() == []
