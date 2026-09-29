"""P5-T11: kill switches trip end to end from realistic fills (BR-41, O4; SPEC §6.3).

Everything real except the market and the phone: the engine built as `build_engine` builds it (SimBroker
with the quote fill model, ledger, RiskManager, ProposalService with the kill-switch entry guard,
KillSwitches) over a scripted test plug-in (registered through `StrategyRegistry(plugins=...)`, emitting
`EnterLong` stop entries and relying on the real protective-stop path through `on_fill`), a fake
`MarketDataView` with scripted quotes, the real `NotificationRelay` + `MessageRenderer` + `TelegramNotifier`
over `FakeTelegramApi`, and the P4 reset route through `make_client`. Settings: starting cash 720 USD, risk
2%, auto approval, `expectancy_min_trades` 5, other thresholds default (daily loss 5%, drawdown 15%,
expectancy threshold 0R).

Also here (orchestrator additions): the manual pause through the real Telegram bot and commands, and the kill
switches inside a replay (the real runner and replay engine over `FakeReplayMarket`), with the replay's own
run id and nothing relayed.
"""

import asyncio
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.factories import add_symbol
from tests.fakes_api import FakeCredentialStore, fixed_plan, make_services, test_core
from tests.fakes_replay import FakeReplayMarket, candle, seed_replay_world
from tests.fakes_telegram import FakeMessenger, FakeTelegramApi
from tests.strategies.fakes import FakeCatalysts, FakeData, quote
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer
from trader.adapters.telegram.commands import CommandDeps, Commands
from trader.api.routers.killswitch import router as killswitch_router
from trader.bootstrap import Core
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import Fill, FillEvent
from trader.db import models as m
from trader.engine.killswitch import KillSwitches, KillSwitchInputs
from trader.engine.orchestrator import Engine, IntentOutcome
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.notify.messages import MessageRenderer
from trader.notify.notifier import TelegramNotifier
from trader.notify.relay import NotificationRelay
from trader.replay import runner
from trader.replay.clock import ReplayClock
from trader.replay.setup import PinnedRegistry, build_replay_engine, pinned_views
from trader.replay.types import ReplayMarket, ReplayRequest, ReplayRun, StrategyOverride
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
DAY = date(2026, 10, 6)  # a Tuesday session
CHAT = 4242
KEY = "scripted"
Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
START = Decimal("720")
SIGNER = CallbackSigner.derive("p5-t11-test-secret")


def et(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


def sessions(n: int, first: date = DAY) -> list[date]:
    out = [first]
    while len(out) < n:
        out.append(CAL.next_session(out[-1]))
    return out


async def no_sleep(seconds: float) -> None:
    return None


# --- the scripted plug-in -----------------------------------------------------------------------------------
class ScriptParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_positions: int = 1


Script = Callable[[StrategyContext, str], Sequence[Intent]]


def scripted_plugin(schedule: dict[str, str], script: Script) -> type[Any]:
    """An entry plug-in: `schedule` maps event keys to session offsets, `script(ctx, key)` gives the event's
    intents, and every entry fill gets its protective stop at the entry's stop_loss (as orb_sip does)."""

    class _Scripted:
        key = KEY
        version = "0.0.1"
        kind = "entry"
        params_model = ScriptParams

        def __init__(self, params: ScriptParams) -> None:
            self.params = params

        def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
            return [ScheduledEvent(k, SessionOffset.parse(at)) for k, at in schedule.items()]

        async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
            return list(script(ctx, event.key))

        async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
            if fill.purpose != "entry" or fill.stop_loss is None:
                return []
            return [Exit(fill.position_id, "stop", fill.stop_loss, "protective_stop")]

    return _Scripted


# --- the live world -----------------------------------------------------------------------------------------
@dataclass
class World:
    factory: sessionmaker[Session]
    clock: FixedClock
    store: SettingsStore
    data: FakeData
    registry: StrategyRegistry
    engine: Engine
    ks: KillSwitches
    run_id: int
    ids: dict[str, int]
    core: Core
    api: FakeTelegramApi
    relay: NotificationRelay
    renderer: MessageRenderer
    pending: list[Intent] = field(default_factory=list)

    @property
    def client(self) -> TestClient:
        return make_client(make_services(self.core, registry=self.registry), killswitch_router)


def _script(w_ref: list[World]) -> Script:
    def script(ctx: StrategyContext, key: str) -> list[Intent]:
        w = w_ref[0]
        if key == "enter":
            out, w.pending = list(w.pending), []
            return out
        if key == "flatten":
            return [Exit(p.id, "market", None, "flatten_close") for p in ctx.positions]
        return []

    return script


async def build(factory: sessionmaker[Session], *, max_positions: int) -> World:
    clock = FixedClock(et(9, 30))
    store = SettingsStore(factory, now=clock.now)
    store.set("approval_mode", "auto", actor="test")
    store.set("killswitch.expectancy_min_trades", 5, actor="test")
    store.set("max_position_pct", "1", actor="test")  # SIZECAP: this scenario keeps the pre-cap sizing
    settings = store.load()
    assert (settings.starting_cash, settings.risk_pct) == (START, Decimal("0.02"))
    run = get_live_run(factory, clock, settings)
    with factory() as s:
        ids = {t: add_symbol(s, t, questrade_id=500 + i) for i, t in enumerate(("AAA", "BBB", "CCC", "DDD"))}
        s.commit()
    ref: list[World] = []
    plugin = scripted_plugin({"enter": "open+10m", "flatten": "close-10m"}, _script(ref))
    registry = StrategyRegistry(factory, clock, plugins={KEY: plugin})
    registry.ensure_defaults()
    registry.update(KEY, params={"max_positions": max_positions}, actor="test")
    broker = SimBroker(
        factory,
        clock,
        Ledger(CAL),
        QuoteFillModel(FillParams.from_settings(settings)),
        run.id,
        currency=settings.account_currency,
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
        proposals=ProposalService(factory, clock, store, broker, run.id, entry_blocked=ks.entry_guard()),
        risk=RiskManager(CAL),
        killswitches=ks,
        run_id=run.id,
    )
    api = FakeTelegramApi()
    renderer = MessageRenderer("https://trader.home", ZoneInfo("America/Edmonton"), clock=clock)
    relay = NotificationRelay(
        factory,
        clock,
        TelegramNotifier(api, CHAT, factory, clock, sleep=no_sleep),
        renderer,
        FakeMessenger(),
        run.id,
        settings=store.load,
    )
    await relay.pump()  # the cursors start here: no history is sent
    w = World(
        factory,
        clock,
        store,
        data,
        registry,
        engine,
        ks,
        run.id,
        ids,
        test_core(factory, clock),
        api,
        relay,
        renderer,
    )
    ref.append(w)
    return w


# --- driving the market -------------------------------------------------------------------------------------
async def tape(w: World, t: datetime, prices: dict[int, tuple[str, str]]) -> list[FillEvent]:
    """Quotes at `t` per symbol (bid, ask; last = bid), also served as the marks; one `on_quotes` call."""
    w.clock.set(t)
    quotes = [quote(sid, bid, ask, bid, at=t, age=0) for sid, (bid, ask) in prices.items()]
    for q in quotes:
        w.data.quote_map[q.symbol_id] = q
    return await w.engine.on_quotes(quotes, t)


async def enter(
    w: World, day: date, t: datetime, *symbols: int, entry: str = "5.00", stop_loss: str = "4.00"
) -> list[IntentOutcome]:
    w.clock.set(t)
    w.pending = [
        EnterLong(sid, "stop", Decimal(entry), None, Decimal(stop_loss), "scripted breakout", {})
        for sid in symbols
    ]
    return (await w.engine.run_event("enter", day)).outcomes


async def stop_out(
    w: World,
    day: date,
    t: datetime,
    sid: int,
    *,
    entry: str = "5.00",
    stop_loss: str = "4.00",
    bid: str = "3.99",
) -> FillEvent:
    """Enter at a stop, fill (ask at the stop), get the protective stop, then trade through it at `bid`."""
    (out,) = await enter(w, day, t, sid, entry=entry, stop_loss=stop_loss)
    assert out.status == "submitted", out
    ask = Decimal(entry)
    (fill,) = await tape(w, t + timedelta(seconds=1), {sid: (str(ask - Decimal("0.01")), str(ask))})
    assert fill.purpose == "entry"
    (protective,) = [
        o for o in w.engine.broker.working_orders() if o.purpose == "stop" and o.symbol_id == sid
    ]
    assert protective.stop == Decimal(stop_loss)
    (stopped,) = await tape(w, t + timedelta(seconds=2), {sid: (bid, str(Decimal(bid) + Decimal("0.02")))})
    assert stopped.purpose == "stop" and stopped.trade_id is not None
    return stopped


async def round_trip(w: World, day: date, t: datetime, sid: int, exit_bid: str) -> FillEvent:
    """Enter at 10.00 (stop_loss 8.00: 7 shares, so five trades fit one day's settled cash), then leave at
    the market through the plug-in's flatten event."""
    (out,) = await enter(w, day, t, sid, entry="10.00", stop_loss="8.00")
    assert out.status == "submitted", out
    await tape(w, t + timedelta(seconds=1), {sid: ("9.99", "10.00")})
    w.clock.set(t + timedelta(seconds=2))
    (exit_out,) = (await w.engine.run_event("flatten", day)).outcomes
    assert exit_out.status == "submitted"
    (done,) = await tape(
        w, t + timedelta(seconds=3), {sid: (exit_bid, str(Decimal(exit_bid) + Decimal("0.02")))}
    )
    assert done.purpose == "exit" and done.trade_id is not None
    return done


async def close_session(w: World, day: date) -> None:
    w.clock.set(CAL.session_close(day))
    assert await w.engine.end_of_session(day) == []


# --- reading the outcome ------------------------------------------------------------------------------------
def trips(w: World, switch: str | None = None) -> list[m.KillSwitchEvent]:
    with w.factory() as s:
        q = select(m.KillSwitchEvent).where(m.KillSwitchEvent.run_id == w.run_id)
        if switch is not None:
            q = q.where(m.KillSwitchEvent.switch == switch)
        return list(s.execute(q.order_by(m.KillSwitchEvent.id)).scalars())


def trip_events(w: World, run_id: int | None = None) -> list[m.EventLog]:
    with w.factory() as s:
        return list(
            s.execute(
                select(m.EventLog)
                .where(
                    m.EventLog.run_id == (run_id if run_id is not None else w.run_id),
                    m.EventLog.source == "killswitch",
                    m.EventLog.level == "error",
                )
                .order_by(m.EventLog.id)
            ).scalars()
        )


def trades(w: World) -> list[m.Trade]:
    with w.factory() as s:
        return list(
            s.execute(select(m.Trade).where(m.Trade.run_id == w.run_id).order_by(m.Trade.id)).scalars()
        )


def equity_now(w: World) -> Decimal:
    """Everything is closed: equity is the starting cash plus every trade's P&L (fees and slippage in)."""
    return START + sum((t.pnl for t in trades(w)), Decimal(0))


def phone(w: World, contains: str = "KILL SWITCH") -> list[str]:
    """The texts the fake Telegram received that contain `contains`."""
    return [c["text"] for c in w.api.calls_of("send_message") if contains in c["text"]]


def rejection_check(out: IntentOutcome) -> str | None:
    return out.rejection.check if out.rejection is not None else None


# --- 1. daily loss ------------------------------------------------------------------------------------------
async def test_daily_loss_trips_on_the_crossing_fill_alerts_once_and_clears_next_session(
    db_factory: sessionmaker[Session],
) -> None:
    w = await build(db_factory, max_positions=3)
    a, b, c, d = (w.ids[t] for t in ("AAA", "BBB", "CCC", "DDD"))

    await stop_out(w, DAY, et(9, 40), a)
    await stop_out(w, DAY, et(9, 50), b)
    assert trips(w) == []  # two stop-outs: about 4% down, under the 5% limit
    third = await stop_out(w, DAY, et(10, 0), c)

    (row,) = trips(w)
    equity = equity_now(w)
    assert (row.switch, row.session_date, row.threshold) == ("daily_loss_pct", DAY, Decimal("0.05"))
    assert row.tripped_at == third.ts  # the fill that crossed it
    assert row.value == ((START - equity) / START).quantize(Q6, ROUND_HALF_UP)
    assert row.value > Decimal("0.05")
    closed = trades(w)
    assert len(closed) == 3 and all(t.exit_reason == "protective_stop" for t in closed)
    assert all(t.fees_total > 0 and t.slippage_total > 0 for t in closed)  # the loss includes both
    (event,) = trip_events(w)
    assert event.message == "kill switch daily_loss_pct tripped: entries blocked"

    await w.relay.pump()
    (alert,) = phone(w)
    pct = f"{(row.value * 100).quantize(Decimal('0.01'), ROUND_HALF_UP):.2f}%"
    assert "KILL SWITCH: daily_loss_pct" in alert and f"Value {pct} vs threshold 5.00%" in alert
    assert "until the next session" in alert

    (fourth,) = await enter(w, DAY, et(10, 10), d)
    assert fourth.status == "rejected_by_risk" and rejection_check(fourth) == "kill_switch"
    await close_session(w, DAY)

    nxt = CAL.next_session(DAY)
    assert w.ks.blocking(w.run_id, nxt) is None  # it resets by itself
    (first,) = await enter(w, nxt, et(9, 40, day=nxt), d)
    assert first.status == "submitted"
    await w.relay.pump()
    assert len(phone(w)) == 1 and phone(w, "RESET") == []  # no reset alert for the automatic clear
    assert len(trips(w)) == 1 and trips(w)[0].reset_at is None


# --- 2. drawdown --------------------------------------------------------------------------------------------
async def test_drawdown_trips_blocks_later_sessions_and_rearms_from_the_equity_at_reset(
    db_factory: sessionmaker[Session],
) -> None:
    """Three losing sessions (two gapped stop-outs under 5% a day, then a bigger gap) take the equity 15%
    below its peak. A 15% fall in three sessions needs one day worse than 5% (1 - 0.95^3 < 15%), so the
    daily switch trips on the same fill; the drawdown switch trips once and alerts once."""
    w = await build(db_factory, max_positions=1)
    a = w.ids["AAA"]
    s1, s2, s3, s4, s5 = sessions(5)
    kw = {"entry": "10.00", "stop_loss": "9.50"}

    await stop_out(w, s1, et(9, 40, day=s1), a, bid="8.90", **kw)
    await close_session(w, s1)
    await stop_out(w, s2, et(9, 40, day=s2), a, bid="8.90", **kw)
    await close_session(w, s2)
    assert trips(w) == []
    await w.relay.pump()
    crossing = await stop_out(w, s3, et(9, 40, day=s3), a, bid="8.00", **kw)
    await close_session(w, s3)

    (dd,) = trips(w, "max_drawdown_pct")
    equity_at_trip = equity_now(w)
    assert dd.tripped_at == crossing.ts and dd.threshold == Decimal("0.15")
    assert dd.value == ((START - equity_at_trip) / START).quantize(Q6, ROUND_HALF_UP) >= Decimal("0.15")
    assert [t.switch for t in trips(w)] == ["daily_loss_pct", "max_drawdown_pct"]  # same fill, one call
    await w.relay.pump()
    (dd_alert,) = phone(w, "KILL SWITCH: max_drawdown_pct")
    assert "Entries blocked; reset in the web app." in dd_alert
    assert len(phone(w, "KILL SWITCH: daily_loss_pct")) == 1

    # blocked on the following sessions
    (out4,) = await enter(w, s4, et(9, 40, day=s4), a, **kw)
    assert out4.status == "rejected_by_risk" and rejection_check(out4) == "kill_switch"
    assert out4.rejection is not None and "max_drawdown_pct" in out4.rejection.reason
    await close_session(w, s4)
    (out5,) = await enter(w, s5, et(9, 40, day=s5), a, **kw)
    assert rejection_check(out5) == "kill_switch"

    # a reset needs a typed reason: a blank one changes nothing
    w.clock.set(et(9, 45, day=s5))
    assert w.client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": "  "}).status_code == 422
    assert w.ks.blocking(w.run_id, s5) == "max_drawdown_pct"
    resp = w.client.post(
        "/api/killswitch/max_drawdown_pct/reset", json={"reason": "reviewed the losing streak"}
    )
    assert resp.status_code == 200, resp.text
    (reset_row,) = trips(w, "max_drawdown_pct")
    assert (reset_row.reset_reason, reset_row.reset_by) == ("reviewed the losing streak", "web:stephen")
    with w.factory() as s:
        (audit,) = s.execute(
            select(m.AuditLog).where(m.AuditLog.action == "killswitch.reset:max_drawdown_pct")
        ).scalars()
    assert audit.actor == "web:stephen" and audit.after == {"reason": "reviewed the losing streak"}
    await w.relay.pump()
    (confirmation,) = phone(w, "KILL SWITCH RESET")
    assert "KILL SWITCH RESET: max_drawdown_pct" in confirmation
    assert "Reason: reviewed the losing streak" in confirmation

    # entries are allowed again; the entry fill (still 15% under the OLD peak) does not re-trip
    (again,) = await enter(w, s5, et(9, 50, day=s5), a, **kw)
    assert again.status == "submitted"
    await tape(w, et(9, 50, 1, day=s5), {a: ("9.99", "10.00")})
    assert len(trips(w, "max_drawdown_pct")) == 1
    # a further 15% fall from the equity at the reset trips it again
    await tape(w, et(9, 50, 2, day=s5), {a: ("6.00", "6.02")})
    first, second = trips(w, "max_drawdown_pct")
    assert first.reset_at is not None and second.reset_at is None and second.session_date == s5
    equity = equity_now(w)
    assert second.value == ((equity_at_trip - equity) / equity_at_trip).quantize(Q6, ROUND_HALF_UP)
    assert second.value >= Decimal("0.15")
    await w.relay.pump()
    assert len(phone(w, "KILL SWITCH: max_drawdown_pct")) == 2


# --- 3. expectancy ------------------------------------------------------------------------------------------
WIN, LOSS = "12.00", "9.10"  # about +0.98R and -0.46R on a 10.01 fill with an 8.00 stop


async def trade_series(w: World, day: date, exits: Sequence[str], start_minute: int = 35) -> None:
    for i, exit_bid in enumerate(exits):
        await round_trip(w, day, et(9, start_minute + 5 * i, day=day), w.ids["AAA"], exit_bid)


def mean_r(rows: Sequence[m.Trade]) -> Decimal:
    rs = [t.pnl_r for t in rows]
    assert all(r is not None for r in rs)
    return (sum((r for r in rs if r is not None), Decimal(0)) / len(rs)).quantize(Q4, ROUND_HALF_UP)


async def test_expectancy_trips_on_the_fifth_close_and_rearms_only_after_five_more(
    db_factory: sessionmaker[Session],
) -> None:
    w = await build(db_factory, max_positions=10)
    day2 = CAL.next_session(DAY)

    await trade_series(w, DAY, [WIN, LOSS, LOSS, LOSS])
    assert mean_r(trades(w)) <= 0 and trips(w) == []  # four trades: below the minimum of five
    await trade_series(w, DAY, [LOSS], start_minute=55)
    (row,) = trips(w)
    assert (row.switch, row.threshold) == ("expectancy", Decimal("0"))
    assert row.value == mean_r(trades(w)) and row.value <= 0
    await w.relay.pump()
    (alert,) = phone(w)
    assert "KILL SWITCH: expectancy" in alert and "Entries blocked; reset in the web app." in alert
    (blocked,) = await enter(w, DAY, et(10, 30), w.ids["BBB"], entry="10.00", stop_loss="9.50")
    assert rejection_check(blocked) == "kill_switch"
    await close_session(w, DAY)

    w.clock.set(et(9, 30, day=day2))
    resp = w.client.post("/api/killswitch/expectancy/reset", json={"reason": "new parameters under test"})
    assert resp.status_code == 200, resp.text
    await trade_series(w, day2, [LOSS, LOSS, LOSS, LOSS])
    assert len(trips(w)) == 1  # re-armed only after five more trades
    await trade_series(w, day2, [LOSS], start_minute=55)
    first, second = trips(w)
    since_reset = [t for t in trades(w) if first.reset_at is not None and t.closed_at > first.reset_at]
    assert len(since_reset) == 5 and second.switch == "expectancy" and second.reset_at is None
    assert second.value == mean_r(since_reset)
    assert trips(w, "daily_loss_pct") == []
    await w.relay.pump()
    assert (
        len(phone(w, "KILL SWITCH: expectancy")) == 2 and len(phone(w, "KILL SWITCH RESET: expectancy")) == 1
    )


async def test_a_positive_expectancy_after_five_trades_does_not_trip(
    db_factory: sessionmaker[Session],
) -> None:
    w = await build(db_factory, max_positions=10)
    await trade_series(w, DAY, [WIN, WIN, LOSS, LOSS, LOSS])
    assert len(trades(w)) == 5 and mean_r(trades(w)) > 0
    assert trips(w) == []
    await w.relay.pump()
    assert phone(w) == []


# --- 4. exits are never blocked -----------------------------------------------------------------------------
async def test_exits_stops_and_flatten_work_with_every_switch_tripped(
    db_factory: sessionmaker[Session],
) -> None:
    w = await build(db_factory, max_positions=2)
    a, b, c = w.ids["AAA"], w.ids["BBB"], w.ids["CCC"]
    outs = await enter(w, DAY, et(9, 40), a, b, entry="10.00", stop_loss="9.50")
    assert [o.status for o in outs] == ["submitted", "submitted"]

    settings = w.store.load()
    tripped = w.ks.evaluate(
        w.run_id,
        DAY,
        KillSwitchInputs(START, Decimal("500"), START, 5, Decimal("-1")),
        settings,
    )
    assert sorted(tripped) == ["daily_loss_pct", "expectancy", "max_drawdown_pct"]
    assert w.ks.pause(w.run_id, DAY, actor="web:stephen")
    assert sorted(x.switch for x in w.ks.active(w.run_id, DAY)) == [
        "daily_loss_pct",
        "expectancy",
        "manual_pause",
        "max_drawdown_pct",
    ]

    # the working entries fill; each protective stop proposal is still submitted
    fills = await tape(w, et(9, 41), {a: ("9.99", "10.00"), b: ("9.99", "10.00")})
    assert sorted(f.purpose for f in fills) == ["entry", "entry"]
    with w.factory() as s:
        stops = s.execute(
            select(m.Proposal.status).where(m.Proposal.run_id == w.run_id, m.Proposal.kind == "stop")
        ).all()
    assert [st for (st,) in stops] == ["submitted", "submitted"]
    # the stop fills
    (stopped,) = await tape(w, et(9, 42), {a: ("9.40", "9.42")})
    assert stopped.purpose == "stop"
    # a new entry is still blocked
    (blocked,) = await enter(w, DAY, et(9, 43), c, entry="10.00", stop_loss="9.50")
    assert rejection_check(blocked) == "kill_switch"
    # the flatten event exits the other position
    w.clock.set(et(15, 50))
    (flat,) = (await w.engine.run_event("flatten", DAY)).outcomes
    assert flat.status == "submitted"
    (exited,) = await tape(w, et(15, 50, 2), {b: ("10.20", "10.22")})
    assert exited.purpose == "exit"
    assert w.engine.broker.open_positions() == []
    with w.factory() as s:
        reasons = sorted(t.exit_reason for t in s.execute(select(m.Trade)).scalars())
    assert reasons == ["flatten_close", "protective_stop"]


# --- 5. one trip, one alert, under concurrency --------------------------------------------------------------
def test_two_fills_evaluating_at_once_write_one_trip_and_one_alert(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both stop fills cross the daily limit at the same moment in two threads; a barrier makes both compute
    their inputs before either evaluates, so only the kill-switch lock keeps it to one trip."""
    w = asyncio.run(build(db_factory, max_positions=2))
    a, b = w.ids["AAA"], w.ids["BBB"]
    asyncio.run(enter(w, DAY, et(9, 40), a, b, entry="10.00", stop_loss="9.50"))
    asyncio.run(tape(w, et(9, 41), {a: ("9.99", "10.00"), b: ("9.99", "10.00")}))
    assert len([o for o in w.engine.broker.working_orders() if o.purpose == "stop"]) == 2

    now = et(9, 45)
    w.clock.set(now)
    gap = {sid: quote(sid, "8.50", "8.52", "8.50", at=now, age=0) for sid in (a, b)}
    w.data.quote_map.update(gap)  # both marks are down: each fill alone sees the day past -5%
    barrier = threading.Barrier(2, timeout=10)
    inputs = w.ks.inputs

    def together(*args: Any, **kwargs: Any) -> KillSwitchInputs:
        out = inputs(*args, **kwargs)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return out

    monkeypatch.setattr(w.ks, "inputs", together)
    fills: list[FillEvent] = []

    def fill(sid: int) -> None:
        fills.extend(asyncio.run(w.engine.on_quotes([gap[sid]], now)))

    threads = [threading.Thread(target=fill, args=(sid,)) for sid in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(f.purpose for f in fills) == ["stop", "stop"]
    assert not barrier.broken  # both fills really evaluated at the same moment
    (row,) = trips(w)
    assert row.switch == "daily_loss_pct"
    assert len(trip_events(w)) == 1
    asyncio.run(w.relay.pump())
    assert len(phone(w)) == 1


# --- manual pause (orchestrator addition): through the real Telegram bot and commands -----------------------
async def test_manual_pause_from_telegram_blocks_entries_only_and_is_confirmed_on_the_phone(
    db_factory: sessionmaker[Session],
) -> None:
    w = await build(db_factory, max_positions=3)
    a, b = w.ids["AAA"], w.ids["BBB"]
    issuer = DbCallbackIssuer(db_factory, w.clock, SIGNER)
    commands = Commands(
        CommandDeps(
            factory=db_factory,
            clock=w.clock,
            calendar=CAL,
            settings=w.store.load,
            killswitches=w.ks,
            run_id=w.run_id,
            chat_id=CHAT,
            plan=fixed_plan(),
            fired=lambda d: set(),
            token_health=FakeCredentialStore().health,
            quotes=w.data.quotes,
            messenger=FakeMessenger(),
            issuer=issuer,
            render=w.renderer,
        )
    )
    bot = TelegramBot(
        w.api,
        CHAT,
        db_factory,
        w.clock,
        issuer,
        SIGNER,
        w.engine.proposals.decide,
        commands,
        w.renderer,
        w.run_id,
        settings=w.store.load,
        sleep=no_sleep,
    )

    (out,) = await enter(w, DAY, et(9, 40), a, entry="10.00", stop_loss="9.50")
    await tape(w, et(9, 40, 1), {a: ("9.99", "10.00")})  # held, with its protective stop

    w.clock.set(et(9, 45))
    await bot.handle_update(w.api.text_update("/pause", CHAT))
    sends = w.api.calls_of("send_message")
    confirm = sends[-1]
    assert confirm["text"].startswith("Pause new entries?")
    yes = next(btn.callback_data for row in confirm["buttons"] for btn in row if btn.text == "Yes, pause")
    await bot.handle_update(w.api.callback_update(yes, CHAT, len(sends)))
    assert w.api.calls_of("answer_callback")[-1]["text"] == "Paused: new entries are blocked."
    assert w.ks.blocking(w.run_id, DAY) == "manual_pause"
    with db_factory() as s:
        (audit,) = s.execute(select(m.AuditLog).where(m.AuditLog.action == "killswitch.pause")).scalars()
    assert audit.actor == "telegram"

    (blocked,) = await enter(w, DAY, et(9, 46), b, entry="10.00", stop_loss="9.50")
    assert rejection_check(blocked) == "kill_switch"
    assert blocked.rejection is not None and "manual_pause" in blocked.rejection.reason
    (stopped,) = await tape(w, et(9, 47), {a: ("9.40", "9.42")})  # the stop still works
    assert stopped.purpose == "stop"
    assert trips(w, "manual_pause")[0].reset_at is None

    w.clock.set(et(9, 50))
    await bot.handle_update(w.api.text_update("/resume", CHAT))
    assert w.api.last_sent()["text"] == "Manual pause lifted."
    (allowed,) = await enter(w, DAY, et(9, 51), b, entry="10.00", stop_loss="9.50")
    assert allowed.status == "submitted"
    assert out.status == "submitted"


# --- kill switches inside a replay (orchestrator addition) --------------------------------------------------
REPLAY_DAY2 = CAL.next_session(DAY)
WALL = datetime(2026, 10, 10, 12, 0, tzinfo=ET)  # the Saturday after: nothing of these days is live


async def test_kill_switches_work_inside_a_replay_with_its_own_run_id_and_nothing_is_relayed(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real runner and replay engine over 1-minute bars: three entries each stopped out in their own bar
    (the same-bar worst case) take the replay's day past -5%; the trip carries the replay's run id and time,
    the fourth entry is rejected, the next replayed session trades again, and the live relay sends nothing."""
    ids: dict[str, int] = {}
    events = {"e1": "open+5m", "e2": "open+15m", "e3": "open+25m", "e4": "open+35m"}
    by_event = {"e1": "AAA", "e2": "BBB", "e3": "CCC", "e4": "DDD"}

    def script(ctx: StrategyContext, key: str) -> list[Intent]:
        if ctx.session_date != DAY and key != "e1":
            return []
        sid = ids[by_event[key]]
        return [EnterLong(sid, "stop", Decimal("5.00"), None, Decimal("4.00"), "scripted breakout", {})]

    plugins = {KEY: scripted_plugin(events, script)}
    wall = FixedClock(WALL.astimezone(UTC))
    world = seed_replay_world(db_factory, clock=wall, tickers=("AAA", "BBB", "CCC", "DDD"), plugins=plugins)
    ids.update(world.symbols)
    store = SettingsStore(db_factory, now=wall.now)

    # the live relay, started before the replay
    api = FakeTelegramApi()
    relay = NotificationRelay(
        db_factory,
        wall,
        TelegramNotifier(api, CHAT, db_factory, wall, sleep=no_sleep),
        MessageRenderer("https://trader.home", ZoneInfo("America/Edmonton"), clock=wall),
        FakeMessenger(),
        world.live_run_id,
        settings=store.load,
    )
    await relay.pump()

    registry = StrategyRegistry(db_factory, wall, plugins=plugins)
    run_id = runner.create_replay(
        db_factory,
        wall,
        CAL,
        store,
        registry,
        ReplayRequest(DAY, REPLAY_DAY2, strategies={KEY: StrategyOverride(params={"max_positions": 3})}),
        "web:stephen",
    )

    def market_factory(run: ReplayRun, clock: ReplayClock) -> ReplayMarket:
        market = FakeReplayMarket(clock)
        for ticker, sid in ids.items():
            market.add_symbol(ticker, sid)
        for key, ticker in by_event.items():
            if ticker == "DDD":
                continue
            at = SessionOffset.parse(events[key]).resolve(CAL, DAY)
            market.set_minute_bars(ids[ticker], DAY, [candle(at, "5.00", "5.05", "3.90", "3.95")])
        at2 = SessionOffset.parse(events["e1"]).resolve(CAL, REPLAY_DAY2)
        market.set_minute_bars(
            ids["AAA"],
            REPLAY_DAY2,
            [
                candle(at2, "5.00", "5.10", "4.95", "5.05"),
                candle(at2 + timedelta(minutes=1), "5.05", "5.05", "3.90", "3.95"),
            ],
        )
        return market

    def engine_factory(run: ReplayRun, clock: ReplayClock, market: ReplayMarket, catalysts: Any) -> Engine:
        pinned = PinnedRegistry(db_factory, clock, pinned_views(db_factory, run), run.id, plugins=plugins)
        return build_replay_engine(db_factory, clock, CAL, run, market, catalysts, pinned)

    monkeypatch.setattr(runner, "load_all", lambda: plugins)
    deps = runner.ReplayDeps(
        factory=db_factory,
        wall=wall,
        calendar=CAL,
        market_factory=market_factory,
        catalysts_factory=lambda run: FakeCatalysts(),
        engine_factory=engine_factory,
    )
    done = await runner.run_replay(deps, run_id)
    assert done.status == "completed", done.error

    with db_factory() as s:
        rows = list(s.execute(select(m.KillSwitchEvent).order_by(m.KillSwitchEvent.id)).scalars())
        replay_trades = list(
            s.execute(select(m.Trade).where(m.Trade.run_id == run_id).order_by(m.Trade.id)).scalars()
        )
        signals = list(
            s.execute(select(m.Signal).where(m.Signal.run_id == run_id).order_by(m.Signal.id)).scalars()
        )
        proposals = {
            p.signal_id: p.status
            for p in s.execute(select(m.Proposal).where(m.Proposal.run_id == run_id)).scalars()
        }
        trip_rows = list(
            s.execute(
                select(m.EventLog).where(m.EventLog.source == "killswitch", m.EventLog.level == "error")
            ).scalars()
        )
        live_rows = s.execute(
            select(func.count())
            .select_from(m.KillSwitchEvent)
            .where(m.KillSwitchEvent.run_id == world.live_run_id)
        ).scalar_one()

    (row,) = rows
    assert (row.run_id, row.switch, row.session_date) == (run_id, "daily_loss_pct", DAY)
    # e3 enters at 09:55 and is stopped out in the bar ending 09:56 ET: the trip carries replay time
    assert row.tripped_at == et(9, 56)
    assert row.value is not None and row.value > Decimal("0.05")
    assert live_rows == 0
    (trip_event,) = trip_rows
    assert trip_event.run_id == run_id and trip_event.ts == row.tripped_at
    day1 = [t for t in replay_trades if t.opened_at < CAL.session_open(REPLAY_DAY2)]
    assert len(day1) == 3 and all(t.exit_reason == "protective_stop" for t in day1)

    entries = [sg for sg in signals if sg.intent.get("type") == "enter_long"]
    by_day = [
        (sg.session_date, sg.event_key, sg.evidence.get("rejection", {}).get("check")) for sg in entries
    ]
    assert by_day[:4] == [
        (DAY, "e1", None),
        (DAY, "e2", None),
        (DAY, "e3", None),
        (DAY, "e4", "kill_switch"),
    ]
    day2 = [sg for sg in signals if sg.session_date == REPLAY_DAY2 and sg.intent.get("type") == "enter_long"]
    (entry2,) = day2
    assert proposals[entry2.id] == "submitted"  # the daily switch cleared at the next replayed session

    await relay.pump()
    assert api.calls_of("send_message") == []
