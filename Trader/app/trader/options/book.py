"""`DbBook`: the options run's structures, positions and cash in the database (OPTSIM task plan T5), the
`OptionBook` protocol over ONE session and so one transaction. The caller holds the book lock
(`trader.options.account.lock_book`) and commits; the book only flushes.

Cash is the cash ledger's TOTAL for the run (premium is usable at once: risk R6), written through
`trader.broker.ledger.Ledger.record` with kinds `buy`, `sell` and `fee`. Money is kept to 4 decimals (half
up), as the columns are. `tests/options/contract_book.py` is the behaviour this book shares with `FakeBook`.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trader.broker.ledger import Ledger
from trader.db import models as m
from trader.market.clock import Clock, et_date
from trader.options.account import OPTIONS_CURRENCY
from trader.options.protocols import CashKind, UnknownContract, UnknownUnderlying
from trader.options.types import (
    ZERO,
    CloseReason,
    Instrument,
    LifecycleKind,
    OptionContract,
    OptPositionView,
    StructureKind,
    StructureView,
)

Q4 = Decimal("0.0001")


def q4(value: Decimal) -> Decimal:
    return value.quantize(Q4, ROUND_HALF_UP)


def contract_view(row: m.OptionContract) -> OptionContract:
    """A contract-master row as the value type."""
    return OptionContract(
        id=row.id,
        underlying=row.underlying,
        underlying_symbol_id=row.underlying_symbol_id,
        qt_symbol_id=row.qt_symbol_id,
        root=row.root,
        expiry=row.expiry,
        strike=row.strike,
        right=cast(Any, row.right),
        multiplier=row.multiplier,
        is_monthly=row.is_monthly,
        adjusted=row.adjusted,
    )


def load_contracts(session: Session, contract_ids: Sequence[int]) -> dict[int, OptionContract]:
    """The known contracts among `contract_ids`, by id (an unknown id is simply absent)."""
    ids = sorted(set(contract_ids))
    if not ids:
        return {}
    rows = session.execute(select(m.OptionContract).where(m.OptionContract.id.in_(ids))).scalars()
    return {row.id: contract_view(row) for row in rows}


def symbol_id_of(session: Session, ticker: str) -> int:
    """The `symbols.id` of an underlying (the row Questrade knows first when a ticker is listed twice).
    Raises `UnknownUnderlying`."""
    found = session.execute(
        select(m.Symbol.id)
        .where(m.Symbol.ticker == ticker)
        .order_by(m.Symbol.questrade_id.is_(None), m.Symbol.id)
        .limit(1)
    ).scalar_one_or_none()
    if found is None:
        raise UnknownUnderlying(ticker)
    return found


class DbBook:
    """The database `OptionBook` of one options run. Beyond the protocol: `set_entry` (a roll gives the
    structure a new entry and take-profit) and `position_row`."""

    def __init__(self, session: Session, run_id: int, ledger: Ledger, clock: Clock) -> None:
        self._s = session
        self._run_id = run_id
        self._ledger = ledger
        self._clock = clock

    # --- reads ----------------------------------------------------------------------------------------------
    def cash(self) -> Decimal:
        return self._ledger.balances(self._s, self._run_id, et_date(self._clock.now())).total

    def reserved(self) -> Decimal:
        structures = self._s.execute(
            select(func.coalesce(func.sum(m.OptStructure.reserved_cash), 0)).where(
                m.OptStructure.run_id == self._run_id, m.OptStructure.state == "open"
            )
        ).scalar_one()
        orders = self._s.execute(
            select(func.coalesce(func.sum(m.OptOrder.reserved_cash), 0)).where(
                m.OptOrder.run_id == self._run_id, m.OptOrder.status == "working"
            )
        ).scalar_one()
        return Decimal(structures) + Decimal(orders)

    def _get(self, structure_id: int) -> m.OptStructure:
        row = self._s.execute(
            select(m.OptStructure).where(
                m.OptStructure.id == structure_id, m.OptStructure.run_id == self._run_id
            )
        ).scalar_one_or_none()
        if row is None:
            raise KeyError(f"unknown structure {structure_id}")
        return row

    def _contract(self, contract_id: int) -> OptionContract:
        row = self._s.get(m.OptionContract, contract_id)
        if row is None:
            raise UnknownContract(contract_id)
        return contract_view(row)

    def _position_view(
        self, p: m.OptPosition, underlying: str, contracts: Mapping[int, OptionContract] | None = None
    ) -> OptPositionView:
        if p.contract_id is None:
            contract = None
        elif contracts is not None and p.contract_id in contracts:
            contract = contracts[p.contract_id]
        else:
            contract = self._contract(p.contract_id)
        return OptPositionView(
            id=p.id,
            structure_id=p.structure_id,
            instrument=cast(Instrument, p.instrument),
            contract=contract,
            underlying=underlying,
            qty=p.qty,
            avg_price=p.avg_price,
            realized_pnl=p.realized_pnl,
        )

    def _views(self, rows: Sequence[m.OptStructure]) -> list[StructureView]:
        if not rows:
            return []
        positions = list(
            self._s.execute(
                select(m.OptPosition)
                .where(m.OptPosition.structure_id.in_([s.id for s in rows]))
                .order_by(m.OptPosition.id)
            ).scalars()
        )
        contracts = load_contracts(self._s, [p.contract_id for p in positions if p.contract_id is not None])
        by_structure: dict[int, list[m.OptPosition]] = {}
        for p in positions:
            by_structure.setdefault(p.structure_id, []).append(p)
        return [
            StructureView(
                id=s.id,
                source=s.source,
                strategy_config_id=s.strategy_config_id,
                kind=cast(StructureKind, s.kind),
                underlying=s.underlying,
                state="open" if s.state == "open" else "closed",
                close_reason=cast(Any, s.close_reason),
                frozen=s.frozen,
                qty=s.qty,
                entry_net=s.entry_net,
                reserved_cash=s.reserved_cash,
                take_profit_net=s.take_profit_net,
                cover_structure_id=s.cover_structure_id,
                parent_structure_id=s.parent_structure_id,
                realized_pnl=s.realized_pnl,
                fees_total=s.fees_total,
                opened_at=s.opened_at,
                closed_at=s.closed_at,
                positions=tuple(
                    self._position_view(p, s.underlying, contracts) for p in by_structure.get(s.id, [])
                ),
                meta=dict(s.meta or {}),
            )
            for s in rows
        ]

    def structure(self, structure_id: int) -> StructureView:
        return self._views([self._get(structure_id)])[0]

    def structures(
        self, *, open_only: bool = True, source: str | None = None, underlying: str | None = None
    ) -> list[StructureView]:
        stmt = select(m.OptStructure).where(m.OptStructure.run_id == self._run_id)
        if open_only:
            stmt = stmt.where(m.OptStructure.state == "open")
        if source is not None:
            stmt = stmt.where(m.OptStructure.source == source)
        if underlying is not None:
            stmt = stmt.where(m.OptStructure.underlying == underlying)
        return self._views(list(self._s.execute(stmt.order_by(m.OptStructure.id)).scalars()))

    def position_row(self, structure_id: int, contract_id: int | None) -> m.OptPosition | None:
        """The structure's position in that contract (None: its shares), if it ever had one."""
        stmt = select(m.OptPosition).where(m.OptPosition.structure_id == structure_id)
        stmt = stmt.where(
            m.OptPosition.contract_id.is_(None)
            if contract_id is None
            else m.OptPosition.contract_id == contract_id
        )
        return self._s.execute(stmt).scalar_one_or_none()

    # --- writes ---------------------------------------------------------------------------------------------
    def add_structure(
        self,
        *,
        kind: StructureKind,
        source: str,
        strategy_config_id: int | None,
        underlying: str,
        qty: int,
        entry_net: Decimal,
        reserved_cash: Decimal,
        cover_structure_id: int | None,
        parent_structure_id: int | None,
        take_profit_net: Decimal | None,
        meta: Mapping[str, Any],
        ts: datetime,
    ) -> int:
        if qty <= 0:
            raise ValueError("a structure's qty must be positive")
        if reserved_cash < 0:
            raise ValueError("reserved cash can't be negative")
        row = m.OptStructure(
            run_id=self._run_id,
            source=source,
            strategy_config_id=strategy_config_id,
            kind=kind,
            underlying_symbol_id=symbol_id_of(self._s, underlying),
            underlying=underlying,
            state="open",
            close_reason=None,
            frozen=False,
            qty=qty,
            entry_net=q4(entry_net),
            reserved_cash=q4(reserved_cash),
            take_profit_net=None if take_profit_net is None else q4(take_profit_net),
            cover_structure_id=cover_structure_id,
            parent_structure_id=parent_structure_id,
            realized_pnl=ZERO,
            fees_total=ZERO,
            opened_at=ts,
            closed_at=None,
            meta=dict(meta),
        )
        self._s.add(row)
        self._s.flush()
        return row.id

    def apply(
        self,
        structure_id: int,
        instrument: Instrument,
        contract_id: int | None,
        qty_delta: int,
        price: Decimal,
        ts: datetime,
    ) -> OptPositionView:
        s = self._get(structure_id)
        if (instrument == "option") != (contract_id is not None):
            raise ValueError("an option position names a contract; a shares position never does")
        if not isinstance(price, Decimal):
            raise TypeError(f"price must be a Decimal, not {type(price).__name__}")
        if qty_delta == 0:
            raise ValueError("qty_delta can't be 0")
        multiplier = 1 if contract_id is None else self._contract(contract_id).multiplier
        p = self.position_row(structure_id, contract_id)
        if p is None:
            p = m.OptPosition(
                run_id=self._run_id,
                structure_id=structure_id,
                instrument=instrument,
                contract_id=contract_id,
                underlying_symbol_id=s.underlying_symbol_id,
                qty=qty_delta,
                avg_price=q4(price),
                realized_pnl=ZERO,
                opened_at=ts,
                closed_at=None,
            )
            self._s.add(p)
        elif p.qty == 0:  # reopened
            p.qty, p.avg_price, p.closed_at = qty_delta, q4(price), None
        elif (p.qty > 0) == (qty_delta > 0):  # adding: re-average
            total = abs(p.qty) + abs(qty_delta)
            p.avg_price = q4((p.avg_price * abs(p.qty) + price * abs(qty_delta)) / total)
            p.qty += qty_delta
        else:  # reducing: realize
            closed = abs(qty_delta)
            if closed > abs(p.qty):
                raise ValueError(f"a change of {qty_delta} would take the position of {p.qty} through zero")
            per_unit = price - p.avg_price if p.qty > 0 else p.avg_price - price
            realized = q4(per_unit * closed * multiplier)
            p.realized_pnl += realized
            s.realized_pnl += realized
            p.qty += qty_delta
            if p.qty == 0:
                p.closed_at = ts
        self._s.flush()
        return self._position_view(p, s.underlying)

    def move_cash(
        self, amount: Decimal, kind: CashKind, ref: str, ts: datetime, *, structure_id: int | None = None
    ) -> None:
        if not isinstance(amount, Decimal):
            raise TypeError(f"amount must be a Decimal, not {type(amount).__name__}")
        if amount == 0 or (amount > 0) != (kind == "sell"):
            raise ValueError(f"wrong sign for a {kind} of {amount}: sells are > 0, buys and fees < 0")
        amount = q4(amount)
        if amount == 0:
            return  # under a hundredth of a cent: nothing to book
        if structure_id is not None:
            s = self._get(structure_id)
            if kind == "fee":
                s.fees_total += -amount
        self._ledger.record(
            self._s,
            run_id=self._run_id,
            ts=ts,
            trade_date=et_date(ts),
            amount=amount,
            kind=kind,
            ref=ref,
            currency=OPTIONS_CURRENCY,
        )

    def set_reserved(self, structure_id: int, amount: Decimal) -> None:
        if amount < 0:
            raise ValueError("reserved cash can't be negative")
        self._get(structure_id).reserved_cash = q4(amount)
        self._s.flush()

    def set_entry(self, structure_id: int, entry_net: Decimal, take_profit_net: Decimal | None) -> None:
        """A roll replaced the structure's legs: its entry and take-profit are those of the new legs."""
        s = self._get(structure_id)
        s.entry_net = q4(entry_net)
        s.take_profit_net = None if take_profit_net is None else q4(take_profit_net)
        self._s.flush()

    def close_structure(self, structure_id: int, close_reason: CloseReason, ts: datetime) -> None:
        s = self._get(structure_id)
        s.state, s.close_reason, s.closed_at, s.reserved_cash = "closed", close_reason, ts, ZERO
        self._s.flush()

    def freeze(self, structure_id: int, detail: str) -> None:
        s = self._get(structure_id)
        s.frozen = True
        s.meta = {**(s.meta or {}), "frozen_detail": detail, "frozen_at": self._clock.now().isoformat()}
        self._s.flush()

    def record_lifecycle(
        self,
        *,
        structure_id: int,
        position_id: int,
        contract_id: int | None,
        kind: LifecycleKind,
        session_date: date,
        ts: datetime,
        underlying_close: Decimal | None,
        strike: Decimal | None,
        qty: int,
        shares_delta: int,
        cash_delta: Decimal,
        detail: Mapping[str, Any],
    ) -> int | None:
        self._get(structure_id)
        exists = self._s.execute(
            select(m.OptLifecycleEvent.id).where(
                m.OptLifecycleEvent.position_id == position_id,
                m.OptLifecycleEvent.kind == kind,
                m.OptLifecycleEvent.session_date == session_date,
            )
        ).scalar_one_or_none()
        if exists is not None:
            return None
        row = m.OptLifecycleEvent(
            run_id=self._run_id,
            structure_id=structure_id,
            position_id=position_id,
            contract_id=contract_id,
            kind=kind,
            session_date=session_date,
            ts=ts,
            underlying_close=underlying_close,
            strike=strike,
            qty=qty,
            shares_delta=shares_delta,
            cash_delta=q4(cash_delta),
            detail=dict(detail),
        )
        self._s.add(row)
        self._s.flush()
        return row.id

    def link_lifecycle(self, event_id: int, new_structure_id: int) -> None:
        self._get(new_structure_id)
        row = self._s.execute(
            select(m.OptLifecycleEvent).where(
                m.OptLifecycleEvent.id == event_id, m.OptLifecycleEvent.run_id == self._run_id
            )
        ).scalar_one_or_none()
        if row is None:
            raise KeyError(f"unknown lifecycle event {event_id}")
        row.new_structure_id = new_structure_id
        self._s.flush()
