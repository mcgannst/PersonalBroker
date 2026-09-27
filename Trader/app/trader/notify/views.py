"""View builders shared by every Telegram path (P3-T12), so two callers can't show different numbers.

- `proposal_view`: the ProposalView of a proposal row, used by the bot (approval messages, closed
  messages) and the relay (auto-mode messages). `risk_usd` is the entry's actual dollar risk: quantity x
  per-share risk (the sizing's `per_share_risk`, else entry price - stop loss); None for other kinds.
- `status_view` and `position_lines`: the StatusView of `/status` and of the 11:30/13:30 check-ins.
  `position_lines` is `open_position_rows` (the database part), `last_prices` and `lines_from`, which the
  web API calls separately so its database work stays off the event loop.
- `pnl_view`: the `/pnl` numbers, shared by Telegram and the web dashboard.

Read-only: nothing here writes to the database.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import PROVIDER, TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.scheduler import DayPlan
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.market.sessions import current_session, session_phase
from trader.notify.types import PnlView, PositionLine, ProposalView, StatusView
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("notify.views")

Q4 = Decimal("0.0001")
TOKEN_MAX_AGE_HOURS = 26.0  # the daily 02:00 refresh plus slack
WORKER_PROCESS = "worker"  # the worker's heartbeat row (the one definition: runtime and preopen import it)
STOPPED_PHASES = frozenset({"stopping", "stopped"})  # heartbeat phases of a worker that is going away

Quotes = Callable[[Sequence[int]], Awaitable[Mapping[int, QtQuote]]]


def dec(value: Any) -> Decimal | None:
    """A Decimal from a jsonb value (string or number), or None."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


# --- proposals --------------------------------------------------------------------------------------------


def risk_usd(p: m.Proposal, spec: Mapping[str, Any]) -> Decimal | None:
    """Dollar risk of an entry: qty x per-share risk (from the sizing, else entry price - stop loss)."""
    if p.kind != "entry":
        return None
    sizing = p.sizing if isinstance(p.sizing, dict) else {}
    per_share = dec(sizing.get("per_share_risk"))
    if per_share is None:
        entry = dec(spec.get("stop")) or dec(spec.get("limit"))
        stop_loss = dec(spec.get("stop_loss"))
        if entry is None or stop_loss is None:
            return None
        per_share = entry - stop_loss
    return per_share * p.qty


def proposal_view(s: Session, p: m.Proposal) -> ProposalView:
    """The view of one proposal row (its symbol from the order spec, else the cancelled order, else the
    signal; its reason from the order spec, else the signal's intent)."""
    spec: dict[str, Any] = p.order_spec if isinstance(p.order_spec, dict) else {}
    sig = s.get(m.Signal, p.signal_id)
    cfg = s.get(m.StrategyConfig, sig.strategy_config_id) if sig is not None else None
    cancelled = s.get(m.Order, p.cancel_order_id) if p.cancel_order_id is not None else None
    symbol_id = spec.get("symbol_id")
    if symbol_id is None and cancelled is not None:
        symbol_id = cancelled.symbol_id
    if symbol_id is None and sig is not None:
        symbol_id = sig.symbol_id
    symbol = s.get(m.Symbol, symbol_id) if symbol_id is not None else None
    intent: dict[str, Any] = sig.intent if sig is not None and isinstance(sig.intent, dict) else {}
    return ProposalView(
        proposal_id=p.id,
        kind=p.kind,
        status=p.status,
        ticker=symbol.ticker if symbol is not None else "?",
        side=str(spec.get("side") or (cancelled.side if cancelled is not None else "")),
        order_type=str(spec.get("order_type") or (cancelled.order_type if cancelled is not None else "")),
        qty=p.qty,
        stop=dec(spec.get("stop")),
        limit=dec(spec.get("limit")),
        stop_loss=dec(spec.get("stop_loss")),
        risk_usd=risk_usd(p, spec),
        reason=str(spec.get("reason") or intent.get("reason") or ""),
        strategy_key=cfg.strategy_key if cfg is not None else "",
        created_at=p.created_at,
        expires_at=p.expires_at,
        decided_via=p.decided_via,
        error=p.error,
        decided_at=p.decided_at,
    )


# --- status -----------------------------------------------------------------------------------------------


def db_token_health(factory: sessionmaker[Session]) -> TokenHealth:
    """QuestradeAuth.health() without the crypto: the token chain's state, reading no token value."""
    with factory() as s:
        row = s.get(m.ApiCredential, PROVIDER)
        if row is None:
            return TokenHealth(False, None, None, None)
        return TokenHealth(bool(row.refresh_token_enc), row.expires_at, row.last_refresh_at, row.last_error)


def token_state(
    token_health: Callable[[], TokenHealth], now: datetime
) -> tuple[bool, float | None, str | None]:
    """(ok, hours since the last refresh, the reason it is not OK). A failing health check is shown, never
    raised."""
    try:
        health = token_health()
    except Exception as exc:
        log.warning("views.token_health_failed", error_type=type(exc).__name__)
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


def heartbeat_age(factory: sessionmaker[Session], now: datetime) -> float | None:
    """Seconds since the worker's last beat; None when there is no row or the worker said it is going
    away (phase `stopping` or `stopped`: a shutdown, so it is not running however recent the beat; the
    same rule as the pre-open check)."""
    with factory() as s:
        row = s.execute(
            select(m.WorkerHeartbeat.beat_at, m.WorkerHeartbeat.phase).where(
                m.WorkerHeartbeat.process == WORKER_PROCESS
            )
        ).one_or_none()
    if row is None or row[1] in STOPPED_PHASES:
        return None
    beat_at: datetime = row[0]
    return (now - beat_at).total_seconds()


def pending_count(factory: sessionmaker[Session], run_id: int) -> int:
    with factory() as s:
        return int(
            s.execute(
                select(func.count())
                .select_from(m.Proposal)
                .where(m.Proposal.run_id == run_id, m.Proposal.status == "pending")
            ).scalar_one()
        )


async def last_prices(quotes: Quotes | None, symbol_ids: Sequence[int]) -> dict[int, Decimal]:
    """The last price of each symbol (`last`, else `last_regular`); empty when the quotes fail."""
    if quotes is None or not symbol_ids:
        return {}
    try:
        got = await quotes(symbol_ids)
    except Exception as exc:  # a quote failure never fails a status: prices show as n/a
        log.warning("views.quotes_failed", error_type=type(exc).__name__)
        return {}
    prices: dict[int, Decimal] = {}
    for sid, q in got.items():
        last = q.last if q.last is not None else q.last_regular
        if last is not None:
            prices[sid] = last
    return prices


@dataclass(frozen=True, slots=True)
class OpenPositionRows:
    """The run's open positions (with their tickers), oldest first, and each one's working stop price."""

    positions: tuple[tuple[m.Position, str], ...]
    stops: Mapping[int, Decimal]

    @property
    def symbol_ids(self) -> list[int]:
        return list(dict.fromkeys(p.symbol_id for p, _ in self.positions))


def open_position_rows(factory: sessionmaker[Session], run_id: int) -> OpenPositionRows:
    """The database half of `position_lines` (synchronous: callers on an event loop run it in a thread)."""
    with factory() as s:
        rows = s.execute(
            select(m.Position, m.Symbol.ticker)
            .join(m.Symbol, m.Symbol.id == m.Position.symbol_id)
            .where(m.Position.run_id == run_id, m.Position.closed_at.is_(None))
            .order_by(m.Position.opened_at, m.Position.id)
        ).all()
        positions = [(p, ticker) for p, ticker in rows]
        stops: dict[int, Decimal] = {}
        if positions:
            for position_id, stop_price in s.execute(
                select(m.Order.position_id, m.Order.stop_price)
                .where(
                    m.Order.run_id == run_id,
                    m.Order.position_id.in_([p.id for p, _ in positions]),
                    m.Order.purpose == "stop",
                    m.Order.status == "working",
                    m.Order.stop_price.is_not(None),
                )
                .order_by(m.Order.id)
            ).all():
                if position_id is not None and stop_price is not None:
                    stops[position_id] = stop_price  # the newest working stop wins
    return OpenPositionRows(tuple(positions), stops)


def lines_from(
    rows: OpenPositionRows, prices: Mapping[int, Decimal], now: datetime
) -> tuple[PositionLine, ...]:
    """The position lines of `rows` with the given last prices (pure: reads nothing)."""
    stops = rows.stops
    lines: list[PositionLine] = []
    for p, ticker in rows.positions:
        last = prices.get(p.symbol_id)
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


async def position_lines(
    factory: sessionmaker[Session], clock: Clock, run_id: int, quotes: Quotes | None
) -> tuple[PositionLine, ...]:
    """Open positions of the run, oldest first: the last price, the stop (the newest working stop order's
    price, else the position's stop loss with `stop_working` False) and the unprotected time so far."""
    now = clock.now()
    rows = open_position_rows(factory, run_id)
    prices = await last_prices(quotes, rows.symbol_ids)
    return lines_from(rows, prices, now)


def _trade_pnl(s: Session, run_id: int, since: date, until: date | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(m.Trade.pnl), 0)).where(
        m.Trade.run_id == run_id, m.Trade.session_date >= since
    )
    if until is not None:
        q = q.where(m.Trade.session_date <= until)
    return Decimal(s.execute(q).scalar_one())


def pnl_view(
    factory: sessionmaker[Session],
    calendar: SessionCalendar,
    now: datetime,
    run_id: int,
    lines: Sequence[PositionLine],
) -> PnlView:
    """The `/pnl` numbers (synchronous): today's realized P&L (the current session), the unrealized P&L of
    `lines` (positions without a quote are left out), the week to date (from the Monday of the current ET
    week) and equity/drawdown from the latest snapshot (or the starting cash when none)."""
    session_date = current_session(calendar, now)
    today = et_date(now)
    monday = today - timedelta(days=today.weekday())
    with factory() as s:
        realized = _trade_pnl(s, run_id, session_date, session_date)
        week = _trade_pnl(s, run_id, monday)
        snap = s.execute(
            select(m.EquitySnapshot)
            .where(m.EquitySnapshot.run_id == run_id)
            .order_by(m.EquitySnapshot.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is not None:
            equity, peak, drawdown = snap.equity, snap.peak_equity, snap.drawdown_pct
        else:
            cash = s.execute(
                select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == run_id)
            ).scalar_one_or_none()
            equity = peak = cash if cash is not None else Decimal(0)
            drawdown = Decimal(0)
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


def next_event(
    calendar: SessionCalendar,
    now: datetime,
    session_date: date,
    plan: Callable[[date], DayPlan],
    fired: Callable[[date], set[str]],
) -> tuple[str | None, datetime | None]:
    """The first unfired planned event of today's session, or (None, None) outside it."""
    if session_phase(calendar, now) not in ("pre_market", "open"):
        return None, None
    day_plan = plan(session_date)
    if not day_plan.is_session:
        return None, None
    done = fired(session_date)
    for ev in day_plan.events:  # sorted by time, then key
        if ev.key not in done:
            return ev.key, ev.at
    return None, None


async def status_view(
    *,
    factory: sessionmaker[Session],
    clock: Clock,
    calendar: SessionCalendar,
    settings: RuntimeSettings,
    killswitches: KillSwitches,
    run_id: int,
    plan: Callable[[date], DayPlan],
    fired: Callable[[date], set[str]],
    token_health: Callable[[], TokenHealth],
    quotes: Quotes | None,
    session_date: date | None = None,
) -> StatusView:
    """The one status view of `/status` and the check-ins: phase and session, the next unfired event,
    approval mode, blocking kill switches, token health, worker heartbeat age, open positions and the
    pending proposal count. `session_date` defaults to the current (or next) session."""
    now = clock.now()
    day = session_date or current_session(calendar, now)
    next_key, next_at = next_event(calendar, now, day, plan, fired)
    token_ok, token_age, token_error = token_state(token_health, now)
    switches = killswitches.active(run_id, day)
    return StatusView(
        now=now,
        phase=session_phase(calendar, now),
        session_date=day,
        next_event_key=next_key,
        next_event_at=next_at,
        approval_mode=settings.approval_mode,
        blocking_switches=tuple(a.switch for a in switches),
        token_ok=token_ok,
        token_age_hours=token_age,
        token_error=token_error,
        heartbeat_age_seconds=heartbeat_age(factory, now),
        positions=await position_lines(factory, clock, run_id, quotes),
        pending_count=pending_count(factory, run_id),
    )
