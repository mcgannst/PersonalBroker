"""POST /api/auth/login (public), POST /api/auth/logout, GET /api/auth/me, PUT /api/auth/password,
POST /api/auth/totp/setup, POST /api/auth/totp/confirm, POST /api/auth/totp/disable.

Stub (P4-T1): T4 adds the routes. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["auth"])
