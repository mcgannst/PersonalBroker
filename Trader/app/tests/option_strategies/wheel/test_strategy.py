"""OPTSIM T11: the wheel plug-in, run through the real strategy host and registry with the T1 fakes for the
market, the broker and the prompt store; the wheel tables are the real ones.

The default ticker F passes all ten tests on 2026-10-06 with a 45-day monthly chain (puts at 50, 48 and 46)
and 20,000 of cash. `put_open`, `assigned` and `call_open` walk a position to each state of WS §6."""

import itertools
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.options import factories as f
from tests.options.fakes import FakeBook, FakeOptionBroker, FakeOptionMarket, FakePromptStore
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock, et_date
from trader.market.types import Candle
from trader.option_strategies.base import OptionEvent, PanelActionRequest
from trader.option_strategies.host import DefaultStrategyHost
from trader.option_strategies.registry import OptionStrategyRegistry
from trader.option_strategies.state import DbStrategyState
from trader.option_strategies.wheel import screener
from trader.option_strategies.wheel.config import WheelParams
from trader.option_strategies.wheel.store import PositionRow, WheelStore
from trader.option_strategies.wheel.strategy import ACCOUNT_SCOPE, WheelStrategy, pos_scope
from trader.options.settings import OptionSettingsStore
from trader.options.types import LifecycleEvent, LifecycleKind, OptionContract, OrderRequest

pytestmark = pytest.mark.db
CAL = SessionCalendar()
D = Decimal
DAY0 = f.SESSION  # Tue 2026-10-06
PUT_EXPIRY = f.EXPIRY  # 2026-11-20, 45 days out
NEXT_EXPIRY = date(2026, 12, 18)
CALL_EXPIRY = date(2027, 1, 15)
TIME_EXIT_DAY = date(2026, 11, 2)  # 18 days before PUT_EXPIRY
CALL_DAY = date(2026, 12, 7)  # CALL_EXPIRY is 39 days out
FACTS: dict[str, Any] = {
    "security_type": "STOCK",
    "sector": "Technology",
    "eps_ttm": D(3),
    "eps_growth_yoy": D("0.10"),
    "debt_to_equity": D("0.5"),
    "book_value_per_share": D(20),
    "market_cap_usd": D(50_000_000_000),
    "sma50": D(52),
    "sma50_prior": D(51),
    "low_52w": D(40),
    "sessions_since_52w_low": 150,
    "rsi14": D(55),
    "next_earnings_date": date(2027, 2, 10),
}
_LIFE = itertools.count(1)


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 16, tzinfo=UTC)  # late morning ET, inside the session


@dataclass
class Env:
    factory: sessionmaker[Session]
    clock: FixedClock
    market: FakeOptionMarket
    broker: FakeOptionBroker
    prompts: FakePromptStore
    registry: OptionStrategyRegistry
    host: DefaultStrategyHost
    store: WheelStore
    run_id: int
    symbols: dict[str, int]
    puts: dict[str, OptionContract] = field(default_factory=dict)
    alerts: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def book(self) -> FakeBook:
        return self.broker.book

    def go(self, day: date) -> None:
        self.clock.set(at(day))

    async def fire(self, key: str, *, force: bool = False) -> None:
        event = OptionEvent(key, et_date(self.clock.now()), scheduled=False)
        outcome = await self.host.fire("wheel", event, force=force)
        assert outcome.status == "succeeded", outcome

    async def daily(self, *, force: bool = False) -> None:
        await self.fire("opt_daily", force=force)

    def add_stock(self, ticker: str, price: str = "55", **facts: Any) -> None:
        """An underlying that passes every test, with the three puts of the 45-day monthly."""
        with self.factory() as s:
            self.symbols[ticker] = f.add_underlying(s, ticker)
            s.commit()
        self.market.add_underlying(ticker, price, symbol_id=self.symbols[ticker])
        self.market.set_facts(ticker, **{**FACTS, **facts})
        for strike, delta, bid in (("50", "-0.30", "1.50"), ("48", "-0.25", "1.10"), ("46", "-0.20", "0.85")):
            put = self.market.add_contract(ticker, PUT_EXPIRY, strike, "put")
            self.quote(put, bid, delta=delta)
            self.puts[f"{ticker}{strike}"] = put

    def quote(self, contract: OptionContract, bid: str, *, delta: str = "-0.30") -> None:
        """A liquid quote five cents wide."""
        ask = str(D(bid) + D("0.05"))
        self.market.set_quote(contract.id, bid, ask, delta=delta, iv="0.30", open_interest=1000)

    def approve(self, ticker: str = "F") -> None:
        self.store.add_ticker(self.symbols[ticker], ticker, origin="manual", actor="test")
        self.store.update_ticker(
            ticker, "test", status="approved", would_own=True, ownership_reason="a business I want to hold"
        )

    def mirror(self) -> None:
        """The fake book's structures as `opt_structures` rows with the same ids (the wheel tables' FKs)."""
        with self.factory() as s:
            have = s.execute(select(func.count()).select_from(m.OptStructure)).scalar_one()
            for view in self.book.structures(open_only=False)[have:]:
                row_id = f.add_structure(
                    s,
                    self.run_id,
                    self.symbols[view.underlying],
                    underlying=view.underlying,
                    source=view.source,
                )
                assert row_id == view.id
            s.commit()

    async def fill(self) -> int:
        """Fill every working order at the market and deliver the fills; how many filled."""
        fills = await self.broker.fill_all_at_market()
        self.mirror()
        for fill in fills:
            await self.host.deliver_fill(fill)
        return len(fills)

    def settle(self, structure_id: int | None) -> None:
        """Take a structure's positions to zero and close it, as the lifecycle engine would."""
        assert structure_id is not None
        now = self.clock.now()
        for p in self.book.structure(structure_id).positions:
            if p.qty != 0:
                contract_id = None if p.contract is None else p.contract.id
                self.book.apply(structure_id, p.instrument, contract_id, -p.qty, D(0), now)
        self.book.close_structure(structure_id, "expired", now)

    async def lifecycle(
        self,
        structure_id: int | None,
        kind: LifecycleKind,
        *,
        strike: str | None = None,
        new_structure_id: int | None = None,
    ) -> None:
        assert structure_id is not None
        contract = next((p.contract for p in self.book.structure(structure_id).positions if p.contract), None)
        self.settle(structure_id)
        self.mirror()
        await self.host.deliver_lifecycle(
            LifecycleEvent(
                id=next(_LIFE),
                structure_id=structure_id,
                source="wheel",
                strategy_config_id=None,
                kind=kind,
                session_date=et_date(self.clock.now()),
                ts=self.clock.now(),
                contract=contract,
                qty=1,
                strike=None if strike is None else D(strike),
                underlying_close=None,
                shares_delta=0,
                cash_delta=D(0),
                new_structure_id=new_structure_id,
            )
        )

    async def answer(self, key: str, choice: str, text: str | None = None) -> None:
        await self.host.sync_prompts()
        prompt = self.prompts.by_key(key)
        assert prompt is not None, [p.dedupe_key for p in self.prompts.rows.values()]
        done = self.prompts.answer(prompt.id, choice, text=text, via="web", actor="web:stephen")
        assert done.status == "ok"
        assert await self.host.deliver_answers() == 1

    def pos(self, position_id: int | None = None) -> PositionRow:
        if position_id is None:
            (only,) = self.store.open_positions()
            return only
        found = self.store.position(position_id)
        assert found is not None
        return found

    def actions(self) -> list[str]:
        return [e.action for e in self.store.events() if e.action is not None]

    def requests(self) -> list[str]:
        return [kind_of(self, req) for req in self.broker.submitted]

    def errors(self) -> list[str]:
        with self.factory() as s:
            rows = s.execute(select(m.EventLog).where(m.EventLog.level == "error")).scalars()
            return [f"{row.message} {row.data}" for row in rows]


def state(env: Env) -> DbStrategyState:
    return DbStrategyState(env.factory, env.clock, "wheel")


def kind_of(env: Env, req: OrderRequest) -> str:
    """What an order request is, in the wheel's words."""
    if req.intent == "roll":
        return "roll"
    leg = req.legs[0]
    if req.intent == "close":
        return "sell_shares" if leg.instrument == "shares" else "close"
    assert leg.contract_id is not None
    return f"open_{env.market.contracts[leg.contract_id].right}"


def make_env(db_factory: sessionmaker[Session], *, cash: int = 20_000, **params: Any) -> Env:
    clock = FixedClock(at(DAY0))
    with db_factory() as s:
        run_id = f.add_options_run(s)
        s.commit()
    market = FakeOptionMarket(clock)
    broker = FakeOptionBroker(market, FakeBook(cash, market.contracts))
    prompts = FakePromptStore(clock)
    registry = OptionStrategyRegistry(db_factory, clock, plugins={"wheel": WheelStrategy})
    registry.ensure_defaults()
    if params:
        registry.update("wheel", params=params, actor="test")
    alerts: list[tuple[str, str, str, str]] = []

    async def sink(source: str, kind: str, message: str, dedupe_key: str) -> None:
        alerts.append((source, kind, message, dedupe_key))

    host = DefaultStrategyHost(
        db_factory,
        clock,
        CAL,
        registry,
        broker,
        market,
        prompts,
        OptionSettingsStore(db_factory, clock.now),
        lambda: run_id,
        sink,
    )
    env = Env(
        db_factory,
        clock,
        market,
        broker,
        prompts,
        registry,
        host,
        WheelStore(db_factory, clock, run_id),
        run_id,
        {},
        alerts=alerts,
    )
    env.add_stock("F")
    return env


EnvFactory = Callable[..., Env]


@pytest.fixture
def build(db_factory: sessionmaker[Session]) -> Iterator[EnvFactory]:
    """Builds the environment; afterwards no hook may have failed (the host turns a raising hook into an
    error event instead of raising)."""
    made: list[Env] = []

    def factory(**options: Any) -> Env:
        made.append(make_env(db_factory, **options))
        return made[-1]

    yield factory
    for env in made:
        assert env.errors() == []


@pytest.fixture
def env(build: EnvFactory) -> Env:
    return build()


# --- stages of the WS §6 state machine ----------------------------------------------------------------------


async def put_open(env: Env) -> PositionRow:
    """F approved; the 50 put sold for 1.50 on day 0."""
    env.approve()
    await env.daily()
    assert await env.fill() == 1
    return env.pos()


async def assigned(env: Env) -> PositionRow:
    """The put assigned at 50 on its expiry day: 100 shares in a new structure."""
    pos = await put_open(env)
    env.go(PUT_EXPIRY)
    env.market.set_price("F", "48")
    shares = env.book.add_structure(
        kind="shares",
        source="wheel",
        strategy_config_id=None,
        underlying="F",
        qty=1,
        entry_net=D(-50),
        reserved_cash=D(0),
        cover_structure_id=None,
        parent_structure_id=None,
        take_profit_net=None,
        meta={},
        ts=env.clock.now(),
    )
    env.book.apply(shares, "shares", None, 100, D(50), env.clock.now())
    await env.lifecycle(pos.put_structure_id, "assigned", strike="50", new_structure_id=shares)
    return env.pos()


def add_call(env: Env, *, delta: str = "0.27", bid: str = "0.60") -> OptionContract:
    call = env.market.add_contract("F", CALL_EXPIRY, "50", "call")
    env.quote(call, bid, delta=delta)
    return call


async def call_open(env: Env) -> PositionRow:
    """Assigned, then on CALL_DAY the fresh-cash test is answered yes and the 50 call is sold for 0.60."""
    pos = await assigned(env)
    env.go(CALL_DAY)
    env.market.set_price("F", "48.80")
    add_call(env)
    await env.answer(f"wheel:fresh:{pos.id}:{PUT_EXPIRY}", "y")
    assert await env.fill() == 1
    return env.pos()


# --- entry --------------------------------------------------------------------------------------------------


async def test_daily_opens_a_put_for_a_qualified_approved_ticker(env: Env) -> None:
    env.approve()
    await env.daily()

    (req,) = env.broker.submitted
    assert (req.source, req.intent, req.underlying, req.qty) == ("wheel", "open", "F", 1)
    (leg,) = req.legs
    assert (leg.side, leg.effect, leg.ratio, leg.contract_id) == ("sell", "open", 1, env.puts["F50"].id)
    assert (req.order_type, req.net_limit, req.tif, req.walk) == ("limit", None, "day", True)
    assert req.take_profit_pct == D("0.50")
    assert req.evidence["verdict"] == "QUALIFIED"
    assert [t["status"] for t in req.evidence["tests"]] == ["PASS"] * 10
    assert (req.evidence["chosen"]["strike"], req.evidence["breakeven"]) == ("50", "48.50")
    ticker = env.store.ticker("F")
    assert ticker is not None and ticker.last_verdict == "QUALIFIED"
    assert [e.reason for e in env.store.events(kind="screen")] == ["QUALIFIED"]

    await env.daily(force=True)  # re-run after a crash: the working order is not sent twice
    assert len(env.broker.submitted) == 1


async def test_a_fixed_limit_at_the_midpoint_when_orders_are_not_walked(build: EnvFactory) -> None:
    env = build(walk_orders=False)
    env.approve()
    await env.daily()
    (req,) = env.broker.submitted
    assert (req.walk, req.net_limit) == (False, D("1.53"))  # the 1.525 midpoint, rounded in our favour


@pytest.mark.parametrize(
    "case", ["rejected", "candidate_prompt_pending", "position_open", "paused", "caution", "cash"]
)
async def test_no_entry(build: EnvFactory, case: str) -> None:
    env = build(cash=4_000) if case == "cash" else build()
    if case == "rejected":
        env.approve()
        env.store.update_ticker("F", "test", status="rejected", would_own=False)
    elif case == "candidate_prompt_pending":
        env.store.add_ticker(env.symbols["F"], "F", origin="screen", actor="test")
        await env.daily()
        await env.host.sync_prompts()
        asked = env.prompts.by_key("wheel:cand:F")
        assert asked is not None and asked.status == "pending"
        env.go(date(2026, 10, 7))
    elif case == "position_open":  # WS §13 case 16
        await put_open(env)
        env.go(date(2026, 10, 7))
    elif case == "paused":
        env.approve()
        state(env).put(ACCOUNT_SCOPE, {"new_positions_paused": True})
    elif case == "caution":
        env.market.set_facts("F", eps_growth_yoy=None)  # test 2 CAUTION: NEEDS_REVIEW until acknowledged
        env.approve()
    else:
        env.approve()
    before = len(env.broker.submitted)
    await env.daily()
    assert len(env.broker.submitted) == before
    if case == "caution":
        ticker = env.store.ticker("F")
        assert ticker is not None and ticker.last_verdict == "NEEDS_REVIEW"
    if case == "cash":
        ticker = env.store.ticker("F")
        assert ticker is not None and ticker.last_verdict == "NOT_A_CANDIDATE"


async def test_manual_position_on_the_ticker_does_not_block(env: Env) -> None:
    env.approve()
    manual = f.make_request((f.make_leg(contract_id=env.puts["F46"].id),), source="manual")
    order = (await env.broker.submit(manual)).order
    env.broker.fill(order.id, {1: D("0.85")})
    await env.daily()
    assert env.requests() == ["open_put", "open_put"]  # the manual one, then the wheel's
    assert env.broker.submitted[-1].source == "wheel"


async def test_manual_reservations_count_as_collateral(env: Env) -> None:
    env.approve()
    env.book.add_structure(
        kind="csp",
        source="manual",
        strategy_config_id=None,
        underlying="F",
        qty=1,
        entry_net=D("1"),
        reserved_cash=D(16_000),  # leaves 4,000 of the 20,000: the 5,000 collateral no longer fits
        cover_structure_id=None,
        parent_structure_id=None,
        take_profit_net=None,
        meta={},
        ts=env.clock.now(),
    )
    await env.daily()
    assert env.broker.submitted == []
    ticker = env.store.ticker("F")
    assert ticker is not None and ticker.last_screen is not None
    assert ticker.last_screen["tests"][6]["status"] == "FAIL"


async def test_entries_follow_the_rank_and_stop_when_the_cash_is_used(build: EnvFactory) -> None:
    env = build(cash=9_000, entry_per_ticker_limit_pct_of_wheel_cash=None)  # room for one put
    env.add_stock("G", rsi14=D(74))  # trend CAUTION: QUALIFIED_WITH_CONDITION, ranked after F
    env.approve("G")
    env.approve("F")
    await env.daily()
    assert [req.underlying for req in env.broker.submitted] == ["F"]


async def test_stale_quotes_flagged_not_blocking(env: Env) -> None:
    env.market.open = False
    env.approve()
    await env.daily()
    (req,) = env.broker.submitted
    assert req.evidence["stale"] is True and "VERIFY_LIVE" in req.evidence["flags"]
    panel = await env.host.panel("wheel")
    tickers = next(t for t in panel.tables if t.key == "tickers")
    assert any("verify live" in kv.value for kv in tickers.rows[0].detail)


# --- the action table ---------------------------------------------------------------------------------------

Step = Callable[[Env], Awaitable[None]]


def put_case(day: date, **change: Any) -> Step:
    """From PUT_OPEN: move to `day`, change the inputs, run the daily event."""

    async def run(env: Env) -> None:
        await put_open(env)
        env.go(day)
        if "price" in change:
            env.market.set_price("F", change["price"])
        if "ask" in change:
            env.quote(env.puts["F50"], str(D(change["ask"]) - D("0.05")))
        if "facts" in change:
            env.market.set_facts("F", **change["facts"])
        if change.get("thesis_broken"):
            env.store.update_ticker("F", "test", thesis_broken=True)
        if change.get("next_put"):
            env.quote(env.market.add_contract("F", NEXT_EXPIRY, "50", "put"), change["next_put"])
        await env.daily()

    return run


def shares_case(*, call_delta: str | None = None, thesis_broken: bool = False) -> Step:
    """From SHARES_HELD on CALL_DAY: the fresh-cash test answered yes (the answer is applied at once)."""

    async def run(env: Env) -> None:
        pos = await assigned(env)
        env.go(CALL_DAY)
        if call_delta is not None:
            add_call(env, delta=call_delta, bid="0.60" if call_delta == "0.27" else "0.02")
        if thesis_broken:
            env.store.update_ticker("F", "test", thesis_broken=True)
        await env.answer(f"wheel:fresh:{pos.id}:{PUT_EXPIRY}", "y")

    return run


def call_case(day: date, **change: Any) -> Step:
    async def run(env: Env) -> None:
        pos = await call_open(env)
        env.go(day)
        if "price" in change:
            env.market.set_price("F", change["price"])
        if "ask" in change:
            (held,) = env.book.structure(pos.call_structure_id or 0).positions
            assert held.contract is not None
            env.quote(held.contract, str(D(change["ask"]) - D("0.05")), delta="0.27")
        if "facts" in change:
            env.market.set_facts("F", **change["facts"])
        if change.get("thesis_broken"):
            env.store.update_ticker("F", "test", thesis_broken=True)
        await env.daily()

    return run


NEXT_DAY = date(2026, 10, 7)
CALL_EXIT_DAY = date(2026, 12, 28)  # 18 days before CALL_EXPIRY
ACTION_TABLE: dict[str, tuple[Step, dict[str, Any], str | None]] = {
    "HOLD": (put_case(NEXT_DAY), {}, None),
    "CLOSE_PUT_PROFIT": (put_case(NEXT_DAY, ask="0.70"), {}, "close"),
    "CLOSE_PUT_TIME": (put_case(TIME_EXIT_DAY), {}, "close"),
    "CLOSE_PUT_BEFORE_EARNINGS": (
        put_case(NEXT_DAY, ask="1.20", facts={"next_earnings_date": date(2026, 11, 10)}),
        {},
        "close",
    ),
    "CLOSE_PUT_NOW": (put_case(NEXT_DAY, thesis_broken=True), {}, "close"),
    "TAKE_ASSIGNMENT": (put_case(TIME_EXIT_DAY, price="48"), {}, None),
    "ROLL_PUT": (
        put_case(TIME_EXIT_DAY, price="48.50", ask="2.60", next_put="3.10"),
        {"manage_itm_at_time_exit_preference": "ROLL_ONCE"},
        "roll",
    ),
    "SELL_CALL": (shares_case(call_delta="0.27"), {}, "open_call"),
    "HOLD_UNCOVERED": (shares_case(call_delta="0.05"), {}, None),
    "SKIP_CALL_THIS_CYCLE": (shares_case(), {}, None),
    "SELL_SHARES": (shares_case(thesis_broken=True), {}, "sell_shares"),
    "CLOSE_CALL_PROFIT": (call_case(date(2026, 12, 8), ask="0.25"), {}, "close"),
    "CLOSE_CALL_AND_SELL_SHARES": (call_case(date(2026, 12, 8), thesis_broken=True), {}, "close"),
    "EARLY_ASSIGNMENT_RISK": (
        call_case(date(2026, 12, 8), price="51", facts={"next_ex_dividend_date": date(2026, 12, 20)}),
        {},
        None,
    ),
    "HOLD_FOR_CALL_AWAY": (call_case(CALL_EXIT_DAY, price="51"), {}, None),
    "CLOSE_OR_EXPIRE_CALL": (call_case(CALL_EXIT_DAY, price="49"), {}, "close"),
}


@pytest.mark.parametrize("action", sorted(ACTION_TABLE))
async def test_action_to_intent(build: EnvFactory, action: str) -> None:
    step, params, expected = ACTION_TABLE[action]
    env = build(**params)
    await step(env)
    assert env.actions()[-1] == action
    # The order an action sends carries its name as the reason; an action without an order sends none.
    last = env.broker.submitted[-1]
    sent = kind_of(env, last) if last.reason.startswith(f"wheel: {action}") else None
    assert sent == expected
    if expected in ("close", "open_call"):
        assert (last.order_type, last.walk, last.net_limit, last.tif) == ("limit", True, None, "day")
    if expected == "open_call":
        assert last.take_profit_pct == D("0.50") and last.qty == 1
    if expected == "sell_shares":
        assert last.order_type == "market"
    if expected == "roll":
        closing, opening = last.legs
        assert (closing.side, closing.effect, opening.side, opening.effect) == (
            "buy",
            "close",
            "sell",
            "open",
        )
        assert last.net_limit is not None and last.net_limit > 0 and last.walk is False
    if action == "EARLY_ASSIGNMENT_RISK":
        assert [a[1] for a in env.alerts] == ["EX_DIVIDEND_AHEAD"]


async def test_shares_are_sold_from_on_fill_once_the_call_is_closed(env: Env) -> None:
    await call_case(date(2026, 12, 8), thesis_broken=True)(env)
    assert await env.fill() == 1  # the call is bought back
    assert env.pos().state == "SHARES_HELD"
    assert env.requests()[-1] == "sell_shares"


async def test_evaluated_once_per_session(env: Env) -> None:
    pos = await put_open(env)
    env.go(NEXT_DAY)
    env.quote(env.puts["F50"], "0.65")
    await env.daily()
    await env.daily(force=True)
    evaluations = env.store.events(kind="evaluate", position_id=pos.id)
    assert [(e.session_date, e.action) for e in evaluations] == [(NEXT_DAY, "CLOSE_PUT_PROFIT")]
    assert env.requests() == ["open_put", "close"]


# --- the state machine --------------------------------------------------------------------------------------


async def _put_closed(env: Env) -> int:
    pos = await put_open(env)
    env.go(NEXT_DAY)
    env.quote(env.puts["F50"], "0.65")
    await env.daily()
    assert await env.fill() == 1
    return pos.id


async def _put_expired(env: Env) -> int:
    pos = await put_open(env)
    env.go(PUT_EXPIRY)
    await env.lifecycle(pos.put_structure_id, "expired")
    return pos.id


async def _call_closed(env: Env) -> int:
    pos = await call_open(env)
    env.go(date(2026, 12, 8))
    (held,) = env.book.structure(pos.call_structure_id or 0).positions
    assert held.contract is not None
    env.quote(held.contract, "0.20", delta="0.27")
    await env.daily()
    assert await env.fill() == 1
    return pos.id


async def _call_expired(env: Env) -> int:
    pos = await call_open(env)
    env.go(CALL_EXPIRY)
    await env.lifecycle(pos.call_structure_id, "expired")
    return pos.id


async def _shares_sold(env: Env) -> int:
    pos = await assigned(env)
    await env.answer(f"wheel:fresh:{pos.id}:{PUT_EXPIRY}", "n")
    assert await env.fill() == 1
    return pos.id


async def _called_away(env: Env) -> int:
    pos = await call_open(env)
    env.go(CALL_EXPIRY)
    env.settle(pos.shares_structure_id)
    await env.lifecycle(pos.call_structure_id, "called_away", strike="50")
    return pos.id


async def _id(pos: Awaitable[PositionRow]) -> int:
    return (await pos).id


TRANSITIONS: dict[str, tuple[Callable[[Env], Awaitable[int]], str, str | None]] = {
    "NONE->PUT_OPEN": (lambda env: _id(put_open(env)), "PUT_OPEN", None),
    "PUT_OPEN->NONE closed": (_put_closed, "NONE", "put_closed"),
    "PUT_OPEN->NONE expired": (_put_expired, "NONE", "put_expired"),
    "PUT_OPEN->SHARES_HELD": (lambda env: _id(assigned(env)), "SHARES_HELD", None),
    "SHARES_HELD->CALL_OPEN": (lambda env: _id(call_open(env)), "CALL_OPEN", None),
    "SHARES_HELD->NONE": (_shares_sold, "NONE", "shares_sold"),
    "CALL_OPEN->SHARES_HELD closed": (_call_closed, "SHARES_HELD", None),
    "CALL_OPEN->SHARES_HELD expired": (_call_expired, "SHARES_HELD", None),
    "CALL_OPEN->NONE": (_called_away, "NONE", "called_away"),
}


@pytest.mark.parametrize("edge", sorted(TRANSITIONS))
async def test_fill_and_lifecycle_transitions(env: Env, edge: str) -> None:
    walk, state, reason = TRANSITIONS[edge]
    pos = env.pos(await walk(env))
    assert (pos.state, pos.close_reason, pos.closed_at is None) == (state, reason, state != "NONE")
    if edge.startswith("CALL_OPEN->SHARES_HELD"):  # a new call needs a new fresh-cash answer
        assert pos.fresh_cash_answer is None and pos.call_structure_id is None
    if state == "NONE":
        assert pos.full_cycle_result is not None


async def test_put_sold_records_the_entry(env: Env) -> None:
    pos = await put_open(env)
    assert (pos.contracts, pos.total_put_premium, pos.fees) == (1, D("1.50"), D("0.99"))
    assert pos.entry == {
        "open_date": "2026-10-06",
        "ticker": "F",
        "expiry": "2026-11-20",
        "strike": "50",
        "contracts": 1,
        "premium": "1.50",
        "breakeven": "48.50",
        "delta_at_entry": "-0.30",
        "iv_at_entry": "0.30",
        "next_earnings_date": "2027-02-10",
        "ownership_reason": "a business I want to hold",
        "usd_cad_rate": "1",
    }


async def test_net_cost_and_full_cycle_result_recorded(env: Env) -> None:
    pos = await call_open(env)
    assert (pos.assignment_strike, pos.net_cost, pos.total_call_premium) == (D(50), D("48.50"), D("0.60"))
    done = env.pos(await _called_away_from(env, pos))
    # (1.50 put + 0.60 call + 50 sale - 50 assignment) x 100, less two fills at 0.99
    assert done.full_cycle_result == D("208.02")


async def _called_away_from(env: Env, pos: PositionRow) -> int:
    env.go(CALL_EXPIRY)
    env.settle(pos.shares_structure_id)
    await env.lifecycle(pos.call_structure_id, "called_away", strike="50")
    return pos.id


async def test_called_away_ticker_needs_new_approval(env: Env) -> None:
    await _called_away(env)
    ticker = env.store.ticker("F")
    assert ticker is not None and (ticker.status, ticker.last_screen) == ("candidate", None)
    env.go(date(2027, 1, 19))
    env.market.set_facts("F", next_earnings_date=date(2027, 4, 20))
    env.quote(env.market.add_contract("F", date(2027, 2, 19), "50", "put"), "1.50")
    sent = len(env.broker.submitted)
    await env.daily()
    assert len(env.broker.submitted) == sent  # screened again, not traded
    again = env.store.ticker("F")
    assert again is not None and again.last_verdict == "QUALIFIED_WITH_CONDITION"  # entry would be allowed
    await env.host.sync_prompts()
    asked = env.prompts.by_key("wheel:cand:F:1")  # a new question: the first cycle's key is used up
    assert asked is not None and asked.status == "pending"


async def test_roll_is_net_credit_and_counts(build: EnvFactory) -> None:
    env = build(manage_itm_at_time_exit_preference="ROLL_ONCE")
    await put_case(TIME_EXIT_DAY, price="48.50", ask="2.60", next_put="3.10")(env)
    roll = env.broker.submitted[-1]
    assert roll.net_limit == D("0.01") and roll.evidence["net_credit"] == "0.50"
    assert await env.fill() == 1
    pos = env.pos()
    assert (pos.state, pos.roll_count, pos.total_put_premium) == ("PUT_OPEN", 1, D("2.00"))
    assert (pos.entry["expiry"], pos.entry["premium"]) == ("2026-12-18", "3.10")

    env.go(date(2026, 11, 30))  # 18 days before the new expiry, still in the money: no second roll
    env.quote(env.market.add_contract("F", CALL_EXPIRY, "50", "put"), "4.00")
    await env.daily()
    assert env.actions()[-1] == "TAKE_ASSIGNMENT"
    assert env.requests().count("roll") == 1


# --- prompts and answers ------------------------------------------------------------------------------------


async def ask_candidate(env: Env) -> str:
    env.store.add_ticker(env.symbols["F"], "F", origin="manual", actor="test")
    await env.daily()
    return "wheel:cand:F"


async def ask_ack(env: Env) -> str:
    env.market.set_facts("F", eps_growth_yoy=None)
    env.approve()
    await env.daily()
    return "wheel:ack:F:profitability"


async def ask_fresh(env: Env) -> str:
    pos = await assigned(env)
    env.go(CALL_DAY)
    add_call(env)
    return f"wheel:fresh:{pos.id}:2026-11-20"


async def ask_earnings(env: Env) -> str:
    await put_case(NEXT_DAY, ask="1.80", facts={"next_earnings_date": date(2026, 11, 10)}, next_put="3.10")(
        env
    )
    return f"wheel:earn:{env.pos().id}:2026-11-10"


async def ask_pin(env: Env) -> str:
    await put_case(PUT_EXPIRY, price="50.20")(env)
    return f"wheel:pin:{env.pos().id}"


async def ask_drawdown(env: Env) -> str:
    pos = await assigned(env)
    env.go(CALL_DAY)
    await env.answer(f"wheel:fresh:{pos.id}:{PUT_EXPIRY}", "y")
    env.go(date(2026, 12, 8))
    env.market.set_price("F", "35")
    await env.daily()
    return f"wheel:dd:{pos.id}:2026-12-08"


ASK: dict[str, tuple[Callable[[Env], Awaitable[str]], str, bool]] = {
    "candidate": (ask_candidate, "ar", True),
    "ack_caution": (ask_ack, "ak", False),
    "fresh_cash": (ask_fresh, "yn", False),
    "review_before_earnings": (ask_earnings, "cho", False),
    "pin_risk": (ask_pin, "ba", False),
    "drawdown_review": (ask_drawdown, "ws", True),
}


@pytest.mark.parametrize("kind", sorted(ASK))
async def test_prompts_requested(env: Env, kind: str) -> None:
    ask, choices, needs_text = ASK[kind]
    key = await ask(env)
    await env.host.sync_prompts()
    asked = env.prompts.by_key(key)
    assert asked is not None, [p.dedupe_key for p in env.prompts.rows.values()]
    assert (asked.kind, asked.status, asked.scope_key) == (kind, "pending", "F")
    assert ("".join(c.code for c in asked.choices), asked.needs_text) == (choices, needs_text)
    if kind in ("candidate", "fresh_cash"):  # the ten tests are shown with the question
        assert "1. ownership:" in asked.body and "10. premium:" in asked.body
    assert [p.kind for p in env.prompts.pending("wheel")] == [kind]


ANSWERS: list[tuple[str, str, str | None, str | None]] = [
    ("candidate", "a", "a business I want to hold", "open_put"),
    ("candidate", "r", None, None),
    ("ack_caution", "a", None, "open_put"),
    ("ack_caution", "k", None, None),
    ("fresh_cash", "y", None, "open_call"),
    ("fresh_cash", "n", None, "sell_shares"),
    ("review_before_earnings", "c", None, "close"),
    ("review_before_earnings", "h", None, None),
    ("review_before_earnings", "o", None, "roll"),
    ("pin_risk", "b", None, "close"),
    ("pin_risk", "a", None, None),
    ("drawdown_review", "w", "the business is unchanged", None),
    ("drawdown_review", "s", None, "sell_shares"),
]


@pytest.mark.parametrize(("kind", "choice", "text", "sends"), ANSWERS)
async def test_answers_applied(env: Env, kind: str, choice: str, text: str | None, sends: str | None) -> None:
    key = await ASK[kind][0](env)
    sent = len(env.broker.submitted)
    await env.answer(key, choice, text)

    new = env.requests()[sent:]
    assert new == ([sends] if sends else [])  # the market is open: the answer is applied at once
    ticker = env.store.ticker("F")
    assert ticker is not None
    if kind == "candidate":
        expected = ("approved", True, text or "") if choice == "a" else ("rejected", False, "")
        assert (ticker.status, ticker.would_own, ticker.ownership_reason) == expected
        assert ticker.updated_by == "prompt:web"
    elif kind == "ack_caution":
        assert ticker.acknowledged == (frozenset({"profitability"}) if choice == "a" else frozenset())
    elif kind == "fresh_cash":
        pos = env.pos()
        assert pos.fresh_cash_answer is (choice == "y") and pos.fresh_cash_at == env.clock.now()
    elif kind == "drawdown_review" and choice == "w":
        pos = env.pos()
        assert (pos.drawdown_review_text, pos.drawdown_review_at) == (text, env.clock.now())
    await env.host.sync_prompts()
    assert env.prompts.pending("wheel") == []


async def test_an_answer_given_while_the_market_is_closed_is_applied_by_the_next_daily_event(
    env: Env,
) -> None:
    key = await ask_earnings(env)
    env.market.open = False
    await env.answer(key, "c")
    assert env.requests() == ["open_put"]
    env.market.open = True
    env.go(date(2026, 10, 8))
    await env.daily()
    assert env.requests() == ["open_put", "close"]
    assert env.actions()[-1] == "CLOSE_PUT_NOW"


async def test_unanswered_prompt_changes_nothing(env: Env) -> None:
    pos = await assigned(env)
    add_call(env)
    sent = len(env.broker.submitted)
    for day in (date(2026, 11, 23), date(2026, 11, 24), CALL_DAY):
        env.go(day)
        await env.daily()
        await env.host.sync_prompts()
    assert len(env.broker.submitted) == sent  # no call is sold until the owner answers
    assert env.actions()[-3:] == ["RUN_FRESH_CASH_TEST"] * 3
    asked = [p for p in env.prompts.rows.values() if p.kind == "fresh_cash"]
    assert [(p.dedupe_key, p.status) for p in asked] == [(f"wheel:fresh:{pos.id}:2026-11-20", "pending")]


async def test_fresh_cash_retest_after_30_days(env: Env) -> None:
    pos = await assigned(env)
    await env.answer(f"wheel:fresh:{pos.id}:{PUT_EXPIRY}", "y")  # answered on the assignment day
    env.go(date(2026, 12, 18))  # 28 days later: not due
    await env.daily()
    assert env.actions()[-1] == "SKIP_CALL_THIS_CYCLE"
    env.go(date(2026, 12, 21))  # 31 days later
    await env.daily()
    await env.host.sync_prompts()
    assert env.actions()[-1] == "RUN_FRESH_CASH_TEST"
    again = env.prompts.by_key(f"wheel:fresh:{pos.id}:2026-12-21")
    assert again is not None and again.status == "pending"


# --- alerts, the market screen, the quarter end -------------------------------------------------------------


async def test_alerts_raised_once_per_day(env: Env) -> None:
    pos = await put_open(env)
    env.go(date(2026, 10, 30))  # 21 days left: the time exit starts today
    env.quote(env.puts["F50"], "1.50")
    await env.daily()
    await env.daily(force=True)
    env.go(TIME_EXIT_DAY)
    await env.daily()
    assert [(a[0], a[1], a[3]) for a in env.alerts] == [
        ("wheel", "TIME_EXIT_DUE", f"wheel:TIME_EXIT_DUE:{pos.id}:2026-10-30")
    ]
    assert [e.data["alert"] for e in env.store.events(kind="alert")] == ["TIME_EXIT_DUE"]
    kept = state(env).get(pos_scope(pos.id))
    assert kept is not None and kept["snapshot"]["today"] == "2026-11-02"


async def test_saturday_screen_adds_capped_ranked_candidates(
    build: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = build(market_screen_max_candidates=2)
    env.approve()  # F is known: never offered again
    env.add_stock("AAA", eps_ttm=D(-1))  # NOT_A_CANDIDATE
    env.add_stock("BBB", rsi14=D(74))  # trend CAUTION
    env.add_stock("CCC")
    asked: list[str] = []

    def finviz(filters: str) -> list[str]:
        asked.append(filters)
        return ["AAA", "F", "BBB", "CCC", "ZZZ"]  # ZZZ is unknown to the market data

    monkeypatch.setattr(screener, "source", finviz)
    env.go(date(2026, 10, 10))  # a Saturday
    await env.fire("screen")

    assert asked == [",".join(WheelParams().market_screen_filters)]
    candidates = env.store.tickers("candidate")
    assert [(t.ticker, t.origin, t.updated_by) for t in candidates] == [
        ("BBB", "screen", "strategy:wheel"),
        ("CCC", "screen", "strategy:wheel"),
    ]
    assert all(t.last_verdict == "NEEDS_REVIEW" for t in candidates)  # ownership is still to be asked
    assert env.broker.submitted == []
    await env.host.sync_prompts()
    assert sorted(p.dedupe_key for p in env.prompts.pending("wheel")) == ["wheel:cand:BBB", "wheel:cand:CCC"]


async def test_quarter_end_review_pauses_and_alerts(env: Env) -> None:
    env.approve()
    quarter_end = date(2026, 12, 31)
    with env.factory() as s:
        s.add(
            m.EquitySnapshot(
                run_id=env.run_id,
                ts=at(date(2026, 9, 30)),
                equity=D(21_000),
                cash=D(21_000),
                settled_cash=D(21_000),
                peak_equity=D(21_000),
                drawdown_pct=D(0),
            )
        )
        s.commit()

    def bar(day: date, close: str) -> Candle:
        return Candle(at(day), at(day), D(close), D(close), D(close), D(close), 1000, None)

    env.market.set_bars("SOFI", [bar(date(2026, 9, 30), "10"), bar(quarter_end, "9.90")])
    env.go(date(2026, 12, 30))
    await env.fire("postclose")
    assert env.alerts == []  # not the last session of the quarter

    env.go(quarter_end)
    await env.fire("postclose")
    ((source, kind, message, key),) = env.alerts
    assert (source, kind, key) == ("wheel", "BENCHMARK_REVIEW_DUE", "wheel:BENCHMARK_REVIEW_DUE:2026-12-31")
    assert "-4.8%" in message and "SOFI -1.0%" in message and "paused" in message
    account = state(env).get(ACCOUNT_SCOPE)
    assert account is not None and account["new_positions_paused"] is True

    env.go(date(2027, 1, 5))
    await env.daily()
    assert env.broker.submitted == []
    done = await env.host.action("wheel", PanelActionRequest("clear_pause"), "web:stephen")
    assert done.ok
    env.market.set_facts("F", next_earnings_date=date(2027, 4, 20))
    env.quote(env.market.add_contract("F", date(2027, 2, 19), "50", "put"), "1.50")
    await env.daily(force=True)
    assert env.requests() == ["open_put"]


async def test_watch_underlyings_and_schedule(env: Env) -> None:
    env.approve()
    assert await env.host.watch_underlyings() == {"F", "SOFI"}
    due = await env.host.due_events(DAY0, at(DAY0))  # 12:00 ET is past open+60m
    assert [(key, event.key) for key, event in due] == [("wheel", "opt_daily")]
    assert WheelStrategy.manual_events == ("screen",)
