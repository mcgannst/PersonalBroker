"""The only module allowed to read the wall clock (Global Constraints)."""

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock tests and replay can move by hand."""

    def __init__(self, at: datetime) -> None:
        self.set(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._at = at.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self._at += delta


def et_date(at: datetime) -> date:
    return at.astimezone(ET).date()
