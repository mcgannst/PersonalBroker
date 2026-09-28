"""P5-T1 acceptance test 2: the Phase 5 runtime settings (`replay.*`, `reports.*`, `jobs.*`, `logging.*`)."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from trader.api import forms
from trader.api.forms import group_of
from trader.settings_store import RuntimeSettings

PHASE5_DEFAULTS: dict[str, tuple[str, Any]] = {
    # DB key: (field name, default)
    "replay.half_spread_bps": ("replay_half_spread_bps", Decimal("5")),
    "replay.catalyst_mode": ("replay_catalyst_mode", "stored"),
    "replay.questrade_rps": ("replay_questrade_rps", 4.0),
    "replay.questrade_window_days": ("replay_questrade_window_days", 85),
    "replay.max_sessions": ("replay_max_sessions", 130),
    "reports.weekly_commentary": ("reports_weekly_commentary", True),
    "reports.weekly_max_cost_usd": ("reports_weekly_max_cost_usd", Decimal("0.05")),
    "jobs.retry_attempts": ("jobs_retry_attempts", 3),
    "jobs.retry_delay_seconds": ("jobs_retry_delay_seconds", 120),
    "logging.mirror_level": ("logging_mirror_level", "error"),
    "logging.mirror_max_per_minute": ("logging_mirror_max_per_minute", 30),
}

# DB key: (lowest valid, highest valid, a step outside). Bounds are inclusive.
BOUNDS: dict[str, tuple[Any, Any, Any]] = {
    "replay.half_spread_bps": (Decimal("0"), Decimal("100"), Decimal("0.01")),
    "replay.questrade_rps": (1.0, 10.0, 0.01),
    "replay.questrade_window_days": (1, 120, 1),
    "replay.max_sessions": (1, 500, 1),
    "reports.weekly_max_cost_usd": (Decimal("0"), Decimal("1"), Decimal("0.01")),
    "jobs.retry_attempts": (1, 3, 1),  # P5-GO fix round 1 (was 1-5)
    "jobs.retry_delay_seconds": (10, 600, 1),  # P5-GO fix round 1 (was 10-1800)
    "logging.mirror_max_per_minute": (1, 600, 1),
}

LITERALS: dict[str, tuple[set[str], str]] = {
    "replay.catalyst_mode": ({"stored", "unknown"}, "claude"),
    "logging.mirror_level": ({"error", "critical", "off"}, "warning"),
}

GROUPS = {"replay.": "Replay", "reports.": "Reports", "jobs.": "Operations", "logging.": "Operations"}


@pytest.mark.parametrize("key", sorted(PHASE5_DEFAULTS))
def test_default_and_alias(key: str) -> None:
    field, default = PHASE5_DEFAULTS[key]
    assert RuntimeSettings.model_fields[field].alias == key
    value = getattr(RuntimeSettings(), field)
    assert value == default and type(value) is type(default)


@pytest.mark.parametrize("key", sorted(BOUNDS))
def test_bounds_are_inclusive(key: str) -> None:
    low, high, _ = BOUNDS[key]
    field = PHASE5_DEFAULTS[key][0]
    assert getattr(RuntimeSettings.model_validate({key: low}), field) == low
    assert getattr(RuntimeSettings.model_validate({key: high}), field) == high


@pytest.mark.parametrize("key", sorted(BOUNDS))
def test_out_of_bounds_rejected(key: str) -> None:
    low, high, step = BOUNDS[key]
    for bad in (low - step, high + step):
        with pytest.raises(ValidationError):
            RuntimeSettings.model_validate({key: bad})


@pytest.mark.parametrize("key", sorted(LITERALS))
def test_literal_settings(key: str) -> None:
    allowed, bad = LITERALS[key]
    field = PHASE5_DEFAULTS[key][0]
    for value in allowed:
        assert getattr(RuntimeSettings.model_validate({key: value}), field) == value
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({key: bad})


@pytest.mark.parametrize(
    "key", ["replay.questrade_rps", "replay.half_spread_bps", "reports.weekly_max_cost_usd"]
)
@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
def test_numbers_reject_nan_and_inf(key: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({key: bad})


@pytest.mark.parametrize("key", sorted(PHASE5_DEFAULTS))
def test_every_new_key_has_a_settings_group(key: str) -> None:
    prefix = key.split(".")[0] + "."
    assert group_of(key) == GROUPS[prefix]
    assert group_of(key) != forms.OTHER_GROUP


def test_round_trips_through_json() -> None:
    s = RuntimeSettings()
    assert RuntimeSettings.model_validate(s.model_dump(mode="json", by_alias=True)) == s
