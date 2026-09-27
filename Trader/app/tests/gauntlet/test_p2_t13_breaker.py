"""P2-T13 gauntlet (Breaker, attempt 1): the engine orchestrator under hostile strategies and edge sessions.

Fake plug-ins are injected into the registry (no entry points), market data comes from FakeData, and the
broker, proposals, risk manager, kill switches and ledger are the real ones on a testcontainers database.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.strategies.fakes import FakeCatalysts, FakeData, quote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import FillEvent
from trader.db import models as m
from trader.engine.killswitch import SWITCHES, KillSwitches
from trader.engine.orchestrator import Engine
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import SettingsStore
from trader.strategies.base import (
    EnterLong,
    Exit,
    Intent,
    ScheduledEvent,
    SessionOffset,
    StrategyContext,
)
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # a Tuesday
T_ORB = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET
T_FILL = T_ORB + timedelta(seconds=55)
THANKSGIVING = date(2026, 11, 26)

EventHandler = Callable[[StrategyContext], list[Intent]]
FillHandler = Callable[[StrategyContext, FillEvent], list[Intent]]


class FakeParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_positions: int = 1


@dataclass
class Calls:
    """What each fake plug-in saw, keyed by strategy key."""

    events: list[tuple[str, str]] = field(default_factory=list)
    fills: list[tuple[str, int, int | None, int]] = field(default_factory=list)


def make_plugin(
    key: str,
    calls: Calls,
    events: dict[str, EventHandler],
    on_fill: FillHandler | None = None,
    kind: Literal["entry", "overlay"] = "entry",
) -> type[Any]:
    class _Plugin:
        version = "0.0.1"
        params_model = FakeParams

        def __init__(self, params: FakeParams) -> None:
            self.params = params

        def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
            return [ScheduledEvent(k, SessionOffset.parse("open+5m5s")) for k in events]

        async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
            calls.events.append((key, event.key))
            return list(events[event.key](ctx))

        async def on_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[Intent]:
            calls.fills.append((key, fill.symbol_id, fill.strategy_config_id, ctx.strategy_config_id))
            return list(on_fill(ctx, fill)) if on_fill is not None else []

    _Plugin.key = key  # type: ignore[attr-defined]
    _Plugin.kind = kind  # type: ignore[attr-defined]
    _Plugin.__name__ = f"Fake_{key}"
    return _Plugin


@dataclass
class World:
    engine: Engine
    clock: FixedClock
    data: FakeData
    ids: dict[str, int]
    run_id: int
    registry: StrategyRegistry
    broker: SimBroker
    killswitches: KillSwitches
    factory: sessionmaker[Session]


def build(
    factory: sessionmaker[Session],
    plugins: list[type[Any]],
    *,
    auto: bool = True,
    now: datetime = T_ORB,
    max_positions: dict[str, int] | None = None,
    starting_cash: str | None = None,
) -> World:
    clock = FixedClock(now)
    store = SettingsStore(factory, now=clock.now)
    if auto:
        store.set("approval_mode", "auto", actor="test")
    if starting_cash is not None:
        store.set("starting_cash", starting_cash, actor="test")
    settings = store.load()
    run = get_live_run(factory, clock, settings)
    ids: dict[str, int] = {}
    with factory() as s:
        for i, t in enumerate(("AAA", "BBB", "CCC")):
            ids[t] = add_symbol(s, t, questrade_id=101 + i)
        s.commit()
    registry = StrategyRegistry(factory, clock, plugins={p.key: p for p in plugins})
    registry.ensure_defaults()
    for key, n in (max_positions or {}).items():
        registry.update(key, params={"max_positions": n}, actor="test")
    broker = SimBroker(
        factory,
        clock,
        Ledger(CAL),
        QuoteFillModel(FillParams.from_settings(settings)),
        run.id,
        calendar=CAL,
        settings=store.load,
    )
    ks = KillSwitches(factory, clock)
    data = FakeData()
    engine = Engine(
        factory=factory,
        clock=clock,
        calendar=CAL,
        settings=store,
        registry=registry,
        data=data,
        catalysts=FakeCatalysts(),
        broker=broker,
        proposals=ProposalService(factory, clock, store, broker, run.id),
        risk=RiskManager(CAL),
        killswitches=ks,
        run_id=run.id,
    )
    return World(engine, clock, data, ids, run.id, registry, broker, ks, factory)


def enter(symbol_id: int, evidence: dict[str, Any] | None = None) -> EnterLong:
    return EnterLong(
        symbol_id,
        "stop",
        stop=Decimal("21.51"),
        limit=None,
        stop_loss=Decimal("21.41"),
        reason="breaker",
        evidence=evidence if evidence is not None else {"rvol": Decimal("5.0000")},
    )


def protect(ctx: StrategyContext, fill: FillEvent) -> list[Intent]:
    if fill.purpose != "entry" or fill.stop_loss is None:
        return []
    return [Exit(fill.position_id, "stop", fill.stop_loss, "protective_stop")]


async def fill_all(w: World, *tickers: str) -> list[FillEvent]:
    """Quotes through every entry stop at T_FILL; fills come back in order-id order."""
    w.clock.set(T_FILL)
    qs = [quote(w.ids[t], "21.52", "21.55", "21.53", at=w.clock.now()) for t in tickers]
    for q in qs:
        w.data.quote_map[q.symbol_id] = q
    return await w.engine.on_quotes(qs, w.clock.now())


def count(factory: sessionmaker[Session], model: Any) -> int:
    with factory() as s:
        return int(s.execute(select(func.count()).select_from(model)).scalar_one())


def loud_events_mentioning(factory: sessionmaker[Session], needle: str) -> list[m.EventLog]:
    with factory() as s:
        rows = s.execute(select(m.EventLog).where(m.EventLog.level.in_(("error", "critical")))).scalars()
        return [r for r in rows if needle in f"{r.source} {r.message} {r.data}"]


# 1 ----------------------------------------------------------------------------------------------------
async def test_a_strategy_whose_on_event_raises_does_not_stop_the_others(
    db_factory: sessionmaker[Session],
) -> None:
    """One broken plug-in must not take the 09:35 entry of every other plug-in down with it (SPEC §5.1
    isolation of plug-ins). The failure is recorded as an error event naming the strategy."""
    calls = Calls()

    def boom(ctx: StrategyContext) -> list[Intent]:
        raise RuntimeError("plug-in bug")

    w: World
    ok = make_plugin("bbb_ok", calls, {"open_evt": lambda ctx: [enter(w.ids["AAA"])]})
    w = build(db_factory, [make_plugin("aaa_boom", calls, {"open_evt": boom}), ok])
    res = await w.engine.run_event("open_evt", DAY)
    assert ("bbb_ok", "open_evt") in calls.events
    assert "bbb_ok" in res.strategies
    assert [o.status for o in res.outcomes] == ["submitted"]
    assert count(db_factory, m.Proposal) == 1
    assert loud_events_mentioning(db_factory, "aaa_boom"), "the crash must be logged as an error event"


# 2 ----------------------------------------------------------------------------------------------------
async def test_the_same_scheduled_event_fired_twice_creates_one_proposal(
    db_factory: sessionmaker[Session],
) -> None:
    """Review Focus 5 (master): worker + cron backup fire the same event. A naive plug-in that ignores
    entries_today must still end up with exactly one entry proposal and one working order."""
    calls = Calls()
    w: World
    w = build(db_factory, [make_plugin("alpha", calls, {"open_evt": lambda ctx: [enter(w.ids["AAA"])]})])
    first = await w.engine.run_event("open_evt", DAY)
    second = await w.engine.run_event("open_evt", DAY)
    assert [o.status for o in first.outcomes] == ["submitted"]
    assert all(o.status == "rejected_by_risk" for o in second.outcomes)
    assert count(db_factory, m.Proposal) == 1
    assert len(w.broker.working_orders()) == 1


# 3 ----------------------------------------------------------------------------------------------------
async def test_more_intents_than_max_entries_per_day(db_factory: sessionmaker[Session]) -> None:
    """Three entries in one on_event with max_positions=2: the third is rejected by max_positions, even
    though the first two were created moments earlier in the same call."""
    calls = Calls()
    w: World
    three = make_plugin(
        "alpha", calls, {"open_evt": lambda ctx: [enter(w.ids[t]) for t in ("AAA", "BBB", "CCC")]}
    )
    w = build(db_factory, [three], max_positions={"alpha": 2})
    res = await w.engine.run_event("open_evt", DAY)
    assert [o.status for o in res.outcomes] == ["submitted", "submitted", "rejected_by_risk"]
    rejection = res.outcomes[2].rejection
    assert rejection is not None and rejection.check == "max_positions"
    assert count(db_factory, m.Proposal) == 2
    assert sorted(o.symbol_id for o in w.broker.working_orders()) == [w.ids["AAA"], w.ids["BBB"]]


# 4 ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("state", ["pending_proposal", "held_position"])
async def test_no_second_entry_for_a_symbol_already_pending_or_held(
    db_factory: sessionmaker[Session], state: str
) -> None:
    """With room for more entries (max_positions=3), a second EnterLong for AAA while AAA already has a
    pending entry proposal (manual mode) or an open position must not create another AAA entry: it would
    double the position and the risk that sizing assumed."""
    calls = Calls()
    w: World
    both = make_plugin(
        "alpha",
        calls,
        {"open_evt": lambda ctx: [enter(w.ids["AAA"])], "again_evt": lambda ctx: [enter(w.ids["AAA"])]},
    )
    # enough cash that the second entry could not be stopped by cash alone
    w = build(
        db_factory,
        [both],
        auto=state == "held_position",
        max_positions={"alpha": 3},
        starting_cash="100000",
    )
    (first,) = (await w.engine.run_event("open_evt", DAY)).outcomes
    if state == "held_position":
        assert len(await fill_all(w, "AAA")) == 1
        assert len(w.broker.open_positions()) == 1
    else:
        assert first.status == "pending"
    (second,) = (await w.engine.run_event("again_evt", DAY)).outcomes
    assert second.status == "rejected_by_risk", f"a second AAA entry was created ({second.status})"
    with db_factory() as s:
        entries = s.execute(
            select(func.count()).select_from(m.Proposal).where(m.Proposal.kind == "entry")
        ).scalar_one()
    assert entries == 1


# 5 ----------------------------------------------------------------------------------------------------
async def test_each_fill_goes_to_the_owning_strategy_on_fill(db_factory: sessionmaker[Session]) -> None:
    """Two entry strategies, both filled in one quote batch: each on_fill sees only its own fill, with its
    own config id in the context, and each protective stop is tagged with the right strategy."""
    calls = Calls()
    w: World
    a = make_plugin("alpha", calls, {"open_evt": lambda ctx: [enter(w.ids["AAA"])]}, on_fill=protect)
    b = make_plugin("beta", calls, {"open_evt": lambda ctx: [enter(w.ids["BBB"])]}, on_fill=protect)
    w = build(db_factory, [a, b])
    await w.engine.run_event("open_evt", DAY)
    fills = await fill_all(w, "AAA", "BBB")
    assert len(fills) == 2
    alpha_id, beta_id = w.registry.current("alpha").id, w.registry.current("beta").id
    assert sorted(calls.fills) == [
        ("alpha", w.ids["AAA"], alpha_id, alpha_id),
        ("beta", w.ids["BBB"], beta_id, beta_id),
    ]
    stops = {o.symbol_id: o.strategy_config_id for o in w.broker.working_orders() if o.purpose == "stop"}
    assert stops == {w.ids["AAA"]: alpha_id, w.ids["BBB"]: beta_id}


# 6 ----------------------------------------------------------------------------------------------------
async def test_on_fill_raising_does_not_lose_the_other_fills_follow_up(
    db_factory: sessionmaker[Session],
) -> None:
    """alpha's on_fill crashes on its fill. The fills are already booked by the broker, so on_quotes must
    not blow up the worker's fill loop: beta (filled in the same batch, after alpha) still gets its
    protective stop, the fills are returned, and the crash is logged loudly."""
    calls = Calls()

    def crash(ctx: StrategyContext, fill: FillEvent) -> list[Intent]:
        raise RuntimeError("on_fill bug")

    w: World
    a = make_plugin("alpha", calls, {"open_evt": lambda ctx: [enter(w.ids["AAA"])]}, on_fill=crash)
    b = make_plugin("beta", calls, {"open_evt": lambda ctx: [enter(w.ids["BBB"])]}, on_fill=protect)
    w = build(db_factory, [a, b])
    await w.engine.run_event("open_evt", DAY)
    fills = await fill_all(w, "AAA", "BBB")
    assert len(fills) == 2
    assert ("beta", w.ids["BBB"]) in [(k, sid) for k, sid, _, _ in calls.fills]
    stops = [o for o in w.broker.working_orders() if o.purpose == "stop"]
    assert [o.symbol_id for o in stops] == [w.ids["BBB"]]
    assert loud_events_mentioning(db_factory, "alpha"), "the on_fill crash must be logged as an error"


# 7 ----------------------------------------------------------------------------------------------------
async def test_an_exit_passes_with_every_kill_switch_tripped_after_hours(
    db_factory: sessionmaker[Session],
) -> None:
    """Review Focus 5: every switch tripped and the clock past the entry cutoff (15:50 ET flatten). The
    flatten still becomes a submitted exit order, and it fills."""
    calls = Calls()
    w: World
    flat = make_plugin(
        "alpha",
        calls,
        {
            "open_evt": lambda ctx: [enter(w.ids["AAA"])],
            "flatten": lambda ctx: [Exit(p.id, "market", None, "flatten") for p in ctx.positions],
        },
    )
    w = build(db_factory, [flat])
    await w.engine.run_event("open_evt", DAY)
    assert len(await fill_all(w, "AAA")) == 1
    with db_factory() as s:
        for sw in SWITCHES:
            s.add(m.KillSwitchEvent(run_id=w.run_id, switch=sw, session_date=DAY, tripped_at=T_FILL))
        s.commit()
    assert {a.switch for a in w.killswitches.active(w.run_id, DAY)} == set(SWITCHES)
    w.clock.set(datetime(2026, 10, 6, 19, 50, tzinfo=UTC))  # 15:50 ET
    (out,) = (await w.engine.run_event("flatten", DAY)).outcomes
    assert out.status == "submitted", out.rejection
    (exit_order,) = [o for o in w.broker.working_orders() if o.purpose == "exit"]
    q = quote(w.ids["AAA"], "21.60", "21.62", "21.61", at=w.clock.now())
    (fill,) = await w.engine.on_quotes([q], w.clock.now())
    assert fill.order_id == exit_order.id and w.broker.open_positions() == []


# 8 ----------------------------------------------------------------------------------------------------
async def test_no_strategy_enabled_does_nothing_but_open_positions_still_get_on_fill(
    db_factory: sessionmaker[Session],
) -> None:
    """Disabled after its entry was submitted: the next event runs no strategy and writes nothing, yet the
    late fill still reaches the (now disabled) owner so its position gets a protective stop."""
    calls = Calls()
    w: World
    a = make_plugin("alpha", calls, {"open_evt": lambda ctx: [enter(w.ids["AAA"])]}, on_fill=protect)
    w = build(db_factory, [a])
    await w.engine.run_event("open_evt", DAY)
    w.registry.update("alpha", enabled=False, actor="stephen")
    signals_before = count(db_factory, m.Signal)
    res = await w.engine.run_event("open_evt", DAY)
    assert (res.strategies, res.outcomes) == ([], [])
    assert count(db_factory, m.Signal) == signals_before
    assert len(await fill_all(w, "AAA")) == 1
    assert [k for k, *_ in calls.fills] == ["alpha"]
    assert [o.purpose for o in w.broker.working_orders()] == ["stop"]


# 9 ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("session", "now"),
    [
        pytest.param(THANKSGIVING, datetime(2026, 11, 26, 14, 35, 5, tzinfo=UTC), id="holiday"),
        pytest.param(DAY, datetime(2026, 10, 6, 20, 30, tzinfo=UTC), id="after-close"),
        pytest.param(DAY, datetime(2026, 10, 6, 19, 45, tzinfo=UTC), id="inside-no-entry-window"),
        pytest.param(DAY, datetime(2026, 10, 6, 13, 0, tzinfo=UTC), id="pre-market"),
    ],
)
async def test_an_entry_outside_the_session_is_rejected_with_its_check_name(
    db_factory: sessionmaker[Session], session: date, now: datetime
) -> None:
    """No proposal and no order; the rejection's check name is kept on the signal and in the risk
    warning event, so the UI and the journal can say why."""
    calls = Calls()
    w: World
    w = build(
        db_factory, [make_plugin("alpha", calls, {"open_evt": lambda ctx: [enter(w.ids["AAA"])]})], now=now
    )
    (out,) = (await w.engine.run_event("open_evt", session)).outcomes
    assert out.status == "rejected_by_risk" and out.rejection is not None
    assert out.rejection.check == "market_hours"
    assert count(db_factory, m.Proposal) == 0 and w.broker.working_orders() == []
    with db_factory() as s:
        signal = s.execute(select(m.Signal)).scalar_one()
        warn = s.execute(select(m.EventLog).where(m.EventLog.source == "risk")).scalar_one()
    assert signal.evidence["rejection"]["check"] == "market_hours"
    assert warn.level == "warning" and warn.data["rejection"]["check"] == "market_hours"
    assert "market_hours" in warn.message


# 10 ---------------------------------------------------------------------------------------------------
def _floats(value: Any, path: str = "$") -> list[str]:
    if isinstance(value, float):
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _floats(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _floats(v, f"{path}[{i}]")]
    return []


async def test_money_stays_decimal_end_to_end(db_factory: sessionmaker[Session]) -> None:
    """Intent → signal → sizing → proposal → order → fill → position → ledger → equity: every price and
    amount is a Decimal in Python, and nothing written to JSONB is a float (Global Constraints)."""
    calls = Calls()
    w: World
    ev = {"rvol": Decimal("5.12345"), "nested": {"atr": Decimal("1.0000"), "bars": [Decimal("21.5")]}}
    a = make_plugin("alpha", calls, {"open_evt": lambda ctx: [enter(w.ids["AAA"], ev)]}, on_fill=protect)
    w = build(db_factory, [a])
    await w.engine.run_event("open_evt", DAY)
    (fill,) = await fill_all(w, "AAA")
    assert isinstance(fill.price, Decimal) and isinstance(fill.qty, int)
    assert fill.stop_loss == Decimal("21.41")
    (stop,) = w.broker.working_orders()
    assert isinstance(stop.stop, Decimal) and stop.stop == Decimal("21.4100")
    (pos,) = w.broker.open_positions()
    assert isinstance(pos.avg_price, Decimal) and pos.avg_price == fill.price
    with db_factory() as s:
        signal = s.execute(select(m.Signal).where(m.Signal.event_key == "open_evt")).scalar_one()
        stop_signal = s.execute(select(m.Signal).where(m.Signal.event_key != "open_evt")).scalar_one()
        proposals = s.execute(select(m.Proposal)).scalars().all()
        events = s.execute(select(m.EventLog)).scalars().all()
        ledger = s.execute(select(m.CashLedger)).scalars().all()
        snaps = s.execute(select(m.EquitySnapshot)).scalars().all()
    assert signal.evidence["rvol"] == "5.12345" and signal.evidence["nested"]["bars"] == ["21.5"]
    Decimal(signal.evidence["sizing"]["shares"])
    Decimal(signal.evidence["sizing"]["risk_dollars"])
    assert stop_signal.event_key == f"fill:{fill.fill_id}" and stop_signal.intent["stop"] == "21.4100"
    bad = _floats(signal.evidence) + _floats(signal.intent) + _floats(stop_signal.intent)
    for p in proposals:
        bad += _floats(p.order_spec) + _floats(p.sizing)
    for e in events:
        bad += [f"event {e.id} ({e.source}): {x}" for x in _floats(e.data)]
    assert bad == []
    assert ledger and all(isinstance(row.amount, Decimal) for row in ledger)
    assert all(row.amount == row.amount.quantize(Decimal("0.0001")) for row in ledger)
    assert snaps and all(isinstance(sn.equity, Decimal) for sn in snaps)
