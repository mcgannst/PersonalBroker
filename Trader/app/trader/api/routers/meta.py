"""GET /api/health -> HealthOut and GET /api/meta -> MetaOut, both public.

Stub (P4-T1): T3 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["meta"])
