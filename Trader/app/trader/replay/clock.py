"""The replay clock (SPEC §3a, §8; P5-T3): a monotonic `Clock` the runner sets to each visited time, so every
simulated record takes its time from it. It refuses a naive datetime and never goes backwards."""

from datetime import UTC, datetime, timedelta


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("ReplayClock needs a timezone-aware datetime")
    return at.astimezone(UTC)


class ReplayClock:
    def __init__(self, start: datetime) -> None:
        self._at = _utc(start)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        """Move to `at` (UTC-aware); ValueError when it is before the current time."""
        target = _utc(at)
        if target < self._at:
            raise ValueError(
                f"the replay clock can't go back from {self._at.isoformat()} to {target.isoformat()}"
            )
        self._at = target

    def advance(self, delta: timedelta) -> None:
        """Move forward by `delta`; ValueError for a negative delta."""
        if delta < timedelta(0):
            raise ValueError(f"the replay clock can't advance by a negative delta ({delta})")
        self._at += delta
