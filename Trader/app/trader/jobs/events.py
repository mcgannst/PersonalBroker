"""`trader event <key>` / `trader event --due`: the cron backup for the worker's session events (SPEC §9).

P3-T1 stub: the contracts are final, P3-T10 implements them.
"""

from collections.abc import Awaitable, Callable
from datetime import date

from trader.engine.scheduler import DayPlan, FireResult
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock


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
    """`key` given: fire it once. `due=True`: fire every due event (needs `plan` and `fired`)."""
    raise NotImplementedError("P3-T10")
