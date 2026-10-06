"""OPTSIM T16, feature acceptance item 8 end to end: stopping and restarting the options worker mid-session
loses no order, fill or prompt, and repeats none (see `options_world`). Each "restart" throws the worker and
every service it built away and builds new ones with `runtime.build_worker_deps`, as a new process does.

Also here: an owner prompt answered by a tap on its Telegram button, through the stock worker's real bot,
and the one known gap (a fill committed whose worker died before telling anyone), pinned as a strict xfail.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import trader.runtime as stock_runtime
from tests.e2e import options_world as w
from tests.e2e.options_world import OptionsWorld
from tests.fakes_telegram import FakeRenderer
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import DbCallbackIssuer
from trader.db import models as m
from trader.engine.proposals import Decision, DecisionResult, Via
from trader.engine.runs import get_live_run
from trader.notify.types import OutboundMessage
from trader.option_strategies.host import job_name
from trader.options.worker import HEARTBEAT_PROCESS
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db


@pytest.fixture
async def world(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[OptionsWorld]:
    async with w.open_world(db_factory, monkeypatch, tmp_path) as made:
        yield made
        assert made.errors() == []


class NoCommands:
    async def handle(self, text: str) -> list[OutboundMessage]:
        return []

    async def confirm_pause(self, action: str) -> str:
        return ""


def no_decide(proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
    raise AssertionError("a prompt tap must not decide a proposal")


def stock_bot(world: OptionsWorld) -> TelegramBot:
    """The stock worker's bot as `trader.runtime` builds it: the same signer (from the session secret) and
    issuer the options side signs its prompt buttons with, bound to the STOCK live run."""

    async def no_sleep(seconds: float) -> None:
        return None

    core = world.core
    signer = stock_runtime.build_signer(core)
    live = get_live_run(core.factory, core.clock, RuntimeSettings())
    issuer = DbCallbackIssuer(core.factory, core.clock, signer)
    return TelegramBot(
        world.telegram, w.CHAT_ID, core.factory, core.clock, issuer, signer, no_decide, NoCommands(),
        FakeRenderer(), live.id, settings=RuntimeSettings, sleep=no_sleep,
    )  # fmt: skip


def notifications(world: OptionsWorld, prefix: str) -> list[str]:
    with world.factory() as s:
        keys = s.execute(select(m.Notification.dedupe_key).order_by(m.Notification.id)).scalars()
        return [k for k in keys if k is not None and k.startswith(prefix)]


async def submit_and_stop(world: OptionsWorld) -> None:
    """DAY0 10:30: the daily event submits the walking put order (limit 1.53, bid 1.50); the worker dies."""
    await world.refresh(w.DAY0)
    world.go(w.DAY0, 9, 0)
    world.approve("F")
    world.go(w.DAY0, 10, 30)
    report = await world.step()
    assert report.events == (("wheel", "opt_daily", "succeeded"),) and report.fills == 0
    (working,) = await world.orders("working")
    assert working.net_limit is not None and working.net_limit > 0
    await world.stop_worker()


async def test_restart_mid_session_loses_nothing(world: OptionsWorld) -> None:
    # --- between a submit and its fill ---
    await submit_and_stop(world)
    assert world.count(m.OptFill) == 0

    world.go(w.DAY0, 10, 31)  # a new process: nothing in memory, every timer due
    reports = await world.run_until(w.DAY0, 10, 36)
    assert sum(r.fills for r in reports) == 1
    assert all(r.events == () for r in reports)  # the day's event is not fired a second time
    assert world.count(m.OptOrder) == 1 and world.count(m.OptFill) == 1
    assert notifications(world, "opt:fill:") == ["opt:fill:1"]
    assert world.wheel_position().state == "PUT_OPEN"  # the new worker told the strategy about the fill
    daily = job_name("wheel", "opt_daily")
    assert world.count(m.JobRun, m.JobRun.job == daily) == 1

    await world.stop_worker()  # and again, with nothing in flight
    world.go(w.DAY0, 10, 40)
    reports = await world.run_until(w.DAY0, 10, 43)
    assert sum(r.fills for r in reports) == 0 and all(r.events == () for r in reports)
    assert world.count(m.OptOrder) == 1 and world.count(m.OptFill) == 1
    assert notifications(world, "opt:fill:") == ["opt:fill:1"]
    assert len(await world.structures()) == 1
    await world.assert_reconciled()

    # --- between a prompt and its answer ---
    await world.stop_worker()
    await world.refresh(w.PUT_EXPIRY)
    world.set_price("F", "48")
    world.quote("F", w.PUT_EXPIRY, "50", "put", "2.00")
    await world.postclose(w.PUT_EXPIRY)  # assigned: the job opens the fresh-cash prompt and sends it
    pos = world.wheel_position()
    (asked,) = notifications(world, "opt:prompt:")

    world.go(w.PUT_EXPIRY, 17, 0)
    await world.step()  # a new worker, after the close: the pending prompt is neither lost nor sent again
    await world.step()
    assert notifications(world, "opt:prompt:") == [asked]
    (pending,) = world.get("/prompts")["items"]
    await world.stop_worker()

    # The owner taps "Yes" in Telegram while no options worker is running. The stock worker's bot takes it.
    sent = next(c for c in reversed(world.telegram.calls_of("send_message")) if c["buttons"])
    buttons = {b.text: b.callback_data for row in sent["buttons"] for b in row}
    message_id = len(world.telegram.calls_of("send_message"))
    yes = next(data for text, data in buttons.items() if text.lower().startswith("yes"))
    bot = stock_bot(world)
    await bot.handle_update(world.telegram.callback_update(yes, chat_id=w.CHAT_ID, message_id=message_id))
    (stored,) = world.get("/prompts", status="all")["items"]
    assert (stored["id"], stored["status"], stored["answer"]) == (pending["id"], "answered", "y")
    assert stored["answered_via"] == "telegram"

    world.go(w.PUT_EXPIRY, 17, 5)
    await world.step()  # a new worker hands the answer to the wheel, once
    with world.factory() as s:
        row = s.get_one(m.OwnerPrompt, pending["id"])
        delivered_at = row.delivered_at
    assert delivered_at is not None
    assert world.wheel_position(pos.id).fresh_cash_answer is True
    await world.stop_worker()
    world.go(w.PUT_EXPIRY, 17, 10)
    await world.step()
    await world.step()
    with world.factory() as s:
        assert s.get_one(m.OwnerPrompt, pending["id"]).delivered_at == delivered_at
    assert len(world.wheel().events(kind="answer")) == 1
    assert world.get("/prompts")["items"] == []
    assert notifications(world, "opt:prompt:") == [asked]
    await world.assert_reconciled()

    assert world.workers_built == 6
    with world.factory() as s:
        beat = s.execute(
            select(m.WorkerHeartbeat).where(m.WorkerHeartbeat.process == HEARTBEAT_PROCESS)
        ).scalar_one()
        assert beat.detail["run_id"] == world.run_id
    account = world.get("/account")
    assert account["worker_beat_at"] is not None  # the Account tab reads the same heartbeat row


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP (T12 and T7 hand-off notes): a fill is committed by broker.poll, then the worker sends "
    "its message and calls the plug-in's on_fill. There is no delivered marker for a fill, so a worker that "
    "dies in between leaves the fill in the books but nobody told: no message, and the wheel never opens "
    "its position for a put it has sold. Fix: a delivered marker for fills, re-read at every step.",
)
async def test_a_fill_whose_worker_died_before_delivery_still_reaches_the_strategy(
    world: OptionsWorld,
) -> None:
    await submit_and_stop(world)
    world.quote("F", w.PUT_EXPIRY, "50", "put", "1.55")  # the bid passes the 1.53 limit
    world.go(w.DAY0, 10, 31)
    doomed = await world.worker()
    fills = await doomed.deps.broker.poll(world.clock.now())  # the fill commits ...
    assert len(fills) == 1 and world.count(m.OptFill) == 1
    await world.stop_worker()  # ... and the process dies before the message and on_fill

    world.go(w.DAY0, 10, 32)
    await world.run_until(w.DAY0, 10, 40)
    assert notifications(world, "opt:fill:") == ["opt:fill:1"]
    assert world.count(m.WheelPosition) == 1
