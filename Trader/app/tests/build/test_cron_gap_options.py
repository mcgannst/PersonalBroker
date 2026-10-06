"""OPTSIM: `build/cron_gap.py` pins the options cron commands to their session (the deploy catch-up tool)."""

from datetime import date
from types import ModuleType

import pytest

from tests.build import load_build_script
from trader.market.calendar import SessionCalendar

CAL = SessionCalendar()
TUESDAY = date(2026, 10, 6)
SATURDAY = date(2026, 10, 10)
THANKSGIVING = date(2026, 11, 26)


@pytest.fixture(scope="module")
def cron_gap() -> ModuleType:
    return load_build_script("cron_gap")


@pytest.mark.parametrize(
    "command",
    ["trader options-refresh", "trader options-postclose", "trader options-event --due"],
)
def test_session_day_option_commands_are_pinned_to_the_fire_day(cron_gap: ModuleType, command: str) -> None:
    assert cron_gap.pinned_command(CAL, command, TUESDAY) == (f"{command} --date 2026-10-06", "")
    pinned, note = cron_gap.pinned_command(CAL, command, THANKSGIVING)
    assert pinned is None and note == "not a session (2026-11-26): nothing to run"


def test_a_named_strategy_event_is_pinned_to_the_latest_session(cron_gap: ModuleType) -> None:
    """The Saturday `options-event wheel screen` runs for the Friday just ended."""
    command = "trader options-event wheel screen"
    assert cron_gap.pinned_command(CAL, command, SATURDAY) == (f"{command} --date 2026-10-09", "")
    assert cron_gap.pinned_command(CAL, command, TUESDAY) == (f"{command} --date 2026-10-06", "")
