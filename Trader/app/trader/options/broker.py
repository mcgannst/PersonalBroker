"""`SimOptionBroker`: option orders from submit to fill for one options run (OPTSIM task plan T5, feature
plan §3.2 and §3.4). The `OptionBroker` protocol over the database; nothing is kept in memory between calls,
so the worker and the API process can each hold one.

- Every write happens in one transaction that first takes the book lock (`account.lock_book`) and then
  re-reads the order row, so two brokers on one run never fill, cancel or reprice the same order twice.
- Quotes are fetched BEFORE the transaction (one pass per call), never while the lock is held.
- The collateral engine decides at submit and again at fill (the book may have changed in between); a fill
  that no longer passes cancels the order with the engine's reason.
- A working order's `reserved_cash` is the extra cash its fill would take out of free cash: the rise of the
  structure's reserve, plus fees, plus the debit (or minus the credit, which arrives with the fill), never
  below 0. Free cash is therefore the same just before and just after a fill at the order's own net.
- All legs fill together from one quotes pass or none do; there are no partial fills.
"""

import dataclasses
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote
from trader.broker.ledger import Ledger
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.options import valuation
from trader.options.account import lock_book
from trader.options.book import DbBook, load_contracts, q4, symbol_id_of
from trader.options.fill_model import MULTIPLIER_KEY, leg_fee, market_net, mid_net, order_fees
from trader.options.protocols import (
    CollateralBook,
    CollateralEngine,
    OptionFillModel,
    OptionMarketView,
    UnknownUnderlying,
)
from trader.options.settings import OptionSettings, OptionSettingsStore
from trader.options.types import (
    HUNDRED,
    SHARES_PER_CONTRACT,
    ZERO,
    CollateralDecision,
    FillDecision,
    LegFill,
    LegQuote,
    OptionAccountState,
    OptionContract,
    OptionFillEvent,
    OptionQuote,
    OptOrderView,
    OrderLeg,
    OrderRequest,
    OrderStatus,
    RejectReason,
    StructureView,
    SubmitResult,
    net_price,
    round_tick,
)

EVENT_SOURCE = "options.broker"
TAKE_PROFIT_REASON = "take_profit"
SettingsSource = OptionSettings | OptionSettingsStore | Callable[[], OptionSettings]


@dataclass(frozen=True, slots=True)
class _Quotes:
    """One quotes pass: option quotes by contract id, share quotes by ticker."""

    options: dict[int, OptionQuote]
    shares: dict[str, QtQuote]


def _snap(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def take_profit_net(entry_net: Decimal, pct: Decimal | None) -> Decimal | None:
    """The net at which a credit structure is bought back: `-entry x (1 - pct)`; None for a debit."""
    return -entry_net * (1 - pct) if pct is not None and entry_net > 0 else None


def leg_quotes(
    legs: Iterable[OrderLeg], quotes: _Quotes, contracts: dict[int, OptionContract]
) -> dict[int, LegQuote]:
    """A `LegQuote` per leg that has a quote, by `leg_no`. `raw` is the snapshot stored on the fill; for an
    option leg it also carries the contract multiplier (the fill model reads it)."""
    out: dict[int, LegQuote] = {}
    for leg in legs:
        if leg.contract_id is not None:
            q = quotes.options.get(leg.contract_id)
            if q is None:
                continue
            known = contracts.get(leg.contract_id)
            raw = {
                name: _snap(getattr(q, name))
                for name in (
                    "bid",
                    "ask",
                    "last",
                    "bid_size",
                    "ask_size",
                    "volume",
                    "open_interest",
                    "iv",
                    "delta",
                    "fetched_at",
                )
            }
            raw[MULTIPLIER_KEY] = known.multiplier if known is not None else SHARES_PER_CONTRACT
            out[leg.leg_no] = LegQuote(
                leg.leg_no, q.bid, q.ask, q.last, q.fetched_at, q.delay, q.is_halted, raw
            )
        else:
            share = quotes.shares.get(leg.underlying)
            if share is None or share.fetched_at is None:  # never assume a quote is live
                continue
            raw = {name: _snap(getattr(share, name)) for name in ("bid", "ask", "last", "fetched_at")}
            out[leg.leg_no] = LegQuote(
                leg.leg_no,
                share.bid,
                share.ask,
                share.last,
                share.fetched_at,
                share.delay,
                share.is_halted,
                raw,
            )
    return out


def _request(order: OptOrderView) -> OrderRequest:
    return OrderRequest(
        source=order.source,
        strategy_config_id=order.strategy_config_id,
        intent=order.intent,
        structure_id=order.structure_id,
        underlying=order.underlying,
        legs=order.legs,
        qty=order.qty,
        order_type=order.order_type,
        net_limit=order.net_limit,
        tif=order.tif,
        walk=order.walk,
        take_profit_pct=order.take_profit_pct,
        reason=order.reason,
        evidence=order.evidence,
        submitted_by=order.submitted_by,
    )


def _refuse(decision: CollateralDecision, reason: RejectReason, detail: str) -> CollateralDecision:
    return dataclasses.replace(decision, accepted=False, reject_reason=reason, detail=detail)


class SimOptionBroker:
    """`settings` is an `OptionSettings`, an `OptionSettingsStore` (loaded on every call) or a callable.
    `usd_cad_rate` is read once per fill and stored on its rows."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        market: OptionMarketView,
        collateral: CollateralEngine,
        fill_model: OptionFillModel,
        settings: SettingsSource,
        run_id: int,
        usd_cad_rate: Callable[[], Decimal],
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cal = calendar
        self._market = market
        self._collateral = collateral
        self._fill_model = fill_model
        self._settings_source = settings
        self._run_id = run_id
        self._usd_cad_rate = usd_cad_rate
        self._ledger = Ledger(calendar)

    # --- helpers --------------------------------------------------------------------------------------------
    def _settings(self) -> OptionSettings:
        source = self._settings_source
        if isinstance(source, OptionSettings):
            return source
        if isinstance(source, OptionSettingsStore):
            return source.load()
        return source()

    def _book(self, s: Session) -> DbBook:
        return DbBook(s, self._run_id, self._ledger, self._clock)

    def _hours(self, now: datetime) -> tuple[datetime, datetime] | None:
        """The open and close of `now`'s ET date, or None when it is not a session."""
        day = et_date(now)
        try:
            if not self._cal.is_session(day):
                return None
        except ValueError:  # outside the calendar's range
            return None
        return self._cal.session_open(day), self._cal.session_close(day)

    def _order_session(self, now: datetime) -> date:
        """The session an order submitted at `now` belongs to: today's until the close, else the next."""
        day = et_date(now)
        hours = self._hours(now)
        return day if hours is not None and now < hours[1] else self._cal.next_session(day)

    def _log(self, s: Session, now: datetime, message: str, level: str = "info", **data: Any) -> None:
        s.add(
            m.EventLog(
                ts=now, level=level, source=EVENT_SOURCE, run_id=self._run_id, message=message, data=data
            )
        )

    def _order_row(self, s: Session, order_id: int) -> m.OptOrder | None:
        """The order row, locked and read afresh (call after `lock_book`)."""
        return s.execute(
            select(m.OptOrder)
            .where(m.OptOrder.id == order_id, m.OptOrder.run_id == self._run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _order_views(self, s: Session, rows: Sequence[m.OptOrder]) -> list[OptOrderView]:
        if not rows:
            return []
        legs: dict[int, list[m.OptOrderLeg]] = {}
        for leg in s.execute(
            select(m.OptOrderLeg)
            .where(m.OptOrderLeg.order_id.in_([r.id for r in rows]))
            .order_by(m.OptOrderLeg.order_id, m.OptOrderLeg.leg_no)
        ).scalars():
            legs.setdefault(leg.order_id, []).append(leg)
        tickers = {
            sid: ticker
            for sid, ticker in s.execute(
                select(m.Symbol.id, m.Symbol.ticker).where(
                    m.Symbol.id.in_({r.underlying_symbol_id for r in rows})
                )
            )
        }
        return [
            OptOrderView(
                id=r.id,
                source=r.source,
                strategy_config_id=r.strategy_config_id,
                intent=cast(Any, r.intent),
                structure_id=r.structure_id,
                underlying=tickers[r.underlying_symbol_id],
                legs=tuple(
                    OrderLeg(
                        leg_no=leg.leg_no,
                        instrument=cast(Any, leg.instrument),
                        side=cast(Any, leg.side),
                        effect=cast(Any, leg.effect),
                        ratio=leg.ratio,
                        underlying=tickers[r.underlying_symbol_id],
                        contract_id=leg.contract_id,
                    )
                    for leg in legs.get(r.id, [])
                ),
                qty=r.qty,
                order_type=cast(Any, r.order_type),
                net_limit=r.net_limit,
                tif=cast(Any, r.tif),
                walk=r.walk,
                take_profit_pct=r.take_profit_pct,
                reason=r.reason,
                evidence=dict(r.evidence or {}),
                submitted_by=r.submitted_by,
                status=cast(OrderStatus, r.status),
                reject_reason=cast(Any, r.reject_reason),
                reject_detail=r.reject_detail,
                reserved_cash=r.reserved_cash,
                submitted_at=r.submitted_at,
                closed_at=r.closed_at,
                fill_net=r.fill_net,
                fees=r.fees,
            )
            for r in rows
        ]

    def _working(self, s: Session) -> list[OptOrderView]:
        """The run's working orders, in submit order."""
        rows = s.execute(
            select(m.OptOrder)
            .where(m.OptOrder.run_id == self._run_id, m.OptOrder.status == "working")
            .order_by(m.OptOrder.id)
        ).scalars()
        return self._order_views(s, list(rows))

    def _scope(self, s: Session) -> tuple[set[int], set[str]]:
        """What the account's value needs quotes for: the contracts of the open positions, and the
        underlyings of the open structures."""
        open_structures = (m.OptStructure.run_id == self._run_id, m.OptStructure.state == "open")
        ids = s.execute(
            select(m.OptPosition.contract_id)
            .join(m.OptStructure, m.OptStructure.id == m.OptPosition.structure_id)
            .where(*open_structures, m.OptPosition.qty != 0, m.OptPosition.contract_id.is_not(None))
        ).scalars()
        tickers = s.execute(select(m.OptStructure.underlying).where(*open_structures)).scalars()
        return {int(i) for i in ids if i is not None}, set(tickers)

    async def _fetch(self, contract_ids: Iterable[int], tickers: Iterable[str]) -> _Quotes:
        ids = sorted(set(contract_ids))
        options = dict(await self._market.quotes(ids)) if ids else {}
        shares: dict[str, QtQuote] = {}
        for ticker in sorted(set(tickers)):
            try:
                quote = await self._market.underlying_quote(ticker)
            except UnknownUnderlying:
                continue
            if quote is not None:
                shares[ticker] = quote
        return _Quotes(options, shares)

    async def _fetch_for(self, orders: Sequence[OptOrderView | OrderRequest]) -> _Quotes:
        """One quotes pass for these orders' legs and for everything the account's value needs."""
        with self._factory() as s:
            ids, tickers = self._scope(s)
        for order in orders:
            tickers.add(order.underlying)
            ids.update(leg.contract_id for leg in order.legs if leg.contract_id is not None)
        return await self._fetch(ids, tickers)

    def _marks(self, s: Session, contract_ids: Iterable[int]) -> dict[int, OptionQuote]:
        """The last recorded mark of each contract (the fallback when it has no live quote)."""
        ids = sorted(set(contract_ids))
        if not ids:
            return {}
        rows = s.execute(select(m.OptionQuoteMark).where(m.OptionQuoteMark.contract_id.in_(ids))).scalars()
        return {
            r.contract_id: OptionQuote(
                contract_id=r.contract_id,
                bid=r.bid,
                ask=r.ask,
                last=r.last,
                bid_size=r.bid_size,
                ask_size=r.ask_size,
                volume=r.volume,
                open_interest=r.open_interest,
                iv=r.iv,
                delta=r.delta,
                gamma=r.gamma,
                theta=r.theta,
                vega=r.vega,
                last_trade_time=r.last_trade_time,
                delay=r.delay,
                is_halted=r.is_halted,
                fetched_at=r.fetched_at,
            )
            for r in rows
        }

    def _premium(self, s: Session) -> Decimal:
        """Premium collected: the credits of the sell-to-open option fills."""
        total = s.execute(
            select(func.coalesce(func.sum(m.OptFill.price * m.OptFill.qty * m.OptionContract.multiplier), 0))
            .select_from(m.OptFill)
            .join(m.OptOrderLeg, m.OptOrderLeg.id == m.OptFill.leg_id)
            .join(m.OptionContract, m.OptionContract.id == m.OptOrderLeg.contract_id)
            .where(
                m.OptFill.run_id == self._run_id,
                m.OptOrderLeg.instrument == "option",
                m.OptOrderLeg.side == "sell",
                m.OptOrderLeg.effect == "open",
            )
        ).scalar_one()
        return Decimal(total)

    def _account(
        self,
        s: Session,
        book: DbBook,
        structures: Sequence[StructureView],
        quotes: _Quotes,
        now: datetime,
        *,
        less_reserved: Decimal = ZERO,
    ) -> OptionAccountState:
        held = {p.contract.id for st in structures for p in st.positions if p.contract is not None and p.qty}
        return valuation.account_state(
            book.cash(),
            book.reserved() - less_reserved,
            structures,
            quotes.options,
            quotes.shares,
            self._marks(s, held - set(quotes.options)),
            now,
            premium_collected=self._premium(s),
        )

    def _evaluate(
        self,
        s: Session,
        req: OrderRequest,
        quotes: _Quotes,
        settings: OptionSettings,
        now: datetime,
        *,
        exclude: m.OptOrder | None = None,
    ) -> tuple[OrderRequest, CollateralDecision]:
        """Ask the collateral engine about `req` against the book as it is now (call under the lock for a
        write). `exclude` is the order being re-checked: it and its reservation are left out. Returns the
        request as evaluated: a walk order without a limit gets its starting limit, the midpoint net rounded
        up to the tick."""
        book = self._book(s)
        structures = book.structures()
        working = [o for o in self._working(s) if exclude is None or o.id != exclude.id]
        all_legs = [*req.legs, *(leg for o in working for leg in o.legs)]
        ids = {leg.contract_id for leg in all_legs if leg.contract_id is not None}
        ids |= {p.contract.id for st in structures for p in st.positions if p.contract is not None}
        contracts = load_contracts(s, sorted(ids))
        cbook = CollateralBook(
            account=self._account(
                s, book, structures, quotes, now, less_reserved=exclude.reserved_cash if exclude else ZERO
            ),
            structures=structures,
            working_orders=working,
            contracts=contracts,
            quotes=quotes.options,
            share_quotes=quotes.shares,
            settings=settings,
            today=et_date(now),
        )
        start: Decimal | None = None
        needs_start = req.order_type == "limit" and req.walk and req.net_limit is None
        if needs_start:
            mid = mid_net(req.legs, leg_quotes(req.legs, quotes, contracts))
            if mid is not None:
                start = round_tick(mid, settings.tick_size, "up")
                req = dataclasses.replace(req, net_limit=start)
        decision = self._collateral.evaluate(req, cbook)
        if not decision.accepted:
            return req, decision
        # What the database could not store, whatever the engine says.
        if needs_start and start is None:
            return req, _refuse(decision, "no_quote", "a walk order needs a bid and an ask on every leg")
        unknown = sorted(
            leg.contract_id
            for leg in req.legs
            if leg.contract_id is not None and leg.contract_id not in contracts
        )
        if unknown:
            return req, _refuse(decision, "unknown_contract", f"unknown contract {unknown[0]}")
        if req.intent != "open" and not any(st.id == req.structure_id for st in structures):
            return req, _refuse(decision, "invalid_order", f"no open structure {req.structure_id}")
        return req, decision

    def _reservation(
        self,
        s: Session,
        req: OrderRequest,
        decision: CollateralDecision,
        quotes: _Quotes,
        settings: OptionSettings,
    ) -> Decimal:
        """The extra cash a fill of `req` at its own net would take out of free cash (see the module text)."""
        if req.order_type == "limit" and req.net_limit is not None:
            net = req.net_limit
        else:
            contracts = load_contracts(
                s, [leg.contract_id for leg in req.legs if leg.contract_id is not None]
            )
            at_market = market_net(req.legs, leg_quotes(req.legs, quotes, contracts))
            net = at_market if at_market is not None else (decision.net_at_market or ZERO)
        held = ZERO
        if req.intent != "open" and req.structure_id is not None:
            held = self._book(s).structure(req.structure_id).reserved_cash
        extra = (
            decision.reserve_cash - held + order_fees(req.legs, req.qty, settings) - net * HUNDRED * req.qty
        )
        return max(ZERO, q4(extra))

    def _store(
        self,
        s: Session,
        req: OrderRequest,
        decision: CollateralDecision,
        quotes: _Quotes,
        settings: OptionSettings,
        now: datetime,
    ) -> OptOrderView:
        """Write the order (working, or rejected with the reason) and its legs."""
        symbol_id = symbol_id_of(s, req.underlying)
        accepted = decision.accepted
        walking = accepted and req.walk and req.order_type == "limit"
        known = load_contracts(s, [leg.contract_id for leg in req.legs if leg.contract_id is not None])
        structure_id = req.structure_id
        if structure_id is not None and s.get(m.OptStructure, structure_id) is None:
            structure_id = None  # a rejected order may name a structure that does not exist
        row = m.OptOrder(
            run_id=self._run_id,
            source=req.source,
            strategy_config_id=req.strategy_config_id,
            intent=req.intent,
            structure_id=structure_id,
            underlying_symbol_id=symbol_id,
            order_type=req.order_type,
            net_limit=None if req.net_limit is None else q4(req.net_limit),
            tif=req.tif,
            qty=req.qty,
            status="working" if accepted else "rejected",
            walk=walking,
            walk_next_at=now + timedelta(seconds=settings.reprice_seconds) if walking else None,
            take_profit_pct=req.take_profit_pct,
            reject_reason=None if accepted else decision.reject_reason,
            reject_detail=None if accepted else decision.detail,
            reason=req.reason,
            evidence=dict(req.evidence),
            reserved_cash=self._reservation(s, req, decision, quotes, settings) if accepted else ZERO,
            session_date=self._order_session(now),
            submitted_at=now,
            submitted_by=req.submitted_by,
            updated_at=now,
            closed_at=None if accepted else now,
        )
        s.add(row)
        s.flush()
        for leg in req.legs:
            if leg.contract_id is not None and leg.contract_id not in known:
                continue  # only on a rejected order: the leg row could not point at its contract
            s.add(
                m.OptOrderLeg(
                    order_id=row.id,
                    leg_no=leg.leg_no,
                    instrument=leg.instrument,
                    contract_id=leg.contract_id,
                    underlying_symbol_id=symbol_id,
                    side=leg.side,
                    effect=leg.effect,
                    ratio=leg.ratio,
                )
            )
        s.flush()
        view = self._order_views(s, [row])[0]
        return dataclasses.replace(view, legs=req.legs)

    # --- OptionBroker: orders -------------------------------------------------------------------------------
    async def preview(self, req: OrderRequest) -> CollateralDecision:
        quotes = await self._fetch_for([req])
        with self._factory() as s:
            return self._evaluate(s, req, quotes, self._settings(), self._clock.now())[1]

    async def submit(self, req: OrderRequest) -> SubmitResult:
        """Store the order as `working` or `rejected`. Raises `UnknownUnderlying` (nothing stored) when the
        underlying is not a known symbol."""
        settings, now = self._settings(), self._clock.now()
        quotes = await self._fetch_for([req])
        with session_scope(self._factory) as s:
            lock_book(s, self._run_id)
            evaluated, decision = self._evaluate(s, req, quotes, settings, now)
            return SubmitResult(self._store(s, evaluated, decision, quotes, settings, now), decision)

    async def cancel(self, order_id: int, reason: str, actor: str) -> bool:
        now = self._clock.now()
        with session_scope(self._factory) as s:
            lock_book(s, self._run_id)
            row = self._order_row(s, order_id)
            if row is None or row.status != "working":
                return False
            row.status, row.closed_at, row.updated_at, row.reserved_cash = "cancelled", now, now, ZERO
            self._log(s, now, "option order cancelled", order_id=order_id, reason=reason, actor=actor)
            return True

    async def reprice(self, order_id: int, net_limit: Decimal, actor: str) -> bool:
        """Give a working order a new limit (a market order becomes a limit order) and stop its walk. False
        when the order is not working, or when the collateral engine refuses it at the new limit."""
        if not isinstance(net_limit, Decimal):
            raise TypeError(f"net_limit must be a Decimal, not {type(net_limit).__name__}")
        settings, now = self._settings(), self._clock.now()
        current = await self.order(order_id)
        if current is None or current.status != "working":
            return False
        quotes = await self._fetch_for([current])
        with session_scope(self._factory) as s:
            lock_book(s, self._run_id)
            row = self._order_row(s, order_id)
            if row is None or row.status != "working":
                return False
            order = self._order_views(s, [row])[0]
            req = dataclasses.replace(_request(order), order_type="limit", net_limit=net_limit, walk=False)
            _, decision = self._evaluate(s, req, quotes, settings, now, exclude=row)
            if not decision.accepted:
                return False
            before = row.net_limit
            row.order_type, row.net_limit, row.walk, row.walk_next_at = "limit", q4(net_limit), False, None
            row.reserved_cash, row.updated_at = self._reservation(s, req, decision, quotes, settings), now
            self._log(
                s,
                now,
                "option order repriced",
                order_id=order_id,
                actor=actor,
                before=_snap(before),
                after=_snap(row.net_limit),
            )
            return True

    async def poll(self, now: datetime) -> list[OptionFillEvent]:
        """Fill every working order the quotes allow, in submit order. One quotes pass for all of them."""
        hours = self._hours(now)
        if hours is None or not hours[0] <= now < hours[1]:
            return []  # the fill model would answer `outside_hours` for every order
        with self._factory() as s:
            orders = self._working(s)
        if not orders:
            return []
        settings = self._settings()
        quotes = await self._fetch_for(orders)
        events: list[OptionFillEvent] = []
        for order in orders:
            event = self._fill(order.id, quotes, settings, now, hours)
            if event is not None:
                events.append(event)
        return events

    def _fill(
        self,
        order_id: int,
        quotes: _Quotes,
        settings: OptionSettings,
        now: datetime,
        hours: tuple[datetime, datetime],
    ) -> OptionFillEvent | None:
        """One order, one transaction: everything a fill writes, or nothing."""
        with session_scope(self._factory) as s:
            lock_book(s, self._run_id)
            row = self._order_row(s, order_id)
            if row is None or row.status != "working":
                return None  # filled, cancelled or expired since it was read
            order = self._order_views(s, [row])[0]
            contracts = load_contracts(
                s, [leg.contract_id for leg in order.legs if leg.contract_id is not None]
            )
            priced = leg_quotes(order.legs, quotes, contracts)
            assessed = self._fill_model.assess(order, priced, now, hours[0], hours[1], settings)
            if not isinstance(assessed, FillDecision):
                return None
            _, decision = self._evaluate(s, _request(order), quotes, settings, now, exclude=row)
            if not decision.accepted:
                row.status, row.closed_at, row.updated_at, row.reserved_cash = "cancelled", now, now, ZERO
                row.reject_reason, row.reject_detail = decision.reject_reason, decision.detail
                self._log(
                    s,
                    now,
                    "option order cancelled at fill: the collateral check failed",
                    level="warning",
                    order_id=order_id,
                    reject_reason=decision.reject_reason,
                    detail=decision.detail,
                )
                return None
            return self._write_fill(s, row, order, assessed, decision, priced, contracts, settings, now)

    def _write_fill(
        self,
        s: Session,
        row: m.OptOrder,
        order: OptOrderView,
        fill: FillDecision,
        decision: CollateralDecision,
        priced: dict[int, LegQuote],
        contracts: dict[int, OptionContract],
        settings: OptionSettings,
        now: datetime,
    ) -> OptionFillEvent:
        book = self._book(s)
        prices = dict(fill.leg_prices)
        net = fill.net_price
        by_no = sorted(order.legs, key=lambda leg: leg.leg_no)
        closing = [leg for leg in by_no if leg.effect == "close"]
        opening = [leg for leg in by_no if leg.effect == "open"]
        multipliers = {
            leg.leg_no: contracts[leg.contract_id].multiplier for leg in by_no if leg.contract_id is not None
        }
        leg_ids = {
            leg_no: leg_id
            for leg_no, leg_id in s.execute(
                select(m.OptOrderLeg.leg_no, m.OptOrderLeg.id).where(m.OptOrderLeg.order_id == order.id)
            )
        }
        if order.intent == "open":
            target = book.add_structure(
                kind=decision.kind,
                source=order.source,
                strategy_config_id=order.strategy_config_id,
                underlying=order.underlying,
                qty=order.qty,
                entry_net=net,
                reserved_cash=decision.reserve_cash,
                cover_structure_id=decision.cover_structure_id,
                parent_structure_id=None,
                take_profit_net=take_profit_net(net, order.take_profit_pct),
                meta={"order_id": order.id},
                ts=now,
            )
        elif order.structure_id is not None:
            target = order.structure_id
        else:
            raise ValueError(f"order {order.id} is a {order.intent} and names no structure")
        before = book.structure(target).realized_pnl
        rate = self._usd_cad_rate()
        fees, fee_ref, fills = ZERO, "", []
        for leg in [*closing, *opening]:  # closes first: a roll never holds both sides at once
            units = leg.ratio * order.qty
            price = prices[leg.leg_no]
            fee = leg_fee(leg, order.qty, settings)
            fill_row = m.OptFill(
                run_id=self._run_id,
                order_id=order.id,
                leg_id=leg_ids[leg.leg_no],
                structure_id=target,
                ts=now,
                side=leg.side,
                qty=units,
                price=q4(price),
                fee=q4(fee),
                quote=dict(priced[leg.leg_no].raw),
                usd_cad_rate=rate,
            )
            s.add(fill_row)
            s.flush()
            ref = f"opt_fill:{fill_row.id}"
            fee_ref = fee_ref or ref
            book.apply(
                target, leg.instrument, leg.contract_id, units if leg.side == "buy" else -units, price, now
            )
            amount = price * units * multipliers.get(leg.leg_no, 1)
            if amount != 0:
                book.move_cash(
                    amount if leg.side == "sell" else -amount, leg.side, ref, now, structure_id=target
                )
            fees += fee
            fills.append(
                LegFill(
                    leg_no=leg.leg_no,
                    instrument=leg.instrument,
                    contract_id=leg.contract_id,
                    side=leg.side,
                    effect=leg.effect,
                    qty=units,
                    price=price,
                    fee=fee,
                    quote=dict(priced[leg.leg_no].raw),
                )
            )
        if fees > 0:
            book.move_cash(-fees, "fee", fee_ref, now, structure_id=target)
        view = book.structure(target)
        if all(p.qty == 0 for p in view.positions):
            book.close_structure(target, "sold" if view.kind == "shares" else "closed", now)
        elif order.intent != "open":
            book.set_reserved(target, decision.reserve_cash)
            if order.intent == "roll" and opening:
                entry = net_price(opening, prices, multipliers)
                book.set_entry(target, entry, take_profit_net(entry, order.take_profit_pct))
        row.status, row.closed_at, row.updated_at, row.reserved_cash = "filled", now, now, ZERO
        row.fill_net, row.fees, row.structure_id = q4(net), q4(fees), target
        return OptionFillEvent(
            order_id=order.id,
            structure_id=target,
            source=order.source,
            strategy_config_id=order.strategy_config_id,
            intent=order.intent,
            ts=now,
            qty=order.qty,
            net_price=net,
            fees=fees,
            legs=tuple(sorted(fills, key=lambda f: f.leg_no)),
            realized_pnl=(view.realized_pnl - before) if closing else None,
        )

    async def walk(self, now: datetime) -> int:
        """Move each due walk order one tick toward the market net, never past it. Returns how many moved."""
        settings = self._settings()
        with self._factory() as s:
            due = [o for o in self._working(s) if o.walk and o.order_type == "limit"]
        if not due:
            return 0
        quotes = await self._fetch_for(due)
        moved = 0
        for candidate in due:
            with session_scope(self._factory) as s:
                lock_book(s, self._run_id)
                row = self._order_row(s, candidate.id)
                if (
                    row is None
                    or row.status != "working"
                    or not row.walk
                    or row.net_limit is None
                    or row.walk_next_at is None
                    or row.walk_next_at > now
                ):
                    continue
                order = self._order_views(s, [row])[0]
                contracts = load_contracts(
                    s, [leg.contract_id for leg in order.legs if leg.contract_id is not None]
                )
                at_market = market_net(order.legs, leg_quotes(order.legs, quotes, contracts))
                if at_market is None:
                    continue  # no market to walk toward: try again on the next pass
                row.walk_next_at, row.updated_at = now + timedelta(seconds=settings.reprice_seconds), now
                if row.net_limit <= at_market:
                    continue  # already at the market: the next poll fills it
                limit = max(row.net_limit - settings.tick_size, at_market)
                req = dataclasses.replace(_request(order), net_limit=limit)
                _, decision = self._evaluate(s, req, quotes, settings, now, exclude=row)
                if not decision.accepted:
                    continue  # the account can't carry the next step: the order rests where it is
                row.net_limit = q4(limit)
                row.reserved_cash = self._reservation(s, req, decision, quotes, settings)
                moved += 1
        return moved

    async def take_profits(self, now: datetime) -> list[int]:
        """For each open structure with a take-profit whose net to close at the market is at or better than
        it, and with no working order of its own, submit one closing limit order at the take-profit net.
        Returns the ids of the orders submitted."""
        settings = self._settings()
        with self._factory() as s:
            candidates = [
                st for st in self._book(s).structures() if st.take_profit_net is not None and not st.frozen
            ]
        if not candidates:
            return []
        ids = {p.contract.id for st in candidates for p in st.positions if p.contract is not None and p.qty}
        with self._factory() as s:
            scope_ids, tickers = self._scope(s)
        quotes = await self._fetch(ids | scope_ids, tickers)
        submitted: list[int] = []
        for candidate in candidates:
            at_market = valuation.close_net(candidate, quotes.options)
            if (
                at_market is None
                or candidate.take_profit_net is None
                or at_market < candidate.take_profit_net
            ):
                continue
            with session_scope(self._factory) as s:
                lock_book(s, self._run_id)
                st = self._book(s).structure(candidate.id)
                busy = s.execute(
                    select(func.count())
                    .select_from(m.OptOrder)
                    .where(
                        m.OptOrder.run_id == self._run_id,
                        m.OptOrder.status == "working",
                        m.OptOrder.structure_id == st.id,
                    )
                ).scalar_one()
                units = valuation.structure_units(st)
                if st.state != "open" or st.take_profit_net is None or busy or units == 0:
                    continue
                held = [p for p in st.positions if p.qty != 0 and p.contract is not None]
                req = OrderRequest(
                    source=st.source,
                    strategy_config_id=st.strategy_config_id,
                    intent="close",
                    structure_id=st.id,
                    underlying=st.underlying,
                    legs=tuple(
                        OrderLeg(
                            leg_no=n,
                            instrument="option",
                            side="buy" if p.qty < 0 else "sell",
                            effect="close",
                            ratio=abs(p.qty) // units,
                            underlying=st.underlying,
                            contract_id=p.contract.id if p.contract is not None else None,
                        )
                        for n, p in enumerate(held, start=1)
                    ),
                    qty=units,
                    order_type="limit",
                    net_limit=st.take_profit_net,
                    tif="day",
                    walk=False,
                    take_profit_pct=None,
                    reason=TAKE_PROFIT_REASON,
                    evidence={"close_net": str(at_market), "take_profit_net": str(st.take_profit_net)},
                    submitted_by=TAKE_PROFIT_REASON,
                )
                evaluated, decision = self._evaluate(s, req, quotes, settings, now)
                if decision.accepted:  # a refused take-profit is not stored: it would repeat on every pass
                    submitted.append(self._store(s, evaluated, decision, quotes, settings, now).id)
        return submitted

    async def expire_day_orders(self, session_date: date, now: datetime) -> int:
        """`day` orders of that session (or an earlier one) become `expired`; `gtc` orders stay."""
        with session_scope(self._factory) as s:
            lock_book(s, self._run_id)
            rows = list(
                s.execute(
                    select(m.OptOrder)
                    .where(
                        m.OptOrder.run_id == self._run_id,
                        m.OptOrder.status == "working",
                        m.OptOrder.tif == "day",
                        m.OptOrder.session_date <= session_date,
                    )
                    .with_for_update()
                ).scalars()
            )
            for row in rows:
                row.status, row.closed_at, row.updated_at, row.reserved_cash = "expired", now, now, ZERO
            return len(rows)

    # --- OptionBroker: reads --------------------------------------------------------------------------------
    async def account(self) -> OptionAccountState:
        now = self._clock.now()
        quotes = await self._fetch_for([])
        with self._factory() as s:
            book = self._book(s)
            return self._account(s, book, book.structures(), quotes, now)

    async def structures(self, *, source: str | None = None, open_only: bool = True) -> list[StructureView]:
        with self._factory() as s:
            return self._book(s).structures(open_only=open_only, source=source)

    async def orders(
        self, *, status: OrderStatus | None = None, source: str | None = None, limit: int = 200
    ) -> list[OptOrderView]:
        """Newest first."""
        stmt = select(m.OptOrder).where(m.OptOrder.run_id == self._run_id)
        if status is not None:
            stmt = stmt.where(m.OptOrder.status == status)
        if source is not None:
            stmt = stmt.where(m.OptOrder.source == source)
        with self._factory() as s:
            rows = s.execute(stmt.order_by(m.OptOrder.id.desc()).limit(limit)).scalars()
            return self._order_views(s, list(rows))

    async def order(self, order_id: int) -> OptOrderView | None:
        with self._factory() as s:
            row = s.execute(
                select(m.OptOrder).where(m.OptOrder.id == order_id, m.OptOrder.run_id == self._run_id)
            ).scalar_one_or_none()
            return None if row is None else self._order_views(s, [row])[0]
