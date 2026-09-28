"""P5-T15: in-process retries of the day-level jobs (`run_job` / `run_job_async` with a `RetryPolicy`).

A failed attempt is a `failed` job_runs row with a `warning` event; only the last failure writes the
`error` event the relay alerts; the waits (120 s, then 240 s) go through the injected sleep, so nothing
here really waits. `retry=None` and `attempts=1` are pinned by the P3 runner tests and the P5-T1 contract
tests.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.factories import add_run
from tests.fakes_telegram import FakeMessenger, RecordingNotifier
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs.runner import JobFailure, JobOutcome, RetryPolicy, run_job, run_job_async
from trader.market.clock import FixedClock
from trader.notify.messages import MessageRenderer
from trader.notify.relay import NotificationRelay
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CLOCK = FixedClock(datetime(2026, 10, 6, 6, 0, tzinfo=UTC))  # 02:00 ET, the nightly cron line
D = date(2026, 10, 6)
THREE = RetryPolicy(attempts=3, first_delay_s=120.0, backoff=2.0)


def flaky(failures: int, exc: BaseException | None = None) -> tuple[Callable[[], dict[str, Any]], list[int]]:
    """A sync body failing `failures` times (RuntimeError, or `exc`), then returning {"n": 1}."""
    calls: list[int] = []

    def fn() -> dict[str, Any]:
        calls.append(1)
        if len(calls) <= failures:
            raise exc if exc is not None else RuntimeError(f"FinViz down #{len(calls)}")
        return {"n": 1}

    return fn, calls


def rows(factory: sessionmaker[Session]) -> list[tuple[str, str | None]]:
    with factory() as s:
        found = s.execute(select(m.JobRun.status, m.JobRun.error).order_by(m.JobRun.id)).all()
    return [(st, err) for st, err in found]


def events(factory: sessionmaker[Session]) -> list[tuple[str, str, str, dict[str, Any]]]:
    with factory() as s:
        found = s.execute(
            select(m.EventLog.level, m.EventLog.source, m.EventLog.message, m.EventLog.data).order_by(
                m.EventLog.id
            )
        ).all()
    return [(lv, src, msg, data or {}) for lv, src, msg, data in found]


def levels(factory: sessionmaker[Session]) -> list[str]:
    return [lv for lv, *_ in events(factory)]


# --- test 1: two failures, then a success --------------------------------------------------------------


def test_two_failures_then_success(db_factory: sessionmaker[Session]) -> None:
    fn, calls = flaky(2)
    slept: list[float] = []
    out = run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=slept.append)

    assert out == JobOutcome("succeeded", {"n": 1, "attempts": 3})
    assert len(calls) == 3
    assert slept == [120.0, 240.0]
    assert rows(db_factory) == [
        ("failed", "RuntimeError: FinViz down #1"),
        ("failed", "RuntimeError: FinViz down #2"),
        ("succeeded", None),
    ]
    with db_factory() as s:
        assert s.execute(select(m.JobRun.detail).where(m.JobRun.status == "succeeded")).scalar_one() == {
            "n": 1,
            "attempts": 3,
        }
    assert events(db_factory) == [
        (
            "warning",
            "job.nightly",
            "nightly failed for 2026-10-06 (attempt 1 of 3), retrying in 120 s",
            {"error": "RuntimeError: FinViz down #1", "attempt": 1, "attempts": 3},
        ),
        (
            "warning",
            "job.nightly",
            "nightly failed for 2026-10-06 (attempt 2 of 3), retrying in 240 s",
            {"error": "RuntimeError: FinViz down #2", "attempt": 2, "attempts": 3},
        ),
    ]


def test_first_attempt_success_has_no_attempts_key(db_factory: sessionmaker[Session]) -> None:
    fn, _ = flaky(0)
    slept: list[float] = []
    out = run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=slept.append)
    assert out == JobOutcome("succeeded", {"n": 1})
    assert slept == [] and events(db_factory) == []


def test_backoff_follows_the_policy(db_factory: sessionmaker[Session]) -> None:
    fn, _ = flaky(3)
    slept: list[float] = []
    policy = RetryPolicy(attempts=4, first_delay_s=90.0, backoff=3.0)
    out = run_job(db_factory, CLOCK, "premarket", D, fn, retry=policy, sleep=slept.append)
    assert out.detail == {"n": 1, "attempts": 4}
    assert slept == [90.0, 270.0, 810.0]


# --- test 2: every attempt fails -----------------------------------------------------------------------


def relay_world(factory: sessionmaker[Session]) -> tuple[NotificationRelay, RecordingNotifier]:
    with session_scope(factory) as s:
        run_id = add_run(s)
    notifier = RecordingNotifier()
    relay = NotificationRelay(
        factory,
        CLOCK,
        notifier,
        MessageRenderer("http://trader.home:8080", ZoneInfo("America/Edmonton"), clock=CLOCK),
        FakeMessenger(),
        run_id,
        settings=RuntimeSettings,
    )
    return relay, notifier


async def test_three_failures_alert_once(db_factory: sessionmaker[Session]) -> None:
    relay, notifier = relay_world(db_factory)
    await relay.pump()  # the cursors start here
    fn, calls = flaky(5)
    slept: list[float] = []

    out = run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=slept.append)

    assert out == JobOutcome("failed", error="RuntimeError: FinViz down #3")
    assert len(calls) == 3 and slept == [120.0, 240.0]
    assert [st for st, _ in rows(db_factory)] == ["failed", "failed", "failed"]
    assert levels(db_factory) == ["warning", "warning", "error"]
    assert events(db_factory)[-1] == (
        "error",
        "job.nightly",
        "nightly failed for 2026-10-06 after 3 attempts",
        {"error": "RuntimeError: FinViz down #3", "attempt": 3, "attempts": 3},
    )
    # the P3 relay turns the last failure, and only it, into a phone alert
    await relay.pump()
    assert [msg.kind for msg in notifier.sent] == ["job_failure"]
    assert "after 3 attempts" in notifier.sent[0].text


def test_the_last_attempt_keeps_a_lowered_failure_level(db_factory: sessionmaker[Session]) -> None:
    """P3-REVIEW: `retry` never raises an alert level the caller lowered."""
    fn, _ = flaky(5)
    out = run_job(
        db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=lambda _: None, failure_level="warning"
    )
    assert out.status == "failed"
    assert levels(db_factory) == ["warning", "warning", "warning"]
    fn, _ = flaky(5)
    run_job(db_factory, CLOCK, "postclose", D, fn, retry=THREE, sleep=lambda _: None, failure_level="info")
    assert levels(db_factory)[3:] == ["info", "info", "info"]


def test_already_succeeded_is_still_skipped(db_factory: sessionmaker[Session]) -> None:
    fn, _ = flaky(0)
    run_job(db_factory, CLOCK, "nightly", D, fn)
    fn, calls = flaky(0)
    out = run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=lambda _: None)
    assert out == JobOutcome("skipped", {"reason": "already succeeded"}) and calls == []


# --- test 3: what is never retried ---------------------------------------------------------------------


def test_job_failure_is_not_retried(db_factory: sessionmaker[Session]) -> None:
    fn, calls = flaky(5, JobFailure("universe degenerate: 3 names"))
    slept: list[float] = []
    out = run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=slept.append)
    assert out == JobOutcome("failed", error="universe degenerate: 3 names")
    assert len(calls) == 1 and slept == []
    assert rows(db_factory) == [("failed", "universe degenerate: 3 names")]
    assert levels(db_factory) == ["error"]


def test_interrupt_in_the_body_is_not_retried(db_factory: sessionmaker[Session]) -> None:
    fn, calls = flaky(5, KeyboardInterrupt())
    slept: list[float] = []
    with pytest.raises(KeyboardInterrupt):
        run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=slept.append)
    assert len(calls) == 1 and slept == []
    assert rows(db_factory) == [("failed", "KeyboardInterrupt: ")]
    assert levels(db_factory) == ["error"]


def test_unrecorded_success_is_not_retried(db_factory: sessionmaker[Session]) -> None:
    calls: list[int] = []

    def deletes_its_row() -> dict[str, Any]:
        calls.append(1)
        with session_scope(db_factory) as s:
            s.execute(delete(m.JobRun))
        return {}

    out = run_job(db_factory, CLOCK, "nightly", D, deletes_its_row, retry=THREE, sleep=lambda _: None)
    assert out.status == "failed" and out.error == "succeeded but could not be recorded: JobRunMissing"
    assert calls == [1]
    assert levels(db_factory) == ["critical"]


def advisory_locks(factory: sessionmaker[Session]) -> int:
    with factory.kw["bind"].connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                )
            ).scalar_one()
        )


def test_cancel_during_the_wait_is_recorded_and_reraised(db_factory: sessionmaker[Session]) -> None:
    fn, calls = flaky(5)

    def cancelled(_: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=cancelled)
    assert len(calls) == 1
    assert rows(db_factory) == [("failed", "RuntimeError: FinViz down #1")]
    # the interrupted retry is the job's last word: one error event, so the relay alerts once
    assert levels(db_factory) == ["warning", "error"]
    last = events(db_factory)[-1]
    assert last[2] == "nightly failed for 2026-10-06: retries stopped after attempt 1 of 3 (CancelledError)"
    assert last[3] == {
        "error": "RuntimeError: FinViz down #1",
        "attempt": 1,
        "attempts": 3,
        "interrupted": "CancelledError",
    }
    assert advisory_locks(db_factory) == 0
    # the lock was released: the next run goes ahead
    fn, _ = flaky(0)
    assert (
        run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=lambda _: None).status == "succeeded"
    )


# --- test 4: single flight across the waits ------------------------------------------------------------


def test_second_process_is_skipped_while_the_first_waits(db_factory: sessionmaker[Session]) -> None:
    fn, _ = flaky(1)
    other: list[JobOutcome] = []
    other_calls: list[int] = []

    def sleep(_: float) -> None:
        # another process (its own connection, so its own advisory-lock session) starts the same run
        def body() -> dict[str, Any]:
            other_calls.append(1)
            return {}

        other.append(run_job(db_factory, CLOCK, "nightly", D, body, retry=THREE, sleep=lambda _: None))

    out = run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=sleep)
    assert other == [JobOutcome("skipped", {"reason": "already running"})]
    assert other_calls == []
    assert out.status == "succeeded" and out.detail["attempts"] == 2


# --- test 5: run_job_async ------------------------------------------------------------------------------


def aflaky(
    failures: int, exc: BaseException | None = None
) -> tuple[Callable[[], Awaitable[dict[str, Any]]], list[int]]:
    calls: list[int] = []

    async def fn() -> dict[str, Any]:
        calls.append(1)
        await asyncio.sleep(0)
        if len(calls) <= failures:
            raise exc if exc is not None else RuntimeError(f"Questrade 503 #{len(calls)}")
        return {"n": 1}

    return fn, calls


class AsyncSleep:
    def __init__(self, during: Callable[[], Awaitable[None]] | None = None) -> None:
        self.slept: list[float] = []
        self.during = during

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.during is not None:
            await self.during()


async def test_async_two_failures_then_success(db_factory: sessionmaker[Session]) -> None:
    fn, calls = aflaky(2)
    sleep = AsyncSleep()
    out = await run_job_async(db_factory, CLOCK, "premarket", D, fn, retry=THREE, sleep=sleep)
    assert out == JobOutcome("succeeded", {"n": 1, "attempts": 3})
    assert len(calls) == 3 and sleep.slept == [120.0, 240.0]
    assert [st for st, _ in rows(db_factory)] == ["failed", "failed", "succeeded"]
    assert levels(db_factory) == ["warning", "warning"]


async def test_async_three_failures_alert_once(db_factory: sessionmaker[Session]) -> None:
    fn, _ = aflaky(5)
    sleep = AsyncSleep()
    out = await run_job_async(db_factory, CLOCK, "premarket", D, fn, retry=THREE, sleep=sleep)
    assert out == JobOutcome("failed", error="RuntimeError: Questrade 503 #3")
    assert [st for st, _ in rows(db_factory)] == ["failed", "failed", "failed"]
    assert levels(db_factory) == ["warning", "warning", "error"]
    assert events(db_factory)[-1][2] == "premarket failed for 2026-10-06 after 3 attempts"


async def test_async_job_failure_is_not_retried(db_factory: sessionmaker[Session]) -> None:
    fn, calls = aflaky(5, JobFailure("no candidates file"))
    sleep = AsyncSleep()
    out = await run_job_async(db_factory, CLOCK, "premarket", D, fn, retry=THREE, sleep=sleep)
    assert out == JobOutcome("failed", error="no candidates file")
    assert len(calls) == 1 and sleep.slept == [] and levels(db_factory) == ["error"]


async def test_async_cancel_during_the_wait(db_factory: sessionmaker[Session]) -> None:
    """A real task cancellation while the job waits between attempts: recorded, re-raised, lock released."""
    fn, calls = aflaky(5)
    waiting = asyncio.Event()

    async def block() -> None:
        waiting.set()
        await asyncio.Event().wait()  # until cancelled

    task = asyncio.create_task(
        run_job_async(db_factory, CLOCK, "premarket", D, fn, retry=THREE, sleep=AsyncSleep(block))
    )
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1
    assert levels(db_factory) == ["warning", "error"]
    assert events(db_factory)[-1][3]["interrupted"] == "CancelledError"
    assert advisory_locks(db_factory) == 0


async def test_async_second_process_is_skipped_while_the_first_waits(
    db_factory: sessionmaker[Session],
) -> None:
    fn, _ = aflaky(1)
    other: list[JobOutcome] = []

    async def second() -> None:
        body, calls = aflaky(0)
        other.append(
            await run_job_async(db_factory, CLOCK, "premarket", D, body, retry=THREE, sleep=AsyncSleep())
        )
        assert calls == []

    out = await run_job_async(db_factory, CLOCK, "premarket", D, fn, retry=THREE, sleep=AsyncSleep(second))
    assert other == [JobOutcome("skipped", {"reason": "already running"})]
    assert out == JobOutcome("succeeded", {"n": 1, "attempts": 2})


# --- fix round 1 (P5-GO): deadline, validation, detail key, one row per incident -------------------------


def test_a_wait_past_the_deadline_is_never_started_and_the_failure_alerts(
    db_factory: sessionmaker[Session],
) -> None:
    fn, calls = flaky(5)
    slept: list[float] = []
    # the first wait (120 s) ends before the deadline, the second (240 s) would end after it
    policy = RetryPolicy(attempts=3, first_delay_s=120.0, deadline=CLOCK.now() + timedelta(seconds=200))
    out = run_job(db_factory, CLOCK, "preopen", D, fn, retry=policy, sleep=slept.append)
    assert out.status == "failed" and len(calls) == 2 and slept == [120.0]
    assert rows(db_factory) == [
        ("failed", "RuntimeError: FinViz down #1"),
        ("failed", "RuntimeError: FinViz down #2"),
    ]
    assert levels(db_factory) == ["warning", "error"]
    _, _, message, data = events(db_factory)[-1]
    assert message == "preopen failed for 2026-10-06 after attempt 2 of 3 (retries stopped: deadline)"
    assert data == {
        "error": "RuntimeError: FinViz down #2",
        "attempt": 2,
        "attempts": 3,
        "stopped": "deadline",
    }


def test_a_wait_ending_exactly_at_the_deadline_still_runs(db_factory: sessionmaker[Session]) -> None:
    fn, _ = flaky(1)
    slept: list[float] = []
    policy = RetryPolicy(attempts=3, first_delay_s=120.0, deadline=CLOCK.now() + timedelta(seconds=120))
    out = run_job(db_factory, CLOCK, "preopen", D, fn, retry=policy, sleep=slept.append)
    assert out == JobOutcome("succeeded", {"n": 1, "attempts": 2}) and slept == [120.0]


async def test_async_deadline_already_passed_means_one_attempt_only(
    db_factory: sessionmaker[Session],
) -> None:
    fn, calls = aflaky(3)
    policy = RetryPolicy(attempts=3, first_delay_s=10.0, deadline=CLOCK.now())
    out = await run_job_async(db_factory, CLOCK, "premarket", D, fn, retry=policy, sleep=AsyncSleep())
    assert out.status == "failed" and len(calls) == 1
    assert levels(db_factory) == ["error"]
    assert events(db_factory)[-1][3]["stopped"] == "deadline"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attempts": 0},
        {"first_delay_s": -1.0},
        {"first_delay_s": float("inf")},
        {"backoff": 0.5},
        {"deadline": datetime(2026, 10, 6, 13, 28)},  # naive
    ],
)
def test_retry_policy_is_validated(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_retry_policy_from_settings_takes_a_deadline() -> None:
    deadline = datetime(2026, 10, 6, 9, 28, tzinfo=ZoneInfo("America/New_York"))
    policy = RetryPolicy.from_settings(RuntimeSettings(), deadline=deadline)
    assert policy == RetryPolicy(3, 120.0, 2.0, deadline)


def test_a_body_attempts_key_is_kept_and_the_count_goes_in_retry_attempts(
    db_factory: sessionmaker[Session],
) -> None:
    calls: list[int] = []

    def fn() -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("once")
        return {"attempts": 7}

    out = run_job(db_factory, CLOCK, "nightly", D, fn, retry=THREE, sleep=lambda _: None)
    assert out == JobOutcome("succeeded", {"attempts": 7, "retry_attempts": 2})


def test_outcome_unknown_log_line_is_marked_event_logged(db_factory: sessionmaker[Session]) -> None:
    """The critical event is the record: the log mirror (P5-T14) must not write a second row for it."""
    with session_scope(db_factory) as s:
        s.add(m.JobRun(job="event:orb_open", session_date=D, started_at=CLOCK.now(), status="running"))
    with capture_logs() as logs:
        out = run_job(db_factory, CLOCK, "event:orb_open", D, dict, rerun_abandoned=False)
    assert out.status == "failed"
    [line] = [e for e in logs if e["event"] == "job.outcome_unknown"]
    assert line["event_logged"] is True
