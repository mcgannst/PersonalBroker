"""The collateral engine (OPTSIM task plan T4, feature plan §3.3 and §3.7): one pure decision over a snapshot
of the option book. It accepts or rejects an order, pairs every short leg with cover and says what cash the
structure must reserve. The owner's "no naked shorts, no margin" rule rests on this module.

No I/O and no clock: everything it reads is in the `CollateralBook` it is given.

What the numbers of a `CollateralDecision` mean:

- `reserve_cash` is the TOTAL reserve the order's structure carries once the order has filled: for an
  opening order the reserve of the new structure, for a close or a roll the new reserve of the structure
  acted on (0 when nothing short remains). The broker sets the structure's reserve to it at the fill.
- `free_cash_after = free cash + net x 100 x qty - fees - (reserve_cash - the structure's reserve now)`,
  with `net` the limit of a limit order and the market net (buy at the ask, sell at the bid) otherwise.
- Free cash is the account's cash less the reserve of every open structure and of every working order in
  the snapshot, whatever its source (one pool: cash reserved by one structure is never counted for
  another). A caller re-checking an order that is itself working leaves that order out of
  `working_orders`: the request carries no order id, so the engine cannot.
- Exposure of an underlying = for each open structure its reserve plus what its positions cost net of its
  own short credits (never below 0), plus the reservations of working opening orders.
- `max_loss`, `max_profit` and `breakevens` are in dollars for the whole order, before fees, and only for
  an opening order.

Cover: a short leg is covered by a long leg of the same right and multiplier that expires on or after it
(used once), else a short call by 100 free shares per contract of the same source, else a short put by
cash. Shares are free when no other open structure's call and no working order's call relies on them.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from trader.options.protocols import CollateralBook
from trader.options.settings import OptionSettings
from trader.options.types import (
    HUNDRED,
    SOURCE_MANUAL,
    ZERO,
    CollateralDecision,
    CoverKind,
    CoverPair,
    Instrument,
    OptionContract,
    OrderLeg,
    OrderRequest,
    RejectReason,
    StructureKind,
    StructureView,
    contract_label,
    net_price,
)

_FOUR_PLACES = Decimal("0.0001")
_NEW = 0  # the key, among structure ids, of the structure an opening order would create

# (structure id, instrument, contract id) -> contracts or shares that working orders already close
_Pending = Mapping[tuple[int, Instrument, int | None], int]


@dataclass(slots=True)
class _Opt:
    """One option line of a structure as it would stand after the order."""

    contract: OptionContract
    qty: int  # signed contracts: short < 0
    leg_no: int | None = None  # the opening leg of this order; None for a position already held
    unused: int = 0  # long contracts not yet used as cover


@dataclass(frozen=True, slots=True)
class _Cover:
    short: _Opt
    n: int  # contracts
    kind: CoverKind
    long: _Opt | None = None
    ref: int | None = None  # the shares structure (kind `shares`); None: the order's own structure


class DefaultCollateralEngine:
    """`CollateralEngine`: sync, pure, stateless."""

    def evaluate(self, req: OrderRequest, book: CollateralBook) -> CollateralDecision:
        settings = book.settings
        free_now = _free_cash(book)
        exposure_now = _exposure(req.underlying, book)
        base = CollateralDecision(
            accepted=False,
            reject_reason=None,
            detail="",
            kind="custom",
            net_at_market=None,
            reserve_cash=ZERO,
            max_loss=None,
            max_profit=None,
            breakevens=(),
            fees=ZERO,
            cash_after=book.account.cash,
            free_cash_after=free_now,
            exposure_after=exposure_now,
            cap_limit=settings.max_position_pct * book.account.equity,
            pairs=(),
            cover_structure_id=None,
        )

        def no(reason: RejectReason, detail: str) -> CollateralDecision:
            return replace(base, accepted=False, reject_reason=reason, detail=detail)

        # 1. the shape of the order
        problem = _shape_problem(req, settings)
        if problem is not None:
            return no("invalid_order", problem)
        base = replace(base, fees=_fees(req, settings))

        # 2. strategies paused
        if req.source != SOURCE_MANUAL and settings.strategies_paused:
            return no("strategies_paused", "strategy orders are paused (options.strategies_paused)")

        # 3. contracts
        contracts: dict[int, OptionContract] = {}  # by leg_no
        for leg in req.legs:
            if leg.contract_id is None:
                continue
            contract = book.contracts.get(leg.contract_id)
            if contract is None:
                return no("unknown_contract", f"leg {leg.leg_no}: contract {leg.contract_id} is not known")
            if contract.expiry < book.today:
                return no("expired_contract", f"leg {leg.leg_no}: {contract_label(contract)} has expired")
            if contract.underlying != req.underlying:
                return no(
                    "invalid_order", f"leg {leg.leg_no}: {contract_label(contract)} is not {req.underlying}"
                )
            contracts[leg.leg_no] = contract

        # 4. the structure a close or a roll acts on
        structure: StructureView | None = None
        if req.intent != "open":
            structure = next((s for s in book.structures if s.id == req.structure_id), None)
            if structure is None or structure.state != "open":
                return no("invalid_order", f"structure {req.structure_id} is not an open structure")
            if structure.source != req.source or structure.underlying != req.underlying:
                return no(
                    "invalid_order",
                    f"structure {structure.id} belongs to {structure.source} on {structure.underlying}",
                )
            if structure.frozen:
                return no("structure_frozen", f"structure {structure.id} is frozen")
            base = replace(base, kind=structure.kind)

        # 5. closing legs, and the structure as it would stand afterwards
        pending = _pending_closes(book)
        lines: list[_Opt] = []
        own_shares = 0
        if structure is not None:
            left = {p.id: p.qty for p in structure.positions if p.qty != 0}
            for leg in req.legs:
                position = next(
                    (
                        p
                        for p in structure.positions
                        if p.qty != 0
                        and p.instrument == leg.instrument
                        and (p.contract.id if p.contract is not None else None) == leg.contract_id
                    ),
                    None,
                )
                held = 0 if position is None else position.qty
                if leg.effect == "open":
                    if (held > 0 and leg.side == "sell") or (held < 0 and leg.side == "buy"):
                        return no(
                            "invalid_order",
                            f"leg {leg.leg_no} opens against a position the structure holds: close it",
                        )
                    continue
                closable = held if leg.side == "sell" else -held  # a sell closes a long, a buy a short
                already = pending.get((structure.id, leg.instrument, leg.contract_id), 0)
                if position is None or leg.ratio * req.qty > closable - already:
                    return no(
                        "nothing_to_close",
                        f"leg {leg.leg_no} closes {leg.ratio * req.qty} but {max(closable - already, 0)} "
                        "can be closed (working close orders counted)",
                    )
                left[position.id] += -leg.ratio * req.qty if held > 0 else leg.ratio * req.qty
            for p in structure.positions:
                qty = left.get(p.id, 0)
                if qty > 0:  # a long a working order may close is not counted as cover
                    key = (structure.id, p.instrument, None if p.contract is None else p.contract.id)
                    qty = max(qty - pending.get(key, 0), 0)
                if p.contract is None:
                    own_shares += qty
                elif qty != 0:
                    lines.append(_Opt(p.contract, qty))
        short_shares = False
        for leg in req.legs:
            if leg.effect != "open":
                continue
            if leg.instrument == "shares":
                if leg.side == "sell":
                    short_shares = True
                else:
                    own_shares += leg.ratio * req.qty
            else:
                signed = leg.ratio * req.qty if leg.side == "buy" else -leg.ratio * req.qty
                lines.append(_Opt(contracts[leg.leg_no], signed, leg.leg_no))
        if req.intent != "close":
            base = replace(base, kind=_classify(lines, own_shares, short_shares))

        # 6 to 8. cover for every short leg that would remain or be opened
        own_key = _NEW if structure is None else structure.id
        free_shares = _free_shares(req.underlying, book, structure, pending)
        share_change = sum(
            (leg.ratio if leg.side == "buy" else -leg.ratio) * req.qty
            for leg in req.legs
            if leg.instrument == "shares"
        )
        free_shares[own_key] = free_shares.get(own_key, 0) + share_change
        sources = {s.id: s.source for s in book.structures}
        candidates = [own_key]
        if structure is not None and structure.cover_structure_id is not None:
            candidates.append(structure.cover_structure_id)
        candidates += sorted(sid for sid in free_shares if sid not in candidates)
        candidates = [sid for sid in candidates if sid == own_key or sources.get(sid) == req.source]

        sells_shares = any(
            leg.instrument == "shares" and leg.effect == "close" and leg.side == "sell" for leg in req.legs
        )
        others_rely_on_these = free_shares[own_key] < 0
        covers, open_shorts = _pair_longs(lines)
        uncovered: list[_Opt] = []
        for short, n in open_shorts:
            if short.contract.right == "put":
                covers.append(_Cover(short, n, "cash"))
                continue
            need = n * short.contract.multiplier
            holder = next((sid for sid in candidates if free_shares.get(sid, 0) >= need), None)
            if holder is None:
                uncovered.append(short)
                continue
            free_shares[holder] -= need
            covers.append(_Cover(short, n, "shares", ref=None if holder == own_key else holder))
        held_uncovered = [u for u in uncovered if u.leg_no is None]
        if sells_shares and (others_rely_on_these or held_uncovered):
            return no("shares_committed", "the shares being sold cover an open or working short call")
        if held_uncovered:
            return no(
                "not_covered_after_close",
                f"{contract_label(held_uncovered[0].contract)} would be left short without cover",
            )
        if short_shares:
            return no("naked_short", "selling shares short is not allowed")
        if uncovered:
            return no(
                "naked_short",
                f"leg {uncovered[0].leg_no}: short {contract_label(uncovered[0].contract)} has no cover "
                f"(needs a long call expiring on or after it, or {uncovered[0].contract.multiplier} free "
                f"shares per contract held by {req.source} in one structure)",
            )
        reserve = _reserve(covers)
        external = [c.ref for c in covers if c.kind == "shares" and c.ref is not None]
        base = replace(
            base,
            reserve_cash=reserve,
            pairs=tuple(
                CoverPair(c.short.leg_no, c.kind, c.ref if c.long is None else c.long.leg_no)
                for c in covers
                if c.short.leg_no is not None
            ),
            cover_structure_id=external[0] if external else None,
        )

        # 9. prices
        multipliers = {leg_no: contract.multiplier for leg_no, contract in contracts.items()}
        market_net = _market_net(req, book, multipliers)
        base = replace(base, net_at_market=market_net)
        net = req.net_limit if req.order_type == "limit" and req.net_limit is not None else market_net
        if net is None:
            return no("no_quote", "a leg has no bid or no ask, so the order cannot be priced")

        # 10. cash never below zero
        cash_flow = net * HUNDRED * req.qty
        reserve_now = ZERO if structure is None else structure.reserved_cash
        reserve_change = reserve - reserve_now
        free_after = free_now + cash_flow - base.fees - reserve_change
        debit = max(-cash_flow, ZERO)
        exposure_change = reserve_change if req.intent == "close" else reserve_change + debit
        base = replace(
            base,
            cash_after=book.account.cash + cash_flow - base.fees,
            free_cash_after=free_after,
            exposure_after=max(exposure_now + exposure_change, ZERO),
        )
        if req.intent == "open":
            basis = _share_basis(book, base.cover_structure_id)
            max_loss, max_profit, breakevens = _preview(base.kind, lines, own_shares, cash_flow, basis)
            base = replace(base, max_loss=max_loss, max_profit=max_profit, breakevens=breakevens)
        if free_after < 0:
            return no(
                "insufficient_cash",
                f"free cash would be {free_after:.2f}: {free_now:.2f} free, {cash_flow:.2f} from the order, "
                f"{base.fees:.2f} fees, {reserve_change:.2f} more reserved",
            )

        # 11. the cap per underlying (a closing order never fails it)
        if req.intent != "close" and exposure_change > 0 and base.exposure_after > base.cap_limit:
            return no(
                "position_cap",
                f"{req.underlying} exposure would be {base.exposure_after:.2f}, over the cap of "
                f"{base.cap_limit:.2f} ({settings.max_position_pct} of the account value)",
            )
        return replace(base, accepted=True)


# --- step 1 -------------------------------------------------------------------------------------------------


def _shape_problem(req: OrderRequest, settings: OptionSettings) -> str | None:
    """Why the order is malformed, or None."""
    if not 1 <= len(req.legs) <= settings.max_legs:
        return f"an order has 1 to {settings.max_legs} legs, not {len(req.legs)}"
    if not 1 <= req.qty <= settings.max_contracts_per_order:
        return f"quantity must be 1 to {settings.max_contracts_per_order}, not {req.qty}"
    seen: set[tuple[str, int | None, str]] = set()
    numbers: set[int] = set()
    for leg in req.legs:
        if leg.underlying != req.underlying:
            return f"leg {leg.leg_no} is on {leg.underlying}, the order on {req.underlying}"
        if leg.ratio < 1:
            return f"leg {leg.leg_no} has ratio {leg.ratio}"
        if (leg.instrument == "option") != (leg.contract_id is not None):
            return f"leg {leg.leg_no}: an option leg names a contract, a shares leg does not"
        key = (leg.instrument, leg.contract_id, leg.effect)
        if key in seen or leg.leg_no in numbers:
            return f"leg {leg.leg_no} repeats another leg"
        seen.add(key)
        numbers.add(leg.leg_no)
    effects = {leg.effect for leg in req.legs}
    if req.intent == "open" and effects != {"open"}:
        return "an opening order has only opening legs"
    if req.intent == "close" and effects != {"close"}:
        return "a closing order has only closing legs"
    if req.intent == "roll" and effects != {"open", "close"}:
        return "a roll has a closing leg and an opening leg"
    if req.intent != "open" and req.structure_id is None:
        return f"a {req.intent} names the structure it acts on"
    if req.order_type == "market":
        if not settings.allow_market_orders:
            return "market orders are turned off (options.allow_market_orders)"
    elif req.net_limit is None and not req.walk:
        return "a limit order needs a net limit, or walk"
    return None


def _fees(req: OrderRequest, settings: OptionSettings) -> Decimal:
    total = ZERO
    for leg in req.legs:
        if leg.instrument == "shares":
            total += settings.share_commission
        else:
            total += settings.fee_per_contract * leg.ratio * req.qty
    return total


# --- the book's state ---------------------------------------------------------------------------------------


def _free_cash(book: CollateralBook) -> Decimal:
    """Cash no open structure and no working order has reserved. Worked out from the snapshot's own
    structures and working orders (not from `account.reserved`), so the decision and the lists agree."""
    reserved = sum((s.reserved_cash for s in book.structures if s.state == "open"), ZERO)
    reserved += sum((o.reserved_cash for o in book.working_orders if o.status == "working"), ZERO)
    return book.account.cash - reserved


def _exposure(underlying: str, book: CollateralBook) -> Decimal:
    total = ZERO
    for s in book.structures:
        if s.underlying != underlying or s.state != "open":
            continue
        cost = sum(
            (p.qty * p.avg_price * (1 if p.contract is None else p.contract.multiplier) for p in s.positions),
            ZERO,
        )
        total += s.reserved_cash + max(cost, ZERO)
    for o in book.working_orders:
        if o.status == "working" and o.underlying == underlying and o.intent != "close":
            total += o.reserved_cash
    return total


def _pending_closes(book: CollateralBook) -> dict[tuple[int, Instrument, int | None], int]:
    pending: dict[tuple[int, Instrument, int | None], int] = {}
    for o in book.working_orders:
        if o.status != "working" or o.structure_id is None:
            continue
        for leg in o.legs:
            if leg.effect == "close":
                key = (o.structure_id, leg.instrument, leg.contract_id)
                pending[key] = pending.get(key, 0) + leg.ratio * o.qty
    return pending


def _free_shares(
    underlying: str, book: CollateralBook, acted: StructureView | None, pending: _Pending
) -> dict[int, int]:
    """Shares of each open structure on the underlying that no call relies on: what is held, less what
    working orders already sell, less what the short calls of every OTHER structure and of every working
    order need. The calls of the structure the order acts on are left out (the caller works out what that
    structure needs after the order); a negative number means more is relied on than is there."""
    structures = [s for s in book.structures if s.underlying == underlying and s.state == "open"]
    free: dict[int, int] = {}
    for s in structures:
        held = sum(p.qty for p in s.positions if p.contract is None and p.qty > 0)
        free[s.id] = held - pending.get((s.id, "shares", None), 0)
    for s in structures:
        if acted is not None and s.id == acted.id:
            continue
        need = _share_need(
            [_Opt(p.contract, p.qty) for p in s.positions if p.contract is not None and p.qty != 0]
        )
        own = min(need, max(free[s.id], 0))
        free[s.id] -= own
        cover_id = s.cover_structure_id
        if need > own and cover_id is not None and cover_id in free:
            free[cover_id] -= need - own
    by_id = {s.id: s for s in structures}
    for o in book.working_orders:
        if o.status != "working" or o.underlying != underlying or o.intent == "close":
            continue
        opening = [leg for leg in o.legs if leg.effect == "open"]
        need = _share_need(_order_lines(opening, o.qty, book.contracts))
        need -= sum(leg.ratio * o.qty for leg in opening if leg.instrument == "shares" and leg.side == "buy")
        target = by_id.get(o.structure_id) if o.structure_id is not None else None
        preferred = [] if target is None else [target.cover_structure_id, target.id]
        order = [sid for sid in preferred if sid is not None and sid in free] + sorted(free)
        for sid in order:
            if need <= 0:
                break
            if by_id[sid].source != o.source:
                continue
            take = min(need, max(free[sid], 0))
            free[sid] -= take
            need -= take
    return free


def _order_lines(legs: Sequence[OrderLeg], qty: int, contracts: Mapping[int, OptionContract]) -> list[_Opt]:
    lines: list[_Opt] = []
    for leg in legs:
        contract = None if leg.contract_id is None else contracts.get(leg.contract_id)
        if contract is not None:
            lines.append(_Opt(contract, leg.ratio * qty if leg.side == "buy" else -leg.ratio * qty))
    return lines


def _share_need(lines: list[_Opt]) -> int:
    """Shares the short calls of these lines need once their own long legs are used."""
    _, open_shorts = _pair_longs(lines)
    return sum(n * short.contract.multiplier for short, n in open_shorts if short.contract.right == "call")


# --- cover and reserve --------------------------------------------------------------------------------------


def _pair_longs(lines: Sequence[_Opt]) -> tuple[list[_Cover], list[tuple[_Opt, int]]]:
    """Cover each short line with long lines of the same right and multiplier that expire on or after it,
    each long contract used once. Returns the covers and, per short line, the contracts left without one.
    The latest short is served first, so an earlier short never takes the only long that can cover it; a
    short takes the most protective strike on offer."""
    longs = [line for line in lines if line.qty > 0]
    for line in longs:
        line.unused = line.qty
    covers: list[_Cover] = []
    left: list[tuple[_Opt, int]] = []
    for short in sorted((ln for ln in lines if ln.qty < 0), key=lambda ln: ln.contract.expiry, reverse=True):
        want = short.contract
        usable = [
            ln
            for ln in longs
            if ln.contract.right == want.right
            and ln.contract.multiplier == want.multiplier
            and ln.contract.expiry >= want.expiry
        ]
        usable.sort(key=lambda ln: ln.contract.strike, reverse=want.right == "put")
        need = -short.qty
        for long in usable:
            take = min(need, long.unused)
            if take > 0:
                long.unused -= take
                need -= take
                covers.append(_Cover(short, take, "long_leg", long=long))
        if need > 0:
            left.append((short, need))
    return covers, left


def _intrinsic(contract: OptionContract, price: Decimal) -> Decimal:
    gap = price - contract.strike if contract.right == "call" else contract.strike - price
    return max(gap, ZERO)


def _reserve(covers: Sequence[_Cover]) -> Decimal:
    """The largest loss at expiry by intrinsic value, premium not counted. The short legs of one expiry and
    the long legs covering them are valued together at the prices 0, every strike and far above the highest
    strike (a later long at its intrinsic value on that day); expiries are added up, because a long leg is
    gone once it has paid for its own short. A share-covered call counts 0."""
    by_expiry: dict[date, list[_Cover]] = {}
    for cover in covers:
        if cover.kind != "shares":
            by_expiry.setdefault(cover.short.contract.expiry, []).append(cover)
    total = ZERO
    for group in by_expiry.values():
        strikes = {c.short.contract.strike for c in group}
        strikes |= {c.long.contract.strike for c in group if c.long is not None}
        prices = [ZERO, *sorted(strikes), max(strikes) * 2 + 1]
        worst = ZERO
        for price in prices:
            value = ZERO
            for c in group:
                value -= _intrinsic(c.short.contract, price) * c.n * c.short.contract.multiplier
                if c.long is not None:
                    value += _intrinsic(c.long.contract, price) * c.n * c.long.contract.multiplier
            worst = min(worst, value)
        total -= worst
    return total


# --- pricing, kind and the preview numbers ------------------------------------------------------------------


def _market_net(req: OrderRequest, book: CollateralBook, multipliers: Mapping[int, int]) -> Decimal | None:
    """The net of buying at the ask and selling at the bid; None when a leg lacks a bid or an ask."""
    prices: dict[int, Decimal] = {}
    for leg in req.legs:
        bid: Decimal | None
        ask: Decimal | None
        if leg.contract_id is None:
            share_quote = book.share_quotes.get(leg.underlying)
            bid, ask = (None, None) if share_quote is None else (share_quote.bid, share_quote.ask)
        else:
            quote = book.quotes.get(leg.contract_id)
            bid, ask = (None, None) if quote is None else (quote.bid, quote.ask)
        if bid is None or ask is None:
            return None
        prices[leg.leg_no] = ask if leg.side == "buy" else bid
    return net_price(req.legs, prices, multipliers)


def _classify(lines: Sequence[_Opt], shares: int, short_shares: bool) -> StructureKind:
    """The structure's kind by its shape. A two-leg spread of one expiry is a debit spread when the long
    strike is the protective one (the net is then a debit at any sane price), else a credit spread."""
    if short_shares:
        return "custom"
    if not lines:
        return "shares" if shares > 0 else "custom"
    if len(lines) == 1:
        only = lines[0]
        is_call = only.contract.right == "call"
        if shares > 0:
            return "covered_call" if is_call and only.qty < 0 else "custom"
        if only.qty > 0:
            return "long_call" if is_call else "long_put"
        return "covered_call" if is_call else "csp"
    if shares > 0:
        return "custom"
    longs = [line for line in lines if line.qty > 0]
    shorts = [line for line in lines if line.qty < 0]
    if len({abs(line.qty) for line in lines}) != 1:
        return "custom"
    if len(lines) == 2 and len(longs) == 1 and longs[0].contract.right == shorts[0].contract.right:
        long, short = longs[0].contract, shorts[0].contract
        if long.expiry != short.expiry:
            return "calendar" if long.strike == short.strike else "diagonal"
        if long.strike == short.strike:
            return "custom"
        protective = long.strike < short.strike if long.right == "call" else long.strike > short.strike
        return "debit_spread" if protective else "credit_spread"
    if (
        len(lines) == 4
        and len({line.contract.expiry for line in lines}) == 1
        and sorted(line.contract.right for line in longs) == ["call", "put"]
        and sorted(line.contract.right for line in shorts) == ["call", "put"]
    ):
        return "iron_condor"
    return "custom"


def _share_basis(book: CollateralBook, cover_structure_id: int | None) -> Decimal | None:
    """What a share of the covering structure cost; None when the order brings its own shares."""
    for s in book.structures:
        if s.id == cover_structure_id:
            return next((p.avg_price for p in s.positions if p.contract is None and p.qty > 0), None)
    return None


def _preview(
    kind: StructureKind, lines: Sequence[_Opt], shares: int, cash_flow: Decimal, share_basis: Decimal | None
) -> tuple[Decimal | None, Decimal | None, tuple[Decimal, ...]]:
    """(max loss, max profit, breakevens) in dollars for the whole order, before fees; (None, None, ()) for
    the kinds the plan does not ask for. `cash_flow` is what the order receives (a debit is negative)."""

    def at(price: Decimal) -> tuple[Decimal, ...]:
        return (price.quantize(_FOUR_PLACES),)

    if kind in ("long_call", "long_put", "csp"):
        contract = lines[0].contract
        size = abs(lines[0].qty) * contract.multiplier
        at_strike = contract.strike * size
        if kind == "long_call":
            return -cash_flow, None, at(contract.strike - cash_flow / size)
        if kind == "long_put":
            return -cash_flow, at_strike + cash_flow, at(contract.strike + cash_flow / size)
        return at_strike - cash_flow, cash_flow, at(contract.strike - cash_flow / size)
    if kind == "covered_call":
        contract = lines[0].contract
        size = abs(lines[0].qty) * contract.multiplier
        if shares == 0 and share_basis is None:
            return None, None, ()
        paid = -cash_flow if share_basis is None else share_basis * size - cash_flow
        return paid, contract.strike * size - paid, at(paid / size)
    if kind in ("debit_spread", "credit_spread"):
        long = next(line.contract for line in lines if line.qty > 0)
        short = next(line.contract for line in lines if line.qty < 0)
        size = abs(lines[0].qty) * long.multiplier
        width = abs(long.strike - short.strike) * size
        sign = 1 if long.right == "call" else -1
        if kind == "debit_spread":
            return -cash_flow, width + cash_flow, at(long.strike - sign * cash_flow / size)
        return width - cash_flow, cash_flow, at(short.strike + sign * cash_flow / size)
    return None, None, ()
