"""The replay data source (SPEC §8; P5-T5): everything a strategy reads during a replay, from the candle
archive and caches first and Questrade second (only within `replay.questrade_window_days` of the wall-clock
date, never offline), with no lookahead (no bar ending after the replay clock) and no writes to any table.

Implements `trader.replay.types.ReplayMarket`. Fetched Questrade data is held in memory for the run only:
each session's opening 5-minute bar, and the current session's 1-minute bars.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.data_service import QuoteClient
from trader.market.types import Candle, Interval, OpenBarStats, OpeningBars, UniverseMember, UniverseStatus

BIASED_SOURCE = "biased"


class ReplayData:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        wall: Clock,
        calendar: SessionCalendar,
        client: QuoteClient | None,
        *,
        run_id: int,
        date_from: date,
        date_to: date,
        half_spread_bps: Decimal,
        questrade_window_days: int,
        lookback_sessions: int,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._wall = wall
        self._calendar = calendar
        self._client = client
        self.run_id = run_id
        self.date_from = date_from
        self.date_to = date_to
        self.half_spread_bps = half_spread_bps
        self.questrade_window_days = questrade_window_days
        self.lookback_sessions = lookback_sessions

    # --- MarketDataView (strategy-facing; never past the replay clock) --------------------------------------
    async def universe(self, session_date: date) -> list[UniverseMember]:
        raise NotImplementedError("P5-T5")

    async def universe_status(self, session_date: date) -> UniverseStatus:
        raise NotImplementedError("P5-T5")

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        raise NotImplementedError("P5-T5")

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        raise NotImplementedError("P5-T5")

    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        """Synthetic quotes from the last complete 1-minute bar (last = close, bid/ask = close -/+ hs)."""
        raise NotImplementedError("P5-T5")

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        raise NotImplementedError("P5-T5")

    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None:
        raise NotImplementedError("P5-T5")

    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]:
        raise NotImplementedError("P5-T5")

    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]:
        raise NotImplementedError("P5-T5")

    # --- runner-only helpers --------------------------------------------------------------------------------
    async def prepare_day(self, session_date: date) -> None:
        """Load the day's universe, opening bars, stats and SPY's 1-minute bars. Drop the previous day's."""
        raise NotImplementedError("P5-T5")

    async def load_minute_bars(self, symbol_ids: Sequence[int], session_date: date) -> None:
        raise NotImplementedError("P5-T5")

    def bar_ending_at(self, symbol_id: int, at: datetime) -> Candle | None:
        raise NotImplementedError("P5-T5")

    def last_close(self, symbol_id: int, at: datetime) -> Decimal | None:
        raise NotImplementedError("P5-T5")

    def progress_counts(self) -> Mapping[str, int]:
        """`missing_opening_bars`, `missing_minute_bars`, `questrade_requests`."""
        raise NotImplementedError("P5-T5")

    @property
    def biased_days(self) -> frozenset[date]:
        raise NotImplementedError("P5-T5")
