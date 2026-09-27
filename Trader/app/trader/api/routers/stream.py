"""GET /api/stream -> text/event-stream: live `invalidate` and `events` messages from the change feed
(SPEC §11, §12 "Live updates use SSE"; decision "SSE by polling").

Frames: `retry: 3000`, then `event: hello` (`StreamHello`) and `event: invalidate` with every topic (so a
reconnect always resyncs: no `Last-Event-ID` handling), then the feed's messages as `event: invalidate` /
`event: events` with JSON data, and a `: keepalive` comment every `KEEPALIVE_SECONDS`.

A stream ends when the client disconnects, when its session is no longer valid (re-checked every
`SESSION_RECHECK_SECONDS`), when the feed stops (the app's lifespan sets the feed's stop event), or when the
uvicorn server begins to exit. The last one matters because uvicorn waits for open connections BEFORE it runs
the lifespan shutdown, so the feed's stop event alone would hold a graceful shutdown for its whole timeout:
`uvicorn.Server.handle_exit` (what SIGTERM and SIGINT call) is wrapped once at import to mark the server's
event loop as exiting, and every stream on that loop ends within a second.
"""

import asyncio
import json
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import timedelta
from types import FrameType
from typing import Any

import structlog
import uvicorn
from fastapi import APIRouter
from sqlalchemy.orm import Session, sessionmaker
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from trader.api.deps import ChangeFeed, CurrentUser, FeedMessage, Services
from trader.api.errors import ApiError
from trader.api.feed import full_invalidate
from trader.api.schemas import StreamHello
from trader.db import models as m
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("api.stream")

router = APIRouter(tags=["stream"])

MAX_STREAMS = 10  # open streams at once; one more gets 429
KEEPALIVE_SECONDS = 15  # a `: keepalive` comment this often
SESSION_RECHECK_SECONDS = 60  # a revoked or expired session ends its stream within this time
RETRY_MS = 3000  # the browser's EventSource reconnect delay
TICK_SECONDS = 1.0  # how often a stream checks keepalive, its session and server exit

STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # NPM/nginx must not buffer the stream
}

KEEPALIVE = ": keepalive\n\n"


# --- server exit --------------------------------------------------------------------------------------------

_exiting_loops: "weakref.WeakSet[asyncio.AbstractEventLoop]" = weakref.WeakSet()
_exiting_everywhere = False  # handle_exit ran outside any event loop (the process is going away)


def _mark_exiting() -> None:
    global _exiting_everywhere
    try:
        _exiting_loops.add(asyncio.get_running_loop())
    except (RuntimeError, TypeError):
        _exiting_everywhere = True


def server_exiting() -> bool:
    """True once the uvicorn server running on this event loop has been told to exit."""
    if _exiting_everywhere:
        return True
    try:
        return asyncio.get_running_loop() in _exiting_loops
    except RuntimeError:
        return False


def _install_exit_hook() -> None:
    original = uvicorn.Server.handle_exit
    if getattr(original, "_trader_stream_hook", False):
        return

    def handle_exit(self: uvicorn.Server, sig: int, frame: FrameType | None) -> None:
        _mark_exiting()
        original(self, sig, frame)

    handle_exit._trader_stream_hook = True  # type: ignore[attr-defined]
    uvicorn.Server.handle_exit = handle_exit  # type: ignore[method-assign]


_install_exit_hook()


# --- sessions -----------------------------------------------------------------------------------------------


def session_valid(factory: sessionmaker[Session], clock: Clock, session_id: int, idle_hours: int) -> bool:
    """The rules `trader.api.auth.authenticate` applies to a cookie's session row: it exists, is not revoked,
    has not expired and has been used within `idle_hours`."""
    now = clock.now()
    with factory() as s:
        row = s.get(m.WebSession, session_id)
        return (
            row is not None
            and row.revoked_at is None
            and now < row.expires_at
            and now - row.last_seen_at < timedelta(hours=idle_hours)
        )


# --- the stream ---------------------------------------------------------------------------------------------


def _frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _message_frame(msg: FeedMessage) -> str:
    return _frame(msg.kind, msg.data)


async def event_stream(
    feed: ChangeFeed,
    *,
    hello: StreamHello,
    session_valid: Callable[[], Awaitable[bool]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    exiting: Callable[[], bool] = server_exiting,
) -> AsyncGenerator[str, None]:
    """The SSE text of one stream. Leaving it (the end, a disconnect, `aclose()`) always unsubscribes."""
    async with feed.subscribe() as messages:
        yield f"retry: {RETRY_MS}\n\n"
        yield f"event: hello\ndata: {hello.model_dump_json()}\n\n"
        yield _message_frame(full_invalidate())
        out: asyncio.Queue[str | None] = asyncio.Queue()
        tasks = (
            asyncio.ensure_future(_pump(messages, out)),
            asyncio.ensure_future(_tick(out, session_valid, sleep, exiting)),
        )
        try:
            while (chunk := await out.get()) is not None:
                yield chunk
        finally:
            for t in tasks:  # no await here: this may run inside a cancelled scope
                t.cancel()


async def _pump(messages: AsyncIterator[FeedMessage], out: "asyncio.Queue[str | None]") -> None:
    try:
        async for msg in messages:
            out.put_nowait(_message_frame(msg))
    finally:
        out.put_nowait(None)  # the feed stopped (or this task was cancelled): the stream ends


async def _tick(
    out: "asyncio.Queue[str | None]",
    session_valid: Callable[[], Awaitable[bool]],
    sleep: Callable[[float], Awaitable[None]],
    exiting: Callable[[], bool],
) -> None:
    since_keepalive = 0.0
    since_check = 0.0
    while True:
        if exiting():
            out.put_nowait(None)
            return
        await sleep(TICK_SECONDS)
        since_keepalive += TICK_SECONDS
        since_check += TICK_SECONDS
        if since_keepalive >= KEEPALIVE_SECONDS:
            since_keepalive = 0.0
            out.put_nowait(KEEPALIVE)
        if since_check >= SESSION_RECHECK_SECONDS:
            since_check = 0.0
            try:
                ok = await session_valid()
            except Exception as exc:  # a DB hiccup must not end a good stream
                log.warning("stream.session_check_failed", error_type=type(exc).__name__)
                ok = True
            if not ok:
                out.put_nowait(None)
                return


def _idle_hours(services: Services) -> int:
    try:
        settings = services.core.settings.load()
    except Exception as exc:
        log.warning("stream.settings_unusable", error_type=type(exc).__name__)
        settings = RuntimeSettings()
    return settings.web_session_idle_hours


@router.get("/stream", response_class=StreamingResponse)
async def stream(services: Services, user: CurrentUser) -> StreamingResponse:
    """Live updates for the signed-in browser (EventSource sends the cookie; a GET needs no CSRF)."""
    feed = services.feed
    if feed.subscriber_count() >= MAX_STREAMS:
        raise ApiError(429, "too_many_requests", "Too many live update streams are open")
    core = services.core
    idle_hours = await asyncio.to_thread(_idle_hours, services)

    async def still_valid() -> bool:
        return await asyncio.to_thread(session_valid, core.factory, core.clock, user.session_id, idle_hours)

    body = event_stream(feed, hello=StreamHello(server_time=core.clock.now()), session_valid=still_valid)
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
        background=BackgroundTask(body.aclose),  # runs after a disconnect too: always unsubscribes
    )
