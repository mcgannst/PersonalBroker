"""OPTSIM T16, feature acceptance items 4 and 5 end to end: the wheel from an approved ticker to shares
called away, through the real worker, jobs, broker, collateral engine, lifecycle engine, wheel plug-in,
prompts and API (see `options_world`). The books are reconciled after every step."""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path

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
        yield made
        assert made.errors() == []  # no hook, job step or worker part failed anywhere in the script


def lifecycle_rows(world: OptionsWorld) -> list[m.OptLifecycleEvent]:
    with world.factory() as s:
        return list(s.execute(select(m.OptLifecycleEvent).order_by(m.OptLifecycleEvent.id)).scalars())


async def sell_first_put(world: OptionsWorld) -> None:
    """DAY0: the 08:15 refresh, the owner approves F, the 10:30 daily event sells the 50 put with a walking
    limit that starts at the midpoint (1.53) and fills when it reaches the 1.50 bid."""
    await world.refresh(w.DAY0)
    world.go(w.DAY0, 9, 0)
    world.approve("F")
    world.go(w.DAY0, 10, 30)
    reports = await world.run_until(w.DAY0, 10, 35)
    assert reports[0].events == (("wheel", "opt_daily", "succeeded"),)
    assert sum(r.fills for r in reports) == 1


async def test_the_wheel_from_an_approved_ticker_to_shares_called_away(world: OptionsWorld) -> None:
    # --- (a) screen -> put sold by a walking limit at the bid -> the 50% take-profit closes it -> NONE ---
    await sell_first_put(world)
    (order,) = await world.orders("filled")
    assert (order.source, order.walk, order.fill_net, order.fees) == ("wheel", True, D("1.50"), FEE)
    assert order.net_limit == D("1.50")  # walked down from 1.53, one tick a minute, never below the bid
    assert order.evidence["verdict"] == "QUALIFIED"
    assert [t["status"] for t in order.evidence["tests"]] == ["PASS"] * 10
    (put,) = await world.structures()
    assert (put.kind, put.source, put.reserved_cash, put.take_profit_net) == (
        "csp",
        "wheel",
        D(5000),
        D("-0.75"),
    )
    first = world.wheel_position()
    assert (first.state, first.put_structure_id, first.total_put_premium) == ("PUT_OPEN", put.id, D("1.50"))
    account = await world.assert_reconciled()
    assert (account.cash, account.reserved) == (D(20_000) + 150 - FEE, D(5000))

    await world.refresh(w.DAY1)
    world.go(w.DAY1, 10, 0)
    world.quote("F", w.PUT_EXPIRY, "50", "put", "0.65")  # ask 0.70: at or better than the 0.75 target
    await world.run_until(w.DAY1, 10, 2)
    closed = await world.structure(put.id)
    assert (closed.state, closed.close_reason, closed.reserved_cash) == ("closed", "closed", D(0))
    assert closed.realized_pnl == D(80) and closed.fees_total == 2 * FEE
    tp = next(o for o in await world.orders("filled") if o.intent == "close")
    assert (tp.reason, tp.submitted_by, tp.source, tp.fill_net) == (
        "take_profit",
        "take_profit",
        "wheel",
        D("-0.70"),
    )
    done = world.wheel_position(first.id)
    assert (done.state, done.close_reason) == ("NONE", "put_closed")
    assert done.full_cycle_result == D(80) - 2 * FEE
    account = await world.assert_reconciled()
    assert (account.cash, account.reserved) == (D(20_000) + 80 - 2 * FEE, D(0))
    cash_after_a = account.cash

    # --- (b) a second put -> assigned at expiry -> shares at the strike -> fresh cash yes -> call sold ->
    #         called away -> the full-cycle result -> the ticker is a candidate again ---
    world.quote("F", w.PUT_EXPIRY, "50", "put", "1.50")
    world.go(w.DAY1, 10, 30)
    reports = await world.run_until(w.DAY1, 10, 35)
    assert sum(r.fills for r in reports) == 1
    (put2,) = await world.structures()
    pos = world.wheel_position()
    assert pos.id != first.id and (pos.state, pos.put_structure_id) == ("PUT_OPEN", put2.id)
    await world.assert_reconciled()

    # Expiry day: F closes at 48, two dollars through the strike.
    await world.refresh(w.PUT_EXPIRY)
    world.set_price("F", "48")
    world.quote("F", w.PUT_EXPIRY, "50", "put", "2.00")
    await world.postclose(w.PUT_EXPIRY)
    (assigned,) = lifecycle_rows(world)
    assert (assigned.kind, assigned.structure_id, assigned.qty) == ("assigned", put2.id, 1)
    assert (assigned.strike, assigned.underlying_close) == (D(50), D(48))
    assert (assigned.shares_delta, assigned.cash_delta) == (100, D(-5000))
    assert assigned.delivered_at is not None  # the real engine's event reached the real plug-in
    (shares,) = await world.structures()
    assert assigned.new_structure_id == shares.id
    assert (shares.kind, shares.source, shares.qty, shares.entry_net) == ("shares", "wheel", 1, D(-50))
    assert (shares.parent_structure_id, shares.reserved_cash) == (put2.id, D(0))
    (lot,) = shares.positions
    assert (lot.instrument, lot.qty, lot.avg_price) == ("shares", 100, D(50))
    gone = await world.structure(put2.id)
    assert (gone.state, gone.close_reason, gone.realized_pnl) == ("closed", "assigned", D(150))
    pos = world.wheel_position()
    assert (pos.state, pos.shares_structure_id) == ("SHARES_HELD", shares.id)
    assert (pos.assignment_strike, pos.net_cost) == (D(50), D("48.50"))  # WS 9.1: strike less the premium
    account = await world.assert_reconciled()
    assert account.cash == cash_after_a + 150 - FEE - 5000
    assert account.equity == account.cash + 4800  # the shares at their bid, not at cost

    # The fresh-cash question is open on the Options page and went to Telegram with its buttons.
    (prompt,) = world.get("/prompts")["items"]
    assert (prompt["source"], prompt["kind"], prompt["status"]) == ("wheel", "fresh_cash", "pending")
    assert [c["code"] for c in prompt["choices"]] == ["y", "n"]
    asked = [c for c in world.telegram.calls_of("send_message") if c["buttons"]]
    assert len(asked) == 1 and prompt["title"] in asked[0]["text"]

    await world.refresh(w.CALL_DAY)
    world.set_price("F", "48.80")
    world.quote("F", w.CALL_EXPIRY, "50", "call", "0.60", delta="0.27")
    world.go(w.CALL_DAY, 10, 0)
    # The wheel's shares cover the wheel's calls only: the manual ticket can't sell a call against them.
    manual_call = world.chain_ids(w.CALL_EXPIRY)[("50", "call")]
    refused = world.submit([world.leg(manual_call, "sell")])
    assert (refused["status"], refused["reject_reason"], refused["source"]) == (
        "rejected",
        "naked_short",
        "manual",
    )
    answered = world.post(f"/prompts/{prompt['id']}/answer", {"choice": "y", "text": None})
    assert (answered["status"], answered["answer"], answered["answered_via"]) == ("answered", "y", "web")
    again = world.post(f"/prompts/{prompt['id']}/answer", {"choice": "n", "text": None}, status=409)
    assert again["error"]["code"] == "conflict"
    reports = await world.run_until(w.CALL_DAY, 10, 6)
    assert sum(r.fills for r in reports) == 1
    call = next(s for s in await world.structures() if s.kind == "covered_call")
    assert (call.source, call.cover_structure_id, call.reserved_cash) == ("wheel", shares.id, D(0))
    assert call.entry_net == D("0.60")
    pos = world.wheel_position()
    assert (pos.state, pos.call_structure_id, pos.fresh_cash_answer) == ("CALL_OPEN", call.id, True)
    account = await world.assert_reconciled()
    assert account.cash == cash_after_a + 150 - FEE - 5000 + 60 - FEE

    # The call's expiry: F closes at 51, the shares are called away at 50 from the lot the call relies on.
    await world.refresh(w.CALL_EXPIRY)
    world.set_price("F", "51")
    world.quote("F", w.CALL_EXPIRY, "50", "call", "1.00", delta="0.90")
    await world.postclose(w.CALL_EXPIRY)
    away = lifecycle_rows(world)[-1]
    assert (away.kind, away.structure_id, away.strike) == ("called_away", call.id, D(50))
    assert (away.shares_delta, away.cash_delta) == (-100, D(5000))
    assert await world.structures() == []
    for sid, reason in ((call.id, "called_away"), (shares.id, "called_away")):
        ended = await world.structure(sid)
        assert (ended.state, ended.close_reason) == ("closed", reason), sid
    result = world.wheel_position(pos.id)
    assert (result.state, result.close_reason) == ("NONE", "called_away")
    # WS 11: (1.50 put + 0.60 call + 50 sale - 50 assignment) x 100, less the two fills' fees
    assert result.full_cycle_result == D("208.02")
    ticker = world.wheel().ticker("F")
    assert ticker is not None and ticker.status == "candidate"  # a new approval is needed (R14)
    account = await world.assert_reconciled()
    assert account.cash == cash_after_a + D("208.02") == D("20286.04")
    assert (account.reserved, account.positions_value, account.equity) == (D(0), D(0), account.cash)

    # Messages (dedupe keys make each go out once): four fills, two lifecycle events, one prompt, and one
    # summary per post-close whose first line is the account value.
    texts = world.messages()
    assert len([t for t in texts if t.startswith("<b>OPTION FILL</b>")]) == 4
    assert any("Sell to open 1 F 2026-11-20 P 50.00 @ 1.50" in t for t in texts)
    assert any("Buy to close 1 F 2026-11-20 P 50.00 @ 0.70" in t for t in texts)
    assert any("Sell to open 1 F 2027-01-15 C 50.00 @ 0.60" in t for t in texts)
    with world.factory() as s:
        keys = list(s.execute(select(m.Notification.dedupe_key).order_by(m.Notification.id)).scalars())
    assert len(keys) == len(set(keys))
    kinds = [str(k).rsplit(":", 1)[0] for k in keys]
    assert kinds.count("opt:fill") == 4 and kinds.count("opt:life") == 2 and kinds.count("opt:summary") == 2
    summary = next(t for t in reversed(texts) if "20,286.04" in t)
    assert summary.splitlines()[0] == "<b>Options account value $20,286.04</b>"
    assigned_text = next(t for t in texts if t.startswith("<b>ASSIGNED</b>"))
    assert "Close 48.00, strike 50.00" in assigned_text and "Shares +100, cash -$5,000.00" in assigned_text
    away_text = next(t for t in texts if t.startswith("<b>CALLED AWAY</b>"))
    assert "Shares -100, cash +$5,000.00" in away_text
    question = next(t for t in texts if t.startswith("<b>QUESTION</b>"))
    assert "fresh-cash test (wheel)" in question


async def test_a_no_to_fresh_cash_sells_the_assigned_shares_through_the_broker(world: OptionsWorld) -> None:
    """The shares structure the lifecycle engine creates (qty = contracts, 100 shares a contract) is one
    the broker can close: the host's SellShares order sells all 100 at the bid and the books agree."""
    await sell_first_put(world)
    await world.refresh(w.PUT_EXPIRY)
    world.set_price("F", "48")
    world.quote("F", w.PUT_EXPIRY, "50", "put", "2.00")
    await world.postclose(w.PUT_EXPIRY)
    (shares,) = await world.structures()
    pos = world.wheel_position()
    assert (shares.kind, pos.state) == ("shares", "SHARES_HELD")
    monday = date(2026, 11, 23)
    await world.refresh(monday)
    world.set_price("F", "47.50")
    world.go(monday, 10, 0)
    (prompt,) = world.get("/prompts")["items"]
    world.post(f"/prompts/{prompt['id']}/answer", {"choice": "n", "text": None})
    reports = await world.run_until(monday, 10, 2)
    assert sum(r.fills for r in reports) == 1
    sale = next(o for o in await world.orders("filled") if o.intent == "close")
    (leg,) = sale.legs
    assert (leg.instrument, leg.side, leg.ratio, sale.qty, sale.order_type) == (
        "shares",
        "sell",
        100,
        1,
        "market",
    )
    assert sale.fill_net == D("47.50")  # per share, at the bid
    sold = await world.structure(shares.id)
    assert (sold.state, sold.realized_pnl) == ("closed", D(-250))
    assert [p.qty for p in sold.positions] == [0]
    assert await world.structures() == []
    done = world.wheel_position(pos.id)
    assert (done.state, done.close_reason) == ("NONE", "shares_sold")
    account = await world.assert_reconciled()
    # 150 of premium, 100 shares bought at 50 and sold at 47.50, one option fee (shares trade free)
    assert account.cash == D(20_000) + 150 - FEE - 5000 + 4750
    assert done.full_cycle_result == D(150) - 250 - FEE


async def test_roll_once_for_a_net_credit_then_take_assignment(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Preference ROLL_ONCE: at the time exit an in-the-money put is rolled to the next monthly in ONE
    two-leg order for a net credit; the same structure carries on; the second time it is not rolled."""
    params = {"manage_itm_at_time_exit_preference": "ROLL_ONCE"}
    async with w.open_world(db_factory, monkeypatch, tmp_path, wheel_params=params) as world:
        await sell_first_put(world)
        (put,) = await world.structures()

        await world.refresh(w.TIME_EXIT_DAY)
        world.set_price("F", "48.50")
        world.quote("F", w.PUT_EXPIRY, "50", "put", "2.55")  # ask 2.60
        world.quote("F", w.NEXT_EXPIRY, "50", "put", "3.10")
        world.go(w.TIME_EXIT_DAY, 10, 30)
        reports = await world.run_until(w.TIME_EXIT_DAY, 10, 32)
        assert sum(r.fills for r in reports) == 1
        assert world.wheel_actions()[-1] == "ROLL_PUT"
        roll = next(o for o in await world.orders("filled") if o.intent == "roll")
        closing, opening = roll.legs
        assert (closing.side, closing.effect, opening.side, opening.effect) == (
            "buy",
            "close",
            "sell",
            "open",
        )
        assert (roll.structure_id, roll.walk, roll.fill_net) == (put.id, False, D("0.50"))  # 3.10 - 2.60
        (rolled,) = await world.structures()
        assert (rolled.id, rolled.kind, rolled.reserved_cash) == (put.id, "csp", D(5000))
        held = [p for p in rolled.positions if p.qty != 0]
        assert [(p.qty, p.contract.expiry if p.contract else None) for p in held] == [(-1, w.NEXT_EXPIRY)]
        pos = world.wheel_position()
        assert (pos.state, pos.roll_count, pos.put_structure_id) == ("PUT_OPEN", 1, put.id)
        assert pos.total_put_premium == D("2.00")  # 1.50, less 2.60 paid, plus 3.10 received
        account = await world.assert_reconciled()
        assert account.cash == D(20_000) + 150 - FEE + 50 - 2 * FEE

        # The rolled put reaches its own time exit in the money: no second roll.
        second_exit = date(2026, 11, 30)  # 18 days before NEXT_EXPIRY
        await world.refresh(second_exit)
        world.quote("F", w.NEXT_EXPIRY, "50", "put", "2.55")
        world.quote("F", w.CALL_EXPIRY, "50", "put", "3.10")
        world.go(second_exit, 10, 30)
        reports = await world.run_until(second_exit, 10, 32)
        assert sum(r.fills for r in reports) == 0
        assert world.wheel_actions()[-1] == "TAKE_ASSIGNMENT"
        assert len([o for o in await world.orders() if o.intent == "roll"]) == 1
        assert world.wheel_position().roll_count == 1
        await world.assert_reconciled()
        assert world.errors() == []
