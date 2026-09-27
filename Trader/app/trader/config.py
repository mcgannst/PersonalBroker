"""Process configuration from environment variables (SPEC §13).

Runtime settings that Stephen edits in the UI live in the database instead
(see trader.settings_store).
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    # A blank value (KEY= in an env template) means "unset", not an empty string or a parse error.
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    # The URLs embed the DB role passwords, so they are secrets too: use .get_secret_value().
    database_url: SecretStr
    # Optional (Phase 4): only Alembic's env.py uses it (reading the environment directly), and the image's
    # entrypoint unsets it before api, worker and cron start, so every process must load without it.
    migration_database_url: SecretStr | None = None
    app_encryption_key: SecretStr
    session_secret: SecretStr
    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    questrade_refresh_token: SecretStr | None = None
    public_base_url: str = "https://trader-dev.sunspinner.ca"
    tz_display: str = "America/Edmonton"
    # --- Phase 4 (all optional). The admin values matter only until the first web user exists; the name is
    # checked by `trader.api.auth.ensure_admin`, never here, so a bad value can't stop the worker or cron.
    admin_username: str | None = None
    admin_password_initial: SecretStr | None = None
    app_env: Literal["dev", "prod"] = "dev"
    app_version: str = "dev"
    web_dist_dir: str | None = None  # WEB_DIST_DIR: the built web app; None means the repo's web/dist
    # The FinViz cache directory itself. Trunk's name (P3-T12 fix round, `runtime.finviz_cache_dir`) is
    # TRADER_FINVIZ_CACHE_DIR; the P4 plan's TRADER_CACHE_DIR parent directory was not adopted.
    finviz_cache_dir: str | None = Field(default=None, validation_alias="TRADER_FINVIZ_CACHE_DIR")


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    return EnvSettings()
