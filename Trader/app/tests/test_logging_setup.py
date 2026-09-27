import logging

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from trader.bootstrap import build_core
from trader.config import EnvSettings
from trader.logging_setup import quiet_http_loggers


@pytest.fixture
def noisy_http_loggers() -> None:
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)


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
    core = build_core(env)  # creating the engine does not connect
    try:
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert core.engine.hide_parameters
    finally:
        core.engine.dispose()
