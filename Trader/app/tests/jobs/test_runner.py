from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import EventLog, JobRun
from trader.jobs.runner import run_job
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
