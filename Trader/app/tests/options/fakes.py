"""In-memory fakes of the option protocols (OPTSIM task plan §3.10). No network, no database, no sleeping.

- `FakeOptionMarket`: an `OptionMarketView` you fill by hand (`add_underlying`, `add_contract`, `set_quote`,
  `set_price`, `set_facts`, `set_bars`). Quotes are stamped with its clock's time when they are read.
- `FakeBook`: the reference `OptionBook` (real arithmetic). `tests/options/contract_book.py` is its contract.
- `FakeOptionBroker`: an `OptionBroker` over a `FakeBook` with a pluggable `CollateralEngine` (default
  `AcceptAll`: accept, reserve nothing). It records every request; `fill(order_id, leg_prices)` and
  `fill_all_at_market()` fill working orders and return the `OptionFillEvent`s.
- `FakeQtOptions`: an `OptionQuoteClient` (chains, option quotes, details, share quotes, daily candles).
- `FakePromptStore`, `RecordingHost`, `FakeFacts`, `FakeRegistry`: in-memory `PromptStore`, `StrategyHost`,
  `FactsProvider` and `OptionStrategyRegistryView`.
- `make_option_services(...)`: an `OptionApiServices` built from the fakes, for route tests.
"""

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote, QtSymbol
from trader.adapters.questrade.option_types import (
    QtChainExpiry,
    QtChainRoot,
    QtChainStrike,
    QtOptionQuote,
    QtSymbolDetails,
)
from trader.jobs.runner import JobOutcome
from trader.market.clock import Clock, et_date
from trader.market.types import Candle, Interval
from trader.option_strategies.base import (
    OptionEvent,
    OptionStrategyConfigView,
    PanelActionRequest,
    PanelActionResult,
    StrategyPanel,
)
from trader.options.protocols import (
    AnswerResult,
    CashKind,
    CollateralBook,
    CollateralEngine,
    OptionApiServices,
    UnknownContract,
    UnknownUnderlying,
)
from trader.options.settings import OptionSettings, OptionSettingsStore
from trader.options.types import (
    SOURCE_MANUAL,
    ZERO,
    AnsweredVia,
    ChainStrike,
    CloseReason,
    CollateralDecision,
    ContractKey,
    ExpiryInfo,
    Instrument,
    LegFill,
    LifecycleEvent,
    LifecycleKind,
    OptionAccountState,
    OptionContract,
    OptionFillEvent,
    OptionQuote,
    OptOrderView,
    OptPositionView,
    OrderRequest,
    OrderStatus,
    OwnerPromptRequest,
    PromptView,
    Right,
    StructureKind,
    StructureView,
    SubmitResult,
    UnderlyingFacts,
    dte,
    net_price,
)

Q4 = Decimal("0.0001")


def _dec(value: Decimal | str | int | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# --- the market ---------------------------------------------------------------------------------------------


@dataclass
class _Underlying:
    symbol_id: int
    last: Decimal
    bid: Decimal | None
    ask: Decimal | None


class FakeOptionMarket:
    """An in-memory `OptionMarketView`. `open` is what `is_open` answers. A quote set without `fetched_at`
    is stamped with the clock's time each time it is read (a live quote); pass `fetched_at=` to `set_quote`
    for a stale one. An unknown underlying raises `UnknownUnderlying`, an unknown contract id
    `UnknownContract`."""

    def __init__(self, clock: Clock, *, open: bool = True) -> None:
        self.clock = clock
        self.open = open
        self.underlyings: dict[str, _Underlying] = {}
        self.contracts: dict[int, OptionContract] = {}
        self.facts_by_ticker: dict[str, UnderlyingFacts] = {}
        self.bars: dict[str, list[Candle]] = {}
        self.quote_calls: list[tuple[int, ...]] = []
        self._quotes: dict[int, OptionQuote] = {}
        self._pinned: set[int] = set()  # contract ids whose quote keeps the fetched_at it was given

    # setup
    def add_underlying(
        self,
        ticker: str,
        price: Decimal | str | int,
        *,
        symbol_id: int | None = None,
        bid: Decimal | str | None = None,
        ask: Decimal | str | None = None,
    ) -> int:
        """Add (or re-price) an underlying; returns its symbol id."""
        known = self.underlyings.get(ticker)
        sid = symbol_id or (known.symbol_id if known else len(self.underlyings) + 1)
        self.underlyings[ticker] = _Underlying(sid, Decimal(str(price)), _dec(bid), _dec(ask))
        return sid

    set_price = add_underlying

    def add_contract(
        self,
        underlying: str,
        expiry: date,
        strike: Decimal | str | int,
        right: Right,
        *,
        bid: Decimal | str | None = None,
        ask: Decimal | str | None = None,
        multiplier: int = 100,
        is_monthly: bool | None = None,
        adjusted: bool = False,
        root: str | None = None,
        **quote: Any,
    ) -> OptionContract:
        """Add a contract (and its quote when `bid` and `ask` are given; `quote` as for `set_quote`)."""
        cid = len(self.contracts) + 1
        contract = OptionContract(
            id=cid,
            underlying=underlying,
            underlying_symbol_id=self._underlying(underlying).symbol_id,
            qt_symbol_id=900_000 + cid,
            root=root or underlying,
            expiry=expiry,
            strike=Decimal(str(strike)),
            right=right,
            multiplier=multiplier,
            is_monthly=self.is_monthly(expiry) if is_monthly is None else is_monthly,
            adjusted=adjusted,
        )
        self.contracts[cid] = contract
        if bid is not None or ask is not None or quote:
            self.set_quote(cid, bid, ask, **quote)
        return contract

    def set_quote(
        self,
        contract_id: int,
        bid: Decimal | str | None,
        ask: Decimal | str | None,
        *,
        fetched_at: datetime | None = None,
        delay: int | None = 0,
        is_halted: bool = False,
        last_trade_time: datetime | None = None,
        bid_size: int | None = 10,
        ask_size: int | None = 10,
        volume: int | None = 100,
        open_interest: int | None = 1000,
        **greeks: Decimal | str | None,
    ) -> None:
        """`greeks`: last, iv, delta, gamma, theta, vega (Decimals or strings)."""
        unknown = set(greeks) - {"last", "iv", "delta", "gamma", "theta", "vega"}
        if unknown:
            raise TypeError(f"set_quote: unknown fields {sorted(unknown)}")
        if contract_id not in self.contracts:
            raise UnknownContract(contract_id)
        self._quotes[contract_id] = OptionQuote(
            contract_id=contract_id,
            bid=_dec(bid),
            ask=_dec(ask),
            last=_dec(greeks.get("last")),
            bid_size=bid_size,
            ask_size=ask_size,
            volume=volume,
            open_interest=open_interest,
            iv=_dec(greeks.get("iv")),
            delta=_dec(greeks.get("delta")),
            gamma=_dec(greeks.get("gamma")),
            theta=_dec(greeks.get("theta")),
            vega=_dec(greeks.get("vega")),
            last_trade_time=last_trade_time,
            delay=delay,
            is_halted=is_halted,
            fetched_at=fetched_at or self.clock.now(),
        )
        if fetched_at is None:
            self._pinned.discard(contract_id)
        else:
            self._pinned.add(contract_id)

    def clear_quote(self, contract_id: int) -> None:
        self._quotes.pop(contract_id, None)

    def set_facts(self, ticker: str, **fields: Any) -> UnderlyingFacts:
        """Set (or update) the ticker's facts; unnamed fields keep their value (None at first)."""
        base = self.facts_by_ticker.get(ticker) or UnderlyingFacts(
            symbol_id=self._underlying(ticker).symbol_id, as_of=et_date(self.clock.now()), ticker=ticker
        )
        facts = dataclasses.replace(base, **fields)
        self.facts_by_ticker[ticker] = facts
        return facts

    def set_bars(self, ticker: str, bars: Iterable[Candle]) -> None:
        self.bars[ticker] = sorted(bars, key=lambda c: c.start)

    # OptionMarketView
    def _underlying(self, ticker: str) -> _Underlying:
        try:
            return self.underlyings[ticker]
        except KeyError:
            raise UnknownUnderlying(ticker) from None

    def _of(self, underlying: str) -> list[OptionContract]:
        self._underlying(underlying)
        return [c for c in self.contracts.values() if c.underlying == underlying]

    async def expiries(self, underlying: str) -> list[ExpiryInfo]:
        today = et_date(self.clock.now())
        by_expiry: dict[date, set[Decimal]] = {}
        for c in self._of(underlying):
            by_expiry.setdefault(c.expiry, set()).add(c.strike)
        return [
            ExpiryInfo(expiry, dte(expiry, today), self.is_monthly(expiry), len(strikes))
            for expiry, strikes in sorted(by_expiry.items())
        ]

    async def strikes(self, underlying: str, expiry: date) -> list[ChainStrike]:
        rows: dict[Decimal, dict[str, int]] = {}
        for c in self._of(underlying):
            if c.expiry == expiry:
                rows.setdefault(c.strike, {})[c.right] = c.id
        return [ChainStrike(k, v.get("call"), v.get("put")) for k, v in sorted(rows.items())]

    async def contract(self, contract_id: int) -> OptionContract:
        try:
            return self.contracts[contract_id]
        except KeyError:
            raise UnknownContract(contract_id) from None

    async def find_contract(self, key: ContractKey) -> OptionContract | None:
        for c in self.contracts.values():
            if (c.underlying, c.expiry, c.strike, c.right) == (
                key.underlying,
                key.expiry,
                key.strike,
                key.right,
            ):
                return c
        return None

    def _quote(self, contract_id: int) -> OptionQuote | None:
        quote = self._quotes.get(contract_id)
        if quote is None or contract_id in self._pinned:
            return quote
        return dataclasses.replace(quote, fetched_at=self.clock.now())

    async def quotes(self, contract_ids: Sequence[int]) -> dict[int, OptionQuote]:
        self.quote_calls.append(tuple(contract_ids))
        found = {cid: self._quote(cid) for cid in contract_ids}
        return {cid: q for cid, q in found.items() if q is not None}

    async def quotes_for_expiry(
        self,
        underlying: str,
        expiry: date,
        right: Right,
        min_strike: Decimal | None = None,
        max_strike: Decimal | None = None,
    ) -> list[tuple[OptionContract, OptionQuote]]:
        out: list[tuple[OptionContract, OptionQuote]] = []
        for c in sorted(self._of(underlying), key=lambda c: c.strike):
            if c.expiry != expiry or c.right != right:
                continue
            if (min_strike is not None and c.strike < min_strike) or (
                max_strike is not None and c.strike > max_strike
            ):
                continue
            quote = self._quote(c.id)
            if quote is not None:
                out.append((c, quote))
        return out

    async def underlying_quote(self, underlying: str) -> QtQuote | None:
        u = self._underlying(underlying)
        now = self.clock.now()
        return QtQuote(
            symbol_id=u.symbol_id,
            symbol=underlying,
            bid=u.bid if u.bid is not None else u.last,
            ask=u.ask if u.ask is not None else u.last,
            last=u.last,
            last_regular=u.last,
            volume=0,
            last_trade_time=now,
            delay=0,
            is_halted=False,
            vwap=None,
            fetched_at=now,
        )

    async def facts(self, underlying: str) -> UnderlyingFacts | None:
        return self.facts_by_ticker.get(underlying)

    async def daily_bars(self, underlying: str, start: date, end: date) -> list[Candle]:
        return [c for c in self.bars.get(underlying, []) if start <= et_date(c.start) <= end]

    def is_open(self, now: datetime) -> bool:
        return self.open

    def is_monthly(self, expiry: date) -> bool:
        return expiry.weekday() == 4 and 15 <= expiry.day <= 21


# --- the book -----------------------------------------------------------------------------------------------


@dataclass
class _Position:
    id: int
    structure_id: int
    instrument: Instrument
    contract_id: int | None
    qty: int
    avg_price: Decimal
    realized_pnl: Decimal
    opened_at: datetime
    closed_at: datetime | None = None


@dataclass
class _Structure:
    id: int
    source: str
    strategy_config_id: int | None
    kind: StructureKind
    underlying: str
    qty: int
    entry_net: Decimal
    reserved_cash: Decimal
    take_profit_net: Decimal | None
    cover_structure_id: int | None
    parent_structure_id: int | None
    opened_at: datetime
    meta: dict[str, Any]
    state: str = "open"
    close_reason: CloseReason | None = None
    frozen: bool = False
    realized_pnl: Decimal = ZERO
    fees_total: Decimal = ZERO
    closed_at: datetime | None = None


@dataclass(frozen=True)
class CashMove:
    amount: Decimal
    kind: str  # deposit | buy | sell | fee
    ref: str
    ts: datetime | None
    structure_id: int | None = None


@dataclass
class LifecycleRow:
    id: int
    structure_id: int
    position_id: int
    contract_id: int | None
    kind: LifecycleKind
    session_date: date
    ts: datetime
    underlying_close: Decimal | None
    strike: Decimal | None
    qty: int
    shares_delta: int
    cash_delta: Decimal
    detail: dict[str, Any]
    new_structure_id: int | None = None


class FakeBook:
    """The reference in-memory `OptionBook`. `contracts` is a live mapping of the known contracts (pass
    `FakeOptionMarket.contracts`); an option position on an unknown contract raises `UnknownContract`.

    Beyond the protocol: `ledger` (every cash move, the opening deposit first), `lifecycle` (the recorded
    events), `order_reserved` (order id -> reserved cash of a working order; `FakeOptionBroker` keeps it),
    `position(structure_id, contract_id)`."""

    def __init__(
        self,
        cash: Decimal | str | int = 5000,
        contracts: Mapping[int, OptionContract] | None = None,
    ) -> None:
        self.contracts: Mapping[int, OptionContract] = contracts if contracts is not None else {}
        self.ledger: list[CashMove] = [CashMove(Decimal(str(cash)), "deposit", "sim_account:1", None)]
        self.lifecycle: list[LifecycleRow] = []
        self.order_reserved: dict[int, Decimal] = {}
        self._structures: dict[int, _Structure] = {}
        self._positions: dict[int, _Position] = {}

    # reads
    def cash(self) -> Decimal:
        return sum((move.amount for move in self.ledger), ZERO)

    def reserved(self) -> Decimal:
        open_structures = sum((s.reserved_cash for s in self._structures.values() if s.state == "open"), ZERO)
        return open_structures + sum(self.order_reserved.values(), ZERO)

    def _contract(self, contract_id: int) -> OptionContract:
        try:
            return self.contracts[contract_id]
        except KeyError:
            raise UnknownContract(contract_id) from None

    def _position_view(self, p: _Position, underlying: str) -> OptPositionView:
        return OptPositionView(
            id=p.id,
            structure_id=p.structure_id,
            instrument=p.instrument,
            contract=None if p.contract_id is None else self._contract(p.contract_id),
            underlying=underlying,
            qty=p.qty,
            avg_price=p.avg_price,
            realized_pnl=p.realized_pnl,
        )

    def _view(self, s: _Structure) -> StructureView:
        positions = sorted(
            (p for p in self._positions.values() if p.structure_id == s.id), key=lambda p: p.id
        )
        return StructureView(
            id=s.id,
            source=s.source,
            strategy_config_id=s.strategy_config_id,
            kind=s.kind,
            underlying=s.underlying,
            state="open" if s.state == "open" else "closed",
            close_reason=s.close_reason,
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
            positions=tuple(self._position_view(p, s.underlying) for p in positions),
            meta=dict(s.meta),
        )

    def _get(self, structure_id: int) -> _Structure:
        try:
            return self._structures[structure_id]
        except KeyError:
            raise KeyError(f"unknown structure {structure_id}") from None

    def structure(self, structure_id: int) -> StructureView:
        return self._view(self._get(structure_id))

    def structures(
        self, *, open_only: bool = True, source: str | None = None, underlying: str | None = None
    ) -> list[StructureView]:
        return [
            self._view(s)
            for s in sorted(self._structures.values(), key=lambda s: s.id)
            if (not open_only or s.state == "open")
            and (source is None or s.source == source)
            and (underlying is None or s.underlying == underlying)
        ]

    def position(self, structure_id: int, contract_id: int | None) -> OptPositionView | None:
        """The structure's position in that contract (None: its shares), if it ever had one."""
        s = self._get(structure_id)
        for p in self._positions.values():
            if p.structure_id == structure_id and p.contract_id == contract_id:
                return self._position_view(p, s.underlying)
        return None

    # writes
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
        sid = len(self._structures) + 1
        self._structures[sid] = _Structure(
            id=sid,
            source=source,
            strategy_config_id=strategy_config_id,
            kind=kind,
            underlying=underlying,
            qty=qty,
            entry_net=entry_net,
            reserved_cash=reserved_cash,
            take_profit_net=take_profit_net,
            cover_structure_id=cover_structure_id,
            parent_structure_id=parent_structure_id,
            opened_at=ts,
            meta=dict(meta),
        )
        return sid

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
        p = next(
            (
                p
                for p in self._positions.values()
                if p.structure_id == structure_id and p.contract_id == contract_id
            ),
            None,
        )
        if p is None:
            pid = len(self._positions) + 1
            p = _Position(pid, structure_id, instrument, contract_id, qty_delta, price, ZERO, ts)
            self._positions[pid] = p
        elif p.qty == 0:  # reopened
            p.qty, p.avg_price, p.closed_at = qty_delta, price, None
        elif (p.qty > 0) == (qty_delta > 0):  # adding: re-average
            total = abs(p.qty) + abs(qty_delta)
            p.avg_price = ((p.avg_price * abs(p.qty) + price * abs(qty_delta)) / total).quantize(
                Q4, ROUND_HALF_UP
            )
            p.qty += qty_delta
        else:  # reducing: realize
            closed = abs(qty_delta)
            if closed > abs(p.qty):
                raise ValueError(f"a change of {qty_delta} would take the position of {p.qty} through zero")
            per_unit = price - p.avg_price if p.qty > 0 else p.avg_price - price
            realized = per_unit * closed * multiplier
            p.realized_pnl += realized
            s.realized_pnl += realized
            p.qty += qty_delta
            if p.qty == 0:
                p.closed_at = ts
        return self._position_view(p, s.underlying)

    def move_cash(
        self, amount: Decimal, kind: CashKind, ref: str, ts: datetime, *, structure_id: int | None = None
    ) -> None:
        if not isinstance(amount, Decimal):
            raise TypeError(f"amount must be a Decimal, not {type(amount).__name__}")
        if amount == 0 or (amount > 0) != (kind == "sell"):
            raise ValueError(f"wrong sign for a {kind} of {amount}: sells are > 0, buys and fees < 0")
        if structure_id is not None:
            s = self._get(structure_id)
            if kind == "fee":
                s.fees_total += -amount
        self.ledger.append(CashMove(amount, kind, ref, ts, structure_id))

    def set_reserved(self, structure_id: int, amount: Decimal) -> None:
        if amount < 0:
            raise ValueError("reserved cash can't be negative")
        self._get(structure_id).reserved_cash = amount

    def close_structure(self, structure_id: int, close_reason: CloseReason, ts: datetime) -> None:
        s = self._get(structure_id)
        s.state, s.close_reason, s.closed_at, s.reserved_cash = "closed", close_reason, ts, ZERO

    def freeze(self, structure_id: int, detail: str) -> None:
        s = self._get(structure_id)
        s.frozen = True
        s.meta = {**s.meta, "frozen_detail": detail}

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
        for row in self.lifecycle:
            if (row.position_id, row.kind, row.session_date) == (position_id, kind, session_date):
                return None
        row = LifecycleRow(
            id=len(self.lifecycle) + 1,
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
            cash_delta=cash_delta,
            detail=dict(detail),
        )
        self.lifecycle.append(row)
        return row.id

    def link_lifecycle(self, event_id: int, new_structure_id: int) -> None:
        self._get(new_structure_id)
        for row in self.lifecycle:
            if row.id == event_id:
                row.new_structure_id = new_structure_id
                return
        raise KeyError(f"unknown lifecycle event {event_id}")

    def lifecycle_event(self, event_id: int) -> LifecycleEvent:
        """The recorded event as the `LifecycleEvent` value a lifecycle engine returns."""
        row = next(r for r in self.lifecycle if r.id == event_id)
        s = self._get(row.structure_id)
        return LifecycleEvent(
            id=row.id,
            structure_id=row.structure_id,
            source=s.source,
            strategy_config_id=s.strategy_config_id,
            kind=row.kind,
            session_date=row.session_date,
            ts=row.ts,
            contract=None if row.contract_id is None else self._contract(row.contract_id),
            qty=row.qty,
            strike=row.strike,
            underlying_close=row.underlying_close,
            shares_delta=row.shares_delta,
            cash_delta=row.cash_delta,
            new_structure_id=row.new_structure_id,
        )


# --- the broker ---------------------------------------------------------------------------------------------


class AcceptAll:
    """The default `CollateralEngine` of `FakeOptionBroker`: accept everything, reserve nothing."""

    def evaluate(self, req: OrderRequest, book: CollateralBook) -> CollateralDecision:
        return accept(cash=book.account.cash, free_cash=book.account.free_cash)


def accept(
    *,
    kind: StructureKind = "custom",
    reserve_cash: Decimal | str = "0",
    cash: Decimal | str = "5000",
    free_cash: Decimal | str | None = None,
    cover_structure_id: int | None = None,
) -> CollateralDecision:
    """An accepting decision (for a canned engine or a view test)."""
    cash_d = Decimal(str(cash))
    return CollateralDecision(
        accepted=True,
        reject_reason=None,
        detail="",
        kind=kind,
        net_at_market=None,
        reserve_cash=Decimal(str(reserve_cash)),
        max_loss=None,
        max_profit=None,
        breakevens=(),
        fees=ZERO,
        cash_after=cash_d,
        free_cash_after=cash_d if free_cash is None else Decimal(str(free_cash)),
        exposure_after=ZERO,
        cap_limit=ZERO,
        pairs=(),
        cover_structure_id=cover_structure_id,
    )


def reject(reason: Any, detail: str = "", *, kind: StructureKind = "custom") -> CollateralDecision:
    """A rejecting decision with that `RejectReason`."""
    return dataclasses.replace(accept(kind=kind), accepted=False, reject_reason=reason, detail=detail)


class CannedCollateral:
    """A `CollateralEngine` that answers `decision` to everything and records what it was asked."""

    def __init__(self, decision: CollateralDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[OrderRequest, CollateralBook]] = []

    def evaluate(self, req: OrderRequest, book: CollateralBook) -> CollateralDecision:
        self.calls.append((req, book))
        return self.decision


class FakeOptionBroker:
    """An `OptionBroker` over a `FakeBook`. Orders work until the test fills them: `fill(order_id,
    {leg_no: price})`, `fill_all_at_market()` (buy at the ask, sell at the bid, from the market's quotes),
    or set `fill_on_poll = True` and `poll(now)` fills every working order it can at the market.

    Recorded: `previews`, `submitted` (every request), `calls` (method names in call order), `cancels` and
    `reprices`. A fill writes the book as the real broker does: positions, ledger rows (`opt_fill:<n>`),
    fees (`options.fee_per_contract` per contract and leg, `options.share_commission` per shares leg), the
    structure and its reserve; a structure whose positions are all zero is closed."""

    def __init__(
        self,
        market: FakeOptionMarket,
        book: FakeBook | None = None,
        *,
        collateral: CollateralEngine | None = None,
        settings: OptionSettings | None = None,
    ) -> None:
        self.market = market
        self.clock = market.clock
        self.book = book if book is not None else FakeBook(contracts=market.contracts)
        self.collateral: CollateralEngine = collateral if collateral is not None else AcceptAll()
        self.settings = settings if settings is not None else OptionSettings()
        self.fill_on_poll = False
        self.previews: list[OrderRequest] = []
        self.submitted: list[OrderRequest] = []
        self.calls: list[str] = []
        self.cancels: list[tuple[int, str, str]] = []
        self.reprices: list[tuple[int, Decimal, str]] = []
        self.fills: list[OptionFillEvent] = []
        self.premium_collected = ZERO
        self._orders: dict[int, OptOrderView] = {}
        self._decisions: dict[int, CollateralDecision] = {}

    # helpers
    def _working(self) -> list[OptOrderView]:
        return [o for o in self._orders.values() if o.status == "working"]

    def _replace(self, order_id: int, **changes: Any) -> OptOrderView:
        order = dataclasses.replace(self._orders[order_id], **changes)
        self._orders[order_id] = order
        return order

    async def _collateral_book(self, req: OrderRequest) -> CollateralBook:
        ids = sorted(
            {leg.contract_id for leg in req.legs if leg.contract_id is not None}
            | {
                p.contract.id
                for s in self.book.structures()
                for p in s.positions
                if p.contract is not None and p.qty != 0
            }
        )
        known = [cid for cid in ids if cid in self.market.contracts]
        share_quotes: dict[str, QtQuote] = {}
        for ticker in {req.underlying, *(s.underlying for s in self.book.structures())}:
            if ticker in self.market.underlyings:
                quote = await self.market.underlying_quote(ticker)
                if quote is not None:
                    share_quotes[ticker] = quote
        return CollateralBook(
            account=await self.account(),
            structures=self.book.structures(),
            working_orders=self._working(),
            contracts={cid: self.market.contracts[cid] for cid in known},
            quotes={cid: q for cid in known if (q := self.market._quote(cid)) is not None},
            share_quotes=share_quotes,
            settings=self.settings,
            today=et_date(self.clock.now()),
        )

    # OptionBroker
    async def preview(self, req: OrderRequest) -> CollateralDecision:
        self.calls.append("preview")
        self.previews.append(req)
        return self.collateral.evaluate(req, await self._collateral_book(req))

    async def submit(self, req: OrderRequest) -> SubmitResult:
        self.calls.append("submit")
        self.submitted.append(req)
        decision = self.collateral.evaluate(req, await self._collateral_book(req))
        now = self.clock.now()
        order_id = len(self._orders) + 1
        order = OptOrderView(
            id=order_id,
            source=req.source,
            strategy_config_id=req.strategy_config_id,
            intent=req.intent,
            structure_id=req.structure_id,
            underlying=req.underlying,
            legs=req.legs,
            qty=req.qty,
            order_type=req.order_type,
            net_limit=req.net_limit,
            tif=req.tif,
            walk=req.walk,
            take_profit_pct=req.take_profit_pct,
            reason=req.reason,
            evidence=req.evidence,
            submitted_by=req.submitted_by,
            status="working" if decision.accepted else "rejected",
            reject_reason=decision.reject_reason,
            reject_detail=None if decision.accepted else decision.detail,
            reserved_cash=decision.reserve_cash if decision.accepted else ZERO,
            submitted_at=now,
            closed_at=None if decision.accepted else now,
            fill_net=None,
            fees=None,
        )
        self._orders[order_id] = order
        self._decisions[order_id] = decision
        if decision.accepted:
            self.book.order_reserved[order_id] = decision.reserve_cash
        return SubmitResult(order, decision)

    async def cancel(self, order_id: int, reason: str, actor: str) -> bool:
        self.calls.append("cancel")
        order = self._orders.get(order_id)
        if order is None or order.status != "working":
            return False
        self.cancels.append((order_id, reason, actor))
        self._replace(order_id, status="cancelled", closed_at=self.clock.now(), reserved_cash=ZERO)
        self.book.order_reserved.pop(order_id, None)
        return True

    async def reprice(self, order_id: int, net_limit: Decimal, actor: str) -> bool:
        self.calls.append("reprice")
        if not isinstance(net_limit, Decimal):
            raise TypeError(f"net_limit must be a Decimal, not {type(net_limit).__name__}")
        order = self._orders.get(order_id)
        if order is None or order.status != "working":
            return False
        self.reprices.append((order_id, net_limit, actor))
        self._replace(order_id, net_limit=net_limit, walk=False, order_type="limit")
        return True

    async def poll(self, now: datetime) -> list[OptionFillEvent]:
        self.calls.append("poll")
        return await self.fill_all_at_market(now) if self.fill_on_poll else []

    async def walk(self, now: datetime) -> int:
        self.calls.append("walk")
        return 0

    async def take_profits(self, now: datetime) -> list[int]:
        self.calls.append("take_profits")
        return []

    async def expire_day_orders(self, session_date: date, now: datetime) -> int:
        self.calls.append("expire_day_orders")
        expired = [o for o in self._working() if o.tif == "day"]
        for order in expired:
            self._replace(order.id, status="expired", closed_at=now, reserved_cash=ZERO)
            self.book.order_reserved.pop(order.id, None)
        return len(expired)

    async def account(self) -> OptionAccountState:
        now = self.clock.now()
        cash, reserved = self.book.cash(), self.book.reserved()
        value, complete = ZERO, True
        for s in self.book.structures():
            for p in s.positions:
                if p.qty == 0:
                    continue
                if p.contract is None:
                    share = self.market.underlyings.get(s.underlying)
                    mark, multiplier = (share.last if share else None), 1
                else:
                    quote = self.market._quote(p.contract.id)
                    mark = None if quote is None else (quote.bid if p.qty > 0 else quote.ask)
                    multiplier = p.contract.multiplier
                if mark is None:
                    complete = False
                else:
                    value += mark * p.qty * multiplier
        return OptionAccountState(
            cash=cash,
            reserved=reserved,
            free_cash=cash - reserved,
            positions_value=value,
            equity=cash + value,
            premium_collected=self.premium_collected,
            as_of=now,
            marks_complete=complete,
        )

    async def structures(self, *, source: str | None = None, open_only: bool = True) -> list[StructureView]:
        return self.book.structures(open_only=open_only, source=source)

    async def orders(
        self, *, status: OrderStatus | None = None, source: str | None = None, limit: int = 200
    ) -> list[OptOrderView]:
        found = [
            o
            for o in sorted(self._orders.values(), key=lambda o: o.id, reverse=True)
            if (status is None or o.status == status) and (source is None or o.source == source)
        ]
        return found[:limit]

    async def order(self, order_id: int) -> OptOrderView | None:
        return self._orders.get(order_id)

    # filling (test controls)
    def fill(
        self, order_id: int, leg_prices: Mapping[int, Decimal], now: datetime | None = None
    ) -> OptionFillEvent:
        """Fill a working order at the given price per leg (`leg_no` -> Decimal) and write the book."""
        order = self._orders[order_id]
        if order.status != "working":
            raise ValueError(f"order {order_id} is {order.status}, not working")
        ts = now or self.clock.now()
        decision = self._decisions[order_id]
        multipliers = {
            leg.leg_no: self.market.contracts[leg.contract_id].multiplier
            for leg in order.legs
            if leg.contract_id is not None
        }
        net = net_price(order.legs, leg_prices, multipliers)
        opening = [leg for leg in order.legs if leg.effect == "open"]
        closing = [leg for leg in order.legs if leg.effect == "close"]
        target = order.structure_id
        if closing and target is None:
            raise ValueError("a closing leg needs the order's structure_id")
        # A roll's opening legs go into the same structure; an open creates one.
        if order.intent == "open":
            entry = net
            pct = order.take_profit_pct
            target = self.book.add_structure(
                kind=decision.kind,
                source=order.source,
                strategy_config_id=order.strategy_config_id,
                underlying=order.underlying,
                qty=order.qty,
                entry_net=entry,
                reserved_cash=decision.reserve_cash,
                cover_structure_id=decision.cover_structure_id,
                parent_structure_id=None,
                take_profit_net=-entry * (1 - pct) if pct is not None and entry > 0 else None,
                meta={},
                ts=ts,
            )
        if target is None:
            raise ValueError("the order names no structure")
        before = self.book.structure(target).realized_pnl
        ref = f"opt_fill:{len(self.fills) + 1}"
        fills: list[LegFill] = []
        fees = ZERO
        for leg in [*closing, *opening]:
            price = leg_prices[leg.leg_no]
            multiplier = multipliers.get(leg.leg_no, 1)
            units = leg.ratio * order.qty
            self.book.apply(
                target, leg.instrument, leg.contract_id, units if leg.side == "buy" else -units, price, ts
            )
            amount = price * units * multiplier
            if amount != 0:
                self.book.move_cash(
                    amount if leg.side == "sell" else -amount, leg.side, ref, ts, structure_id=target
                )
            fee = (
                self.settings.share_commission
                if leg.instrument == "shares"
                else self.settings.fee_per_contract * units
            )
            fees += fee
            if leg.instrument == "option" and leg.side == "sell" and leg.effect == "open":
                self.premium_collected += amount
            fills.append(
                LegFill(
                    leg.leg_no, leg.instrument, leg.contract_id, leg.side, leg.effect, units, price, fee, {}
                )
            )
        if fees > 0:
            self.book.move_cash(-fees, "fee", ref, ts, structure_id=target)
        view = self.book.structure(target)
        if all(p.qty == 0 for p in view.positions):
            self.book.close_structure(target, "sold" if view.kind == "shares" else "closed", ts)
        elif order.intent != "open":
            self.book.set_reserved(target, decision.reserve_cash)
        self.book.order_reserved.pop(order_id, None)
        self._replace(order_id, status="filled", closed_at=ts, fill_net=net, fees=fees, reserved_cash=ZERO)
        event = OptionFillEvent(
            order_id=order_id,
            structure_id=target,
            source=order.source,
            strategy_config_id=order.strategy_config_id,
            intent=order.intent,
            ts=ts,
            qty=order.qty,
            net_price=net,
            fees=fees,
            legs=tuple(sorted(fills, key=lambda f: f.leg_no)),
            realized_pnl=(view.realized_pnl - before) if closing else None,
        )
        self.fills.append(event)
        return event

    def market_prices(self, order: OptOrderView) -> dict[int, Decimal] | None:
        """Buy at the ask, sell at the bid, per leg; None when a leg has no such quote."""
        prices: dict[int, Decimal] = {}
        for leg in order.legs:
            if leg.contract_id is None:
                share = self.market.underlyings.get(leg.underlying)
                if share is None:
                    return None
                side_price = share.ask if leg.side == "buy" else share.bid
                price = side_price if side_price is not None else share.last
            else:
                quote = self.market._quote(leg.contract_id)
                price = None if quote is None else (quote.ask if leg.side == "buy" else quote.bid)
            if price is None:
                return None
            prices[leg.leg_no] = price
        return prices

    async def fill_all_at_market(self, now: datetime | None = None) -> list[OptionFillEvent]:
        """Fill every working order whose legs all have a quote, in submit order."""
        events: list[OptionFillEvent] = []
        for order in sorted(self._working(), key=lambda o: o.id):
            prices = self.market_prices(order)
            if prices is not None:
                events.append(self.fill(order.id, prices, now))
        return events


# --- Questrade ----------------------------------------------------------------------------------------------


class FakeQtOptions:
    """An `OptionQuoteClient`. Ids are Questrade symbol ids. Fill it with `add_symbol`, `add_expiry`,
    `set_option_quote`, `set_share_quote`, `set_candles`; `calls` records (method, arguments); put an
    exception in `errors[method]` to make that method raise."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.symbols: dict[str, QtSymbol] = {}
        self.details: dict[int, QtSymbolDetails] = {}
        self.chains: dict[int, list[QtChainExpiry]] = {}
        self.option_quote_rows: dict[int, QtOptionQuote] = {}
        self.share_quotes: dict[int, QtQuote] = {}
        self.daily: dict[int, list[Candle]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.errors: dict[str, BaseException] = {}
        self._options: dict[int, tuple[int, date, Right, Decimal]] = {}  # option id -> its contract

    def _called(self, method: str, *args: Any) -> None:
        self.calls.append((method, args))
        if method in self.errors:
            raise self.errors[method]

    def add_symbol(
        self,
        ticker: str,
        symbol_id: int,
        *,
        has_options: bool = True,
        security_type: str = "Stock",
        description: str | None = None,
        **details: Any,
    ) -> None:
        """A share symbol and its details (`details`: eps, pe, market_cap, dividend, ex_date, yield_pct,
        industry_sector, industry_group)."""
        text = description or f"{ticker} Inc"
        self.symbols[ticker] = QtSymbol(symbol_id, ticker, "NYSE", "USD", text, True, True)
        values: dict[str, Any] = {
            "eps": None,
            "pe": None,
            "market_cap": None,
            "dividend": None,
            "ex_date": None,
            "yield_pct": None,
            "industry_sector": None,
            "industry_group": None,
            **details,
        }
        self.details[symbol_id] = QtSymbolDetails(
            symbol_id=symbol_id,
            symbol=ticker,
            description=text,
            security_type=security_type,
            listing_exchange="NYSE",
            currency="USD",
            has_options=has_options,
            **values,
        )

    def add_expiry(
        self,
        underlying_id: int,
        expiry: date,
        strikes: Iterable[tuple[Decimal | str, int, int]],
        *,
        root: str | None = None,
        multiplier: int = 100,
    ) -> None:
        """One expiry of the underlying's chain; `strikes` are (strike, call id, put id)."""
        ticker = next((t for t, s in self.symbols.items() if s.symbol_id == underlying_id), "X")
        rows = tuple(QtChainStrike(Decimal(str(k)), call, put) for k, call, put in strikes)
        for row in rows:
            self._options[row.call_id] = (underlying_id, expiry, "call", row.strike)
            self._options[row.put_id] = (underlying_id, expiry, "put", row.strike)
        chain = [e for e in self.chains.get(underlying_id, []) if e.expiry != expiry]
        chain.append(QtChainExpiry(expiry, (QtChainRoot(root or ticker, multiplier, rows),)))
        self.chains[underlying_id] = sorted(chain, key=lambda e: e.expiry)

    def set_option_quote(
        self,
        option_id: int,
        bid: Decimal | str | None,
        ask: Decimal | str | None,
        *,
        delay: int | None = 0,
        is_halted: bool = False,
        **fields: Any,
    ) -> None:
        """`fields`: last, bid_size, ask_size, volume, open_interest, iv_pct, delta, gamma, theta, vega,
        rho, last_trade_time, vwap."""
        underlying_id = self._options.get(option_id, (0, None, None, None))[0]
        ticker = next((t for t, s in self.symbols.items() if s.symbol_id == underlying_id), "")
        values: dict[str, Any] = {
            "last": None,
            "bid_size": 10,
            "ask_size": 10,
            "volume": 100,
            "open_interest": 1000,
            "iv_pct": None,
            "delta": None,
            "gamma": None,
            "theta": None,
            "vega": None,
            "rho": None,
            "last_trade_time": None,
            "vwap": None,
            **fields,
        }
        for name in ("last", "iv_pct", "delta", "gamma", "theta", "vega", "rho", "vwap"):
            values[name] = _dec(values[name])
        self.option_quote_rows[option_id] = QtOptionQuote(
            symbol_id=option_id,
            symbol=f"{ticker}{option_id}",
            underlying=ticker,
            underlying_id=underlying_id,
            bid=_dec(bid),
            ask=_dec(ask),
            delay=delay,
            is_halted=is_halted,
            fetched_at=self.clock.now(),
            requested_at=None,
            **values,
        )

    def set_share_quote(
        self,
        symbol_id: int,
        last: Decimal | str,
        *,
        bid: Decimal | str | None = None,
        ask: Decimal | str | None = None,
        delay: int | None = 0,
    ) -> None:
        ticker = next((t for t, s in self.symbols.items() if s.symbol_id == symbol_id), "")
        price = Decimal(str(last))
        self.share_quotes[symbol_id] = QtQuote(
            symbol_id=symbol_id,
            symbol=ticker,
            bid=_dec(bid) or price,
            ask=_dec(ask) or price,
            last=price,
            last_regular=price,
            volume=0,
            last_trade_time=self.clock.now(),
            delay=delay,
            is_halted=False,
            vwap=None,
        )

    def set_candles(self, symbol_id: int, candles: Iterable[Candle]) -> None:
        self.daily[symbol_id] = sorted(candles, key=lambda c: c.start)

    def _stamped(self, quote: QtOptionQuote) -> QtOptionQuote:
        now = self.clock.now()
        return dataclasses.replace(quote, fetched_at=now, requested_at=now)

    # OptionQuoteClient
    async def option_chain(self, symbol_id: int) -> list[QtChainExpiry]:
        self._called("option_chain", symbol_id)
        return list(self.chains.get(symbol_id, []))

    async def option_quotes(self, ids: Sequence[int]) -> list[QtOptionQuote]:
        self._called("option_quotes", tuple(ids))
        return [self._stamped(self.option_quote_rows[i]) for i in ids if i in self.option_quote_rows]

    async def option_quotes_filter(
        self,
        underlying_id: int,
        expiry: date,
        right: Right,
        min_strike: Decimal | None = None,
        max_strike: Decimal | None = None,
    ) -> list[QtOptionQuote]:
        self._called("option_quotes_filter", underlying_id, expiry, right, min_strike, max_strike)
        out: list[tuple[Decimal, QtOptionQuote]] = []
        for option_id, (uid, exp, side, strike) in self._options.items():
            if (uid, exp, side) != (underlying_id, expiry, right) or option_id not in self.option_quote_rows:
                continue
            if (min_strike is not None and strike < min_strike) or (
                max_strike is not None and strike > max_strike
            ):
                continue
            out.append((strike, self._stamped(self.option_quote_rows[option_id])))
        return [quote for _, quote in sorted(out, key=lambda pair: pair[0])]

    async def symbol_details(self, ids: Sequence[int]) -> dict[int, QtSymbolDetails]:
        self._called("symbol_details", tuple(ids))
        return {i: self.details[i] for i in ids if i in self.details}

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        self._called("quotes", tuple(ids))
        now = self.clock.now()
        return [
            dataclasses.replace(self.share_quotes[i], fetched_at=now, requested_at=now)
            for i in ids
            if i in self.share_quotes
        ]

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        self._called("candles", symbol_id, start, end, interval)
        return [c for c in self.daily.get(symbol_id, []) if start <= c.start < end]

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        self._called("symbols_by_names", tuple(names))
        return {n: self.symbols[n] for n in names if n in self.symbols}


# --- prompts ------------------------------------------------------------------------------------------------

TEXT_CHOICES = ("a", "w")  # with `needs_text`, these choices need a non-empty text (task plan T10)


class FakePromptStore:
    """An in-memory `PromptStore` with the behaviour task T10 specifies for the database one."""

    def __init__(self, clock: Clock, *, repeat_hours: int = 24) -> None:
        self.clock = clock
        self.repeat_hours = repeat_hours
        self.rows: dict[int, PromptView] = {}
        self.run_ids: dict[int, int] = {}
        self.answered_by: dict[int, str] = {}

    def _put(self, prompt: PromptView) -> PromptView:
        self.rows[prompt.id] = prompt
        return prompt

    def ensure(self, run_id: int, source: str, req: OwnerPromptRequest) -> PromptView:
        existing = self.by_key(req.dedupe_key)
        if existing is not None:
            return existing
        pid = len(self.rows) + 1
        self.run_ids[pid] = run_id
        return self._put(
            PromptView(
                id=pid,
                source=source,
                kind=req.kind,
                scope_key=req.scope_key,
                dedupe_key=req.dedupe_key,
                title=req.title,
                body=req.body,
                choices=req.choices,
                needs_text=req.needs_text,
                default_choice=req.default_choice,
                data=dict(req.data),
                status="pending",
                asked_at=self.clock.now(),
                last_sent_at=None,
                send_count=0,
                answered_at=None,
                answer=None,
                answer_text=None,
                answered_via=None,
                delivered_at=None,
            )
        )

    def get(self, prompt_id: int) -> PromptView | None:
        return self.rows.get(prompt_id)

    def by_key(self, dedupe_key: str) -> PromptView | None:
        return next((p for p in self.rows.values() if p.dedupe_key == dedupe_key), None)

    def pending(self, source: str | None = None) -> list[PromptView]:
        return [
            p for p in self.rows.values() if p.status == "pending" and (source is None or p.source == source)
        ]

    def answer(
        self, prompt_id: int, choice: str, *, text: str | None = None, via: AnsweredVia, actor: str
    ) -> AnswerResult:
        prompt = self.rows.get(prompt_id)
        if prompt is None:
            return AnswerResult("unknown", None)
        if prompt.status != "pending":
            return AnswerResult("already", prompt)
        if choice not in {c.code for c in prompt.choices}:
            return AnswerResult("invalid_choice", prompt)
        if prompt.needs_text and choice in TEXT_CHOICES and not (text or "").strip():
            return AnswerResult("text_required", prompt)
        self.answered_by[prompt_id] = actor
        answered = dataclasses.replace(
            prompt,
            status="answered",
            answered_at=self.clock.now(),
            answer=choice,
            answer_text=text,
            answered_via=via,
        )
        return AnswerResult("ok", self._put(answered))

    def undelivered(self, source: str) -> list[PromptView]:
        return [
            p
            for p in self.rows.values()
            if p.source == source and p.status == "answered" and p.delivered_at is None
        ]

    def mark_delivered(self, prompt_id: int) -> None:
        self._put(dataclasses.replace(self.rows[prompt_id], delivered_at=self.clock.now()))

    def due_for_send(self, now: datetime) -> list[PromptView]:
        repeat = timedelta(hours=self.repeat_hours)
        return [
            p
            for p in self.rows.values()
            if p.status == "pending" and (p.last_sent_at is None or now - p.last_sent_at >= repeat)
        ]

    def mark_sent(self, prompt_id: int, now: datetime) -> None:
        prompt = self.rows[prompt_id]
        self._put(dataclasses.replace(prompt, last_sent_at=now, send_count=prompt.send_count + 1))

    def cancel(self, dedupe_key: str) -> None:
        prompt = self.by_key(dedupe_key)
        if prompt is not None and prompt.status == "pending":
            self._put(dataclasses.replace(prompt, status="cancelled"))


# --- the strategy host, facts, the registry -----------------------------------------------------------------


class RecordingHost:
    """A `StrategyHost` that records every call in `calls` as (method, arguments) and answers from what the
    test set: `due` (for `due_events`), `outcome` (for `fire`), `watch`, `panels`, `action_result`,
    `answers_delivered`, `prompts_synced`. An unknown strategy key raises KeyError in `panel` and `action`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.due: list[tuple[str, OptionEvent]] = []
        self.outcome = JobOutcome("succeeded", {})
        self.watch: set[str] = set()
        self.panels: dict[str, StrategyPanel] = {}
        self.action_result = PanelActionResult(True, "done")
        self.answers_delivered = 0
        self.prompts_synced = 0
        self.fired: list[tuple[str, OptionEvent, bool]] = []
        self.fills: list[OptionFillEvent] = []
        self.lifecycle: list[LifecycleEvent] = []
        self.actions: list[tuple[str, PanelActionRequest, str]] = []

    async def ensure_defaults(self) -> None:
        self.calls.append(("ensure_defaults", ()))

    async def due_events(self, session_date: date, now: datetime) -> list[tuple[str, OptionEvent]]:
        self.calls.append(("due_events", (session_date, now)))
        return list(self.due)

    async def fire(self, strategy_key: str, event: OptionEvent, *, force: bool = False) -> JobOutcome:
        self.calls.append(("fire", (strategy_key, event, force)))
        self.fired.append((strategy_key, event, force))
        self.due = [(k, e) for k, e in self.due if (k, e) != (strategy_key, event)]
        return self.outcome

    async def deliver_fill(self, fill: OptionFillEvent) -> None:
        self.calls.append(("deliver_fill", (fill,)))
        self.fills.append(fill)

    async def deliver_lifecycle(self, event: LifecycleEvent) -> None:
        self.calls.append(("deliver_lifecycle", (event,)))
        self.lifecycle.append(event)

    async def deliver_answers(self) -> int:
        self.calls.append(("deliver_answers", ()))
        return self.answers_delivered

    async def sync_prompts(self) -> int:
        self.calls.append(("sync_prompts", ()))
        return self.prompts_synced

    async def watch_underlyings(self) -> set[str]:
        self.calls.append(("watch_underlyings", ()))
        return set(self.watch)

    async def panel(self, strategy_key: str) -> StrategyPanel:
        self.calls.append(("panel", (strategy_key,)))
        return self.panels[strategy_key]

    async def action(self, strategy_key: str, req: PanelActionRequest, actor: str) -> PanelActionResult:
        self.calls.append(("action", (strategy_key, req, actor)))
        if strategy_key not in self.panels:
            raise KeyError(strategy_key)
        self.actions.append((strategy_key, req, actor))
        return self.action_result


class FakeFacts:
    """A `FactsProvider`: `facts` by ticker; `errors[ticker]` is the error text `refresh` reports for it."""

    def __init__(self, facts: Mapping[str, UnderlyingFacts] | None = None) -> None:
        self.facts: dict[str, UnderlyingFacts] = dict(facts or {})
        self.errors: dict[str, str] = {}
        self.refreshed: list[tuple[str, ...]] = []

    async def get(self, underlying: str) -> UnderlyingFacts | None:
        return self.facts.get(underlying)

    async def refresh(self, underlyings: Sequence[str]) -> dict[str, UnderlyingFacts | str]:
        self.refreshed.append(tuple(underlyings))
        out: dict[str, UnderlyingFacts | str] = {}
        for ticker in underlyings:
            if ticker in self.errors:
                out[ticker] = self.errors[ticker]
            elif ticker in self.facts:
                out[ticker] = self.facts[ticker]
            else:
                out[ticker] = "no facts"
        return out


class FakeRegistry:
    """An in-memory `OptionStrategyRegistryView` over plug-in classes: every key starts at revision 1 with
    its default params, enabled. `update` validates with the plug-in's params model (ValidationError) and
    adds a revision only when something changed."""

    def __init__(self, clock: Clock, *plugins: type[Any]) -> None:
        self.clock = clock
        self.plugins: dict[str, type[Any]] = {p.key: p for p in plugins}
        self.updates: list[tuple[str, dict[str, Any] | None, bool | None, str]] = []
        self._configs: dict[str, OptionStrategyConfigView] = {}
        for n, (key, plugin) in enumerate(sorted(self.plugins.items()), start=1):
            params: BaseModel = plugin.params_model()
            self._configs[key] = OptionStrategyConfigView(
                n, key, plugin.version, 1, params.model_dump(mode="json"), True, clock.now(), "system"
            )

    def keys(self) -> list[str]:
        return sorted(self.plugins)

    def plugin_class(self, key: str) -> type[Any]:
        return self.plugins[key]

    def json_schema(self, key: str) -> dict[str, Any]:
        schema: dict[str, Any] = self.plugins[key].params_model.model_json_schema()
        return schema

    def current(self, key: str) -> OptionStrategyConfigView:
        return self._configs[key]

    def update(
        self, key: str, *, params: dict[str, Any] | None = None, enabled: bool | None = None, actor: str
    ) -> OptionStrategyConfigView:
        current = self._configs[key]
        self.updates.append((key, params, enabled, actor))
        model: BaseModel = self.plugins[key].params_model.model_validate({**current.params, **(params or {})})
        new_params = model.model_dump(mode="json")
        new_enabled = current.enabled if enabled is None else enabled
        if (new_params, new_enabled) == (current.params, current.enabled):
            return current
        updated = dataclasses.replace(
            current,
            id=current.id + 100,
            revision=current.revision + 1,
            params=new_params,
            enabled=new_enabled,
            created_at=self.clock.now(),
            created_by=actor,
        )
        self._configs[key] = updated
        return updated


@dataclass
class OptionFakes:
    """What `make_option_services` built, so a test can reach each fake."""

    market: FakeOptionMarket
    book: FakeBook
    broker: FakeOptionBroker
    prompts: FakePromptStore
    registry: FakeRegistry
    host: RecordingHost
    run: dict[str, int | None] = field(default_factory=dict)


def make_option_services(
    factory: sessionmaker[Session],
    clock: Clock,
    *plugins: type[Any],
    run_id: int | None | Callable[[], int | None] = 1,
    collateral: CollateralEngine | None = None,
) -> tuple[OptionApiServices, OptionFakes]:
    """`OptionApiServices` over the fakes, with the real `OptionSettingsStore` on `factory`. Pass it to
    `tests.fakes_api.make_services(core, options=services)`. `run_id` is the active options run (None: no
    run); change `fakes.run["id"]` later to switch it, or pass a callable."""
    market = FakeOptionMarket(clock)
    broker = FakeOptionBroker(market, collateral=collateral)
    fakes = OptionFakes(
        market=market,
        book=broker.book,
        broker=broker,
        prompts=FakePromptStore(clock),
        registry=FakeRegistry(clock, *plugins),
        host=RecordingHost(),
    )
    if callable(run_id):
        current: Callable[[], int | None] = run_id
    else:
        fakes.run["id"] = run_id

        def current() -> int | None:
            return fakes.run["id"]

    services = OptionApiServices(
        run_id=current,
        broker=broker,
        market=market,
        prompts=fakes.prompts,
        settings=OptionSettingsStore(factory, clock.now),
        registry=fakes.registry,
        host=fakes.host,
    )
    return services, fakes


__all__ = [
    "SOURCE_MANUAL",
    "AcceptAll",
    "CannedCollateral",
    "CashMove",
    "FakeBook",
    "FakeFacts",
    "FakeOptionBroker",
    "FakeOptionMarket",
    "FakePromptStore",
    "FakeQtOptions",
    "FakeRegistry",
    "LifecycleRow",
    "OptionFakes",
    "RecordingHost",
    "accept",
    "make_option_services",
    "reject",
]
