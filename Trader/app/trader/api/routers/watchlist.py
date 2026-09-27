"""GET /api/watchlist, POST /api/watchlist (multipart), DELETE /api/watchlist/{date}.

Stub (P4-T1): T10 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["watchlist"])
