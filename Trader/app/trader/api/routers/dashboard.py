"""GET /api/dashboard -> DashboardOut: the day at a glance (session, timeline, pending proposals, positions,
P&L, kill switches, events, token, worker, top candidates).

Stub (P4-T1): T5 adds the route, fills `DAY_JOBS` (the crontab's day-level lines: premarket 08:00, preopen
09:20, check-ins 11:30 and 13:30, postclose 16:15, plus `nightly`) and gives `build_timeline` its signature.
The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from dataclasses import dataclass
from datetime import time
from typing import Any

from fastapi import APIRouter

from trader.api.schemas import TimelineItemOut

router = APIRouter(tags=["dashboard"])


@dataclass(frozen=True, slots=True)
class DayJob:
    """A day-level cron job on the timeline: `job_name` is its `job_runs.job`, `et_time` its crontab time."""

    key: str
    label: str
    job_name: str
    et_time: time


DAY_JOBS: tuple[DayJob, ...] = ()


def build_timeline(*args: Any, **kwargs: Any) -> list[TimelineItemOut]:
    """The session's day jobs and planned events in time order, each with its status (T5)."""
    raise NotImplementedError("P4-T5")
