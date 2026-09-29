"""The 09:20 ET pre-open check: token, universe, opening-bar stats, pre-market run, kill switches and the
worker heartbeat, sent directly from the cron process (SPEC §9).

A failed check is an alert, not a job failure: the job succeeds and its detail lists every check. A check
that raises becomes an `error` check, so one broken dependency never hides the others. The message goes
out through the cron process's own notifier (not the worker's relay), so it arrives even when the worker
is down; it is sent when any check is not OK, or always when `preopen.notify_when_ok` is on.
"""

import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import QuestradeApiError
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.logging_setup import redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import UniverseStatus
from trader.notify.types import Check, Notifier, PreopenView, Renderer
from trader.notify.views import STOPPED_PHASES, WORKER_PROCESS
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("jobs.preopen")

NOT_A_SESSION = {"skipped": "not a session"}
MANUAL_PAUSE = "manual_pause"
MAX_DETAIL_CHARS = 300


@dataclass(frozen=True)
class PreopenDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    token_check: Callable[[], Awaitable[None]]  # raises when the Questrade token can't be refreshed
    universe_status: Callable[[date], Awaitable[UniverseStatus]]
    killswitches: KillSwitches
    run_id: int
    notifier: Notifier
    render: Renderer
    # FIX-401: ONE real market-data call through the app's client (a quote for SPY, else the first universe
    # symbol); returns the ticker quoted, None when no symbol has a Questrade id, and raises the
    # QuestradeApiError Questrade answered with. None: no `market_data` check.
    market_check: Callable[[date], Awaitable[str | None]] | None = None


def _ok(name: str, detail: str) -> Check:
    return Check(name, True, "info", detail)


def _warning(name: str, detail: str) -> Check:
    return Check(name, False, "warning", detail)


def _error(name: str, detail: str) -> Check:
    return Check(name, False, "error", detail)


def _one_line(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_DETAIL_CHARS else flat[: MAX_DETAIL_CHARS - 1] + "…"


def _exc_text(exc: BaseException) -> str:
    """An exception as one masked line: the check detail is stored in job_runs and shown in the web app."""
    message = _one_line(redact_text(str(exc)))
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _sessions(n: int | None) -> str:
    if n is None:
        return "age unknown"
    return f"{n} session{'' if n == 1 else 's'} old"


async def _token(deps: PreopenDeps, session_date: date, settings: RuntimeSettings) -> Check:
    try:
        await deps.token_check()
    except Exception as exc:  # any failure means the token is unusable
        return _error("token", f"Questrade token refresh failed: {_exc_text(exc)}")
    return _ok("token", "Questrade token OK")


async def _market_data(deps: PreopenDeps, session_date: date, settings: RuntimeSettings) -> Check:
    """FIX-401: a minted token is not proof that Questrade serves market data (on 09-29 it refused every
    call with HTTP 401). One real quote; a 401/403 (or any other failure) fails the check and alerts."""
    assert deps.market_check is not None
    try:
        ticker = await deps.market_check(session_date)
    except QuestradeApiError as exc:
        if exc.status in (401, 403):
            return _error("market_data", f"Questrade refused market data: {_one_line(exc.summary)}")
        return _error("market_data", f"Questrade market data call failed: {_one_line(exc.summary)}")
    if ticker is None:
        return _warning("market_data", "no symbol with a Questrade id to quote (market data not checked)")
    return _ok("market_data", f"Questrade market data OK (HTTP 200, quote {ticker})")


async def _universe(deps: PreopenDeps, session_date: date, settings: RuntimeSettings) -> Check:
    st = await deps.universe_status(session_date)
    if st.source is None:
        return _error("universe", "no universe for the session (the nightly job did not store one)")
    if st.stale:
        since = st.fallback_from.isoformat() if st.fallback_from else "an unknown date"
        return _error("universe", f"stale fallback universe from {since} ({_sessions(st.age_sessions)})")
    if st.fallback_from is not None or st.source == "fallback":
        since = st.fallback_from.isoformat() if st.fallback_from else "an unknown date"
        return _warning("universe", f"fallback universe from {since} ({_sessions(st.age_sessions)})")
    return _ok("universe", f"universe from {st.source}")


async def _open_bar_stats(deps: PreopenDeps, session_date: date, settings: RuntimeSettings) -> Check:
    with deps.factory() as s:
        n = s.execute(
            select(func.count()).select_from(m.OpenBarStat).where(m.OpenBarStat.session_date == session_date)
        ).scalar_one()
    if n > 0:
        return _ok("open_bar_stats", f"{n} symbols")
    return _error(
        "open_bar_stats", "no opening-bar stats for the session (the 9:35 scan has no volume baseline)"
    )


async def _premarket(deps: PreopenDeps, session_date: date, settings: RuntimeSettings) -> Check:
    with deps.factory() as s:
        n = s.execute(
            select(func.count())
            .select_from(m.JobRun)
            .where(
                m.JobRun.job == "premarket",
                m.JobRun.session_date == session_date,
                m.JobRun.status == "succeeded",
            )
        ).scalar_one()
    if n > 0:
        return _ok("premarket", "pre-market scan ran")
    return _warning("premarket", "no successful pre-market scan for the session (no catalysts)")


async def _kill_switches(deps: PreopenDeps, session_date: date, settings: RuntimeSettings) -> Check:
    active = [a.switch for a in deps.killswitches.active(deps.run_id, session_date)]
    if not active:
        return _ok("kill_switches", "none active")
    automatic = [sw for sw in active if sw != MANUAL_PAUSE]
    if automatic:
        return _error("kill_switches", f"{', '.join(active)} active: entries blocked today")
    return _warning("kill_switches", f"{MANUAL_PAUSE} active: entries blocked until /resume")


async def _worker(deps: PreopenDeps, session_date: date, settings: RuntimeSettings) -> Check:
    with deps.factory() as s:
        row = s.execute(
            select(m.WorkerHeartbeat.beat_at, m.WorkerHeartbeat.phase).where(
                m.WorkerHeartbeat.process == WORKER_PROCESS
            )
        ).one_or_none()
    down = "worker not running: approvals and fills will not happen"
    if row is None:
        return _error("worker", f"{down} (no heartbeat)")
    age = (deps.clock.now() - row.beat_at).total_seconds()
    if age > settings.worker_heartbeat_stale_seconds:
        return _error("worker", f"{down} (last heartbeat {int(age)}s ago)")
    if row.phase in STOPPED_PHASES:  # a fresh beat from a worker that is shutting down is no comfort
        return _error("worker", f"{down} (heartbeat phase {row.phase}, {int(age)}s ago)")
    return _ok("worker", f"heartbeat {int(age)}s ago")


CHECKS: tuple[tuple[str, Callable[[PreopenDeps, date, RuntimeSettings], Awaitable[Check]]], ...] = (
    ("token", _token),
    ("universe", _universe),
    ("open_bar_stats", _open_bar_stats),
    ("premarket", _premarket),
    ("kill_switches", _kill_switches),
    ("worker", _worker),
)


async def run_preopen(deps: PreopenDeps, session_date: date) -> dict[str, Any]:
    """The job detail: every Check as a dict, and `ok`."""
    if not deps.calendar.is_session(session_date):
        return dict(NOT_A_SESSION)
    settings = deps.settings()
    checks: list[Check] = []
    planned = list(CHECKS)
    if deps.market_check is not None:
        planned.insert(1, ("market_data", _market_data))  # right after the token
    for name, check in planned:
        try:
            checks.append(await check(deps, session_date, settings))
        except Exception as exc:  # a broken check is reported, never fatal
            log.warning("preopen.check_failed", check=name, error=type(exc).__name__)
            checks.append(_error(name, f"check failed: {_exc_text(exc)}"))
    ok = all(c.ok for c in checks)
    sent = False
    if not ok or settings.preopen_notify_when_ok:
        view = PreopenView(session_date, settings.approval_mode, tuple(checks))
        msg = deps.render.preopen(view)
        await deps.notifier.send(dataclasses.replace(msg, dedupe_key=f"preopen:{session_date.isoformat()}"))
        sent = True
    return {
        "session_date": session_date.isoformat(),
        "ok": ok,
        "approval_mode": settings.approval_mode,
        "checks": [dataclasses.asdict(c) for c in checks],
        "sent": sent,
    }
