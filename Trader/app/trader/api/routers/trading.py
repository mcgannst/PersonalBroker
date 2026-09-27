"""GET /api/candidates, /api/orders, /api/fills, /api/positions, /api/positions/{id}, /api/trades.

Stub (P4-T1): T5 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["trading"])
