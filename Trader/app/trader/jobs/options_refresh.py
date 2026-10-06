"""The options morning refresh, 08:15 ET (OPTSIM task plan T13; job `options_refresh`).

The underlyings kept current are the union of: what the enabled plug-ins watch (their candidates and
holdings), the underlyings of the open structures, `options.watchlist`, and `options.benchmark_ticker`. For
each: the facts (T8), a fresh option chain, and the daily bars of the last 260 sessions. Then the adjustment
check: a held contract that is adjusted or gone from its fresh chain freezes its structure, and each freeze
is messaged and delivered to the plug-in that owns it.

One ticker failing is listed in the detail (and in one `warning` event); the job fails, and is retried by
the runner, only when every ticker failed. Everything here is safe to repeat.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog
from sqlalchemy.orm import Session, sessionmaker

from trader.db.session import session_scope
from trader.events import log_event
from trader.jobs.options_postclose import (
    MAX_REASON_CHARS,
    JobMarket,
    LifecycleFactory,
    announce,
    close_source,
    lifecycle_factory,
    not_a_session,
    reason,
)
from trader.jobs.runner import JobOutcome, RetryPolicy, run_job_async
from trader.logging_setup import redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.notify.types import Notifier
from trader.options.protocols import FactsProvider, OptionBroker, OptionRenderer, StrategyHost
from trader.options.settings import OptionSettings

log = structlog.get_logger("jobs.options_refresh")

REFRESH_JOB = "options_refresh"
SOURCE = "options.refresh"  # event_log.source (the activity feed shows `options.*`)
BAR_SESSIONS = 260


class RefreshFailed(RuntimeError):
    """Every underlying failed. Retried by the runner."""


@dataclass(frozen=True)
class RefreshDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], OptionSettings]
    run_id: Callable[[], int | None]  # the active options run, read when the job starts
    broker: OptionBroker
    market: JobMarket
    facts: FactsProvider
    host: StrategyHost
    notifier: Notifier
    renderer: OptionRenderer
    lifecycle: LifecycleFactory | None = None  # None: the engine over the database book
    retry: RetryPolicy | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


async def refresh_underlyings(deps: RefreshDeps, settings: OptionSettings) -> list[str]:
    """Watched by a plug-in, held, on the watchlist, or the benchmark: each ticker once, sorted."""
    tickers = set(await deps.host.watch_underlyings())
    tickers |= {st.underlying for st in await deps.broker.structures()}
    tickers |= set(settings.watchlist)
    tickers.add(settings.benchmark_ticker)
    return sorted({t.strip().upper() for t in tickers if t.strip()})


async def run_refresh(deps: RefreshDeps, run_id: int, session_date: date) -> dict[str, Any]:
    """The job's body. Returns `{"underlyings", "ok", "failed": {ticker: {part: error}}, "frozen"}`; raises
    `RefreshFailed` when every underlying failed (after the adjustment check has run)."""
    settings = deps.settings()
    tickers = await refresh_underlyings(deps, settings)
    failed: dict[str, dict[str, str]] = {}

    try:
        facts = await deps.facts.refresh(tickers)
    except Exception as exc:
        facts = dict.fromkeys(tickers, reason(exc))
    for ticker in tickers:
        got = facts.get(ticker, "no answer")
        if isinstance(got, str):
            failed.setdefault(ticker, {})["facts"] = redact_text(got)[:MAX_REASON_CHARS]

    start = deps.calendar.sessions_before(session_date, BAR_SESSIONS)[0]
    for ticker in tickers:
        try:
            await deps.market.refresh_chain(ticker, force=True)
        except Exception as exc:
            failed.setdefault(ticker, {})["chain"] = reason(exc)
        try:
            await deps.market.daily_bars(ticker, start, session_date)
        except Exception as exc:
            failed.setdefault(ticker, {})["bars"] = reason(exc)

    build = deps.lifecycle or lifecycle_factory(deps.factory, deps.clock, deps.calendar, deps.market)
    engine = build(run_id, settings, close_source(deps.market, deps.calendar, deps.clock))
    frozen = await engine.check_adjustments()
    undelivered = await announce(deps.notifier, deps.renderer, deps.host, frozen)

    detail: dict[str, Any] = {
        "underlyings": len(tickers),
        "ok": len(tickers) - len(failed),
        "failed": failed,
        "frozen": len(frozen),
    }
    if undelivered:  # the post-close job reads undelivered events back and delivers them
        detail["undelivered"] = undelivered
    if failed:
        log.warning("options_refresh.failed_underlyings", failed=sorted(failed))
        with session_scope(deps.factory) as s:
            log_event(
                s,
                deps.clock,
                "warning",
                SOURCE,
                f"Options refresh for {session_date}: {len(failed)} of {len(tickers)} underlying(s) "
                f"failed ({', '.join(sorted(failed))}).",
                {"failed": failed, "session_date": session_date.isoformat()},
                run_id,
            )
    if tickers and len(failed) == len(tickers):
        raise RefreshFailed(f"every underlying failed: {', '.join(tickers)}")
    return detail


async def refresh_job(deps: RefreshDeps, session_date: date, *, force: bool = False) -> JobOutcome:
    """Job `options_refresh` for one session. Skipped (no `job_runs` row) on a day that is not a session
    and when there is no options run."""
    if not_a_session(deps.calendar, session_date):
        return JobOutcome("skipped", {"reason": "not a session"})
    run_id = deps.run_id()
    if run_id is None:
        return JobOutcome("skipped", {"reason": "no options run"})
    active = run_id

    async def body() -> dict[str, Any]:
        return await run_refresh(deps, active, session_date)

    return await run_job_async(
        deps.factory,
        deps.clock,
        REFRESH_JOB,
        session_date,
        body,
        force,
        retry=deps.retry,
        sleep=deps.sleep,
    )
