"""GET /api/journal, PUT /api/journal/{date}.

Stub (P4-T1): T7 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["journal"])
