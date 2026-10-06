"""OPTSIM T5: the simulated option broker over the database. Orders from submit to fill: the reservation
while an order works, all legs or none, one transaction per fill, the collateral re-check at the fill, closes
and rolls, the walk, the take-profit, day orders, and the book lock. The collateral engine is canned (T4 owns
the real one); quotes come from `FakeOptionMarket`."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.options import factories as f
from tests.options.fakes import CannedCollateral, FakeOptionMarket, accept, reject
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.options.book import DbBook
from trader.options.broker import SimOptionBroker, undelivered_fills
from trader.options.fill_model import QuoteFillModel
from trader.options.protocols import UnknownUnderlying
from trader.options.settings import OptionSettings
from trader.options.types import OptionQuote, OrderLeg, OrderRequest, SubmitResult

pytestmark = pytest.mark.db
D = Decimal
CAL = SessionCalendar()
FEE = D("0.99")
MINUTE = timedelta(seconds=60)


class YieldingMarket(FakeOptionMarket):
    """Gives the event loop away on every quotes pass, so two brokers polling together interleave."""

    async def quotes(self, contract_ids: Sequence[int]) -> dict[int, OptionQuote]:
        await asyncio.sleep(0)
        return await super().quotes(contract_ids)


@dataclass
class World:
    factory: sessionmaker[Session]
    clock: FixedClock
    market: FakeOptionMarket
    collateral: CannedCollateral
    broker: SimOptionBroker
    run_id: int
    put: int  # F 14.50 put, quoted 0.45 / 0.50
    low_put: int  # F 14.00 put, quoted 0.30 / 0.35
    call: int  # F 15.00 call, quoted 1.10 / 1.20

    def new_broker(self) -> SimOptionBroker:
        return SimOptionBroker(
            self.factory,
            self.clock,
            CAL,
            self.market,
            self.collateral,
            QuoteFillModel(),
            OptionSettings(),
            self.run_id,
            lambda: D("1.37"),
        )

    def count(self, model: Any) -> int:
        with self.factory() as s:
            return int(s.execute(select(func.count()).select_from(model)).scalar_one())

    def ledger(self) -> list[tuple[str, Decimal]]:
        with self.factory() as s:
            rows = s.execute(select(m.CashLedger.kind, m.CashLedger.amount).order_by(m.CashLedger.id))
            return [(kind, amount) for kind, amount in rows]

    def sell_put(self, limit: str | None = "0.45", **over: Any) -> OrderRequest:
        """Sell to open one 14.50 put; the canned engine calls it a cash-secured put reserving 1,450."""
        self.collateral.decision = accept(kind="csp", reserve_cash="1450")
        return f.make_request(
            [f.make_leg(1, contract_id=self.put)], net_limit=None if limit is None else D(limit), **over
        )

    async def open_put(self, **over: Any) -> int:
        """A filled short put at 0.45; returns its structure id."""
        await self.broker.submit(self.sell_put(**over))
        (event,) = await self.broker.poll(self.clock.now())
        return event.structure_id


@pytest.fixture
def w(db_factory: sessionmaker[Session]) -> World:
    clock = FixedClock(f.T0)
    market = YieldingMarket(clock)
    with db_factory() as s:
        run_id = f.add_options_run(s, 5000)
        symbol_id = f.add_underlying(s, "F")
        market.add_underlying("F", "14.80", symbol_id=symbol_id, bid="14.79", ask="14.81")
        ids: list[int] = []
        for strike, right, bid, ask in (
            ("14.50", "put", "0.45", "0.50"),
            ("14.00", "put", "0.30", "0.35"),
            ("15.00", "call", "1.10", "1.20"),
        ):
            cid = f.add_contract(s, symbol_id, strike=strike, right=right)  # type: ignore[arg-type]
            market.contracts[cid] = f.make_contract(
                cid,
                strike=strike,
                right=right,
                underlying_symbol_id=symbol_id,  # type: ignore[arg-type]
            )
            market.set_quote(cid, bid, ask)
            ids.append(cid)
        s.commit()
    world = World(db_factory, clock, market, CannedCollateral(accept()), None, run_id, *ids)  # type: ignore[arg-type]
    world.broker = world.new_broker()
    return world


async def test_submit_stores_rejected_orders_with_reason(w: World) -> None:
    w.collateral.decision = reject("naked_short", "the call has no cover")
    result = await w.broker.submit(f.make_request([f.make_leg(1, contract_id=w.call)]))
    assert isinstance(result, SubmitResult) and result.decision.accepted is False
    stored = await w.broker.order(result.order.id)
    assert stored is not None
    for order in (result.order, stored):
        assert (order.status, order.reject_reason) == ("rejected", "naked_short")
        assert (order.reject_detail, order.reserved_cash) == ("the call has no cover", 0)
        assert order.closed_at == f.T0 and [leg.contract_id for leg in order.legs] == [w.call]
    assert await w.broker.poll(f.T0) == [] and (await w.broker.account()).reserved == 0
    # An order on something that is not a symbol can't be stored at all.
    with pytest.raises(UnknownUnderlying):
        await w.broker.submit(f.make_request(underlying="ZZZ"))
    assert w.count(m.OptOrder) == 1


async def test_submit_reserves_and_poll_releases(w: World) -> None:
    result = await w.broker.submit(w.sell_put())
    # The reserve and the fee, less the credit that arrives with the fill.
    held = D("1450") + FEE - D("45")
    assert (result.order.status, result.order.reserved_cash) == ("working", held)
    before = await w.broker.account()
    assert (before.cash, before.reserved, before.free_cash) == (D("5000"), held, D("5000") - held)

    (event,) = await w.broker.poll(f.T0)
    assert (event.order_id, event.intent, event.net_price, event.fees) == (
        result.order.id,
        "open",
        D("0.45"),
        FEE,
    )
    assert event.realized_pnl is None
    (leg,) = event.legs
    assert (leg.side, leg.effect, leg.qty, leg.price, leg.fee) == ("sell", "open", 1, D("0.45"), FEE)
    assert (leg.quote["bid"], leg.quote["ask"], leg.quote["multiplier"]) == ("0.45", "0.50", 100)

    order = await w.broker.order(result.order.id)
    assert order is not None and (order.status, order.reserved_cash, order.fill_net) == (
        "filled",
        0,
        D("0.45"),
    )
    assert (order.fees, order.structure_id, order.closed_at) == (FEE, event.structure_id, f.T0)
    (structure,) = await w.broker.structures()
    assert (structure.id, structure.kind, structure.reserved_cash) == (event.structure_id, "csp", D("1450"))
    assert (structure.entry_net, structure.fees_total) == (D("0.45"), FEE)
    assert [(p.qty, p.avg_price) for p in structure.positions] == [(-1, D("0.45"))]
    after = await w.broker.account()
    assert (after.cash, after.reserved) == (D("5000") + D("45") - FEE, D("1450"))
    assert after.free_cash == before.free_cash  # the fill moved the hold from the order to the structure
    assert (after.premium_collected, after.positions_value) == (D("45"), D("-50"))  # the put at its ask
    with w.factory() as s:
        fill = s.execute(select(m.OptFill)).scalar_one()
    assert (fill.usd_cad_rate, fill.quote["bid"], fill.structure_id) == (D("1.37"), "0.45", structure.id)
    assert await w.broker.poll(f.T0) == []  # nothing is left working


async def test_multi_leg_fills_all_or_none(w: World) -> None:
    legs = [f.make_leg(1, contract_id=w.put), f.make_leg(2, side="buy", contract_id=w.low_put)]
    w.collateral.decision = accept(kind="credit_spread", reserve_cash="50")
    result = await w.broker.submit(f.make_request(legs, order_type="market"))
    w.market.set_quote(w.low_put, "0.30", "0.35", fetched_at=f.T0 - MINUTE)  # one stale leg
    assert await w.broker.poll(f.T0) == []
    assert (w.count(m.OptFill), w.count(m.OptPosition), w.count(m.OptStructure)) == (0, 0, 0)
    assert (await w.broker.orders(status="working"))[0].id == result.order.id
    w.market.set_quote(w.low_put, "0.30", "0.35")
    (event,) = await w.broker.poll(f.T0)
    assert event.net_price == D("0.10") and [leg.price for leg in event.legs] == [D("0.45"), D("0.35")]
    assert (w.count(m.OptFill), w.count(m.OptPosition), w.count(m.OptStructure)) == (2, 2, 1)
    assert w.ledger()[1:] == [("sell", D("45")), ("buy", D("-35")), ("fee", -2 * FEE)]


async def test_fill_writes_fills_ledger_positions_structure_in_one_transaction(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await w.broker.submit(w.sell_put())
    real = DbBook.move_cash

    def fail_on_the_fee(self: DbBook, amount: Decimal, kind: Any, *args: Any, **kwargs: Any) -> None:
        if kind == "fee":  # the last write of a fill: the fill row, the position and the sale exist by now
            raise RuntimeError("forced")
        real(self, amount, kind, *args, **kwargs)

    monkeypatch.setattr(DbBook, "move_cash", fail_on_the_fee)
    assert await w.broker.poll(f.T0) == []  # the failure is logged, not raised: other orders still get a turn
    with w.factory() as s:
        error = s.execute(select(m.EventLog).where(m.EventLog.level == "error")).scalar_one()
    assert (error.source, error.run_id) == ("options.broker", w.run_id)
    assert error.data == {"order_id": result.order.id, "error": "RuntimeError"}  # the type, not the text
    assert (w.count(m.OptFill), w.count(m.OptPosition), w.count(m.OptStructure)) == (0, 0, 0)
    assert w.ledger() == [("deposit", D("5000"))]
    order = await w.broker.order(result.order.id)
    assert order is not None and (order.status, order.reserved_cash) == (
        "working",
        result.order.reserved_cash,
    )

    monkeypatch.undo()
    assert len(await w.broker.poll(f.T0)) == 1
    assert (w.count(m.OptFill), w.count(m.OptPosition), w.count(m.OptStructure)) == (1, 1, 1)
    assert w.ledger() == [("deposit", D("5000")), ("sell", D("45")), ("fee", -FEE)]


async def test_collateral_recheck_at_fill_cancels(w: World) -> None:
    result = await w.broker.submit(w.sell_put())
    w.collateral.decision = reject("insufficient_cash", "the cash went elsewhere")
    assert await w.broker.poll(f.T0) == []
    order = await w.broker.order(result.order.id)
    assert order is not None and (order.status, order.reject_reason) == ("cancelled", "insufficient_cash")
    assert (order.reject_detail, order.reserved_cash) == ("the cash went elsewhere", 0)
    assert w.count(m.OptFill) == 0 and w.ledger() == [("deposit", D("5000"))]
    # The re-check saw the book without the order itself: no working order, nothing reserved.
    _, book = w.collateral.calls[-1]
    assert (list(book.working_orders), book.account.reserved) == ([], 0)


async def test_close_and_roll_update_the_structure(w: World) -> None:
    sid = await w.open_put()
    # Roll: buy the 14.50 put back at 0.50, sell the 14.00 put at 0.30, in one order on the same structure.
    w.collateral.decision = accept(kind="csp", reserve_cash="1400")
    roll = f.make_request(
        [
            f.make_leg(1, side="buy", effect="close", contract_id=w.put),
            f.make_leg(2, side="sell", effect="open", contract_id=w.low_put),
        ],
        intent="roll",
        structure_id=sid,
        order_type="market",
        take_profit_pct=D("0.50"),
    )
    # The 20.00 debit and two fees, less the 50.00 the structure's reserve falls by: nothing extra to hold.
    assert (await w.broker.submit(roll)).order.reserved_cash == 0
    (rolled,) = await w.broker.poll(f.T0)
    assert (rolled.structure_id, rolled.net_price, rolled.realized_pnl) == (sid, D("-0.20"), D("-5"))
    (structure,) = await w.broker.structures()
    assert [(p.contract.id, p.qty) for p in structure.positions if p.contract] == [
        (w.put, 0),
        (w.low_put, -1),
    ]
    assert (structure.state, structure.reserved_cash, structure.realized_pnl) == ("open", D("1400"), D("-5"))
    assert (structure.entry_net, structure.take_profit_net) == (D("0.30"), D("-0.15"))  # of the new leg
    # Close: buy the 14.00 put back at 0.35.
    w.collateral.decision = accept(kind="csp", reserve_cash="0")
    close = f.make_request(
        [f.make_leg(1, side="buy", effect="close", contract_id=w.low_put)],
        intent="close",
        structure_id=sid,
        order_type="market",
    )
    await w.broker.submit(close)
    (closed,) = await w.broker.poll(f.T0)
    assert (closed.structure_id, closed.realized_pnl) == (sid, D("-5"))  # (0.30 - 0.35) x 100
    assert await w.broker.structures() == []
    (done,) = await w.broker.structures(open_only=False)
    assert (done.state, done.close_reason, done.reserved_cash) == ("closed", "closed", 0)
    assert (done.realized_pnl, done.fees_total, done.closed_at) == (D("-10"), 4 * FEE, f.T0)
    assert (await w.broker.account()).reserved == 0


async def test_undelivered_fills_rebuilds_the_events_poll_returned(w: World) -> None:
    """What the worker reads back after a restart is, field for field, what `poll` handed the dead one."""
    polled = []
    await w.broker.submit(w.sell_put(take_profit_pct=D("0.50")))
    polled += await w.broker.poll(f.T0)
    sid = polled[0].structure_id
    w.collateral.decision = accept(kind="csp", reserve_cash="1400")
    legs = [
        f.make_leg(1, side="buy", effect="close", contract_id=w.put),
        f.make_leg(2, side="sell", effect="open", contract_id=w.low_put),
    ]
    await w.broker.submit(f.make_request(legs, intent="roll", structure_id=sid, order_type="market"))
    w.clock.advance(MINUTE)
    polled += await w.broker.poll(w.clock.now())
    w.collateral.decision = accept(kind="csp", reserve_cash="0")
    close = [f.make_leg(1, side="buy", effect="close", contract_id=w.low_put)]
    await w.broker.submit(f.make_request(close, intent="close", structure_id=sid, order_type="market"))
    w.clock.advance(MINUTE)
    polled += await w.broker.poll(w.clock.now())
    assert [(e.intent, e.realized_pnl) for e in polled] == [
        ("open", None),
        ("roll", D("-5")),
        ("close", D("-5")),
    ]

    assert undelivered_fills(w.factory, w.run_id, f.T0, "opt_fill:") == polled
    assert undelivered_fills(w.factory, w.run_id, f.T0 + MINUTE, "opt_fill:") == polled[1:]  # the look-back
    assert undelivered_fills(w.factory, w.run_id + 1, f.T0, "opt_fill:") == []  # another run's are not ours
    with w.factory() as s:  # a failed delivery leaves the fill undelivered; a succeeded one settles it
        for order_id, status in ((polled[0].order_id, "failed"), (polled[1].order_id, "succeeded")):
            job = f"opt_fill:{order_id}"
            s.add(m.JobRun(job=job, session_date=f.SESSION, started_at=f.T0, status=status))
        s.commit()
    assert undelivered_fills(w.factory, w.run_id, f.T0, "opt_fill:") == [polled[0], polled[2]]


async def test_a_close_or_roll_stores_the_cover_link_of_its_decision(w: World) -> None:
    """A close can leave a short call relying on another structure's shares: the link the collateral
    decision names is written to the structure, as it is when a structure is opened."""
    other = await w.open_put()
    sid = await w.open_put()
    w.collateral.decision = accept(kind="csp", reserve_cash="1400", cover_structure_id=other)
    roll = f.make_request(
        [
            f.make_leg(1, side="buy", effect="close", contract_id=w.put),
            f.make_leg(2, side="sell", effect="open", contract_id=w.low_put),
        ],
        intent="roll",
        structure_id=sid,
        order_type="market",
    )
    await w.broker.submit(roll)
    await w.broker.poll(f.T0)

    links = {s.id: s.cover_structure_id for s in await w.broker.structures()}
    assert links == {other: None, sid: other}


async def test_walk_steps_one_tick_and_stops_at_the_market(w: World) -> None:
    result = await w.broker.submit(w.sell_put(limit=None, walk=True))
    oid = result.order.id
    assert result.order.net_limit == D("0.48")  # the 0.475 midpoint, rounded up to the tick
    assert result.order.reserved_cash == D("1450") + FEE - D("48")

    async def limit() -> Decimal | None:
        order = await w.broker.order(oid)
        assert order is not None
        return order.net_limit

    assert await w.broker.poll(f.T0) == [] and await w.broker.walk(f.T0) == 0  # not due yet
    for expected in ("0.47", "0.46", "0.45"):
        w.clock.advance(MINUTE)
        assert await w.broker.walk(w.clock.now()) == 1
        assert await limit() == D(expected)
    assert (await w.broker.account()).reserved == D("1450") + FEE - D("45")  # the hold followed the limit
    w.clock.advance(MINUTE)
    assert await w.broker.walk(w.clock.now()) == 0 and await limit() == D("0.45")  # never past the bid
    (event,) = await w.broker.poll(w.clock.now())
    assert event.net_price == D("0.45")


async def test_walk_waits_outside_the_session_and_on_an_unusable_quote(w: World) -> None:
    oid = (await w.broker.submit(w.sell_put(limit=None, walk=True, tif="gtc"))).order.id

    async def walked(now: Any) -> tuple[int, Decimal | None]:
        w.clock.set(now)
        moved = await w.broker.walk(now)
        order = await w.broker.order(oid)
        assert order is not None
        return moved, order.net_limit

    assert await walked(f.T0 + timedelta(hours=6)) == (0, D("0.48"))  # due, but the session has closed
    due = f.T0 + MINUTE
    for bad in ({"delay": 15}, {"is_halted": True}, {"fetched_at": due - MINUTE}):
        w.market.set_quote(w.put, "0.45", "0.50", **bad)
        assert await walked(due) == (0, D("0.48"))
    w.market.set_quote(w.put, "0.50", "0.45")  # crossed
    assert await walked(due) == (0, D("0.48"))
    w.market.set_quote(w.put, "0.45", "0.50")
    assert await walked(due) == (1, D("0.47"))  # the skipped passes did not use the step up


async def test_take_profit_waits_outside_the_session_and_on_an_unusable_quote(w: World) -> None:
    await w.open_put(take_profit_pct=D("0.50"))
    w.collateral.decision = accept(kind="csp", reserve_cash="0")
    w.market.set_quote(w.put, "0.15", "0.20")  # at the take-profit
    closed = f.T0 + timedelta(hours=6)
    w.clock.set(closed)
    assert await w.broker.take_profits(closed) == []
    w.clock.set(f.T0)
    for bad in ({"delay": None}, {"is_halted": True}, {"fetched_at": f.T0 - MINUTE}):
        w.market.set_quote(w.put, "0.15", "0.20", **bad)
        assert await w.broker.take_profits(f.T0) == []
    w.market.set_quote(w.put, "0.25", "0.20")  # crossed
    assert await w.broker.take_profits(f.T0) == [] and w.count(m.OptOrder) == 1
    w.market.set_quote(w.put, "0.15", "0.20")
    assert len(await w.broker.take_profits(f.T0)) == 1


async def test_manual_reprice_stops_the_walk(w: World) -> None:
    oid = (await w.broker.submit(w.sell_put(limit=None, walk=True))).order.id
    assert await w.broker.reprice(oid, D("0.46"), "web:stephen") is True
    order = await w.broker.order(oid)
    assert order is not None and (order.net_limit, order.walk) == (D("0.46"), False)
    assert order.reserved_cash == D("1450") + FEE - D("46")
    w.clock.advance(5 * MINUTE)
    assert await w.broker.walk(w.clock.now()) == 0
    order = await w.broker.order(oid)
    assert order is not None and order.net_limit == D("0.46")
    with pytest.raises(TypeError):
        await w.broker.reprice(oid, 0.46, "web:stephen")  # type: ignore[arg-type]


async def test_take_profit_submits_one_close(w: World) -> None:
    sid = await w.open_put(take_profit_pct=D("0.50"))
    (structure,) = await w.broker.structures()
    assert structure.take_profit_net == D("-0.225")  # buy back at half the 0.45 credit
    w.collateral.decision = accept(kind="csp", reserve_cash="0")
    assert await w.broker.take_profits(f.T0) == []  # 0.50 to buy back: not yet
    w.market.set_quote(w.put, "0.15", "0.20")
    (oid,) = await w.broker.take_profits(f.T0)
    order = await w.broker.order(oid)
    assert order is not None
    assert (order.intent, order.structure_id, order.source) == ("close", sid, "manual")
    assert (order.order_type, order.net_limit, order.reason, order.qty) == (
        "limit",
        D("-0.225"),
        "take_profit",
        1,
    )
    assert order.legs == (OrderLeg(1, "option", "buy", "close", 1, "F", w.put),)
    assert await w.broker.take_profits(f.T0) == []  # one working close per structure
    (event,) = await w.broker.poll(f.T0)
    assert (event.net_price, event.realized_pnl) == (D("-0.20"), D("25"))  # at the market, not the limit
    assert await w.broker.structures() == [] and await w.broker.take_profits(f.T0) == []
    # A take-profit the collateral engine refuses is not stored (it would repeat on every pass).
    w.market.set_quote(w.put, "0.45", "0.50")
    await w.open_put(take_profit_pct=D("0.50"))
    w.market.set_quote(w.put, "0.15", "0.20")
    w.collateral.decision = reject("strategies_paused")
    orders = w.count(m.OptOrder)
    assert await w.broker.take_profits(f.T0) == [] and w.count(m.OptOrder) == orders


async def test_day_orders_expire_gtc_stay(w: World) -> None:
    day = (await w.broker.submit(w.sell_put("0.60"))).order.id
    gtc = (await w.broker.submit(w.sell_put("0.60", tif="gtc"))).order.id
    w.clock.set(f.T0 + timedelta(hours=7))  # 17:00 ET: an order placed now belongs to the next session
    late = (await w.broker.submit(w.sell_put("0.60"))).order.id
    assert await w.broker.expire_day_orders(f.SESSION, w.clock.now()) == 1
    statuses = {o.id: (o.status, o.reserved_cash) for o in await w.broker.orders()}
    held = D("1450") + FEE - D("60")
    assert statuses == {day: ("expired", 0), gtc: ("working", held), late: ("working", held)}
    assert (await w.broker.account()).reserved == 2 * held
    assert await w.broker.expire_day_orders(f.SESSION, w.clock.now()) == 0


async def test_cancel_and_reprice_only_when_working(w: World) -> None:
    oid = (await w.broker.submit(w.sell_put("0.60"))).order.id
    assert await w.broker.cancel(oid, "changed my mind", "web:stephen") is True
    order = await w.broker.order(oid)
    assert order is not None and (order.status, order.reserved_cash, order.closed_at) == (
        "cancelled",
        0,
        f.T0,
    )
    assert await w.broker.cancel(oid, "again", "web:stephen") is False
    assert await w.broker.reprice(oid, D("0.45"), "web:stephen") is False
    assert await w.broker.cancel(999, "x", "web:stephen") is False
    assert await w.broker.reprice(999, D("0.45"), "web:stephen") is False
    assert await w.broker.poll(f.T0) == [] and await w.broker.order(999) is None
    # A reprice the collateral engine refuses changes nothing.
    other = (await w.broker.submit(w.sell_put("0.60"))).order.id
    w.collateral.decision = reject("insufficient_cash")
    assert await w.broker.reprice(other, D("0.10"), "web:stephen") is False
    order = await w.broker.order(other)
    assert order is not None and (order.status, order.net_limit) == ("working", D("0.60"))


async def test_cash_and_reserved_reconcile_to_the_ledger(w: World) -> None:
    sid = await w.open_put()  # +45.00, fee 0.99, reserve 1,450
    w.collateral.decision = accept(kind="long_call", reserve_cash="0")
    buy_call = f.make_request([f.make_leg(1, side="buy", contract_id=w.call)], qty=2, net_limit=D("-1.20"))
    assert (await w.broker.submit(buy_call)).order.reserved_cash == D("240") + 2 * FEE  # the debit and fees
    assert len(await w.broker.poll(f.T0)) == 1  # -240.00, fees 1.98
    close = f.make_request(
        [f.make_leg(1, side="buy", effect="close", contract_id=w.put)],
        intent="close",
        structure_id=sid,
        order_type="market",
    )
    await w.broker.submit(close)
    assert len(await w.broker.poll(f.T0)) == 1  # -50.00, fee 0.99; the reserve is released
    await w.broker.submit(w.sell_put("0.60"))  # left working
    await w.broker.cancel((await w.broker.submit(w.sell_put("0.60"))).order.id, "test", "test")

    account = await w.broker.account()
    ledger = w.ledger()
    assert account.cash == sum(amount for _, amount in ledger) == D("5000") + 45 - 240 - 50 - 4 * FEE
    with w.factory() as s:
        open_reserve = s.execute(
            select(func.sum(m.OptStructure.reserved_cash)).where(m.OptStructure.state == "open")
        ).scalar_one()
        working = s.execute(
            select(func.sum(m.OptOrder.reserved_cash)).where(m.OptOrder.status == "working")
        ).scalar_one()
        fill_fees = s.execute(select(func.sum(m.OptFill.fee))).scalar_one()
        structure_fees = s.execute(select(func.sum(m.OptStructure.fees_total))).scalar_one()
    assert (open_reserve, working) == (0, D("1450") + FEE - D("60"))
    assert account.reserved == open_reserve + working and account.free_cash == account.cash - account.reserved
    assert -sum(amount for kind, amount in ledger if kind == "fee") == fill_fees == structure_fees == 4 * FEE
    assert account.positions_value == D("220") and account.equity == account.cash + D(
        "220"
    )  # calls at the bid
    assert (account.premium_collected, account.marks_complete) == (D("45"), True)


async def test_two_brokers_on_one_run_do_not_double_fill(w: World) -> None:
    await w.broker.submit(w.sell_put())
    first, second = await asyncio.gather(w.broker.poll(f.T0), w.new_broker().poll(f.T0))
    assert len(first) + len(second) == 1  # both read the order as working; the lock let one fill it
    assert (w.count(m.OptFill), w.count(m.OptStructure)) == (1, 1)
    assert w.ledger() == [("deposit", D("5000")), ("sell", D("45")), ("fee", -FEE)]
