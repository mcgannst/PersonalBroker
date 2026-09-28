"""Decision log retention (P6-T10).

`prune` deletes live-run rows whose `session_date` is older than `reports.decisions_retention_days` (ET
today) and the rows of replay runs finished more than `reports.decisions_replay_retention_days` ago, in
batches of 5,000 (one short transaction each), and returns the counts. It deletes `decision_log` rows only.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock, et_date
from trader.settings_store import RuntimeSettings

BATCH = 5000


@dataclass(frozen=True, slots=True)
class PruneResult:
    live_deleted: int
    replay_deleted: int


def _delete_batches(factory: sessionmaker[Session], where: ColumnElement[bool]) -> int:
    total = 0
    while True:
        with session_scope(factory) as s:
            ids = (
                select(m.DecisionLog.id)
                .where(where)
                .order_by(m.DecisionLog.id)
                .limit(BATCH)
                .scalar_subquery()
            )
            result = s.execute(delete(m.DecisionLog).where(m.DecisionLog.id.in_(ids)))
            n = int(cast(CursorResult[Any], result).rowcount)
        total += n
        if n < BATCH:
            return total


def prune(factory: sessionmaker[Session], clock: Clock, settings: RuntimeSettings) -> PruneResult:
    now = clock.now()
    cutoff = et_date(now) - timedelta(days=settings.reports_decisions_retention_days)
    live_runs = select(m.Run.id).where(m.Run.mode == "live")
    live = _delete_batches(
        factory, (m.DecisionLog.run_id.in_(live_runs)) & (m.DecisionLog.session_date < cutoff)
    )
    replay_before = now - timedelta(days=settings.reports_decisions_replay_retention_days)
    old_replays = select(m.Run.id).where(
        m.Run.mode == "replay", m.Run.finished_at.is_not(None), m.Run.finished_at < replay_before
    )
    replay = _delete_batches(factory, m.DecisionLog.run_id.in_(old_replays))
    return PruneResult(live_deleted=live, replay_deleted=replay)
