"""Process-wide logging settings that protect secrets."""

import logging

# httpx logs every request URL at INFO. The Questrade refresh token travels in the token URL's
# query string, so these loggers must never log below WARNING.
HTTP_LOGGERS = ("httpx", "httpcore")


def quiet_http_loggers() -> None:
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
