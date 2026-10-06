"""OPTSIM T16, feature acceptance item 7 at run time: an options session leaves the stock side exactly as
it was (see `options_world`). `tests/options/test_stock_untouched.py` guards the stock engine's files; this
guards its rows and its two outward channels (the relay to Telegram and the web's change feed).
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import trader.runtime as stock_runtime
from tests.e2e import options_world as w
from tests.e2e.options_world import OptionsWorld
from tests.fakes_telegram import FakeMessenger, FakeRenderer, RecordingNotifier
from trader.api.feed import watermarks
from trader.db import models as m
from trader.engine.runs import get_live_run, sim_account
from trader.jobs import soak
from trader.notify.relay import NotificationRelay
from trader.options.account import active_options_run_id
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

# Every table the stock engine keeps per run.
STOCK_RUN_TABLES = (
    m.Signal, m.Proposal, m.Order, m.Fill, m.Position, m.Trade, m.Candidate, m.KillSwitchEvent, m.DecisionLog,
)  # fmt: skip


@pytest.fixture
async def world(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[OptionsWorld]:
    async with w.open_world(db_factory, monkeypatch, tmp_path) as made:
        yield made
        assert made.errors() == []


def stock_rows(world: OptionsWorld, run_id: int) -> dict[str, Any]:
    """What the stock side holds for its live run, and anything at all in its per-run tables."""
    with world.factory() as s:
        out: dict[str, Any] = {
            table.__tablename__: s.execute(select(func.count()).select_from(table)).scalar_one()
            for table in STOCK_RUN_TABLES
        }
        out["ledger"] = s.execute(
            select(func.count(), func.coalesce(func.sum(m.CashLedger.amount), 0)).where(
                m.CashLedger.run_id == run_id
            )
        ).one()
        out["snapshots"] = s.execute(
            select(func.count()).select_from(m.EquitySnapshot).where(m.EquitySnapshot.run_id == run_id)
        ).scalar_one()
        out["run"] = s.execute(select(m.Run.status, m.Run.mode, m.Run.params).where(m.Run.id == run_id)).one()
        out["strategy_configs"] = s.execute(select(func.count()).select_from(m.StrategyConfig)).scalar_one()
        return out


async def test_stock_run_is_untouched(world: OptionsWorld) -> None:
    core = world.core
    live = get_live_run(core.factory, core.clock, RuntimeSettings())  # the stock live run and its account
    assert live.id != world.run_id
    account = sim_account(core.factory, live.id)
    assert world.api.get("/api/dashboard").status_code == 200  # the API is up (it writes the stock defaults)
    relayed = RecordingNotifier()
    relay = NotificationRelay(
        core.factory, core.clock, relayed, FakeRenderer(), FakeMessenger(), live.id, settings=RuntimeSettings
    )
    await relay.pump()  # the stock worker's relay, caught up before the options session
    with world.factory() as s:
        marks = watermarks(s)  # what the web's change feed watches
    before = stock_rows(world, live.id)

    # An options session with everything in it: a refresh, an approval, a strategy event, an order, a
    # fill and its message, a strike-touched warning, a take-profit, a post-close with its summary.
    await world.refresh(w.DAY0)
    world.go(w.DAY0, 9, 0)
    world.approve("F")
    world.go(w.DAY0, 10, 30)
    await world.run_until(w.DAY0, 10, 35)
    world.set_price("F", "49.90")  # at the short put's strike: the worker's warning
    world.quote("F", w.PUT_EXPIRY, "50", "put", "1.60")
    await world.run_until(w.DAY0, 10, 38)
    world.set_price("F", "55")
    world.quote("F", w.PUT_EXPIRY, "50", "put", "0.65")
    await world.run_until(w.DAY0, 10, 41)
    await world.postclose(w.DAY0)
    assert await world.structures() == []
    assert world.count(m.OptFill) == 2 and world.count(
        m.EquitySnapshot, m.EquitySnapshot.run_id == world.run_id
    )
    warnings = world.count(m.EventLog, m.EventLog.level == "warning", m.EventLog.run_id == world.run_id)
    assert warnings >= 1  # the strike-touched alert is there, and it is the options run's own
    assert any("STRIKE_TOUCHED" in t or "at or through its strike" in t for t in world.messages())

    # The stock run is the live run, with the rows it had.
    assert stock_runtime.active_live_run_id(core.factory) == live.id
    assert get_live_run(core.factory, core.clock, RuntimeSettings()).id == live.id
    assert active_options_run_id(core.factory) == world.run_id
    assert stock_rows(world, live.id) == before
    assert sim_account(core.factory, live.id) == account
    with world.factory() as s:
        for table in STOCK_RUN_TABLES:  # nothing of the options run ever lands in a stock table
            assert s.execute(select(func.count()).select_from(table)).scalar_one() == 0, table.__tablename__
        # The change feed has nothing to tell the stock pages. The one shared topic is the job list:
        # `job_runs` is reused by design, so the System page lists the option jobs next to the stock ones.
        now_marks = watermarks(s)
        assert {k: v for k, v in now_marks.items() if k != "jobs"} == {
            k: v for k, v in marks.items() if k != "jobs"
        }
        jobs = set(s.execute(select(m.JobRun.job)).scalars())
        assert jobs == {
            "options_refresh",
            "options_postclose",
            "opt_event:wheel:opt_daily",
            "opt_event:wheel:postclose",
        }
        # ... under names the soak never judges: not one of its fixed jobs, not a stock `event:<key>` row
        assert not jobs & set(soak.DEADLINES) and soak.WEEKLY_JOB not in jobs
        assert not any(job.startswith(soak.EVENT_JOB_PREFIX) for job in jobs)
        # every options event carries the options run, so no stock reader (run NULL or the live run) sees it
        loose = s.execute(
            select(m.EventLog.source).where(m.EventLog.source.like("options.%"), m.EventLog.run_id.is_(None))
        ).all()
        assert loose == []

    # The stock relay has nothing to send: option messages go out from the options side only, once.
    report = await relay.pump()
    assert (report.proposals, report.fills, report.events) == (0, 0, 0)
    assert relayed.sent == []

    # The stock pages still answer for the stock run, next to the Options page.
    client = world.api
    assert client.get("/api/dashboard").status_code == 200
    assert world.get("/account")["run_id"] == world.run_id
