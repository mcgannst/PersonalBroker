"""Session event scheduler: today's event times from every enabled strategy, which are due, and firing
each (event, session) exactly once across the worker and the cron backups (SPEC §5.1, §9).

P3-T1 stub: the contracts are final, the functions are implemented by P3-T3.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session, sessionmaker

from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings
from trader.strategies.base import Strategy

EVENT_JOB_PREFIX = "event:"
MAX_EVENT_ATTEMPTS = 3


def event_job(key: str) -> str:
    """The job_runs.job name of a session event: `event:<key>`."""
    raise NotImplementedError("P3-T3")


class MissedEvent(Exception):
    """Raised inside the job body of an event that is too late to fire, so it is recorded as a failure
    whose error starts with `missed:`."""


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    key: str
    at: datetime  # UTC
    strategies: tuple[str, ...]  # strategy keys that scheduled this key
    always_fire_late: bool


@dataclass(frozen=True, slots=True)
class DayPlan:
    session_date: date
    is_session: bool
    open: datetime | None
    close: datetime | None
    events: tuple[PlannedEvent, ...]  # sorted by time, then key


def day_plan(
    strategies: Sequence[Strategy], cal: SessionCalendar, session_date: date, settings: RuntimeSettings
) -> DayPlan:
    raise NotImplementedError("P3-T3")


def fired_keys(factory: sessionmaker[Session], session_date: date) -> set[str]:
    """The settled keys: succeeded, missed, or failed MAX_EVENT_ATTEMPTS times."""
    raise NotImplementedError("P3-T3")


def due_events(plan: DayPlan, now: datetime, fired: set[str]) -> list[PlannedEvent]:
    """Unfired events with `at <= now`, in time order."""
    raise NotImplementedError("P3-T3")


class EventRunner(Protocol):
    """What fire_event runs an event on. P2's Engine satisfies it (same parameter names)."""

    async def run_event(self, event_key: str, session_date: date) -> Any: ...


@dataclass(frozen=True, slots=True)
class FireDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    plan: Callable[[date], DayPlan]
    runner: Callable[[], Awaitable[EventRunner]]  # built lazily, only when an event actually runs


FireStatus = Literal["fired", "skipped", "missed", "too_early", "not_scheduled", "not_session", "failed"]


@dataclass(frozen=True, slots=True)
class FireResult:
    key: str
    session_date: date
    status: FireStatus
    detail: dict[str, Any] = field(default_factory=dict)


async def fire_event(deps: FireDeps, key: str, session_date: date, *, force: bool = False) -> FireResult:
    raise NotImplementedError("P3-T3")
