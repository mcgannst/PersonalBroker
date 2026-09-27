"""The replay's read-only catalyst source (SPEC §8 `replay_catalyst_mode`; P5-T5). `stored` reads the stored
`catalysts` rows and reports a missing name as `unknown`; `unknown` reports every name as `unknown`. It never
calls Claude and never writes."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.claude.catalyst import CatalystStore, StoredCatalyst
from trader.replay.types import CatalystMode
from trader.strategies.base import CatalystInfo

UNKNOWN_REASON = "replay: no stored catalyst"


class _NoClock:
    """`CatalystStore.get` never reads the time; a read-only store gets a clock that refuses to."""

    def now(self) -> datetime:
        raise RuntimeError("replay catalysts never read the clock")


def unknown_catalyst(symbol_id: int) -> StoredCatalyst:
    return StoredCatalyst(
        symbol_id, "unknown", "neutral", None, None, UNKNOWN_REASON, None, Decimal(0), False
    )


class ReplayCatalysts:
    def __init__(self, factory: sessionmaker[Session], mode: CatalystMode) -> None:
        self._factory = factory
        self.mode = mode
        self._store = CatalystStore(factory, _NoClock())

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> Mapping[int, CatalystInfo]:
        ids = sorted(set(symbol_ids))
        if not ids:
            return {}
        stored = self._store.get(ids, session_date) if self.mode == "stored" else {}
        return {sid: stored.get(sid) or unknown_catalyst(sid) for sid in ids}
