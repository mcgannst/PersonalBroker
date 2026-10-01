"""FIX-DAY1: the worker's two timed quote captures of the opening bar (trader.worker.QuoteCaptures).

- `open` at 09:29:55 ET (OPEN_CAPTURE_LEAD before the open, 5 s since FIX-DAY1b): the volume at the open.
  Questrade's quote volume is the day's, pre-market included (CLDX Wed 2026-09-30: 339,533 at 09:35 against
  an official 09:30-09:35 candle of 35,628), so the opening bar's volume is the 09:35 volume minus this
  one. Read just BEFORE 09:30:00 so the opening print, which belongs to the 09:30 bar, is not subtracted;
  a quote whose last trade is at or after 09:30:00 anyway is not used (quote_bars.open_snapshot_volume).
- `bar` at 09:35:00.0 ET: the 09:30-09:35 bar, read the moment it closes (NVTS Wed: read 6.3 s late, the high
  had moved past the bar's). The 09:35:05 ORB event builds its bars from this stored capture.

Both are one batched quotes pass over the session's universe (<= 100 ids a request, sent concurrently since
FIX-DAY1b, a small budget), stored in `opening_quote_captures` by MarketDataService.capture_quotes, which does
not run a capture already stored for the session (a worker restart).
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from trader.market.calendar import SessionCalendar
from trader.market.quote_bars import CAPTURE_BAR, CAPTURE_OPEN, OPEN_CAPTURE_LEAD

BAR_LENGTH = timedelta(minutes=5)  # the 09:30-09:35 opening bar


class Capturer(Protocol):
    async def capture_quotes(
        self, session_date: date, kind: str, symbol_ids: Sequence[int] | None = None
    ) -> dict[str, Any]: ...


class OpeningCaptures:
    def __init__(self, data: Capturer, calendar: SessionCalendar) -> None:
        self._data = data
        self._cal = calendar

    def times(self, day: date) -> Sequence[tuple[str, datetime]]:
        if not self._cal.is_session(day):
            return ()
        open_ = self._cal.session_open(day)
        return ((CAPTURE_OPEN, open_ - OPEN_CAPTURE_LEAD), (CAPTURE_BAR, open_ + BAR_LENGTH))

    async def capture(self, kind: str, day: date) -> dict[str, Any]:
        return await self._data.capture_quotes(day, kind)
