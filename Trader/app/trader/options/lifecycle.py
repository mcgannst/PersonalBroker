"""What happens to option positions at expiry, on early assignment and on a contract adjustment (OPTSIM task
plan T6; feature plan §3.5, defaults P5, P6, P8). Everything is written through `OptionBook`.

The rules, in plain words:

- EXPIRY (`run_expiry`): every open option position whose expiry is on or before the session date is
  resolved from the underlying's official close ON ITS EXPIRY DATE. Less than `options.itm_threshold`
  through the strike: it expires at 0. Otherwise a long option is exercised and a short one assigned: 100
  shares per contract change hands at the strike. A short put leaves the shares in a NEW `shares` structure
  (same source and config, `parent_structure_id` = the put's structure). A short call takes the shares from
  its own structure (a buy-write) and then from its cover structure.
- Several legs of one structure in the money are settled together: shares bought and sold the same night
  pass through the structure's own shares position, so a fully in-the-money vertical ends with cash only.
- CASH SETTLEMENT (risk R7): a leg whose exercise or assignment would leave short shares, or that free cash
  cannot pay for, is closed at its intrinsic value against the official close (`detail.settled` is
  `intrinsic`) and no shares move.
- EARLY ASSIGNMENT (`run_early_assignment`, P5): a short call covered by shares, in the money at the close
  of the session before the ex-dividend date, with less time value than the dividend, is called away.
- ADJUSTMENTS (`check_adjustments`, P8): a held contract that is adjusted, or gone from the fresh chain
  before its expiry, freezes its structure. A frozen structure is skipped by everything here.
- No official close: nothing changes for that structure; one `error` row in `event_log`; the position is
  picked up again by the next run. `missing_closes` lists what was left.

Each structure is handled in its own transaction under the book lock. `record_lifecycle` is called before
anything is changed for a position, and when it answers None (the event exists) nothing is done for it.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from trader.db.session import session_scope
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.options.account import lock_book
from trader.options.protocols import OptionBook, OptionMarketView, UnknownContract, UnknownUnderlying
from trader.options.settings import OptionSettings
from trader.options.types import (
    ZERO,
    CloseReason,
    LifecycleEvent,
    LifecycleKind,
    OptionContract,
    OptPositionView,
    StructureView,
    contract_label,
)

EVENT_SOURCE = "options.lifecycle"  # event_log.source of the error and alert rows written here
Settlement = Literal["expired", "shares", "intrinsic"]  # `detail.settled` of an event
TWO = Decimal(2)


@dataclass(frozen=True, slots=True)
class MissingClose:
    """An official close that could not be had: these structures were left unchanged."""

    underlying: str
    close_date: date
    structure_ids: tuple[int, ...]


@dataclass(slots=True)
class _Leg:
    """One option position being resolved, and how."""

    pos: OptPositionView
    contract: OptionContract
    close: Decimal
    close_date: date
    settled: Settlement
    early: bool = False

    @property
    def contracts(self) -> int:
        return abs(self.pos.qty)

    @property
    def shares(self) -> int:
        return self.contracts * self.contract.multiplier

    @property
    def long(self) -> bool:
        return self.pos.qty > 0

    @property
    def buys(self) -> bool:
        """Its exercise or assignment brings shares in: a short put or a long call."""
        return self.long == (self.contract.right == "call")

    @property
    def intrinsic(self) -> Decimal:
        return max(moneyness(self.contract, self.close), ZERO)

    def cash(self, fee: Decimal) -> Decimal:
        """The cash this leg moves (positive = received), the assignment fee included."""
        if self.settled == "expired":
            return ZERO
        if self.settled == "shares":
            amount = self.contract.strike * self.shares
            return (-amount if self.buys else amount) - fee
        amount = self.intrinsic * self.shares
        return (amount if self.long else -amount) - fee


def moneyness(contract: OptionContract, close: Decimal) -> Decimal:
    """How far the close is through the strike; positive is in the money."""
    return close - contract.strike if contract.right == "call" else contract.strike - close


def _shares_held(structure: StructureView | None) -> int:
    if structure is None:
        return 0
    return sum(p.qty for p in structure.positions if p.instrument == "shares")


def _choose_settlement(legs: Sequence[_Leg], held: int, free_cash: Decimal, fee: Decimal) -> None:
    """Turn share settlement into cash settlement, one leg at a time, until no shares go short and free
    cash covers the night's net cash (risk R7). Shares bought the same night count as deliverable."""
    while True:
        live = [leg for leg in legs if leg.settled == "shares"]
        buys = [leg for leg in live if leg.buys]
        sells = [leg for leg in live if not leg.buys]
        if held + sum(leg.shares for leg in buys) - sum(leg.shares for leg in sells) < 0:
            sells[-1].settled = "intrinsic"
        elif buys and free_cash + sum((leg.cash(fee) for leg in legs), ZERO) < 0:
            buys[-1].settled = "intrinsic"
        else:
            return


def _kind(leg: _Leg, held_before: int) -> LifecycleKind:
    if leg.settled == "expired":
        return "expired"
    if leg.early:
        return "early_assignment"
    if leg.long:
        return "exercised"
    if leg.contract.right == "call" and leg.settled == "shares" and held_before > 0:
        return "called_away"
    return "assigned"


def _close_reason(kinds: Sequence[LifecycleKind]) -> CloseReason:
    if "called_away" in kinds or "early_assignment" in kinds:
        return "called_away"
    if "assigned" in kinds:
        return "assigned"
    return "exercised" if "exercised" in kinds else "expired"


class LifecycleEngine:
    """Expiry, exercise, assignment, early assignment and adjustment freezes for one options run.

    `official_close(underlying, day)` answers the underlying's official close of that session, or None when
    it is not known yet. `book_factory(session)` gives the book of one transaction. `missing_closes` holds
    the closes that could not be had: `run_expiry` starts it afresh, `run_early_assignment` adds to it.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        market: OptionMarketView,
        settings: OptionSettings,
        run_id: int,
        book_factory: Callable[[Session], OptionBook],
        official_close: Callable[[str, date], Awaitable[Decimal | None]],
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._calendar = calendar
        self._market = market
        self._settings = settings
        self._run_id = run_id
        self._book_factory = book_factory
        self._official_close = official_close
        self.missing_closes: list[MissingClose] = []

    # --- expiry ---------------------------------------------------------------------------------------------

    async def run_expiry(self, session_date: date) -> list[LifecycleEvent]:
        """Resolve every open option position with expiry <= `session_date` (so a missed day is caught up).
        Safe to repeat: a second run finds nothing left to do."""
        self.missing_closes = []
        needed: dict[int, set[tuple[str, date]]] = {}
        for st in self._open_structures():
            keys = {(st.underlying, self._close_date(c.expiry)) for _, c in _expiring(st, session_date)}
            if keys and not st.frozen:
                needed[st.id] = keys
        closes: dict[tuple[str, date], Decimal | None] = {}
        for key in sorted({key for keys in needed.values() for key in keys}):
            closes[key] = await self._official_close(*key)
        missing: dict[tuple[str, date], list[int]] = {}
        events: list[LifecycleEvent] = []
        for sid in sorted(needed):
            absent = [key for key in sorted(needed[sid]) if closes[key] is None]
            if absent:
                for key in absent:
                    missing.setdefault(key, []).append(sid)
                continue
            events += self._expire_structure(sid, session_date, closes)
        self._report_missing(missing)
        return events

    def _expire_structure(
        self, structure_id: int, session_date: date, closes: dict[tuple[str, date], Decimal | None]
    ) -> list[LifecycleEvent]:
        with session_scope(self._factory) as s:
            lock_book(s, self._run_id)
            book = self._book_factory(s)
            st = book.structure(structure_id)
            if st.state != "open" or st.frozen:
                return []
            legs: list[_Leg] = []
            for pos, contract in _expiring(st, session_date):
                close_date = self._close_date(contract.expiry)
                close = closes.get((st.underlying, close_date))
                if close is None:  # a position that appeared after the closes were read: the next run
                    return []
                itm = moneyness(contract, close) >= self._settings.itm_threshold
                legs.append(_Leg(pos, contract, close, close_date, "shares" if itm else "expired"))
            return self._settle(book, st, legs, session_date, self._clock.now())

    # --- early assignment -----------------------------------------------------------------------------------

    async def run_early_assignment(self, session_date: date) -> list[LifecycleEvent]:
        """P5, run after expiry on `session_date`: call away every share-covered short call whose
        underlying goes ex-dividend on the next session, that is in the money at this session's close and
        has less time value left (the last quote's midpoint minus intrinsic; no quote counts as 0) than the
        dividend. Missing facts: no action."""
        if not self._settings.early_assignment_enabled:
            return []
        ex_day = self._calendar.next_session(session_date)
        structures = {st.id: st for st in self._open_structures()}
        picks: list[tuple[int, int, Decimal]] = []  # structure id, position id, the close
        missing: dict[tuple[str, date], list[int]] = {}
        closes: dict[str, Decimal | None] = {}
        for st in structures.values():
            if st.frozen:
                continue
            cover = structures.get(st.cover_structure_id) if st.cover_structure_id is not None else None
            for pos in st.positions:
                c = pos.contract
                if c is None or pos.qty >= 0 or c.right != "call" or c.expiry <= session_date:
                    continue
                if _shares_held(st) + _shares_held(cover) < abs(pos.qty) * c.multiplier:
                    continue
                facts = await self._market.facts(st.underlying)
                if facts is None or facts.next_ex_dividend_date != ex_day or facts.dividend_per_share is None:
                    continue
                if st.underlying not in closes:
                    closes[st.underlying] = await self._official_close(st.underlying, session_date)
                close = closes[st.underlying]
                if close is None:
                    missing.setdefault((st.underlying, session_date), []).append(st.id)
                    continue
                through = moneyness(c, close)
                if through < self._settings.itm_threshold:
                    continue
                quote = (await self._market.quotes([c.id])).get(c.id)
                time_value = ZERO
                if quote is not None and quote.bid is not None and quote.ask is not None:
                    time_value = (quote.bid + quote.ask) / TWO - through
                if time_value < facts.dividend_per_share:
                    picks.append((st.id, pos.id, close))
        events: list[LifecycleEvent] = []
        for structure_id, position_id, close in picks:
            events += self._assign_early(structure_id, position_id, close, session_date)
        self._report_missing(missing)
        return events

    def _assign_early(
        self, structure_id: int, position_id: int, close: Decimal, session_date: date
    ) -> list[LifecycleEvent]:
        with session_scope(self._factory) as s:
            lock_book(s, self._run_id)
            book = self._book_factory(s)
            st = book.structure(structure_id)
            pos = next((p for p in st.positions if p.id == position_id and p.qty < 0), None)
            if st.state != "open" or st.frozen or pos is None or pos.contract is None:
                return []
            leg = _Leg(pos, pos.contract, close, session_date, "shares", early=True)
            if _shares_held(st) + _shares_held(self._cover(book, st)) < leg.shares:
                return []
            return self._settle(book, st, [leg], session_date, self._clock.now())

    # --- adjustments ----------------------------------------------------------------------------------------

    async def check_adjustments(self) -> list[LifecycleEvent]:
        """P8: freeze every open structure that holds an adjusted contract, or one that is no longer in the
        underlying's chain before its expiry. One `frozen` event and one `warning` row in `event_log` (the
        alert) per structure; a frozen structure is not looked at again. An underlying with no chain at all
        (nothing fetched) freezes nothing."""
        today = et_date(self._clock.now())
        found: list[tuple[int, int, str]] = []  # structure id, position id, why
        for st in self._open_structures():
            if st.frozen:
                continue
            for pos in st.positions:
                if pos.contract is None or pos.qty == 0:
                    continue
                why = await self._adjustment(pos.contract, today)
                if why is not None:
                    found.append((st.id, pos.id, why))
                    break
        events: list[LifecycleEvent] = []
        for structure_id, position_id, why in found:
            events += self._freeze(structure_id, position_id, why, today)
        return events

    async def _adjustment(self, held: OptionContract, today: date) -> str | None:
        try:
            fresh = await self._market.contract(held.id)
        except UnknownContract:
            fresh = held
        if held.adjusted or fresh.adjusted:
            return "the contract was adjusted"
        if held.expiry < today:
            return None
        try:
            if not await self._market.expiries(held.underlying):
                return None
            strikes = await self._market.strikes(held.underlying, held.expiry)
        except UnknownUnderlying:
            return None
        listed = {s.call_id for s in strikes} | {s.put_id for s in strikes}
        return None if held.id in listed else "the contract is no longer in the chain"

    def _freeze(self, structure_id: int, position_id: int, why: str, today: date) -> list[LifecycleEvent]:
        with session_scope(self._factory) as s:
            lock_book(s, self._run_id)
            book = self._book_factory(s)
            st = book.structure(structure_id)
            pos = next((p for p in st.positions if p.id == position_id), None)
            if st.state != "open" or st.frozen or pos is None or pos.contract is None:
                return []
            contract, ts = pos.contract, self._clock.now()
            event_id = book.record_lifecycle(
                structure_id=st.id,
                position_id=pos.id,
                contract_id=contract.id,
                kind="frozen",
                session_date=today,
                ts=ts,
                underlying_close=None,
                strike=contract.strike,
                qty=abs(pos.qty),
                shares_delta=0,
                cash_delta=ZERO,
                detail={"reason": why},
            )
            if event_id is None:
                return []
            label = contract_label(contract)
            book.freeze(st.id, f"{label}: {why}")
            log_event(
                s,
                self._clock,
                "warning",
                EVENT_SOURCE,
                f"Structure {st.id} ({st.underlying}) is frozen: {label}, {why}. Resolve it by hand.",
                {"structure_id": st.id, "contract_id": contract.id, "source": st.source, "reason": why},
                self._run_id,
            )
            return [self._event(event_id, st, "frozen", today, ts, contract, abs(pos.qty), None, 0, ZERO)]

    # --- the shared settlement ------------------------------------------------------------------------------

    def _settle(
        self, book: OptionBook, st: StructureView, legs: list[_Leg], session_date: date, ts: datetime
    ) -> list[LifecycleEvent]:
        """Resolve these legs of one structure inside the caller's transaction."""
        fee = self._settings.assignment_fee
        cover = self._cover(book, st)
        own = _shares_held(st)
        held_before = own + _shares_held(cover)
        going = {leg.pos.id for leg in legs}
        shorts_stay = any(p.instrument == "option" and p.qty < 0 and p.id not in going for p in st.positions)
        free_cash = book.cash() - book.reserved() + (ZERO if shorts_stay else st.reserved_cash)
        _choose_settlement(legs, held_before, free_cash, fee)
        selling = any(leg.settled == "shares" and not leg.buys for leg in legs)

        def rank(leg: _Leg) -> int:  # expiries, then shares in, then shares out, then cash settlements
            if leg.settled == "shares":
                return 1 if leg.buys else 2
            return 0 if leg.settled == "expired" else 3

        events: list[LifecycleEvent] = []
        cover_used = False
        for leg in sorted(legs, key=rank):
            c = leg.contract
            kind = _kind(leg, held_before)
            charged = ZERO if leg.settled == "expired" else fee
            shares_delta = 0 if leg.settled != "shares" else (leg.shares if leg.buys else -leg.shares)
            cash_delta = leg.cash(fee)
            event_id = book.record_lifecycle(
                structure_id=st.id,
                position_id=leg.pos.id,
                contract_id=c.id,
                kind=kind,
                session_date=session_date,
                ts=ts,
                underlying_close=leg.close,
                strike=c.strike,
                qty=leg.contracts,
                shares_delta=shares_delta,
                cash_delta=cash_delta,
                detail={
                    "settled": leg.settled,
                    "close_date": leg.close_date.isoformat(),
                    "right": c.right,
                    "position_qty": leg.pos.qty,
                    "fee": str(charged),
                },
            )
            if event_id is None:  # already handled: nothing else is done for this position
                continue
            ref = f"opt_life:{event_id}"
            new_structure_id: int | None = None
            price = leg.intrinsic if leg.settled == "intrinsic" else ZERO
            book.apply(st.id, "option", c.id, -leg.pos.qty, price, ts)
            if leg.settled == "intrinsic":
                amount = leg.intrinsic * leg.shares
                if amount != 0 and leg.long:
                    book.move_cash(amount, "sell", ref, ts, structure_id=st.id)
                elif amount != 0:
                    book.move_cash(-amount, "buy", ref, ts, structure_id=st.id)
            elif leg.settled == "shares" and leg.buys:
                book.move_cash(-c.strike * leg.shares, "buy", ref, ts, structure_id=st.id)
                if selling:  # sold again the same night: through this structure's own shares position
                    book.apply(st.id, "shares", None, leg.shares, c.strike, ts)
                    own += leg.shares
                else:
                    new_structure_id = book.add_structure(
                        kind="shares",
                        source=st.source,
                        strategy_config_id=st.strategy_config_id,
                        underlying=st.underlying,
                        qty=leg.contracts,
                        entry_net=-c.strike * c.multiplier / 100,
                        reserved_cash=ZERO,
                        cover_structure_id=None,
                        parent_structure_id=st.id,
                        take_profit_net=None,
                        meta={"origin": kind, "lifecycle_event_id": event_id},
                        ts=ts,
                    )
                    book.apply(new_structure_id, "shares", None, leg.shares, c.strike, ts)
                    book.link_lifecycle(event_id, new_structure_id)
            elif leg.settled == "shares":
                book.move_cash(c.strike * leg.shares, "sell", ref, ts, structure_id=st.id)
                from_own = min(leg.shares, own)
                if from_own:
                    book.apply(st.id, "shares", None, -from_own, c.strike, ts)
                    own -= from_own
                if leg.shares > from_own and cover is not None:
                    book.apply(cover.id, "shares", None, from_own - leg.shares, c.strike, ts)
                    cover_used = True
            if charged > 0:
                book.move_cash(-charged, "fee", ref, ts, structure_id=st.id)
            events.append(
                self._event(
                    event_id,
                    st,
                    kind,
                    session_date,
                    ts,
                    c,
                    leg.contracts,
                    leg.close,
                    shares_delta,
                    cash_delta,
                    new_structure_id,
                )
            )
        if not events:
            return []
        after = book.structure(st.id)
        if all(p.qty == 0 for p in after.positions):
            book.close_structure(st.id, _close_reason([e.kind for e in events]), ts)
        elif not any(p.instrument == "option" and p.qty < 0 for p in after.positions):
            book.set_reserved(st.id, ZERO)
        if cover is not None and cover_used:
            if all(p.qty == 0 for p in book.structure(cover.id).positions):
                book.close_structure(cover.id, "called_away", ts)
        return events

    # --- helpers --------------------------------------------------------------------------------------------

    def _open_structures(self) -> list[StructureView]:
        with session_scope(self._factory) as s:
            return self._book_factory(s).structures()

    def _cover(self, book: OptionBook, st: StructureView) -> StructureView | None:
        """The open shares structure that covers this structure's short call, if it has one."""
        if st.cover_structure_id is None:
            return None
        cover = book.structure(st.cover_structure_id)
        return cover if cover.state == "open" else None

    def _close_date(self, expiry: date) -> date:
        """The session whose close decides the expiry: the expiry date, or the session before it."""
        return expiry if self._calendar.is_session(expiry) else self._calendar.previous_session(expiry)

    def _report_missing(self, missing: dict[tuple[str, date], list[int]]) -> None:
        if not missing:
            return
        with session_scope(self._factory) as s:
            for (underlying, day), ids in sorted(missing.items()):
                found = MissingClose(underlying, day, tuple(sorted(set(ids))))
                self.missing_closes.append(found)
                log_event(
                    s,
                    self._clock,
                    "error",
                    EVENT_SOURCE,
                    f"No official close for {underlying} on {day.isoformat()}: "
                    f"{len(found.structure_ids)} structure(s) left unchanged, retried on the next run.",
                    {
                        "underlying": underlying,
                        "close_date": day.isoformat(),
                        "structure_ids": list(found.structure_ids),
                    },
                    self._run_id,
                )

    def _event(
        self,
        event_id: int,
        st: StructureView,
        kind: LifecycleKind,
        session_date: date,
        ts: datetime,
        contract: OptionContract,
        qty: int,
        close: Decimal | None,
        shares_delta: int,
        cash_delta: Decimal,
        new_structure_id: int | None = None,
    ) -> LifecycleEvent:
        return LifecycleEvent(
            id=event_id,
            structure_id=st.id,
            source=st.source,
            strategy_config_id=st.strategy_config_id,
            kind=kind,
            session_date=session_date,
            ts=ts,
            contract=contract,
            qty=qty,
            strike=contract.strike,
            underlying_close=close,
            shares_delta=shares_delta,
            cash_delta=cash_delta,
            new_structure_id=new_structure_id,
        )


def _expiring(st: StructureView, session_date: date) -> list[tuple[OptPositionView, OptionContract]]:
    """The structure's open option positions whose expiry is on or before the session date."""
    return [
        (p, p.contract)
        for p in st.positions
        if p.contract is not None and p.qty != 0 and p.contract.expiry <= session_date
    ]
