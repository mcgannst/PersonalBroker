"""run_job_async mirrors run_job (P3-T3 test 10), and both end cleanly when success can't be recorded
(P3-T3 test 11, P1-REVIEW should-fix 4)."""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
import structlog
from sqlalchemy import delete, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import EventLog, JobRun
from trader.jobs import runner
from trader.jobs.runner import JobFailure, JobOutcome, lock_key, run_job, run_job_async
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db
CLOCK = FixedClock(datetime(2026, 9, 27, 0, 0, tzinfo=UTC))
D = date(2026, 9, 28)


def _body(calls: list[int], result: dict[str, Any] | None = None) -> Callable[[], Any]:
    async def fn() -> dict[str, Any]:
        calls.append(1)
        await asyncio.sleep(0)
        return result or {}

    return fn


# --- test 10: run_job's skip, lock and failure semantics --------------------------------------


async def test_async_success_then_skip_unless_forced(db_factory: sessionmaker[Session]) -> None:
    calls: list[int] = []
    out = await run_job_async(db_factory, CLOCK, "event:x", D, _body(calls, {"n": 3}))
    assert out == JobOutcome("succeeded", {"n": 3})
    assert await run_job_async(db_factory, CLOCK, "event:x", D, _body(calls)) == JobOutcome(
        "skipped", {"reason": "already succeeded"}
    )
    assert (
        await run_job_async(db_factory, CLOCK, "event:x", D, _body(calls), force=True)
    ).status == "succeeded"
    assert len(calls) == 2
    with db_factory() as s:
        rows = s.execute(select(JobRun.status, JobRun.detail).order_by(JobRun.id)).all()
    assert [tuple(r) for r in rows] == [("succeeded", {"n": 3}), ("succeeded", {})]


async def test_async_is_skipped_while_another_process_holds_the_lock(
    db_factory: sessionmaker[Session],
) -> None:
    calls: list[int] = []
    engine = db_factory.kw["bind"]
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key("event:x", D)})
        out = await run_job_async(db_factory, CLOCK, "event:x", D, _body(calls))
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key("event:x", D)})
        other.commit()
    assert out == JobOutcome("skipped", {"reason": "already running"})
    assert calls == []
    assert (await run_job_async(db_factory, CLOCK, "event:x", D, _body(calls))).status == "succeeded"


async def test_async_concurrent_runs_run_the_body_once(db_factory: sessionmaker[Session]) -> None:
    calls: list[int] = []
    a, b = await asyncio.gather(
        run_job_async(db_factory, CLOCK, "event:x", D, _body(calls)),
        run_job_async(db_factory, CLOCK, "event:x", D, _body(calls)),
    )
    assert sorted([a.status, b.status]) == ["skipped", "succeeded"]
    assert len(calls) == 1


async def test_async_failure_is_recorded_and_retried(db_factory: sessionmaker[Session]) -> None:
    async def boom() -> dict[str, Any]:
        raise RuntimeError("Questrade down")

    out = await run_job_async(db_factory, CLOCK, "event:x", D, boom)
    assert out == JobOutcome("failed", error="RuntimeError: Questrade down")
    with db_factory() as s:
        assert s.execute(select(JobRun.status)).scalar_one() == "failed"
        ev = s.execute(select(EventLog)).scalar_one()
    assert ev.level == "error" and ev.source == "job.event:x"
    calls: list[int] = []
    assert (await run_job_async(db_factory, CLOCK, "event:x", D, _body(calls))).status == "succeeded"


async def test_async_abandoned_running_row_is_marked_failed(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(JobRun(job="event:x", session_date=D, started_at=CLOCK.now(), status="running"))
        s.commit()
    assert (await run_job_async(db_factory, CLOCK, "event:x", D, _body([]))).status == "succeeded"
    with db_factory() as s:
        rows = s.execute(select(JobRun.status, JobRun.error).order_by(JobRun.id)).all()
    assert [tuple(r) for r in rows] == [("failed", "abandoned"), ("succeeded", None)]


async def test_async_cancellation_is_recorded_then_reraised(db_factory: sessionmaker[Session]) -> None:
    async def cancelled() -> dict[str, Any]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_job_async(db_factory, CLOCK, "event:x", D, cancelled)
    with db_factory() as s:
        assert s.execute(select(JobRun.status)).scalar_one() == "failed"
    # the lock was released: a retry runs
    assert (await run_job_async(db_factory, CLOCK, "event:x", D, _body([]))).status == "succeeded"


async def test_job_failure_is_recorded_with_its_bare_message(db_factory: sessionmaker[Session]) -> None:
    async def missed() -> dict[str, Any]:
        raise JobFailure("missed: 295s late")

    out = await run_job_async(db_factory, CLOCK, "event:x", D, missed)
    assert out == JobOutcome("failed", error="missed: 295s late")
    with db_factory() as s:
        assert s.execute(select(JobRun.error)).scalar_one() == "missed: 295s late"


# --- test 11: success that can't be recorded ends cleanly -------------------------------------


def _delete_own_row(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        s.execute(delete(JobRun).where(JobRun.status == "running"))
        s.commit()


def _fail_job_runs_once_armed(
    factory: sessionmaker[Session],
) -> tuple[Callable[[], None], Callable[[], None]]:
    """Returns (arm, remove): once armed, every job_runs statement raises (the database went away). The
    advisory unlock still works, so the retry below is not racing a dropped connection."""
    engine = factory.kw["bind"]
    armed: list[bool] = []

    def hook(*args: Any, **__: Any) -> None:
        if armed and "job_runs" in args[2]:
            raise ConnectionError("database went away")

    event.listen(engine, "before_cursor_execute", hook)
    return (lambda: armed.append(True)), (lambda: event.remove(engine, "before_cursor_execute", hook))


def _assert_unrecorded(out: JobOutcome, detail: dict[str, Any], exc_type: str) -> None:
    assert out.status == "failed"
    assert out.detail == detail
    assert out.error == f"succeeded but could not be recorded: {exc_type}"


def test_run_job_row_deleted_underneath_returns_failed(db_factory: sessionmaker[Session]) -> None:
    def fn() -> dict[str, Any]:
        _delete_own_row(db_factory)
        return {"n": 1}

    with structlog.testing.capture_logs() as logs:
        out = run_job(db_factory, CLOCK, "nightly", D, fn)
    _assert_unrecorded(out, {"n": 1}, "JobRunMissing")
    assert any(e["event"] == "job.record_success_failed" and e["log_level"] == "critical" for e in logs)
    # fix round 1: a best-effort critical event, so the relay alerts Stephen
    with db_factory() as s:
        crit = s.execute(select(EventLog).where(EventLog.level == "critical")).scalars().all()
    assert [(e.source, e.data["error_type"]) for e in crit] == [("job.nightly", "JobRunMissing")]


async def test_rerun_abandoned_false_settles_a_leftover_running_row(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        s.add(JobRun(job="event:x", session_date=D, started_at=CLOCK.now(), status="running"))
        s.commit()
    calls: list[int] = []
    out = await run_job_async(db_factory, CLOCK, "event:x", D, _body(calls), rerun_abandoned=False)
    assert out == JobOutcome("failed", {"reason": "outcome unknown"}, error=runner.OUTCOME_UNKNOWN)
    assert calls == []
    with db_factory() as s:
        rows = s.execute(select(JobRun.status, JobRun.error)).all()
        crit = s.execute(select(EventLog.source).where(EventLog.level == "critical")).scalars().all()
    assert [tuple(r) for r in rows] == [("failed", runner.OUTCOME_UNKNOWN)]
    assert crit == ["job.event:x"]
    # force runs it anyway
    forced = await run_job_async(db_factory, CLOCK, "event:x", D, _body(calls), True, rerun_abandoned=False)
    assert forced.status == "succeeded" and calls == [1]


def test_run_job_db_failure_on_success_update_returns_failed(db_factory: sessionmaker[Session]) -> None:
    arm, remove = _fail_job_runs_once_armed(db_factory)

    def fn() -> dict[str, Any]:
        arm()
        return {"n": 2}

    try:
        out = run_job(db_factory, CLOCK, "nightly", D, fn)
    finally:
        remove()
    _assert_unrecorded(out, {"n": 2}, "ConnectionError")
    # the row stayed `running`; the next run marks it abandoned and goes ahead
    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).status == "succeeded"


async def test_run_job_async_row_deleted_underneath_returns_failed(db_factory: sessionmaker[Session]) -> None:
    async def fn() -> dict[str, Any]:
        _delete_own_row(db_factory)
        return {"n": 1}

    with structlog.testing.capture_logs() as logs:
        out = await run_job_async(db_factory, CLOCK, "event:x", D, fn)
    _assert_unrecorded(out, {"n": 1}, "JobRunMissing")
    assert any(e["event"] == "job.record_success_failed" and e["log_level"] == "critical" for e in logs)


async def test_run_job_async_db_failure_on_success_update_returns_failed(
    db_factory: sessionmaker[Session],
) -> None:
    arm, remove = _fail_job_runs_once_armed(db_factory)

    async def fn() -> dict[str, Any]:
        arm()
        return {"n": 2}

    try:
        out = await run_job_async(db_factory, CLOCK, "event:x", D, fn)
    finally:
        remove()
    _assert_unrecorded(out, {"n": 2}, "ConnectionError")
    assert (await run_job_async(db_factory, CLOCK, "event:x", D, _body([]))).status == "succeeded"


def test_runner_module_exposes_job_failure() -> None:
    assert issubclass(runner.JobFailure, Exception)


# --- P3-REVIEW: masked error text and the failure event's level ------------------------------


async def test_failure_text_is_masked_and_its_level_follows_failure_level(
    db_factory: sessionmaker[Session],
) -> None:
    token = "123456789:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"

    async def leaky() -> dict[str, Any]:
        raise RuntimeError(f"POST https://api.telegram.org/bot{token}/sendMessage failed")

    out = await run_job_async(db_factory, CLOCK, "event:x", D, leaky, failure_level="warning")
    assert out.status == "failed" and out.error is not None
    assert token not in out.error and "[REDACTED]" in out.error
    with db_factory() as s:
        stored = s.execute(select(JobRun.error)).scalar_one()
        ev = s.execute(select(EventLog).where(EventLog.source == "job.event:x")).scalar_one()
    assert stored is not None and token not in stored
    assert ev.level == "warning" and token not in str(ev.data)
