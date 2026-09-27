"""GET /api/proposals, GET /api/proposals/{id}, POST /api/proposals/{id}/approve and /reject (through
`ApiServices.decider_for` only: this module never constructs a ProposalService).

Stub (P4-T1): T6 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["proposals"])
