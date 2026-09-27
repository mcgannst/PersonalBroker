"""`python -m trader.api`: the API process (supervisord program `api`).

One uvicorn worker (the change feed and the login rate limiter live in memory) on 0.0.0.0:8000 behind Nginx
Proxy Manager. uvicorn's own logging config is off (`log_config=None`, no access log), so its `uvicorn` and
`uvicorn.error` records reach the root handler `configure_logging("api")` installed: JSON and redacted. The
request log is the app's own `http.request` line.
"""

import uvicorn

from trader import logging_setup
from trader.api.main import create_app


def main() -> None:
    logging_setup.configure_logging("api")  # first, before anything can log
    uvicorn.run(
        create_app(),
        host="0.0.0.0",  # noqa: S104  # inside the container, reached only through the proxy network
        port=8000,
        log_config=None,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
