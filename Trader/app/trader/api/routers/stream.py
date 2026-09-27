"""GET /api/stream -> text/event-stream: live `invalidate` and `events` messages from the change feed.

Stub (P4-T1): T11 adds the route. The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["stream"])

MAX_STREAMS = 10  # open streams at once; one more gets 429
KEEPALIVE_SECONDS = 15  # a `: keepalive` comment this often
SESSION_RECHECK_SECONDS = 60  # a revoked or expired session ends its stream within this time
