"""The 16:15 ET post-close job: end-of-day cancels (BR-42 safety net), the journal row, the candle archive
and the daily summary with the Rules-followed buttons (BR-60, BR-33; SPEC §8, §9).

P3-T1 stub: the contracts are final, P3-T11 implements them.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import CallbackIssuer
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import Candle, Interval, OpeningBars, UniverseMember
from trader.notify.types import DailySummaryView, Notifier, Renderer
from trader.settings_store import RuntimeSettings
from trader.worker import WorkerEngine


class ArchiveData(Protocol):
    """The market data the archive reads (P2's MarketDataService satisfies it)."""

    async def universe(self, session_date: date) -> list[UniverseMember]: ...

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]: ...

    async def opening_bars(
        self, session_date: date, symbol_ids: Sequence[int] | None = None
    ) -> OpeningBars: ...


@dataclass(frozen=True)
class PostcloseDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    engine: WorkerEngine
    data: ArchiveData
    notifier: Notifier
    render: Renderer
    issuer: CallbackIssuer
    chat_id: int
    run_id: int


async def run_postclose(deps: PostcloseDeps, session_date: date) -> dict[str, Any]:
    """Returns {"open_positions", "cancelled", "archive", "summary_sent"}."""
    raise NotImplementedError("P3-T11")


async def archive_candles(deps: PostcloseDeps, session_date: date) -> dict[str, Any]:
    raise NotImplementedError("P3-T11")


def daily_summary_view(
    factory: sessionmaker[Session],
    run_id: int,
    session_date: date,
    now: datetime,
    archive: Mapping[str, int],
) -> DailySummaryView:
    raise NotImplementedError("P3-T11")
