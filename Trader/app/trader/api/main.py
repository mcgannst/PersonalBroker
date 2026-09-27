"""The FastAPI application: lifespan (services and the change-feed task), error handlers, security headers,
the request log, every router under `/api`, and the built web app with SPA deep links (SPEC §11, §12, §14).

- **Lifespan:** an `AsyncExitStack` holds whatever the services open; the change feed runs as a task until
  shutdown (bounded by `FEED_STOP_SECONDS`), then the stack closes. A build failure is logged and re-raised,
  so the process exits and supervisord restarts it.
- **Request log** (a plain ASGI middleware, so SSE streams are never buffered): a fresh request id per
  request (`request.state.request_id`, the `X-Request-ID` header, structlog contextvars) and one
  `http.request` line with method, path (no query string), status and duration. Never bodies, query strings,
  cookies or auth headers. An unhandled exception is rendered here as the 500 `ErrorOut`, so it carries the
  request id and the security headers too.
- **Security headers** on every response; `Cache-Control: no-store` on `/api` responses.
- **SPA:** `/assets/...` files are immutable; any other GET that matches no route returns `index.html`
  (deep links, contract refinement 1) or a real file at the top of `dist`. Unknown `/api/...` paths are a
  JSON 404, never the SPA. Nothing outside `dist` and no hidden file is ever served.
"""

import asyncio
import inspect
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trader.api.deps import ApiServices
from trader.api.errors import ApiError, install_error_handlers
from trader.api.routers import ROUTERS
from trader.config import EnvSettings, get_env
from trader.logging_setup import redact_text

log = structlog.get_logger("api.http")

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
API_CACHE_CONTROL = "no-store"
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"  # hashed file names: cached forever
PAGE_CACHE_CONTROL = "no-cache"  # index.html: always revalidated, so a deploy is picked up at once
NOT_BUILT = "web app not built"
QUIET_PATHS = frozenset({"/api/health", "/api/stream"})  # logged at DEBUG (health checks, long streams)
FEED_STOP_SECONDS = 5.0  # how long shutdown waits for the change feed before cancelling it

ServicesFactory = Callable[[AsyncExitStack], Awaitable[ApiServices]]
# An exception handler as FastAPI stores it (`app.exception_handlers`): sync or async.
ErrorHandler = Callable[[Request, Any], Response | Awaitable[Response]]


async def _handle(handler: ErrorHandler, request: Request, exc: Exception) -> Response:
    result = handler(request, exc)
    return await result if inspect.isawaitable(result) else result


def web_dist_dir(env: EnvSettings) -> Path:
    """`WEB_DIST_DIR`, else `<package>/../../web/dist` (the repo's built web app)."""
    if env.web_dist_dir:
        return Path(env.web_dist_dir)
    return Path(__file__).resolve().parents[3] / "web" / "dist"  # Trader/app/trader/api -> Trader


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def request_log_level(path: str) -> int:
    return logging.DEBUG if path in QUIET_PATHS else logging.INFO


# --- request log and headers --------------------------------------------------------------------------------


class RequestLogMiddleware:
    """Request id, security headers and one `http.request` log line per HTTP request (plain ASGI: a
    streaming response passes through unbuffered)."""

    def __init__(self, app: ASGIApp, *, error_handler: ErrorHandler) -> None:
        self.app = app
        self.error_handler = error_handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = secrets.token_hex(6)
        scope.setdefault("state", {})["request_id"] = request_id
        path: str = scope.get("path", "")
        api = is_api_path(path)
        status = 500
        started = False
        t0 = time.perf_counter()

        async def send_with_headers(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = int(message["status"])
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                headers["X-Request-ID"] = request_id
                if api and "cache-control" not in headers:
                    headers["Cache-Control"] = API_CACHE_CONTROL
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_headers)
            except Exception as exc:
                if started:  # too late for an error response: the server closes the connection
                    self._log(scope, path, status, t0, request_id)
                    raise
                response = await _handle(self.error_handler, Request(scope), exc)
                await response(scope, receive, send_with_headers)
            self._log(scope, path, status, t0, request_id)

    @staticmethod
    def _log(scope: Scope, path: str, status: int, t0: float, request_id: str) -> None:
        log.log(
            request_log_level(path),
            "http.request",
            method=scope.get("method"),
            path=path,
            status=status,
            duration_ms=round((time.perf_counter() - t0) * 1000, 1),
            request_id=request_id,
        )


# --- the built web app --------------------------------------------------------------------------------------


def _safe_file(root: Path, relative: str) -> Path | None:
    """The regular file `root/relative`, or None when it is missing, hidden (any part starting with a dot),
    or outside `root` after resolving `..` and symlinks."""
    parts = [p for p in relative.replace("\\", "/").split("/") if p]
    if not parts or any(p.startswith(".") for p in parts):
        return None
    try:
        base = root.resolve()
        candidate = base.joinpath(*parts).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            return None
    except (OSError, ValueError):  # e.g. an embedded NUL byte
        return None
    return candidate


def _spa_response(dist: Path, path: str) -> Response:
    """A real file at the top of `dist`, else `index.html` (a deep link), else 503 "web app not built"."""
    index = dist / "index.html"
    if not index.is_file():
        return PlainTextResponse(NOT_BUILT, status_code=503)
    target = _safe_file(dist, path) or index
    return FileResponse(target, headers={"Cache-Control": PAGE_CACHE_CONTROL})


def _install_web(app: FastAPI, dist: Path) -> None:
    http_error = app.exception_handlers[StarletteHTTPException]

    @app.api_route("/assets/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def asset(path: str) -> FileResponse:
        file = _safe_file(dist / "assets", path)
        if file is None:
            raise ApiError(404, "not_found", "Not Found")
        return FileResponse(file, headers={"Cache-Control": ASSET_CACHE_CONTROL})

    async def not_found_or_spa(request: Request, exc: Exception) -> Response:
        # A GET that matches no route and is not under /api is a page of the SPA (deep links).
        assert isinstance(exc, StarletteHTTPException)
        path = request.url.path
        if exc.status_code == 404 and request.method in ("GET", "HEAD") and not is_api_path(path):
            return _spa_response(dist, path)
        return await _handle(http_error, request, exc)

    app.add_exception_handler(StarletteHTTPException, not_found_or_spa)


# --- the app ------------------------------------------------------------------------------------------------


async def default_services(stack: AsyncExitStack) -> ApiServices:
    """Core from the environment, then `build_services` (T18); the engine is disposed on shutdown."""
    from trader.api.services import build_services
    from trader.bootstrap import build_core

    core = build_core()
    stack.callback(core.engine.dispose)
    return await build_services(core, stack)


async def _stop_feed(task: "asyncio.Task[None]", stop: asyncio.Event) -> None:
    stop.set()
    try:
        await asyncio.wait_for(task, FEED_STOP_SECONDS)
    except TimeoutError:
        log.warning("api.feed_stop_timeout", seconds=FEED_STOP_SECONDS)
    except Exception as exc:
        log.error("api.feed_failed", error_type=type(exc).__name__, error=redact_text(str(exc))[:500])


def create_app(
    *,
    services_factory: ServicesFactory | None = None,
    web_dist: Path | None = None,
) -> FastAPI:
    """The app. The default `services_factory` builds Core with `build_core()` and calls
    `trader.api.services.build_services(core, stack)`; `web_dist` defaults to `web_dist_dir(env)`."""
    factory = services_factory or default_services
    dist = web_dist if web_dist is not None else web_dist_dir(get_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            try:
                services = await factory(stack)
            except Exception as exc:
                log.error(
                    "api.services_failed", error_type=type(exc).__name__, error=redact_text(str(exc))[:500]
                )
                raise
            app.state.services = services
            stop = asyncio.Event()
            task = asyncio.create_task(services.feed.run(stop), name="change-feed")
            try:
                yield
            finally:
                await _stop_feed(task, stop)

    app = FastAPI(title="Trader", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router, prefix="/api")
    _install_web(app, dist)
    app.add_middleware(RequestLogMiddleware, error_handler=app.exception_handlers[Exception])
    return app
