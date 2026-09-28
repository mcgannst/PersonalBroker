"""The decision recorder (stub from P6-T9; P6-T10 implements it).

`record_day` builds run R's journal for session D from the authoritative rows (catalysts, job details,
universe snapshots, candidates, orb notes, signals, proposals, orders, fills, trades, kill-switch and overlay
events) and the stored opening bars, and writes only `decision_log`. It is idempotent per (run, day): a pass
takes `pg_advisory_xact_lock(hashtext('trader.decisions:<run>:<date>'))`, then (under the lock) returns
`final` for a frozen day unless `rebuild`, `unchanged` when the source fingerprint matches, and otherwise
rebuilds the day in one transaction. The whole pass runs in one `asyncio.to_thread` call.
"""

from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from trader.decisions.types import RecorderDeps, RecordResult, ScanData
from trader.market.types import Candle, OpenBarStats, UniverseMember


async def record_day(
    deps: RecorderDeps, run_id: int, session_date: date, *, final: bool = False, rebuild: bool = False
) -> RecordResult:
    raise NotImplementedError("P6-T10: decision recorder")


class LiveScanData(ScanData):
    """`ScanData` over the database only (universe snapshots, open-bar stats, stored 1-minute bars)."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    async def universe(self, session_date: date) -> list[UniverseMember]:
        raise NotImplementedError("P6-T10: LiveScanData.universe")

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        raise NotImplementedError("P6-T10: LiveScanData.open_bar_stats")

    async def stored_opening_bars(self, session_date: date, symbol_ids: list[int]) -> dict[int, Candle]:
        raise NotImplementedError("P6-T10: LiveScanData.stored_opening_bars")
