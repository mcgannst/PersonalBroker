"""The replay's read-only catalyst source (SPEC §8 `replay_catalyst_mode`; P5-T5). `stored` reads the stored
`catalysts` rows and reports a missing name as `unknown`; `unknown` reports every name as `unknown`. It never
calls Claude and never writes."""

from collections.abc import Mapping, Sequence
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from trader.replay.types import CatalystMode
from trader.strategies.base import CatalystInfo

UNKNOWN_REASON = "replay: no stored catalyst"


class ReplayCatalysts:
    def __init__(self, factory: sessionmaker[Session], mode: CatalystMode) -> None:
        self._factory = factory
        self.mode = mode

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> Mapping[int, CatalystInfo]:
        raise NotImplementedError("P5-T5")
