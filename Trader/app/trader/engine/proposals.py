"""Proposal workflow (SPEC §6.2, BR-30, BR-31, BR-33).

pending → approved → submitted · pending → rejected · pending → expired · auto_approved → submitted.
A broker refusal ends in `failed`. decide() locks the row, so the first decision wins and every later one
gets already_decided (Review Focus 2). An approved proposal is submitted to the broker in the same
transaction as the decision, inside a savepoint so a refusal leaves no half-written broker rows.

Entry guard (SPEC §6.3, BR-34/BR-41): kill switches are "checked before every entry proposal", and a
switch can trip (or /pause can land) while an entry waits for Stephen. So an approval of an ENTRY, manual
or automatic, asks `entry_blocked(run_id)` first; when it returns a reason nothing is submitted and the
proposal ends `rejected` (the SPEC §6.2 terminal state for "not executed by decision"; `failed` is kept for
broker refusals), with the reason in `error`, the audit row and the event log. Exits, stops and cancels
are never guarded, so a kill switch can't trap an open position (Review Focus 5).
"""

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

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
ProposalStatus = Literal["pending", "approved", "auto_approved", "submitted", "rejected", "expired", "failed"]
SOURCE = "proposals"
PAST: dict[Decision, str] = {"approve": "approved", "reject": "rejected"}
AUTO_FLATTEN_ACTOR = "auto_flatten_on_expiry"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    status: ProposalStatus
    already_decided: bool
    order_id: int | None = None
    blocked: str | None = None  # why an approved entry was not submitted (entry guard), else None


def _ttl(kind: ProposalKind, s: RuntimeSettings) -> timedelta:
    seconds = {
        "entry": s.proposal_ttl_entry_seconds,
        "stop": s.proposal_ttl_stop_seconds,
        "exit": s.proposal_ttl_exit_seconds,
        "cancel": s.proposal_ttl_exit_seconds,
    }[kind]
    return timedelta(seconds=seconds)


def _status(p: m.Proposal) -> ProposalStatus:
    return cast(ProposalStatus, p.status)


class ProposalService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        settings: SettingsStore,
        broker: Broker,
        run_id: int,
        *,
        entry_blocked: Callable[[int], str | None] | None = None,
        audit_auto: bool = True,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._settings = settings
        self._broker = broker
        self.run_id = run_id
        self._entry_blocked = entry_blocked
        # False (replay only): skip the automatic audit rows (proposal.auto_approve and
        # proposal.auto_execute_on_expiry:*), so a replay never writes audit_log rows stamped with simulated
        # past times. Human decisions (decide) are always audited.
        self._audit_auto = audit_auto

    def _log(self, s: Session, level: str, message: str, p: m.Proposal, **extra: Any) -> None:
        data = {
            "proposal_id": p.id,
            "kind": p.kind,
            "status": p.status,
            "position_id": p.position_id,
            **extra,
        }
        log_event(s, self._clock, level, SOURCE, message, data, run_id=self.run_id)

    def _entry_block(self, p: m.Proposal) -> str | None:
        if p.kind != "entry" or self._entry_blocked is None:
            return None
        return self._entry_blocked(self.run_id)

    def _approve(self, s: Session, p: m.Proposal, approved_status: ProposalStatus) -> str | None:
        """Submit `p`, unless it is an entry the guard blocks. Returns the block reason, if any."""
        blocked = self._entry_block(p)
        if blocked is not None:
            p.status, p.error = "rejected", f"entry blocked: {blocked}"
            return blocked
        p.status = approved_status
        self._execute(s, p)
        return None

    def set_approval_mode(self, mode: Literal["manual", "auto"], actor: str) -> None:
        self._settings.set("approval_mode", mode, actor)  # the settings store writes the audit row

    def create(self, signal_id: int, sized: SizedOrder, kind: ProposalKind) -> m.Proposal:
        if kind != sized.kind:
            raise ValueError(f"kind {kind!r} doesn't match the sized order's kind {sized.kind!r}")
        if kind != "cancel" and sized.spec is None:
            raise ValueError(f"a {kind} proposal needs an order spec")
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
            blocked: str | None = None
            if settings.approval_mode == "auto":
                p.decided_at, p.decided_via, p.decided_by = now, "auto", "auto"
                p.decision_latency_ms = 0
                blocked = self._approve(s, p, "auto_approved")
                if self._audit_auto:
                    s.add(
                        m.AuditLog(
                            ts=now,
                            actor="auto",
                            action="proposal.auto_approve",
                            before={"proposal_id": p.id, "status": "pending"},
                            after={
                                "status": p.status,
                                "via": "auto",
                                "order_id": p.order_id,
                                "blocked": blocked,
                            },
                        )
                    )
            self._log(
                s,
                "warning" if blocked else "info",
                f"proposal {p.id} ({kind}) {p.status}" + (f": {blocked}" if blocked else ""),
                p,
                expires_at=p.expires_at.isoformat(),
            )
            return p

    def decide(self, proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
        settings = self._settings.load()
        with session_scope(self._factory) as s:
            p = s.execute(
                select(m.Proposal)
                .where(m.Proposal.id == proposal_id, m.Proposal.run_id == self.run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if p is None:
                raise KeyError(f"proposal {proposal_id} not found")
            now = self._clock.now()  # read after the lock, so a wait on a racing decision isn't counted early
            if p.status != "pending":
                return DecisionResult(_status(p), True, p.order_id)
            if now >= p.expires_at:
                self._expire(s, p, now, settings)
                return DecisionResult(_status(p), True, p.order_id)
            p.decided_at, p.decided_via, p.decided_by = now, via, actor
            p.decision_latency_ms = int((now - p.created_at).total_seconds() * 1000)
            blocked: str | None = None
            if decision == "reject":
                p.status = "rejected"
            else:
                blocked = self._approve(s, p, "approved")
            after: dict[str, Any] = {"status": p.status, "via": via, "order_id": p.order_id}
            if blocked is not None:
                after["blocked"] = blocked
            s.add(
                m.AuditLog(
                    ts=now,
                    actor=actor,
                    action=f"proposal.{decision}",
                    before={"proposal_id": p.id, "status": "pending"},
                    after=after,
                )
            )
            message = f"proposal {p.id} {PAST[decision]} via {via}: {p.status}"
            if blocked is not None:
                message += f" ({blocked})"
            self._log(s, "warning" if blocked else "info", message, p, actor=actor)
            return DecisionResult(_status(p), False, p.order_id, blocked)

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
            # Lock every affected position up front, by id, as SimBroker.on_quotes/end_of_session do. Expiring
            # proposal by proposal would lock positions in proposal-id order and could deadlock with them.
            position_ids = sorted({p.position_id for p in rows if p.position_id is not None})
            if position_ids:
                s.execute(
                    select(m.Position.id)
                    .where(m.Position.id.in_(position_ids))
                    .order_by(m.Position.id)
                    .with_for_update()
                ).all()
            for p in rows:
                self._expire(s, p, now, settings)
            return rows

    def escalate_unprotected(self, now: datetime) -> list[m.Proposal]:
        """Re-alert, once per position per tick, for each position whose protective stop expired unanswered.

        A proposal whose position has closed or gained a working stop has its escalation marker cleared, so it
        is never selected again. Returns the proposal each alert was counted on (the newest for its position).
        """
        interval = timedelta(seconds=self._settings.load().stop_escalation_seconds)
        out: list[m.Proposal] = []
        with session_scope(self._factory) as s:
            rows = s.execute(
                select(m.Proposal)
                .where(
                    m.Proposal.run_id == self.run_id,
                    m.Proposal.kind == "stop",
                    m.Proposal.status == "expired",
                    m.Proposal.escalated_at.is_not(None),
                    m.Proposal.escalated_at <= now - interval,
                )
                .order_by(m.Proposal.id)
                .with_for_update(skip_locked=True)
            ).scalars()
            due: dict[int, list[m.Proposal]] = {}
            for p in rows:
                pos = s.get(m.Position, p.position_id) if p.position_id is not None else None
                if pos is None or pos.closed_at is not None or pos.stop_order_id is not None:
                    p.escalated_at = None  # protected or gone: stop escalating this proposal for good
                    continue
                due.setdefault(pos.id, []).append(p)
            for position_id, proposals in due.items():
                # every expired stop for this position restarts its cadence, so one position = one alert
                siblings = s.execute(
                    select(m.Proposal)
                    .where(
                        m.Proposal.run_id == self.run_id,
                        m.Proposal.kind == "stop",
                        m.Proposal.status == "expired",
                        m.Proposal.position_id == position_id,
                        m.Proposal.escalated_at.is_not(None),
                    )
                    .with_for_update(skip_locked=True)
                ).scalars()
                for sib in siblings:
                    sib.escalated_at = now
                for p in proposals:
                    p.escalated_at = now
                lead = proposals[-1]
                lead.escalations += 1
                pos = s.get(m.Position, position_id)
                assert pos is not None
                since = pos.unprotected_since
                unprotected = int((now - since).total_seconds()) + pos.unprotected_seconds if since else None
                self._log(
                    s,
                    "error",
                    f"position {pos.id} is still unprotected (alert {lead.escalations})",
                    lead,
                    unprotected_seconds=unprotected,
                    proposal_ids=[p.id for p in proposals],
                )
                out.append(lead)
        return out

    # --- internals -----------------------------------------------------------------------------------------
    def _execute(self, s: Session, p: m.Proposal) -> None:
        try:
            with s.begin_nested():  # a refusal rolls back only the broker's writes, never the decision
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
            p.status, p.error, p.order_id = "failed", str(exc), None
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
            # BR-42: an unanswered flatten, or an unanswered cancel of a working entry, executes by itself.
            # decision_latency_ms stays empty: it measures how long a decision took, and nobody decided.
            p.decided_at, p.decided_via, p.decided_by = now, "auto", AUTO_FLATTEN_ACTOR
            self._execute(s, p)
            if self._audit_auto:  # unreachable in a replay (auto mode), skipped there for safety
                s.add(
                    m.AuditLog(
                        ts=now,
                        actor=AUTO_FLATTEN_ACTOR,
                        action=f"proposal.auto_execute_on_expiry:{p.kind}",
                        before={"proposal_id": p.id, "status": "pending"},
                        after={"status": p.status, "via": "auto", "order_id": p.order_id, "error": p.error},
                    )
                )
            self._log(s, "warning", f"{p.kind} proposal {p.id} expired and was executed automatically", p)
        elif p.kind == "exit":
            p.escalated_at, p.escalations = now, 1
            self._log(s, "error", f"exit proposal {p.id} expired: position {p.position_id} is still open", p)
        else:
            self._log(s, "info", f"proposal {p.id} ({p.kind}) expired", p)
