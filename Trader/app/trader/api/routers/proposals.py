"""GET /api/proposals, GET /api/proposals/{id}, POST /api/proposals/{id}/approve and /reject (SPEC §4.4,
§6.2, §11; BR-30, BR-31).

SAFETY (contract refinement 3, Review Focus 1): a web decision goes through `ApiServices.decider_for` ONLY.
T18 builds it from the same guarded decider the Telegram bot uses (the proposal service's `decide` with the
kill-switch entry guard), so both routes do exactly the same thing: an entry approved while a kill switch or
a pause is active ends `rejected` with no order, stops/exits/cancels go through, and the row lock makes the
first decision win (a later one, from either route, gets `already_decided`). This module builds no service
or broker of its own. The audit row and the event-log line come from the service, with actor `web:<user>`.

The Telegram message of a proposal decided here is closed by the worker's relay; nothing here calls
Telegram. Routes are plain `def` (FastAPI's thread pool), so the DB work stays off the event loop.
"""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from trader.api.deps import ApiServices, CsrfUser, CurrentUser, Services, actor, live_run_id
from trader.api.errors import ApiError
from trader.api.schemas import DecisionOut, Items, ProposalOut
from trader.db import models as m
from trader.engine.proposals import Decision, DecisionResult
from trader.notify.views import proposal_view

router = APIRouter(tags=["proposals"])

MAX_LIMIT = 200
ProposalId = Annotated[int, Path(ge=1, le=2**63 - 1)]  # a bigint: a larger id is a 422, never a DB error


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "Unknown proposal")


def _out(s: Session, p: m.Proposal) -> ProposalOut:
    """The same view Telegram shows, plus the row's decision details."""
    return ProposalOut.from_view(proposal_view(s, p), p)


def _read(services: ApiServices, run_id: int, proposal_id: int) -> ProposalOut:
    with services.core.factory() as s:
        p = s.execute(
            select(m.Proposal).where(m.Proposal.id == proposal_id, m.Proposal.run_id == run_id)
        ).scalar_one_or_none()
        if p is None:
            raise _not_found()
        return _out(s, p)


def decision_message(decision: Decision, result: DecisionResult) -> str:
    """What the page shows after a tap."""
    if result.already_decided:
        return f"Already {result.status.replace('_', '-')}"  # auto_approved -> auto-approved
    if result.blocked is not None:
        return f"Entry blocked: {result.blocked}"
    if decision == "reject":
        return "Rejected"
    if result.status == "failed":
        return "Approved, but the order failed"
    return "Approved"


def _decide(services: ApiServices, user: CsrfUser, proposal_id: int, decision: Decision) -> DecisionOut:
    run_id = live_run_id(services)  # read now, after the session and CSRF checks
    decide = services.decider_for(run_id)
    try:
        result = decide(proposal_id, decision, "web", actor(user))
    except KeyError:  # not a proposal of the live run
        raise _not_found() from None
    return DecisionOut(
        proposal=_read(services, run_id, proposal_id),
        already_decided=result.already_decided,
        blocked=result.blocked,
        message=decision_message(decision, result),
    )


@router.get("/proposals")
def list_proposals(
    services: Services,
    user: CurrentUser,
    status: Literal["pending", "all"] = "pending",
    session_date: Annotated[date | None, Query(alias="date")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
) -> Items[ProposalOut]:
    """The live run's proposals: `pending` ones oldest first (the order to decide them in), or `all` newest
    first; `date` keeps one session's (the ET session date of the proposal's signal)."""
    run_id = live_run_id(services)
    q = select(m.Proposal).where(m.Proposal.run_id == run_id)
    if status == "pending":
        q = q.where(m.Proposal.status == "pending").order_by(m.Proposal.created_at, m.Proposal.id)
    else:
        q = q.order_by(m.Proposal.created_at.desc(), m.Proposal.id.desc())
    if session_date is not None:
        q = q.join(m.Signal, m.Signal.id == m.Proposal.signal_id).where(m.Signal.session_date == session_date)
    with services.core.factory() as s:
        rows = s.execute(q.limit(limit)).scalars().all()
        return Items[ProposalOut](items=[_out(s, p) for p in rows])


@router.get("/proposals/{proposal_id}")
def get_proposal(services: Services, user: CurrentUser, proposal_id: ProposalId) -> ProposalOut:
    return _read(services, live_run_id(services), proposal_id)


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(services: Services, user: CsrfUser, proposal_id: ProposalId) -> DecisionOut:
    """One tap, as on Telegram (no confirmation step). An already-decided proposal is a 200 with its
    status."""
    return _decide(services, user, proposal_id, "approve")


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(services: Services, user: CsrfUser, proposal_id: ProposalId) -> DecisionOut:
    return _decide(services, user, proposal_id, "reject")
