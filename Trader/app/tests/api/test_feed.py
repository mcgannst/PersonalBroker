"""P4-T11 acceptance tests 1-6: the polling change feed (`trader.api.feed`), on the real test database with a
fake sleep driving the poll loop one step at a time."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import structlog
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from trader.api.deps import ChangeFeed, FeedMessage
from trader.api.feed import WATERMARK_TOPICS, PollingChangeFeed, watermarks
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings, SettingsStore

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
DAY = date(2026, 10, 6)
WAIT = 5.0  # real seconds a test waits for the feed before failing


class StepSleep:
    """A fake `sleep`: every call blocks until the test releases it, so the test runs the feed loop one
    iteration at a time. `calls` records the requested durations."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._pending: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()
        self._current: asyncio.Future[None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._pending.put(fut)
        await fut

    async def started(self) -> None:
        """Wait until the feed is in its first sleep."""
        self._current = await asyncio.wait_for(self._pending.get(), WAIT)

    async def step(self) -> None:
        """Release the current sleep and wait until the feed has run one more iteration and sleeps again."""
        assert self._current is not None, "call started() first"
        self._current.set_result(None)
        self._current = await asyncio.wait_for(self._pending.get(), WAIT)


def settings_with(poll: float = 1.0) -> Callable[[], RuntimeSettings]:
    rs = RuntimeSettings.model_validate({"web.sse_poll_seconds": poll})
    return lambda: rs


class CountingFactory:
    """A session factory that counts sessions and can be told to fail the next `fail` sessions."""

    def __init__(self, inner: sessionmaker[Session]) -> None:
        self.inner = inner
        self.count = 0
        self.fail = 0

    def __call__(self) -> Session:
        self.count += 1
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("connection refused")
        return self.inner()


async def next_msg(it: AsyncIterator[FeedMessage]) -> FeedMessage:
    return await asyncio.wait_for(it.__anext__(), WAIT)


async def no_msg(it: AsyncIterator[FeedMessage]) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(it.__anext__(), 0.05)


async def drain(it: AsyncIterator[FeedMessage]) -> list[FeedMessage]:
    out: list[FeedMessage] = []
    while True:
        try:
            out.append(await asyncio.wait_for(it.__anext__(), 0.05))
        except TimeoutError:
            return out


def topics_of(msgs: list[FeedMessage]) -> set[str]:
    return {t for msg in msgs if msg.kind == "invalidate" for t in msg.data["topics"]}


# --- seed rows ------------------------------------------------------------------------------------------


def seed(s: Session) -> tuple[int, int, int, int]:
    """(run id, symbol id, strategy config id, signal id)."""
    run_id = add_run(s)
    sym = add_symbol(s)
    cfg = add_strategy_config(s)
    sig = m.Signal(
        run_id=run_id,
        strategy_config_id=cfg,
        symbol_id=sym,
        session_date=DAY,
        event_key="orb_935",
        ts=NOW,
        intent={},
        evidence={},
    )
    s.add(sig)
    s.flush()
    return run_id, sym, cfg, sig.id


def add_proposal(s: Session, run_id: int, signal_id: int, status: str = "pending") -> int:
    p = m.Proposal(
        run_id=run_id,
        signal_id=signal_id,
        kind="entry",
        order_spec={},
        qty=10,
        status=status,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        escalations=0,
    )
    s.add(p)
    s.flush()
    return p.id


def add_order(s: Session, run_id: int, symbol_id: int) -> int:
    o = m.Order(
        run_id=run_id,
        symbol_id=symbol_id,
        side="buy",
        order_type="market",
        purpose="entry",
        qty=10,
        tif="day",
        status="working",
        reason="entry",
        session_date=DAY,
        submitted_at=NOW,
        stale_alerted=False,
    )
    s.add(o)
    s.flush()
    return o.id


def add_event(s: Session, level: str, message: str) -> int:
    ev = m.EventLog(ts=NOW, level=level, source="test", run_id=None, message=message, data=None)
    s.add(ev)
    s.flush()
    return ev.id


def beat(s: Session, phase: str, at: datetime) -> None:
    row = s.get(m.WorkerHeartbeat, "worker")
    if row is None:
        s.add(
            m.WorkerHeartbeat(
                process="worker", pid=1, host="h", started_at=at, beat_at=at, session_date=DAY, phase=phase
            )
        )
    else:
        row.beat_at, row.phase = at, phase


class Running:
    """The feed's `run(stop)` as a task, stopped and awaited on exit."""

    def __init__(self, feed: PollingChangeFeed) -> None:
        self.feed = feed
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "Running":
        self.task = asyncio.create_task(self.feed.run(self.stop))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.stop.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, WAIT)


# --- the watermark query ----------------------------------------------------------------------------------


def test_watermarks_cover_every_topic_in_one_statement(db_factory: sessionmaker[Session]) -> None:
    statements: list[str] = []
    from sqlalchemy import event

    engine = db_factory.kw["bind"]

    def record(*args: Any) -> None:
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", record)
    try:
        with db_factory() as s:
            marks = watermarks(s)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert tuple(marks) == WATERMARK_TOPICS
    assert len(statements) == 1
    assert marks["proposals"] == (None, None, None, 0)


def test_polling_feed_is_a_change_feed(db_factory: sessionmaker[Session]) -> None:
    feed: ChangeFeed = PollingChangeFeed(db_factory, FixedClock(NOW), settings_with())
    assert feed.subscriber_count() == 0


# --- 1. a new proposal ------------------------------------------------------------------------------------


async def test_new_proposal_gives_one_invalidate_on_the_next_poll(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id, _, _, sig = seed(s)
        s.commit()
    sleep = StepSleep()
    feed = PollingChangeFeed(db_factory, FixedClock(NOW), settings_with(0.5), sleep=sleep)
    async with feed.subscribe() as it, Running(feed):
        await sleep.started()  # the first poll set the baseline
        await no_msg(it)
        with db_factory() as s:
            add_proposal(s, run_id, sig)
            s.commit()
        await sleep.step()
        msgs = await drain(it)
        assert msgs == [FeedMessage("invalidate", {"topics": ["proposals"]})]
        assert sleep.calls[-1] == 0.5  # polled within one web.sse_poll_seconds
        await sleep.step()
        await no_msg(it)  # nothing changed since


# --- 2. each kind of change -------------------------------------------------------------------------------


async def test_each_change_invalidates_its_topic(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    with db_factory() as s:
        run_id, sym, _, sig = seed(s)
        pid = add_proposal(s, run_id, sig)
        pid2 = add_proposal(s, run_id, sig)
        oid = add_order(s, run_id, sym)
        ks = m.KillSwitchEvent(run_id=run_id, switch="manual_pause", session_date=DAY, tripped_at=NOW)
        s.add(ks)
        beat(s, "session", NOW)
        s.commit()
        ks_id = ks.id
    sleep = StepSleep()
    feed = PollingChangeFeed(db_factory, clock, settings_with(), sleep=sleep)

    async def after(change: Callable[[Session], None]) -> set[str]:
        with db_factory() as s:
            change(s)
            s.commit()
        await sleep.step()
        return topics_of(await drain(it))

    async with feed.subscribe() as it, Running(feed):
        await sleep.started()
        decided = await after(
            lambda s: s.execute(
                update(m.Proposal).where(m.Proposal.id == pid).values(status="approved", decided_at=NOW)
            )
        )
        assert "proposals" in decided
        expired = await after(
            lambda s: s.execute(
                update(m.Proposal).where(m.Proposal.id == pid2).values(status="expired", expired_at=NOW)
            )
        )
        assert "proposals" in expired

        def fill(s: Session) -> None:
            s.add(
                m.Fill(
                    run_id=run_id,
                    order_id=oid,
                    ts=NOW,
                    qty=10,
                    price=Decimal("10"),
                    fees={},
                    quote_snapshot={},
                    slippage=Decimal("0"),
                )
            )

        assert "fills" in await after(fill)
        reset = await after(
            lambda s: s.execute(
                update(m.KillSwitchEvent)
                .where(m.KillSwitchEvent.id == ks_id)
                .values(reset_at=NOW, reset_reason="ok", reset_by="web:stephen")
            )
        )
        assert "killswitch" in reset

        def journal(s: Session) -> None:
            s.add(m.Journal(run_id=run_id, session_date=DAY, rules_followed=True, updated_at=NOW))

        assert "journal" in await after(journal)
        clock.advance(timedelta(seconds=5))
        store = SettingsStore(db_factory, now=clock.now)
        assert "settings" in await after(lambda s: store.set("approval_mode", "auto", "web:stephen"))
        heartbeat = await after(lambda s: beat(s, "session", NOW + timedelta(seconds=30)))
        assert heartbeat == {"system"}


# --- 3. new events ----------------------------------------------------------------------------------------


async def test_new_events_are_sent_oldest_first_without_debug(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        add_event(s, "info", "before")  # existing rows are never sent
        s.commit()
    sleep = StepSleep()
    feed = PollingChangeFeed(db_factory, FixedClock(NOW), settings_with(), sleep=sleep)
    async with feed.subscribe() as it, Running(feed):
        await sleep.started()
        with db_factory() as s:
            add_event(s, "info", "one")
            add_event(s, "debug", "noise")
            add_event(s, "warning", "two")
            add_event(s, "error", "three token=abc123secret")
            s.commit()
        await sleep.step()
        msgs = await drain(it)
    events = [msg for msg in msgs if msg.kind == "events"]
    assert len(events) == 1
    items = events[0].data["items"]
    assert [i["message"][:5] for i in items] == ["one", "two", "three"]
    assert [i["level"] for i in items] == ["info", "warning", "error"]
    assert "abc123secret" not in str(items)  # EventOut masks secrets
    assert isinstance(items[0]["ts"], str)  # JSON-ready
    assert "events" in topics_of(msgs)


# --- 4. idle: no queries, then a baseline -----------------------------------------------------------------


async def test_no_queries_without_subscribers_and_first_poll_is_baseline(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        run_id, _, _, sig = seed(s)
        s.commit()
    counting = CountingFactory(db_factory)
    sleep = StepSleep()
    feed = PollingChangeFeed(counting, FixedClock(NOW), settings_with(), sleep=sleep)  # type: ignore[arg-type]
    async with Running(feed):
        await sleep.started()
        for _ in range(3):
            await sleep.step()
        assert counting.count == 0
        with db_factory() as s:
            add_proposal(s, run_id, sig)  # changed while nobody listened
            s.commit()
        async with feed.subscribe() as it:
            assert feed.subscriber_count() == 1
            await sleep.step()
            assert counting.count >= 1
            await no_msg(it)  # the first poll only set the baseline
            with db_factory() as s:
                add_proposal(s, run_id, sig)
                s.commit()
            await sleep.step()
            assert topics_of(await drain(it)) == {"proposals"}
        assert feed.subscriber_count() == 0
        # idle again: the next subscriber starts from a fresh baseline
        before = counting.count
        await sleep.step()
        await sleep.step()
        assert counting.count == before


# --- 5. a slow subscriber ---------------------------------------------------------------------------------


async def test_slow_subscriber_is_bounded_and_resyncs(db_factory: sessionmaker[Session]) -> None:
    feed = PollingChangeFeed(db_factory, FixedClock(NOW), settings_with(), queue_size=100)
    got: list[FeedMessage] = []
    async with feed.subscribe() as slow, feed.subscribe() as fast:
        sent = [FeedMessage("invalidate", {"topics": ["events"], "n": i}) for i in range(150)]
        for msg in sent:
            feed.publish(msg)
            got.append(await next_msg(fast))
        assert got == sent  # the reader gets every message
        pending = await drain(slow)
    assert len(pending) <= 100
    assert pending[-1] == FeedMessage("invalidate", {"topics": list(WATERMARK_TOPICS)})
    assert sum(1 for msg in pending if msg.data["topics"] == list(WATERMARK_TOPICS)) == 1
    assert feed.subscriber_count() == 0


async def test_slow_subscriber_gets_new_messages_after_reading_the_resync(
    db_factory: sessionmaker[Session],
) -> None:
    feed = PollingChangeFeed(db_factory, FixedClock(NOW), settings_with(), queue_size=3)
    async with feed.subscribe() as it:
        for i in range(5):
            feed.publish(FeedMessage("invalidate", {"topics": ["jobs"], "n": i}))
        assert (await next_msg(it)).data["topics"] == list(WATERMARK_TOPICS)
        feed.publish(FeedMessage("invalidate", {"topics": ["jobs"], "n": 9}))
        assert (await next_msg(it)).data == {"topics": ["jobs"], "n": 9}


# --- 6. the database failing ------------------------------------------------------------------------------


async def test_db_failure_logs_once_and_recovers(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id, _, _, sig = seed(s)
        s.commit()
    flaky = CountingFactory(db_factory)
    sleep = StepSleep()
    feed = PollingChangeFeed(flaky, FixedClock(NOW), settings_with(), sleep=sleep)  # type: ignore[arg-type]
    with structlog.testing.capture_logs() as logs:
        async with feed.subscribe() as it, Running(feed) as running:
            await sleep.started()  # baseline
            flaky.fail = 3
            with db_factory() as s:
                add_proposal(s, run_id, sig)
                s.commit()
            for _ in range(3):
                await sleep.step()
            assert flaky.fail == 0
            await no_msg(it)
            await sleep.step()  # the fourth poll works and sees the change made during the outage
            assert topics_of(await drain(it)) == {"proposals"}
            assert running.task is not None and not running.task.done()  # the loop never died
    failures = [e for e in logs if e["event"] == "feed.poll_failed"]
    assert len(failures) == 1
    assert "connection refused" not in str(failures[0])  # the type only, never the text
    assert any(e["event"] == "feed.poll_recovered" for e in logs)


# --- stop -------------------------------------------------------------------------------------------------


async def test_stop_ends_every_subscription_and_new_ones(db_factory: sessionmaker[Session]) -> None:
    sleep = StepSleep()
    feed = PollingChangeFeed(db_factory, FixedClock(NOW), settings_with(), sleep=sleep)
    stop = asyncio.Event()
    async with feed.subscribe() as it:
        task = asyncio.create_task(feed.run(stop))
        await sleep.started()
        stop.set()
        await asyncio.wait_for(task, WAIT)  # the pending sleep does not hold it up
        with pytest.raises(StopAsyncIteration):
            await next_msg(it)
    async with feed.subscribe() as late:
        with pytest.raises(StopAsyncIteration):
            await next_msg(late)
    assert feed.subscriber_count() == 0
