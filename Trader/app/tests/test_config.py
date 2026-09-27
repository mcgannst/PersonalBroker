import pytest
from pydantic import ValidationError

from trader.config import EnvSettings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://app:pw@db:5432/trader_dev")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://owner:pw@db:5432/trader_dev")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "k" * 44)
    monkeypatch.setenv("SESSION_SECRET", "super-secret-value")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


def test_reads_environment(env: None) -> None:
    s = EnvSettings()  # type: ignore[call-arg]
    assert s.database_url.endswith("/trader_dev")
    assert s.telegram_chat_id == 42
    assert s.tz_display == "America/Edmonton"


def test_secrets_are_hidden_in_repr(env: None) -> None:
    s = EnvSettings()  # type: ignore[call-arg]
    assert "super-secret-value" not in repr(s)
    assert s.session_secret.get_secret_value() == "super-secret-value"


def test_missing_required_value_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        EnvSettings()  # type: ignore[call-arg]
