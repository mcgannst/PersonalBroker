"""The replay clock (SPEC §3a, §8; P5-T3): a monotonic `Clock` the runner sets to each visited time, so every
simulated record takes its time from it. It refuses a naive datetime and never goes backwards."""

from datetime import datetime, timedelta


class ReplayClock:
    def __init__(self, start: datetime) -> None:
        self._start = start

    def now(self) -> datetime:
        raise NotImplementedError("P5-T3")

    def set(self, at: datetime) -> None:
        """Move to `at` (UTC-aware); ValueError when it is before the current time."""
        raise NotImplementedError("P5-T3")

    def advance(self, delta: timedelta) -> None:
        """Move forward by `delta`; ValueError for a negative delta."""
        raise NotImplementedError("P5-T3")
