"""The weekly report job body (BR-61; SPEC §9 Sat 09:00; P5-T9): facts, commentary (within budget, number
checked, one retry), the stored row, and one Telegram message per week (dedupe `weekly:<week_ending>`).

Budget: the first call is made only when `claude.daily_budget_usd` minus the day's Claude spend (catalysts
of the ET date plus weekly reports updated that date) is at least `reports.weekly_max_cost_usd`. The one
retry is made only when twice the first call's cost is within that cap (the retry costs about the same, so
the report never passes its cap) and the daily check still passes with the first call counted.

The report is stored and sent whatever happens to the commentary; a forced re-run replaces the stored row
(its cost accumulates) and the notifier's dedupe keeps it to one message.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.claude.reports import Commentary, CommentaryWriter
from trader.api.schemas import CommentaryStatus
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.notify.types import Notifier, Renderer, WeeklyReportView
from trader.reports.weekly import (
    CommentaryOutcome,
    WeekWindow,
    build_facts,
    check_length,
    check_numbers,
    claude_spent,
    upsert_report,
)
from trader.settings_store import RuntimeSettings

log = structlog.get_logger(__name__)

NO_API_KEY = "ANTHROPIC_API_KEY not set"
NOTES: dict[str, str] = {
    "budget": "Commentary unavailable: the daily Claude budget is used up.",
    "numbers": "Commentary unavailable: it quoted numbers not in the report.",
    "length": "Commentary unavailable: it was not the expected length.",
    "not_configured": "Commentary unavailable: Claude is not configured.",
    "off": "Commentary unavailable: it is turned off in Settings.",
    "error": "Commentary unavailable: Claude failed.",
}


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
    """Build, store and send the week's report; returns the job detail
    {"week_ending", "commentary_status", "cost_usd", "trades", "sent"}, or {"skipped": "no sessions"}.

    `sent` is what the `notifications` table shows for the dedupe key (`Notifier.send` returns nothing):
    `sent`, `handed_off` (a notifier that keeps no row, such as a test fake), `duplicate` (the week's message
    was recorded before this run, so the notifier dropped it), `failed`, or `error` (rendering or sending
    raised)."""
    if week.week_ending is None:
        return {"skipped": "no sessions"}
    settings = deps.settings()
    facts = build_facts(deps.factory, deps.calendar, deps.run_id, week, settings=settings)
    outcome, note = await _commentary(deps, settings, facts)
    upsert_report(deps.factory, deps.clock, week, deps.run_id, facts, outcome)
    sent = await _send(deps, _view(week, week.week_ending, facts, outcome, note), week.week_ending)
    log.info("weekly.done", week_ending=week.week_ending.isoformat(), status=outcome.status, sent=sent)
    return {
        "week_ending": week.week_ending.isoformat(),
        "commentary_status": outcome.status,
        "cost_usd": format(outcome.cost_usd, "f"),
        "trades": facts["week_metrics"]["trades"],
        "sent": sent,
    }


def _no_call(status: CommentaryStatus, error: str | None) -> CommentaryOutcome:
    return CommentaryOutcome(status, None, error, None, 0, 0, Decimal(0))


def _combined(
    status: CommentaryStatus, text: str | None, error: str | None, calls: Sequence[Commentary]
) -> CommentaryOutcome:
    return CommentaryOutcome(
        status,
        text,
        error,
        calls[0].model,
        sum(c.input_tokens for c in calls),
        sum(c.output_tokens for c in calls),
        sum((c.cost_usd for c in calls), Decimal(0)),
    )


def _problems(text: str, facts: Mapping[str, Any]) -> tuple[list[str], str | None]:
    """(the numbers not in the facts, the length problem or None)."""
    return check_numbers(text, facts), check_length(text)


def _rejection(numbers: Sequence[str], length: str | None) -> tuple[str, str]:
    """(the stored error, the note key) of a rejected commentary."""
    parts = []
    if numbers:
        parts.append(f"numbers not in the facts: {', '.join(numbers)}")
    if length:
        parts.append(length)
    return "; ".join(parts), "numbers" if numbers else "length"


async def _commentary(
    deps: WeeklyDeps, s: RuntimeSettings, facts: Mapping[str, Any]
) -> tuple[CommentaryOutcome, str | None]:
    """The commentary outcome and, when there is no commentary, the key of the note explaining why."""
    if not s.reports_weekly_commentary:
        return _no_call("disabled", None), "off"
    if deps.writer is None:
        return _no_call("disabled", NO_API_KEY), "not_configured"
    today = et_date(deps.clock.now())
    budget, cap = s.claude_daily_budget_usd, s.reports_weekly_max_cost_usd
    spent = claude_spent(deps.factory, today)
    if budget - spent < cap:
        error = f"daily Claude budget: US${spent} of US${budget} spent; the report needs US${cap}"
        return _no_call("budget", error), "budget"

    first = await deps.writer.write(facts)
    if first.status == "error" or first.text is None:
        return _combined("error", None, first.error, [first]), "error"
    numbers, length = _problems(first.text, facts)
    if not numbers and length is None:
        return _combined("ok", first.text, None, [first]), None
    seen = list(numbers)
    retry_fits = (
        first.cost_usd * 2 <= cap and budget - (claude_spent(deps.factory, today) + first.cost_usd) >= cap
    )
    if not retry_fits:
        error, note = _rejection(seen, length)
        log.warning("weekly.commentary_rejected", error=error, retried=False)
        return _combined("rejected", None, error, [first]), note

    second = await deps.writer.write(facts, avoid=numbers)
    calls = [first, second]
    if second.status == "error" or second.text is None:
        return _combined("error", None, f"retry: {second.error}", calls), "error"
    numbers, length = _problems(second.text, facts)
    if not numbers and length is None:
        return _combined("ok", second.text, None, calls), None
    seen += [n for n in numbers if n not in seen]
    error, note = _rejection(seen, length)
    log.warning("weekly.commentary_rejected", error=error, retried=True)
    return _combined("rejected", None, error, calls), note


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _view(
    week: WeekWindow,
    week_ending: date,
    facts: Mapping[str, Any],
    outcome: CommentaryOutcome,
    note: str | None,
) -> WeeklyReportView:
    wm = facts["week_metrics"]
    return WeeklyReportView(
        week_start=week.start,
        week_ending=week_ending,
        trades=int(wm["trades"]),
        wins=int(wm["wins"]),
        win_rate=_dec(wm["win_rate"]),
        expectancy_r=_dec(wm["expectancy_r"]),
        total_pnl=Decimal(str(wm["total_pnl"])),
        max_drawdown_pct=_dec(wm["max_drawdown_pct"]),
        adherence_pct=_dec(wm["adherence_pct"]),
        commentary=outcome.text if outcome.status == "ok" else None,
        commentary_note=NOTES[note] if note is not None else None,
    )


def _notification_status(factory: sessionmaker[Session], dedupe_key: str) -> str | None:
    with factory() as s:
        return s.execute(
            select(m.Notification.status).where(m.Notification.dedupe_key == dedupe_key)
        ).scalar_one_or_none()


async def _send(deps: WeeklyDeps, view: WeeklyReportView, week_ending: date) -> str:
    dedupe_key = f"weekly:{week_ending.isoformat()}"
    try:
        before = _notification_status(deps.factory, dedupe_key)
        msg = deps.render.weekly_report(view)
        await deps.notifier.send(dataclasses.replace(msg, dedupe_key=dedupe_key))
        after = _notification_status(deps.factory, dedupe_key)
    except Exception as exc:  # the report is stored; the web Reports page still shows it
        log.error("weekly.send_failed", error=type(exc).__name__)
        return "error"
    if before is not None:
        return "duplicate"
    if after is None:
        return "handed_off"
    return "sent" if after == "sent" else "failed"
