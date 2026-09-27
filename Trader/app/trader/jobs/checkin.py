"""The 11:30 and 13:30 ET check-ins: a status push, plus a backup firing of every due session event
(SPEC §9, "entry-cancel event at 11:30").

The status is rebuilt here from the database (the same reads as `/status`, without importing the Telegram
commands). A failure to build or send it is logged as an `error` event (source `job.checkin`, which the
relay alerts) and never stops the backup firing, which is the check-in's safety half. After the session
close (13:30 on an early-close day) the check-in does nothing.
"""

import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import PROVIDER
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches
from trader.engine.scheduler import DayPlan, FireResult, due_events
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.sessions import session_phase
from trader.notify.types import Notifier, PositionLine, Renderer, StatusView
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("jobs.checkin")

NOT_A_SESSION = {"skipped": "not a session"}
AFTER_CLOSE = {"skipped": "after close"}
SOURCE = "job.checkin"
WORKER_PROCESS = "worker"
TOKEN_MAX_AGE = timedelta(hours=26)  # the daily 02:00 refresh plus slack (as /status)


@dataclass(frozen=True)
class CheckinDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    run_id: int
    notifier: Notifier
    render: Renderer
    plan: Callable[[date], DayPlan]
    fired: Callable[[date], set[str]]
    fire: Callable[[str, date], Awaitable[FireResult]]
    quotes: Callable[[Sequence[int]], Awaitable[Mapping[int, QtQuote]]] | None


# --- the status view --------------------------------------------------------------------------------------
def _token(s: Session, now: datetime) -> tuple[bool, float | None, str | None]:
    """(ok, hours since the last refresh, reason when not OK). Reads no token value."""
    row = s.execute(
        select(
            m.ApiCredential.refresh_token_enc.is_not(None),
            m.ApiCredential.last_refresh_at,
            m.ApiCredential.last_error,
        ).where(m.ApiCredential.provider == PROVIDER)
    ).one_or_none()
    if row is None or not row[0]:
        return False, None, "Questrade token not seeded"
    _, last_refresh_at, last_error = row
    age = (now - last_refresh_at).total_seconds() / 3600 if last_refresh_at is not None else None
    if last_error:
        return False, age, str(last_error)
    if last_refresh_at is None:
        return False, None, "Questrade token never refreshed"
    if now - last_refresh_at > TOKEN_MAX_AGE:
        return False, age, f"last refresh {age:.1f} h ago (over {TOKEN_MAX_AGE.total_seconds() / 3600:.0f} h)"
    return True, age, None


async def _last_prices(deps: CheckinDeps, symbol_ids: Sequence[int]) -> dict[int, Decimal]:
    if deps.quotes is None or not symbol_ids:
        return {}
    try:
        quotes = await deps.quotes(symbol_ids)
    except Exception as exc:  # a quote failure never fails the check-in: prices show as n/a
        log.warning("checkin.quotes_failed", error=type(exc).__name__)
        return {}
    prices: dict[int, Decimal] = {}
    for sid, q in quotes.items():
        last = q.last if q.last is not None else q.last_regular
        if last is not None:
            prices[sid] = last
    return prices


async def _positions(deps: CheckinDeps, now: datetime) -> tuple[PositionLine, ...]:
    with deps.factory() as s:
        rows = s.execute(
            select(m.Position, m.Symbol.ticker)
            .join(m.Symbol, m.Symbol.id == m.Position.symbol_id)
            .where(m.Position.run_id == deps.run_id, m.Position.closed_at.is_(None), m.Position.qty > 0)
            .order_by(m.Position.opened_at, m.Position.id)
        ).all()
        stops: dict[int, Decimal] = {}
        for position_id, stop_price in s.execute(
            select(m.Order.position_id, m.Order.stop_price)
            .where(
                m.Order.run_id == deps.run_id,
                m.Order.purpose == "stop",
                m.Order.status == "working",
                m.Order.stop_price.is_not(None),
                m.Order.position_id.in_([p.id for p, _ in rows]),
            )
            .order_by(m.Order.id)
        ).all():
            if position_id is not None and stop_price is not None:
                stops[position_id] = stop_price
    prices = await _last_prices(deps, list(dict.fromkeys(p.symbol_id for p, _ in rows)))
    lines: list[PositionLine] = []
    for p, ticker in rows:
        last = prices.get(p.symbol_id)
        working = p.id in stops
        unprotected = p.unprotected_seconds
        if p.unprotected_since is not None:
            unprotected += max(0, int((now - p.unprotected_since).total_seconds()))
        lines.append(
            PositionLine(
                position_id=p.id,
                ticker=ticker,
                qty=p.qty,
                entry=p.avg_price,
                last=last,
                stop=stops[p.id] if working else p.stop_loss,
                unrealized_pnl=(last - p.avg_price) * p.qty if last is not None else None,
                unprotected_seconds=unprotected,
                stop_working=working,
            )
        )
    return tuple(lines)


async def _status(
    deps: CheckinDeps, session_date: date, now: datetime, plan: DayPlan, fired: set[str]
) -> StatusView:
    settings = deps.settings()
    upcoming = [e for e in plan.events if e.key not in fired]
    switches = KillSwitches(deps.factory, deps.clock).active(deps.run_id, session_date)
    with deps.factory() as s:
        token_ok, token_age, token_error = _token(s, now)
        beat_at = s.execute(
            select(m.WorkerHeartbeat.beat_at).where(m.WorkerHeartbeat.process == WORKER_PROCESS)
        ).scalar_one_or_none()
        pending = s.execute(
            select(func.count())
            .select_from(m.Proposal)
            .where(m.Proposal.run_id == deps.run_id, m.Proposal.status == "pending")
        ).scalar_one()
    return StatusView(
        now=now,
        phase=session_phase(deps.calendar, now),
        session_date=session_date,
        next_event_key=upcoming[0].key if upcoming else None,
        next_event_at=upcoming[0].at if upcoming else None,
        approval_mode=settings.approval_mode,
        blocking_switches=tuple(a.switch for a in switches),
        token_ok=token_ok,
        token_age_hours=token_age,
        token_error=token_error,
        heartbeat_age_seconds=(now - beat_at).total_seconds() if beat_at is not None else None,
        positions=await _positions(deps, now),
        pending_count=int(pending),
    )


# --- the job ----------------------------------------------------------------------------------------------
async def run_checkin(deps: CheckinDeps, session_date: date, at_label: str) -> dict[str, Any]:
    if not deps.calendar.is_session(session_date):
        return dict(NOT_A_SESSION)
    now = deps.clock.now()
    if now >= deps.calendar.session_close(session_date):
        return dict(AFTER_CLOSE)
    plan = deps.plan(session_date)
    fired = deps.fired(session_date)
    detail: dict[str, Any] = {"session_date": session_date.isoformat(), "at": at_label}
    try:
        view = await _status(deps, session_date, now, plan, fired)
        msg = deps.render.checkin(view, at_label)
        dedupe = f"checkin:{session_date.isoformat()}:{at_label}"
        await deps.notifier.send(dataclasses.replace(msg, dedupe_key=dedupe))
        detail["sent"] = True
    except Exception as exc:  # the status is informative; the backup firing below must still happen
        error = type(exc).__name__
        log.error("checkin.status_failed", error=error)
        detail["sent"] = False
        detail["error"] = error
        try:
            with session_scope(deps.factory) as s:
                log_event(
                    s,
                    deps.clock,
                    "error",
                    SOURCE,
                    f"check-in {at_label}: status not sent ({error})",
                    {"session_date": session_date.isoformat(), "at": at_label, "error": error},
                    deps.run_id,
                )
        except Exception as db_exc:  # the database may be the reason; the log line above remains
            log.error("checkin.event_log_failed", error=type(db_exc).__name__)
    results: list[dict[str, Any]] = []
    for event in due_events(plan, now, fired):
        result = await deps.fire(event.key, session_date)
        results.append({"key": result.key, "status": result.status})
    detail["fired"] = results
    return detail
