"""SIZECAP: the `max_position_pct` runtime setting (a Decimal in (0, 1], default 0.10), its place in the
Settings form's Risk group, and replay snapshots: a run created before the setting existed keeps its sizing
(it loads as 1, no cap beyond cash) while a new run gets the stored value."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from trader.api.forms import group_of, model_fields_out
from trader.replay.types import replay_run_from_row
from trader.settings_store import RuntimeSettings


def test_default_is_ten_percent_as_a_decimal() -> None:
    s = RuntimeSettings()
    assert s.max_position_pct == Decimal("0.10") and isinstance(s.max_position_pct, Decimal)
    assert s.model_dump(mode="json", by_alias=True)["max_position_pct"] == "0.10"


@pytest.mark.parametrize("value", ["0.0001", "0.10", "0.5", "1", "1.0"])
def test_accepts_a_fraction_above_zero_up_to_one(value: str) -> None:
    s = RuntimeSettings.model_validate({"max_position_pct": value})
    assert s.max_position_pct == Decimal(value)


@pytest.mark.parametrize("value", ["0", "-0.1", "1.01", "2", "NaN", "Infinity", "-Infinity", "ten"])
def test_rejects_zero_negative_above_one_and_non_finite(value: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({"max_position_pct": value})


def test_is_in_the_settings_forms_risk_group_after_risk_pct() -> None:
    assert group_of("max_position_pct") == "Risk"
    names = [f.name for f in model_fields_out(RuntimeSettings, by_alias=True)]
    assert names.index("max_position_pct") == names.index("risk_pct") + 1
    (field,) = [f for f in model_fields_out(RuntimeSettings, by_alias=True) if f.name == "max_position_pct"]
    assert field.kind == "decimal" and field.default == "0.10"
    assert (field.minimum, field.exclusive_minimum, field.maximum) == ("0", True, "1")


def _replay_row(settings: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=9,
        label=None,
        status="completed",
        params={
            "date_from": "2026-11-23",
            "date_to": "2026-11-27",
            "data_mode": "offline",
            "catalyst_mode": "stored",
            "half_spread_bps": "5",
            "settings": settings,
        },
        progress=None,
        started_at=datetime(2026, 11, 28, tzinfo=UTC),
        finished_at=None,
        error=None,
        cancel_requested=False,
    )


def test_a_replay_snapshot_from_before_the_cap_keeps_its_behaviour() -> None:
    legacy = RuntimeSettings().model_dump(mode="json", by_alias=True)
    del legacy["max_position_pct"]
    run = replay_run_from_row(_replay_row(legacy))
    assert run.settings.max_position_pct == Decimal("1")  # no cap: the sizing it ran with


def test_a_new_replay_snapshot_keeps_its_cap() -> None:
    snap = RuntimeSettings(max_position_pct=Decimal("0.25")).model_dump(mode="json", by_alias=True)
    assert replay_run_from_row(_replay_row(snap)).settings.max_position_pct == Decimal("0.25")
    default = RuntimeSettings().model_dump(mode="json", by_alias=True)
    assert replay_run_from_row(_replay_row(default)).settings.max_position_pct == Decimal("0.10")
