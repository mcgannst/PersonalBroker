"""Open positions with marks, sparklines and bars (live dashboard plan S16, D2; DB-T4).

One row per open position of the run. The mark is the run's `quote_marks` row of the symbol (written by the
worker's mark publisher from quotes it already fetched: this module never calls Questrade, D8). The stop is
Telegram's `/positions` stop (`notify.views.open_position_rows`: the newest working stop order's price, else
the position's stop loss with `stop_working` false), and the unrealized P&L and unprotected time are
`notify.views.lines_from`'s, so the page and Telegram can't disagree.

Bars since entry are stored 1-minute candles (`intraday_candles`, then `candle_archive`) where they exist,
else the 1-minute bars the publisher built from quotes (`mark_bars`). Those are only ever turned into a
`BarOut(source="marks")` for the page, never into a `trader.market.types.Candle`, and never written anywhere.

Read-only, synchronous (the route runs it in a worker thread), and a fixed number of statements whatever the
number of positions (no per-row query, S14).
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import cache
from math import ceil
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.livedata.types import (
    EXPAND_MAX,
    MARK_STALE_SECONDS,
    NEAR_STOP_R,
    SPARK_POINTS,
    LivePositions,
    OpenValue,
)
from trader.api.routers.trading import strategy_keys
from trader.api.schemas import BarOut, FillMarkerOut, LivePositionOut, MarkState, SparkPointOut
from trader.broker.ledger import Ledger
from trader.broker.types import Fees
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import et_date
from trader.notify.views import OpenPositionRows, dec, lines_from, open_position_rows

Q4 = Decimal("0.0001")
BARS_MAX = 390  # one regular session of 1-minute bars; the latest are kept
BARS_LOOKBACK = timedelta(days=5)  # bounds the bar queries for a position carried over a long weekend
CANDLE_INTERVAL = "1m"
UNKNOWN_STRATEGY = "unknown"


def _q4(value: Decimal) -> Decimal:
    return value.quantize(Q4, ROUND_HALF_UP)


@cache
def _ledger() -> Ledger:
    """The ledger reader (`Ledger.balances` does not use the calendar; it is built once per process)."""
    return Ledger(SessionCalendar())


def mark_state(observed_at: datetime | None, now: datetime) -> MarkState:
    """`live` when observed at most `MARK_STALE_SECONDS` ago, `stale` when older, `missing` when None."""
    if observed_at is None:
        return "missing"
    return "live" if (now - observed_at).total_seconds() <= MARK_STALE_SECONDS else "stale"


def spark_from(bars: Sequence[BarOut], mark: Decimal | None, mark_at: datetime | None) -> list[SparkPointOut]:
    """The bars' closes reduced to at most `SPARK_POINTS - 1` points (every k-th, k = ceil(n / 59), counted
    back from the last bar so it is always kept), then a final point at the mark when there is one."""
    closes = [(b.start, b.close) for b in bars]
    room = SPARK_POINTS - 1
    n = len(closes)
    if n > room:
        k = ceil(n / room)
        closes = [c for i, c in enumerate(closes) if (n - 1 - i) % k == 0]
    points = [SparkPointOut(ts=ts, price=price) for ts, price in closes]
    if mark is not None and mark_at is not None:
        points.append(SparkPointOut(ts=mark_at, price=mark))
    return points


def _minute(at: datetime) -> datetime:
    return at.replace(second=0, microsecond=0)


def _expanded(expand: Collection[int], open_ids: set[int]) -> set[int]:
    """The first `EXPAND_MAX` ids asked for (in the order given), of those only the open ones."""
    first = list(dict.fromkeys(expand))[:EXPAND_MAX]
    return {i for i in first if i in open_ids}


def _entry_fees(fees: Any) -> Decimal:
    """round4 of a fill's fees total (as the ledger stores it); 0 when the stored fees are unreadable."""
    try:
        return _q4(Fees.from_json(fees).total)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return Decimal(0)


def _target(evidence: Any) -> Decimal | None:
    """A numeric `target` in the entry signal's evidence (`orb_sip` records none)."""
    if not isinstance(evidence, Mapping):
        return None
    value = evidence.get("target")
    if value is None or isinstance(value, bool):
        return None
    return dec(value)


def _bars_by_symbol(
    s: Session, run_id: int, symbol_ids: Sequence[int], since: datetime, now: datetime
) -> dict[int, dict[datetime, BarOut]]:
    """Per symbol, one bar per minute in [since, now]: a stored 1-minute candle (`intraday_candles` over
    `candle_archive`) where there is one, else the mark bar of the run. Three statements in all."""
    out: dict[int, dict[datetime, BarOut]] = {sid: {} for sid in symbol_ids}
    mb = m.MarkBar
    for sid, start, o, h, lo, c in s.execute(
        select(mb.symbol_id, mb.minute_start, mb.open, mb.high, mb.low, mb.close).where(
            mb.run_id == run_id,
            mb.symbol_id.in_(symbol_ids),
            mb.minute_start >= since,
            mb.minute_start <= now,
        )
    ).tuples():
        out[sid][start] = BarOut(start=start, open=o, high=h, low=lo, close=c, source="marks")
    ca = m.CandleArchive
    for sid, start, o, h, lo, c in s.execute(
        select(ca.symbol_id, ca.start_ts, ca.open, ca.high, ca.low, ca.close).where(
            ca.symbol_id.in_(symbol_ids),
            ca.interval == CANDLE_INTERVAL,
            ca.start_ts >= since,
            ca.start_ts <= now,
        )
    ).tuples():
        out[sid][start] = BarOut(start=start, open=o, high=h, low=lo, close=c, source="candle")
    ic = m.IntradayCandle
    for sid, start, o, h, lo, c in s.execute(
        select(ic.symbol_id, ic.ts, ic.open, ic.high, ic.low, ic.close).where(
            ic.symbol_id.in_(symbol_ids),
            ic.interval == CANDLE_INTERVAL,
            ic.ts >= since,
            ic.ts <= now,
        )
    ).tuples():
        out[sid][start] = BarOut(start=start, open=o, high=h, low=lo, close=c, source="candle")
    return out


def _fills(
    s: Session, run_id: int, positions: Sequence[m.Position], tickers: Mapping[int, str]
) -> tuple[dict[int, list[FillMarkerOut]], dict[int, Decimal]]:
    """Each position's fills (entry and exit legs, oldest first) and its entry fill's fees (round4)."""
    by_entry = {p.entry_order_id: p for p in positions}
    ids = [p.id for p in positions]
    markers: dict[int, list[FillMarkerOut]] = {pid: [] for pid in ids}
    entry_fees: dict[int, Decimal] = {}
    rows = s.execute(
        select(m.Fill, m.Order)
        .join(m.Order, m.Order.id == m.Fill.order_id)
        .where(
            m.Fill.run_id == run_id,
            or_(m.Order.position_id.in_(ids), m.Order.id.in_(list(by_entry))),
        )
        .order_by(m.Fill.ts, m.Fill.id)
    ).tuples()
    for fill, order in rows:
        entry_of = by_entry.get(order.id)
        pid = entry_of.id if entry_of is not None else order.position_id
        if pid is None or pid not in markers:
            continue
        if entry_of is not None:
            entry_fees[pid] = _entry_fees(fill.fees)
        markers[pid].append(
            FillMarkerOut(
                fill_id=fill.id,
                ts=fill.ts,
                ticker=tickers.get(pid, "?"),
                side=order.side,
                purpose=order.purpose,
                qty=fill.qty,
                price=fill.price,
                position_id=pid,
            )
        )
    return markers, entry_fees


def _targets(s: Session, entry_order_ids: Iterable[int]) -> dict[int, Decimal]:
    """Entry order id -> the numeric target of the signal behind it."""
    ids = list(entry_order_ids)
    out: dict[int, Decimal] = {}
    for order_id, evidence in s.execute(
        select(m.Order.id, m.Signal.evidence)
        .join(m.Proposal, m.Proposal.id == m.Order.proposal_id)
        .join(m.Signal, m.Signal.id == m.Proposal.signal_id)
        .where(m.Order.id.in_(ids))
    ).tuples():
        target = _target(evidence)
        if target is not None:
            out[order_id] = target
    return out


def live_positions(
    factory: sessionmaker[Session], run_id: int, now: datetime, expand: Collection[int]
) -> LivePositions:
    """The run's open positions (oldest first) with marks, R, distance to the stop, sparkline and (for the ids
    in `expand`, at most `EXPAND_MAX`) the bars since entry; the open values and the equity at marks.

    Every open position is returned: the design sizes the page for 20 (the strategies' slot limits keep it far
    below that) and the web table handles 20; more rows are still sent rather than silently dropped."""
    rows: OpenPositionRows = open_position_rows(factory, run_id)
    positions = [p for p, _ in rows.positions]
    tickers = {p.id: ticker for p, ticker in rows.positions}
    with factory() as s:
        cash = _ledger().balances(s, run_id, et_date(now)).total
        if not positions:
            return LivePositions([], [], _q4(cash), True)
        symbol_ids = rows.symbol_ids
        marks = {
            q.symbol_id: q
            for q in s.execute(
                select(m.QuoteMark).where(m.QuoteMark.run_id == run_id, m.QuoteMark.symbol_id.in_(symbol_ids))
            ).scalars()
        }
        keys = strategy_keys(s, (p.strategy_config_id for p in positions))
        targets = _targets(s, (p.entry_order_id for p in positions))
        fills, entry_fees = _fills(s, run_id, positions, tickers)
        since = max(min(_minute(p.opened_at) for p in positions), now - BARS_LOOKBACK)
        bars = _bars_by_symbol(s, run_id, symbol_ids, since, now)
    prices = {sid: q.last for sid, q in marks.items() if q.last is not None}
    lines = {line.position_id: line for line in lines_from(rows, prices, now)}
    wanted = _expanded(expand, set(tickers))

    out: list[LivePositionOut] = []
    values: list[OpenValue] = []
    marked_value = Decimal(0)
    for p in positions:
        line = lines[p.id]
        q = marks.get(p.symbol_id)
        mark = line.last
        mark_at = q.observed_at if q is not None and mark is not None else None
        entry_minute = _minute(p.opened_at)
        since_entry = [b for start, b in sorted(bars[p.symbol_id].items()) if start >= entry_minute][
            -BARS_MAX:
        ]
        planned = p.planned_risk
        unrealized = line.unrealized_pnl
        unrealized_r = _q4(unrealized / planned) if unrealized is not None and planned else None
        distance: Decimal | None = None
        if mark is not None and line.stop is not None and planned and planned > 0 and p.qty > 0:
            distance = _q4((mark - line.stop) / (planned / p.qty))
        out.append(
            LivePositionOut(
                id=p.id,
                symbol_id=p.symbol_id,
                ticker=tickers[p.id],
                strategy_key=keys.get(p.strategy_config_id, UNKNOWN_STRATEGY)
                if p.strategy_config_id is not None
                else UNKNOWN_STRATEGY,
                side="long",  # positions have qty >= 0: the engine only enters long
                qty=p.qty,
                entry=p.avg_price,
                mark=mark,
                bid=q.bid if q is not None else None,
                ask=q.ask if q is not None else None,
                mark_at=mark_at,
                mark_state=mark_state(mark_at, now),
                stop=line.stop,
                stop_working=line.stop_working,
                target=targets.get(p.entry_order_id),
                planned_risk=planned,
                unrealized=unrealized,
                unrealized_r=unrealized_r,
                distance_to_stop_r=distance,
                near_stop=distance is not None and distance <= NEAR_STOP_R,
                opened_at=p.opened_at,
                held_seconds=max(0, int((now - p.opened_at).total_seconds())),
                unprotected_seconds=line.unprotected_seconds,
                spark=spark_from(since_entry, mark, mark_at),
                bars=since_entry if p.id in wanted else None,
                fills=fills.get(p.id, []),
                link=f"/trades?position={p.id}",
            )
        )
        values.append(
            OpenValue(
                position_id=p.id,
                symbol_id=p.symbol_id,
                qty=p.qty,
                avg_price=p.avg_price,
                mark=mark,
                entry_fees=entry_fees.get(p.id, Decimal(0)),
            )
        )
        marked_value += (mark if mark is not None else p.avg_price) * p.qty
    return LivePositions(
        positions=out,
        open_values=values,
        equity_at_marks=_q4(cash + marked_value),
        all_marked=all(v.mark is not None for v in values),
    )
