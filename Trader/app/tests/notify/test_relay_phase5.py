"""P5-T10: the relay sends a kill-switch reset confirmation and never relays mirrored log rows (`log.*`)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run
from tests.fakes_telegram import FakeMessenger, RecordingNotifier
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches
from trader.market.clock import FixedClock
from trader.notify.messages import MessageRenderer
from trader.notify.relay import NotificationRelay
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)  # 16:30 ET, 14:30 MT
SESSION = date(2026, 10, 6)


class World:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.clock = FixedClock(NOW)
        self.notifier = RecordingNotifier()
        with session_scope(factory) as s:
            self.run_id = add_run(s)
            self.replay_id = add_run(s, mode="replay", status="running")

    def relay(self) -> NotificationRelay:
        return NotificationRelay(
            self.factory,
            self.clock,
            self.notifier,
            MessageRenderer("http://trader.home:8080", ZoneInfo("America/Edmonton"), clock=self.clock),
            FakeMessenger(),
            self.run_id,
            settings=RuntimeSettings,
        )

    async def started(self) -> NotificationRelay:
        relay = self.relay()
        await relay.pump()
        self.notifier.sent.clear()
        return relay

    def trip(self, run_id: int, switch: str = "max_drawdown_pct") -> None:
        """An open trip row (no event: only the reset's event matters here)."""
        with session_scope(self.factory) as s:
            s.add(
                m.KillSwitchEvent(
                    run_id=run_id,
                    switch=switch,
                    session_date=SESSION,
                    tripped_at=NOW,
                    value=Decimal("0.1"),
                    threshold=Decimal("0.08"),
                )
            )

    def event(self, level: str, source: str, message: str, run_id: int | None) -> int:
        with session_scope(self.factory) as s:
            e = m.EventLog(ts=NOW, level=level, source=source, run_id=run_id, message=message, data={})
            s.add(e)
            s.flush()
            return e.id

    def cursor(self, stream: str) -> int | None:
        with session_scope(self.factory) as s:
            c = s.get(m.NotifyCursor, stream)
            return None if c is None else c.last_id

    def reset_event_id(self, run_id: int) -> int:
        with session_scope(self.factory) as s:
            row = (
                s.query(m.EventLog)
                .filter(m.EventLog.run_id == run_id, m.EventLog.message.like("kill switch % reset"))
                .one()
            )
            return row.id


@pytest.fixture
def world(db_factory: sessionmaker[Session]) -> World:
    return World(db_factory)


# --- 5. the reset confirmation ---
async def test_a_live_reset_sends_one_confirmation(world: World) -> None:
    relay = await world.started()
    world.trip(world.run_id)
    KillSwitches(world.factory, world.clock).reset(
        world.run_id, "max_drawdown_pct", "reviewed", "web:stephen"
    )

    await relay.pump()

    [msg] = world.notifier.sent
    assert msg.kind == "kill_switch"
    assert "KILL SWITCH RESET: max_drawdown_pct" in msg.text
    assert "Reason: reviewed" in msg.text
    assert msg.dedupe_key == f"event:{world.reset_event_id(world.run_id)}"

    world.notifier.sent.clear()
    await relay.pump()
    assert world.notifier.sent == []


async def test_a_reset_in_a_replay_run_is_not_sent(world: World) -> None:
    relay = await world.started()
    world.trip(world.replay_id)
    KillSwitches(world.factory, world.clock).reset(world.replay_id, "max_drawdown_pct", "reviewed", "replay")

    await relay.pump()

    assert world.notifier.sent == []
    assert world.cursor("events") == world.reset_event_id(world.replay_id)


async def test_other_killswitch_warnings_are_still_not_relayed(world: World) -> None:
    relay = await world.started()
    world.event("warning", "killswitch", "manual pause: entries blocked", world.run_id)
    world.event("info", "killswitch", "manual pause lifted", world.run_id)
    world.event("warning", "engine", "kill switch max_drawdown_pct reset", world.run_id)  # wrong source

    await relay.pump()

    assert world.notifier.sent == []


# --- 6. never relayed ---
async def test_mirrored_log_rows_are_never_relayed_and_the_cursor_moves_past_them(world: World) -> None:
    relay = await world.started()
    mirrored = world.event("error", "log.worker", "relay.step_failed", None)
    critical = world.event("critical", "log.api", "unhandled", world.run_id)
    job = world.event("error", "job.nightly", "job nightly failed", None)

    await relay.pump()

    kinds: list[Any] = [msg.kind for msg in world.notifier.sent]
    assert kinds == ["job_failure"]
    assert world.notifier.sent[0].dedupe_key == f"event:{job}"
    assert world.cursor("events") == job
    assert mirrored < critical < job

    # the trailing re-scan does not pick them up either
    world.notifier.sent.clear()
    await relay.pump()
    assert world.notifier.sent == []


async def test_a_mirrored_row_alone_still_advances_the_cursor(world: World) -> None:
    relay = await world.started()
    mirrored = world.event("error", "log.worker", "boom", None)

    await relay.pump()

    assert world.notifier.sent == []
    assert world.cursor("events") == mirrored


async def test_a_source_merely_containing_log_is_still_relayed(world: World) -> None:
    relay = await world.started()
    world.event("error", "catalog", "catalog broke", world.run_id)
    world.event("error", "logx", "x broke", world.run_id)

    await relay.pump()

    assert len(world.notifier.sent) == 2
