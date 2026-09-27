"""The change feed behind SSE: while a browser is connected, one watermark query every
`web.sse_poll_seconds`; changed topics become `invalidate` messages and new `event_log` rows `events`
messages (decision "SSE by polling, not LISTEN/NOTIFY").

- `watermarks(s)` reads every topic's watermark with ONE statement of scalar subqueries. The `max(id)` ones
  use primary keys; the timestamp ones scan tables that stay small; `events` is `max(id)` only, so the growing
  `event_log` is never scanned.
- `PollingChangeFeed.run(stop)` polls only while someone is subscribed. The baseline is read when the first
  subscriber arrives (before its stream sends the full `invalidate`, so nothing changed after the client's
  refetch can be missed); with no subscribers the loop sleeps without querying. A DB error is logged once per
  failure streak and retried; the loop never dies. When `stop` is set every subscription ends.
- Each subscriber has a bounded queue. A full queue drops what is pending and holds one `invalidate` of every
  topic instead (the client resyncs); later messages are dropped until that one is read.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from trader.api.deps import FeedMessage
from trader.api.schemas import Topic
from trader.api.views import event_out
from trader.db import models as m
from trader.market.clock import Clock
from trader.notify.views import WORKER_PROCESS
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("api.feed")

# The topics the watermark query covers (every Topic).
WATERMARK_TOPICS: tuple[Topic, ...] = (
    "proposals",
    "orders",
    "fills",
    "positions",
    "trades",
    "candidates",
    "killswitch",
    "events",
    "journal",
    "jobs",
    "settings",
    "strategies",
    "system",
)

MAX_EVENTS = 50  # new event_log rows per `events` message (the newest ones)
EVENT_LEVELS = ("info", "warning", "error", "critical")  # `debug` rows are never pushed
DEFAULT_POLL_SECONDS = 1.0


def _max(col: InstrumentedAttribute[Any]) -> Select[Any]:
    return select(func.max(col))


def _watermark_columns() -> dict[Topic, tuple[Select[Any], ...]]:
    hb = m.WorkerHeartbeat
    worker = hb.process == WORKER_PROCESS
    return {
        "proposals": (
            _max(m.Proposal.id),
            _max(m.Proposal.decided_at),
            _max(m.Proposal.expired_at),
            select(func.count()).select_from(m.Proposal).where(m.Proposal.status == "pending"),
        ),
        "orders": (_max(m.Order.id), _max(m.Order.closed_at)),
        "fills": (_max(m.Fill.id),),
        "positions": (_max(m.Position.id), _max(m.Position.closed_at), _max(m.Position.unprotected_since)),
        "trades": (_max(m.Trade.id),),
        "candidates": (_max(m.Candidate.id),),
        "killswitch": (_max(m.KillSwitchEvent.id), _max(m.KillSwitchEvent.reset_at)),
        "events": (_max(m.EventLog.id),),
        "journal": (_max(m.Journal.updated_at),),
        "jobs": (_max(m.JobRun.id), _max(m.JobRun.finished_at)),
        "settings": (_max(m.Setting.updated_at),),
        "strategies": (_max(m.StrategyConfig.id),),
        "system": (select(hb.phase).where(worker), select(hb.beat_at).where(worker)),
    }


def watermarks(s: Session) -> dict[Topic, tuple[Any, ...]]:
    """Every topic's watermark, read with one SQL statement."""
    columns = _watermark_columns()
    stmt = select(*(q.scalar_subquery() for topic in WATERMARK_TOPICS for q in columns[topic]))
    row = s.execute(stmt).one()
    out: dict[Topic, tuple[Any, ...]] = {}
    i = 0
    for topic in WATERMARK_TOPICS:
        n = len(columns[topic])
        out[topic] = tuple(row[i : i + n])
        i += n
    return out


def _new_events(s: Session, after_id: int, up_to_id: int) -> list[dict[str, Any]]:
    """The newest `MAX_EVENTS` rows with `after_id < id <= up_to_id` at level info or above, oldest first,
    as JSON-ready `EventOut` dicts (masked by `event_out`)."""
    rows = (
        s.execute(
            select(m.EventLog)
            .where(m.EventLog.id > after_id, m.EventLog.id <= up_to_id, m.EventLog.level.in_(EVENT_LEVELS))
            .order_by(m.EventLog.id.desc())
            .limit(MAX_EVENTS)
        )
        .scalars()
        .all()
    )
    return [event_out(r).model_dump(mode="json") for r in reversed(rows)]


def full_invalidate() -> FeedMessage:
    """An `invalidate` of every topic: the client refetches everything."""
    return FeedMessage("invalidate", {"topics": list(WATERMARK_TOPICS)})


class _Subscriber:
    """One subscription: a bounded queue read through `async for`. `None` in the queue ends the iteration.
    A class iterator (not an async generator), so a cancelled `__anext__` loses nothing."""

    def __init__(self, size: int) -> None:
        self._queue: asyncio.Queue[FeedMessage | None] = asyncio.Queue(maxsize=max(size, 1))
        self._resync = False  # the queue holds only the full invalidate
        self._closed = False

    def put(self, msg: FeedMessage) -> None:
        if self._closed:
            return
        if self._resync:
            if not self._queue.empty():
                return  # the pending full invalidate covers it
            self._resync = False
        if self._queue.full():
            self._clear()
            self._queue.put_nowait(full_invalidate())
            self._resync = True
            return
        self._queue.put_nowait(msg)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._queue.full():
            self._clear()
        self._queue.put_nowait(None)

    def qsize(self) -> int:
        return self._queue.qsize()

    def _clear(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    def __aiter__(self) -> "_Subscriber":
        return self

    async def __anext__(self) -> FeedMessage:
        msg = await self._queue.get()
        if msg is None:
            self._queue.put_nowait(None)  # stays ended
            raise StopAsyncIteration
        return msg


class PollingChangeFeed:
    """Implements `trader.api.deps.ChangeFeed`."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        settings: Callable[[], RuntimeSettings],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        queue_size: int = 100,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._settings = settings
        self._sleep = sleep
        self._queue_size = queue_size
        self._subs: list[_Subscriber] = []
        self._marks: dict[Topic, tuple[Any, ...]] | None = None  # the baseline; None while idle
        self._last_event_id = 0
        self._lock = asyncio.Lock()
        self._stopped = False
        self._failures = 0

    # --- subscriptions --------------------------------------------------------------------------------------

    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[FeedMessage]]:
        return self._subscription()

    @asynccontextmanager
    async def _subscription(self) -> AsyncIterator[AsyncIterator[FeedMessage]]:
        sub = _Subscriber(self._queue_size)
        if self._stopped:
            sub.close()
        self._subs.append(sub)
        try:
            if not self._stopped:
                await self._ensure_baseline()
            yield sub
        finally:
            # Synchronous on purpose: this runs even inside a cancelled scope.
            sub.close()
            if sub in self._subs:
                self._subs.remove(sub)
            if not self._subs:
                self._marks = None  # idle: the next subscriber starts from a fresh baseline

    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, msg: FeedMessage) -> None:
        """Send one message to every subscriber (the poll loop's output; public for tests and other
        sources)."""
        for sub in list(self._subs):
            sub.put(msg)

    # --- the loop -------------------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        self._stopped = False
        try:
            while not stop.is_set():
                if self._subs:
                    await self.poll()
                await self._pause(stop, self._interval())
        finally:
            self._stopped = True
            for sub in list(self._subs):
                sub.close()

    async def poll(self) -> None:
        """One poll: set the baseline, or compare with it and publish what changed."""
        async with self._lock:
            baseline = self._marks is None
            try:
                marks, events = await asyncio.to_thread(self._read, self._marks, self._last_event_id)
            except Exception as exc:
                self._failures += 1
                if self._failures == 1:
                    log.warning("feed.poll_failed", error_type=type(exc).__name__)
                return
            if self._failures:
                log.info("feed.poll_recovered", failures=self._failures)
                self._failures = 0
            if not self._subs:
                return  # everyone left while reading
            previous = self._marks
            self._marks = marks
            self._last_event_id = _event_mark(marks)
            if baseline or previous is None:
                return
            changed = [t for t in WATERMARK_TOPICS if marks[t] != previous[t]]
        if changed:
            self.publish(FeedMessage("invalidate", {"topics": changed}))
        if events:
            self.publish(FeedMessage("events", {"items": events}))

    async def _ensure_baseline(self) -> None:
        if self._marks is None:
            await self.poll()

    def _read(
        self, previous: dict[Topic, tuple[Any, ...]] | None, last_event_id: int
    ) -> tuple[dict[Topic, tuple[Any, ...]], list[dict[str, Any]]]:
        with self._factory() as s:
            marks = watermarks(s)
            events: list[dict[str, Any]] = []
            if previous is not None and _event_mark(marks) > last_event_id:
                events = _new_events(s, last_event_id, _event_mark(marks))
            return marks, events

    def _interval(self) -> float:
        try:
            return float(self._settings().web_sse_poll_seconds)
        except Exception as exc:
            log.warning("feed.settings_unusable", error_type=type(exc).__name__)
            return DEFAULT_POLL_SECONDS

    async def _pause(self, stop: asyncio.Event, seconds: float) -> None:
        """Sleep `seconds`, or less when `stop` is set meanwhile."""
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (sleeper, stopper):
                t.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)


def _event_mark(marks: dict[Topic, tuple[Any, ...]]) -> int:
    value = marks["events"][0]
    return int(value) if value is not None else 0
