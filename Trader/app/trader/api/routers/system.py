"""GET /api/system, GET /api/events, POST /api/system/telegram-test.

Stub (P4-T1): T9 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["system"])
