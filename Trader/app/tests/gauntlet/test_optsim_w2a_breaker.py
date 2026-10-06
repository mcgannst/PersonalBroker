"""OPTSIM wave 2 checker A: breaker tests for T2 (Questrade option client), T3 (option market data), T5 (fill
model, book, broker) and T10 (owner prompts, the bot's `prompt` branch). Only what the builders' own tests do
not already prove. A test marked `xfail(strict=True)` pins a real defect: it turns red when the defect is
fixed, and the marker is then removed."""

import asyncio
import dataclasses
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.adapters.test_telegram_prompt_callback import CHAT, SIGNER, NoCommands, no_decide
from tests.fakes_telegram import FakeRenderer, FakeTelegramApi
from tests.options import factories as f
from tests.options.fakes import CannedCollateral, accept
from tests.options.test_broker import CAL, FEE, World, YieldingMarket
from tests.options.test_fill_model import BUY, assess, lq, order
from tests.options.test_market import F_QID, WEEKLY, put_id, setup
from trader.adapters.questrade.models import QtQuote
from trader.adapters.questrade.option_types import QtChainExpiry, QtChainRoot
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.notify.notifier import TelegramNotifier
from trader.options.broker import SimOptionBroker
from trader.options.fill_model import QuoteFillModel
from trader.options.messages import OptionMessages
from trader.options.prompts import DbPromptStore, PromptSender
from trader.options.settings import OptionSettings
from trader.options.types import NoFill, OwnerPromptRequest, PromptChoice, PromptView
from trader.settings_store import RuntimeSettings

D = Decimal


# --- T2 / T10: what the stock worker now imports ---


@pytest.mark.parametrize(
    "module",
    [
        "trader.adapters.questrade.client",  # now imports trader.options.types
        "trader.adapters.telegram.bot",  # now imports trader.options.prompts
        "trader.options.types",
        "trader.options.prompts",
    ],
)
def test_each_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    """No import cycle whichever of them the process happens to import first."""
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]


# --- T5: the fill model and the broker ---


class ShareQuoteMarket(YieldingMarket):
    """The share quote's `delay` and age can be set (the fake's is always live)."""

    share_delay: int | None = 0
    share_age = timedelta(0)

    async def underlying_quote(self, underlying: str) -> QtQuote | None:
        quote = await super().underlying_quote(underlying)
        assert quote is not None and quote.fetched_at is not None
        return dataclasses.replace(
            quote, delay=self.share_delay, fetched_at=quote.fetched_at - self.share_age
        )


@pytest.fixture
def w(db_factory: sessionmaker[Session]) -> World:
    """The builders' broker world: F at 14.79 / 14.81, the 14.50 put 0.45 / 0.50, the 14.00 put 0.30 / 0.35,
    the 15.00 call 1.10 / 1.20, and 5,000 of cash."""
    clock = FixedClock(f.T0)
    market = ShareQuoteMarket(clock)
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
                right=right,  # type: ignore[arg-type]
                underlying_symbol_id=symbol_id,
            )
            market.set_quote(cid, bid, ask)
            ids.append(cid)
        s.commit()
    world = World(db_factory, clock, market, CannedCollateral(accept()), None, run_id, *ids)  # type: ignore[arg-type]
    world.broker = world.new_broker()
    return world


def close_put(w: World, sid: int, qty: int = 1) -> Any:
    w.collateral.decision = accept(kind="csp", reserve_cash="0")
    return f.make_request(
        [f.make_leg(1, side="buy", effect="close", contract_id=w.put)],
        intent="close",
        structure_id=sid,
        order_type="market",
        qty=qty,
    )


def test_a_buy_never_fills_at_a_zero_ask() -> None:
    assert isinstance(assess(order([BUY]), [lq(1, "0", "0")]), NoFill)


@pytest.mark.db
async def test_take_profit_does_not_buy_back_for_free_on_a_zero_quote(w: World) -> None:
    await w.open_put(take_profit_pct=D("0.50"))
    w.collateral.decision = accept(kind="csp", reserve_cash="0")
    w.market.set_quote(w.put, "0", "0")  # no market at all (the first seconds of a session, a dead strike)
    await w.broker.take_profits(f.T0)
    assert await w.broker.poll(f.T0) == []
    assert len(await w.broker.structures()) == 1  # the put is still short


@pytest.mark.db
async def test_nothing_fills_outside_the_session_through_the_broker(w: World) -> None:
    """Live, real-time quotes all along: only the clock says no. The builders prove this in the fill model;
    this is the broker's own gate (weekend, the second before the open, the close itself)."""
    oid = (await w.broker.submit(w.sell_put(tif="gtc"))).order.id
    saturday = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
    before_open = f.T0 - timedelta(minutes=30, seconds=1)  # 09:29:59 ET
    at_close = f.T0 + timedelta(hours=6)  # 16:00:00 ET
    for now in (saturday, before_open, at_close):
        w.clock.set(now)
        assert await w.broker.poll(now) == []
    assert w.count(m.OptFill) == 0 and w.ledger() == [("deposit", D("5000"))]
    last_second = at_close - timedelta(seconds=1)
    w.clock.set(last_second)
    (event,) = await w.broker.poll(last_second)
    assert (event.order_id, event.net_price) == (oid, D("0.45"))


@pytest.mark.db
async def test_buy_write_needs_a_live_share_quote_and_books_both_legs(w: World) -> None:
    """A shares leg obeys the same rules from the share quote: delayed or stale, neither leg fills."""
    market = w.market
    assert isinstance(market, ShareQuoteMarket)
    w.collateral.decision = accept(kind="covered_call", reserve_cash="0")
    legs = [
        f.make_leg(1, instrument="shares", side="buy", ratio=100),
        f.make_leg(2, side="sell", contract_id=w.call),
    ]
    await w.broker.submit(f.make_request(legs, order_type="market"))
    for delay, age in ((None, timedelta(0)), (15, timedelta(0)), (0, timedelta(minutes=5))):
        market.share_delay, market.share_age = delay, age
        assert await w.broker.poll(f.T0) == []
    assert (w.count(m.OptFill), w.count(m.OptPosition), w.ledger()) == (0, 0, [("deposit", D("5000"))])

    market.share_delay, market.share_age = 0, timedelta(0)
    (event,) = await w.broker.poll(f.T0)
    settings = OptionSettings()
    fees = settings.share_commission + settings.fee_per_contract
    assert event.net_price == D("-13.71")  # 100 shares at the 14.81 ask, the call at its 1.10 bid
    assert [(leg.instrument, leg.qty, leg.price) for leg in event.legs] == [
        ("shares", 100, D("14.81")),
        ("option", 1, D("1.10")),
    ]
    moves = [row for row in w.ledger()[1:] if row[0] != "fee"]
    assert moves == [("buy", D("-1481")), ("sell", D("110"))]
    account = await w.broker.account()
    assert account.cash == D("5000") - D("1481") + D("110") - fees
    assert account.cash == sum(amount for _, amount in w.ledger())
    (structure,) = await w.broker.structures()
    assert sorted(p.qty for p in structure.positions) == [-1, 100]
    assert (structure.fees_total, event.fees) == (fees, fees)


@pytest.mark.db
async def test_a_three_lot_spread_charges_fees_once_and_keeps_free_cash_level(w: World) -> None:
    legs = [f.make_leg(1, contract_id=w.put), f.make_leg(2, side="buy", contract_id=w.low_put)]
    w.collateral.decision = accept(kind="credit_spread", reserve_cash="150")
    result = await w.broker.submit(f.make_request(legs, order_type="market", qty=3))
    assert result.order.reserved_cash == D("150") + 6 * FEE - D("30")  # the reserve and fees less the credit
    before = await w.broker.account()
    (event,) = await w.broker.poll(f.T0)
    assert (event.net_price, event.fees) == (D("0.10"), 6 * FEE)
    assert w.ledger()[1:] == [("sell", D("135")), ("buy", D("-105")), ("fee", -6 * FEE)]
    after = await w.broker.account()
    assert (after.cash, after.reserved) == (D("5000") + D("30") - 6 * FEE, D("150"))
    assert after.free_cash == before.free_cash
    with w.factory() as s:
        assert s.execute(select(func.sum(m.OptFill.fee))).scalar_one() == 6 * FEE
    # Nothing moves on a filled order: no cancel, no expiry, no second fill.
    assert await w.broker.cancel(result.order.id, "late", "test") is False
    assert await w.broker.expire_day_orders(f.SESSION, f.T0) == 0
    assert await w.broker.poll(f.T0) == [] and len(w.ledger()) == 4
    assert (await w.broker.account()).reserved == D("150")


@pytest.mark.db
async def test_poll_returns_the_fills_it_committed_when_a_later_order_fails(w: World) -> None:
    calls = 0

    def rate() -> Decimal:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("the database went away")
        return D("1.37")

    broker = SimOptionBroker(
        w.factory, w.clock, CAL, w.market, w.collateral, QuoteFillModel(), OptionSettings(), w.run_id, rate
    )
    first = (await broker.submit(w.sell_put())).order.id
    await broker.submit(w.sell_put())
    events = await broker.poll(f.T0)  # the second order's fill raises; the first one's is committed
    assert [e.order_id for e in events] == [first]


@pytest.mark.db
async def test_a_close_for_more_than_is_left_is_cancelled_not_raised(w: World) -> None:
    sid = await w.open_put(qty=3)
    first = (await w.broker.submit(close_put(w, sid, 2))).order.id
    second = (await w.broker.submit(close_put(w, sid, 2))).order.id  # both accepted while 3 are short
    last = (await w.broker.submit(w.sell_put())).order.id
    events = await w.broker.poll(f.T0)  # the second close can't fit: cancelled, not raised
    assert [e.order_id for e in events] == [first, last]
    stuck = await w.broker.order(second)
    assert stuck is not None and stuck.status == "cancelled"


@pytest.mark.db
async def test_poll_and_cancel_at_once_settle_one_way(w: World) -> None:
    oid = (await w.broker.submit(w.sell_put())).order.id
    events, cancelled = await asyncio.gather(
        w.broker.poll(f.T0), w.new_broker().cancel(oid, "changed my mind", "web:stephen")
    )
    stored = await w.broker.order(oid)
    assert stored is not None
    assert (cancelled, stored.status, len(events)) in {(True, "cancelled", 0), (False, "filled", 1)}
    assert (w.count(m.OptFill), w.count(m.OptStructure)) == (len(events), len(events))
    account = await w.broker.account()
    assert account.reserved == (D("1450") if events else 0)
    assert account.cash == sum(amount for _, amount in w.ledger())
    assert await w.broker.poll(f.T0) == []


@pytest.mark.db
async def test_two_brokers_taking_profits_at_once_submit_one_close(w: World) -> None:
    await w.open_put(take_profit_pct=D("0.50"))
    w.collateral.decision = accept(kind="csp", reserve_cash="0")
    w.market.set_quote(w.put, "0.15", "0.20")
    first, second = await asyncio.gather(w.broker.take_profits(f.T0), w.new_broker().take_profits(f.T0))
    assert len(first) + len(second) == 1
    assert len(await w.broker.orders(status="working")) == 1
    assert len(await w.broker.poll(f.T0)) == 1 and await w.broker.structures() == []


# --- T3: the chain cache ---


@pytest.mark.db
async def test_an_empty_chain_answer_keeps_the_stored_chain_and_expired_expiries_drop_out(
    db_factory: sessionmaker[Session],
) -> None:
    svc, qt, clock, _ = setup(db_factory)
    assert [e.expiry for e in await svc.expiries("F")] == [WEEKLY, f.EXPIRY]
    put = await put_id(svc)

    def contracts() -> int:
        with db_factory() as s:
            return int(s.execute(select(func.count()).select_from(m.OptionContract)).scalar_one())

    answers: Sequence[list[QtChainExpiry]] = (
        [],  # nothing at all
        [QtChainExpiry(f.EXPIRY, (QtChainRoot("F", 100, ()),))],  # an expiry without a strike
    )
    for answer in answers:
        clock.advance(timedelta(hours=25))  # the cache is stale: Questrade is asked
        qt.chains[F_QID] = answer
        assert [e.expiry for e in await svc.expiries("F")] == [WEEKLY, f.EXPIRY]
        assert contracts() == 8 and (await svc.contract(put)).strike == D("14.5")
    # The Monday after the weekly expired: the cached chain still lists it, the service does not offer it.
    clock.set(datetime(2026, 11, 16, 15, 0, tzinfo=UTC))
    (left,) = await svc.expiries("F")
    assert (left.expiry, left.dte, left.is_monthly) == (f.EXPIRY, 4, True)


# --- T10: owner prompts and the bot's `prompt` branch ---


@dataclass
class Tg:
    bot: TelegramBot
    api: FakeTelegramApi
    store: DbPromptStore
    sender: PromptSender
    clock: FixedClock
    prompt: PromptView

    async def send(self) -> tuple[int, dict[str, str]]:
        assert await self.sender.send_due(self.clock.now()) == 1
        sent = self.api.last_sent()
        return len(self.messages()), {b.text: b.callback_data for b in sent["buttons"][0]}

    async def tap(self, data: str, message_id: int, chat_id: int = CHAT) -> None:
        await self.bot.handle_update(self.api.callback_update(data, chat_id=chat_id, message_id=message_id))

    def messages(self) -> list[dict[str, Any]]:
        return self.api.calls_of("send_message")

    def answers(self) -> list[str | None]:
        return [kw["text"] for kw in self.api.calls_of("answer_callback")]

    def stored(self) -> PromptView:
        found = self.store.get(self.prompt.id)
        assert found is not None
        return found


@pytest.fixture
def tg(db_factory: sessionmaker[Session]) -> Tg:
    clock = FixedClock(f.T0)
    with db_factory() as s:
        run_id = f.add_options_run(s)
        s.commit()

    async def no_sleep(seconds: float) -> None:
        return None

    api = FakeTelegramApi()
    issuer = DbCallbackIssuer(db_factory, clock, SIGNER)
    store = DbPromptStore(db_factory, clock)
    notifier = TelegramNotifier(api, CHAT, db_factory, clock, sleep=no_sleep)
    sender = PromptSender(
        store, notifier, issuer, OptionMessages("https://t.example"), CHAT, OptionSettings, clock
    )
    bot = TelegramBot(
        api, CHAT, db_factory, clock, issuer, SIGNER, no_decide, NoCommands(), FakeRenderer(), run_id,
        settings=RuntimeSettings, sleep=no_sleep,
    )  # fmt: skip
    request = OwnerPromptRequest(
        kind="approve_ticker",
        scope_key="F",
        dedupe_key="wheel:approve:F",
        title="Approve F for the wheel?",
        body="F passed the screen.",
        choices=(PromptChoice("y", "Yes, approve"), PromptChoice("n", "No")),
    )
    return Tg(bot, api, store, sender, clock, store.ensure(run_id, "wheel", request))


@pytest.mark.db
async def test_a_prompt_tap_is_refused_from_another_chat_or_with_a_bad_signature_or_nonce(tg: Tg) -> None:
    message_id, data = await tg.send()
    real = data["Yes, approve"]
    nonce = real.split(":")[3]
    ref = str(tg.prompt.id)
    await tg.tap(real, message_id, chat_id=999)  # the right button, pressed in another chat
    assert tg.answers() == []  # not even answered
    await tg.tap(CallbackSigner.derive("another-secret").data("o", ref, "y", nonce), message_id)  # forged
    await tg.tap(SIGNER.data("o", ref, "y", "ZZZZzzzz"), message_id)  # a nonce that was never issued
    await tg.tap(SIGNER.data("o", str(tg.prompt.id + 1), "y", nonce), message_id)  # another prompt's id
    await tg.tap(SIGNER.data("p", ref, "a", nonce), message_id)  # the nonce re-used as a proposal approval
    assert len(tg.answers()) == 4 and tg.api.calls_of("edit_buttons") == []
    assert tg.stored().status == "pending"
    await tg.tap(real, message_id)  # none of that used the button up
    assert (tg.stored().answer, tg.stored().answered_via) == ("y", "telegram")


@pytest.mark.db
async def test_answered_on_the_web_first_then_tapped_in_telegram(tg: Tg) -> None:
    message_id, data = await tg.send()
    web = tg.store.answer(tg.prompt.id, "n", via="web", actor="web:stephen")
    assert web.status == "ok"
    await tg.tap(data["Yes, approve"], message_id)
    assert tg.answers() == ["Already answered"]
    assert [kw["message_id"] for kw in tg.api.calls_of("edit_buttons")] == [message_id]
    stored = tg.stored()
    assert (stored.answer, stored.answered_via) == ("n", "web")
    assert [p.id for p in tg.store.undelivered("wheel")] == [tg.prompt.id]  # delivered to the plug-in once
    tg.clock.advance(timedelta(hours=48))
    assert await tg.sender.send_due(tg.clock.now()) == 0 and len(tg.messages()) == 1  # no reminder


@pytest.mark.db
async def test_send_due_twice_at_once_sends_one_message(tg: Tg) -> None:
    now = tg.clock.now()
    await asyncio.gather(tg.sender.send_due(now), tg.sender.send_due(now))
    assert len(tg.messages()) == 1
    assert await tg.sender.send_due(now) == 0 and len(tg.messages()) == 1


@pytest.mark.db
async def test_a_crash_between_the_send_and_mark_sent_does_not_send_twice(
    tg: Tg, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = DbPromptStore.mark_sent
    monkeypatch.setattr(DbPromptStore, "mark_sent", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert await tg.sender.send_due(tg.clock.now()) == 0
    assert len(tg.messages()) == 1 and tg.stored().send_count == 0
    monkeypatch.setattr(DbPromptStore, "mark_sent", real)
    tg.clock.advance(timedelta(seconds=30))
    await tg.sender.send_due(tg.clock.now())  # the next pass of the worker's loop
    assert len(tg.messages()) == 1  # the same message is not sent again
    assert tg.stored().send_count == 1
