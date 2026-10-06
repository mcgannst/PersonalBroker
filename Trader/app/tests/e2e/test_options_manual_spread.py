"""OPTSIM T16: the manual desk end to end, through the real API, broker, collateral engine, fill model,
worker and lifecycle engine (see `options_world`).

- a vertical spread from the ticket, held to expiry and settled leg by leg;
- a partial close: the reserve afterwards is what the remaining legs need;
- feature acceptance item 2: no order that leaves a short leg uncovered is accepted from the ticket;
- feature acceptance item 3: a sell never fills above the bid and a buy never below the ask, and nothing
  fills out of hours or on a stale, delayed, halted, one-sided or zero-bid quote.
"""

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.e2e import options_world as w
from tests.e2e.options_world import OptionsWorld
from trader.db import models as m

pytestmark = pytest.mark.db
D = Decimal
FEE = D("0.99")


@pytest.fixture
async def world(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[OptionsWorld]:
    async with w.open_world(db_factory, monkeypatch, tmp_path) as made:
        made.go(w.DAY0, 10, 0)
        yield made
        assert made.errors() == []


async def fill(world: OptionsWorld, expected: int = 1) -> None:
    """Let the worker run a minute: `expected` orders fill."""
    start = world.clock.now()
    reports = [await world.step()]
    while world.clock.now() < start + timedelta(seconds=60):
        world.clock.advance(timedelta(seconds=5))
        reports.append(await world.step())
    assert sum(r.fills for r in reports) == expected


async def open_put_credit_spread(world: OptionsWorld, qty: int = 1) -> tuple[dict[tuple[str, str], int], int]:
    """Sell the 50 put at its 1.50 bid and buy the 48 put at its 1.15 ask: 0.35 credit a unit."""
    ids = world.chain_ids(w.PUT_EXPIRY)
    legs = [world.leg(ids[("50", "put")], "sell"), world.leg(ids[("48", "put")], "buy")]
    preview = world.post("/orders/preview", world.ticket(legs, qty=qty))
    assert (preview["accepted"], preview["kind"]) == (True, "credit_spread")
    assert D(preview["reserve_cash"]) == 200 * qty and D(preview["net_at_market"]) == D("0.35")
    assert D(preview["max_loss"]) == 165 * qty and D(preview["max_profit"]) == 35 * qty
    order = world.submit(legs, qty=qty)
    assert (order["status"], order["source"]) == ("working", "manual")
    await fill(world)
    (spread,) = await world.structures()
    assert (spread.kind, spread.source, spread.entry_net) == ("credit_spread", "manual", D("0.35"))
    assert spread.reserved_cash == 200 * qty
    return ids, spread.id


def lifecycle(world: OptionsWorld) -> list[tuple[str, str, int, Decimal]]:
    """(kind, how it settled, shares, cash) of each lifecycle event, in order."""
    with world.factory() as s:
        rows = s.execute(select(m.OptLifecycleEvent).order_by(m.OptLifecycleEvent.id)).scalars()
        return [(r.kind, r.detail["settled"], r.shares_delta, r.cash_delta) for r in rows]


@pytest.mark.parametrize(
    ("close", "events", "cash_change", "shares_left"),
    [
        # both legs out of the money: both expire, the credit is kept
        ("52", [("expired", "expired", 0, D(0)), ("expired", "expired", 0, D(0))], D(0), 0),
        # the short 50 put is in the money, the long 48 put is not: 100 shares are put to the account
        ("49", [("expired", "expired", 0, D(0)), ("assigned", "shares", 100, D(-5000))], D(-5000), 100),
        # both in the money: the shares assigned at 50 are sold at 48 the same night, cash only
        ("46", [("assigned", "shares", 100, D(-5000)), ("exercised", "shares", -100, D(4800))], D(-200), 0),
    ],
)
async def test_manual_vertical_spread_through_expiry(
    world: OptionsWorld,
    close: str,
    events: list[tuple[str, str, int, Decimal]],
    cash_change: Decimal,
    shares_left: int,
) -> None:
    _, spread_id = await open_put_credit_spread(world)
    opened = await world.assert_reconciled()
    assert opened.cash == D(20_000) + 35 - 2 * FEE
    (shown,) = world.get("/positions")["items"]
    assert (shown["kind"], shown["source"], len(shown["positions"])) == ("credit_spread", "manual", 2)

    world.set_price("F", close)
    world.quote("F", w.PUT_EXPIRY, "50", "put", "0.50")
    world.quote("F", w.PUT_EXPIRY, "48", "put", "0.20")
    await world.postclose(w.PUT_EXPIRY)

    assert sorted(lifecycle(world)) == sorted(events)
    spread = await world.structure(spread_id)
    assert (spread.state, spread.reserved_cash) == ("closed", D(0))
    assert all(p.qty == 0 for p in spread.positions)
    left = await world.structures()
    assert sum(p.qty for s in left for p in s.positions if p.contract is None) == shares_left
    assert all(s.kind == "shares" and s.source == "manual" for s in left)
    account = await world.assert_reconciled()
    assert account.cash == opened.cash + cash_change
    assert account.reserved == 0
    (history,) = world.get("/positions", state="closed")["items"]
    assert history["id"] == spread_id


async def test_a_partial_close_leaves_the_reserve_of_what_remains(world: OptionsWorld) -> None:
    """Two units of the 50/48 put credit spread (reserve 400). Closing one unit leaves 200. Closing the
    long put of the last unit leaves a lone short 50 put: its reserve becomes the full 5,000."""
    ids, spread_id = await open_put_credit_spread(world, qty=2)
    short, long_ = ids[("50", "put")], ids[("48", "put")]
    before = await world.assert_reconciled()
    assert (before.reserved, before.cash) == (D(400), D(20_000) + 70 - 4 * FEE)

    one_unit = [world.leg(short, "buy", "close"), world.leg(long_, "sell", "close")]
    preview = world.post("/orders/preview", world.ticket(one_unit, intent="close", structure_id=spread_id))
    assert (preview["accepted"], D(preview["reserve_cash"])) == (True, D(200))
    order = world.submit(one_unit, intent="close", structure_id=spread_id)
    assert order["status"] == "working"
    await fill(world)
    half = await world.structure(spread_id)
    assert (half.state, half.reserved_cash) == ("open", D(200))
    assert sorted(p.qty for p in half.positions) == [-1, 1]
    after = await world.assert_reconciled()
    # bought the 50 put back at its 1.55 ask, sold the 48 put at its 1.10 bid: 0.45 a share, two fees
    assert (after.reserved, after.cash) == (D(200), before.cash - 45 - 2 * FEE)

    long_only = [world.leg(long_, "sell", "close")]
    order = world.submit(long_only, intent="close", structure_id=spread_id)
    assert order["status"] == "working"
    await fill(world)
    lone = await world.structure(spread_id)
    assert (lone.state, lone.reserved_cash) == ("open", D(5000))
    assert sorted(p.qty for p in lone.positions) == [-1, 0]
    last = await world.assert_reconciled()
    assert (last.reserved, last.cash) == (D(5000), after.cash + 110 - FEE)

    # Too much: the structure now holds one short put and nothing else.
    order = world.submit([world.leg(short, "buy", "close")], intent="close", structure_id=spread_id, qty=2)
    assert order["status"] == "rejected"
    await world.assert_reconciled()


# --- acceptance item 2: no uncovered short from the ticket --------------------------------------------------


async def test_no_naked_order_is_ever_accepted_from_the_ticket(world: OptionsWorld) -> None:
    """Attacks through the real API, broker and collateral engine. Each comes back `rejected` with its
    reason, is stored as a rejected order, reserves nothing and never fills."""
    nov = world.chain_ids(w.PUT_EXPIRY)
    dec = world.chain_ids(w.NEXT_EXPIRY)
    for expiry in (w.PUT_EXPIRY, w.NEXT_EXPIRY):
        for strike, bid in (("50", "6.00"), ("52", "4.50")):
            world.quote("F", expiry, strike, "call", bid)
    call50, call52, put50 = nov[("50", "call")], nov[("52", "call")], nov[("50", "put")]
    attacks: list[tuple[str, list[dict[str, Any]], dict[str, Any], set[str]]] = [
        ("a call with no shares", [world.leg(call50, "sell")], {}, {"naked_short"}),
        (
            "two calls sold against one bought",
            [world.leg(call50, "sell", ratio=2), world.leg(call52, "buy")],
            {},
            {"naked_short"},
        ),
        (
            "a short call that outlives its long call",
            [world.leg(dec[("50", "call")], "sell"), world.leg(call52, "buy")],
            {},
            {"naked_short"},
        ),
        (
            "puts beyond the cash",
            [world.leg(put50, "sell")],
            {"qty": 5},
            {"insufficient_cash", "position_cap"},
        ),
        (
            "a put beyond the cap for one underlying",
            [world.leg(put50, "sell")],
            {"qty": 3},
            {"position_cap"},
        ),
    ]
    for name, legs, fields, reasons in attacks:
        preview = world.post("/orders/preview", world.ticket(legs, **fields))
        assert preview["accepted"] is False and preview["reject_reason"] in reasons, name
        order = world.submit(legs, **fields)
        assert order["status"] == "rejected" and order["reject_reason"] in reasons, (name, order)
        assert D(order["reserved_cash"]) == 0, name

    # Covered at first, then attacked: 100 shares bought, one call sold against them.
    bought = world.submit([world.leg(None, "buy", ratio=100)])
    assert bought["status"] == "working"
    await fill(world)
    (shares,) = await world.structures()
    assert (shares.kind, sum(p.qty for p in shares.positions)) == ("shares", 100)
    covered = world.submit([world.leg(call52, "sell")])
    assert covered["status"] == "working"
    await fill(world)
    call = next(s for s in await world.structures() if s.kind == "covered_call")
    assert call.cover_structure_id == shares.id  # the cover link, stored by the broker
    later: list[tuple[str, list[dict[str, Any]], dict[str, Any], set[str]]] = [
        ("a second call on the same 100 shares", [world.leg(call50, "sell")], {}, {"naked_short"}),
        (
            "selling the shares a call relies on",
            [world.leg(None, "sell", "close", ratio=100)],
            {"intent": "close", "structure_id": shares.id},
            {"shares_committed", "not_covered_after_close"},
        ),
    ]
    for name, legs, fields, reasons in later:
        order = world.submit(legs, **fields)
        assert order["status"] == "rejected" and order["reject_reason"] in reasons, (name, order)

    # Nothing rejected ever fills, and the books show exactly the two accepted structures.
    await fill(world, expected=0)
    assert world.count(m.OptOrder, m.OptOrder.status == "rejected") == len(attacks) + len(later)
    assert world.count(m.OptOrder, m.OptOrder.status == "working") == 0
    assert sorted(s.kind for s in await world.structures()) == ["covered_call", "shares"]
    await world.assert_reconciled()


# --- acceptance item 3: conservative fills ------------------------------------------------------------------


@pytest.mark.parametrize(
    "block",
    ["out_of_hours", "stale", "delayed", "halted", "one_sided", "zero_bid", "limit_not_reached"],
)
async def test_fill_invariants_through_the_real_stack(world: OptionsWorld, block: str) -> None:
    """A marketable sell-to-open of the 50 put (limit 1.40, bid 1.50) does not fill while `block` holds,
    and fills at the bid, never above it, once it clears. Then the buy-to-close fills at the ask."""
    ids = world.chain_ids(w.PUT_EXPIRY)
    put = ids[("50", "put")]
    limit = "1.40"

    def good() -> None:
        world.qt.lag = timedelta(0)
        world.quote("F", w.PUT_EXPIRY, "50", "put", "1.50")

    if block == "out_of_hours":
        world.go(w.DAY0, 9, 0)
    elif block == "stale":
        world.qt.lag = timedelta(seconds=16)  # options.stale_quote_seconds is 15
    elif block == "delayed":
        world.quote("F", w.PUT_EXPIRY, "50", "put", "1.50", delay=15)
    elif block == "halted":
        world.quote("F", w.PUT_EXPIRY, "50", "put", "1.50", is_halted=True)
    elif block == "one_sided":
        world.qt.set_option_quote(world.ids[("F", w.PUT_EXPIRY, "50", "put")], "1.50", None)
    elif block == "zero_bid":
        world.quote("F", w.PUT_EXPIRY, "50", "put", "0", ask="0.05")
    else:
        limit = "1.60"  # above the bid: a sell waits for the bid to come up to it
    order = world.submit([world.leg(put, "sell")], order_type="limit", net_limit=limit, tif="gtc")
    assert order["status"] == "working", order
    await fill(world, expected=0)
    assert await world.structures() == []
    assert world.count(m.OptFill) == 0

    if block == "out_of_hours":
        world.go(w.DAY0, 9, 31)
    elif block == "limit_not_reached":
        world.quote("F", w.PUT_EXPIRY, "50", "put", "1.65")  # the bid passes the limit: filled at the bid
    else:
        good()
    await fill(world)
    bid = D("1.65") if block == "limit_not_reached" else D("1.50")
    (sold,) = await world.orders("filled")
    assert sold.fill_net == bid
    (short,) = await world.structures()
    with world.factory() as s:
        (row,) = s.execute(select(m.OptFill)).scalars()
        assert (row.side, row.price, D(row.quote["bid"])) == ("sell", bid, bid)

    # The way back: a buy limit well above the ask fills at the ask, never below it.
    world.quote("F", w.PUT_EXPIRY, "50", "put", "1.00", ask="1.20")
    back = world.submit(
        [world.leg(put, "buy", "close")],
        intent="close",
        structure_id=short.id,
        order_type="limit",
        net_limit="-2.00",
    )
    assert back["status"] == "working", back
    await fill(world)
    with world.factory() as s:
        bought = s.execute(select(m.OptFill).where(m.OptFill.side == "buy")).scalar_one()
        assert (bought.price, D(bought.quote["ask"])) == (D("1.20"), D("1.20"))
    account = await world.assert_reconciled()
    assert account.cash == D(20_000) + (bid - D("1.20")) * 100 - 2 * FEE
