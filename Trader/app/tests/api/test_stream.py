"""P4-T11 acceptance tests 7-9: `GET /api/stream` (SSE). The stream generator is driven directly with a fake
sleep (fake time); the route, its headers, the stream limit and shutdown run through a real uvicorn server
on a random localhost port with httpx streaming."""

import asyncio
import signal
import socket
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import DEFAULT_USER
from tests.api.test_feed import add_proposal, seed, settings_with
from tests.fakes_api import FakeFeed, make_services, test_core
from trader.api.deps import FeedMessage, current_user
from trader.api.errors import install_error_handlers
from trader.api.feed import WATERMARK_TOPICS, PollingChangeFeed
from trader.api.routers import stream
from trader.api.schemas import StreamHello
from trader.db import models as m
from trader.market.clock import FixedClock, RealClock

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
HELLO = StreamHello(server_time=NOW)
WAIT = 5.0


class FakeTime:
    """A fake `sleep` that yields to the loop, then moves fake time forward."""

    def __init__(self) -> None:
        self.t = 0.0

    async def __call__(self, seconds: float) -> None:
        await asyncio.sleep(0)
        self.t += seconds


def always(value: bool) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return value

    return check


async def take(gen: AsyncIterator[str], n: int) -> list[str]:
    return [await asyncio.wait_for(gen.__anext__(), WAIT) for _ in range(n)]


# --- 7. the stream's frames ------------------------------------------------------------------------------


async def test_stream_starts_with_retry_hello_and_full_invalidate_then_relays() -> None:
    feed = FakeFeed()
    fake = FakeTime()
    gen = stream.event_stream(feed, hello=HELLO, session_valid=always(True), sleep=fake)
    first = await take(gen, 3)
    assert first[0] == "retry: 3000\n\n"
    assert first[1] == f"event: hello\ndata: {HELLO.model_dump_json()}\n\n"
    assert first[2].startswith("event: invalidate\ndata: ")
    assert '"topics":' in first[2] and all(f'"{t}"' in first[2] for t in WATERMARK_TOPICS)
    assert feed.subscriber_count() == 1
    feed.push(FeedMessage("invalidate", {"topics": ["proposals"]}))
    feed.push(FeedMessage("events", {"items": [{"id": 1, "message": "hi"}]}))
    frames = [f for f in await take(gen, 4) if not f.startswith(":")][:2]
    assert frames[0] == 'event: invalidate\ndata: {"topics": ["proposals"]}\n\n'
    assert frames[1] == 'event: events\ndata: {"items": [{"id": 1, "message": "hi"}]}\n\n'
    await gen.aclose()  # the client went away
    assert feed.subscriber_count() == 0


async def test_keepalive_every_15_seconds_of_fake_time() -> None:
    feed = FakeFeed()
    fake = FakeTime()
    gen = stream.event_stream(feed, hello=HELLO, session_valid=always(True), sleep=fake)
    await take(gen, 3)
    assert await asyncio.wait_for(gen.__anext__(), WAIT) == ": keepalive\n\n"
    assert 15 <= fake.t < 16
    assert await asyncio.wait_for(gen.__anext__(), WAIT) == ": keepalive\n\n"
    assert 30 <= fake.t < 31
    await gen.aclose()
    assert feed.subscriber_count() == 0


async def test_stream_ends_when_the_feed_stops_and_unsubscribes() -> None:
    feed = FakeFeed()
    gen = stream.event_stream(feed, hello=HELLO, session_valid=always(True), sleep=FakeTime())
    await take(gen, 3)
    feed.close()
    rest = [f async for f in gen]
    assert all(f == ": keepalive\n\n" for f in rest)
    assert feed.subscriber_count() == 0


async def test_stream_ends_when_the_server_is_exiting() -> None:
    feed = FakeFeed()
    exiting = False
    gen = stream.event_stream(
        feed, hello=HELLO, session_valid=always(True), sleep=FakeTime(), exiting=lambda: exiting
    )
    await take(gen, 3)
    exiting = True
    rest = [f async for f in gen]
    assert len(rest) <= 1
    assert feed.subscriber_count() == 0


# --- 8. revoked sessions and the stream limit --------------------------------------------------------------


def add_session(s: Session, clock: FixedClock) -> int:
    now = clock.now()
    user = m.User(
        username="stephen",
        password_hash="x",
        failed_logins=0,
        created_at=now,
        updated_at=now,
        password_changed_at=now,
    )
    s.add(user)
    s.flush()
    ws = m.WebSession(
        user_id=user.id,
        token_hash="a" * 64,
        csrf_token="c" * 32,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=30),
    )
    s.add(ws)
    s.flush()
    return ws.id


@pytest.mark.db
def test_session_valid_rules(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    with db_factory() as s:
        sid = add_session(s, clock)
        s.commit()
    assert stream.session_valid(db_factory, clock, sid, idle_hours=168)
    assert not stream.session_valid(db_factory, clock, sid + 99, idle_hours=168)  # no such session
    clock.advance(timedelta(hours=2))
    assert not stream.session_valid(db_factory, clock, sid, idle_hours=1)  # idle too long
    clock.set(NOW + timedelta(days=31))
    assert not stream.session_valid(db_factory, clock, sid, idle_hours=24 * 60)  # expired
    clock.set(NOW)
    with db_factory() as s:
        s.execute(update(m.WebSession).where(m.WebSession.id == sid).values(revoked_at=NOW))
        s.commit()
    assert not stream.session_valid(db_factory, clock, sid, idle_hours=168)  # revoked


@pytest.mark.db
async def test_revoked_session_ends_the_stream_within_60_seconds(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    with db_factory() as s:
        sid = add_session(s, clock)
        s.commit()
    checks: list[float] = []
    fake = FakeTime()

    async def valid() -> bool:
        checks.append(fake.t)
        return await asyncio.to_thread(stream.session_valid, db_factory, clock, sid, 168)

    feed = FakeFeed()
    gen = stream.event_stream(feed, hello=HELLO, session_valid=valid, sleep=fake)
    await take(gen, 3)
    with db_factory() as s:
        s.execute(update(m.WebSession).where(m.WebSession.id == sid).values(revoked_at=NOW))
        s.commit()
    rest = [f async for f in gen]
    assert all(f == ": keepalive\n\n" for f in rest)
    assert fake.t <= 60 + 1
    assert checks and checks[-1] <= 60 + 1
    assert feed.subscriber_count() == 0


# --- the real server --------------------------------------------------------------------------------------


@dataclass
class LiveServer:
    base_url: str
    server: uvicorn.Server
    thread: threading.Thread
    feed: Any
    loop: list[asyncio.AbstractEventLoop] = field(default_factory=list)

    def exit(self) -> None:
        """What SIGTERM does: uvicorn's `handle_exit`, run on the server's loop."""
        self.loop[0].call_soon_threadsafe(self.server.handle_exit, signal.SIGTERM, None)


def build_app(services: Any, loops: list[asyncio.AbstractEventLoop]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loops.append(asyncio.get_running_loop())
        stop = asyncio.Event()
        task = asyncio.create_task(services.feed.run(stop))
        yield
        stop.set()
        await asyncio.wait_for(task, 5)

    app = FastAPI(lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(stream.router, prefix="/api")
    app.state.services = services
    app.dependency_overrides[current_user] = lambda: DEFAULT_USER
    return app


@contextmanager
def live_server(db_factory: sessionmaker[Session], poll: float = 0.5) -> Iterator[LiveServer]:
    clock = RealClock()
    feed = PollingChangeFeed(db_factory, clock, settings_with(poll))
    services = make_services(test_core(db_factory, clock), feed=feed)
    loops: list[asyncio.AbstractEventLoop] = []
    app = build_app(services, loops)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=10
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + WAIT
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    live = LiveServer(f"http://127.0.0.1:{port}", server, thread, feed, loops)
    try:
        yield live
    finally:
        if thread.is_alive():
            server.should_exit = True
            server.force_exit = True
            thread.join(WAIT + 10)
        sock.close()


def open_stream(stack: ExitStack, client: httpx.Client) -> tuple[httpx.Response, Iterator[str]]:
    """Open `/api/stream` and read its first three frames (retry, hello, the full invalidate)."""
    resp = stack.enter_context(client.stream("GET", "/api/stream"))
    lines = resp.iter_lines()
    if resp.status_code == 200:
        seen: list[str] = []
        while not (seen and seen[-1].startswith("data: ") and "topics" in seen[-1]):
            seen.append(next(lines))
        assert seen[0] == "retry: 3000"
        assert "event: hello" in seen and "event: invalidate" in seen
    return resp, lines


def wait_until(check: Callable[[], bool], seconds: float = WAIT) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


@pytest.mark.db
def test_real_server_headers_limit_and_disconnect(db_factory: sessionmaker[Session]) -> None:
    with live_server(db_factory) as live, httpx.Client(base_url=live.base_url, timeout=WAIT) as client:
        with ExitStack() as stack:
            streams = [open_stream(stack, client) for _ in range(stream.MAX_STREAMS)]
            resp = streams[0][0]
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["cache-control"] == "no-cache"
            assert resp.headers["x-accel-buffering"] == "no"
            assert live.feed.subscriber_count() == stream.MAX_STREAMS
            eleventh = client.get("/api/stream")
            assert eleventh.status_code == 429
            assert eleventh.json()["error"]["code"] == "too_many_requests"
        # every client went away: every subscription ends
        assert wait_until(lambda: live.feed.subscriber_count() == 0)
        live.exit()
        live.thread.join(WAIT)
        assert not live.thread.is_alive()


@pytest.mark.db
def test_real_server_new_proposal_within_2s_and_shutdown_within_5s(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id, _, _, sig = seed(s)
        s.commit()
    with (
        live_server(db_factory, poll=0.5) as live,
        httpx.Client(base_url=live.base_url, timeout=20) as client,
    ):
        with ExitStack() as stack:
            _, lines = open_stream(stack, client)
            with db_factory() as s:
                add_proposal(s, run_id, sig)
                s.commit()
            t0 = time.monotonic()
            event = None
            for line in lines:
                if line.startswith("event: "):
                    event = line
                elif event == "event: invalidate" and line.startswith("data: ") and "proposals" in line:
                    break
            assert time.monotonic() - t0 < 2.0
            live.exit()
            t1 = time.monotonic()
            for _ in lines:  # the stream ends
                pass
            assert time.monotonic() - t1 < 5.0
        live.thread.join(WAIT)
        assert not live.thread.is_alive()
        assert time.monotonic() - t1 < 5.0 + 1
