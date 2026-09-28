"""P5-GO fix round 1 (P5-T15 review must-fix): notifications a dead process left `sending` are settled
`unknown` ("interrupted by restart") when the worker starts, so the System page lists them as undelivered,
and they are never re-sent. A row a live process may still be sending is left alone."""

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_telegram import FakeTelegramApi
from tests.test_worker import TUE, Harness, VirtualTime, _run, et
from trader.db.models import Notification
from trader.db.session import session_scope
from trader.market.clock import FixedClock
from trader.notify.notifier import (
    INTERRUPTED_BY_RESTART,
    PART_MAX_SECONDS,
    SENDING_STALE_AFTER,
    TelegramNotifier,
    settle_interrupted_sends,
)
from trader.notify.types import OutboundMessage
from trader.worker import Worker

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)


def add(factory: sessionmaker[Session], key: str, status: str, created_at: datetime) -> None:
    with session_scope(factory) as s:
        s.add(
            Notification(
                kind="fill", dedupe_key=key, text=key, created_at=created_at, status=status, attempts=1
            )
        )


def statuses(factory: sessionmaker[Session]) -> dict[str, tuple[str, str | None]]:
    with factory() as s:
        rows = s.execute(select(Notification.dedupe_key, Notification.status, Notification.error)).all()
    return {key: (status, error) for key, status, error in rows}


def test_the_threshold_is_above_several_worst_case_parts() -> None:
    assert SENDING_STALE_AFTER.total_seconds() > 4 * PART_MAX_SECONDS


def test_only_stale_sending_rows_become_unknown(db_factory: sessionmaker[Session]) -> None:
    old = NOW - SENDING_STALE_AFTER - timedelta(seconds=1)
    add(db_factory, "stale", "sending", old)
    add(db_factory, "live", "sending", NOW - timedelta(seconds=30))  # a live process may be sending it
    add(db_factory, "edge", "sending", NOW - SENDING_STALE_AFTER)  # not older than the threshold
    add(db_factory, "sent", "sent", old)
    add(db_factory, "failed", "failed", old)

    assert settle_interrupted_sends(db_factory, FixedClock(NOW)) == 1
    assert statuses(db_factory) == {
        "stale": ("unknown", INTERRUPTED_BY_RESTART),
        "live": ("sending", None),
        "edge": ("sending", None),
        "sent": ("sent", None),
        "failed": ("failed", None),
    }
    assert settle_interrupted_sends(db_factory, FixedClock(NOW)) == 0  # idempotent


async def test_a_settled_row_is_never_sent_again(db_factory: sessionmaker[Session]) -> None:
    add(db_factory, "fill:1", "sending", NOW - timedelta(hours=1))
    settle_interrupted_sends(db_factory, FixedClock(NOW))
    api = FakeTelegramApi()
    notifier = TelegramNotifier(api, 1, db_factory, FixedClock(NOW))
    await notifier.send(OutboundMessage(kind="fill", text="ENTRY FILLED", dedupe_key="fill:1"))
    assert [name for name, _ in api.calls if name == "send_message"] == []
    assert statuses(db_factory)["fill:1"] == ("unknown", INTERRUPTED_BY_RESTART)


async def test_the_worker_settles_them_once_it_holds_the_lock(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    add(db_factory, "stale", "sending", clock.now() - timedelta(hours=1))
    add(db_factory, "live", "sending", clock.now() - timedelta(seconds=5))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    await _run(Worker(h.deps()), asyncio.Event(), vt, once=True)
    assert statuses(db_factory) == {
        "stale": ("unknown", INTERRUPTED_BY_RESTART),
        "live": ("sending", None),
    }


def test_a_database_failure_never_stops_the_worker_start(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)

    def broken() -> Session:
        raise ConnectionError("db down")

    deps = dataclasses.replace(Harness(db_factory, clock).deps(), factory=broken)
    Worker(deps).recover_after_lock()  # logged, not raised
