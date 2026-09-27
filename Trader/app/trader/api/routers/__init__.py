"""Every API router, in registration order. `create_app` (T3) includes each under the `/api` prefix; no other
module registers routes, so no task edits a shared registration point.

The submodules are imported as modules (never `router as <name>`), so `trader.api.routers.auth` stays the
module and monkeypatch paths keep working.
"""

from fastapi import APIRouter

from trader.api.routers import (
    auth,
    credentials,
    dashboard,
    jobs,
    journal,
    killswitch,
    meta,
    performance,
    proposals,
    settings,
    strategies,
    stream,
    system,
    trading,
    watchlist,
)

ROUTERS: tuple[APIRouter, ...] = (
    meta.router,
    auth.router,
    dashboard.router,
    trading.router,
    proposals.router,
    killswitch.router,
    performance.router,
    journal.router,
    settings.router,
    strategies.router,
    system.router,
    jobs.router,
    credentials.router,
    watchlist.router,
    stream.router,
)
