"""Exchange sessions, holidays and early closes (SPEC §2: exchange_calendars)."""

from datetime import UTC, date, datetime

import exchange_calendars as xcals
import pandas as pd

CALENDAR_START = "2020-01-01"
CALENDAR_END = "2030-12-31"


class SessionCalendar:
    """Trading sessions for one exchange, backed by exchange_calendars.

    The calendar covers a fixed, deterministic range: CALENDAR_START (2020-01-01) to
    CALENDAR_END (2030-12-31), so results never depend on the date the process started.
    Asking about a date outside that range raises a ValueError subclass (exchange_calendars'
    DateOutOfBounds and related errors). This includes is_session, and navigation or
    sessions_before calls whose answer would fall outside the range.
    """

    def __init__(self, exchange: str = "XNYS") -> None:
        self._cal = xcals.get_calendar(exchange, start=CALENDAR_START, end=CALENDAR_END)

    def is_session(self, d: date) -> bool:
        return bool(self._cal.is_session(pd.Timestamp(d)))

    def _require(self, d: date) -> pd.Timestamp:
        if not self.is_session(d):
            raise ValueError(f"{d} is not a trading session")
        return pd.Timestamp(d)

    def session_open(self, d: date) -> datetime:
        ts: pd.Timestamp = self._cal.session_open(self._require(d))
        return ts.to_pydatetime().astimezone(UTC)

    def session_close(self, d: date) -> datetime:
        ts: pd.Timestamp = self._cal.session_close(self._require(d))
        return ts.to_pydatetime().astimezone(UTC)

    def next_session(self, d: date) -> date:
        ts = pd.Timestamp(d)
        nxt: pd.Timestamp = (
            self._cal.next_session(ts) if self.is_session(d) else self._cal.date_to_session(ts, "next")
        )
        return nxt.date()

    def previous_session(self, d: date) -> date:
        ts = pd.Timestamp(d)
        prev: pd.Timestamp = (
            self._cal.previous_session(ts)
            if self.is_session(d)
            else self._cal.date_to_session(ts, "previous")
        )
        return prev.date()

    def sessions_before(self, d: date, n: int) -> list[date]:
        """The n sessions strictly before d, oldest first. n == 0 gives []."""
        if n < 0:
            raise ValueError("n must be >= 0")
        if n == 0:
            return []
        last = self.previous_session(d)
        window = self._cal.sessions_window(pd.Timestamp(last), -n)
        return [ts.date() for ts in window]
