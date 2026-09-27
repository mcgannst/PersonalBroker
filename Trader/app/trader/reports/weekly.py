"""The weekly report (BR-61; SPEC §4.3, §9 Sat 09:00; P5-T9): the Monday-Friday week just ended, its facts
from `trader.reports.metrics`, the Claude commentary's number check and budget, and the stored
`weekly_reports` row.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trader.api.schemas import CommentaryStatus
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock


@dataclass(frozen=True, slots=True)
class WeekWindow:
    start: date  # the Monday
    end: date  # the Friday
    sessions: tuple[date, ...]
    week_ending: date | None  # the week's last session; None when the week had none


@dataclass(frozen=True, slots=True)
class CommentaryOutcome:
    status: CommentaryStatus
    text: str | None
    error: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


def week_window(cal: SessionCalendar, any_day: date) -> WeekWindow:
    """The Monday-Friday week containing `any_day`; a Saturday or Sunday belongs to the week just ended."""
    raise NotImplementedError("P5-T9")


def last_completed_week(cal: SessionCalendar, today: date) -> WeekWindow:
    """Saturday or Sunday -> this week; Monday-Friday -> the previous week."""
    raise NotImplementedError("P5-T9")


def build_facts(
    factory: sessionmaker[Session], cal: SessionCalendar, run_id: int, week: WeekWindow
) -> dict[str, Any]:
    """The JSON-safe facts (Decimals as strings) the commentary may quote."""
    raise NotImplementedError("P5-T9")


def check_numbers(text: str, facts: Mapping[str, Any]) -> list[str]:
    """The number tokens of `text` not found in `facts` (empty means OK)."""
    raise NotImplementedError("P5-T9")


def claude_spent(factory: sessionmaker[Session], day: date) -> Decimal:
    """The day's Claude spend: catalysts of that session date plus weekly reports updated that ET date."""
    raise NotImplementedError("P5-T9")


def upsert_report(
    factory: sessionmaker[Session],
    clock: Clock,
    week: WeekWindow,
    run_id: int,
    facts: Mapping[str, Any],
    outcome: CommentaryOutcome,
) -> None:
    """Insert or replace the week's row, keeping `created_at` and accumulating `cost_usd`."""
    raise NotImplementedError("P5-T9")
