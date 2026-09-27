"""P4-T1 acceptance test 3: the Phase 4 runtime settings (`web.*`) and the new, all-optional env keys."""

from typing import Any

import pytest
from pydantic import ValidationError

from trader.config import EnvSettings
from trader.settings_store import RuntimeSettings

PHASE4_DEFAULTS: dict[str, tuple[str, Any]] = {
    # DB key: (field name, default)
    "web.session_idle_hours": ("web_session_idle_hours", 168),
    "web.session_max_days": ("web_session_max_days", 30),
    "web.login_max_failures": ("web_login_max_failures", 5),
    "web.lockout_minutes": ("web_lockout_minutes", 15),
    "web.login_rate_per_minute": ("web_login_rate_per_minute", 10),
    "web.sse_poll_seconds": ("web_sse_poll_seconds", 1.0),
    "web.quote_cache_seconds": ("web_quote_cache_seconds", 5.0),
}

# DB key: (lowest valid, highest valid, a step outside). Bounds are inclusive.
BOUNDS: dict[str, tuple[float, float, float]] = {
    "web.session_idle_hours": (1, 2160, 1),
    "web.session_max_days": (1, 365, 1),
    "web.login_max_failures": (3, 20, 1),
    "web.lockout_minutes": (1, 1440, 1),
    "web.login_rate_per_minute": (1, 60, 1),
    "web.sse_poll_seconds": (0.5, 10, 0.01),
    "web.quote_cache_seconds": (1, 60, 0.01),
}

BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://app:app-db-pw-11@db:5432/trader_dev",
    "APP_ENCRYPTION_KEY": "k" * 44,
    "SESSION_SECRET": "super-secret-value",
}
NEW_ENV_KEYS = (
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD_INITIAL",
    "APP_ENV",
    "APP_VERSION",
    "WEB_DIST_DIR",
    "TRADER_FINVIZ_CACHE_DIR",
    "FINVIZ_CACHE_DIR",
    "MIGRATION_DATABASE_URL",
)


@pytest.mark.parametrize("key", sorted(PHASE4_DEFAULTS))
def test_default_and_alias(key: str) -> None:
    field, default = PHASE4_DEFAULTS[key]
    assert RuntimeSettings.model_fields[field].alias == key
    value = getattr(RuntimeSettings(), field)
    assert value == default and type(value) is type(default)


@pytest.mark.parametrize("key", sorted(BOUNDS))
def test_bounds_are_inclusive(key: str) -> None:
    low, high, _ = BOUNDS[key]
    field = PHASE4_DEFAULTS[key][0]
    assert getattr(RuntimeSettings.model_validate({key: low}), field) == low
    assert getattr(RuntimeSettings.model_validate({key: high}), field) == high


@pytest.mark.parametrize("key", sorted(BOUNDS))
def test_out_of_bounds_rejected(key: str) -> None:
    low, high, step = BOUNDS[key]
    for bad in (low - step, high + step):
        with pytest.raises(ValidationError):
            RuntimeSettings.model_validate({key: bad})


@pytest.mark.parametrize("key", ["web.sse_poll_seconds", "web.quote_cache_seconds"])
@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
def test_float_settings_reject_nan_and_inf(key: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({key: bad})


def test_round_trips_through_json() -> None:
    s = RuntimeSettings()
    assert RuntimeSettings.model_validate(s.model_dump(mode="json", by_alias=True)) == s


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in NEW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)


def test_every_new_env_key_is_optional(base_env: None) -> None:
    env = EnvSettings(_env_file=None)
    assert env.admin_username is None
    assert env.admin_password_initial is None
    assert env.app_env == "dev"
    assert env.app_version == "dev"
    assert env.web_dist_dir is None
    assert env.finviz_cache_dir is None


def test_migration_url_may_be_absent(base_env: None) -> None:
    """The entrypoint unsets MIGRATION_DATABASE_URL before api, worker and cron start (T17)."""
    assert EnvSettings(_env_file=None).migration_database_url is None


def test_a_bad_admin_username_never_stops_start_up(base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The name is checked by `ensure_admin` (T4), never by EnvSettings: the worker and cron still start."""
    monkeypatch.setenv("ADMIN_USERNAME", "Stephen!")
    monkeypatch.setenv("ADMIN_PASSWORD_INITIAL", "short")
    env = EnvSettings(_env_file=None)
    assert env.admin_username == "Stephen!"
    assert env.admin_password_initial is not None
    assert env.admin_password_initial.get_secret_value() == "short"
    assert "short" not in repr(env)


def test_new_env_keys_are_read(base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("APP_VERSION", "abc1234-dirty")
    monkeypatch.setenv("WEB_DIST_DIR", "/app/web/dist")
    monkeypatch.setenv("TRADER_FINVIZ_CACHE_DIR", "/tmp/cache/trader/finviz")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://owner:owner-pw@db:5432/trader_dev")
    env = EnvSettings(_env_file=None)
    assert (env.app_env, env.app_version, env.web_dist_dir) == ("prod", "abc1234-dirty", "/app/web/dist")
    assert env.finviz_cache_dir == "/tmp/cache/trader/finviz"
    assert env.migration_database_url is not None
    assert "owner-pw" not in repr(env)


def test_finviz_cache_dir_uses_trunks_env_name(base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The P3-T12 fix round named the variable TRADER_FINVIZ_CACHE_DIR (read by runtime.finviz_cache_dir);
    EnvSettings reads the same name, never the field's default name FINVIZ_CACHE_DIR."""
    monkeypatch.setenv("FINVIZ_CACHE_DIR", "/somewhere/else")
    assert EnvSettings(_env_file=None).finviz_cache_dir is None


def test_app_env_rejects_unknown_values(base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(ValidationError):
        EnvSettings(_env_file=None)
