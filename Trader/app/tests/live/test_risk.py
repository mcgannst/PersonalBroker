"""DB-T4: `trading_state` from the active kill switches (pure, with a fake `KillSwitches`).

`paused` when a manual pause is active, `blocked` when an automatic switch is, else `running`; it only reads
`KillSwitches.active` (never `evaluate`, `pause`, `resume` or `reset`).
"""

from datetime import UTC, date, datetime
from typing import Any, cast

import pytest

from trader.api.livedata.risk import trading_state
from trader.engine.killswitch import ActiveSwitch, KillSwitches

DAY = date(2026, 10, 7)
AT = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)


class FakeSwitches:
    def __init__(self, *switches: str) -> None:
        self.switches = switches
        self.calls: list[tuple[str, Any]] = []

    def active(self, run_id: int, session_date: date) -> list[ActiveSwitch]:
        self.calls.append(("active", (run_id, session_date)))
        return [ActiveSwitch(sw, i + 1, AT, None) for i, sw in enumerate(self.switches)]

    def blocking(self, run_id: int, session_date: date) -> str | None:
        self.calls.append(("blocking", (run_id, session_date)))
        return self.switches[0] if self.switches else None

    def __getattr__(self, name: str) -> Any:  # evaluate, pause, resume, reset, inputs...
        raise AssertionError(f"trading_state must not call KillSwitches.{name}")


@pytest.mark.parametrize(
    ("switches", "state"),
    [
        ((), "running"),
        (("manual_pause",), "paused"),
        (("daily_loss_pct",), "blocked"),
        (("expectancy", "max_drawdown_pct"), "blocked"),
        (("max_drawdown_pct", "manual_pause"), "paused"),  # a manual pause wins over an automatic switch
    ],
)
def test_trading_state(switches: tuple[str, ...], state: str) -> None:
    fake = FakeSwitches(*switches)
    assert trading_state(cast(KillSwitches, fake), 7, DAY) == state
    assert all(args == (7, DAY) for _, args in fake.calls)
