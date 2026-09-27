"""Proposal workflow (SPEC §6.2, BR-30, BR-31, BR-33).

pending → approved → submitted · pending → rejected · pending → expired · auto_approved → submitted.
decide() locks the row, so the first decision wins and every later one gets already_decided (Review Focus 2).
An approved proposal is submitted to the broker in the same transaction as the decision.
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.base import Broker, BrokerRejected
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.risk import ProposalKind, SizedOrder
from trader.events import log_event
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import Cancel

Decision = Literal["approve", "reject"]
Via = Literal["telegram", "web", "auto"]
SOURCE = "proposals"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    status: str
    already_decided: bool
    order_id: int | None = None


def _ttl(kind: str, s: RuntimeSettings) -> timedelta:
    seconds = {
        "entry": s.proposal_ttl_entry_seconds,
        "stop": s.proposal_ttl_stop_seconds,
        "exit": s.proposal_ttl_exit_seconds,
        "cancel": s.proposal_ttl_exit_seconds,
    }[kind]
    return timedelta(seconds=seconds)


class ProposalService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        settings: SettingsStore,
        broker: Broker,
        run_id: int,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._settings = settings
        self._broker = broker
        self.run_id = run_id

    def _log(self, s: Session, level: str, message: str, p: m.Proposal, **extra: Any) -> None:
        data = {
            "proposal_id": p.id,
            "kind": p.kind,
            "status": p.status,
            "position_id": p.position_id,
            **extra,
        }
        log_event(s, self._clock, level, SOURCE, message, data, run_id=self.run_id)

    def set_approval_mode(self, mode: Literal["manual", "auto"], actor: str) -> None:
        self._settings.set("approval_mode", mode, actor)  # the settings store writes the audit row

    def create(self, signal_id: int, sized: SizedOrder, kind: ProposalKind) -> m.Proposal:
        if kind != sized.kind:
            raise ValueError(f"kind {kind!r} doesn't match the sized order's kind {sized.kind!r}")
        settings = self._settings.load()
        now = self._clock.now()
        if isinstance(sized.intent, Cancel):
            order_spec: dict[str, Any] = {
                "cancel_order_id": sized.cancel_order_id,
                "reason": sized.intent.reason,
            }
        else:
            order_spec = sized.spec.to_json() if sized.spec is not None else {}
        with session_scope(self._factory) as s:
            p = m.Proposal(
                run_id=self.run_id,
                signal_id=signal_id,
                kind=kind,
                order_spec=order_spec,
                qty=sized.qty,
                status="pending",
                created_at=now,
                expires_at=now + _ttl(kind, settings),
                position_id=sized.position_id,
                cancel_order_id=sized.cancel_order_id,
                sizing=sized.sizing or None,
                escalations=0,
            )
            s.add(p)
            s.flush()
            if settings.approval_mode == "auto":
                p.status, p.decided_at, p.decided_via, p.decided_by = "auto_approved", now, "auto", "auto"
                p.decision_latency_ms = 0
                self._execute(s, p)
            self._log(
                s, "info", f"proposal {p.id} ({kind}) {p.status}", p, expires_at=p.expires_at.isoformat()
            )
            return p

    def decide(self, proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
        settings = self._settings.load()
        now = self._clock.now()
        with session_scope(self._factory) as s:
            p = s.execute(
                select(m.Proposal)
                .where(m.Proposal.id == proposal_id, m.Proposal.run_id == self.run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if p is None:
                raise KeyError(f"proposal {proposal_id} not found")
            if p.status != "pending":
                return DecisionResult(p.status, True, p.order_id)
            if now >= p.expires_at:
                self._expire(s, p, now, settings)
                return DecisionResult(p.status, True, p.order_id)
            p.decided_at, p.decided_via, p.decided_by = now, via, actor
            p.decision_latency_ms = int((now - p.created_at).total_seconds() * 1000)
            if decision == "reject":
                p.status = "rejected"
            else:
                p.status = "approved"
                self._execute(s, p)
            s.add(
                m.AuditLog(
                    ts=now,
                    actor=actor,
                    action=f"proposal.{decision}",
                    before={"proposal_id": p.id, "status": "pending"},
                    after={"status": p.status, "via": via, "order_id": p.order_id},
                )
            )
            self._log(s, "info", f"proposal {p.id} {decision}d via {via}: {p.status}", p, actor=actor)
            return DecisionResult(p.status, False, p.order_id)

    def expire_due(self, now: datetime) -> list[m.Proposal]:
        settings = self._settings.load()
        with session_scope(self._factory) as s:
            rows = list(
                s.execute(
                    select(m.Proposal)
                    .where(
                        m.Proposal.run_id == self.run_id,
                        m.Proposal.status == "pending",
                        m.Proposal.expires_at <= now,
                    )
                    .order_by(m.Proposal.id)
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            for p in rows:
                self._expire(s, p, now, settings)
            return rows

    def escalate_unprotected(self, now: datetime) -> list[m.Proposal]:
        interval = timedelta(seconds=self._settings.load().stop_escalation_seconds)
        out: list[m.Proposal] = []
        with session_scope(self._factory) as s:
            rows = s.execute(
                select(m.Proposal)
                .where(
                    m.Proposal.run_id == self.run_id,
                    m.Proposal.kind == "stop",
                    m.Proposal.status == "expired",
                    m.Proposal.escalated_at <= now - interval,
                )
                .order_by(m.Proposal.id)
                .with_for_update(skip_locked=True)
            ).scalars()
            for p in rows:
                pos = s.get(m.Position, p.position_id) if p.position_id is not None else None
                if pos is None or pos.closed_at is not None or pos.stop_order_id is not None:
                    continue
                p.escalations += 1
                p.escalated_at = now
                since = pos.unprotected_since
                unprotected = int((now - since).total_seconds()) + pos.unprotected_seconds if since else None
                self._log(
                    s,
                    "error",
                    f"position {pos.id} is still unprotected (alert {p.escalations})",
                    p,
                    unprotected_seconds=unprotected,
                )
                out.append(p)
        return out

    # --- internals -----------------------------------------------------------------------------------------
    def _execute(self, s: Session, p: m.Proposal) -> None:
        try:
            if p.kind == "cancel":
                order_id = p.cancel_order_id
                if order_id is None or not self._broker.cancel(
                    order_id, str(p.order_spec.get("reason", "")), session=s
                ):
                    raise BrokerRejected(f"order {order_id} is no longer working")
            else:
                spec = dataclasses.replace(OrderSpec.from_json(p.order_spec), proposal_id=p.id)
                p.order_id = self._broker.submit(spec, session=s)
            p.status = "submitted"
        except BrokerRejected as exc:
            p.status, p.error = "failed", str(exc)
            self._log(s, "error", f"proposal {p.id} could not be executed: {exc}", p)

    def _expire(self, s: Session, p: m.Proposal, now: datetime, settings: RuntimeSettings) -> None:
        p.status, p.expired_at = "expired", now
        if p.kind == "stop":
            p.escalated_at, p.escalations = now, 1
            self._log(
                s,
                "error",
                f"protective stop proposal {p.id} expired: position {p.position_id} has no stop",
                p,
            )
        elif p.kind in ("exit", "cancel") and settings.auto_flatten_on_expiry:
            # BR-42: an unanswered flatten, or an unanswered cancel of a working entry, executes by itself
            p.decided_at, p.decided_via, p.decided_by = now, "auto", "auto_flatten_on_expiry"
            self._execute(s, p)
            self._log(s, "warning", f"{p.kind} proposal {p.id} expired and was executed automatically", p)
        elif p.kind == "exit":
            p.escalated_at, p.escalations = now, 1
            self._log(s, "error", f"exit proposal {p.id} expired: position {p.position_id} is still open", p)
        else:
            self._log(s, "info", f"proposal {p.id} ({p.kind}) expired", p)
