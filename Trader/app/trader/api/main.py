"""The FastAPI application: lifespan (services and the change-feed task), error handlers, security headers,
the request log, every router under `/api`, and the built web app with SPA deep links (SPEC §11, §12, §14).

Stub (P4-T1): T3 implements it.
"""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path

from fastapi import FastAPI

from trader.api.deps import ApiServices
from trader.config import EnvSettings

# Sent on every response (inline styles only, for chart attributes). `/api` responses also get
# `Cache-Control: no-store`.
SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


def web_dist_dir(env: EnvSettings) -> Path:
    """`WEB_DIST_DIR`, else `<package>/../../web/dist` (the repo's built web app)."""
    raise NotImplementedError("P4-T3")


def create_app(
    *,
    services_factory: Callable[[AsyncExitStack], Awaitable[ApiServices]] | None = None,
    web_dist: Path | None = None,
) -> FastAPI:
    """The app. The default `services_factory` builds Core with `build_core()` and calls
    `trader.api.services.build_services(core, stack)`; `web_dist` defaults to `web_dist_dir(env)`."""
    raise NotImplementedError("P4-T3")
