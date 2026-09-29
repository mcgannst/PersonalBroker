"""DB-T10 acceptance tests 1-3 (integration): the mark tap and publisher change nothing a worker day does.

The P3-T13 manual worker day (`tests/integration/test_worker_day.py`: the real `rt.run_worker()` composition,
the cron jobs through the runtime, a fake Telegram chat, a fake Questrade that logs every call with its
arguments) runs in fresh databases:

- "on": the composition under test. The engines and the bot really use the `QuoteTap` (the 9:35 opening-bar
  batch, the 2 s quote polls and `_account` all pass through it), and the worker's own `MarkPublisher` takes
  a pass after every worker step (as its task would on its cadence);
- "off": `live_marks` monkeypatched to `(client, None)`: exactly the trunk composition (no tap, no publisher);
- "failing": as "on", with the publisher's database step raising on every pass.

Trading rows (`test_decisions_day.trading_rows`), the whole Telegram chat, the Questrade call log and the
event-loop iterations of every worker step (the tap adds no scheduling point of its own, so an extra yield or
a quote delayed by a few milliseconds shows) are identical in all three, and right after every publisher
pass no other backend holds a lock in the `trader` schema (a pass leaves nothing a trading transaction
could wait on). Only "on" has marks (the traded symbol's last observed quote and a bar for every
minute it was observed while held or working); "failing" has exactly one `marks` warning event and no marks.
"""

import asyncio
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

import trader.bootstrap
import trader.runtime as rt
from tests.conftest import alembic_config
from tests.fakes_questrade import FakeQuestrade
from tests.integration import test_worker_day as wd
from tests.integration.test_decisions_day import trading_rows
from tests.integration.test_simulated_day import QT, FakeClaude, FakeFinviz
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.bootstrap import Core
from trader.crypto import Crypto
from trader.db import models as m
from trader.db.session import make_engine, make_session_factory
from trader.market.clock import FixedClock
from trader.market.types import Candle, Interval
from trader.marks.publisher import MarkPublisher
from trader.marks.tap import QuoteTap
from trader.marks.types import MARKS_SOURCE, ObservedQuote, PublishStep
from trader.settings_store import SettingsStore
from trader.worker import StepReport

pytestmark = pytest.mark.db

DAY = wd.DAY
# Locks on `trader` tables that other backends of THIS database hold (pg_locks is cluster-wide).
FOREIGN_LOCKS = text(
    """
    SELECT count(*) FROM pg_locks l
    JOIN pg_class c ON c.oid = l.relation
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'trader' AND l.pid <> pg_backend_pid() AND l.granted
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
    """
)


class LoggingQuestrade(FakeQuestrade):
    """FakeQuestrade whose call log also keeps every argument (ids, candle windows, deadlines)."""

    def __init__(self) -> None:
        super().__init__()
        self.detail: list[tuple[Any, ...]] = []

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        self.detail.append(("quotes", tuple(ids)))
        return await super().quotes(ids)

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        self.detail.append(("candles", symbol_id, start, end, interval))
        return await super().candles(symbol_id, start, end, interval)

    async def candles_many(self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None) -> Any:
        self.detail.append(("candles_many", tuple(reqs), deadline_s))
        return await super().candles_many(reqs, deadline_s=deadline_s)


def build_world(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> wd.World:
    """`test_worker_day`'s `world` fixture for any database, with the argument-logging fake Questrade."""
    engine = factory.kw["bind"]
    assert isinstance(engine, SqlEngine)
    clock = FixedClock(wd.et(12, 0, day=date(2026, 10, 5)))
    env = wd.make_env()
    store = SettingsStore(factory, now=clock.now)
    core = Core(env, engine, factory, Crypto(env.app_encryption_key.get_secret_value()), clock, wd.CAL, store)
    w = wd.World(core, clock, wd.ChatApi(), LoggingQuestrade(), FakeFinviz(), FakeClaude(), monkeypatch)
    store.set("approval_mode", "manual", actor="test")
    store.set("auto_flatten_on_expiry", True, actor="test")
    store.set("max_position_pct", "1", actor="test")  # SIZECAP: this scenario keeps the pre-cap sizing

    async def open_catalysts(core: Core, stack: Any) -> Any:
        return w.catalysts()

    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    monkeypatch.setattr(rt, "build_telegram_api", lambda env: w.api)
    monkeypatch.setattr(rt, "questrade_client", lambda core: wd.QtContext(w.fq))
    monkeypatch.setattr(rt, "open_catalysts", open_catalysts)
    monkeypatch.setattr(rt, "questrade_auth", lambda core: wd.FakeAuth(clock))
    return w


class Day:
    """What one manual day left behind, beyond the database."""

    def __init__(self, w: wd.World) -> None:
        self.w = w
        self.steps: list[PublishStep] = []
        self.passes: list[tuple[list[ObservedQuote], PublishStep]] = []
        self.tapped: list[bool] = []
        self.iterations: list[int] = []  # event-loop iterations of each worker step (step, beat, relay pump)
        self.foreign_locks: list[int] = []  # trader-schema locks of other backends right after each pass

    @property
    def chat(self) -> list[tuple[str, Any]]:
        """Every message with its buttons; a callback's random nonce and signature are left out (the rest of
        its data, e.g. `p:1:a`, is kept)."""
        return [
            (s.text, [[(b.text, ":".join(b.callback_data.split(":")[:3])) for b in row] for row in s.buttons])
            for s in self.w.api.sent
        ]

    @property
    def replies(self) -> tuple[list[str], list[str | None], list[str]]:
        """The message edits, the callback answers and the order of every Telegram call."""
        api = self.w.api
        edits = [kw["text"] for kw in api.calls_of("edit_message")]
        return edits, api.answers(), [name for name, _ in api.calls]

    @property
    def calls(self) -> tuple[list[tuple[str, int]], list[tuple[Any, ...]]]:
        fq = self.w.fq
        assert isinstance(fq, LoggingQuestrade)
        return fq.calls, fq.detail


async def manual_day(w: wd.World, mode: str) -> Day:
    """The manual day of `test_manual_day_through_telegram` in `mode` ("on", "off" or "failing")."""
    day = Day(w)
    if mode == "off":
        w.monkeypatch.setattr(rt, "live_marks", lambda core, client, run_id: (client, None))
    if mode == "failing":

        def write(self: MarkPublisher, observed: Any, run_id: int, now: datetime) -> tuple[int, int]:
            raise RuntimeError("database down")

        w.monkeypatch.setattr(MarkPublisher, "_write", write)
    real_pass = MarkPublisher._pass

    def recording_pass(self: MarkPublisher, observed: Sequence[ObservedQuote]) -> PublishStep:
        step = real_pass(self, observed)
        day.passes.append((list(observed), step))
        return step

    w.monkeypatch.setattr(MarkPublisher, "_pass", recording_pass)
    original = wd.Driver.at
    selector: Any = asyncio.get_running_loop()._selector  # type: ignore[attr-defined]
    real_select = selector.select
    selects = [0]

    def counting_select(timeout: float | None = None) -> Any:
        selects[0] += 1
        return real_select(timeout)

    w.monkeypatch.setattr(selector, "select", counting_select)

    async def at(self: wd.Driver, when: Any) -> StepReport:
        before = selects[0]
        report = await original(self, when)
        day.iterations.append(selects[0] - before)
        publisher = self.worker.deps.marks
        engines = self.worker.deps.engine_for.__self__  # type: ignore[attr-defined]
        day.tapped.append(isinstance(engines._client, QuoteTap))
        if mode == "off":
            assert publisher is None
        else:
            assert publisher is not None
            day.steps.append(await publisher.run_once())
            with w.core.factory() as s:
                day.foreign_locks.append(int(s.execute(FOREIGN_LOCKS).scalar_one()))
        return report

    w.monkeypatch.setattr(wd.Driver, "at", at)
    await wd.morning_jobs(w, DAY)
    t = wd.day_times(w, DAY)

    async def script(d: wd.Driver) -> None:
        await wd.pre_open(d, DAY)
        entry = await wd.to_orb(d, DAY, t)
        await wd.entry_to_stop(d, DAY, entry)
        await d.at(wd.et(10, 0))
        await wd.midday(d, DAY, t)
        await wd.checkin(d, DAY, 13, 30, t)
        await wd.afternoon(d, DAY, t)
        await wd.evening(d, DAY)

    await wd.with_worker(w, script)
    wd.assert_sequence(w, wd.expected_day(DAY))
    wd.assert_no_alerts(w)
    wd.assert_ends_flat(w)
    return day


async def run_day(factory: sessionmaker[Session], mode: str) -> Day:
    with pytest.MonkeyPatch.context() as mp:
        w = build_world(factory, mp)
        return await manual_day(w, mode)


@pytest.fixture
def third_factory(pg_url: str) -> Iterator[sessionmaker[Session]]:
    """One more fresh, migrated database in this worker's own container (unique name: parallel-safe)."""
    name = f"marks_{uuid4().hex[:12]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))
        url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
        command.upgrade(alembic_config(url), "head")
        engine = make_engine(url)
        try:
            yield make_session_factory(engine)
        finally:
            engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    finally:
        admin.dispose()


def marks_rows(factory: sessionmaker[Session]) -> tuple[list[Any], list[Any]]:
    with factory() as s:
        tick: dict[int, str] = {int(i): str(t) for i, t in s.execute(select(m.Symbol.id, m.Symbol.ticker))}
        quotes = [
            (tick[r.symbol_id], r.bid, r.ask, r.last, r.quote_time, r.observed_at, r.is_halted, r.run_id)
            for r in s.execute(select(m.QuoteMark)).scalars()
        ]
        bars = [
            (tick[r.symbol_id], r.minute_start, r.open, r.high, r.low, r.close, r.samples)
            for r in s.execute(select(m.MarkBar).order_by(m.MarkBar.minute_start)).scalars()
        ]
    return quotes, bars


def marks_events(factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    with factory() as s:
        rows = s.execute(select(m.EventLog).where(m.EventLog.source == MARKS_SOURCE)).scalars().all()
    return [(r.level, r.message) for r in rows]


def live_run(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return int(s.execute(select(m.Run.id).where(m.Run.mode == "live")).scalar_one())


def _minute(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(second=0, microsecond=0)


# --- 1 and 2 ------------------------------------------------------------------------------------------------


async def test_1_2_a_worker_day_is_identical_with_the_marks_on_and_off(
    db_factory: sessionmaker[Session],
    third_factory: sessionmaker[Session],
) -> None:
    on_factory, off_factory = db_factory, third_factory
    on = await run_day(on_factory, "on")
    off = await run_day(off_factory, "off")

    # 1: the same trades, the same chat (every message, unfiltered), the same Questrade requests
    assert trading_rows(on_factory) == trading_rows(off_factory)
    assert trading_rows(on_factory)["trades"]
    assert on.chat == off.chat and on.replies == off.replies
    assert on.calls == off.calls
    # the 9:35 batch, tapped: one quotes request over the universe (QUOTEBAR; it was candles_many)
    assert any(c[0] == "quotes" and len(c[1]) > 1 for c in on.calls[1])
    assert on.tapped and all(on.tapped) and not any(off.tapped)
    # the tap adds no event-loop iteration to any worker step (the 9:35 batch included), and a pass leaves
    # no lock behind
    assert len(on.iterations) == len(off.iterations) > 100
    assert on.iterations == off.iterations
    assert on.foreign_locks and set(on.foreign_locks) == {0}

    # 2: "on" has the traded symbol's marks, "off" has none; no marks warning in either
    quotes, bars = marks_rows(on_factory)
    assert marks_rows(off_factory) == ([], [])
    assert marks_events(on_factory) == [] and marks_events(off_factory) == []
    assert all(s.skipped != "error" for s in on.steps)
    written = [(obs, step) for obs, step in on.passes if step.marks_written]
    assert written, "the publisher never wrote"
    aaa = QT["AAA"]
    seen = [o for obs, _ in written for o in obs if o.qt_id == aaa]
    last = max(seen, key=lambda o: o.observed_at)
    (row,) = quotes
    assert row[0] == "AAA" and row[7] == live_run(on_factory)
    assert row[1:7] == (
        last.quote.bid,
        last.quote.ask,
        last.quote.last,
        last.quote.last_trade_time,
        last.observed_at,
        False,
    )
    # a bar for exactly the minutes AAA was observed while held or working, OHLC from those observations
    by_minute: dict[datetime, list[Decimal]] = {}
    for o in seen:
        assert o.quote.last is not None
        by_minute.setdefault(_minute(o.observed_at), []).append(o.quote.last)
    assert [b[0] for b in bars] == ["AAA"] * len(bars)
    assert {b[1] for b in bars} == set(by_minute)
    for _, minute, o, h, lo, c, n in bars:
        prices = by_minute[minute]
        assert (o, h, lo, c, n) == (prices[0], max(prices), min(prices), prices[-1], len(prices))
    # held from the 09:36 fill to the expiry flatten: the bars lie in the traded window
    with on_factory() as s:
        fills = s.execute(select(m.Fill.ts).order_by(m.Fill.ts)).scalars().all()
    assert fills and min(b[1] for b in bars) <= _minute(fills[0]) <= max(b[1] for b in bars)


# --- 3 ------------------------------------------------------------------------------------------------------


async def test_3_a_publisher_failing_all_day_changes_nothing_either(
    db_factory: sessionmaker[Session],
    third_factory: sessionmaker[Session],
) -> None:
    failing_factory, off_factory = db_factory, third_factory
    failing = await run_day(failing_factory, "failing")
    off = await run_day(off_factory, "off")

    assert trading_rows(failing_factory) == trading_rows(off_factory)
    assert trading_rows(failing_factory)["trades"]
    assert failing.chat == off.chat and failing.replies == off.replies
    assert failing.calls == off.calls
    assert failing.tapped and all(failing.tapped)
    assert failing.iterations == off.iterations
    assert failing.foreign_locks and set(failing.foreign_locks) == {0}

    errors = [s for s in failing.steps if s.skipped == "error"]
    assert len(errors) >= 10  # it really failed on every pass with observations
    assert all(s.skipped in ("error", "no_new_quotes", "no_run") for s in failing.steps)
    assert marks_rows(failing_factory) == ([], [])
    events = marks_events(failing_factory)
    assert [level for level, _ in events] == ["warning"]  # exactly one per streak: the day is one streak
    assert "database down" in events[0][1]
