"""Read-only lookups shared by GET /api/live and GET /api/control (DB-GDATA fix round 1).

- `live_run`: the active live run's id and start, SELECTed without writing. Only a truly fresh database
  (no live run at all) falls back to `engine.runs.get_live_run`, which creates it as every process does.
- `day_plan`: the day plan built as `trader soak-report` builds it (`soak.readonly_plan`): the active live
  run is looked up read-only (never created), no strategy default is ensured, no plan problem is reported and
  a plug-in that can't start is left out with a debug line (a quiet registry), so opening a page writes no
  event and takes no advisory lock. The API's `services.plan` (`runtime.plan_builder`) stays for the old
  `/api/dashboard`. The plug-in classes are the API registry's (loaded once per process at start-up), so a
  request does no entry-point scan (~10 ms).
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select

from trader.api.deps import ApiServices, _settings
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.engine.scheduler import DayPlan
from trader.jobs import soak
from trader.settings_store import RuntimeSettings


@dataclass(frozen=True, slots=True)
class LiveRunRef:
    id: int
    started_at: datetime


def live_run(services: ApiServices) -> LiveRunRef:
    """The active live run, read without writing; created (as every process does) only when none exists."""
    core = services.core
    with core.factory() as s:
        row = s.execute(
            select(m.Run.id, m.Run.started_at).where(m.Run.mode == "live", m.Run.status == "active")
        ).one_or_none()
    if row is not None:
        return LiveRunRef(row.id, row.started_at)
    run = get_live_run(core.factory, core.clock, _settings(services))
    return LiveRunRef(run.id, run.started_at)


def day_plan(services: ApiServices, day: date, settings: RuntimeSettings) -> DayPlan:
    """`soak.readonly_plan` for `day` with `settings` (already loaded by the caller)."""
    core = services.core
    loaded = services.registry
    plugins = {key: loaded.plugin_class(key) for key in loaded.keys()}
    registry = soak._QuietRegistry(core.factory, core.clock, plugins=plugins)
    plan = soak.readonly_plan(core.factory, core.clock, core.calendar, lambda: settings, registry=registry)
    return plan(day)
