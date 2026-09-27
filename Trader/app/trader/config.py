"""Process configuration from environment variables (SPEC §13).

Runtime settings that Stephen edits in the UI live in the database instead
(see trader.settings_store).
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # The URLs embed the DB role passwords, so they are secrets too: use .get_secret_value().
    database_url: SecretStr
    migration_database_url: SecretStr
    app_encryption_key: SecretStr
    session_secret: SecretStr
    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    questrade_refresh_token: SecretStr | None = None
    public_base_url: str = "https://trader-dev.sunspinner.ca"
    tz_display: str = "America/Edmonton"


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    return EnvSettings()
