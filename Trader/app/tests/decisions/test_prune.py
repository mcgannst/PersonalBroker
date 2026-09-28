"""P6-T10 acceptance test 18: `prune` deletes live rows older than the retention and the rows of replays
finished more than the replay retention ago, in batches, and keeps the rest."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run
from trader.db import models as m
from trader.db.session import session_scope
from trader.decisions import prune as prune_mod
from trader.decisions.prune import PruneResult, prune
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

NOW = datetime(2027, 11, 10, 18, 0, tzinfo=UTC)  # 13:00 ET on 2027-11-10
TODAY = date(2027, 11, 10)


def _rows(s: Session, run_id: int, day: date, n: int = 1) -> None:
    for seq in range(1, n + 1):
        s.add(
            m.DecisionLog(
                run_id=run_id,
                session_date=day,
                seq=seq,
                stage="day",
                outcome="info",
                ts=NOW,
                ref={},
                data={},
                recorded_at=NOW,
                final=True,
            )
        )


def _count(factory: sessionmaker[Session], run_id: int) -> int:
    with factory() as s:
        return s.execute(select(func.count()).where(m.DecisionLog.run_id == run_id)).scalar_one()


def test_18_prune_live_and_replay_rows(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prune_mod, "BATCH", 3)  # several batches
    with session_scope(db_factory) as s:
        live = add_run(s)
        old_replay = add_run(s, mode="replay", status="completed")
        new_replay = add_run(s, mode="replay", status="completed")
        running = add_run(s, mode="replay", status="running")
        s.get(m.Run, old_replay).finished_at = NOW - timedelta(days=31)  # type: ignore[union-attr]
        s.get(m.Run, new_replay).finished_at = NOW - timedelta(days=29)  # type: ignore[union-attr]
        s.flush()
        cutoff = TODAY - timedelta(days=400)
        _rows(s, live, cutoff - timedelta(days=1), 5)  # older than 400 days: deleted
        _rows(s, live, cutoff - timedelta(days=30), 2)
        _rows(s, live, cutoff, 4)  # exactly 400 days: kept
        _rows(s, live, TODAY, 1)
        _rows(s, old_replay, TODAY - timedelta(days=40), 7)
        _rows(s, new_replay, TODAY - timedelta(days=40), 2)
        _rows(s, running, TODAY - timedelta(days=900), 1)  # not finished: kept
    res = prune(db_factory, FixedClock(NOW), RuntimeSettings())
    assert res == PruneResult(live_deleted=7, replay_deleted=7)
    assert _count(db_factory, live) == 5
    assert _count(db_factory, old_replay) == 0
    assert _count(db_factory, new_replay) == 2
    assert _count(db_factory, running) == 1
    assert prune(db_factory, FixedClock(NOW), RuntimeSettings()) == PruneResult(0, 0)


def test_18_prune_follows_the_settings(db_factory: sessionmaker[Session]) -> None:
    with session_scope(db_factory) as s:
        live = add_run(s)
        replay = add_run(s, mode="replay", status="completed")
        s.get(m.Run, replay).finished_at = NOW - timedelta(days=2)  # type: ignore[union-attr]
        s.flush()
        _rows(s, live, TODAY - timedelta(days=31))
        _rows(s, replay, TODAY)
    settings = RuntimeSettings.model_validate(
        {"reports.decisions_retention_days": 30, "reports.decisions_replay_retention_days": 1}
    )
    assert prune(db_factory, FixedClock(NOW), settings) == PruneResult(1, 1)
