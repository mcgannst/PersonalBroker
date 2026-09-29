"""FIX-401: load_report reads the orb_open job detail, so a stored 0-bar scan fails its day."""

import dataclasses
from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_soak_report import MON, TUE, SoakWorld, deps, world  # noqa: F401 (world: fixture)
from tests.jobs.test_soak import clean_rows
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs import soak
from trader.jobs.soak import JobRunRow

pytestmark = pytest.mark.db


def seed_with_detail(factory: sessionmaker[Session], d: date, rows: list[JobRunRow]) -> None:
    with session_scope(factory) as s:
        for r in rows:
            s.add(
                m.JobRun(
                    job=r.job,
                    session_date=d,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    status=r.status,
                    error=r.error,
                    detail=r.detail,
                )
            )


def test_a_stored_zero_bar_scan_fails_its_day(world: SoakWorld) -> None:  # noqa: F811
    seed_with_detail(world.factory, MON, clean_rows(MON))
    bad = {"universe": 542, "bars": 0, "missing": 542, "missing_reasons": {"timeout": 542}}
    rows = [dataclasses.replace(r, detail=bad) if r.job == "event:orb_open" else r for r in clean_rows(TUE)]
    seed_with_detail(world.factory, TUE, rows)
    report = soak.load_report(deps(world), sessions=2)
    assert [d.verdict for d in report.days] == ["clean", "not_clean"]
    assert report.days[1].orb_open == "failed"
    assert "event:orb_open failed" in report.days[1].failed
