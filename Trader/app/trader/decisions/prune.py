"""Decision log retention (stub from P6-T9; P6-T10 implements it).

`prune` deletes live-run rows whose `session_date` is older than `reports.decisions_retention_days` (ET
today) and the rows of replay runs finished more than `reports.decisions_replay_retention_days` ago, in
batches of 5,000, and returns the counts.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings


@dataclass(frozen=True, slots=True)
class PruneResult:
    live_deleted: int
    replay_deleted: int


def prune(factory: sessionmaker[Session], clock: Clock, settings: RuntimeSettings) -> PruneResult:
    raise NotImplementedError("P6-T10: prune")
