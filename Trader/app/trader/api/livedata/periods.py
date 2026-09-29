"""Period P&L and costs (live dashboard plan S4, S5): today, week and run of the live run, in America/New_York
dates.

- `today` is the session day (today's ET date when it is a session, else the latest session before it);
  `week` runs from the Monday of today's ET week to today's ET date; `run` from the ET date of the run's start
  to today's ET date.
- Date-keyed rows (`trades.session_date`, `catalysts.session_date`) compare dates; timestamp-keyed rows
  (`fills.ts`, `weekly_reports.updated_at`) use the window's ET day bounds, computed with `zoneinfo` so a
  DST-change day is 23 or 25 hours long.
- `realized` is Σ `trades.pnl` (already net of both fees); `fees` (paid in the period) is informational and is
  never subtracted again. The counts and ratios come from `trader.reports.metrics.metrics_from_rows`, so they
  equal `/api/metrics` for the same dates.
- Only the given run's trades and fills are read, so replay and other runs' rows never enter.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.types import OpenValue, PeriodWindow
from trader.api.schemas import ClaudeTodayOut, PeriodKey, PeriodPnlOut
from trader.broker.types import Fees
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, et_date
from trader.reports.metrics import TradeRow, metrics_from_rows
from trader.reports.weekly import claude_spent
from trader.settings_store import RuntimeSettings

Q4 = Decimal("0.0001")
ZERO = Decimal(0)


def q4(value: Decimal) -> Decimal:
    """Round to 4 dp half up (the `numeric(14,4)` columns' rounding)."""
    return value.quantize(Q4, ROUND_HALF_UP)


def fee_total(fees_json: Mapping[str, Any]) -> Decimal:
    """A fill's `fees.total` rounded the way the ledger's `fee` row stores it."""
    return q4(Fees.from_json(fees_json).total)


def et_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[00:00 ET of `d`, 00:00 ET of `d` + 1 day) as UTC datetimes (a DST-change day is 23 or 25 hours)."""
    start = datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC)
    end = datetime.combine(d + timedelta(days=1), time(0), tzinfo=ET).astimezone(UTC)
    return start, end


def session_day(calendar: SessionCalendar, now: datetime) -> date:
    """Today's ET date when it is a session, else the latest session before it."""
    today = et_date(now)
    return today if calendar.is_session(today) else calendar.previous_session(today)


def _window(key: PeriodKey, date_from: date, date_to: date) -> PeriodWindow:
    start, _ = et_day_bounds(date_from)
    _, end = et_day_bounds(date_to)
    return PeriodWindow(key=key, date_from=date_from, date_to=date_to, start_at=start, end_at=end)


def period_windows(
    calendar: SessionCalendar, now: datetime, run_started_at: datetime
) -> tuple[PeriodWindow, PeriodWindow, PeriodWindow]:
    """The (today, week, run) windows of S5."""
    today = et_date(now)
    day = session_day(calendar, now)
    monday = today - timedelta(days=today.weekday())
    run_start = min(et_date(run_started_at), today)
    return _window("today", day, day), _window("week", monday, today), _window("run", run_start, today)


def claude_spent_between(factory: sessionmaker[Session], window: PeriodWindow) -> Decimal:
    """Claude spend in the window: catalysts of its session dates plus weekly reports updated within its ET
    bounds (the sources of `trader.reports.weekly.claude_spent`, over a range)."""
    with factory() as s:
        catalysts = s.execute(
            select(func.coalesce(func.sum(m.Catalyst.cost_usd), 0)).where(
                m.Catalyst.session_date >= window.date_from, m.Catalyst.session_date <= window.date_to
            )
        ).scalar_one()
        reports = s.execute(
            select(func.coalesce(func.sum(m.WeeklyReport.cost_usd), 0)).where(
                m.WeeklyReport.updated_at >= window.start_at, m.WeeklyReport.updated_at < window.end_at
            )
        ).scalar_one()
    return Decimal(catalysts) + Decimal(reports)


def open_pnl(open_values: Sequence[OpenValue]) -> tuple[Decimal | None, bool]:
    """(Σ over marked positions of (mark - avg) x qty - entry fees, whether some position lacks a mark).
    The value is None when no position has a mark."""
    marked = [v for v in open_values if v.mark is not None]
    partial = len(marked) < len(open_values)
    if not marked:
        return None, partial
    total = sum(((v.mark - v.avg_price) * v.qty - v.entry_fees for v in marked if v.mark is not None), ZERO)
    return q4(total), partial


def period_blocks(
    factory: sessionmaker[Session],
    run_id: int,
    windows: Sequence[PeriodWindow],
    open_values: Sequence[OpenValue],
) -> list[PeriodPnlOut]:
    """One `PeriodPnlOut` per window (S4), from one query of the run's trades and one of its fills."""
    t, f = m.Trade, m.Fill
    with factory() as s:
        trades = s.execute(
            select(t.session_date, t.pnl, t.pnl_r, t.qty, t.slippage_total, t.fees_total)
            .where(t.run_id == run_id)
            .order_by(t.closed_at, t.id)
        ).all()
        fills = s.execute(select(f.ts, f.fees).where(f.run_id == run_id)).all()
    fill_fees = [(ts, fee_total(fees)) for ts, fees in fills]
    unrealized, partial = open_pnl(open_values)
    blocks: list[PeriodPnlOut] = []
    for w in windows:
        rows = [
            TradeRow(
                pnl=r.pnl, pnl_r=r.pnl_r, qty=r.qty, slippage_total=r.slippage_total, fees_total=r.fees_total
            )
            for r in trades
            if w.date_from <= r.session_date <= w.date_to
        ]
        metrics = metrics_from_rows(run_id, w.date_from, w.date_to, rows, [], [])
        realized = q4(metrics.total_pnl)
        pnl_after_fees = q4(realized + (unrealized if unrealized is not None else ZERO))
        claude = q4(claude_spent_between(factory, w))
        blocks.append(
            PeriodPnlOut(
                period=w.key,
                date_from=w.date_from,
                date_to=w.date_to,
                realized=realized,
                unrealized=unrealized,
                unrealized_partial=partial,
                pnl_after_fees=pnl_after_fees,
                fees=q4(sum((fee for ts, fee in fill_fees if w.start_at <= ts < w.end_at), ZERO)),
                claude_usd=claude,
                net_after_ai=pnl_after_fees - claude,
                trades=metrics.trades,
                wins=metrics.wins,
                losses=metrics.losses,
                win_rate=metrics.win_rate,
                expectancy_r=metrics.expectancy_r,
                trades_without_r=metrics.trades_without_r,
            )
        )
    return blocks


def claude_today(factory: sessionmaker[Session], now: datetime, settings: RuntimeSettings) -> ClaudeTodayOut:
    """Today's (ET) Claude spend against `claude_daily_budget_usd`, the budget the catalyst classifier and the
    weekly report check."""
    day = et_date(now)
    spent = claude_spent(factory, day)
    cap = settings.claude_daily_budget_usd
    return ClaudeTodayOut(
        date=day, spent_usd=spent, cap_usd=cap, used_fraction=q4(spent / cap) if cap else None
    )
