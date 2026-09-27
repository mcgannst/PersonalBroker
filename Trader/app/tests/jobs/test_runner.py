import asyncio
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import EventLog, JobRun
from trader.jobs import runner
from trader.jobs.runner import MAX_ERROR_CHARS, JobOutcome, lock_key, run_job
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db
CLOCK = FixedClock(datetime(2026, 9, 27, 0, 0, tzinfo=UTC))
D = date(2026, 9, 28)


def test_success_is_recorded(db_factory: sessionmaker[Session]) -> None:
    out = run_job(db_factory, CLOCK, "nightly", D, lambda: {"n": 3})
    assert out.status == "succeeded" and out.detail == {"n": 3}
    with db_factory() as s:
        row = s.execute(select(JobRun)).scalar_one()
    assert row.status == "succeeded" and row.detail == {"n": 3} and row.finished_at is not None


def test_second_run_is_skipped_unless_forced(db_factory: sessionmaker[Session]) -> None:
    calls: list[int] = []

    def fn() -> dict[str, Any]:
        calls.append(1)
        return {}

    run_job(db_factory, CLOCK, "nightly", D, fn)
    assert run_job(db_factory, CLOCK, "nightly", D, fn).status == "skipped"
    assert run_job(db_factory, CLOCK, "nightly", D, fn, force=True).status == "succeeded"
    assert len(calls) == 2


def test_failure_is_recorded_and_logged(db_factory: sessionmaker[Session]) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("FinViz down")

    out = run_job(db_factory, CLOCK, "nightly", D, boom)
    assert out.status == "failed" and out.error == "RuntimeError: FinViz down"
    with db_factory() as s:
        assert s.execute(select(JobRun.status)).scalar_one() == "failed"
        assert s.execute(select(EventLog.level)).scalar_one() == "error"
    # a failed run doesn't block a retry
    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).status == "succeeded"


def test_run_is_skipped_while_another_process_holds_the_lock(db_factory: sessionmaker[Session]) -> None:
    """Single flight: another process holds the (job, session_date) advisory lock."""
    calls: list[int] = []

    def fn() -> dict[str, Any]:
        calls.append(1)
        return {}

    engine = db_factory.kw["bind"]
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key("nightly", D)})
        out = run_job(db_factory, CLOCK, "nightly", D, fn)
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key("nightly", D)})
        other.commit()
    assert out == JobOutcome("skipped", {"reason": "already running"})
    assert calls == []
    # once released, the run goes ahead
    assert run_job(db_factory, CLOCK, "nightly", D, fn).status == "succeeded"


def test_lock_keys_differ_by_job_and_date() -> None:
    keys = {lock_key("nightly", D), lock_key("nightly", date(2026, 9, 29)), lock_key("premarket", D)}
    assert len(keys) == 3
    assert lock_key("nightly", D) == lock_key("nightly", D)


def test_leftover_running_row_is_marked_abandoned(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(JobRun(job="nightly", session_date=D, started_at=CLOCK.now(), status="running"))
        s.add(JobRun(job="nightly", session_date=date(2026, 9, 29), started_at=CLOCK.now(), status="running"))
        s.commit()
    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).status == "succeeded"
    with db_factory() as s:
        rows = s.execute(select(JobRun.session_date, JobRun.status, JobRun.error).order_by(JobRun.id)).all()
    assert [tuple(r) for r in rows] == [
        (D, "failed", "abandoned"),
        (date(2026, 9, 29), "running", None),  # a different session is not touched
        (D, "succeeded", None),
    ]


def test_keyboard_interrupt_is_recorded_then_reraised(db_factory: sessionmaker[Session]) -> None:
    def interrupted() -> dict[str, Any]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_job(db_factory, CLOCK, "nightly", D, interrupted)
    with db_factory() as s:
        row = s.execute(select(JobRun)).scalar_one()
    assert row.status == "failed" and row.error == "KeyboardInterrupt: "
    # the lock was released: a retry runs
    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).status == "succeeded"


def test_cancelled_error_is_reraised(db_factory: sessionmaker[Session]) -> None:
    def cancelled() -> dict[str, Any]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run_job(db_factory, CLOCK, "nightly", D, cancelled)
    with db_factory() as s:
        assert s.execute(select(JobRun.status)).scalar_one() == "failed"


def test_long_error_is_truncated(db_factory: sessionmaker[Session]) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("x" * 5000)

    out = run_job(db_factory, CLOCK, "nightly", D, boom)
    assert out.error is not None and len(out.error) == MAX_ERROR_CHARS
    with db_factory() as s:
        assert len(s.execute(select(JobRun.error)).scalar_one() or "") == MAX_ERROR_CHARS


def advisory_locks(factory: sessionmaker[Session], wait_s: float = 5.0) -> int:
    """Advisory locks held in this database. A lock on an invalidated connection is released when
    the server notices the disconnect, so poll briefly for the count to reach zero."""
    engine = factory.kw["bind"]
    deadline = time.monotonic() + wait_s
    while True:
        with engine.connect() as conn:
            n = int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                    )
                ).scalar_one()
            )
        if n == 0 or time.monotonic() > deadline:
            return n
        time.sleep(0.05)


def test_no_advisory_lock_is_left_after_success_failure_or_interrupt(
    db_factory: sessionmaker[Session],
) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("boom")

    def interrupted() -> dict[str, Any]:
        raise KeyboardInterrupt

    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).status == "succeeded"
    assert advisory_locks(db_factory) == 0
    assert run_job(db_factory, CLOCK, "a", D, boom).status == "failed"
    assert advisory_locks(db_factory) == 0
    with pytest.raises(KeyboardInterrupt):
        run_job(db_factory, CLOCK, "b", D, interrupted)
    assert advisory_locks(db_factory) == 0


def _raise_on(factory: sessionmaker[Session], sql: str, when: str) -> Callable[[], None]:
    """Raise KeyboardInterrupt around the first statement containing `sql`. Returns the remover."""
    engine = factory.kw["bind"]
    fired: list[bool] = []

    def hook(*args: Any, **_: Any) -> None:
        if sql in args[2] and not fired:
            fired.append(True)
            raise KeyboardInterrupt

    event.listen(engine, when, hook)
    return lambda: event.remove(engine, when, hook)


def test_interrupt_right_after_the_lock_is_taken_leaves_no_lock(db_factory: sessionmaker[Session]) -> None:
    """The server granted the lock but the interrupt arrived before run_job saw the answer."""
    remove = _raise_on(db_factory, "pg_try_advisory_lock", "after_cursor_execute")
    try:
        with pytest.raises(KeyboardInterrupt):
            run_job(db_factory, CLOCK, "nightly", D, lambda: {})
    finally:
        remove()
    assert advisory_locks(db_factory) == 0
    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).status == "succeeded"


def test_interrupt_during_unlock_discards_the_locked_connection(db_factory: sessionmaker[Session]) -> None:
    remove = _raise_on(db_factory, "pg_advisory_unlock", "before_cursor_execute")
    try:
        with pytest.raises(KeyboardInterrupt):
            run_job(db_factory, CLOCK, "nightly", D, lambda: {})
    finally:
        remove()
    assert advisory_locks(db_factory) == 0
    # the run itself finished before the unlock, so the retry is skipped as already succeeded
    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).detail == {"reason": "already succeeded"}


def test_failure_to_record_a_failure_keeps_the_original_error(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("FinViz down")

    def interrupted() -> dict[str, Any]:
        raise KeyboardInterrupt

    def broken_log_event(*_: object, **__: object) -> None:
        raise ConnectionError("database went away")

    with monkeypatch.context() as m:
        m.setattr(runner, "log_event", broken_log_event)
        out = run_job(db_factory, CLOCK, "nightly", D, boom)
        assert out == JobOutcome("failed", error="RuntimeError: FinViz down")
        with pytest.raises(KeyboardInterrupt):  # the original, not the ConnectionError
            run_job(db_factory, CLOCK, "nightly", D, interrupted)
    # both rows stayed `running`; the next run marks them abandoned
    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).status == "succeeded"
    with db_factory() as s:
        rows = s.execute(select(JobRun.status, JobRun.error).order_by(JobRun.id)).all()
    assert [tuple(r) for r in rows] == [("failed", "abandoned"), ("failed", "abandoned"), ("succeeded", None)]
