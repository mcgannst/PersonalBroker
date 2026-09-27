import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.strategies.fakes import FakeCatalyst, FakeCatalysts, FakeData, quote
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import QtQuote
from trader.bootstrap import Core
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import FillEvent
from trader.config import EnvSettings
from trader.crypto import Crypto
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.orchestrator import Engine, build_engine
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle
from trader.settings_store import SettingsStore
from trader.strategies.base import (
    Cancel,
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
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)
T_ORB = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET


def five(o: str, h: str, low: str, c: str, v: int) -> Candle:
    return Candle(
        OPEN, OPEN + timedelta(minutes=5), Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None
    )


@dataclass
class World:
    engine: Engine
    clock: FixedClock
    fq: FakeQuestrade
    ids: dict[str, int]
    run_id: int
    registry: StrategyRegistry
    proposals: ProposalService
    killswitches: KillSwitches
    broker: SimBroker
    factory: sessionmaker[Session]


def build(factory: sessionmaker[Session], *, auto: bool) -> World:
    clock = FixedClock(T_ORB)
    store = SettingsStore(factory, now=clock.now)
    if auto:
        store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    run = get_live_run(factory, clock, settings)
    ids: dict[str, int] = {}
    with factory() as s:
        for i, t in enumerate(("AAA", "BBB")):
            ids[t] = add_symbol(s, t, questrade_id=101 + i)
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=ids[t],
                    price=Decimal("21"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1.0000"),
                    source="finviz",
                )
            )
            s.add(
                m.OpenBarStat(
                    symbol_id=ids[t],
                    session_date=DAY,
                    avg_open_vol_14d=Decimal("1000.00"),
                    atr14=Decimal("1.0000"),
                )
            )
        ids["SPY"] = add_symbol(s, "SPY", questrade_id=199, exchange="ARCA")
        s.add(
            m.DailyCandle(
                symbol_id=ids["SPY"],
                date=date(2026, 10, 5),
                open=Decimal("499"),
                high=Decimal("501"),
                low=Decimal("498"),
                close=Decimal("500.00"),
                volume=1,
                vwap=None,
            )
        )
        s.commit()
    fq = FakeQuestrade()
    for t, q in (("AAA", 101), ("BBB", 102), ("SPY", 199)):
        fq.add_symbol(t, q)
    fq.add_bars(101, "FiveMinutes", [five("21.00", "21.50", "20.90", "21.40", 5000)])
    fq.add_bars(102, "FiveMinutes", [five("30.00", "30.60", "29.90", "30.50", 3000)])
    registry = StrategyRegistry(factory, clock)
    registry.ensure_defaults()
    broker = SimBroker(
        factory,
        clock,
        Ledger(CAL),
        QuoteFillModel(FillParams.from_settings(settings)),
        run.id,
        calendar=CAL,
        settings=store.load,
    )
    proposals = ProposalService(factory, clock, store, broker, run.id)
    ks = KillSwitches(factory, clock)
    engine = Engine(
        factory=factory,
        clock=clock,
        calendar=CAL,
        settings=store,
        registry=registry,
        data=MarketDataService(factory, clock, CAL, fq),
        catalysts=FakeCatalysts({ids["AAA"]: FakeCatalyst()}),
        broker=broker,
        proposals=proposals,
        risk=RiskManager(CAL),
        killswitches=ks,
        run_id=run.id,
    )
    return World(engine, clock, fq, ids, run.id, registry, proposals, ks, broker, factory)


async def fill_entry(w: World) -> None:
    await w.engine.run_event("orb_open", DAY)
    w.clock.set(T_ORB + timedelta(seconds=55))
    w.fq.set_quote(101, "21.52", "21.55", "21.53", w.clock.now())
    assert len(await w.engine.poll_quotes()) == 1


async def test_intent_to_fill_to_protective_stop_end_to_end(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    res = await w.engine.run_event("orb_open", DAY)
    assert res.strategies == ["orb_sip"]
    (out,) = res.outcomes
    assert out.status == "submitted" and out.proposal_id is not None
    (order,) = w.broker.working_orders()
    # 720 / (21.51 x 1.005) = 33.3 -> 33 shares (cash-limited; risk allows 144)
    assert (order.symbol_id, order.order_type, order.stop, order.qty) == (
        w.ids["AAA"],
        "stop",
        Decimal("21.5100"),
        33,
    )
    with db_factory() as s:
        cands = {
            c.symbol_id: (c.passed, c.reject_reason, c.run_id)
            for c in s.execute(select(m.Candidate)).scalars()
        }
        signal = s.execute(select(m.Signal)).scalar_one()
        notes = s.execute(select(m.EventLog).where(m.EventLog.source == "strategy.orb_sip")).scalars().all()
    assert cands == {
        w.ids["AAA"]: (True, None, w.run_id),
        w.ids["BBB"]: (False, "catalyst_missing", w.run_id),
    }
    assert signal.strategy_config_id == w.registry.current("orb_sip").id and signal.event_key == "orb_open"
    assert signal.evidence["rvol"] == "5.0000" and signal.evidence["sizing"]["shares"] == "33"
    assert signal.intent["type"] == "enter_long" and notes

    w.clock.set(T_ORB + timedelta(seconds=55))
    w.fq.set_quote(101, "21.52", "21.55", "21.53", w.clock.now())
    (fill,) = await w.engine.poll_quotes()
    assert fill.price == Decimal("21.5608") and fill.purpose == "entry"
    (stop,) = w.broker.working_orders()
    assert (stop.purpose, stop.order_type, stop.stop, stop.qty) == ("stop", "stop", Decimal("21.4100"), 33)
    with db_factory() as s:
        kinds = [(p.kind, p.status) for p in s.execute(select(m.Proposal).order_by(m.Proposal.id)).scalars()]
        snaps = s.execute(select(func.count()).select_from(m.EquitySnapshot)).scalar_one()
    assert kinds == [("entry", "submitted"), ("stop", "submitted")] and snaps == 1


async def test_manual_mode_waits_for_a_decision(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=False)
    (out,) = (await w.engine.run_event("orb_open", DAY)).outcomes
    assert out.status == "pending" and w.broker.working_orders() == []
    assert out.proposal_id is not None
    w.proposals.decide(out.proposal_id, "approve", "web", "stephen")
    assert len(w.broker.working_orders()) == 1


async def test_a_risk_rejection_is_logged_and_kept_on_the_signal(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    w.killswitches.pause(w.run_id, DAY, actor="stephen")
    (out,) = (await w.engine.run_event("orb_open", DAY)).outcomes
    assert (
        out.status == "rejected_by_risk"
        and out.rejection is not None
        and out.rejection.check == "kill_switch"
    )
    with db_factory() as s:
        signal = s.execute(select(m.Signal)).scalar_one()
        proposals = s.execute(select(func.count()).select_from(m.Proposal)).scalar_one()
        warn = s.execute(select(m.EventLog).where(m.EventLog.source == "risk")).scalar_one()
    assert (
        signal.evidence["rejection"]["check"] == "kill_switch" and proposals == 0 and warn.level == "warning"
    )


async def test_protective_stop_placed_while_kill_switch_tripped(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 5: a tripped switch blocks entries only; the stop for a filled entry still goes in."""
    w = build(db_factory, auto=True)
    await w.engine.run_event("orb_open", DAY)
    w.killswitches.pause(w.run_id, DAY, actor="stephen")
    w.clock.set(T_ORB + timedelta(seconds=55))
    w.fq.set_quote(101, "21.52", "21.55", "21.53", w.clock.now())
    await w.engine.poll_quotes()
    assert w.killswitches.blocking(w.run_id, DAY) == "manual_pause"
    (stop,) = w.broker.working_orders()
    assert stop.purpose == "stop"


async def test_rerunning_the_orb_event_adds_nothing(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    await w.engine.run_event("orb_open", DAY)
    again = await w.engine.run_event("orb_open", DAY)
    assert again.outcomes == []
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Proposal)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(m.Candidate)).scalar_one() == 2


async def test_end_of_session_flags_open_position(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 4: a position still open at the close is cancelled-around and reported loudly."""
    w = build(db_factory, auto=True)
    await fill_entry(w)
    w.clock.set(datetime(2026, 10, 6, 20, 0, tzinfo=UTC))
    still_open = await w.engine.end_of_session(DAY)
    assert len(still_open) == 1 and w.broker.working_orders() == []
    with db_factory() as s:
        alarm = s.execute(select(m.EventLog).where(m.EventLog.level == "critical")).scalar_one()
    assert alarm.message.startswith("1 position still open") and alarm.run_id == w.run_id


async def test_the_overlay_exits_entry_positions_on_a_down_day(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    await fill_entry(w)
    w.clock.set(datetime(2026, 10, 6, 19, 30, tzinfo=UTC))  # 15:30 ET
    w.fq.set_quote(199, "495.00", "495.02", "495.01", w.clock.now())
    res = await w.engine.run_event("overlay_decision", DAY)
    assert res.strategies == ["spy_overlay"]
    (out,) = res.outcomes
    assert out.status == "submitted"
    with db_factory() as s:
        exit_signal = s.execute(select(m.Signal).where(m.Signal.event_key == "overlay_decision")).scalar_one()
    assert exit_signal.strategy_config_id == w.registry.current("spy_overlay").id
    assert sorted(o.purpose for o in w.broker.working_orders()) == ["exit", "stop"]


async def test_tick_expires_due_proposals(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=False)
    (out,) = (await w.engine.run_event("orb_open", DAY)).outcomes
    await w.engine.tick(T_ORB + timedelta(minutes=5))
    with db_factory() as s:
        p = s.get(m.Proposal, out.proposal_id)
    assert p is not None and p.status == "expired"


async def test_build_engine_wires_a_live_run(
    db_factory: sessionmaker[Session], migrated_engine: SqlEngine
) -> None:
    clock = FixedClock(T_ORB)
    key = Fernet.generate_key().decode()
    env = EnvSettings(
        database_url="postgresql+psycopg://unused",
        migration_database_url="postgresql+psycopg://unused",
        app_encryption_key=key,
        session_secret="test-session-secret",
    )
    core = Core(
        env, migrated_engine, db_factory, Crypto(key), clock, CAL, SettingsStore(db_factory, now=clock.now)
    )
    engine = build_engine(core, FakeQuestrade(), FakeCatalysts())
    assert engine.run_id == get_live_run(db_factory, clock, core.settings.load()).id
    with db_factory() as s:
        keys = set(s.execute(select(m.StrategyConfig.strategy_key)).scalars())
    assert keys == {"orb_sip", "spy_overlay"}


# --- fix round 1: isolation, duplicates and loud failures (fake plug-ins, no entry points) ------------------
T_FILL = T_ORB + timedelta(seconds=55)
Handler = Callable[[StrategyContext], Sequence[Intent]]


class FakeParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_positions: int = 1


def fake_plugin(key: str, events: dict[str, Handler]) -> type[Any]:
    class _Plugin:
        version = "0.0.1"
        kind = "entry"
        params_model = FakeParams

        def __init__(self, params: FakeParams) -> None:
            self.params = params

        def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
            return [ScheduledEvent(k, SessionOffset.parse("open+5m5s")) for k in events]

        async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
            return list(events[event.key](ctx))

        async def on_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[Intent]:
            return []

    _Plugin.key = key  # type: ignore[attr-defined]
    return _Plugin


class RaisingData(FakeData):
    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        raise QuestradeApiError(503, "quotes down")


@dataclass
class FakeWorld:
    engine: Engine
    clock: FixedClock
    store: SettingsStore
    data: FakeData
    ids: dict[str, int]
    broker: SimBroker
    run_id: int


def build_fake(
    factory: sessionmaker[Session],
    events: dict[str, Handler],
    *,
    auto: bool = True,
    max_positions: int = 1,
    data: FakeData | None = None,
    extra_plugins: dict[str, type[Any]] | None = None,
) -> FakeWorld:
    clock = FixedClock(T_ORB)
    store = SettingsStore(factory, now=clock.now)
    if auto:
        store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    run = get_live_run(factory, clock, settings)
    with factory() as s:
        ids = {t: add_symbol(s, t, questrade_id=101 + i) for i, t in enumerate(("AAA", "BBB"))}
        s.commit()
    registry = StrategyRegistry(
        factory, clock, plugins={"alpha": fake_plugin("alpha", events), **(extra_plugins or {})}
    )
    registry.ensure_defaults()
    registry.update("alpha", params={"max_positions": max_positions}, actor="test")
    broker = SimBroker(
        factory,
        clock,
        Ledger(CAL),
        QuoteFillModel(FillParams.from_settings(settings)),
        run.id,
        calendar=CAL,
        settings=store.load,
    )
    data = data if data is not None else FakeData()
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
        killswitches=KillSwitches(factory, clock),
        run_id=run.id,
    )
    return FakeWorld(engine, clock, store, data, ids, broker, run.id)


def enter_stop(symbol_id: int) -> EnterLong:
    return EnterLong(symbol_id, "stop", Decimal("21.51"), None, Decimal("21.41"), "test", {})


async def fill_aaa(w: FakeWorld) -> None:
    w.clock.set(T_FILL)
    q = quote(w.ids["AAA"], "21.52", "21.55", "21.53", at=T_FILL)
    w.data.quote_map[q.symbol_id] = q
    assert len(await w.engine.on_quotes([q], T_FILL)) == 1


def events_at(factory: sessionmaker[Session], *levels: str) -> list[m.EventLog]:
    with factory() as s:
        return list(s.execute(select(m.EventLog).where(m.EventLog.level.in_(levels))).scalars())


async def test_a_failing_intent_does_not_abort_its_siblings(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w: FakeWorld
    w = build_fake(
        db_factory,
        {"open_evt": lambda ctx: [enter_stop(w.ids["AAA"]), enter_stop(w.ids["BBB"])]},
        max_positions=2,
    )
    create = w.engine.proposals.create
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> m.Proposal:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database hiccup")
        return create(*args, **kwargs)

    monkeypatch.setattr(w.engine.proposals, "create", flaky)
    res = await w.engine.run_event("open_evt", DAY)
    assert [o.status for o in res.outcomes] == ["error", "submitted"]
    failed = res.outcomes[0]
    assert failed.signal_id is not None and failed.error is not None and "database hiccup" in failed.error
    with db_factory() as s:
        signal = s.get(m.Signal, failed.signal_id)
    assert signal is not None and signal.evidence["error"]["message"] == "database hiccup"
    assert "rejection" not in signal.evidence
    (err,) = events_at(db_factory, "error")
    assert err.source == "engine" and "alpha" in err.message and err.data["signal_id"] == failed.signal_id


async def test_a_quote_outage_rejects_a_market_entry_instead_of_raising(
    db_factory: sessionmaker[Session],
) -> None:
    w: FakeWorld

    def market(ctx: StrategyContext) -> list[Intent]:
        return [EnterLong(w.ids["AAA"], "market", None, None, Decimal("21.41"), "test", {})]

    w = build_fake(db_factory, {"open_evt": market}, data=RaisingData())
    (out,) = (await w.engine.run_event("open_evt", DAY)).outcomes
    assert out.status == "rejected_by_risk" and out.rejection is not None
    assert out.rejection.check == "invalid" and "no entry price" in out.rejection.reason


async def test_duplicate_symbol_is_run_wide_and_recorded_on_the_signal(
    db_factory: sessionmaker[Session],
) -> None:
    w: FakeWorld
    w = build_fake(
        db_factory,
        {"open_evt": lambda ctx: [enter_stop(w.ids["AAA"]), enter_stop(w.ids["AAA"])]},
        auto=False,
        max_positions=3,
    )
    first, second = (await w.engine.run_event("open_evt", DAY)).outcomes
    assert first.status == "pending"
    assert second.status == "rejected_by_risk" and second.rejection is not None
    assert second.rejection.check == "duplicate_symbol"
    assert second.rejection.detail == {"proposal_id": first.proposal_id}
    with db_factory() as s:
        assert second.signal_id is not None
        signal = s.get(m.Signal, second.signal_id)
    assert signal is not None and signal.evidence["rejection"]["check"] == "duplicate_symbol"


def test_two_processes_cannot_both_pass_the_duplicate_check(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker and cron backup fire the same entry at once. The risk check is slowed so both would pass the
    duplicate check before either creates its proposal, unless the advisory lock serialises them."""
    w: FakeWorld
    w = build_fake(
        db_factory, {"open_evt": lambda ctx: [enter_stop(w.ids["AAA"])]}, auto=False, max_positions=3
    )
    evaluate = w.engine._risk.evaluate

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.4)
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(w.engine._risk, "evaluate", slow)
    statuses: list[str] = []

    def fire() -> None:
        statuses.extend(o.status for o in asyncio.run(w.engine.run_event("open_evt", DAY)).outcomes)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == ["pending", "rejected_by_risk"]
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Proposal)).scalar_one() == 1


async def test_a_rejected_exit_is_critical(db_factory: sessionmaker[Session]) -> None:
    w = build_fake(db_factory, {"flatten": lambda ctx: [Exit(987654, "market", None, "flatten")]})
    (out,) = (await w.engine.run_event("flatten", DAY)).outcomes
    assert out.status == "rejected_by_risk"
    (alarm,) = events_at(db_factory, "critical")
    assert alarm.source == "risk" and "unprotected" in alarm.message


async def test_a_refired_exit_or_cancel_is_skipped_as_a_note(db_factory: sessionmaker[Session]) -> None:
    w: FakeWorld
    w = build_fake(
        db_factory,
        {
            "open_evt": lambda ctx: [enter_stop(w.ids["AAA"]), enter_stop(w.ids["BBB"])],
            "flatten": lambda ctx: [Exit(p.id, "market", None, "flatten") for p in ctx.positions],
            "tidy": lambda ctx: [Cancel(o.id, "stale") for o in ctx.working_orders if o.purpose == "entry"],
        },
        max_positions=2,
    )
    await w.engine.run_event("open_evt", DAY)
    await fill_aaa(w)  # AAA is held, BBB's entry is still working
    w.store.set("approval_mode", "manual", actor="test")
    (exit1,) = (await w.engine.run_event("flatten", DAY)).outcomes
    (exit2,) = (await w.engine.run_event("flatten", DAY)).outcomes
    (cancel1,) = (await w.engine.run_event("tidy", DAY)).outcomes
    (cancel2,) = (await w.engine.run_event("tidy", DAY)).outcomes
    assert (exit1.status, exit2.status) == ("pending", "skipped_duplicate")
    assert (cancel1.status, cancel2.status) == ("pending", "skipped_duplicate")
    assert exit2.signal_id is None and cancel2.signal_id is None
    notes = [e for e in events_at(db_factory, "info") if "re-fired" in e.message]
    assert [n.data["proposal_id"] for n in notes] == [exit1.proposal_id, cancel1.proposal_id]
    assert events_at(db_factory, "error", "critical") == []


async def test_an_entry_fill_without_an_owner_is_critical(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w: FakeWorld
    w = build_fake(db_factory, {"open_evt": lambda ctx: [enter_stop(w.ids["AAA"])]})
    await w.engine.run_event("open_evt", DAY)
    monkeypatch.setattr(w.engine.registry, "config_key", lambda config_id: None)
    await fill_aaa(w)
    (alarm,) = events_at(db_factory, "critical")
    assert alarm.source == "engine" and "no owning strategy" in alarm.message
    assert alarm.data["position_id"] == w.broker.open_positions()[0].id


async def test_a_failing_strategy_is_reported_in_the_result(db_factory: sessionmaker[Session]) -> None:
    def boom(ctx: StrategyContext) -> list[Intent]:
        raise RuntimeError("plug-in bug")

    w = build_fake(db_factory, {"open_evt": boom})
    res = await w.engine.run_event("open_evt", DAY)
    assert (res.strategies, res.failed, res.outcomes) == ([], ["alpha"], [])
    (err,) = events_at(db_factory, "error")
    assert "alpha" in err.message and err.data["event_key"] == "open_evt"


# --- P2-REVIEW ---------------------------------------------------------------------------------------------
async def test_a_strategy_disabled_mid_session_still_flattens_but_never_enters(
    db_factory: sessionmaker[Session],
) -> None:
    """BR-42: disabling a strategy that holds a position must not leave it open overnight."""
    w: FakeWorld
    w = build_fake(
        db_factory,
        {
            "open_evt": lambda ctx: [enter_stop(w.ids["AAA"])],
            "flatten": lambda ctx: [
                *(Exit(p.id, "market", None, "flatten") for p in ctx.positions),
                enter_stop(w.ids["BBB"]),  # a disabled owner's new entry is dropped
            ],
        },
        max_positions=2,
    )
    await w.engine.run_event("open_evt", DAY)
    await fill_aaa(w)
    w.engine.registry.update("alpha", enabled=False, actor="test")
    res = await w.engine.run_event("flatten", DAY)
    assert res.strategies == ["alpha"] and res.failed == []
    (out,) = res.outcomes
    assert isinstance(out.intent, Exit) and out.status == "submitted"
    # with nothing open or working, a disabled strategy doesn't run at all
    w.clock.set(T_FILL + timedelta(seconds=5))
    q = quote(w.ids["AAA"], "21.60", "21.62", "21.61", at=w.clock.now())
    await w.engine.on_quotes([q], w.clock.now())
    assert w.broker.open_positions() == []
    assert (await w.engine.run_event("flatten", DAY)).strategies == []


async def test_the_overlay_sees_exits_already_working_for_its_positions(
    db_factory: sessionmaker[Session],
) -> None:
    """SPEC §5.3: an exit order carries the entry strategy's config id; the overlay must still see it."""
    seen: list[list[int]] = []

    def watch(ctx: StrategyContext) -> list[Intent]:
        seen.append([o.id for o in ctx.working_orders if o.purpose == "exit"])
        return []

    overlay = fake_plugin("omega", {"overlay_evt": watch})
    overlay.kind = "overlay"  # type: ignore[attr-defined]
    w: FakeWorld
    w = build_fake(
        db_factory,
        {
            "open_evt": lambda ctx: [enter_stop(w.ids["AAA"])],
            "flatten": lambda ctx: [Exit(p.id, "market", None, "flatten") for p in ctx.positions],
        },
        extra_plugins={"omega": overlay},
    )
    w.engine.registry.ensure_defaults()
    await w.engine.run_event("open_evt", DAY)
    await fill_aaa(w)
    w.store.set("approval_mode", "manual", actor="test")
    (pending,) = (await w.engine.run_event("flatten", DAY)).outcomes
    w.engine.proposals.decide(pending.proposal_id, "approve", "web", "stephen")  # type: ignore[arg-type]
    exit_ids = [o.id for o in w.broker.working_orders() if o.purpose == "exit"]
    assert len(exit_ids) == 1
    await w.engine.run_event("overlay_evt", DAY)
    assert seen == [exit_ids]
