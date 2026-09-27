import json
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import structlog
from cryptography.fernet import Fernet
from pydantic import SecretStr

from trader.bootstrap import build_core
from trader.config import EnvSettings
from trader.logging_setup import (
    HTTP_LOGGERS,
    configure_logging,
    console_renderer,
    quiet_http_loggers,
    redact_text,
)

# Loggers whose levels configure_logging changes: restored after every test so no test leaks them.
TOUCHED_LOGGERS = ("", "httpx", "httpcore", "telegram", "sqlalchemy")


@pytest.fixture
def noisy_http_loggers() -> None:
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)


@pytest.fixture
def clean_logging() -> Iterator[None]:
    """Undo configure_logging: its handler, the logger levels and the structlog configuration."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    levels = {name: logging.getLogger(name).level for name in TOUCHED_LOGGERS}
    structlog.reset_defaults()
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


def json_lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@pytest.mark.usefixtures("noisy_http_loggers")
def test_quiet_http_loggers_raises_them_to_warning() -> None:
    quiet_http_loggers()
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        assert logger.level == logging.WARNING
        assert not logger.isEnabledFor(logging.INFO)  # the token URL is logged at INFO


@pytest.mark.usefixtures("noisy_http_loggers")
def test_build_core_quiets_http_loggers() -> None:
    env = EnvSettings(
        database_url=SecretStr("postgresql+psycopg://u:p@127.0.0.1:1/none"),
        migration_database_url=SecretStr("postgresql+psycopg://u:p@127.0.0.1:1/none"),
        app_encryption_key=SecretStr(Fernet.generate_key().decode()),
        session_secret=SecretStr("x"),
    )
    root_handlers = list(logging.getLogger().handlers)
    core = build_core(env)  # creating the engine does not connect
    try:
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert core.engine.hide_parameters
        # build_core is used by tests and library code: it must not configure logging itself.
        assert logging.getLogger().handlers == root_handlers
    finally:
        core.engine.dispose()


@pytest.mark.usefixtures("clean_logging")
def test_structlog_event_is_one_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("cron")
    structlog.get_logger("jobs.test").info("x", a=1)
    lines = json_lines(capsys.readouterr().out)
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "x"
    assert line["a"] == 1
    assert line["process"] == "cron"
    assert line["level"] == "info"
    assert line["logger"] == "jobs.test"
    ts = datetime.fromisoformat(line["timestamp"])
    assert ts.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - ts) < timedelta(minutes=5)


@pytest.mark.usefixtures("clean_logging")
def test_stdlib_record_comes_out_in_the_same_json_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("cron")
    logging.getLogger("trader.adapters.finviz.scraper").warning("blocked %s", "page")
    lines = json_lines(capsys.readouterr().out)
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "blocked page"
    assert line["logger"] == "trader.adapters.finviz.scraper"
    assert line["level"] == "warning"
    assert line["process"] == "cron"
    assert datetime.fromisoformat(line["timestamp"]).utcoffset() == timedelta(0)


@pytest.mark.usefixtures("clean_logging")
def test_configure_twice_adds_no_second_handler(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    configure_logging("worker")
    count = len(root.handlers)
    configure_logging("worker")
    configure_logging("cron", level="DEBUG")  # a later call changes nothing
    assert len(root.handlers) == count
    ours = [h for h in root.handlers if isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)]
    assert len(ours) == 1
    structlog.get_logger("t").info("once")
    logging.getLogger("t.stdlib").info("stdlib once")
    lines = json_lines(capsys.readouterr().out)
    assert [line["event"] for line in lines] == ["once", "stdlib once"]
    assert {line["process"] for line in lines} == {"worker"}
    assert root.level == logging.INFO


@pytest.mark.usefixtures("clean_logging")
def test_token_bearing_loggers_are_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    assert "telegram" in HTTP_LOGGERS
    configure_logging("worker", level="DEBUG")
    for name in ("httpx", "httpcore", "telegram"):
        assert logging.getLogger(name).level == logging.WARNING
    logging.getLogger("httpx").info("GET https://api.telegram.org/bot123:SECRET/getUpdates")
    logging.getLogger("telegram.ext").info("POST https://api.telegram.org/bot123:SECRET/x")
    out = capsys.readouterr().out
    assert "SECRET" not in out
    assert out == ""


@pytest.mark.usefixtures("clean_logging")
def test_sqlalchemy_statements_are_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    # An echo=False engine defers to the logger's effective level, so an INFO root would print SQL.
    configure_logging("api")
    assert not logging.getLogger("sqlalchemy.engine.Engine").isEnabledFor(logging.INFO)
    logging.getLogger("sqlalchemy.engine.Engine").warning("pool trouble")
    assert json_lines(capsys.readouterr().out)[0]["event"] == "pool trouble"


@pytest.mark.usefixtures("clean_logging")
def test_exception_is_rendered_into_exception_field(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("cron")
    log = structlog.get_logger("jobs.test")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("job failed", job="nightly")
    try:
        raise KeyError("stdlib boom")
    except KeyError:
        logging.getLogger("trader.stdlib").exception("stdlib failed")
    lines = json_lines(capsys.readouterr().out)
    assert len(lines) == 2
    assert lines[0]["event"] == "job failed"
    assert lines[0]["level"] == "error"
    assert "Traceback (most recent call last)" in lines[0]["exception"]
    assert "ValueError: boom" in lines[0]["exception"]
    assert "exc_info" not in lines[0]
    assert "KeyError: 'stdlib boom'" in lines[1]["exception"]


@pytest.mark.usefixtures("clean_logging")
def test_secrets_are_redacted_from_emitted_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker")
    token = "123456789:AAH4sEcReTvAlUe-abcdefghijklmnopqrstu"
    log = structlog.get_logger("t")
    log.warning("send failed", url=f"https://api.telegram.org/bot{token}/sendMessage")
    logging.getLogger("httpx").warning(
        "POST https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token=R3fr35h"
    )
    log.info(
        "bare token",
        detail=f"token {token} rejected",
        nested={"u": ["https://api.telegram.org/bot123:SECRET/x"]},
        header="Authorization: Bearer abc.DEF-123",
    )
    try:
        raise RuntimeError(f"HTTP 401 for https://api.telegram.org/bot{token}/getMe")
    except RuntimeError:
        log.exception("telegram down")
    out = capsys.readouterr().out
    assert "AAH4sEcReTvAlUe" not in out
    assert "R3fr35h" not in out
    assert "SECRET" not in out
    assert "abc.DEF-123" not in out
    lines = json_lines(out)
    assert len(lines) == 4
    assert "grant_type=refresh_token" in lines[1]["event"]  # only the value is hidden
    assert "api.telegram.org/bot" in lines[0]["url"]
    assert "[REDACTED]" in lines[3]["exception"]


@pytest.mark.usefixtures("clean_logging")
def test_console_renderer_when_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("cli", json=False)
    structlog.get_logger("t").info("hello", a=1)
    out = capsys.readouterr().out
    assert "hello" in out
    assert "a=1" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.splitlines()[0])


# --- P3-T2 fix round 1: the redaction net ------------------------------------------------------------------

FAKE_BOT_TOKEN = "7123456789:AAH4sEcReTvAlUe-abcdefghijklmnopqrstu"
FAKE_REFRESH = "QtR3fr35hT0k3nValue123"


def test_redact_text_masks_json_dict_and_url_credentials() -> None:
    body = json.dumps({"access_token": "A1b2C3d4", "refresh_token": FAKE_REFRESH, "api_server": "https://x/"})
    masked = redact_text(body)
    assert "A1b2C3d4" not in masked and FAKE_REFRESH not in masked
    assert json.loads(masked)["api_server"] == "https://x/"
    pydantic_text = f"input_value={{'refresh_token': '{FAKE_REFRESH}', 'password': 'hunter2'}}"
    assert FAKE_REFRESH not in redact_text(pydantic_text) and "hunter2" not in redact_text(pydantic_text)
    db = redact_text("could not connect to postgresql+psycopg://trader:S3cr3tPw@10.0.0.86:5432/trader")
    assert "S3cr3tPw" not in db and "trader:[REDACTED]@10.0.0.86:5432/trader" in db
    assert redact_text("http://trader.home:8080/journal?date=2026-10-06") == (
        "http://trader.home:8080/journal?date=2026-10-06"
    )


def test_redact_text_leaves_ordinary_words_and_callback_ids() -> None:
    for text in (
        "Bearer market today",
        "session 20261006:abcdefghijklmnopqrstuvwxyzabcdefgh",
        "token_age_hours=3.2 token_ok=True",
        '{"token_ok": true, "token_age_hours": 3.2}',
    ):
        assert redact_text(text) == text


@pytest.mark.usefixtures("clean_logging")
def test_secret_named_fields_and_non_json_values_are_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker")
    log = structlog.get_logger("t")
    log.info(
        "seeded",
        refresh_token=FAKE_REFRESH,
        Password="hunter2",
        nested={"bot_token": FAKE_BOT_TOKEN, "token_ok": True},
        url=httpx.URL(f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getMe"),
        tokens={FAKE_BOT_TOKEN},
        price=Decimal("21.5608"),
        process="impostor",
    )
    logging.getLogger("trader.stdlib").warning("send failed", extra={"token": FAKE_REFRESH, "chat": 42})
    out = capsys.readouterr().out
    assert FAKE_REFRESH not in out and "hunter2" not in out and "AAH4sEcReTvAlUe" not in out
    first, second = json_lines(out)
    assert first["refresh_token"] == "[REDACTED]" and first["Password"] == "[REDACTED]"
    assert first["nested"] == {"bot_token": "[REDACTED]", "token_ok": True}
    assert first["price"] == "21.5608"  # Decimal as its string, not a float
    assert first["process"] == "worker"  # a bound field cannot pose as another process
    assert second["chat"] == 42 and second["token"] == "[REDACTED]"  # stdlib extra= comes through


@pytest.mark.usefixtures("clean_logging")
def test_configure_replaces_a_basic_config_handler(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    basic = logging.StreamHandler(sys.stdout)
    root.addHandler(basic)
    try:
        configure_logging("cron")
        assert basic not in root.handlers
        logging.getLogger("trader.x").warning("token %s", FAKE_BOT_TOKEN)
        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1
        assert "AAH4sEcReTvAlUe" not in out
    finally:
        root.removeHandler(basic)


@pytest.mark.usefixtures("clean_logging")
def test_anthropic_logger_is_quiet() -> None:
    configure_logging("cron", level="DEBUG")
    assert not logging.getLogger("anthropic._base_client").isEnabledFor(logging.INFO)


def _fails_with_a_secret_local() -> None:
    api_key_local = "sk-LOCALS-" + "Z9" * 10  # noqa: F841 (a local a rich traceback would show)
    raise RuntimeError("boom")


@pytest.mark.usefixtures("clean_logging")
def test_console_renderer_never_shows_traceback_locals(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("cli", json=False)
    try:
        _fails_with_a_secret_local()
    except RuntimeError:
        structlog.get_logger("t").exception("failed")
        logging.getLogger("t.stdlib").exception("failed")
    out = capsys.readouterr().out
    assert "RuntimeError: boom" in out
    assert "Z9Z9Z9" not in out
    assert "api_key_local" not in out
    # The renderer itself, handed a live exc_info (where rich's show_locals would print the frame's
    # locals), shows only the plain traceback.
    try:
        _fails_with_a_secret_local()
    except RuntimeError:
        event = {"event": "failed", "exc_info": sys.exc_info()}
        rendered = console_renderer(colors=False)(None, "error", event)
    assert "RuntimeError: boom" in rendered
    assert "Z9Z9Z9" not in rendered and "api_key_local" not in rendered
