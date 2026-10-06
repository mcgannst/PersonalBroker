"""OPTSIM T7: the strategy host. It builds each plug-in's context, fires events once per session, turns
intents into broker requests, records notes and alerts, delivers fills, lifecycle events and answers to the
owning plug-in only, and keeps one plug-in's failure away from the others.

The broker, market and prompt store are the T1 fakes; the registry, state, job_runs and event_log are real."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.options import factories as f
from tests.options.fakes import (
    CannedCollateral,
    FakeOptionBroker,
    FakeOptionMarket,
    FakePromptStore,
    accept,
    reject,
)
from tests.options.toy_plugin import TOY_EVENT, ToyCallBuyer
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.option_strategies.base import (
    CancelOrder,
    CloseStructure,
    OpenStructure,
    OptionEvent,
    OptionIntent,
    OptionStrategyContext,
    PanelActionRequest,
    Reprice,
    RollStructure,
    SellShares,
)
from trader.option_strategies.host import DefaultStrategyHost, NoOptionsRun
from trader.option_strategies.registry import OptionStrategyRegistry
from trader.option_strategies.state import DbStrategyState
from trader.options.protocols import CollateralBook
from trader.options.settings import OptionSettingsStore
from trader.options.types import (
    CollateralDecision,
    ContractKey,
    LegSpec,
    LifecycleEvent,
    OptionContract,
    OptionFillEvent,
    OrderLeg,
    OrderRequest,
    OwnerPromptRequest,
    PromptChoice,
    PromptView,
)

pytestmark = pytest.mark.db
CAL = SessionCalendar()
D = Decimal
GO = OptionEvent("go", f.SESSION, scheduled=False)
ACTOR = "strategy:probe"


class Probe(ToyCallBuyer):
    """A scripted plug-in: every hook records (key, hook, item) and its context, then raises when
    (key, hook) is in `fail`, else returns `intents[key]`. Reset by the `_reset` fixture."""

    key = "probe"
    seen: ClassVar[list[tuple[str, str, Any]]] = []
    contexts: ClassVar[list[OptionStrategyContext]] = []
    fail: ClassVar[set[tuple[str, str]]] = set()
    intents: ClassVar[dict[str, list[OptionIntent]]] = {}
    wanted: ClassVar[dict[str, list[OwnerPromptRequest]]] = {}
    say: ClassVar[Callable[[OptionStrategyContext], None] | None] = None

    def _hook(self, hook: str, ctx: OptionStrategyContext, item: Any = None) -> list[OptionIntent]:
        Probe.seen.append((self.key, hook, item))
        Probe.contexts.append(ctx)
        if Probe.say is not None:
            Probe.say(ctx)
        if (self.key, hook) in Probe.fail:
            raise RuntimeError(f"boom in {hook}")
        return list(Probe.intents.get(self.key, []))

    async def watch_underlyings(self, ctx: OptionStrategyContext) -> set[str]:
        self._hook("watch_underlyings", ctx)
        return {self.params.underlying}

    async def on_event(self, ctx: OptionStrategyContext, event: OptionEvent) -> list[OptionIntent]:
        return self._hook("on_event", ctx, event)

    async def on_fill(self, ctx: OptionStrategyContext, fill: OptionFillEvent) -> list[OptionIntent]:
        return self._hook("on_fill", ctx, fill)

    async def on_lifecycle(self, ctx: OptionStrategyContext, event: LifecycleEvent) -> list[OptionIntent]:
        return self._hook("on_lifecycle", ctx, event)

    async def on_answer(self, ctx: OptionStrategyContext, prompt: PromptView) -> list[OptionIntent]:
        return self._hook("on_answer", ctx, prompt)

    async def prompts(self, ctx: OptionStrategyContext) -> list[OwnerPromptRequest]:
        self._hook("prompts", ctx)
        return list(Probe.wanted.get(self.key, []))


class Other(Probe):
    key = "other"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    Probe.seen.clear()
    Probe.contexts.clear()
    Probe.fail.clear()
    Probe.intents.clear()
    Probe.wanted.clear()
    Probe.say = None


@dataclass
class Env:
    factory: sessionmaker[Session]
    clock: FixedClock
    market: FakeOptionMarket
    broker: FakeOptionBroker
    prompts: FakePromptStore
    registry: OptionStrategyRegistry
    settings: OptionSettingsStore
    host: DefaultStrategyHost
    run_id: int
    put: OptionContract
    put_low: OptionContract
    run: dict[str, int | None]
    alerts: list[tuple[str, str, str, str]] = field(default_factory=list)

    def events(self, level: str | None = None) -> list[m.EventLog]:
        with self.factory() as s:
            rows = s.execute(select(m.EventLog).order_by(m.EventLog.id)).scalars().all()
        return [r for r in rows if level is None or r.level == level]

    def jobs(self) -> list[tuple[str, str]]:
        with self.factory() as s:
            return [(r.job, r.status) for r in s.execute(select(m.JobRun).order_by(m.JobRun.id)).scalars()]

    async def hold(self, source: str, *, qty: int = 1) -> int:
        """A filled short put of `source`; returns the structure id."""
        req = f.make_request((f.make_leg(contract_id=self.put.id),), source=source, qty=qty)
        order = (await self.broker.submit(req)).order
        return self.broker.fill(order.id, {1: D("0.45")}).structure_id

    async def working(self, source: str) -> int:
        """A working order of `source`; returns the order id."""
        req = f.make_request((f.make_leg(contract_id=self.put_low.id),), source=source)
        return (await self.broker.submit(req)).order.id


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(f.T0)
    with db_factory() as s:
        run_id = f.add_options_run(s)
        s.commit()
    market = FakeOptionMarket(clock)
    market.add_underlying("F", "14.80")
    put = market.add_contract("F", f.EXPIRY, "14.50", "put", bid="0.45", ask="0.50")
    put_low = market.add_contract("F", f.EXPIRY, "14", "put", bid="0.30", ask="0.35")
    broker = FakeOptionBroker(market)
    prompts = FakePromptStore(clock)
    registry = OptionStrategyRegistry(db_factory, clock, plugins={"probe": Probe, "other": Other})
    registry.ensure_defaults()
    settings = OptionSettingsStore(db_factory, clock.now)
    run: dict[str, int | None] = {"id": run_id}
    alerts: list[tuple[str, str, str, str]] = []

    async def sink(source: str, kind: str, message: str, dedupe_key: str) -> None:
        alerts.append((source, kind, message, dedupe_key))

    host = DefaultStrategyHost(
        db_factory, clock, CAL, registry, broker, market, prompts, settings, lambda: run["id"], sink
    )
    return Env(
        db_factory,
        clock,
        market,
        broker,
        prompts,
        registry,
        settings,
        host,
        run_id,
        put,
        put_low,
        run,
        alerts,
    )


def _open(contract: OptionContract, **changes: Any) -> OpenStructure:
    fields: dict[str, Any] = {
        "legs": (
            LegSpec(
                "option", "sell", 1, "F", ContractKey("F", contract.expiry, contract.strike, contract.right)
            ),
        ),
        "qty": 1,
        "order_type": "limit",
        "net_limit": D("0.45"),
        "tif": "day",
        "reason": "entry",
        **changes,
    }
    return OpenStructure(**fields)


def _ask(key: str, dedupe_key: str) -> OwnerPromptRequest:
    return OwnerPromptRequest(
        "review", key, dedupe_key, "Review?", "Body", (PromptChoice("y", "Yes"), PromptChoice("n", "No"))
    )


# --- firing ---------------------------------------------------------------------------------------


async def test_fire_runs_once_per_session_and_force_reruns(env: Env) -> None:
    first = await env.host.fire("probe", GO)
    assert (first.status, first.detail) == (
        "succeeded",
        {"intents": 0, "submitted": 0, "rejected": 0, "dropped": 0},
    )
    again = await env.host.fire("probe", GO)
    assert (again.status, again.detail) == ("skipped", {"reason": "already succeeded"})
    assert (await env.host.fire("probe", GO, force=True)).status == "succeeded"
    next_day = OptionEvent("go", date(2026, 10, 7), scheduled=False)
    assert (await env.host.fire("probe", next_day)).status == "succeeded"
    assert [(k, h, e) for k, h, e in Probe.seen] == [("probe", "on_event", GO)] * 2 + [
        ("probe", "on_event", next_day)
    ]
    assert Probe.contexts[-1].session_date == date(2026, 10, 7)
    assert env.jobs() == [("opt_event:probe:go", "succeeded")] * 3
    with pytest.raises(KeyError):
        await env.host.fire("nope", GO)


async def test_fire_skips_when_paused_disabled_or_no_run(env: Env) -> None:
    env.settings.set("options.strategies_paused", True, "test")
    assert (await env.host.fire("probe", GO)).detail == {"reason": "strategies are paused"}
    env.settings.set("options.strategies_paused", False, "test")
    env.registry.update("probe", enabled=False, actor="test")
    assert (await env.host.fire("probe", GO)).detail == {"reason": "probe is disabled"}
    env.registry.update("probe", enabled=True, actor="test")
    env.run["id"] = None
    skipped = await env.host.fire("probe", GO)
    assert (skipped.status, skipped.detail) == ("skipped", {"reason": "there is no options run"})
    assert Probe.seen == [] and env.jobs() == []
    notes = env.events("info")
    assert [n.source for n in notes] == ["options.strategy.probe"] * 3
    assert all("skipped" in n.message for n in notes)

    env.run["id"] = env.run_id  # a skip never uses the session's one run up
    assert (await env.host.fire("probe", GO)).status == "succeeded"


async def test_due_events_respect_job_runs(env: Env) -> None:
    scheduled = OptionEvent(TOY_EVENT, f.SESSION)  # both plug-ins: open+5m = 09:35 ET
    early = f.T0 - timedelta(minutes=26)  # 09:34 ET
    assert await env.host.due_events(f.SESSION, early) == []
    assert await env.host.due_events(f.SESSION, f.T0) == [("other", scheduled), ("probe", scheduled)]
    assert await env.host.due_events(date(2026, 10, 10), f.T0) == []  # a Saturday

    assert (await env.host.fire("probe", scheduled)).status == "succeeded"
    assert await env.host.due_events(f.SESSION, f.T0) == [("other", scheduled)]
    env.registry.update("other", enabled=False, actor="test")
    assert await env.host.due_events(f.SESSION, f.T0) == []
    env.registry.update("other", enabled=True, actor="test")
    env.settings.set("options.strategies_paused", True, "test")
    assert await env.host.due_events(f.SESSION, f.T0) == []


# --- the context ----------------------------------------------------------------------------------


async def test_context_holds_only_own_structures_and_orders(env: Env) -> None:
    mine = await env.hold("probe")
    await env.hold("other")
    await env.hold("manual")
    my_order = await env.working("probe")
    await env.working("other")
    await env.working("manual")
    env.prompts.ensure(env.run_id, "probe", _ask("F", "probe:review:F"))
    env.prompts.ensure(env.run_id, "other", _ask("F", "other:review:F"))
    close = CAL.session_close(f.SESSION)
    with env.factory() as s:
        for ts, equity in ((close - timedelta(hours=1), "5100"), (close, "5123.45")):
            s.add(
                m.EquitySnapshot(
                    run_id=env.run_id,
                    ts=ts,
                    equity=D(equity),
                    cash=D("5000"),
                    settled_cash=D("5000"),
                    peak_equity=D(equity),
                    drawdown_pct=D("0"),
                )
            )
        s.commit()

    await env.host.fire("probe", GO)
    ctx = Probe.contexts[-1]
    assert [s.id for s in ctx.structures] == [mine] and ctx.structures[0].source == "probe"
    assert [o.id for o in ctx.orders] == [my_order]
    assert ctx.account == await env.broker.account() and ctx.account.cash > D("5000")  # the whole account
    assert (ctx.run_id, ctx.strategy_key, ctx.session_date) == (env.run_id, "probe", f.SESSION)
    assert ctx.config_id == env.registry.current("probe").id
    assert ctx.params == Probe.params_model() and ctx.settings == env.settings.load()
    assert ctx.market is env.market and ctx.usd_cad_rate == D(1)
    found = ctx.prompt("probe:review:F")
    assert found is not None and found.source == "probe"
    assert ctx.prompt("other:review:F") is None
    assert ctx.equity_on(f.SESSION) == D("5123.45") and ctx.equity_on(date(2026, 10, 5)) is None
    ctx.state.put("seen", {"n": 1})
    assert DbStrategyState(env.factory, env.clock, "probe").get("seen") == {"n": 1}
    assert DbStrategyState(env.factory, env.clock, "other").get("seen") is None


# --- intents --------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["open", "close", "close_part", "roll", "sell_shares", "reprice", "cancel"])
async def test_intent_mapping(env: Env, case: str) -> None:
    csp = await env.hold("probe", qty=2)
    env.broker.collateral = CannedCollateral(accept(kind="shares"))
    bought = await env.broker.submit(
        f.make_request(
            (f.make_leg(instrument="shares", side="buy", ratio=100),), source="probe", order_type="market"
        )
    )
    shares = env.broker.fill(bought.order.id, {1: D("14.80")}).structure_id
    env.broker.collateral = CannedCollateral(accept())
    order_id = await env.working("probe")
    seeded = len(env.broker.submitted)
    config_id = env.registry.current("probe").id
    put_key = ContractKey("F", f.EXPIRY, D("14"), "put")

    def request(**fields: Any) -> OrderRequest:
        base: dict[str, Any] = {
            "source": "probe",
            "strategy_config_id": config_id,
            "underlying": "F",
            "tif": "day",
            "walk": False,
            "take_profit_pct": None,
            "evidence": {},
            "submitted_by": ACTOR,
        }
        return OrderRequest(**{**base, **fields})

    buy_back = OrderLeg(1, "option", "buy", "close", 1, "F", env.put.id)
    intent: OptionIntent
    expected: OrderRequest | None = None
    if case == "open":
        intent = _open(
            env.put_low, tif="gtc", walk=True, take_profit_pct=D("0.5"), evidence={"delta": "-0.25"}, qty=3
        )
        expected = request(
            intent="open",
            structure_id=None,
            legs=(OrderLeg(1, "option", "sell", "open", 1, "F", env.put_low.id),),
            qty=3,
            order_type="limit",
            net_limit=D("0.45"),
            tif="gtc",
            walk=True,
            take_profit_pct=D("0.5"),
            reason="entry",
            evidence={"delta": "-0.25"},
        )
    elif case in ("close", "close_part"):
        part = 1 if case == "close_part" else None
        intent = CloseStructure(csp, "limit", D("-0.20"), "take_profit", tif="gtc", walk=True, qty=part)
        expected = request(
            intent="close",
            structure_id=csp,
            legs=(buy_back,),
            qty=part or 2,
            order_type="limit",
            net_limit=D("-0.20"),
            tif="gtc",
            walk=True,
            reason="take_profit",
        )
    elif case == "roll":
        intent = RollStructure(
            csp, (LegSpec("option", "sell", 1, "F", put_key),), "limit", D("0.10"), "roll_down"
        )
        expected = request(
            intent="roll",
            structure_id=csp,
            legs=(buy_back, OrderLeg(2, "option", "sell", "open", 1, "F", env.put_low.id)),
            qty=2,
            order_type="limit",
            net_limit=D("0.10"),
            reason="roll_down",
        )
    elif case == "sell_shares":
        intent = SellShares(shares, "thesis_broken")
        expected = request(
            intent="close",
            structure_id=shares,
            legs=(OrderLeg(1, "shares", "sell", "close", 100, "F", None),),
            qty=1,
            order_type="market",
            net_limit=None,
            reason="thesis_broken",
        )
    elif case == "reprice":
        intent = Reprice(order_id, D("0.40"))
    else:
        intent = CancelOrder(order_id, "stale")

    Probe.intents["probe"] = [intent]
    outcome = await env.host.fire("probe", GO)
    assert outcome.status == "succeeded" and outcome.detail["dropped"] == 0
    assert env.broker.submitted[seeded:] == ([] if expected is None else [expected])
    assert env.broker.reprices == ([(order_id, D("0.40"), ACTOR)] if case == "reprice" else [])
    assert env.broker.cancels == ([(order_id, "stale", ACTOR)] if case == "cancel" else [])
    assert env.events("warning") == [] and env.events("error") == []


async def test_foreign_structure_intent_is_dropped(env: Env) -> None:
    theirs = await env.hold("other")
    manual = await env.hold("manual")
    their_order = await env.working("other")
    seeded = len(env.broker.submitted)
    Probe.intents["probe"] = [
        CloseStructure(theirs, "market", None, "grab"),
        RollStructure(manual, (), "market", None, "grab"),
        SellShares(theirs, "grab"),
        CancelOrder(their_order, "grab"),
        Reprice(their_order, D("0.01")),
        CloseStructure(999, "market", None, "no such structure"),
    ]
    outcome = await env.host.fire("probe", GO)
    assert outcome.detail == {"intents": 6, "submitted": 0, "rejected": 0, "dropped": 6}
    assert len(env.broker.submitted) == seeded and env.broker.cancels == [] and env.broker.reprices == []
    errors = env.events("error")
    assert [(e.source, e.run_id) for e in errors] == [("options.strategy.probe", env.run_id)] * 6
    assert f"structure {theirs} is not an open structure of probe" in errors[0].message
    assert f"order {their_order} is not an order of probe" in errors[3].message


class RejectFirst:
    """Rejects the first order it sees, accepts the rest."""

    def __init__(self) -> None:
        self.asked = 0

    def evaluate(self, req: OrderRequest, book: CollateralBook) -> CollateralDecision:
        self.asked += 1
        return reject("insufficient_cash", "needs 1,450.00") if self.asked == 1 else accept()


async def test_rejected_order_becomes_a_note_and_others_continue(env: Env) -> None:
    env.broker.collateral = RejectFirst()
    unknown = LegSpec("option", "sell", 1, "F", ContractKey("F", f.EXPIRY, D("99"), "put"))
    Probe.intents["probe"] = [
        _open(env.put, legs=(unknown,)),
        _open(env.put, reason="first"),
        _open(env.put_low, reason="second"),
    ]
    outcome = await env.host.fire("probe", GO)
    assert outcome.status == "succeeded"
    assert outcome.detail == {"intents": 3, "submitted": 1, "rejected": 1, "dropped": 1}
    assert [r.reason for r in env.broker.submitted] == ["first", "second"]
    assert [o.reason for o in await env.broker.orders(status="working", source="probe")] == ["second"]
    notes = env.events("warning")
    assert len(notes) == 2 and env.events("error") == []
    assert "unknown contract F 2026-11-20 put 99" in notes[0].message
    assert "rejected: insufficient_cash needs 1,450.00" in notes[1].message
    assert notes[1].data["reject_reason"] == "insufficient_cash"

    env.settings.set("options.strategies_paused", True, "test")  # a paused host accepts no intent at all
    Probe.intents["probe"] = [_open(env.put, reason="third")]
    await env.host.deliver_fill(env.broker.fill(2, {1: D("0.30")}))
    assert [r.reason for r in env.broker.submitted] == ["first", "second"]
    assert "1 intent(s) dropped: strategies are paused" in env.events("warning")[-1].message


async def test_notes_and_alerts_are_recorded_and_sent(env: Env) -> None:
    def say(ctx: OptionStrategyContext) -> None:
        ctx.note("checked F", price=D("14.80"), day=ctx.session_date)
        ctx.note("facts are stale", "warning")
        ctx.alert("STRIKE_TOUCHED", "F touched 14.50", "probe:touch:F", strike=D("14.50"))

    Probe.say = say
    await env.host.fire("probe", GO)
    rows = [(e.level, e.source, e.run_id, e.message, e.data) for e in env.events()]
    assert rows == [
        ("info", "options.strategy.probe", env.run_id, "checked F", {"price": "14.80", "day": "2026-10-06"}),
        ("warning", "options.strategy.probe", env.run_id, "facts are stale", {}),
        (
            "warning",
            "options.strategy.probe",
            env.run_id,
            "F touched 14.50",
            {"strike": "14.50", "kind": "STRIKE_TOUCHED", "dedupe_key": "probe:touch:F"},
        ),
    ]
    assert env.alerts == [("probe", "STRIKE_TOUCHED", "F touched 14.50", "probe:touch:F")]


# --- deliveries -----------------------------------------------------------------------------------


def _lifecycle_row(env: Env, source: str) -> LifecycleEvent:
    """A stored `expired` event of a structure of `source`, as the lifecycle engine returns it."""
    with env.factory() as s:
        symbol_id = f.add_underlying(s, f"L{source[:3].upper()}")
        structure = f.add_structure(s, env.run_id, symbol_id, source=source, state="closed")
        position = f.add_position(s, env.run_id, structure, symbol_id, qty=0)
        row = m.OptLifecycleEvent(
            run_id=env.run_id,
            structure_id=structure,
            position_id=position,
            kind="expired",
            session_date=f.SESSION,
            ts=f.T0,
            qty=1,
        )
        s.add(row)
        s.commit()
        return LifecycleEvent(
            row.id, structure, source, None, "expired", f.SESSION, f.T0, None, 1, None, None, 0, D(0), None
        )


def _delivered_at(env: Env, event: LifecycleEvent) -> datetime | None:
    with env.factory() as s:
        return s.get_one(m.OptLifecycleEvent, event.id).delivered_at


async def test_fills_and_lifecycle_reach_only_the_owner(env: Env) -> None:
    mine = env.broker.fills[-1] if await env.hold("probe") else None
    await env.hold("manual")
    manual = env.broker.fills[-1]
    assert mine is not None and (mine.source, manual.source) == ("probe", "manual")
    await env.host.deliver_fill(mine)
    await env.host.deliver_fill(manual)
    assert Probe.seen == [("probe", "on_fill", mine)]

    expired, by_hand = _lifecycle_row(env, "probe"), _lifecycle_row(env, "manual")
    Probe.fail.add(("probe", "on_lifecycle"))
    await env.host.deliver_lifecycle(expired)  # the hook raises: undelivered, retried on the next call
    assert _delivered_at(env, expired) is None
    assert [e.message for e in env.events("error")] == [
        f"probe: on_lifecycle (expired, event {expired.id}) failed"
    ]
    Probe.fail.clear()
    await env.host.deliver_lifecycle(expired)
    await env.host.deliver_lifecycle(expired)  # already delivered: not told twice
    await env.host.deliver_lifecycle(by_hand)  # nobody to tell
    assert Probe.seen[1:] == [("probe", "on_lifecycle", expired)] * 2
    assert _delivered_at(env, expired) == f.T0 and _delivered_at(env, by_hand) == f.T0


async def test_answers_delivered_once_and_retried_after_a_failure(env: Env) -> None:
    asked = env.prompts.ensure(env.run_id, "probe", _ask("F", "probe:review:F"))
    env.prompts.ensure(env.run_id, "probe", _ask("T", "probe:review:T"))  # still pending
    assert await env.host.deliver_answers() == 0 and Probe.seen == []
    answered = env.prompts.answer(asked.id, "y", via="web", actor="web:stephen").prompt

    Probe.fail.add(("probe", "on_answer"))
    assert await env.host.deliver_answers() == 0
    assert [p.id for p in env.prompts.undelivered("probe")] == [asked.id]
    assert [e.message for e in env.events("error")] == [f"probe: on_answer (prompt {asked.id}) failed"]
    Probe.fail.clear()
    assert await env.host.deliver_answers() == 1
    assert await env.host.deliver_answers() == 0
    assert Probe.seen == [("probe", "on_answer", answered)] * 2
    assert env.prompts.undelivered("probe") == []


async def test_sync_prompts_upserts_and_cancels(env: Env) -> None:
    a, b = _ask("F", "probe:review:F"), _ask("T", "probe:review:T")
    by_hand = env.prompts.ensure(env.run_id, "manual", _ask("X", "manual:x"))
    Probe.wanted["probe"] = [a, b]
    assert await env.host.sync_prompts() == 2
    assert await env.host.sync_prompts() == 0  # asking again changes nothing
    pending = env.prompts.pending("probe")
    assert [(p.dedupe_key, p.source) for p in pending] == [(a.dedupe_key, "probe"), (b.dedupe_key, "probe")]
    assert env.prompts.run_ids[pending[0].id] == env.run_id

    Probe.wanted["probe"] = [b]
    assert await env.host.sync_prompts() == 1
    gone = env.prompts.by_key(a.dedupe_key)
    assert gone is not None and gone.status == "cancelled"
    assert [p.dedupe_key for p in env.prompts.pending("probe")] == [b.dedupe_key]
    assert env.prompts.pending("manual") == [by_hand]


async def test_a_raising_hook_does_not_stop_other_plugins(env: Env) -> None:
    for key in ("probe", "other"):
        asked = env.prompts.ensure(env.run_id, key, _ask("F", f"{key}:answered"))
        env.prompts.answer(asked.id, "y", via="telegram", actor="tg:1")
    Probe.wanted.update(probe=[_ask("T", "probe:new")], other=[_ask("T", "other:new")])
    Probe.fail.update(
        {("probe", hook) for hook in ("on_event", "on_answer", "prompts", "watch_underlyings", "panel")}
    )

    assert await env.host.watch_underlyings() == {"F"}
    assert await env.host.deliver_answers() == 1
    assert [p.dedupe_key for p in env.prompts.undelivered("probe")] == ["probe:answered"]
    assert await env.host.sync_prompts() == 1
    assert env.prompts.by_key("other:new") is not None and env.prompts.by_key("probe:new") is None
    failed = await env.host.fire("probe", GO)
    assert (failed.status, failed.error) == ("failed", "RuntimeError: boom in on_event")
    assert (await env.host.fire("other", GO)).status == "succeeded"
    assert env.jobs() == [("opt_event:probe:go", "failed"), ("opt_event:other:go", "succeeded")]
    sources = {e.source for e in env.events("error")}
    assert sources == {"options.strategy.probe", "job.opt_event:probe:go"}

    # the panel and an action use the same context and never trade
    seeded = len(env.broker.submitted)
    Probe.intents["other"] = [_open(env.put)]
    panel = await env.host.panel("other")
    assert panel.tables[0].key == "holdings"
    saved = await env.host.action("other", PanelActionRequest("note", None, "watch F"), "web:stephen")
    assert (saved.ok, saved.message) == (True, "Note saved")
    assert DbStrategyState(env.factory, env.clock, "other").get("note") == {"text": "watch F", "row_id": None}
    assert len(env.broker.submitted) == seeded
    env.run["id"] = None
    with pytest.raises(NoOptionsRun):
        await env.host.panel("other")
    with pytest.raises(KeyError):
        await env.host.panel("nope")
