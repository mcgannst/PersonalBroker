"""`python -m trader.api`: the API process (supervisord program `api`).

One uvicorn worker (the change feed and the login rate limiter live in memory) on 0.0.0.0:8000 behind Nginx
Proxy Manager. uvicorn's own logging config is off (`log_config=None`, no access log), so its `uvicorn` and
`uvicorn.error` records reach the root handler `configure_logging("api")` installed: JSON and redacted. The
request log is the app's own `http.request` line.

**Trusted proxies.** uvicorn honours `X-Forwarded-For` only from a peer in `TRADER_FORWARDED_ALLOW_IPS` (a
comma-separated list of IP addresses and CIDR networks), and then takes the rightmost hop that is NOT trusted,
i.e. the address NPM appended, never a first hop the client chose. The default is the external Docker `proxy`
network's subnet on the Docker host (`172.19.0.0/16`, read with `docker network inspect proxy` on
2026-09-27). Docker picked that subnet, it is not pinned, so the deploy (P4-T17 compose, P4-T19 deploy
check) must set the env key if the network is ever recreated. If the default stops matching, nothing is
trusted: every request then looks like it comes from NPM, which fails closed (one shared login rate-limit
bucket), never open.
`*` is refused: it would let any client pick its own address and dodge the per-IP login limit.
"""

import ipaddress
import os

import uvicorn

from trader import logging_setup
from trader.api.main import create_app

FORWARDED_ALLOW_IPS_ENV = "TRADER_FORWARDED_ALLOW_IPS"
DEFAULT_FORWARDED_ALLOW_IPS = "172.19.0.0/16"  # the Docker `proxy` network (NPM), see the module docstring


def forwarded_allow_ips(raw: str | None) -> str:
    """The trusted proxy list for uvicorn: `raw` (or the default when unset or blank), each entry checked to
    be a private IP address or network (a proxy network never is public). Raises ValueError on `*`,
    `0.0.0.0/0` or anything else."""
    value = (raw or "").strip() or DEFAULT_FORWARDED_ALLOW_IPS
    entries = [e.strip() for e in value.split(",") if e.strip()]
    for entry in entries:
        try:
            private = ipaddress.ip_network(entry, strict=False).is_private
        except ValueError:
            private = False
        if not private:
            raise ValueError(
                f"{FORWARDED_ALLOW_IPS_ENV} must list private IP addresses or CIDR networks, not {entry!r}"
            )
    return ",".join(entries)


def main() -> None:
    logging_setup.configure_logging("api")  # first, before anything can log
    trusted = forwarded_allow_ips(os.environ.get(FORWARDED_ALLOW_IPS_ENV))
    uvicorn.run(
        create_app(),
        host="0.0.0.0",  # noqa: S104  # inside the container, reached only through the proxy network
        port=8000,
        log_config=None,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=trusted,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
