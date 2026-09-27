"""Simulated broker (SPEC §7, BR-20, BR-21, BR-23): orders, fills, positions, trades and cash.

Each fill is one transaction: fill row, order status, ledger rows, position and trade. Working orders are
locked FOR UPDATE SKIP LOCKED while quotes are applied, so two callers never fill one order twice.
An entry met at or after the no-entry cutoff (close - no_entry_before_close_minutes) is cancelled, never
filled (BR-42). The broker depends on the FillModel protocol only; P5-T2 adds on_candles() for replay,
running the same per-order loop with a CandleFillModel.

Hardening (P2-B1 fix round):
- Each order in a quote batch runs in its own savepoint: an exception rolls back that order only, logs an
  error event (order id, exception type) and the batch carries on.
- Nothing fills outside regular hours [session_open, session_close); such an order stays working.
- Lock order is always POSITION rows (by id), then ORDER rows, then the cash advisory lock (submit, cancel,
  on_quotes, end_of_session), so two callers can't deadlock. The proposal service locks its PROPOSAL rows
  first, then positions by id (expire_due locks all of a sweep's positions up front).
- A sell is re-checked against its locked position (open, same qty) and cancelled if it no longer fits.
- An entry whose cost (price x qty + fees) exceeds buying power is cancelled at fill time.
- account_state reads cash and positions in one REPEATABLE READ transaction.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote
from trader.broker.base import BrokerRejected
from trader.broker.ledger import Ledger
from trader.broker.types import (
    Q4,
    ZERO,
    AccountState,
    Fees,
    FillDecision,
    FillEvent,
    FillModel,
    NoFill,
    OrderSpec,
    OrderView,
    PositionView,
)
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.settings_store import RuntimeSettings

UNUSABLE_QUOTE = frozenset({"stale_quote", "halted", "delayed_quote", "no_ask", "no_bid", "crossed_quote"})
STALE_ALERT_AFTER = timedelta(seconds=60)
ENTRY_CUTOFF = "entry cutoff"
POSITION_GONE = "position no longer open"
NO_BUYING_POWER = "insufficient buying power"
SOURCE = "broker"
# Serialises the entry fills of one run, so two quote batches can't both spend the same buying power.
_CASH_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


def position_view(p: m.Position) -> PositionView:
    return PositionView(
        id=p.id,
        symbol_id=p.symbol_id,
        strategy_config_id=p.strategy_config_id,
        qty=p.qty,
        avg_price=p.avg_price,
        stop_loss=p.stop_loss,
        opened_at=p.opened_at,
        session_date=p.session_date,
        stop_order_id=p.stop_order_id,
        unprotected_since=p.unprotected_since,
        unprotected_seconds=p.unprotected_seconds,
    )


def order_view(o: m.Order) -> OrderView:
    return OrderView(
        id=o.id,
        symbol_id=o.symbol_id,
        side=o.side,  # type: ignore[arg-type]
        order_type=o.order_type,  # type: ignore[arg-type]
        purpose=o.purpose,  # type: ignore[arg-type]
        qty=o.qty,
        stop=o.stop_price,
        limit=o.limit_price,
        status=o.status,
        position_id=o.position_id,
        strategy_config_id=o.strategy_config_id,
        proposal_id=o.proposal_id,
        submitted_at=o.submitted_at,
    )


def _spec(o: m.Order) -> OrderSpec:
    return OrderSpec(
        symbol_id=o.symbol_id,
        side=o.side,  # type: ignore[arg-type]
        order_type=o.order_type,  # type: ignore[arg-type]
        qty=o.qty,
        stop=o.stop_price,
        limit=o.limit_price,
        tif=o.tif,  # type: ignore[arg-type]
        purpose=o.purpose,  # type: ignore[arg-type]
        position_id=o.position_id,
        proposal_id=o.proposal_id,
        strategy_config_id=o.strategy_config_id,
        stop_loss=o.stop_loss,
        reason=o.reason,
    )


def _end_unprotected(pos: m.Position, now: datetime) -> None:
    if pos.unprotected_since is not None:
        pos.unprotected_seconds += int((now - pos.unprotected_since).total_seconds())
        pos.unprotected_since = None


class _Batch:
    """One on_quotes batch: its time, the positions it locked, and settings read at most once."""

    def __init__(self, broker: "SimBroker", now: datetime) -> None:
        self.now = now
        self.today = et_date(now)
        self.in_hours = broker.in_regular_hours(now)
        self.positions: dict[int, m.Position] = {}
        self._broker = broker
        self._cutoff: datetime | None = None
        self._cutoff_read = False
        self._settings: RuntimeSettings | None = None

    def settings(self) -> RuntimeSettings:
        if self._settings is None:
            self._settings = self._broker.read_settings()
        return self._settings

    def cutoff(self) -> datetime | None:
        if not self._cutoff_read:  # read once per batch, and only when an entry is working
            self._cutoff, self._cutoff_read = self._broker.entry_cutoff(self.today), True
        return self._cutoff


class SimBroker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        ledger: Ledger,
        fill_model: FillModel,
        run_id: int,
        currency: str = "USD",
        *,
        calendar: SessionCalendar | None = None,
        settings: Callable[[], RuntimeSettings] | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._ledger = ledger
        self._fill_model = fill_model
        self.run_id = run_id
        self._currency = currency
        self._cal = calendar if calendar is not None else SessionCalendar()
        self._settings: Callable[[], RuntimeSettings] = settings if settings is not None else RuntimeSettings

    def entry_cutoff(self, day: date) -> datetime | None:
        """The moment entries stop filling on `day`: session close - no_entry_before_close_minutes (BR-42)."""
        if not self._cal.is_session(day):
            return None
        minutes = self._settings().no_entry_before_close_minutes
        return self._cal.session_close(day) - timedelta(minutes=minutes)

    @contextmanager
    def _session(self, session: Session | None) -> Iterator[Session]:
        if session is not None:
            yield session
        else:
            with session_scope(self._factory) as s:
                yield s

    def _log(self, s: Session, level: str, message: str, data: dict[str, Any]) -> None:
        log_event(s, self._clock, level, SOURCE, message, data, run_id=self.run_id)

    # --- orders -------------------------------------------------------------------------------------
    def submit(self, spec: OrderSpec, session: Session | None = None) -> int:
        with self._session(session) as s:
            return self._submit(s, spec, self._clock.now())

    def _submit(self, s: Session, spec: OrderSpec, now: datetime) -> int:
        pos: m.Position | None = None
        if spec.position_id is not None:
            pos = s.get(m.Position, spec.position_id, with_for_update=True, populate_existing=True)
            if pos is None or pos.run_id != self.run_id or pos.closed_at is not None:
                raise BrokerRejected(f"position {spec.position_id} is not open")
            if spec.symbol_id != pos.symbol_id:
                raise BrokerRejected(
                    f"order symbol {spec.symbol_id} doesn't match position symbol {pos.symbol_id}"
                )
            if spec.qty > pos.qty:
                raise BrokerRejected(f"a sell of {spec.qty} exceeds the position of {pos.qty} (long only)")
            if spec.qty < pos.qty:
                raise BrokerRejected(f"partial exits are not supported: sell all {pos.qty} shares")
        order = m.Order(
            run_id=self.run_id,
            proposal_id=spec.proposal_id,
            position_id=spec.position_id,
            strategy_config_id=spec.strategy_config_id or (pos.strategy_config_id if pos else None),
            symbol_id=spec.symbol_id,
            side=spec.side,
            order_type=spec.order_type,
            purpose=spec.purpose,
            qty=spec.qty,
            stop_price=spec.stop,
            limit_price=spec.limit,
            stop_loss=spec.stop_loss,
            tif=spec.tif,
            status="working",
            reason=spec.reason,
            session_date=et_date(now),
            submitted_at=now,
            stale_alerted=False,
        )
        s.add(order)
        s.flush()
        if spec.purpose == "stop" and pos is not None:
            if pos.stop_order_id is not None:
                old = s.get(m.Order, pos.stop_order_id, with_for_update=True)
                if old is not None and old.status == "working":
                    self._close_order(old, now, "replaced by a new stop")
            pos.stop_order_id = order.id
            _end_unprotected(pos, now)
        self._log(s, "info", f"order {order.id} working", {"order_id": order.id, **spec.to_json()})
        return order.id

    def cancel(self, order_id: int, reason: str, session: Session | None = None) -> bool:
        now = self._clock.now()
        with self._session(session) as s:
            head = s.execute(
                select(m.Order.run_id, m.Order.status, m.Order.position_id).where(m.Order.id == order_id)
            ).one_or_none()
            if head is None or head.run_id != self.run_id or head.status != "working":
                return False
            pos: m.Position | None = None
            if head.position_id is not None:  # lock order: the position row first, then the order row
                pos = s.get(m.Position, head.position_id, with_for_update=True, populate_existing=True)
            order = s.get(m.Order, order_id, with_for_update=True, populate_existing=True)
            if order is None or order.status != "working":
                return False
            self._close_order(order, now, reason)
            if order.purpose == "stop" and order.position_id is not None:
                if pos is not None and pos.closed_at is None and pos.stop_order_id == order.id:
                    pos.stop_order_id = None
                    pos.unprotected_since = now
            self._log(s, "info", f"order {order_id} cancelled", {"order_id": order_id, "reason": reason})
            return True

    @staticmethod
    def _close_order(order: m.Order, now: datetime, reason: str) -> None:
        order.status, order.closed_at, order.cancel_reason = "cancelled", now, reason

    def end_of_session(self, session_date: date) -> list[int]:
        now = self._clock.now()
        with session_scope(self._factory) as s:
            working = (
                m.Order.run_id == self.run_id,
                m.Order.status == "working",
                m.Order.session_date <= session_date,
            )
            self._lock_positions(s, select(m.Order.position_id).where(*working))
            orders = (
                s.execute(select(m.Order).where(*working).order_by(m.Order.id).with_for_update())
                .scalars()
                .all()
            )
            for order in orders:
                self._close_order(order, now, "end_of_session")
                if order.purpose == "stop" and order.position_id is not None:
                    pos = s.get(m.Position, order.position_id)
                    if pos is not None and pos.closed_at is None and pos.stop_order_id == order.id:
                        pos.stop_order_id = None
                        pos.unprotected_since = now
            ids = [o.id for o in orders]
            if ids:
                self._log(s, "info", f"end of session: cancelled {len(ids)} orders", {"order_ids": ids})
            return ids

    # --- quotes and fills -------------------------------------------------------------------------------
    def on_quotes(self, quotes: Sequence[QtQuote], now: datetime) -> list[FillEvent]:
        by_symbol = {q.symbol_id: q for q in quotes}
        if not by_symbol:
            return []
        events: list[FillEvent] = []
        batch = _Batch(self, now)
        with session_scope(self._factory) as s:
            working = (
                m.Order.run_id == self.run_id,
                m.Order.status == "working",
                m.Order.symbol_id.in_(list(by_symbol)),
            )
            # Lock order: every position a working sell here closes, then the orders themselves.
            batch.positions = self._lock_positions(s, select(m.Order.position_id).where(*working))
            orders = (
                s.execute(
                    select(m.Order).where(*working).order_by(m.Order.id).with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for order in orders:
                order_id = order.id
                try:
                    with s.begin_nested():  # one savepoint per order: a failure costs this order only
                        event = self._apply(s, order, by_symbol[order.symbol_id], batch)
                except Exception as exc:
                    self._log(
                        s,
                        "error",
                        f"order {order_id}: fill failed ({type(exc).__name__}), rolled back and skipped",
                        {"order_id": order_id, "error_type": type(exc).__name__, "error": str(exc)[:500]},
                    )
                    continue
                if event is not None:
                    events.append(event)
        return events

    def _lock_positions(self, s: Session, position_ids: Any) -> dict[int, m.Position]:
        """Lock (FOR UPDATE, by id) the positions named by `position_ids` (a select of ids)."""
        ids = sorted({pid for pid in s.execute(position_ids).scalars() if pid is not None})
        if not ids:
            return {}
        rows = s.execute(
            select(m.Position)
            .where(m.Position.id.in_(ids))
            .order_by(m.Position.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalars()
        return {p.id: p for p in rows}

    def in_regular_hours(self, now: datetime) -> bool:
        """True in [session_open, session_close) of a trading session (early closes included)."""
        day = et_date(now)
        return self._cal.is_session(day) and self._cal.session_open(day) <= now < self._cal.session_close(day)

    def read_settings(self) -> RuntimeSettings:
        return self._settings()

    def _apply(self, s: Session, order: m.Order, quote: QtQuote, batch: "_Batch") -> FillEvent | None:
        """Apply one quote to one locked working order. Runs inside the order's savepoint."""
        now = batch.now
        if order.status != "working":  # cancelled earlier in this batch (position closed)
            return None
        if order.purpose == "entry":
            cutoff = batch.cutoff()
            if cutoff is None or now >= cutoff or order.session_date != batch.today:
                self._refuse_late_entry(s, order, now, cutoff)
                return None
        if not batch.in_hours:  # regular hours only: the order stays working until the open
            return None
        pos: m.Position | None = None
        if order.side == "sell":
            if order.position_id is not None and order.position_id not in batch.positions:
                if s.get(m.Position, order.position_id) is not None:
                    return None  # submitted after this batch locked its positions: the next batch
            pos = batch.positions.get(order.position_id) if order.position_id is not None else None
            if pos is None or pos.run_id != self.run_id or pos.closed_at is not None or pos.qty != order.qty:
                self._refuse_orphan_sell(s, order, now, pos)
                return None
        outcome = self._fill_model.assess(_spec(order), quote, now)
        if isinstance(outcome, NoFill):
            self._no_fill(s, order, outcome, now)
            return None
        if order.side == "buy" and not self._affordable(s, order, outcome, batch):
            return None
        return self._fill(s, order, outcome, now, pos)

    def _refuse_orphan_sell(self, s: Session, order: m.Order, now: datetime, pos: m.Position | None) -> None:
        self._close_order(order, now, POSITION_GONE)
        self._log(
            s,
            "warning",
            f"order {order.id}: {POSITION_GONE}, cancelled instead of filled",
            {
                "order_id": order.id,
                "position_id": order.position_id,
                "order_qty": order.qty,
                "position_qty": pos.qty if pos is not None else None,
                "position_closed": pos is None or pos.closed_at is not None,
            },
        )

    def _affordable(self, s: Session, order: m.Order, d: FillDecision, batch: "_Batch") -> bool:
        """Buying-power backstop: an entry costing more than buying power is cancelled, never filled."""
        s.execute(_CASH_LOCK, {"key": f"trader.sim_broker.cash:{self.run_id}"})
        cost = (d.price * d.qty).quantize(Q4, ROUND_HALF_UP) + d.fees.total
        power = self._ledger.balances(s, self.run_id, batch.today).buying_power(
            batch.settings().cash_account_mode
        )
        if cost <= power:
            return True
        self._close_order(order, batch.now, NO_BUYING_POWER)
        self._log(
            s,
            "warning",
            f"order {order.id}: {NO_BUYING_POWER} ({cost} > {power}), cancelled instead of filled",
            {
                "order_id": order.id,
                "reason": NO_BUYING_POWER,
                "cost": str(cost),
                "buying_power": str(power),
            },
        )
        return False

    def _refuse_late_entry(self, s: Session, order: m.Order, now: datetime, cutoff: datetime | None) -> None:
        self._close_order(order, now, ENTRY_CUTOFF)
        self._log(
            s,
            "warning",
            f"order {order.id}: entry cutoff reached, cancelled instead of filled (no overnight hold)",
            {
                "order_id": order.id,
                "reason": ENTRY_CUTOFF,
                "cutoff": cutoff.isoformat() if cutoff else None,
                "order_session": order.session_date.isoformat(),
            },
        )

    def _no_fill(self, s: Session, order: m.Order, outcome: NoFill, now: datetime) -> None:
        if outcome.reason not in UNUSABLE_QUOTE:  # a usable quote: the next outage alerts afresh
            order.stale_since, order.stale_alerted = None, False
            return
        data = {"order_id": order.id, "reason": outcome.reason, "detail": outcome.detail}
        if order.stale_since is None:
            order.stale_since = now
            self._log(s, "warning", f"order {order.id}: unusable quote ({outcome.reason}), not filling", data)
        elif not order.stale_alerted and now - order.stale_since >= STALE_ALERT_AFTER:
            order.stale_alerted = True
            self._log(s, "error", f"order {order.id}: unusable quote for over 60 s ({outcome.reason})", data)

    def _fill(
        self, s: Session, order: m.Order, d: FillDecision, now: datetime, closing: m.Position | None
    ) -> FillEvent:
        fill = m.Fill(
            run_id=self.run_id,
            order_id=order.id,
            ts=now,
            qty=d.qty,
            price=d.price,
            fees=d.fees.to_json(),
            quote_snapshot=d.quote_snapshot,
            slippage=d.slippage,
        )
        s.add(fill)
        s.flush()
        order.status, order.closed_at, order.stale_since = "filled", now, None
        trade_date = et_date(now)
        value = (d.price * d.qty).quantize(Q4, ROUND_HALF_UP)
        ref = f"fill:{fill.id}"
        trade: m.Trade | None = None
        if order.side == "buy":
            self._ledger.record(
                s,
                run_id=self.run_id,
                ts=now,
                trade_date=trade_date,
                amount=-value,
                kind="buy",
                ref=ref,
                currency=self._currency,
            )
            planned = (d.price - order.stop_loss) * d.qty if order.stop_loss is not None else None
            pos = m.Position(
                run_id=self.run_id,
                symbol_id=order.symbol_id,
                strategy_config_id=order.strategy_config_id,
                qty=d.qty,
                avg_price=d.price,
                stop_loss=order.stop_loss,
                planned_risk=planned.quantize(Q4, ROUND_HALF_UP)
                if planned is not None and planned > 0
                else None,
                session_date=trade_date,
                opened_at=now,
                entry_order_id=order.id,
                stop_order_id=None,
                unprotected_since=now,
                unprotected_seconds=0,
            )
            s.add(pos)
            s.flush()
            order.position_id = pos.id
        else:
            self._ledger.record(
                s,
                run_id=self.run_id,
                ts=now,
                trade_date=trade_date,
                amount=value,
                kind="sell",
                ref=ref,
                currency=self._currency,
            )
            if closing is None:  # _apply checked it; the position row is locked by this batch
                raise BrokerRejected(f"order {order.id} sells position {order.position_id}, which isn't open")
            pos = closing
            trade = self._close_position(s, pos, order, d, now, trade_date)
        if d.fees.total > 0:
            self._ledger.record(
                s,
                run_id=self.run_id,
                ts=now,
                trade_date=trade_date,
                amount=-d.fees.total,
                kind="fee",
                ref=f"{ref}:fees",
                currency=self._currency,
            )
        self._log(
            s,
            "info",
            f"order {order.id} filled: {order.side} {d.qty} @ {d.price}",
            {"order_id": order.id, "fill_id": fill.id, "price": str(d.price), "trigger": d.trigger},
        )
        return FillEvent(
            fill_id=fill.id,
            order_id=order.id,
            run_id=self.run_id,
            symbol_id=order.symbol_id,
            side=order.side,  # type: ignore[arg-type]
            purpose=order.purpose,  # type: ignore[arg-type]
            qty=d.qty,
            price=d.price,
            ts=now,
            position_id=pos.id,
            strategy_config_id=pos.strategy_config_id,
            stop_loss=pos.stop_loss,
            proposal_id=order.proposal_id,
            trade_id=trade.id if trade else None,
            pnl=trade.pnl if trade else None,
        )

    def _close_position(
        self, s: Session, pos: m.Position, order: m.Order, d: FillDecision, now: datetime, trade_date: date
    ) -> m.Trade:
        entry_fill = s.execute(select(m.Fill).where(m.Fill.order_id == pos.entry_order_id)).scalar_one()
        fees_total = Fees.from_json(entry_fill.fees).total + d.fees.total
        pnl = ((d.price - pos.avg_price) * pos.qty - fees_total).quantize(Q4, ROUND_HALF_UP)
        pnl_r = (pnl / pos.planned_risk).quantize(Q4, ROUND_HALF_UP) if pos.planned_risk else None
        _end_unprotected(pos, now)
        pos.closed_at = now
        trade = m.Trade(
            run_id=self.run_id,
            position_id=pos.id,
            symbol_id=pos.symbol_id,
            session_date=trade_date,
            entry_price=pos.avg_price,
            exit_price=d.price,
            qty=pos.qty,
            pnl=pnl,
            pnl_r=pnl_r,
            planned_risk=pos.planned_risk,
            exit_reason=order.reason or order.purpose,
            slippage_total=((entry_fill.slippage + d.slippage) * pos.qty).quantize(Q4, ROUND_HALF_UP),
            fees_total=fees_total,
            opened_at=pos.opened_at,
            closed_at=now,
        )
        s.add(trade)
        s.flush()
        others = s.execute(
            select(m.Order)
            .where(m.Order.position_id == pos.id, m.Order.status == "working", m.Order.id != order.id)
            .with_for_update()
        ).scalars()
        for other in others:
            self._close_order(other, now, "position closed")
        return trade

    # --- read side ----------------------------------------------------------------------------------------
    def open_positions(self) -> list[PositionView]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Position)
                .where(m.Position.run_id == self.run_id, m.Position.closed_at.is_(None))
                .order_by(m.Position.id)
            ).scalars()
            return [position_view(p) for p in rows]

    def working_orders(self) -> list[OrderView]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Order)
                .where(m.Order.run_id == self.run_id, m.Order.status == "working")
                .order_by(m.Order.id)
            ).scalars()
            return [order_view(o) for o in rows]

    def working_symbol_ids(self) -> list[int]:
        return sorted({o.symbol_id for o in self.working_orders()})

    def account_state(
        self, today: date, marks: Mapping[int, Decimal], cash_account_mode: bool
    ) -> AccountState:
        with self._factory() as s:  # one snapshot: a fill can't land between the cash and position reads
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            bal = self._ledger.balances(s, self.run_id, today)
            positions = [
                position_view(p)
                for p in s.execute(
                    select(m.Position)
                    .where(m.Position.run_id == self.run_id, m.Position.closed_at.is_(None))
                    .order_by(m.Position.id)
                ).scalars()
            ]
        value = ZERO
        at_cost: list[dict[str, int]] = []
        for p in positions:
            mark = marks.get(p.symbol_id)
            if mark is None or mark <= 0:
                mark = p.avg_price
                at_cost.append({"position_id": p.id, "symbol_id": p.symbol_id})
            value += mark * p.qty
        value = value.quantize(Q4, ROUND_HALF_UP)
        if at_cost:
            with session_scope(self._factory) as s:
                self._log(
                    s,
                    "warning",
                    f"no mark for {len(at_cost)} open position(s): valued at cost",
                    {"positions": at_cost},
                )
        total = bal.total.quantize(Q4, ROUND_HALF_UP)
        settled = bal.settled.quantize(Q4, ROUND_HALF_UP)
        return AccountState(
            total_cash=total,
            settled_cash=settled,
            buying_power=settled if cash_account_mode else total,
            positions_value=value,
            equity=total + value,
        )

    def snapshot_equity(self, now: datetime, account: AccountState) -> None:
        with session_scope(self._factory) as s:
            prev = s.execute(
                select(func.max(m.EquitySnapshot.peak_equity)).where(m.EquitySnapshot.run_id == self.run_id)
            ).scalar_one()
            peak = max(account.equity, prev) if prev is not None else account.equity
            dd = ((peak - account.equity) / peak).quantize(Q4, ROUND_HALF_UP) if peak > 0 else ZERO
            stmt = pg_insert(m.EquitySnapshot).values(
                run_id=self.run_id,
                ts=now,
                equity=account.equity,
                cash=account.total_cash,
                settled_cash=account.settled_cash,
                peak_equity=peak,
                drawdown_pct=dd,
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[m.EquitySnapshot.run_id, m.EquitySnapshot.ts],
                    set_={
                        k: stmt.excluded[k]
                        for k in ("equity", "cash", "settled_cash", "peak_equity", "drawdown_pct")
                    },
                )
            )
