"""GET /api/killswitch, POST /api/killswitch/{switch}/reset, POST /api/killswitch/pause and /resume.

Stub (P4-T1): T6 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["killswitch"])
