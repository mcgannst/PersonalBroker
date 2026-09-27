"""P5-T15: the worker is hard-killed in market hours and restarted; nothing is duplicated.

Built on P3-T13's whole-day harness (`tests/integration/test_worker_day.py`, imported, not edited): the
real composition root (`rt.run_worker()`), fake Questrade / Telegram / FinViz / Claude, a FixedClock and the
testcontainers database. The harness replaces `Worker.run`'s loop, so each script takes the worker's real
single-instance lock itself (`acquire_single_instance`, as `Worker.run` does first).

A hard kill (SIGKILL, an OOM kill) runs nothing: no `Worker._shutdown` (no `stopping`/`stopped` heartbeat,
no unlock), no job or notifier bookkeeping after the instant it lands. `HardKill` models it: a
BaseException nothing in `trader/` catches, raised after the dying worker's lock connection is dropped
(the server then frees the lock, as it does when the process dies). What an in-process exception cannot
leave behind is written as the crash left it: the `event:orb_open` job row still `running` (a kill inside
the event body; in-process, run_job records every exception).

The main day has two kills. Worker A is killed at 09:35:40 with the approved entry order working and its
orb_open row `running`. Worker B starts at 09:35:50 and is killed at 09:36:00 inside the relay's send of
the ENTRY FILLED message, after Telegram delivered it and before the notifier recorded it: its
`notifications` row is left `sending` and the relay's `fills` cursor behind that fill. Worker C finishes
the day. A second test kills A while the entry proposal waits for a tap, and checks it expires on time.
"""

import time as wall
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, func, select

from tests.integration.test_worker_day import (
    DAY,
    Driver,
    Sent,
    World,
    afternoon,
    approve,
    assert_ends_flat,
    checkin,
    day_times,
    et,
    evening,
    label,
    midday,
    morning_jobs,
    notifications,
    pre_open,
    proposal_statuses,
    quote,
    to_orb,
    with_worker,
    working,
)
from tests.integration.test_worker_day import world as world  # the P3 fixture, used as is
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs.runner import OUTCOME_UNKNOWN
from trader.worker import acquire_single_instance, release_single_instance

pytestmark = pytest.mark.db


class HardKill(BaseException):
    """The worker process dies here: nothing in `trader/` catches a BaseException it does not know."""


# --- the process: its lock, its death -----------------------------------------------------------------------


def take_lock(d: Driver, at: datetime) -> Connection:
    """What `Worker.run` does first, at `at`: the single-instance lock. A new worker gets it once the old
    process is gone; the server frees a dead session's lock as soon as it notices the disconnect, so poll
    briefly."""
    d.w.clock.set(at)
    engine = d.w.core.engine
    deadline = wall.monotonic() + 5.0
    while True:
        conn = acquire_single_instance(engine)
        if conn is not None:
            d.worker._lock = conn
            d.worker._started_at = d.w.clock.now()
            _held.append(conn)
            return conn
        assert wall.monotonic() < deadline, "the dead worker's lock was never freed"
        wall.sleep(0.05)


def stop(d: Driver) -> None:
    """A clean stop's last step (`Worker._shutdown`): the lock is released."""
    assert d.worker._lock is not None
    release_single_instance(d.worker._lock)
    d.worker._lock = None


_held: list[Connection] = []


@pytest.fixture(autouse=True)
def _no_lock_outlives_the_test() -> Iterator[None]:
    """Whatever a test ends with, no worker lock is left for the next test (the engine is shared)."""
    yield
    while _held:
        conn = _held.pop()
        if not conn.closed:
            conn.invalidate()
            conn.close()


def die(d: Driver) -> None:
    """The process is gone: its lock connection drops without an unlock (the server frees the lock), and
    nothing else runs."""
    conn = d.worker._lock
    assert conn is not None
    assert acquire_single_instance(d.w.core.engine) is None  # while it lived, no second worker could start
    conn.invalidate()
    raise HardKill


def orb_open_left_running(w: World) -> None:
    """The footprint of a kill inside the orb_open body: its job row stays `running`."""
    with session_scope(w.factory) as s:
        row = s.execute(
            select(m.JobRun).where(m.JobRun.job == "event:orb_open", m.JobRun.session_date == DAY)
        ).scalar_one()
        row.status, row.finished_at = "running", None


def kill_after_delivering(w: World, prefix: str, d: Callable[[], Driver]) -> None:
    """The next message whose title starts with `prefix` reaches the chat, and the process dies before the
    notifier can record it."""
    api = w.api
    original = api.send_message

    async def send_message(chat_id: int, text: str, buttons: Any = (), silent: bool = False) -> int:
        message_id = await original(chat_id, text, buttons, silent)
        if label(text).startswith(prefix):
            w.monkeypatch.setattr(api, "send_message", original)
            die(d())
        return message_id

    w.monkeypatch.setattr(api, "send_message", send_message)


async def killed(w: World, script: Callable[[Driver], Awaitable[None]]) -> None:
    with pytest.raises(HardKill):
        await with_worker(w, script)


# --- reading the database -----------------------------------------------------------------------------------


def heartbeat(w: World) -> tuple[str, datetime, datetime]:
    with w.factory() as s:
        hb = s.get_one(m.WorkerHeartbeat, "worker")
        return hb.phase, hb.beat_at, hb.started_at


def job_rows(w: World, job: str) -> list[tuple[str, str | None]]:
    with w.factory() as s:
        rows = s.execute(
            select(m.JobRun.status, m.JobRun.error).where(m.JobRun.job == job).order_by(m.JobRun.id)
        ).all()
    return [(st, err) for st, err in rows]


def events_of(w: World, source: str) -> list[tuple[str, str]]:
    with w.factory() as s:
        rows = s.execute(
            select(m.EventLog.level, m.EventLog.message)
            .where(m.EventLog.source == source)
            .order_by(m.EventLog.id)
        ).all()
    return [(lv, msg) for lv, msg in rows]


def cursor(w: World, stream: str) -> int:
    with w.factory() as s:
        return s.get_one(m.NotifyCursor, stream).last_id


def fill_ids(w: World) -> list[int]:
    with w.factory() as s:
        return list(s.execute(select(m.Fill.id).order_by(m.Fill.id)).scalars())


def order_count(w: World) -> int:
    with w.factory() as s:
        return s.execute(select(func.count()).select_from(m.Order)).scalar_one()


def notification_status(w: World, key: str) -> str:
    with w.factory() as s:
        return s.execute(select(m.Notification.status).where(m.Notification.dedupe_key == key)).scalar_one()


def count_labels(w: World) -> dict[str, int]:
    out: dict[str, int] = {}
    for lb in w.api.labels():
        out[lb] = out.get(lb, 0) + 1
    return out


# --- tests 6, 7 and 8 (flatten): the killed day -------------------------------------------------------------


async def test_hard_kills_mid_session_duplicate_nothing(world: World) -> None:
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)
    held: dict[str, Any] = {}

    async def worker_a(d: Driver) -> None:
        take_lock(d, et(9, 15))
        await pre_open(d, DAY)
        entry = await to_orb(d, DAY, t)
        d.w.clock.set(et(9, 35, 30))
        await approve(d, entry)
        assert working(world, "entry") == [("entry", Decimal("21.5100"), 33)]
        await d.at(et(9, 35, 40))
        held["entry"] = entry
        orb_open_left_running(world)
        die(d)

    await killed(world, worker_a)

    # what the kill left: the heartbeat still says `session`, orb_open `running`, the entry order working
    assert heartbeat(world)[:2] == ("session", et(9, 35, 40))
    assert job_rows(world, "event:orb_open") == [("running", None)]
    assert working(world, "entry") == [("entry", Decimal("21.5100"), 33)]
    sent_by_a = count_labels(world)

    async def worker_b(d: Driver) -> None:
        take_lock(d, et(9, 35, 50))  # A's lock is gone with A
        report = await d.at(et(9, 35, 50))
        # orb_open is not run again: its unknown outcome is settled with one critical event
        assert [(f.key, f.status) for f in report.fired] == [("orb_open", "failed")]
        assert job_rows(world, "event:orb_open") == [("failed", OUTCOME_UNKNOWN)]
        phase, beat_at, started_at = heartbeat(world)
        assert (phase, beat_at, started_at) == ("session", et(9, 35, 50), et(9, 35, 50))
        # Telegram delivers again the Approve tap A handled but never confirmed (A died before its next
        # getUpdates): answered, and nothing is submitted twice
        answers_before = len(world.api.answers())
        await d.poll()
        assert world.api.answers()[answers_before:] == ["Already answered"]
        assert working(world, "entry") == [("entry", Decimal("21.5100"), 33)]
        assert order_count(world) == 1
        await d.walk(et(9, 35, 52), et(9, 35, 58))
        assert fill_ids(world) == []
        # the entry order A left working is still polled, and fills once; the relay then sends the stop
        # proposal and the fill message, and the process dies inside the fill message's send
        held["fills_cursor"] = cursor(world, "fills")
        kill_after_delivering(world, "ENTRY FILLED", lambda: d)
        d.w.clock.set(et(9, 36))
        quote(world, "AAA", "21.52", "21.55", "21.53")
        await d.at(et(9, 36))

    await killed(world, worker_b)

    [entry_fill] = fill_ids(world)
    assert cursor(world, "fills") == held["fills_cursor"] < entry_fill  # send-then-advance: never advanced
    assert notification_status(world, f"fill:{entry_fill}") == "sending"
    assert world.api.labels()[-2:] == ["PROTECTIVE STOP: SELL 33 AAA", "ENTRY FILLED"]
    stop_msg: Sent = world.api.sent[-2]
    alerts = [
        lb for lb in world.api.labels() if lb not in sent_by_a and not lb.startswith(("PROTECTIVE", "ENTRY"))
    ]
    assert len(alerts) == 1, world.api.labels()  # the unknown orb_open outcome, alerted once

    async def worker_c(d: Driver) -> None:
        take_lock(d, et(9, 36, 10))
        sent_before = len(world.api.sent)
        report = await d.at(et(9, 36, 10))
        assert report.fired == [] and report.fills == 0  # nothing re-fired, the entry is not filled twice
        assert len(world.api.sent) == sent_before  # neither the fill message nor the stop proposal again
        assert cursor(world, "fills") >= entry_fill  # the relay moved past the fill without re-sending it
        assert heartbeat(world)[0] == "session"
        # B died before confirming it too: the old entry tap comes a third time, still harmless
        answers_before = len(world.api.answers())
        await d.poll()
        assert world.api.answers()[answers_before:] == ["Already answered"]
        assert order_count(world) == 1
        d.w.clock.set(et(9, 36, 20))
        await approve(d, stop_msg)  # B's message, C's bot
        assert working(world, "stop") == [("stop", Decimal("21.4100"), 33)]
        await d.walk(et(9, 36, 22), et(9, 36, 40))
        assert len(world.api.sent) == sent_before
        await d.at(et(10, 0))
        await midday(d, DAY, t)
        await checkin(d, DAY, 13, 30, t)
        await afternoon(d, DAY, t)  # C's flatten closes the position before the close (test 8)
        await evening(d, DAY)
        stop(d)

    await with_worker(world, worker_c)

    # test 6: the in-flight notification is never re-sent
    assert notification_status(world, f"fill:{entry_fill}") == "sending"
    assert events_of(world, "job.event:orb_open") == [
        (
            "critical",
            "event:orb_open for 2026-10-06: an earlier run's outcome is unknown, so it is not re-run "
            "automatically. Check the orders, then run it by hand with --force if needed.",
        )
    ]
    # test 7: one of everything
    labels = count_labels(world)
    assert {lb: n for lb, n in labels.items() if n != 1} == {}, world.api.labels()
    for title in (
        "ENTRY: BUY 33 AAA",
        "PROTECTIVE STOP: SELL 33 AAA",
        "ENTRY FILLED",
        "EXIT: SELL 33 AAA",
        "FLATTENED (end of day)",
        "Daily summary 2026-10-06",
    ):
        assert labels.get(title) == 1, (title, world.api.labels())
    with world.factory() as s:
        signals = s.execute(
            select(m.Signal.event_key, m.Signal.symbol_id, func.count()).group_by(
                m.Signal.event_key, m.Signal.symbol_id
            )
        ).all()
        orders = s.execute(select(m.Order.purpose, m.Order.proposal_id).order_by(m.Order.id)).all()
        fills = s.execute(select(m.Fill.order_id)).scalars().all()
        keys = s.execute(
            select(m.Notification.dedupe_key, func.count())
            .where(m.Notification.dedupe_key.is_not(None))
            .group_by(m.Notification.dedupe_key)
        ).all()
    assert signals and all(n == 1 for *_, n in signals), signals
    assert [p for p, _ in orders] == ["entry", "stop", "exit"]
    assert len({pid for _, pid in orders}) == 3  # one order per proposal
    # one fill per order: the entry and the exit (the protective stop was cancelled by the flatten)
    assert len(fills) == len(set(fills)) == 2
    assert keys and all(n == 1 for _, n in keys)
    assert proposal_statuses(world) == [
        ("entry", "submitted", "telegram"),
        ("stop", "submitted", "telegram"),
        ("exit", "submitted", "auto"),
    ]
    # test 8: C's flatten closed the position before the close
    assert_ends_flat(world)
    with world.factory() as s:
        closed_at = s.execute(select(m.Position.closed_at)).scalar_one()
    assert closed_at is not None and closed_at < t.close
    assert [k for k, st in notifications(world) if st not in ("sent", "sending")] == []


# --- test 8: a pending entry proposal of the dead worker expires on time ------------------------------------


async def test_pending_entry_proposal_expires_on_time_after_a_kill(world: World) -> None:
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)
    held: dict[str, Sent] = {}

    async def worker_a(d: Driver) -> None:
        take_lock(d, et(9, 15))
        await pre_open(d, DAY)
        held["entry"] = await to_orb(d, DAY, t)
        await d.at(et(9, 35, 20))
        die(d)

    await killed(world, worker_a)
    assert proposal_statuses(world) == [("entry", "pending", None)]
    with world.factory() as s:
        expires_at = s.execute(select(m.Proposal.expires_at)).scalar_one()
    ttl = world.store.load().proposal_ttl_entry_seconds
    assert expires_at == t.orb + timedelta(seconds=ttl)

    async def worker_b(d: Driver) -> None:
        take_lock(d, et(9, 35, 30))
        sent_before = len(world.api.sent)
        await d.walk(et(9, 35, 30), expires_at - timedelta(seconds=1), every=15.0)
        await d.at(expires_at - timedelta(seconds=1))
        assert proposal_statuses(world) == [("entry", "pending", None)]  # not early
        assert len(world.api.sent) == sent_before  # the proposal is not offered again
        await d.at(expires_at)
        assert proposal_statuses(world) == [("entry", "expired", None)]  # on time, under B
        assert "Expired" in world.api.edits_of(held["entry"].message_id)[-1]
        assert working(world, "entry") == []
        await d.walk(expires_at + timedelta(seconds=2), expires_at + timedelta(seconds=10))
        assert len(world.api.sent) == sent_before
        stop(d)

    await with_worker(world, worker_b)
    with world.factory() as s:
        assert s.execute(select(func.count()).select_from(m.Order)).scalar_one() == 0
