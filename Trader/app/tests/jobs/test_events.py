"""P3-T10: `trader event KEY` / `trader event --due`, the cron backup for the worker's session events."""

from datetime import UTC, date, datetime

import pytest

from trader.engine.scheduler import DayPlan, FireResult, FireStatus, PlannedEvent
from trader.jobs.events import run_event_backup
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock

CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tuesday, EDT
THANKSGIVING = date(2026, 11, 26)


def _plan(d: date = DAY) -> DayPlan:
    events = (
        PlannedEvent("orb_open", datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC), ("orb_sip",), False),
        PlannedEvent("entry_cancel", datetime(2026, 10, 6, 15, 30, tzinfo=UTC), ("orb_sip",), True),
        PlannedEvent("overlay_decision", datetime(2026, 10, 6, 19, 30, tzinfo=UTC), ("spy_overlay",), True),
        PlannedEvent("flatten", datetime(2026, 10, 6, 19, 50, tzinfo=UTC), ("orb_sip",), True),
    )
    return DayPlan(
        d,
        True,
        datetime(2026, 10, 6, 13, 30, tzinfo=UTC),
        datetime(2026, 10, 6, 20, 0, tzinfo=UTC),
        events,
    )


class FakeFire:
    def __init__(self, status: FireStatus = "fired") -> None:
        self.status = status
        self.calls: list[tuple[str, date]] = []

    async def __call__(self, key: str, session_date: date) -> FireResult:
        self.calls.append((key, session_date))
        return FireResult(key, session_date, self.status, {"by": "fake"})


async def test_holiday_does_nothing() -> None:
    fire = FakeFire()
    clock = FixedClock(datetime(2026, 11, 26, 14, 36, tzinfo=UTC))
    by_key = await run_event_backup(fire, CAL, clock, "orb_open", THANKSGIVING)
    by_due = await run_event_backup(
        fire, CAL, clock, None, THANKSGIVING, due=True, plan=lambda d: _plan(d), fired=lambda d: set()
    )
    assert fire.calls == []
    assert [r.status for r in by_key] == ["not_session"]
    assert by_key[0].key == "orb_open"
    assert [r.status for r in by_due] == ["not_session"]
    assert by_key[0].detail == {"skipped": "not a session"}


async def test_key_passes_fire_result_through() -> None:
    fire = FakeFire("fired")
    clock = FixedClock(datetime(2026, 10, 6, 13, 36, tzinfo=UTC))
    results = await run_event_backup(fire, CAL, clock, "orb_open", DAY)
    assert fire.calls == [("orb_open", DAY)]
    assert results == [FireResult("orb_open", DAY, "fired", {"by": "fake"})]


async def test_key_skipped_when_worker_fired_first_is_returned_unchanged() -> None:
    fire = FakeFire("skipped")
    clock = FixedClock(datetime(2026, 10, 6, 13, 36, tzinfo=UTC))
    results = await run_event_backup(fire, CAL, clock, "orb_open", DAY)
    assert results == [FireResult("orb_open", DAY, "skipped", {"by": "fake"})]
    assert len(fire.calls) == 1


async def test_due_fires_every_due_event_and_none_not_yet_due() -> None:
    fire = FakeFire()
    clock = FixedClock(datetime(2026, 10, 6, 19, 35, tzinfo=UTC))  # 15:35 ET: overlay due, flatten not
    results = await run_event_backup(
        fire, CAL, clock, None, DAY, due=True, plan=lambda d: _plan(d), fired=lambda d: {"orb_open"}
    )
    assert fire.calls == [("entry_cancel", DAY), ("overlay_decision", DAY)]
    assert [r.key for r in results] == ["entry_cancel", "overlay_decision"]


async def test_due_with_nothing_due_fires_nothing() -> None:
    fire = FakeFire()
    clock = FixedClock(datetime(2026, 10, 6, 13, 0, tzinfo=UTC))  # 09:00 ET
    results = await run_event_backup(
        fire, CAL, clock, None, DAY, due=True, plan=lambda d: _plan(d), fired=lambda d: set()
    )
    assert fire.calls == [] and results == []


async def test_due_needs_plan_and_fired_and_a_key_or_due() -> None:
    fire = FakeFire()
    clock = FixedClock(datetime(2026, 10, 6, 13, 36, tzinfo=UTC))
    with pytest.raises(ValueError, match="plan"):
        await run_event_backup(fire, CAL, clock, None, DAY, due=True)
    with pytest.raises(ValueError, match="key"):
        await run_event_backup(fire, CAL, clock, None, DAY)
    with pytest.raises(ValueError, match="both"):
        await run_event_backup(
            fire, CAL, clock, "orb_open", DAY, due=True, plan=lambda d: _plan(d), fired=lambda d: set()
        )
    assert fire.calls == []


class RaisingFire(FakeFire):
    """Raises for the keys in `raise_for` (a DB blip inside fire_event), fires the rest."""

    def __init__(self, raise_for: set[str]) -> None:
        super().__init__("fired")
        self.raise_for = raise_for

    async def __call__(self, key: str, session_date: date) -> FireResult:
        if key in self.raise_for:
            self.calls.append((key, session_date))
            raise ConnectionError("text that must not be reported")
        return await super().__call__(key, session_date)


AT_1551 = datetime(2026, 10, 6, 19, 51, tzinfo=UTC)  # 15:51 ET: entry_cancel, overlay, flatten all due


async def test_due_a_raising_fire_is_failed_and_the_later_events_still_fire() -> None:
    """Fix round 1: entry_cancel and overlay_decision raising must not cost the flatten. Each is reported
    as failed with only the exception type."""
    fire = RaisingFire({"entry_cancel", "overlay_decision"})
    results = await run_event_backup(
        fire, CAL, FixedClock(AT_1551), None, DAY, due=True, plan=_plan, fired=lambda d: {"orb_open"}
    )
    assert [k for k, _ in fire.calls] == ["entry_cancel", "overlay_decision", "flatten"]
    assert results == [
        FireResult("entry_cancel", DAY, "failed", {"error": "ConnectionError"}),
        FireResult("overlay_decision", DAY, "failed", {"error": "ConnectionError"}),
        FireResult("flatten", DAY, "fired", {"by": "fake"}),
    ]


async def test_due_force_a_raising_first_event_still_reaches_the_last() -> None:
    """With force every due event is passed to fire, settled or not. A raise on the first still lets the
    rest fire."""
    fire = RaisingFire({"orb_open"})
    results = await run_event_backup(
        fire,
        CAL,
        FixedClock(AT_1551),
        None,
        DAY,
        due=True,
        plan=_plan,
        fired=lambda d: {"orb_open"},
        force=True,
    )
    assert [r.key for r in results] == ["orb_open", "entry_cancel", "overlay_decision", "flatten"]
    assert [r.status for r in results] == ["failed", "fired", "fired", "fired"]


async def test_key_a_raising_fire_is_a_failed_result() -> None:
    """A single key that raises is a failed result (the CLI exits 1), not an escaping exception."""
    fire = RaisingFire({"flatten"})
    results = await run_event_backup(fire, CAL, FixedClock(AT_1551), "flatten", DAY)
    assert results == [FireResult("flatten", DAY, "failed", {"error": "ConnectionError"})]
