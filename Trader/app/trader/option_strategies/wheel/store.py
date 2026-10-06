"""The wheel's own tables: `wheel_tickers` (the approved list and the owner's inputs), `wheel_positions` (one
row per wheel cycle: the WS §6 state machine) and `wheel_events` (the journal: every screen, daily
evaluation, action, alert, answer and lifecycle event).

Every method is its own short transaction and returns plain values, never a live ORM row. Tickers are not
tied to a run (the approved list outlives an options run); positions and events are."""

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock

CANDIDATE, APPROVED, REJECTED = "candidate", "approved", "rejected"
OPEN_STATES = ("PUT_OPEN", "SHARES_HELD", "CALL_OPEN")
EVALUATE = "evaluate"


@dataclass(frozen=True, slots=True)
class TickerRow:
    symbol_id: int
    ticker: str
    status: str  # candidate | approved | rejected
    would_own: bool | None
    ownership_reason: str
    thesis_broken: bool
    security_type_override: str | None
    acknowledged: frozenset[str]  # names of the tests whose caution the owner acknowledged
    last_verdict: str | None
    last_screen: dict[str, Any] | None
    last_screened_at: datetime | None
    origin: str  # screen | manual
    updated_at: datetime
    updated_by: str


@dataclass(frozen=True, slots=True)
class PositionRow:
    id: int
    symbol_id: int
    ticker: str
    state: str  # PUT_OPEN | SHARES_HELD | CALL_OPEN | NONE
    put_structure_id: int | None
    shares_structure_id: int | None
    call_structure_id: int | None
    contracts: int
    roll_count: int
    total_put_premium: Decimal  # per share, net of roll debits
    total_call_premium: Decimal  # per share, net of buy-backs
    dividends: Decimal
    assignment_strike: Decimal | None
    net_cost: Decimal | None
    entry: dict[str, Any]  # the WS §5.4 record
    fresh_cash_answer: bool | None
    fresh_cash_at: datetime | None
    drawdown_review_at: datetime | None
    drawdown_review_text: str | None
    fees: Decimal
    opened_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    full_cycle_result: Decimal | None


@dataclass(frozen=True, slots=True)
class EventRow:
    id: int
    position_id: int | None
    symbol_id: int
    session_date: date
    ts: datetime
    kind: str
    action: str | None
    reason: str
    data: dict[str, Any]


def jsonable(value: Any) -> Any:
    """`value` as JSON values: a Decimal, a date or a datetime becomes its text."""
    return json.loads(json.dumps(value, default=str))


def _ticker(row: m.WheelTicker) -> TickerRow:
    return TickerRow(
        symbol_id=row.symbol_id,
        ticker=row.ticker,
        status=row.status,
        would_own=row.would_own,
        ownership_reason=row.ownership_reason or "",
        thesis_broken=row.thesis_broken,
        security_type_override=row.security_type_override,
        acknowledged=frozenset(row.acknowledged_cautions or ()),
        last_verdict=row.last_verdict,
        last_screen=dict(row.last_screen) if row.last_screen else None,
        last_screened_at=row.last_screened_at,
        origin=row.origin,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _position(row: m.WheelPosition) -> PositionRow:
    return PositionRow(
        id=row.id,
        symbol_id=row.symbol_id,
        ticker=row.ticker,
        state=row.state,
        put_structure_id=row.put_structure_id,
        shares_structure_id=row.shares_structure_id,
        call_structure_id=row.call_structure_id,
        contracts=row.contracts,
        roll_count=row.roll_count,
        total_put_premium=row.total_put_premium,
        total_call_premium=row.total_call_premium,
        dividends=row.dividends,
        assignment_strike=row.assignment_strike,
        net_cost=row.net_cost,
        entry=dict(row.entry or {}),
        fresh_cash_answer=row.fresh_cash_answer,
        fresh_cash_at=row.fresh_cash_at,
        drawdown_review_at=row.drawdown_review_at,
        drawdown_review_text=row.drawdown_review_text,
        fees=row.fees,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        close_reason=row.close_reason,
        full_cycle_result=row.full_cycle_result,
    )


def _event(row: m.WheelEvent) -> EventRow:
    return EventRow(
        row.id,
        row.wheel_position_id,
        row.symbol_id,
        row.session_date,
        row.ts,
        row.kind,
        row.action,
        row.reason,
        dict(row.data or {}),
    )


class WheelStore:
    def __init__(self, factory: sessionmaker[Session], clock: Clock, run_id: int) -> None:
        self._factory = factory
        self._clock = clock
        self._run_id = run_id

    # --- tickers --------------------------------------------------------------------------------------------

    def tickers(self, status: str | None = None) -> list[TickerRow]:
        query = select(m.WheelTicker).order_by(m.WheelTicker.ticker)
        if status is not None:
            query = query.where(m.WheelTicker.status == status)
        with self._factory() as s:
            return [_ticker(row) for row in s.execute(query).scalars()]

    def ticker(self, ticker: str) -> TickerRow | None:
        with self._factory() as s:
            row = s.execute(select(m.WheelTicker).where(m.WheelTicker.ticker == ticker)).scalar_one_or_none()
            return None if row is None else _ticker(row)

    def add_ticker(self, symbol_id: int, ticker: str, *, origin: str, actor: str) -> TickerRow:
        """A new `candidate` row. A ticker already listed is returned as it is."""
        now = self._clock.now()
        with session_scope(self._factory) as s:
            row = s.get(m.WheelTicker, symbol_id)
            if row is None:
                row = m.WheelTicker(
                    symbol_id=symbol_id,
                    ticker=ticker,
                    status=CANDIDATE,
                    thesis_broken=False,
                    acknowledged_cautions=[],
                    origin=origin,
                    created_at=now,
                    updated_at=now,
                    updated_by=actor,
                )
                s.add(row)
                s.flush()
            return _ticker(row)

    def update_ticker(self, ticker: str, actor: str, **changes: Any) -> TickerRow | None:
        """Change owner inputs or the status; stamps `updated_at` and `updated_by`. `acknowledged` (a set of
        test names) is stored as the JSON list. None when the ticker is not listed."""
        if "acknowledged" in changes:
            changes["acknowledged_cautions"] = sorted(changes.pop("acknowledged"))
        with session_scope(self._factory) as s:
            row = s.execute(
                select(m.WheelTicker).where(m.WheelTicker.ticker == ticker).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return None
            for name, value in changes.items():
                setattr(row, name, value)
            row.updated_at = self._clock.now()
            row.updated_by = actor
            s.flush()
            return _ticker(row)

    def save_screen(self, ticker: str, verdict: str | None, screen: dict[str, Any] | None) -> None:
        """The latest screen of a ticker (None clears it). Not an owner change: the audit fields stay."""
        with session_scope(self._factory) as s:
            row = s.execute(
                select(m.WheelTicker).where(m.WheelTicker.ticker == ticker).with_for_update()
            ).scalar_one_or_none()
            if row is not None:
                row.last_verdict = verdict
                row.last_screen = None if screen is None else jsonable(screen)
                row.last_screened_at = None if screen is None else self._clock.now()

    # --- positions ------------------------------------------------------------------------------------------

    def _open(self) -> Any:
        return select(m.WheelPosition).where(
            m.WheelPosition.run_id == self._run_id, m.WheelPosition.closed_at.is_(None)
        )

    def open_positions(self) -> list[PositionRow]:
        with self._factory() as s:
            return [_position(r) for r in s.execute(self._open().order_by(m.WheelPosition.id)).scalars()]

    def position(self, position_id: int) -> PositionRow | None:
        with self._factory() as s:
            row = s.get(m.WheelPosition, position_id)
            return None if row is None or row.run_id != self._run_id else _position(row)

    def by_structure(self, structure_id: int) -> PositionRow | None:
        """The open wheel position one of whose current structures is `structure_id`."""
        p = m.WheelPosition
        with self._factory() as s:
            row = s.execute(
                self._open().where(
                    (p.put_structure_id == structure_id)
                    | (p.shares_structure_id == structure_id)
                    | (p.call_structure_id == structure_id)
                )
            ).scalar_one_or_none()
            return None if row is None else _position(row)

    def cycles(self, symbol_id: int) -> int:
        """How many wheel cycles on this ticker have ended, in any run (prompt keys count on it)."""
        with self._factory() as s:
            return int(
                s.execute(
                    select(func.count())
                    .select_from(m.WheelPosition)
                    .where(m.WheelPosition.symbol_id == symbol_id, m.WheelPosition.closed_at.is_not(None))
                ).scalar_one()
            )

    def open_put(
        self,
        *,
        symbol_id: int,
        ticker: str,
        structure_id: int,
        contracts: int,
        premium: Decimal,
        fees: Decimal,
        entry: dict[str, Any],
    ) -> PositionRow:
        with session_scope(self._factory) as s:
            row = m.WheelPosition(
                run_id=self._run_id,
                symbol_id=symbol_id,
                ticker=ticker,
                state="PUT_OPEN",
                put_structure_id=structure_id,
                contracts=contracts,
                roll_count=0,
                total_put_premium=premium,
                total_call_premium=Decimal(0),
                dividends=Decimal(0),
                entry=jsonable(entry),
                fees=fees,
                opened_at=self._clock.now(),
            )
            s.add(row)
            s.flush()
            return _position(row)

    def update_position(self, position_id: int, **changes: Any) -> PositionRow:
        with session_scope(self._factory) as s:
            row = s.execute(
                select(m.WheelPosition).where(m.WheelPosition.id == position_id).with_for_update()
            ).scalar_one()
            for name, value in changes.items():
                setattr(row, name, jsonable(value) if name == "entry" else value)
            s.flush()
            return _position(row)

    def close_position(
        self, position_id: int, reason: str, result: Decimal | None, **changes: Any
    ) -> PositionRow:
        """State NONE: the cycle is over, with its result."""
        return self.update_position(
            position_id,
            state="NONE",
            closed_at=self._clock.now(),
            close_reason=reason,
            full_cycle_result=result,
            **changes,
        )

    # --- events ---------------------------------------------------------------------------------------------

    def _values(
        self,
        kind: str,
        symbol_id: int,
        session_date: date,
        reason: str,
        position_id: int | None,
        action: str | None,
        data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "run_id": self._run_id,
            "wheel_position_id": position_id,
            "symbol_id": symbol_id,
            "session_date": session_date,
            "ts": self._clock.now(),
            "kind": kind,
            "action": action,
            "reason": reason,
            "data": jsonable(data or {}),
        }

    def add_event(
        self,
        kind: str,
        symbol_id: int,
        session_date: date,
        reason: str,
        *,
        position_id: int | None = None,
        action: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Write one journal row. A second `evaluate` row for the same position and session is refused by
        the table's unique key: False then, and nothing is written."""
        values = self._values(kind, symbol_id, session_date, reason, position_id, action, data)
        with session_scope(self._factory) as s:
            statement = insert(m.WheelEvent).values(**values).on_conflict_do_nothing()
            return s.execute(statement.returning(m.WheelEvent.id)).first() is not None

    def evaluated(self, position_id: int, session_date: date) -> bool:
        e = m.WheelEvent
        with self._factory() as s:
            return (
                s.execute(
                    select(e.id).where(
                        e.wheel_position_id == position_id,
                        e.session_date == session_date,
                        e.kind == EVALUATE,
                    )
                ).first()
                is not None
            )

    def last_actions(self) -> dict[int, EventRow]:
        """Per open position, its newest journal row that names an action (the panel's "next action")."""
        e = m.WheelEvent
        newest = (
            select(func.max(e.id))
            .where(e.run_id == self._run_id, e.action.is_not(None), e.wheel_position_id.is_not(None))
            .group_by(e.wheel_position_id)
        )
        with self._factory() as s:
            rows = s.execute(select(e).where(e.id.in_(newest))).scalars()
            return {row.wheel_position_id: _event(row) for row in rows if row.wheel_position_id is not None}

    def events(self, *, kind: str | None = None, position_id: int | None = None) -> list[EventRow]:
        e = m.WheelEvent
        query = select(e).where(e.run_id == self._run_id).order_by(e.id)
        if kind is not None:
            query = query.where(e.kind == kind)
        if position_id is not None:
            query = query.where(e.wheel_position_id == position_id)
        with self._factory() as s:
            return [_event(row) for row in s.execute(query).scalars()]
