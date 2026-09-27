"""Simulated broker (SPEC §7, BR-20, BR-21, BR-23): orders, fills, positions, trades and cash.

Each fill is one transaction: fill row, order status, ledger rows, position and trade. Working orders are
locked FOR UPDATE SKIP LOCKED while quotes are applied, so two callers never fill one order twice.
An entry met at or after the no-entry cutoff (close - no_entry_before_close_minutes) is cancelled, never
filled (BR-42). The broker depends on the FillModel protocol only; P5-T2 adds on_candles() for replay,
running the same per-order loop with a CandleFillModel.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
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

UNUSABLE_QUOTE = frozenset({"stale_quote", "halted", "delayed_quote", "no_ask", "no_bid"})
STALE_ALERT_AFTER = timedelta(seconds=60)
ENTRY_CUTOFF = "entry cutoff"
SOURCE = "broker"


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
            order = s.get(m.Order, order_id, with_for_update=True, populate_existing=True)
            if order is None or order.run_id != self.run_id or order.status != "working":
                return False
            self._close_order(order, now, reason)
            if order.purpose == "stop" and order.position_id is not None:
                pos = s.get(m.Position, order.position_id, with_for_update=True)
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
            orders = (
                s.execute(
                    select(m.Order)
                    .where(
                        m.Order.run_id == self.run_id,
                        m.Order.status == "working",
                        m.Order.session_date <= session_date,
                    )
                    .order_by(m.Order.id)
                    .with_for_update()
                )
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
        with session_scope(self._factory) as s:
            orders = (
                s.execute(
                    select(m.Order)
                    .where(
                        m.Order.run_id == self.run_id,
                        m.Order.status == "working",
                        m.Order.symbol_id.in_(list(by_symbol)),
                    )
                    .order_by(m.Order.id)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            today = et_date(now)
            cutoff: datetime | None = None
            cutoff_read = False
            for order in orders:
                if order.status != "working":  # cancelled earlier in this batch (position closed)
                    continue
                if order.purpose == "entry":
                    if not cutoff_read:  # read the settings once per batch, and only when an entry is working
                        cutoff, cutoff_read = self.entry_cutoff(today), True
                    if cutoff is None or now >= cutoff or order.session_date != today:
                        self._refuse_late_entry(s, order, now, cutoff)
                        continue
                outcome = self._fill_model.assess(_spec(order), by_symbol[order.symbol_id], now)
                if isinstance(outcome, NoFill):
                    self._no_fill(s, order, outcome, now)
                    continue
                events.append(self._fill(s, order, outcome, now))
        return events

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
        if outcome.reason not in UNUSABLE_QUOTE:
            order.stale_since = None
            return
        data = {"order_id": order.id, "reason": outcome.reason, "detail": outcome.detail}
        if order.stale_since is None:
            order.stale_since = now
            self._log(s, "warning", f"order {order.id}: unusable quote ({outcome.reason}), not filling", data)
        elif not order.stale_alerted and now - order.stale_since >= STALE_ALERT_AFTER:
            order.stale_alerted = True
            self._log(s, "error", f"order {order.id}: unusable quote for over 60 s ({outcome.reason})", data)

    def _fill(self, s: Session, order: m.Order, d: FillDecision, now: datetime) -> FillEvent:
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
            found = s.get(m.Position, order.position_id, with_for_update=True) if order.position_id else None
            if found is None:
                raise BrokerRejected(
                    f"order {order.id} sells position {order.position_id}, which doesn't exist"
                )
            pos = found
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
        with self._factory() as s:
            bal = self._ledger.balances(s, self.run_id, today)
        value = sum(
            ((marks.get(p.symbol_id) or p.avg_price) * p.qty for p in self.open_positions()), ZERO
        ).quantize(Q4, ROUND_HALF_UP)
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
