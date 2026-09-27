"""Process-wide logging: structlog JSON lines to stdout, stdlib routed through it, secrets kept out.

Every process (CLI command, cron job, worker, API) calls configure_logging() once at start. Library
code and tests never do: bootstrap.build_core only calls quiet_http_loggers().
"""

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

# httpx logs every request URL at INFO. The Questrade refresh token travels in the token URL's
# query string, and the Telegram bot token in every Bot API URL path (python-telegram-bot logs through
# "telegram"), so these loggers must never log below WARNING.
HTTP_LOGGERS = ("httpx", "httpcore", "telegram")

# An echo=False SQLAlchemy engine logs every statement when its logger's effective level is INFO,
# which a root logger at INFO would give it. Its warnings still come through.
SQL_LOGGERS = ("sqlalchemy",)

REDACTED = "[REDACTED]"

# Defence in depth behind the quiet loggers: secrets that still reach a log line (an exception
# message quoting a URL, a caller logging a URL) are masked before the line is written.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram Bot API URLs: https://api.telegram.org/bot<token>/<method>
    (re.compile(r"(/bot)\d+:[^/\s\"'<>]+"), rf"\1{REDACTED}"),
    # A bare Telegram bot token: <bot id>:<35-character secret>
    (re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}"), REDACTED),
    # Query strings and key=value text: refresh_token=..., access_token=..., password=...
    (
        re.compile(
            r"(?i)\b((?:refresh_|access_|id_)?token|api_?key|client_secret|secret|password)"
            r"=[^&\s\"'<>,]+"
        ),
        rf"\1={REDACTED}",
    ),
    # Authorization headers
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), rf"\1 {REDACTED}"),
)


def quiet_http_loggers() -> None:
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def redact_text(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: mask token-shaped strings in every value, the traceback included."""
    for key, value in event_dict.items():
        event_dict[key] = _redact_value(value)
    return event_dict


def _add_process(process: str) -> Processor:
    def add_process(
        _logger: WrappedLogger, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event_dict.setdefault("process", process)
        return event_dict

    return add_process


class _TraderLogHandler(logging.StreamHandler[TextIO]):
    """The one root handler configure_logging installs (its type marks it for the idempotency check)."""


def configure_logging(process: str, *, json: bool = True, level: str = "INFO") -> None:
    """Configure structlog and the root stdlib logger once for this process.

    Every line carries timestamp (ISO UTC), level, logger, event and process. A second call changes
    nothing (the first configuration wins) and adds no second handler.
    """
    root = logging.getLogger()
    if any(isinstance(h, _TraderLogHandler) for h in root.handlers):
        return

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_process(process),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    handler = _TraderLogHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                redact_secrets,
                renderer,
            ],
        )
    )
    root.addHandler(handler)
    root.setLevel(level.upper())

    quiet_http_loggers()
    for name in SQL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Not cached: module-level loggers made before this call (and tests that reset structlog)
        # always see the current configuration.
        cache_logger_on_first_use=False,
    )
