"""P4-T6 acceptance tests 8-9: the kill switches on the web (state, typed-reason reset, pause, resume), on a
real database through the real `KillSwitches` (BR-34, BR-41, SPEC §6.3)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.fakes_api import make_services, test_core
from trader.api.routers.killswitch import router
from trader.bootstrap import Core
from trader.db import models as m
from trader.engine.killswitch import SWITCHES, KillSwitches
from trader.engine.runs import get_live_run
from trader.market.clock import ET, FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

DAY = date(2026, 10, 6)  # a Tuesday session


def et(h: int, mi: int, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, mi, tzinfo=ET).astimezone(UTC)


@dataclass
class World:
    core: Core
    clock: FixedClock
    run_id: int
    client: TestClient

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory

    @property
    def killswitches(self) -> KillSwitches:
        return KillSwitches(self.factory, self.clock)


@pytest.fixture
def world(db_factory: sessionmaker[Session]) -> World:
    clock = FixedClock(et(10, 15))
    core = test_core(db_factory, clock)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    return World(core, clock, run.id, make_client(make_services(core), router))


def trip(w: World, switch: str, value: str = "0.160000", threshold: str = "0.150000", day: date = DAY) -> int:
    with w.factory() as s:
        row = m.KillSwitchEvent(
            run_id=w.run_id,
            switch=switch,
            session_date=day,
            tripped_at=w.clock.now() - timedelta(minutes=3),
            value=Decimal(value),
            threshold=Decimal(threshold),
        )
        s.add(row)
        s.commit()
        return row.id


def by_switch(body: dict[str, object]) -> dict[str, dict[str, object]]:
    switches = body["switches"]
    assert isinstance(switches, list)
    return {k["switch"]: k for k in switches}


def audits(w: World, prefix: str = "killswitch.") -> list[m.AuditLog]:
    with w.factory() as s:
        return list(
            s.execute(
                select(m.AuditLog).where(m.AuditLog.action.startswith(prefix)).order_by(m.AuditLog.id)
            ).scalars()
        )


# --- GET ----------------------------------------------------------------------------------------------------


def test_state_lists_four_switches_and_the_history_newest_first(world: World) -> None:
    older = trip(world, "daily_loss_pct", "0.060000", "0.050000", day=date(2026, 10, 5))
    newer = trip(world, "max_drawdown_pct")
    r = world.client.get("/api/killswitch")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [k["switch"] for k in body["switches"]] == list(SWITCHES)
    states = by_switch(body)
    assert (
        states["max_drawdown_pct"]["tripped"] is True
        and states["max_drawdown_pct"]["needs_web_reset"] is True
    )
    assert states["max_drawdown_pct"]["value"] == "0.160000"
    assert states["daily_loss_pct"]["tripped"] is False  # yesterday's daily loss no longer blocks
    assert [h["id"] for h in body["history"]] == [newer, older]
    assert body["history"][0]["switch"] == "max_drawdown_pct" and body["history"][0]["reset_at"] is None


def test_history_is_capped_at_fifty(world: World) -> None:
    with world.factory() as s:
        for i in range(55):
            s.add(
                m.KillSwitchEvent(
                    run_id=world.run_id,
                    switch="manual_pause",
                    session_date=DAY,
                    tripped_at=world.clock.now() - timedelta(minutes=60 - i),
                    reset_at=world.clock.now() - timedelta(minutes=59 - i),
                    reset_reason="resume",
                    reset_by="telegram",
                )
            )
        s.commit()
    history = world.client.get("/api/killswitch").json()["history"]
    assert len(history) == 50
    ids = [h["id"] for h in history]
    assert ids == sorted(ids, reverse=True) and ids[0] == 55


def test_without_a_session_every_route_is_401(world: World) -> None:
    client = make_client(make_services(world.core), router, user=None)
    assert client.get("/api/killswitch").status_code == 401
    assert client.post("/api/killswitch/pause").status_code == 401
    assert client.post("/api/killswitch/resume").status_code == 401
    assert (
        client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": "reviewed"}).status_code == 401
    )
    assert world.killswitches.blocking(world.run_id, DAY) is None


# --- 8. reset -----------------------------------------------------------------------------------------------


def test_reset_with_a_typed_reason_clears_and_audits(world: World) -> None:
    trip(world, "max_drawdown_pct")
    r = world.client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": "  reviewed  "})
    assert r.status_code == 200, r.text
    body = r.json()
    dd = by_switch(body)["max_drawdown_pct"]
    assert dd["tripped"] is False and dd["needs_web_reset"] is False
    assert (
        body["history"][0]["reset_reason"] == "reviewed" and body["history"][0]["reset_by"] == "web:stephen"
    )
    assert world.killswitches.blocking(world.run_id, DAY) is None
    (audit,) = audits(world)
    assert (audit.action, audit.actor, audit.after) == (
        "killswitch.reset:max_drawdown_pct",
        "web:stephen",
        {"reason": "reviewed"},
    )


def test_reset_of_expectancy_and_daily_loss(world: World) -> None:
    trip(world, "expectancy", "-0.3000", "-0.2000")
    trip(world, "daily_loss_pct", "0.060000", "0.050000")
    for switch in ("expectancy", "daily_loss_pct"):
        r = world.client.post(f"/api/killswitch/{switch}/reset", json={"reason": "checked the trades"})
        assert r.status_code == 200, r.text
        assert by_switch(r.json())[switch]["tripped"] is False
    assert [a.action for a in audits(world)] == [
        "killswitch.reset:expectancy",
        "killswitch.reset:daily_loss_pct",
    ]


@pytest.mark.parametrize("reason", ["ab", "   ab   ", "", "x" * 501])
def test_reset_reason_must_be_three_to_five_hundred_characters(world: World, reason: str) -> None:
    trip(world, "max_drawdown_pct")
    r = world.client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": reason})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation"
    assert world.killswitches.blocking(world.run_id, DAY) == "max_drawdown_pct"
    assert audits(world) == []


def test_reset_of_manual_pause_is_400_use_resume(world: World) -> None:
    assert world.killswitches.pause(world.run_id, DAY, "telegram")
    r = world.client.post("/api/killswitch/manual_pause/reset", json={"reason": "reviewed"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"
    assert "Resume" in r.json()["error"]["message"]
    assert world.killswitches.blocking(world.run_id, DAY) == "manual_pause"


def test_reset_when_not_tripped_is_409(world: World) -> None:
    r = world.client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": "reviewed"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert audits(world) == []


def test_reset_racing_another_reset_is_409(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another request resets the switch between the route's check and its call: KillSwitches.reset raises
    ValueError("... is not tripped"), which is a 409 too, never a 500."""
    trip(world, "max_drawdown_pct")
    real_reset = KillSwitches.reset

    def reset_twice(
        self: KillSwitches, run_id: int, switch: str, reason: str, actor: str, **kw: object
    ) -> None:
        real_reset(self, run_id, switch, "someone else", "telegram")
        real_reset(self, run_id, switch, reason, actor)

    monkeypatch.setattr(KillSwitches, "reset", reset_twice)
    r = world.client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": "reviewed"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


def test_reset_of_an_unknown_switch_is_404(world: World) -> None:
    r = world.client.post("/api/killswitch/bogus/reset", json={"reason": "reviewed"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# --- 9. pause and resume ------------------------------------------------------------------------------------


def test_pause_then_resume(world: World) -> None:
    r = world.client.post("/api/killswitch/pause")
    assert r.status_code == 200, r.text
    assert by_switch(r.json())["manual_pause"]["tripped"] is True
    assert world.killswitches.blocking(world.run_id, DAY) == "manual_pause"

    again = world.client.post("/api/killswitch/pause")
    assert again.status_code == 409 and again.json()["error"]["message"] == "Already paused."

    r = world.client.post("/api/killswitch/resume")
    assert r.status_code == 200, r.text
    assert by_switch(r.json())["manual_pause"]["tripped"] is False
    assert world.killswitches.blocking(world.run_id, DAY) is None

    not_paused = world.client.post("/api/killswitch/resume")
    assert not_paused.status_code == 409 and not_paused.json()["error"]["message"] == "Not paused."
    assert [(a.action, a.actor) for a in audits(world)] == [
        ("killswitch.pause", "web:stephen"),
        ("killswitch.resume", "web:stephen"),
    ]


def test_pause_is_for_the_current_session(world: World) -> None:
    world.clock.set(et(12, 0, date(2026, 10, 10)))  # a Saturday: the next session is Monday 2026-10-12
    assert world.client.post("/api/killswitch/pause").status_code == 200
    with world.factory() as s:
        row = s.execute(
            select(m.KillSwitchEvent).where(m.KillSwitchEvent.switch == "manual_pause")
        ).scalar_one()
    assert row.session_date == date(2026, 10, 12) and row.run_id == world.run_id


def test_resume_never_resets_an_automatic_switch(world: World) -> None:
    trip(world, "max_drawdown_pct")
    assert world.client.post("/api/killswitch/pause").status_code == 200
    r = world.client.post("/api/killswitch/resume")
    assert r.status_code == 200, r.text
    states = by_switch(r.json())
    assert states["manual_pause"]["tripped"] is False
    assert states["max_drawdown_pct"]["tripped"] is True
    assert world.killswitches.blocking(world.run_id, DAY) == "max_drawdown_pct"
