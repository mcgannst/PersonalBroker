"""Remote Telegram commands (SPEC §4.4): /status, /positions, /pnl, /pending, /pause (confirmed), /resume,
/help. BR-34, BR-30, BR-33.

The view builders are async (a P3-T1 refinement of the plan's signatures) because the last prices come from
the async `quotes` callable. Read-only commands only read; only the /pause confirmation and /resume change
state, through KillSwitches (which writes the audit rows). /resume never resets an automatic switch.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.adapters.telegram.types import CallbackIssuer, ProposalMessenger
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.scheduler import DayPlan
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.market.sessions import current_session, session_phase
from trader.notify.types import Button, OutboundMessage, PnlView, PositionLine, Renderer, StatusView
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("telegram.commands")

Q4 = Decimal("0.0001")
TOKEN_MAX_AGE_HOURS = 26.0
ACTOR = "telegram"
UNKNOWN = "Unknown command. Try /help."
MANUAL_PAUSE = "manual_pause"


@dataclass(frozen=True)
class CommandDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    killswitches: KillSwitches
    run_id: int
    chat_id: int
    plan: Callable[[date], DayPlan]
    fired: Callable[[date], set[str]]
    token_health: Callable[[], TokenHealth]
    quotes: Callable[[Sequence[int]], Awaitable[Mapping[int, QtQuote]]] | None
    messenger: ProposalMessenger
    issuer: CallbackIssuer
    render: Renderer


# --- view builders ----------------------------------------------------------------------------------------


def _next_event(deps: CommandDeps, now: datetime, session_date: date) -> tuple[str | None, datetime | None]:
    """The first unfired planned event of today's session, or (None, None) outside it."""
    if session_phase(deps.calendar, now) not in ("pre_market", "open"):
        return None, None
    plan = deps.plan(session_date)
    if not plan.is_session:
        return None, None
    fired = deps.fired(session_date)
    for ev in plan.events:  # sorted by time, then key
        if ev.key not in fired:
            return ev.key, ev.at
    return None, None


def _token(deps: CommandDeps, now: datetime) -> tuple[bool, float | None, str | None]:
    """(ok, hours since the last refresh, the reason it is not OK)."""
    try:
        health = deps.token_health()
    except Exception as exc:  # a health check failure is shown, never fails /status
        log.warning("telegram.commands.token_health_failed", error_type=type(exc).__name__)
        return False, None, f"token health unavailable ({type(exc).__name__})"
    age = (now - health.last_refresh_at).total_seconds() / 3600 if health.last_refresh_at else None
    if not health.seeded:
        return False, age, "not seeded"
    if health.last_error:
        return False, age, health.last_error
    if age is None:
        return False, None, "never refreshed"
    if age > TOKEN_MAX_AGE_HOURS:
        return False, age, f"last refresh {age:.1f} h ago (over {TOKEN_MAX_AGE_HOURS:.0f} h)"
    return True, age, None


def _heartbeat_age(deps: CommandDeps, now: datetime) -> float | None:
    """Seconds since the worker's last beat; None when there is no row or the worker said it stopped
    (phase `stopped`: a clean shutdown, so it is not running however recent the beat)."""
    with deps.factory() as s:
        row = s.execute(
            select(m.WorkerHeartbeat.beat_at, m.WorkerHeartbeat.phase).where(
                m.WorkerHeartbeat.process == "worker"
            )
        ).one_or_none()
    if row is None:
        return None
    beat_at: datetime = row[0]
    phase: str = row[1]
    if phase == "stopped":
        return None
    return (now - beat_at).total_seconds()


def _pending_ids(deps: CommandDeps) -> list[int]:
    with deps.factory() as s:
        return list(
            s.execute(
                select(m.Proposal.id)
                .where(m.Proposal.run_id == deps.run_id, m.Proposal.status == "pending")
                .order_by(m.Proposal.created_at, m.Proposal.id)
            ).scalars()
        )


async def _last_prices(deps: CommandDeps, symbol_ids: list[int]) -> Mapping[int, QtQuote]:
    if deps.quotes is None or not symbol_ids:
        return {}
    try:
        return await deps.quotes(symbol_ids)
    except Exception as exc:  # a quote failure never fails the command: prices show as n/a
        log.warning("telegram.commands.quotes_failed", error_type=type(exc).__name__)
        return {}


async def position_lines(deps: CommandDeps) -> tuple[PositionLine, ...]:
    """Open positions of the live run, oldest first, with the last price, stop and unprotected time."""
    now = deps.clock.now()
    with deps.factory() as s:
        rows = s.execute(
            select(m.Position, m.Symbol.ticker)
            .join(m.Symbol, m.Symbol.id == m.Position.symbol_id)
            .where(m.Position.run_id == deps.run_id, m.Position.closed_at.is_(None))
            .order_by(m.Position.opened_at, m.Position.id)
        ).all()
        positions = [(p, ticker) for p, ticker in rows]
        stops: dict[int, Decimal | None] = {}
        if positions:
            for pos_id, price in s.execute(
                select(m.Order.position_id, m.Order.stop_price)
                .where(
                    m.Order.run_id == deps.run_id,
                    m.Order.position_id.in_([p.id for p, _ in positions]),
                    m.Order.purpose == "stop",
                    m.Order.status == "working",
                )
                .order_by(m.Order.id)
            ).all():
                if pos_id is not None:
                    stops[pos_id] = price  # the newest working stop wins
    quotes = await _last_prices(deps, sorted({p.symbol_id for p, _ in positions}))
    lines: list[PositionLine] = []
    for p, ticker in positions:
        q = quotes.get(p.symbol_id)
        last = q.last if q is not None else None
        unrealized = ((last - p.avg_price) * p.qty).quantize(Q4, ROUND_HALF_UP) if last is not None else None
        unprotected = p.unprotected_seconds
        if p.unprotected_since is not None:
            unprotected += max(0, int((now - p.unprotected_since).total_seconds()))
        working = p.id in stops
        lines.append(
            PositionLine(
                position_id=p.id,
                ticker=ticker,
                qty=p.qty,
                entry=p.avg_price,
                last=last,
                stop=stops[p.id] if working else p.stop_loss,
                unrealized_pnl=unrealized,
                unprotected_seconds=unprotected,
                stop_working=working,
            )
        )
    return tuple(lines)


async def status_view(deps: CommandDeps) -> StatusView:
    now = deps.clock.now()
    session_date = current_session(deps.calendar, now)
    next_key, next_at = _next_event(deps, now, session_date)
    token_ok, token_age, token_error = _token(deps, now)
    switches = deps.killswitches.active(deps.run_id, session_date)
    return StatusView(
        now=now,
        phase=session_phase(deps.calendar, now),
        session_date=session_date,
        next_event_key=next_key,
        next_event_at=next_at,
        approval_mode=deps.settings().approval_mode,
        blocking_switches=tuple(a.switch for a in switches),
        token_ok=token_ok,
        token_age_hours=token_age,
        token_error=token_error,
        heartbeat_age_seconds=_heartbeat_age(deps, now),
        positions=await position_lines(deps),
        pending_count=len(_pending_ids(deps)),
    )


def _trade_pnl(s: Session, run_id: int, since: date, until: date | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(m.Trade.pnl), 0)).where(
        m.Trade.run_id == run_id, m.Trade.session_date >= since
    )
    if until is not None:
        q = q.where(m.Trade.session_date <= until)
    return Decimal(s.execute(q).scalar_one())


async def pnl_view(deps: CommandDeps, lines: Sequence[PositionLine] | None = None) -> PnlView:
    """Today's realized P&L, the unrealized P&L of open positions, the week to date (from the Monday of the
    current ET week) and equity/drawdown from the latest snapshot (or the starting cash when none).
    Positions without a quote are left out of `unrealized` (the /pnl handler then says it is partial).
    `lines` reuses already-built position lines (one quote fetch per command)."""
    now = deps.clock.now()
    session_date = current_session(deps.calendar, now)
    today = et_date(now)
    monday = today - timedelta(days=today.weekday())
    with deps.factory() as s:
        realized = _trade_pnl(s, deps.run_id, session_date, session_date)
        week = _trade_pnl(s, deps.run_id, monday)
        snap = s.execute(
            select(m.EquitySnapshot)
            .where(m.EquitySnapshot.run_id == deps.run_id)
            .order_by(m.EquitySnapshot.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is not None:
            equity, peak, drawdown = snap.equity, snap.peak_equity, snap.drawdown_pct
        else:
            cash = s.execute(
                select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == deps.run_id)
            ).scalar_one_or_none()
            equity = peak = cash if cash is not None else Decimal(0)
            drawdown = Decimal(0)
    if lines is None:
        lines = await position_lines(deps)
    unrealized = sum((ln.unrealized_pnl for ln in lines if ln.unrealized_pnl is not None), Decimal(0))
    return PnlView(
        session_date=session_date,
        realized_today=realized,
        unrealized=unrealized,
        week_to_date=week,
        equity=equity,
        peak_equity=peak,
        drawdown_pct=drawdown,
    )


# --- command handler -------------------------------------------------------------------------------------


def _reset_hint(switch: str) -> str:
    """How an automatic switch clears (killswitch.py): daily_loss_pct by itself at the next session; the
    others only by a reset with a reason in the web app."""
    if switch == "daily_loss_pct":
        return "clears by itself at the next session"
    return "reset it in the web app (with a reason)"


def parse_command(text: str) -> str | None:
    """The command word, lower-cased and without an `@BotName` suffix; None for plain text."""
    words = text.strip().split()
    if not words or not words[0].startswith("/"):
        return None
    return words[0].split("@", 1)[0].lower()


class Commands:
    """Implements trader.adapters.telegram.types.CommandHandler."""

    def __init__(self, deps: CommandDeps) -> None:
        self.deps = deps

    async def handle(self, text: str) -> list[OutboundMessage]:
        d = self.deps
        command = parse_command(text)
        if command == "/status":
            return [d.render.status(await status_view(d))]
        if command == "/positions":
            return [d.render.positions(await position_lines(d), d.clock.now())]
        if command == "/pnl":
            lines = await position_lines(d)
            out = [d.render.pnl(await pnl_view(d, lines))]
            unpriced = [ln.ticker for ln in lines if ln.unrealized_pnl is None]
            if unpriced:  # the unrealized figure leaves these out: say so rather than show a false total
                out.append(d.render.reply(f"Unrealized P&L is partial: no quote for {', '.join(unpriced)}."))
            return out
        if command == "/pending":
            return await self._pending()
        if command == "/pause":
            return [self._pause()]
        if command == "/resume":
            return [d.render.reply(self._resume())]
        if command == "/help":
            return [d.render.help()]
        return [d.render.reply(UNKNOWN)]

    async def _pending(self) -> list[OutboundMessage]:
        ids = _pending_ids(self.deps)
        if not ids:
            return [self.deps.render.reply("No pending proposals.")]
        for proposal_id in ids:
            await self.deps.messenger.send_proposal(proposal_id, resend=True)
        return []

    def _session(self) -> date:
        return current_session(self.deps.calendar, self.deps.clock.now())

    def _is_paused(self) -> bool:
        active = self.deps.killswitches.active(self.deps.run_id, self._session())
        return any(a.switch == MANUAL_PAUSE for a in active)

    def _pause(self) -> OutboundMessage:
        d = self.deps
        if self._is_paused():
            return d.render.reply("Already paused.")
        _, data = d.issuer.issue(
            "pause",
            str(d.run_id),
            ("y", "n"),
            d.chat_id,
            d.settings().telegram_confirm_ttl_seconds,
        )
        buttons = ((Button("Yes, pause", data["y"]), Button("No", data["n"])),)
        return d.render.pause_confirm(buttons)

    def _resume(self) -> str:
        d = self.deps
        if not d.killswitches.resume(d.run_id, actor=ACTOR):
            return "Not paused."
        still = [a.switch for a in d.killswitches.active(d.run_id, self._session())]
        if still:
            notes = "\n".join(f"- {switch}: {_reset_hint(switch)}" for switch in still)
            return f"Manual pause lifted.\nStill blocked by:\n{notes}"
        return "Manual pause lifted."

    async def confirm_pause(self, action: str) -> str:
        """The answer to a pause confirmation tap: `y` pauses new entries, anything else cancels."""
        if action != "y":
            return "Cancelled."
        d = self.deps
        if not d.killswitches.pause(d.run_id, self._session(), actor=ACTOR):
            return "Already paused."
        return "Paused: new entries are blocked."
