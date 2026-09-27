"""`trader event <key>` / `trader event --due`: the cron backup for the worker's session events (SPEC §9).

`fire` is the idempotent `fire_event` bound to its deps (and to `--force`, by the caller), so a backup that
arrives after the worker already fired gets `skipped` from it, not an error. The CLI maps the statuses to
exit codes (P3-T12): 0 for fired, skipped, too_early, not_session and not_scheduled; 1 for failed and missed.

Each `fire` is isolated: one that raises is logged (exception type only) and reported as a `failed` result
with `{"error": <type>}` (so the CLI exits 1), and with `due=True` the later due events (the flatten above
all) are still fired.
"""

from collections.abc import Awaitable, Callable
from datetime import date

import structlog

from trader.engine.scheduler import DayPlan, FireResult, due_events
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock

log = structlog.get_logger("jobs.events")

NOT_A_SESSION = {"skipped": "not a session"}
DUE_KEY = "due"  # the key of the single not_session result of a `--due` run on a non-session day


async def run_event_backup(
    fire: Callable[[str, date], Awaitable[FireResult]],
    calendar: SessionCalendar,
    clock: Clock,
    key: str | None,
    session_date: date,
    *,
    due: bool = False,
    plan: Callable[[date], DayPlan] | None = None,
    fired: Callable[[date], set[str]] | None = None,
    force: bool = False,
) -> list[FireResult]:
    """`key` given: fire it once. `due=True`: fire every due event (needs `plan` and `fired`).

    On a non-session day nothing is fired: one `not_session` result is returned. With `due` and `force`,
    every planned event whose time has come is passed to `fire`, settled or not (`fire` decides; `--force`
    is bound into it by the caller); without `force` only the unsettled ones are.
    """
    if key is not None and due:
        raise ValueError("give an event key or due=True, not both")
    if key is None and not due:
        raise ValueError("give an event key or due=True")
    if due and (plan is None or fired is None):
        raise ValueError("due=True needs plan and fired")
    if not calendar.is_session(session_date):
        return [FireResult(key or DUE_KEY, session_date, "not_session", dict(NOT_A_SESSION))]
    if key is not None:
        return [await _fire_isolated(fire, key, session_date)]
    assert plan is not None and fired is not None  # checked above
    settled = set() if force else fired(session_date)
    results: list[FireResult] = []
    for event in due_events(plan(session_date), clock.now(), settled):
        results.append(await _fire_isolated(fire, event.key, session_date))
    return results


async def _fire_isolated(
    fire: Callable[[str, date], Awaitable[FireResult]], key: str, session_date: date
) -> FireResult:
    """`fire(key)`, with an exception turned into a `failed` result so the next due event still fires."""
    try:
        return await fire(key, session_date)
    except Exception as exc:  # one bad event must not cost the later ones (the flatten)
        error = type(exc).__name__
        log.error("event_backup.fire_failed", key=key, session_date=session_date.isoformat(), error=error)
        return FireResult(key, session_date, "failed", {"error": error})
