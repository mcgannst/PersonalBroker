"""P3-T1 acceptance test 3: the Phase 3 runtime settings (defaults, bounds, event-key list)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from trader.settings_store import RuntimeSettings, SettingsStore

PHASE3_DEFAULTS: dict[str, tuple[str, Any]] = {
    # DB key: (field name, default)
    "worker.heartbeat_seconds": ("worker_heartbeat_seconds", 15),
    "worker.heartbeat_stale_seconds": ("worker_heartbeat_stale_seconds", 120),
    "worker.idle_poll_seconds": ("worker_idle_poll_seconds", 30.0),
    "scheduler.late_grace_seconds": ("scheduler_late_grace_seconds", 120),
    "scheduler.always_fire_late": (
        "scheduler_always_fire_late",
        ["flatten", "entry_cancel", "overlay_decision"],
    ),
    "telegram.poll_timeout_seconds": ("telegram_poll_timeout_seconds", 30),
    "telegram.confirm_ttl_seconds": ("telegram_confirm_ttl_seconds", 60),
    "telegram.relay_catchup_max": ("telegram_relay_catchup_max", 20),
    "preopen.notify_when_ok": ("preopen_notify_when_ok", True),
    "postclose.archive_top_n": ("postclose_archive_top_n", 20),
}

# DB key: (lowest valid, highest valid). Numeric bounds are inclusive.
BOUNDS: dict[str, tuple[float, float]] = {
    "worker.heartbeat_seconds": (5, 300),
    "worker.heartbeat_stale_seconds": (30, 3600),
    "worker.idle_poll_seconds": (5, 300),
    "scheduler.late_grace_seconds": (0, 3600),
    "telegram.poll_timeout_seconds": (1, 50),
    "telegram.confirm_ttl_seconds": (10, 600),
    "telegram.relay_catchup_max": (0, 200),
    "postclose.archive_top_n": (0, 100),
}


@pytest.mark.parametrize("key", sorted(PHASE3_DEFAULTS))
def test_default_and_alias(key: str) -> None:
    field, default = PHASE3_DEFAULTS[key]
    assert RuntimeSettings.model_fields[field].alias == key
    assert getattr(RuntimeSettings(), field) == default


def test_idle_poll_is_a_float() -> None:
    assert isinstance(RuntimeSettings().worker_idle_poll_seconds, float)
    assert RuntimeSettings.model_validate({"worker.idle_poll_seconds": 7.5}).worker_idle_poll_seconds == 7.5


@pytest.mark.parametrize("key", sorted(BOUNDS))
def test_bounds_are_inclusive(key: str) -> None:
    low, high = BOUNDS[key]
    field = PHASE3_DEFAULTS[key][0]
    assert getattr(RuntimeSettings.model_validate({key: low}), field) == low
    assert getattr(RuntimeSettings.model_validate({key: high}), field) == high


@pytest.mark.parametrize("key", sorted(BOUNDS))
def test_out_of_bounds_rejected(key: str) -> None:
    low, high = BOUNDS[key]
    for bad in (low - 1, high + 1):
        with pytest.raises(ValidationError):
            RuntimeSettings.model_validate({key: bad})


@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
def test_idle_poll_rejects_nan_and_inf(bad: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({"worker.idle_poll_seconds": bad})


def test_notify_when_ok_accepts_false() -> None:
    assert RuntimeSettings.model_validate({"preopen.notify_when_ok": False}).preopen_notify_when_ok is False


def test_always_fire_late_accepts_valid_keys() -> None:
    longest = "a" + "b" * 38  # 39 characters: `event:<key>` and `job.event:<key>` fit varchar(50)
    s = RuntimeSettings.model_validate({"scheduler.always_fire_late": ["flatten", longest, "x_1"]})
    assert s.scheduler_always_fire_late == ["flatten", longest, "x_1"]
    assert RuntimeSettings.model_validate({"scheduler.always_fire_late": []}).scheduler_always_fire_late == []


@pytest.mark.parametrize(
    "value",
    [
        ["flatten", "flatten"],  # duplicate
        ["Flatten"],  # upper case
        ["1flatten"],  # must start with a letter
        ["_flatten"],
        ["flat-ten"],
        ["flat ten"],
        [""],
        ["a" + "b" * 39],  # 40 characters: too long for job_runs.job / event_log.source
        "flatten",  # not a list
    ],
)
def test_always_fire_late_rejects_malformed(value: Any) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({"scheduler.always_fire_late": value})


def test_round_trips_through_json() -> None:
    s = RuntimeSettings()
    assert RuntimeSettings.model_validate(s.model_dump(mode="json", by_alias=True)) == s


@pytest.mark.db
def test_store_sets_phase3_keys(db_factory: sessionmaker[Session]) -> None:
    store = SettingsStore(db_factory, now=lambda: datetime(2026, 10, 6, 12, 0, tzinfo=UTC))
    store.set("scheduler.always_fire_late", ["flatten"], actor="stephen")
    store.set("worker.idle_poll_seconds", 10, actor="stephen")
    with pytest.raises(ValidationError):
        store.set("scheduler.always_fire_late", ["flatten", "flatten"], actor="stephen")
    loaded = store.load()
    assert loaded.scheduler_always_fire_late == ["flatten"]
    assert loaded.worker_idle_poll_seconds == 10.0
