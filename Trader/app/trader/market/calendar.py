"""Exchange sessions, holidays and early closes (SPEC §2: exchange_calendars)."""

from datetime import UTC, date, datetime

import exchange_calendars as xcals
import pandas as pd


class SessionCalendar:
    def __init__(self, exchange: str = "XNYS") -> None:
        self._cal = xcals.get_calendar(exchange, start="2020-01-01")

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
        nxt = self._cal.next_session(ts) if self.is_session(d) else self._cal.date_to_session(ts, "next")
        return nxt.date()  # type: ignore[no-any-return]

    def previous_session(self, d: date) -> date:
        ts = pd.Timestamp(d)
        prev = (
            self._cal.previous_session(ts)
            if self.is_session(d)
            else self._cal.date_to_session(ts, "previous")
        )
        return prev.date()  # type: ignore[no-any-return]

    def sessions_before(self, d: date, n: int) -> list[date]:
        last = self.previous_session(d)
        window = self._cal.sessions_window(pd.Timestamp(last), -n)
        return [ts.date() for ts in window]
