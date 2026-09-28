"""P6-T11 acceptance test 4 (integration): decisions unchanged through the real wiring.

The P3-T13 manual worker day (`tests/integration/test_worker_day.py`: the real `rt.run_worker()` composition,
the cron jobs through the runtime, a fake Telegram chat) runs twice in two fresh databases: once with the
decision log on (the worker's own `DecisionsLoop` taking a pass after every worker step, and the post-close's
final pass) and once with it off (`reports.decisions_enabled` false: no pass records anything). The trading
rows are identical by natural keys (T10 test 14's comparison), the chat is the same, and only the first
database has a journal, frozen by the post-close.
"""

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import trader.bootstrap
import trader.runtime as rt
from tests.fakes_questrade import FakeQuestrade
from tests.integration import test_worker_day as wd
from tests.integration.test_decisions_day import trading_rows
from tests.integration.test_simulated_day import FakeClaude, FakeFinviz
from tests.replay.test_golden import fresh_factory  # noqa: F401  (a fixture: a second database)
from trader.bootstrap import Core
from trader.crypto import Crypto
from trader.db import models as m
from trader.decisions.loop import LoopStep
from trader.market.clock import FixedClock
from trader.settings_store import SettingsStore
from trader.worker import StepReport

pytestmark = pytest.mark.db

DAY = wd.DAY


def build_world(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> wd.World:
    """`test_worker_day`'s `world` fixture, for any database."""
    engine = factory.kw["bind"]
    assert isinstance(engine, SqlEngine)
    clock = FixedClock(wd.et(12, 0, day=date(2026, 10, 5)))
    env = wd.make_env()  # a fresh encryption key each time
    store = SettingsStore(factory, now=clock.now)
    core = Core(env, engine, factory, Crypto(env.app_encryption_key.get_secret_value()), clock, wd.CAL, store)
    w = wd.World(core, clock, wd.ChatApi(), FakeQuestrade(), FakeFinviz(), FakeClaude(), monkeypatch)
    store.set("approval_mode", "manual", actor="test")
    store.set("auto_flatten_on_expiry", True, actor="test")

    async def open_catalysts(core: Core, stack: Any) -> Any:
        return w.catalysts()

    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    monkeypatch.setattr(rt, "build_telegram_api", lambda env: w.api)
    monkeypatch.setattr(rt, "questrade_client", lambda core: wd.QtContext(w.fq))
    monkeypatch.setattr(rt, "open_catalysts", open_catalysts)
    monkeypatch.setattr(rt, "questrade_auth", lambda core: wd.FakeAuth(clock))
    return w


async def manual_day(w: wd.World, *, loop_on: bool) -> list[LoopStep]:
    """The manual day of `test_manual_day_through_telegram`; with `loop_on`, the worker's decisions loop
    takes one iteration after every worker step (as its own task would, on its own cadence)."""
    steps: list[LoopStep] = []
    if not loop_on:
        w.store.set("reports.decisions_enabled", False, actor="test")
    original = wd.Driver.at

    async def at(self: wd.Driver, when: Any) -> StepReport:
        report = await original(self, when)
        loop = self.worker.deps.decisions
        assert loop is not None  # the composition always passes it
        if loop_on:
            steps.append(await loop.run_once())
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
    return steps


def chat(w: wd.World) -> list[str]:
    """The chat's messages without the one line the decision log adds to the daily summary."""
    return [
        "\n".join(x for x in s.text.split("\n") if not x.startswith("Decisions: ") or "scanned" not in x)
        for s in w.api.sent
    ]


@pytest.fixture
def worlds(
    db_factory: sessionmaker[Session],
    fresh_factory: sessionmaker[Session],  # noqa: F811
) -> Iterator[tuple[sessionmaker[Session], sessionmaker[Session]]]:
    yield db_factory, fresh_factory


async def test_4_a_worker_day_is_identical_with_the_decisions_loop_on_and_off(
    worlds: tuple[sessionmaker[Session], sessionmaker[Session]],
) -> None:
    on_factory, off_factory = worlds
    with pytest.MonkeyPatch.context() as mp:
        w_on = build_world(on_factory, mp)
        steps = await manual_day(w_on, loop_on=True)
        chat_on = chat(w_on)
        summary_on = w_on.api.one(f"Daily summary {DAY.isoformat()}").text
    with pytest.MonkeyPatch.context() as mp:
        w_off = build_world(off_factory, mp)
        assert await manual_day(w_off, loop_on=False) == []
        chat_off = chat(w_off)
        summary_off = w_off.api.one(f"Daily summary {DAY.isoformat()}").text

    assert trading_rows(on_factory) == trading_rows(off_factory)
    assert trading_rows(on_factory)["trades"]
    assert chat_on == chat_off
    assert "Decisions: " in summary_on and "reports?day=2026-10-06" in summary_on
    assert "reports?day=" not in summary_off

    # the loop really recorded during the day, never in the 9:35 quiet minutes, and never failed
    recorded = [s for s in steps if s.skipped is None and s.result is not None and s.result.skipped is None]
    assert recorded
    assert all(s.skipped != "error" for s in steps)
    quiet = [s for s in steps if s.skipped == "scan_quiet"]
    assert quiet and all(wd.et(9, 34) <= s.at < wd.et(9, 38) for s in quiet)
    assert not any(wd.et(9, 34) <= s.at < wd.et(9, 38) for s in recorded)

    with on_factory() as s:
        journal = s.execute(select(m.DecisionLog).order_by(m.DecisionLog.seq)).scalars().all()
        warnings = s.execute(select(m.EventLog).where(m.EventLog.source == "decisions")).scalars().all()
    with off_factory() as s:
        assert s.execute(select(m.DecisionLog)).first() is None
    assert journal and all(r.final for r in journal) and journal[-1].stage == "day"
    assert warnings == []
    (exit_row,) = [r for r in journal if r.stage == "exit"]
    assert (exit_row.ticker, exit_row.rule) == ("AAA", "expiry")  # the flatten, auto-executed on expiry
    scan = {r.ticker: r.outcome for r in journal if r.stage == "scan" and r.symbol_id is not None}
    assert scan["AAA"] == "passed"
