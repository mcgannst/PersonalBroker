"""Gauntlet breaker tests for P1-T1 (config, CLI, env_setup).

No network, no real secrets: every value here is a fake, and env_setup.py only
ever runs against a temporary copy of a fake .env file.
"""

import importlib.util
import re
import stat
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from trader.cli import app
from trader.config import EnvSettings, get_env

FAKE_SESSION = "fake-session-secret-zz91"
FAKE_ENC = "fake-encryption-key-" + "q" * 24
FAKE_BOT = "123456:fake-bot-token-xy77"
FAKE_ANTHROPIC = "sk-ant-fake-key-ab12"
FAKE_QT = "fake-questrade-refresh-cd34"
FAKE_DB_PW = "fake-db-password-ef56"

ALL_ENV_KEYS = [
    "DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "APP_ENCRYPTION_KEY",
    "SESSION_SECRET",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "QUESTRADE_REFRESH_TOKEN",
    "PUBLIC_BASE_URL",
    "TZ_DISPLAY",
]


@pytest.fixture
def full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", f"postgresql+psycopg://trader_app:{FAKE_DB_PW}@db:5432/trader_dev")
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL", f"postgresql+psycopg://trader_owner:{FAKE_DB_PW}@db:5432/trader_dev"
    )
    monkeypatch.setenv("APP_ENCRYPTION_KEY", FAKE_ENC)
    monkeypatch.setenv("SESSION_SECRET", FAKE_SESSION)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_BOT)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("QUESTRADE_REFRESH_TOKEN", FAKE_QT)


# ---------------------------------------------------------------- config


def test_malformed_chat_id_rejected_without_leaking_secrets(
    full_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "not-a-number")
    with pytest.raises(ValidationError) as exc:
        EnvSettings()
    message = str(exc.value)
    assert "telegram_chat_id" in message
    for secret in (FAKE_SESSION, FAKE_ENC, FAKE_BOT, FAKE_ANTHROPIC, FAKE_QT, FAKE_DB_PW):
        assert secret not in message


def test_negative_group_chat_id_is_accepted(full_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Telegram group/supergroup chat IDs are negative (e.g. -100...). They must parse.
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    assert EnvSettings().telegram_chat_id == -1001234567890


def test_secret_fields_never_leak_via_repr_str_or_json(full_env: None) -> None:
    s = EnvSettings()
    rendered = [repr(s), str(s), s.model_dump_json(), str(s.model_dump())]
    for text in rendered:
        for secret in (FAKE_SESSION, FAKE_ENC, FAKE_BOT, FAKE_ANTHROPIC, FAKE_QT):
            assert secret not in text
    # The values are still reachable deliberately.
    assert s.session_secret.get_secret_value() == FAKE_SESSION
    assert s.telegram_bot_token is not None
    assert s.telegram_bot_token.get_secret_value() == FAKE_BOT


def test_database_password_never_leaks_via_repr_str_or_json(full_env: None) -> None:
    # DATABASE_URL and MIGRATION_DATABASE_URL carry the DB role passwords (SPEC §13).
    # Global Constraints: never print or log a secret value. A settings object that
    # ends up in a log line or traceback must not expose them.
    s = EnvSettings()
    for text in (repr(s), str(s), s.model_dump_json()):
        assert FAKE_DB_PW not in text


def test_get_env_is_cached_and_cache_clear_rereads(full_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    get_env.cache_clear()
    try:
        first = get_env()
        monkeypatch.setenv("TZ_DISPLAY", "UTC")
        assert get_env() is first
        assert get_env().tz_display == "America/Edmonton"
        get_env.cache_clear()
        assert get_env().tz_display == "UTC"
    finally:
        get_env.cache_clear()


# ---------------------------------------------------------------- CLI


def test_cli_no_args_shows_help_and_unknown_command_fails() -> None:
    runner = CliRunner()
    no_args = runner.invoke(app, [])
    assert no_args.exit_code in (0, 2)
    assert "version" in no_args.output
    assert "Usage" in no_args.output

    unknown = runner.invoke(app, ["definitely-not-a-command"])
    assert unknown.exit_code != 0
    assert "No such command" in unknown.output


# ---------------------------------------------------------------- env_setup.py


def _load_env_setup() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "build" / "env_setup.py"
    spec = importlib.util.spec_from_file_location("p1_t1_env_setup_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _values(text: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.M))


def test_env_setup_is_idempotent_and_never_prints_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env.dev"
    env_file.write_text(
        f"DATABASE_URL=postgresql+psycopg://trader_app:{FAKE_DB_PW}@db:5432/trader_dev\n"
        f"DATABASE_OWNER_URL=postgresql+psycopg://trader_owner:{FAKE_DB_PW}@db:5432/trader_dev\n"
        f"TELEGRAM_BOT_TOKEN={FAKE_BOT}\n"
    )
    env_file.chmod(0o644)
    module = _load_env_setup()
    monkeypatch.setattr(module, "ENV", env_file)

    module.main()
    out1 = capsys.readouterr().out
    after_first = env_file.read_text()
    values = _values(after_first)

    assert "MIGRATION_DATABASE_URL" in values
    assert "DATABASE_OWNER_URL" not in values
    assert values["DATABASE_URL"].endswith("/trader_dev")
    assert values["APP_ENCRYPTION_KEY"]
    assert values["SESSION_SECRET"]
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    for v in values.values():
        assert v not in out1
    assert FAKE_DB_PW not in out1

    module.main()
    out2 = capsys.readouterr().out
    assert out2.strip() == "changed: nothing"
    assert env_file.read_text() == after_first
    # No stray temp files left beside the env file.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env.dev"]


def test_env_setup_handles_file_without_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Editors often save the last line without a newline. Appending a key must not
    # glue it onto the previous value (that would silently corrupt a secret).
    env_file = tmp_path / ".env.dev"
    env_file.write_text(
        f"MIGRATION_DATABASE_URL=postgresql://o:{FAKE_DB_PW}@db/x\nAPP_ENCRYPTION_KEY={FAKE_ENC}"
    )
    module = _load_env_setup()
    monkeypatch.setattr(module, "ENV", env_file)

    module.main()
    capsys.readouterr()
    values = _values(env_file.read_text())
    assert values["APP_ENCRYPTION_KEY"] == FAKE_ENC
    assert values.get("SESSION_SECRET")
