"""P3-T12: docker/crontab (SPEC §9 plus the 12:55 early-close flatten backup and the 12:32 / 15:32
overlay-decision backups, fix round 1), read by supercronic in ET. P5-T17 adds the Saturday weekly line."""

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
import typer.main

from trader.cli import app
from trader.engine.scheduler import day_plan
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET
from trader.settings_store import RuntimeSettings
from trader.strategies.orb_sip import OrbSip, OrbSipParams
from trader.strategies.spy_overlay import SpyOverlay, SpyOverlayParams

CRONTAB = Path(__file__).resolve().parents[2] / "docker" / "crontab"

EXPECTED = {
    ("0 2 * * *", "trader token-refresh"),
    ("0 20 * * 0-4", "trader nightly"),
    ("0 8 * * 1-5", "trader premarket"),
    ("20 9 * * 1-5", "trader preopen"),
    ("36 9 * * 1-5", "trader event orb_open"),
    ("47 9 * * 1-5", "trader openbar-check"),  # QUOTEBAR: the shadow check once the delayed candle is out
    ("30 11 * * 1-5", "trader checkin --at 11:30"),
    ("32 12 * * 1-5", "trader event --due"),
    ("55 12 * * 1-5", "trader event flatten"),
    ("58 12 * * 1-5", "trader event flatten"),
    ("30 13 * * 1-5", "trader checkin --at 13:30"),
    ("32 15 * * 1-5", "trader event --due"),
    ("55 15 * * 1-5", "trader event flatten"),
    ("58 15 * * 1-5", "trader event flatten"),
    ("15 16 * * 1-5", "trader postclose"),
    ("0 9 * * 6", "trader weekly"),  # P5-T17: Saturday 09:00 ET, the week just ended (SPEC §9)
    ("5 18 * * 1-5", "trader soak-report --notify"),  # P6-T2: 18:05 ET = 16:05 MT, after the post-close
    ("30 10 * * 6", "trader soak-report --notify --final"),  # P6-T2: Sat 10:30 ET, after the weekly report
}
# SPEC §9 times (ET) of the weekday jobs, plus the documented 12:55 flatten and 12:32 / 15:32 overlay backups.
WEEKDAY_ET = {
    "trader premarket": [time(8, 0)],
    "trader preopen": [time(9, 20)],
    "trader event orb_open": [time(9, 36)],
    "trader openbar-check": [time(9, 47)],
    "trader checkin --at 11:30": [time(11, 30)],
    "trader event --due": [time(12, 32), time(15, 32)],
    "trader event flatten": [time(12, 55), time(12, 58), time(15, 55), time(15, 58)],
    "trader checkin --at 13:30": [time(13, 30)],
    "trader postclose": [time(16, 15)],
    "trader soak-report --notify": [time(18, 5)],
}
RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _lines() -> list[str]:
    return [
        line.strip()
        for line in CRONTAB.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _jobs() -> list[tuple[str, str]]:
    out = []
    for line in _lines()[1:]:
        parts = line.split()
        out.append((" ".join(parts[:5]), " ".join(parts[5:])))
    return out


def _valid_field(field: str, low: int, high: int) -> bool:
    if field == "*":
        return True
    for item in field.split(","):
        bounds = item.split("-")
        if len(bounds) > 2 or not all(b.isdigit() for b in bounds):
            return False
        values = [int(b) for b in bounds]
        if any(v < low or v > high for v in values) or values != sorted(values):
            return False
    return True


def test_first_line_sets_the_new_york_time_zone() -> None:
    assert _lines()[0] == "CRON_TZ=America/New_York"


def test_every_line_has_five_valid_fields_and_a_command() -> None:
    for line in _lines()[1:]:
        parts = line.split()
        assert len(parts) >= 7, line  # five fields, "trader", a command
        for field, (low, high) in zip(parts[:5], RANGES, strict=True):
            assert _valid_field(field, low, high), (line, field)
        assert parts[5] == "trader", line


def test_the_schedule_is_spec_section_9_plus_the_documented_backups() -> None:
    jobs = _jobs()
    assert len(jobs) == len(set(jobs)), "a duplicated line"
    assert set(jobs) == EXPECTED


def test_every_command_is_a_registered_cli_command() -> None:
    group = typer.main.get_command(app)
    registered = set(getattr(group, "commands", {}))
    for _, command in _jobs():
        assert command.split()[1] in registered, command


def test_event_keys_are_planned_by_the_default_strategies() -> None:
    cal = SessionCalendar()
    strategies = [OrbSip(OrbSipParams()), SpyOverlay(SpyOverlayParams())]
    planned = {e.key for e in day_plan(strategies, cal, date(2026, 10, 6), RuntimeSettings()).events}
    for _, command in _jobs():
        words = command.split()
        if words[1] == "event" and not words[2].startswith("--"):
            assert words[2] in planned, command


@pytest.mark.parametrize(
    ("day", "utc_offset_hours"),
    [(date(2026, 10, 6), -4), (date(2026, 12, 1), -5)],  # EDT, EST
)
def test_times_are_eastern_on_both_sides_of_the_dst_change(day: date, utc_offset_hours: int) -> None:
    """The file holds ET wall-clock times (CRON_TZ does the conversion): 09:36 is 09:36 ET in October and
    in December, i.e. 13:36Z and then 14:36Z. A file with UTC times baked in would be an hour off in one."""
    found: dict[str, list[time]] = {}
    for schedule, command in _jobs():
        minute, hour, _, _, dow = schedule.split()
        if dow != "1-5":
            continue
        at = datetime.combine(day, time(int(hour), int(minute)), tzinfo=ET)
        assert at.utcoffset() is not None and at.utcoffset().total_seconds() == utc_offset_hours * 3600
        found.setdefault(command, []).append(at.timetz().replace(tzinfo=None))
    assert {k: sorted(v) for k, v in found.items()} == WEEKDAY_ET
    orb = datetime.combine(day, time(9, 36), tzinfo=ET).astimezone(UTC)
    assert orb.hour == 9 - utc_offset_hours and orb.minute == 36


def _et_times(command: str, day: date) -> list[datetime]:
    """When `command`'s weekday lines fire on `day` (ET wall clock, as supercronic reads them)."""
    out = []
    for schedule, cmd in _jobs():
        minute, hour, _, _, dow = schedule.split()
        if cmd == command and dow == "1-5" and day.weekday() < 5:
            out.append(datetime.combine(day, time(int(hour), int(minute)), tzinfo=ET))
    return sorted(out)


@pytest.mark.parametrize(
    "day",
    [date(2026, 10, 6), date(2026, 11, 27), date(2026, 12, 24), date(2026, 12, 1)],  # normal, early x2, EST
)
def test_an_event_due_line_backs_up_the_overlay_decision_before_the_close(day: date) -> None:
    """Fix round 1: overlay_decision (close - 30 min) has a cron backup on normal and early-close days."""
    cal = SessionCalendar()
    strategies = [OrbSip(OrbSipParams()), SpyOverlay(SpyOverlayParams())]
    plan = day_plan(strategies, cal, day, RuntimeSettings())
    [overlay] = [e.at for e in plan.events if e.key == "overlay_decision"]
    close = cal.session_close(day)
    backups = _et_times("trader event --due", day)
    assert any(overlay <= t < close for t in backups), (day, overlay, close, backups)
