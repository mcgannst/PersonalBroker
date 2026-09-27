"""GET /api/settings, PUT /api/settings/{key}.

Stub (P4-T1): T8 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["settings"])
