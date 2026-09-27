"""Process-wide logging: structlog JSON lines to stdout, stdlib routed through it, secrets kept out.

Every process (CLI command, cron job, worker, API) calls configure_logging() once at start. Library
code and tests never do: bootstrap.build_core only calls quiet_http_loggers().

Secrets are kept out in three layers: the token-bearing library loggers stay at WARNING; every value of
an event is masked before rendering (fields named after a secret entirely, other strings by pattern); and
the rendered line itself goes through the same patterns once more, as a final net.
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
# "telegram"), so these loggers must never log below WARNING. The Anthropic SDK logs its request
# options (headers included) at DEBUG.
HTTP_LOGGERS = ("httpx", "httpx2", "httpcore", "telegram", "anthropic")

# An echo=False SQLAlchemy engine logs every statement when its logger's effective level is INFO,
# which a root logger at INFO would give it. Its warnings still come through.
SQL_LOGGERS = ("sqlalchemy",)

REDACTED = "[REDACTED]"

# Field and JSON key names whose value is always a secret (matched exactly, ignoring case, so
# token_age_hours or token_ok are not secrets).
SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "bot_token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "client_secret",
        "session_secret",
        "authorization",
    }
)

_JSON_KEYS = (
    r"access_token|refresh_token|id_token|bot_token|token|api_?key"
    r"|client_secret|session_secret|secret|password|authorization"
)

# Defence in depth behind the quiet loggers: secrets that still reach a log line (an exception
# message quoting a URL or a response body, a caller logging a URL) are masked before the line is
# written. Value character classes exclude the backslash and quotes, so masking a rendered JSON line
# never breaks its string escaping.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram Bot API URLs: https://api.telegram.org/bot<token>/<method>
    (re.compile(r"(/bot)\d+:[^/\s\"'<>\\]+"), rf"\1{REDACTED}"),
    # A bare Telegram bot token: <bot id>:<35-character secret with capitals> (not 20261006:abc...)
    (re.compile(r"\b\d{6,}:(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{30,}"), REDACTED),
    # Credentials in a URL: postgresql://user:PASSWORD@host
    (
        re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://[^:/\s@\"'\\]*:)[^/\s\"'\\]+(@)"),
        rf"\1{REDACTED}\2",
    ),
    # Query strings and key=value text: refresh_token=..., access_token=..., password=...
    (
        re.compile(
            r"(?i)\b((?:refresh_|access_|id_|bot_)?token|api_?key|(?:client_|session_)?secret|password)"
            r"=[^&\s\"'<>,\\]+"
        ),
        rf"\1={REDACTED}",
    ),
    # JSON bodies and Python dict reprs: {"refresh_token": "..."}, input_value={'password': '...'}
    (re.compile(rf"(?i)(\"(?:{_JSON_KEYS})\"\s*:\s*)\"(?:[^\"\\]|\\.)*\""), rf'\1"{REDACTED}"'),
    (re.compile(rf"(?i)('(?:{_JSON_KEYS})'\s*:\s*)'(?:[^'\\]|\\.)*'"), rf"\1'{REDACTED}'"),
    # ... and the same inside a rendered JSON string, where the quotes are escaped
    (re.compile(rf'(?i)(\\"(?:{_JSON_KEYS})\\"\s*:\s*)\\"[^"\\]*\\"'), rf'\1\\"{REDACTED}\\"'),
    # Authorization headers: a token-shaped credential (so "Bearer market today" is left alone)
    (
        re.compile(r"(?i)\b(bearer)\s+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,}"),
        rf"\1 {REDACTED}",
    ),
)


def quiet_http_loggers() -> None:
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def is_secret_key(key: object) -> bool:
    """True when a field or JSON key name always holds a secret (exact name, any case)."""
    return isinstance(key, str) and key.lower() in SECRET_KEYS


def redact_text(text: str) -> str:
    """Mask every token-, password- and credential-shaped part of `text`."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any) -> Any:
    """A JSON-safe, masked copy: strings by pattern, secret-named keys entirely, and any other object
    (a URL object, a Decimal, a set) through its str() so no secret can hide in a repr."""
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {
            k: REDACTED if is_secret_key(k) and v is not None else _redact_value(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact_value(v) for v in value]
    return redact_text(str(value))


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: mask secrets in every value, the traceback included."""
    for key, value in event_dict.items():
        if is_secret_key(key) and value is not None:
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


def _redacting(renderer: Processor) -> Processor:
    """The final net: the rendered line goes through the patterns once more."""

    def render(logger: WrappedLogger, method: str, event_dict: EventDict) -> Any:
        line = renderer(logger, method, event_dict)
        return redact_text(line) if isinstance(line, str) else line

    return render


def _add_process(process: str) -> Processor:
    def add_process(
        _logger: WrappedLogger, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event_dict["process"] = process  # a bound `process` field cannot pose as another process
        return event_dict

    return add_process


def _json_default(value: object) -> str:
    return str(value)  # Decimal "21.5608", not a float or a repr


def console_renderer(*, colors: bool) -> structlog.dev.ConsoleRenderer:
    """The development renderer. Its tracebacks are plain: structlog's default (rich with show_locals)
    would print every frame's local variables, and a frame can hold a token or a password."""
    return structlog.dev.ConsoleRenderer(colors=colors, exception_formatter=structlog.dev.plain_traceback)


class _TraderLogHandler(logging.StreamHandler[TextIO]):
    """The one root handler configure_logging installs (its type marks it for the idempotency check)."""


# Handlers logging.basicConfig() installs: replaced, so a prior basicConfig cannot print a second,
# unmasked copy of every line. (Subclasses, such as pytest's capture handlers, are left alone.)
_BASIC_HANDLER_TYPES = (logging.StreamHandler, logging.FileHandler)


def configure_logging(process: str, *, json: bool = True, level: str = "INFO") -> None:
    """Configure structlog and the root stdlib logger once for this process.

    Every line carries timestamp (ISO UTC), level, logger, event and process. A second call changes
    nothing (the first configuration wins) and adds no second handler.
    """
    root = logging.getLogger()
    if any(isinstance(h, _TraderLogHandler) for h in root.handlers):
        return
    for existing in list(root.handlers):
        if type(existing) in _BASIC_HANDLER_TYPES:
            root.removeHandler(existing)
            existing.close()

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
        structlog.processors.JSONRenderer(default=_json_default)
        if json
        else console_renderer(colors=sys.stdout.isatty())
    )

    handler = _TraderLogHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # ExtraAdder: a stdlib `extra={...}` becomes fields (masked like any other).
            foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *shared],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                redact_secrets,
                _redacting(renderer),
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
