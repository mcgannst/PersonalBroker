"""P3-T13: whole worker days through the real composition root (`trader.runtime`), with a fake Telegram.

Everything is real except the edges: `FakeQuestrade` (market data), the P2-T15 fake FinViz and fake
Claude (catalysts), a `FakeTelegramApi` (the chat), the fake Questrade token, a `FixedClock` and the
testcontainers database. `rt.run_worker()` composes the worker exactly as production does (engine per
session, `live_day_plan`, `fire_event`, the relay, the bot with its signed callbacks and the kill-switch
guarded decider, the commands); only `Worker.run`'s endless loop is replaced by a driver that calls the
real `Worker.step()` at chosen fake times (the plan's "drive the Worker with step()"), then pumps the
real relay and writes the heartbeat, as the worker's own relay and heartbeat tasks would. Taps and texts
reach the real bot through its own polling loop (`TelegramBot.run`, one getUpdates per poll). The cron
side goes through the runtime's job bodies (`preopen_job`, `checkin_job`, `event_backup`,
`postclose_job`, `send_premarket_brief`). No network, no real sleeping.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import trader.bootstrap
import trader.runtime as rt
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_telegram import FakeTelegramApi
from tests.integration.test_simulated_day import QT, FakeClaude, FakeFinviz, daily, opening
from trader.adapters.claude.catalyst import CatalystClassifier, CatalystService, CatalystStore
from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.telegram.api import TelegramNotSentError
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.types import Update
from trader.bootstrap import Core
from trader.config import EnvSettings
from trader.crypto import Crypto
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.runs import get_live_run
from trader.jobs.nightly import NightlyDeps, run_nightly
from trader.jobs.premarket import PremarketDeps, run_premarket
from trader.jobs.runner import run_job_async
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle
from trader.notify.relay import NotificationRelay
from trader.settings_store import SettingsStore
from trader.worker import StepReport, Worker

pytestmark = pytest.mark.db

CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tue, a normal session (13:30Z open, 20:00Z close)
EARLY = date(2026, 11, 27)  # Fri after Thanksgiving: 13:00 ET close
CHAT = 4242  # a private chat: the sender is the chat's user
STRANGER = 777
SECRET = "p3-t13-session-secret"
BOT_TOKEN = "123456:P3T13-FAKE-TOKEN-NOT-REAL"
OUTAGE = TelegramNotSentError(None, "Connection refused")  # Telegram unreachable: surely not delivered


def et(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


def label(text: str) -> str:
    """A message's first line without its HTML tags: what Stephen sees as its title."""
    return re.sub(r"<[^>]+>", "", text.split("\n", 1)[0]).strip()


# --- the fake Telegram chat ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sent:
    message_id: int
    text: str
    buttons: Any

    @property
    def label(self) -> str:
        return label(self.text)

    def button(self, text: str) -> str:
        """The callback data of the button whose text contains `text`."""
        return next(b.callback_data for row in self.buttons for b in row if text in b.text)


class ChatApi(FakeTelegramApi):
    """FakeTelegramApi plus: each delivered message with its id (`sent`), an outage switch (`down`: every
    call fails as if Telegram were unreachable), and a one-shot poll (`poll_stop` is set by the next
    getUpdates, so the real `TelegramBot.run` loop handles one batch of updates and returns)."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Sent] = []
        self.down = False
        self.poll_stop: asyncio.Event | None = None

    def _outage(self, method: str) -> None:
        if self.down:
            self.fail(method, OUTAGE)

    async def send_message(self, chat_id: int, text: str, buttons: Any = (), silent: bool = False) -> int:
        self._outage("send_message")
        message_id = await super().send_message(chat_id, text, buttons, silent)
        self.sent.append(Sent(message_id, text, buttons))
        return message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str, buttons: Any = ()) -> None:
        self._outage("edit_message")
        await super().edit_message(chat_id, message_id, text, buttons)

    async def edit_buttons(self, chat_id: int, message_id: int, buttons: Any = ()) -> None:
        self._outage("edit_buttons")
        await super().edit_buttons(chat_id, message_id, buttons)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self._outage("answer_callback")
        await super().answer_callback(callback_id, text)

    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        updates = await super().get_updates(offset, timeout)
        if self.poll_stop is not None:
            self.poll_stop.set()
        return updates

    # --- reading the chat ---
    def labels(self) -> list[str]:
        return [s.label for s in self.sent]

    def one(self, prefix: str) -> Sent:
        found = [s for s in self.sent if s.label.startswith(prefix)]
        assert len(found) == 1, (prefix, self.labels())
        return found[0]

    def answers(self) -> list[str | None]:
        return [kw["text"] for kw in self.calls_of("answer_callback")]

    def edits_of(self, message_id: int) -> list[str]:
        return [kw["text"] for kw in self.calls_of("edit_message") if kw["message_id"] == message_id]


# --- the world: a test Core with fake edges -----------------------------------------------------------------


class FakeAuth:
    """The Questrade token: always fresh (no real token chain in tests)."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock

    def access(self) -> None:
        return None

    def health(self) -> TokenHealth:
        now = self.clock.now()
        return TokenHealth(True, now + timedelta(minutes=30), now - timedelta(hours=1), None)


class QtContext:
    """`rt.questrade_client` stand-in: the same FakeQuestrade for every process."""

    def __init__(self, qt: FakeQuestrade) -> None:
        self.qt = qt

    async def __aenter__(self) -> FakeQuestrade:
        return self.qt

    async def __aexit__(self, *exc: object) -> None:
        return None


@dataclass
class World:
    core: Core
    clock: FixedClock
    api: ChatApi
    fq: FakeQuestrade
    finviz: FakeFinviz
    claude: FakeClaude
    monkeypatch: pytest.MonkeyPatch
    sleeps: list[float] = field(default_factory=list)

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory

    @property
    def store(self) -> SettingsStore:
        return self.core.settings

    def catalysts(self) -> CatalystService:
        return CatalystService(
            self.factory,
            self.clock,
            CatalystStore(self.factory, self.clock),
            CatalystClassifier(self.claude, self.store.load),
            self.finviz,
        )

    async def fake_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def make_env() -> EnvSettings:
    return EnvSettings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr("postgresql+psycopg://unused"),
        migration_database_url=SecretStr("postgresql+psycopg://unused"),
        app_encryption_key=SecretStr(Fernet.generate_key().decode()),
        session_secret=SecretStr(SECRET),
        telegram_bot_token=SecretStr(BOT_TOKEN),
        telegram_chat_id=CHAT,
        public_base_url="https://trader.test",
        tz_display="America/Edmonton",
        anthropic_api_key=None,
    )


@pytest.fixture
def world(
    db_factory: sessionmaker[Session], migrated_engine: SqlEngine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[World]:
    clock = FixedClock(et(12, 0, day=date(2026, 10, 5)))
    env = make_env()
    store = SettingsStore(db_factory, now=clock.now)
    core = Core(
        env, migrated_engine, db_factory, Crypto(env.app_encryption_key.get_secret_value()), clock, CAL, store
    )
    w = World(core, clock, ChatApi(), FakeQuestrade(), FakeFinviz(), FakeClaude(), monkeypatch)
    store.set("approval_mode", "manual", actor="test")
    store.set("auto_flatten_on_expiry", True, actor="test")
    store.set("max_position_pct", "1", actor="test")  # SIZECAP: this scenario keeps the pre-cap sizing

    async def open_catalysts(core: Core, stack: Any) -> CatalystService:
        return w.catalysts()

    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    monkeypatch.setattr(rt, "build_telegram_api", lambda env: w.api)
    monkeypatch.setattr(rt, "questrade_client", lambda core: QtContext(w.fq))
    monkeypatch.setattr(rt, "open_catalysts", open_catalysts)
    monkeypatch.setattr(rt, "questrade_auth", lambda core: FakeAuth(clock))
    yield w


# --- market data and the morning cron jobs ------------------------------------------------------------------


def minute_bars(day: date, o: str) -> list[Candle]:
    """A few regular-hours 1-minute bars (the post-close archive), plus one pre-market bar it must drop."""
    start = CAL.session_open(day)
    price = Decimal(o)
    bars = [
        Candle(
            start + timedelta(minutes=i),
            start + timedelta(minutes=i + 1),
            price,
            price,
            price,
            price,
            100,
            None,
        )
        for i in range(0, 60, 10)
    ]
    early = start - timedelta(hours=5)
    bars.append(Candle(early, early + timedelta(minutes=1), price, price, price, price, 10, None))
    return bars


def seed_market(w: World, day: date) -> None:
    """P2-T15's market: AAA gaps up 5% on a strong catalyst and breaks out of its opening range."""
    for ticker, qt_id in QT.items():
        w.fq.add_symbol(ticker, qt_id)
    for d in CAL.sessions_before(day, 20):
        w.fq.add_bars(QT["AAA"], "OneDay", [daily(d, "19.50", "20.50", "20.00")])
        w.fq.add_bars(QT["BBB"], "OneDay", [daily(d, "19.50", "20.50", "20.00")])
        w.fq.add_bars(QT["SPY"], "OneDay", [daily(d, "499.50", "500.50", "500.00")])
    for d in CAL.sessions_before(day, 14):
        for t in ("AAA", "BBB"):
            w.fq.add_bars(QT[t], "FiveMinutes", [opening(d, "20.00", "20.10", "19.90", "20.05", 1000)])
    for t, o in (("AAA", "21.40"), ("BBB", "20.30"), ("SPY", "501.00")):
        w.fq.add_bars(QT[t], "OneMinute", minute_bars(day, o))
    w.fq.add_bars(QT["SPY"], "FiveMinutes", [opening(day, "500.00", "501.00", "499.90", "500.80", 90000)])


def quote(w: World, ticker: str, bid: str, ask: str, last: str) -> None:
    w.fq.set_quote(QT[ticker], bid, ask, last, w.clock.now())


def restamp(w: World) -> None:
    """A live feed: every quote is re-quoted at the current time, same prices (otherwise the broker
    rightly alerts a working order's stale quote)."""
    for qt_id, q in list(w.fq.quote_map.items()):
        w.fq.set_quote(qt_id, str(q.bid), str(q.ask), str(q.last), w.clock.now())


async def morning_jobs(w: World, day: date) -> None:
    """The nightly job (the previous session's evening), the 08:00 pre-market scan and its brief."""
    seed_market(w, day)
    prev = CAL.sessions_before(day, 1)[0]
    w.clock.set(et(20, 0, day=prev))
    detail = await run_nightly(NightlyDeps(w.factory, w.clock, CAL, w.finviz, w.fq, w.store.load()), day)
    assert detail["source"] == "finviz" and detail["universe"] == 3

    w.clock.set(et(8, 0, day=day))
    quote(w, "AAA", "20.99", "21.01", "21.00")  # +5% gap
    quote(w, "BBB", "20.09", "20.11", "20.10")
    data = MarketDataService(w.factory, w.clock, CAL, w.fq)
    deps = PremarketDeps(w.factory, w.clock, w.finviz, data, w.catalysts(), w.store.load(), CAL)
    out = await run_job_async(w.factory, w.clock, "premarket", day, lambda: run_premarket(deps, day))
    assert out.status == "succeeded" and out.detail["candidates"] == 1
    await rt.send_premarket_brief(w.core, day, out.detail["brief"])


# --- driving the worker -------------------------------------------------------------------------------------


class Driver:
    """Drives one worker process built by `rt.run_worker()`: its real `step()`, its real relay pump, its
    heartbeat, and its real bot (through the bot's own polling loop)."""

    def __init__(self, w: World, worker: Worker) -> None:
        self.w = w
        self.worker = worker
        relay = worker.deps.relay
        bot = worker.deps.bot
        assert relay is not None and bot is not None, (
            "Telegram is configured: the worker has a relay and a bot"
        )
        self.relay: NotificationRelay = relay.__self__  # type: ignore[attr-defined]
        self.bot: TelegramBot = bot.__self__  # type: ignore[attr-defined]
        self.relay.notifier.sleep = w.fake_sleep  # type: ignore[attr-defined]  # pacing and retry waits
        self.reports: list[StepReport] = []

    async def at(self, when: datetime) -> StepReport:
        """One worker iteration at `when`: the step, then the relay pump and a heartbeat (the worker's own
        relay and heartbeat tasks do these on the same cadence)."""
        self.w.clock.set(when)
        restamp(self.w)
        report = await self.worker.step()
        self.worker._beat(self.worker._hb_phase)
        assert self.worker.deps.relay is not None
        await self.worker.deps.relay()
        self.reports.append(report)
        return report

    async def walk(self, start: datetime, end: datetime, every: float = 2.0) -> list[StepReport]:
        """Step every `every` seconds from `start` to `end` inclusive (the worker's quote_poll cadence)."""
        out: list[StepReport] = []
        t = start
        while t <= end:
            out.append(await self.at(t))
            t += timedelta(seconds=every)
        return out

    async def poll(self) -> None:
        """One getUpdates through the bot's own loop: every queued tap or text is handled."""
        stop = asyncio.Event()
        self.w.api.poll_stop = stop
        try:
            await asyncio.wait_for(self.bot.run(stop), timeout=10)
        finally:
            self.w.api.poll_stop = None

    async def tap(self, message: Sent, button: str, *, chat: int = CHAT) -> str | None:
        """Stephen (or `chat`) taps a button of `message`; returns the callback answer (None if none)."""
        before = len(self.w.api.calls_of("answer_callback"))
        data = message.button(button)
        self.w.api.queue_updates(self.w.api.callback_update(data, chat, message.message_id))
        await self.poll()
        answers = self.w.api.answers()
        return answers[-1] if len(answers) > before else None

    async def say(self, text: str) -> list[Sent]:
        """Stephen sends a text; returns the bot's replies."""
        before = len(self.w.api.sent)
        self.w.api.queue_updates(self.w.api.text_update(text, CHAT))
        await self.poll()
        return self.w.api.sent[before:]


Script = Callable[[Driver], Awaitable[None]]


async def with_worker(w: World, script: Script) -> None:
    """Run `script` inside a worker process composed by `rt.run_worker()`. The worker's endless loop is
    replaced by the script's `step()` calls; the composition (and its exit stack) is the real one."""

    async def drive(self: Worker, stop: asyncio.Event, *, once: bool = False) -> None:
        await script(Driver(w, self))

    w.monkeypatch.setattr(Worker, "run", drive)
    assert await rt.run_worker() == 0


# --- the day, in parts --------------------------------------------------------------------------------------


@dataclass
class Times:
    orb: datetime
    entry_cancel: datetime
    overlay: datetime
    flatten: datetime
    close: datetime


def day_times(w: World, day: date) -> Times:
    plan = rt.plan_builder(w.core)(day)
    at = {e.key: e.at for e in plan.events}
    assert plan.close is not None
    return Times(at["orb_open"], at["entry_cancel"], at["overlay_decision"], at["flatten"], plan.close)


async def pre_open(d: Driver, day: date) -> None:
    """09:15 the worker idles (and beats); 09:20 the pre-open check finds everything healthy."""
    report = await d.at(et(9, 15, day=day))
    assert report.phase == "pre_market" and report.fired == []
    await d.at(et(9, 19, 50, day=day))  # the worker's idle poll (and heartbeat) just before
    d.w.clock.set(et(9, 20, day=day))
    out = await rt.preopen_job(d.w.core, day, force=False)
    assert out.status == "succeeded"
    assert out.detail["ok"] is True, out.detail["checks"]
    assert [c["name"] for c in out.detail["checks"]] == [
        "token",
        "universe",
        "open_bar_stats",
        "premarket",
        "kill_switches",
        "worker",
    ]


async def to_orb(d: Driver, day: date, t: Times) -> Sent:
    """The session opens; the 09:35 opening bars arrive; orb_open fires at the first step at or after
    09:35:05 and the entry proposal goes out with its buttons."""
    for tk, bar in (
        ("AAA", opening(day, "21.00", "21.50", "20.90", "21.40", 5000)),
        ("BBB", opening(day, "20.00", "20.40", "19.95", "20.30", 3000)),
    ):
        d.w.fq.add_bars(QT[tk], "FiveMinutes", [bar])
    steps = await d.walk(t.orb - timedelta(seconds=310), t.orb)
    fired = [(r.now, f.key, f.status) for r in steps for f in r.fired]
    assert fired == [(t.orb, "orb_open", "fired")]  # exactly once, at 09:35:05
    entry = d.w.api.sent[-1]
    assert entry.label == "ENTRY: BUY 33 AAA"
    assert [b.text for b in entry.buttons[0]] == ["✅ Approve", "❌ Reject"]
    return entry


async def approve(d: Driver, message: Sent) -> None:
    """Tap Approve: the callback is answered first, then the message is edited to its final text."""
    calls_before = len(d.w.api.calls)
    assert await d.tap(message, "Approve") == "Approved"
    after = [name for name, _ in d.w.api.calls[calls_before:] if name != "get_updates"]
    assert after == ["answer_callback", "edit_message"]
    assert "Approved via telegram" in d.w.api.edits_of(message.message_id)[-1]


async def entry_to_stop(d: Driver, day: date, entry: Sent) -> tuple[Sent, Sent]:
    """Tap Approve on the entry at 09:35:30; the 09:36 quote fills it; the protective stop proposal and the
    fill message go out (in that order: the relay sends pending proposals first); tap Approve on the stop."""
    d.w.clock.set(et(9, 35, 30, day=day))
    await approve(d, entry)
    assert working(d.w, "entry") == [("entry", Decimal("21.5100"), 33)]

    d.w.clock.set(et(9, 36, day=day))
    quote(d.w, "AAA", "21.52", "21.55", "21.53")
    report = await d.at(et(9, 36, day=day))
    assert report.fills == 1
    stop_msg, fill_msg = d.w.api.sent[-2:]
    assert stop_msg.label == "PROTECTIVE STOP: SELL 33 AAA"
    assert fill_msg.label == "ENTRY FILLED" and "BOUGHT 33 AAA @ 21.5608, stop 21.41" in fill_msg.text

    d.w.clock.set(et(9, 36, 10, day=day))
    await approve(d, stop_msg)
    assert working(d.w, "stop") == [("stop", Decimal("21.4100"), 33)]
    return stop_msg, fill_msg


async def checkin(d: Driver, day: date, hh: int, mm: int, t: Times) -> None:
    """A cron check-in (its own process): the status message, then a backup fire of every due event,
    which finds them settled by the worker. After the close (13:30 on an early-close day) it is skipped."""
    d.w.clock.set(et(hh, mm, 5, day=day))
    out = await rt.checkin_job(d.w.core, day, f"{hh}:{mm:02d}", force=False)
    assert out.status == "succeeded"
    if d.w.clock.now() >= t.close:
        assert out.detail == {"skipped": "after close"}
    else:
        assert out.detail["sent"] is True
        assert [f for f in out.detail["fired"] if f["status"] == "fired"] == []  # the worker fired them
    await d.at(d.w.clock.now())


async def midday(d: Driver, day: date, t: Times) -> None:
    """entry_cancel at its time (nothing left to cancel: the entry filled) and the 11:30 check-in."""
    report = await d.at(t.entry_cancel)
    assert [(f.key, f.status) for f in report.fired] == [("entry_cancel", "fired")]
    await checkin(d, day, 11, 30, t)


async def afternoon(
    d: Driver, day: date, t: Times, *, before_expiry: Script | None = None
) -> tuple[Sent, Sent]:
    """The overlay holds (SPY +0.4%); flatten proposes the exit; nobody taps it; at its expiry it is
    auto-submitted (P2) and the next quote fills it; the session closes and the worker ends it."""
    d.w.clock.set(t.overlay)
    quote(d.w, "SPY", "501.99", "502.01", "502.00")
    quote(d.w, "AAA", "21.70", "21.72", "21.71")
    report = await d.at(t.overlay)
    assert [(f.key, f.status) for f in report.fired] == [("overlay_decision", "fired")]
    assert d.w.api.sent[-1].label.startswith("SPY OVERLAY: HOLD")

    report = await d.at(t.flatten)
    assert [(f.key, f.status) for f in report.fired] == [("flatten", "fired")]
    exit_msg = d.w.api.sent[-1]
    assert exit_msg.label == "EXIT: SELL 33 AAA" and exit_msg.buttons

    expiry = t.flatten + timedelta(seconds=d.w.store.load().proposal_ttl_exit_seconds)
    await d.walk(t.flatten + timedelta(seconds=2), expiry - timedelta(seconds=2))
    assert proposal_statuses(d.w)[-1] == ("exit", "pending", None)
    d.w.clock.set(expiry)
    if before_expiry is not None:
        await before_expiry(d)
    quote(d.w, "AAA", "21.90", "21.92", "21.91")
    await d.at(expiry)  # tick expires it and auto-submits the market exit
    assert proposal_statuses(d.w)[-1] == ("exit", "submitted", "auto")
    assert d.w.api.edits_of(exit_msg.message_id)[-1].endswith("<b>Approved (auto)</b>")
    report = await d.at(expiry + timedelta(seconds=2))
    assert report.fills == 1
    flat_msg = d.w.api.sent[-1]
    assert flat_msg.label == "FLATTENED (end of day)"
    assert "SOLD 33 AAA @ 21.889" in flat_msg.text and "+$10.82 (+2.17R)" in flat_msg.text

    report = await d.at(t.close)
    assert report.phase == "after_close" and report.ended
    await d.at(t.close + timedelta(seconds=2))
    return exit_msg, flat_msg


async def evening(d: Driver, day: date) -> Sent:
    """16:15 post-close: the daily summary with the Rules-followed buttons; Stephen taps Yes."""
    d.w.clock.set(et(16, 15, day=day))
    out = await rt.postclose_job(d.w.core, day, force=False)
    assert out.status == "succeeded" and out.detail["summary_sent"] is True
    assert out.detail["open_positions"] == []
    summary = d.w.api.sent[-1]
    assert summary.label == f"Daily summary {day.isoformat()}"
    assert [b.text for b in summary.buttons[0]] == ["Yes", "No"]
    assert "STILL OPEN" not in summary.text

    d.w.clock.set(et(16, 20, day=day))
    assert await d.tap(summary, "Yes") == "Saved"
    assert d.w.api.calls_of("edit_buttons")[-1] == {
        "chat_id": CHAT,
        "message_id": summary.message_id,
        "buttons": (),
    }
    assert d.w.api.sent[-1].label.startswith(f"Journal {day.isoformat()}")
    await d.at(et(16, 20, 2, day=day))
    return summary


# --- reading the database -----------------------------------------------------------------------------------


def working(w: World, purpose: str) -> list[tuple[str, Decimal | None, int]]:
    with w.factory() as s:
        rows = s.execute(
            select(m.Order)
            .where(m.Order.purpose == purpose, m.Order.status == "working")
            .order_by(m.Order.id)
        ).scalars()
        return [(o.purpose, o.stop_price, o.qty) for o in rows]


def proposal_statuses(w: World) -> list[tuple[str, str, str | None]]:
    with w.factory() as s:
        rows = s.execute(select(m.Proposal).order_by(m.Proposal.id)).scalars()
        return [(p.kind, p.status, p.decided_via) for p in rows]


def event_runs(w: World) -> list[tuple[str, str]]:
    with w.factory() as s:
        rows = s.execute(
            select(m.JobRun.job, m.JobRun.status)
            .where(m.JobRun.job.startswith("event:"))
            .order_by(m.JobRun.id)
        ).all()
    return [(j, st) for j, st in rows]


def open_positions(w: World) -> int:
    with w.factory() as s:
        return len(s.execute(select(m.Position).where(m.Position.closed_at.is_(None))).scalars().all())


def journal(w: World, day: date) -> m.Journal:
    run = get_live_run(w.factory, w.clock, w.store.load())
    with w.factory() as s:
        return s.get_one(m.Journal, (run.id, day))


def notifications(w: World) -> list[tuple[str | None, str]]:
    with w.factory() as s:
        rows = s.execute(select(m.Notification).order_by(m.Notification.id)).scalars()
        return [(n.dedupe_key, n.status) for n in rows]


def assert_ends_flat(w: World) -> None:
    assert open_positions(w) == 0  # BR-42
    with w.factory() as s:
        live = s.execute(select(m.Order).where(m.Order.status == "working")).scalars().all()
        trade = s.execute(select(m.Trade)).scalar_one()
    assert live == []
    assert trade.exit_reason == "flatten_close" and trade.pnl == Decimal("10.8157")


ALERT_TITLES = ("ALERT", "ESCALATION", "JOB FAILED", "KILL SWITCH", "TOKEN FAILURE")


def assert_no_alerts(w: World) -> None:
    assert [lb for lb in w.api.labels() if lb.startswith(ALERT_TITLES)] == []


# --- 1. the manual day --------------------------------------------------------------------------------------


def expected_day(day: date, *, checkin_1330: bool = True) -> list[str]:
    d = day.isoformat()
    return [
        f"Pre-market brief {d}",
        f"Pre-open {d}",
        "ENTRY: BUY 33 AAA",
        "PROTECTIVE STOP: SELL 33 AAA",
        "ENTRY FILLED",
        "Check-in 11:30",
        *(["Check-in 13:30"] if checkin_1330 else []),
        "SPY OVERLAY: HOLD",
        "EXIT: SELL 33 AAA",
        "FLATTENED (end of day)",
        f"Daily summary {d}",
        f"Journal {d}",
    ]


def assert_sequence(w: World, expected: list[str]) -> None:
    labels = w.api.labels()
    assert len(labels) == len(expected), [s.text for s in w.api.sent]
    for got, want in zip(labels, expected, strict=True):
        assert got.startswith(want), (got, want, labels)


async def test_manual_day_through_telegram(world: World) -> None:
    """A whole manual-approval day: brief, pre-open, the 09:35 proposal, a tap, the fill, the protective
    stop tapped, check-ins, the overlay hold, the flatten auto-submitted on expiry, the summary and the
    Rules-followed answer, in this order, each message once; the day ends flat."""
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)
    assert (t.orb, t.flatten) == (et(9, 35, 5), et(15, 50))

    async def script(d: Driver) -> None:
        await pre_open(d, DAY)
        entry = await to_orb(d, DAY, t)
        await entry_to_stop(d, DAY, entry)
        await d.at(et(10, 0))
        await midday(d, DAY, t)
        await checkin(d, DAY, 13, 30, t)
        await afternoon(d, DAY, t)
        await evening(d, DAY)

    await with_worker(world, script)

    assert_sequence(world, expected_day(DAY))
    assert_no_alerts(world)
    assert_ends_flat(world)
    assert proposal_statuses(world) == [
        ("entry", "submitted", "telegram"),
        ("stop", "submitted", "telegram"),
        ("exit", "submitted", "auto"),
    ]
    assert event_runs(world) == [
        ("event:orb_open", "succeeded"),
        ("event:entry_cancel", "succeeded"),
        ("event:overlay_decision", "succeeded"),
        ("event:flatten", "succeeded"),
    ]
    j = journal(world, DAY)
    assert (j.rules_followed, j.answered_via) == (True, "telegram")
    sent = [st for _, st in notifications(world)]
    assert set(sent) == {"sent"}
    keys = [k for k, _ in notifications(world) if k is not None]
    assert len(keys) == len(set(keys))


# --- 2. restarts --------------------------------------------------------------------------------------------


async def test_worker_restart_mid_session(world: World) -> None:
    """Review Focus 4: the worker process is replaced twice, while the entry proposal waits for a tap
    (09:35:20) and with a working protective stop (10:00). Each new worker is composed from the database
    alone: no second orb_open run, no second proposal message, the old message's buttons still work, the
    stop keeps working, and the day still ends flat with one summary."""
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)
    held: dict[str, Sent] = {}

    async def first(d: Driver) -> None:
        await pre_open(d, DAY)
        held["entry"] = await to_orb(d, DAY, t)
        await d.at(et(9, 35, 20))

    async def second(d: Driver) -> None:
        sent_before = len(world.api.sent)
        steps = await d.walk(et(9, 35, 22), et(9, 35, 28))
        assert [f for r in steps for f in r.fired] == []  # orb_open is settled in the database
        assert len(world.api.sent) == sent_before  # the pending proposal is not sent again
        await entry_to_stop(d, DAY, held["entry"])  # the first worker's buttons, the second worker's bot
        await d.at(et(10, 0))

    async def third(d: Driver) -> None:
        sent_before = len(world.api.sent)
        report = await d.at(et(10, 0, 30))
        assert report.fired == [] and report.fills == 0
        assert len(world.api.sent) == sent_before
        assert working(world, "stop") == [("stop", Decimal("21.4100"), 33)]
        await midday(d, DAY, t)
        await checkin(d, DAY, 13, 30, t)
        await afternoon(d, DAY, t)
        await evening(d, DAY)

    await with_worker(world, first)
    await with_worker(world, second)
    await with_worker(world, third)

    assert_sequence(world, expected_day(DAY))
    assert_no_alerts(world)
    assert_ends_flat(world)
    assert [job for job, _ in event_runs(world)] == [
        "event:orb_open",
        "event:entry_cancel",
        "event:overlay_decision",
        "event:flatten",
    ]
    assert [lb for lb in world.api.labels() if lb.startswith("Daily summary")] == ["Daily summary 2026-10-06"]


# --- 3. cron backups ----------------------------------------------------------------------------------------


async def test_cron_backup_after_worker_fired(world: World) -> None:
    """Review Focus 1: the 09:36 orb_open backup after the worker fired at 09:35:05 is `skipped` and makes
    no proposal. The 12:55 flatten backup on a normal day is `too_early`, and the 15:55 flatten backup after
    the worker's 15:50 flatten is `skipped`. Each event ran once."""
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)
    results: dict[str, str] = {}

    async def flatten_backup(d: Driver) -> None:
        assert d.w.clock.now() == et(15, 55)  # the crontab's 15:55 line
        [r] = await rt.event_backup(d.w.core, "flatten", DAY)
        results["15:55 flatten"] = r.status

    async def script(d: Driver) -> None:
        entry = await to_orb(d, DAY, t)
        await entry_to_stop(d, DAY, entry)
        d.w.clock.set(et(9, 36, 20))  # the crontab's 09:36 line (20 s after the stop tap)
        [r] = await rt.event_backup(d.w.core, "orb_open", DAY)
        results["09:36 orb_open"] = r.status
        assert len(proposal_statuses(world)) == 2  # entry and stop: no second entry
        await d.at(et(10, 0))
        await midday(d, DAY, t)
        d.w.clock.set(et(12, 55))
        [r] = await rt.event_backup(d.w.core, "flatten", DAY)
        results["12:55 flatten"] = r.status
        await afternoon(d, DAY, t, before_expiry=flatten_backup)

    await with_worker(world, script)

    assert results == {"09:36 orb_open": "skipped", "12:55 flatten": "too_early", "15:55 flatten": "skipped"}
    assert event_runs(world) == [
        ("event:orb_open", "succeeded"),
        ("event:entry_cancel", "succeeded"),
        ("event:overlay_decision", "succeeded"),
        ("event:flatten", "succeeded"),
    ]
    assert [k for k, _, _ in proposal_statuses(world)] == ["entry", "stop", "exit"]
    assert_ends_flat(world)
    assert_no_alerts(world)


# --- 4. a foreign chat --------------------------------------------------------------------------------------


async def test_foreign_chat_cannot_approve(world: World) -> None:
    """Review Focus 2: an Approve tap with the right signed data from another chat, or from another sender
    in the private chat, is ignored (no answer, no decision) and logged without its data. The proposal stays
    pending and Stephen's own tap still works."""
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)

    async def script(d: Driver) -> None:
        entry = await to_orb(d, DAY, t)
        d.w.clock.set(et(9, 35, 20))
        assert await d.tap(entry, "Approve", chat=STRANGER) is None
        data = entry.button("Approve")
        d.w.api.queue_updates(d.w.api.callback_update(data, CHAT, entry.message_id, from_id=STRANGER))
        await d.poll()
        d.w.api.queue_updates(d.w.api.text_update("/pause", STRANGER))
        await d.poll()
        assert d.w.api.answers() == [] and d.w.api.calls_of("edit_message") == []
        assert proposal_statuses(world) == [("entry", "pending", None)]
        assert working(world, "entry") == []
        assert d.w.api.sent[-1] is entry  # nothing was sent to anyone
        d.w.clock.set(et(9, 35, 30))
        await approve(d, entry)  # the ignored taps did not use up the nonce

    await with_worker(world, script)

    with world.factory() as s:
        events = (
            s.execute(select(m.EventLog).where(m.EventLog.source == "telegram").order_by(m.EventLog.id))
            .scalars()
            .all()
        )
    assert [(e.level, e.data.get("kind")) for e in events] == [
        ("warning", "callback"),
        ("warning", "callback"),
        ("warning", "text"),
    ]
    assert {e.data.get("chat_id") for e in events} == {STRANGER, CHAT}
    assert all("pause" not in str(e.data) and "p:" not in str(e.data) for e in events)  # no text, no data
    assert proposal_statuses(world) == [("entry", "submitted", "telegram")]


# --- 5. an early-close day ----------------------------------------------------------------------------------


async def test_early_close_day_through_telegram(world: World) -> None:
    """Review Focus 5: on Fri 2026-11-27 (13:00 ET close) the overlay decides at 12:30 and the flatten at
    12:50, the exit expires and fills before 13:00, the worker ends the session at 13:00, the 13:30
    check-in is skipped (no message) and the post-close summary follows. The day ends flat."""
    await morning_jobs(world, EARLY)
    t = day_times(world, EARLY)
    assert (t.orb, t.overlay, t.flatten, t.close) == (
        et(9, 35, 5, day=EARLY),
        et(12, 30, day=EARLY),
        et(12, 50, day=EARLY),
        et(13, 0, day=EARLY),
    )

    async def script(d: Driver) -> None:
        await pre_open(d, EARLY)
        entry = await to_orb(d, EARLY, t)
        await entry_to_stop(d, EARLY, entry)
        await d.at(et(10, 0, day=EARLY))
        await midday(d, EARLY, t)
        await afternoon(d, EARLY, t)
        assert [r.now for r in d.reports if r.ended] == [et(13, 0, day=EARLY)]
        await checkin(d, EARLY, 13, 30, t)  # after the close: skipped, nothing sent
        await evening(d, EARLY)

    await with_worker(world, script)

    assert_sequence(world, expected_day(EARLY, checkin_1330=False))
    assert_no_alerts(world)
    assert_ends_flat(world)
    with world.factory() as s:
        fills = s.execute(select(m.Fill).order_by(m.Fill.id)).scalars().all()
        end = s.execute(select(m.JobRun).where(m.JobRun.job == "session_end")).scalar_one()
    assert all(f.ts < et(13, 0, day=EARLY) for f in fills)
    assert (end.session_date, end.status) == (EARLY, "succeeded")


# --- 6. Telegram down for a while ---------------------------------------------------------------------------


async def outage_morning(d: Driver, t: Times) -> None:
    """Telegram becomes unreachable at 09:35:50, just after the entry was approved, and comes back at
    09:37:00. Meanwhile the entry fills and the protective stop is proposed: trading carries on."""
    entry = await to_orb(d, DAY, t)
    d.w.clock.set(et(9, 35, 30))
    await approve(d, entry)
    d.w.api.down = True
    d.w.clock.set(et(9, 36))
    quote(d.w, "AAA", "21.52", "21.55", "21.53")
    steps = await d.walk(et(9, 35, 50), et(9, 36, 58))
    assert sum(r.fills for r in steps) == 1  # the entry filled while Telegram was down
    assert open_positions(d.w) == 1
    assert proposal_statuses(d.w)[-1] == ("stop", "pending", None)
    d.w.api.down = False
    await d.walk(et(9, 37), et(9, 37, 10))


async def test_telegram_outage_trading_continues(world: World) -> None:
    """Review Focus 3: while Telegram is down the worker keeps trading (the fill is booked, the stop is
    proposed, nothing raises, heartbeats go on) and every failure is recorded. Once Telegram is back,
    /pending re-sends the stop proposal with fresh buttons and a tap approves it. The day ends flat."""
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)

    async def script(d: Driver) -> None:
        await pre_open(d, DAY)
        await outage_morning(d, t)
        with world.factory() as s:
            hb = s.get_one(m.WorkerHeartbeat, "worker")
        assert hb.beat_at == et(9, 37, 10) and hb.phase == "session"
        replies = await d.say("/pending")
        stops = [r for r in replies if r.label == "PROTECTIVE STOP: SELL 33 AAA"]
        assert len(stops) == 1
        d.w.clock.set(et(9, 37, 20))
        await approve(d, stops[0])
        assert working(world, "stop") == [("stop", Decimal("21.4100"), 33)]
        await d.at(et(10, 0))
        await midday(d, DAY, t)
        await afternoon(d, DAY, t)
        await evening(d, DAY)

    await with_worker(world, script)

    assert_ends_flat(world)
    with world.factory() as s:
        failures = (
            s.execute(select(m.EventLog).where(m.EventLog.source.in_(("telegram", "telegram.proposal"))))
            .scalars()
            .all()
        )
    assert failures  # each failed send is on record (never with the token)
    assert all(BOT_TOKEN not in (e.message + str(e.data)) for e in failures)
    assert [lb for lb in world.api.labels() if lb.startswith("Daily summary")] == ["Daily summary 2026-10-06"]


async def test_telegram_outage_alerts_delivered_later_exactly_once(world: World) -> None:
    """Review Focus 3 and the task: after an outage, what could not be sent (the protective stop proposal
    and the entry fill) is delivered once Telegram is back, exactly once each, without Stephen asking."""
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)

    async def script(d: Driver) -> None:
        await outage_morning(d, t)
        await d.walk(et(9, 37, 12), et(9, 37, 40))

    await with_worker(world, script)

    labels = world.api.labels()
    assert labels.count("PROTECTIVE STOP: SELL 33 AAA") == 1, labels
    assert labels.count("ENTRY FILLED") == 1, labels


# --- 7. /pause blocks a tapped entry, a stop still goes through ---------------------------------------------


async def pause(d: Driver) -> None:
    """/pause, then the confirmation tap (the bot's pause callback through Commands and KillSwitches)."""
    [confirm] = await d.say("/pause")
    assert confirm.label.startswith("Pause new entries?")
    assert await d.tap(confirm, "Yes, pause") == "Paused: new entries are blocked."


async def entry_to_stop_paused(d: Driver, entry: Sent) -> None:
    """Approve the entry (not paused), fill it at 09:36, /pause, then approve the stop: it goes through."""
    d.w.clock.set(et(9, 35, 30))
    await approve(d, entry)
    d.w.clock.set(et(9, 36))
    quote(d.w, "AAA", "21.52", "21.55", "21.53")
    assert (await d.at(et(9, 36))).fills == 1
    stop_msg = d.w.api.sent[-2]
    assert stop_msg.label == "PROTECTIVE STOP: SELL 33 AAA"
    d.w.clock.set(et(9, 36, 5))
    await pause(d)
    d.w.clock.set(et(9, 36, 10))
    await approve(d, stop_msg)
    assert working(d.w, "stop") == [("stop", Decimal("21.4100"), 33)]


async def test_pause_blocks_a_tapped_entry_but_a_stop_goes_through(world: World) -> None:
    """SPEC §6.3 / BR-34: /pause lands while the 09:35 entry waits, so tapping Approve submits nothing and
    the message says why. After /resume a re-fired orb_open proposes again and is approved. Once it fills,
    /pause again: the protective stop is still approved by a tap, and the exit still flattens. Ends flat."""
    await morning_jobs(world, DAY)
    t = day_times(world, DAY)

    async def script(d: Driver) -> None:
        entry = await to_orb(d, DAY, t)
        d.w.clock.set(et(9, 35, 10))
        await pause(d)
        run = get_live_run(world.factory, world.clock, world.store.load())
        assert KillSwitches(world.factory, world.clock).blocking(run.id, DAY) == "manual_pause"

        d.w.clock.set(et(9, 35, 20))
        answer = await d.tap(entry, "Approve")
        assert answer is not None and answer.startswith("Not submitted, entry blocked: ")
        assert "manual_pause" in answer
        assert "Rejected: entry blocked" in d.w.api.edits_of(entry.message_id)[-1]
        assert working(world, "entry") == []
        assert proposal_statuses(world) == [("entry", "rejected", "telegram")]

        d.w.clock.set(et(9, 35, 25))
        [resumed] = await d.say("/resume")
        assert resumed.text == "Manual pause lifted."
        d.w.clock.set(et(9, 35, 26))
        [again] = await rt.event_backup(
            d.w.core, "orb_open", DAY, force=True
        )  # trader event orb_open --force
        assert again.status == "fired"
        await d.at(et(9, 35, 28))
        second = d.w.api.sent[-1]
        assert second.label == "ENTRY: BUY 33 AAA" and second is not entry
        await entry_to_stop_paused(d, second)
        await d.at(et(10, 0))
        await midday(d, DAY, t)
        await afternoon(d, DAY, t)  # the exit is never guarded: it flattens while paused

    await with_worker(world, script)

    assert_ends_flat(world)
    assert proposal_statuses(world) == [
        ("entry", "rejected", "telegram"),
        ("entry", "submitted", "telegram"),
        ("stop", "submitted", "telegram"),
        ("exit", "submitted", "auto"),
    ]
    with world.factory() as s:
        audit = s.execute(
            select(m.AuditLog.actor, m.AuditLog.action).where(m.AuditLog.action.like("killswitch%"))
        ).all()
    assert audit and {actor for actor, _ in audit} == {"telegram"}
