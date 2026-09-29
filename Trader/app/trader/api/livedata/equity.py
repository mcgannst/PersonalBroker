"""The equity series (live dashboard plan S6): snapshots, per-minute points from marks and candles (today
only) and a final `now` point, downsampled to at most 500 points.

- `snapshot` points are the run's stored `equity_snapshots` (of the session day's ET day for `today`, of the
  whole run for `run`).
- `marks` points (`today` only): one per complete minute of the session day's regular hours, up to `now`, in
  which a position of the run was open, stamped at the minute's end:
  `equity(m) = Σ cash_ledger rows with ts ≤ end of m + Σ over positions open at the end of m of
  qty x close of that symbol's bar for m` (the latest earlier bar's close of the day when m has none, else the
  position's avg price). A minute's bar is the stored 1-minute candle (`intraday_candles`, then
  `candle_archive`) when there is one, else the `mark_bars` row. Mark bars are only read as prices here, never
  turned into candles.
- a `now` point at `now` with the equity at the latest marks, when given (not for `today` once the session day
  is over, e.g. on a weekend).
- At one instant a snapshot wins over a marks point, which wins over the now point.
"""

from bisect import bisect_right
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.periods import ZERO, et_day_bounds, q4, session_day
from trader.api.livedata.types import EQUITY_MAX_POINTS, FILL_MARKERS_MAX
from trader.api.schemas import EquityPointLiveOut, EquitySeriesOut, EquitySource, FillMarkerOut, LiveRange
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import et_date

MINUTE = timedelta(minutes=1)
_US = timedelta(microseconds=1)
_PRIORITY: dict[EquitySource, int] = {"now": 0, "marks": 1, "snapshot": 2}


def downsample(
    points: Sequence[EquityPointLiveOut], max_points: int = EQUITY_MAX_POINTS
) -> tuple[list[EquityPointLiveOut], bool]:
    """At most `max_points` points keeping every bucket's extremes and the first and last point; the flag says
    whether anything was dropped.

    The time range is split into `max_points // 2` equal buckets; each non-empty bucket keeps its minimum- and
    maximum-equity points (the first of equal values). The first and last points are always kept; when adding
    them overflows the cap they replace a non-extreme point of their own bucket, then of the others. The
    global minimum and maximum are never dropped. Deterministic and O(n)."""
    n = len(points)
    if n <= max_points:
        return list(points), False
    cap = max(max_points, 4)
    buckets = max(cap // 2, 1)
    t0 = points[0].ts
    span = (points[-1].ts - t0) // _US
    lo: list[int | None] = [None] * buckets
    hi: list[int | None] = [None] * buckets
    for i, p in enumerate(points):
        b = 0 if span <= 0 else min(buckets - 1, ((p.ts - t0) // _US) * buckets // span)
        low, high = lo[b], hi[b]
        if low is None or p.equity < points[low].equity:
            lo[b] = i
        if high is None or p.equity > points[high].equity:
            hi[b] = i
    picks: list[list[int]] = [sorted({x for x in (lo[b], hi[b]) if x is not None}) for b in range(buckets)]
    keep = {i for bucket in picks for i in bucket} | {0, n - 1}
    g_lo = min(range(n), key=lambda i: (points[i].equity, i))
    g_hi = min(range(n), key=lambda i: (-points[i].equity, i))
    protected = {0, n - 1, g_lo, g_hi}
    ends = list(dict.fromkeys((0, buckets - 1)))
    for b in ends + [b for b in range(buckets) if b not in ends]:  # at most one point from each bucket
        if len(keep) <= cap:
            break
        removable = [i for i in picks[b] if i not in protected]
        if removable:
            keep.discard(removable[0])
    for i in sorted(keep):  # a last resort that the passes above make unreachable for cap >= 4
        if len(keep) <= cap:
            break
        if i not in protected:
            keep.discard(i)
    return [points[i] for i in sorted(keep)], True


def _merge(points: Sequence[EquityPointLiveOut]) -> list[EquityPointLiveOut]:
    """Sorted by time; at one instant the highest-priority source wins (snapshot > marks > now)."""
    by_ts: dict[datetime, EquityPointLiveOut] = {}
    for p in points:
        cur = by_ts.get(p.ts)
        if cur is None or _PRIORITY[p.source] > _PRIORITY[cur.source]:
            by_ts[p.ts] = p
    return [by_ts[ts] for ts in sorted(by_ts)]


def _prices(
    s: Session, run_id: int, symbol_ids: set[int], start: datetime, end: datetime
) -> dict[int, tuple[list[datetime], list[Decimal]]]:
    """Per symbol, the day's 1-minute closes by minute start in [start, end): candles over mark bars."""
    closes: dict[int, dict[datetime, Decimal]] = {sid: {} for sid in symbol_ids}
    mb, ic, ca = m.MarkBar, m.IntradayCandle, m.CandleArchive
    marks = s.execute(
        select(mb.symbol_id, mb.minute_start, mb.close).where(
            mb.run_id == run_id, mb.symbol_id.in_(symbol_ids), mb.minute_start >= start, mb.minute_start < end
        )
    ).all()
    archived = s.execute(
        select(ca.symbol_id, ca.start_ts, ca.close).where(
            ca.symbol_id.in_(symbol_ids), ca.interval == "1m", ca.start_ts >= start, ca.start_ts < end
        )
    ).all()
    stored = s.execute(
        select(ic.symbol_id, ic.ts, ic.close).where(
            ic.symbol_id.in_(symbol_ids), ic.interval == "1m", ic.ts >= start, ic.ts < end
        )
    ).all()
    for rows in (marks, archived, stored):  # later sources override: intraday candles win
        for sid, ts, close in rows:
            closes[sid][ts] = close
    out: dict[int, tuple[list[datetime], list[Decimal]]] = {}
    for sid, by_minute in closes.items():
        keys = sorted(by_minute)
        out[sid] = (keys, [by_minute[k] for k in keys])
    return out


def _minute_points(
    s: Session, run_id: int, day_start: datetime, open_at: datetime, until: datetime
) -> list[EquityPointLiveOut]:
    if until - open_at < MINUTE:
        return []
    p = m.Position
    positions = s.execute(
        select(p.symbol_id, p.qty, p.avg_price, p.opened_at, p.closed_at).where(
            p.run_id == run_id,
            p.opened_at < until,
            or_(p.closed_at.is_(None), p.closed_at > open_at),
        )
    ).all()
    if not positions:
        return []
    cl = m.CashLedger
    base = s.execute(
        select(func.coalesce(func.sum(cl.amount), 0)).where(cl.run_id == run_id, cl.ts <= open_at)
    ).scalar_one()
    moves = s.execute(
        select(cl.ts, cl.amount)
        .where(cl.run_id == run_id, cl.ts > open_at, cl.ts <= until)
        .order_by(cl.ts, cl.id)
    ).all()
    prices = _prices(s, run_id, {r.symbol_id for r in positions}, day_start, until)
    cash = Decimal(base)
    k = 0
    out: list[EquityPointLiveOut] = []
    start = open_at
    while start + MINUTE <= until:
        end = start + MINUTE
        while k < len(moves) and moves[k].ts <= end:
            cash += moves[k].amount
            k += 1
        if any(r.opened_at < end and (r.closed_at is None or r.closed_at > start) for r in positions):
            value = ZERO
            for r in positions:
                if r.opened_at <= end and (r.closed_at is None or r.closed_at > end):
                    keys, closes = prices[r.symbol_id]
                    j = bisect_right(keys, start)
                    value += r.qty * (closes[j - 1] if j else r.avg_price)
            out.append(EquityPointLiveOut(ts=end, equity=q4(cash + value), source="marks"))
        start = end
    return out


def _fills(s: Session, run_id: int, start: datetime | None, end: datetime | None) -> list[FillMarkerOut]:
    f, o, sym = m.Fill, m.Order, m.Symbol
    q = (
        select(f.id, f.ts, sym.ticker, o.side, o.purpose, f.qty, f.price, o.position_id)
        .join(o, o.id == f.order_id)
        .join(sym, sym.id == o.symbol_id)
        .where(f.run_id == run_id)
        .order_by(f.ts.desc(), f.id.desc())
        .limit(FILL_MARKERS_MAX)
    )
    if start is not None and end is not None:
        q = q.where(f.ts >= start, f.ts < end)
    rows = s.execute(q).all()
    return [
        FillMarkerOut(
            fill_id=r.id,
            ts=r.ts,
            ticker=r.ticker,
            side=r.side,
            purpose=r.purpose,
            qty=r.qty,
            price=r.price,
            position_id=r.position_id,
        )
        for r in reversed(rows)
    ]


def equity_series(
    factory: sessionmaker[Session],
    calendar: SessionCalendar,
    run_id: int,
    now: datetime,
    range_: LiveRange,
    equity_now: Decimal | None,
) -> EquitySeriesOut:
    day = session_day(calendar, now)
    es = m.EquitySnapshot
    snaps_q = select(es.ts, es.equity).where(es.run_id == run_id).order_by(es.ts)
    with factory() as s:
        starting = s.execute(
            select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == run_id)
        ).scalar_one_or_none()
        derived: list[EquityPointLiveOut] = []
        if range_ == "today":
            day_start, day_end = et_day_bounds(day)
            open_at = calendar.session_open(day)
            before_open = s.execute(
                select(es.equity).where(es.run_id == run_id, es.ts < open_at).order_by(es.ts.desc()).limit(1)
            ).scalar_one_or_none()
            start_equity = before_open if before_open is not None else starting
            snaps = s.execute(snaps_q.where(es.ts >= day_start, es.ts < day_end)).all()
            derived = _minute_points(s, run_id, day_start, open_at, min(now, calendar.session_close(day)))
            fills = _fills(s, run_id, day_start, day_end)
            with_now = et_date(now) == day
        else:
            start_equity = starting
            snaps = s.execute(snaps_q).all()
            fills = _fills(s, run_id, None, None)
            with_now = True
    points = [EquityPointLiveOut(ts=ts, equity=eq, source="snapshot") for ts, eq in snaps] + derived
    if equity_now is not None and with_now:
        points.append(EquityPointLiveOut(ts=now, equity=q4(equity_now), source="now"))
    merged, downsampled = downsample(_merge(points))
    return EquitySeriesOut(
        range=range_,
        start_equity=start_equity,
        points=merged,
        fills=fills,
        downsampled=downsampled,
    )
