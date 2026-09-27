from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from trader.events import LEVELS, log_event
from trader.market.clock import FixedClock

CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))


def test_every_known_level_is_logged() -> None:
    session = MagicMock()
    for level in sorted(LEVELS):
        log_event(session, CLOCK, level, "engine", "hello")
    assert session.add.call_count == len(LEVELS)


@pytest.mark.parametrize("level", ["ERROR", "warn", "fatal", ""])
def test_an_unknown_level_is_refused(level: str) -> None:
    """P2-REVIEW: event_log.level has no DB check and alerts match exact levels, so a typo is refused."""
    session = MagicMock()
    with pytest.raises(ValueError, match="event level"):
        log_event(session, CLOCK, level, "engine", "hello")
    session.add.assert_not_called()
