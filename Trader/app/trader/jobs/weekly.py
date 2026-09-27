"""The weekly report job body (BR-61; SPEC §9 Sat 09:00; P5-T9): facts, commentary (within budget, number
checked, one retry), the stored row, and one Telegram message per week (dedupe `weekly:<week_ending>`)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.claude.reports import CommentaryWriter
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.notify.types import Notifier, Renderer
from trader.reports.weekly import WeekWindow
from trader.settings_store import RuntimeSettings


@dataclass(frozen=True)
class WeeklyDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    writer: CommentaryWriter | None  # None when ANTHROPIC_API_KEY is not set
    notifier: Notifier
    render: Renderer
    run_id: int  # the live run the report describes


async def run_weekly(deps: WeeklyDeps, week: WeekWindow) -> dict[str, Any]:
    """Build, store and send the week's report; returns the job detail."""
    raise NotImplementedError("P5-T9")
