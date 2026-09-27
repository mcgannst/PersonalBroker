"""`trader nightly` refuses bad dates and too-early runs; `trader notify` fails cleanly on bad config.

No database, network or real credentials: build_core and httpx are replaced."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from trader import bootstrap, config
from trader.cli import app
from trader.jobs import runner
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

TOKEN = "123456:SECRET-token-value"


def fake_core(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    core = SimpleNamespace(
        calendar=SessionCalendar(),
        clock=FixedClock(now),
        settings=SimpleNamespace(load=RuntimeSettings),
        factory=None,
        crypto=None,
    )
    monkeypatch.setattr(bootstrap, "build_core", lambda: core)

    def no_run(*_: object, **__: object) -> None:
        raise AssertionError("the job must not start")

    monkeypatch.setattr(runner, "run_job", no_run)


@pytest.mark.parametrize("value", ["2026-09-27", "2026-11-26", "not-a-date", "2031-06-02"])
def test_nightly_date_must_be_a_trading_session(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A Sunday, Thanksgiving, garbage, and a date past the calendar's range."""
    fake_core(monkeypatch, datetime(2026, 9, 28, 0, 0, tzinfo=UTC))
    result = CliRunner().invoke(app, ["nightly", "--date", value, "--force"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "is not a trading session" in result.output


def test_nightly_refuses_a_daytime_run_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mon 28 Sep 10:00 ET prepares Tue 29 Sep, whose last lookback session (today) hasn't closed."""
    fake_core(monkeypatch, datetime(2026, 9, 28, 14, 0, tzinfo=UTC))
    result = CliRunner().invoke(app, ["nightly"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "too early for 2026-09-29" in result.output


def test_nightly_refuses_until_15_minutes_after_the_close(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_core(monkeypatch, datetime(2026, 9, 25, 20, 14, 59, tzinfo=UTC))  # Fri 16:14:59 EDT
    result = CliRunner().invoke(app, ["nightly", "--date", "2026-09-28"])
    assert result.exit_code == 1
    assert "too early for 2026-09-28" in result.output


def _env(monkeypatch: pytest.MonkeyPatch, **telegram: str) -> None:
    for key, value in {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:1/x",
        "MIGRATION_DATABASE_URL": "postgresql+psycopg://u:p@localhost:1/x",
        "APP_ENCRYPTION_KEY": "k" * 44,
        "SESSION_SECRET": "s" * 32,
        **telegram,
    }.items():
        monkeypatch.setenv(key, value)
    config.get_env.cache_clear()


@pytest.fixture(autouse=True)
def _clear_env_cache() -> object:
    yield
    config.get_env.cache_clear()


def test_notify_with_invalid_chat_id_exits_1_without_echoing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=f"not-a-number-{TOKEN}")
    result = CliRunner().invoke(app, ["notify", "hello"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "telegram_chat_id" in result.output
    assert TOKEN not in result.output


def test_notify_invalid_url_exits_1_without_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    _env(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="42")

    def bad_url(url: str, **_: object) -> None:
        raise httpx.InvalidURL(f"Invalid URL {url}")

    monkeypatch.setattr(httpx, "post", bad_url)
    result = CliRunner().invoke(app, ["notify", "hello"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "InvalidURL" in result.output
    assert TOKEN not in result.output


def test_blank_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="", PUBLIC_BASE_URL="")
    env = config.get_env()
    assert env.telegram_bot_token is None and env.telegram_chat_id is None
    assert env.public_base_url == "https://trader-dev.sunspinner.ca"
