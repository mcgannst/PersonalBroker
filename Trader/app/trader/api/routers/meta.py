"""GET /api/health -> HealthOut and GET /api/meta -> MetaOut, both public (no session).

- **Health** (SPEC §11, §15: the container health check): the database (`SELECT 1` under a 2 s statement
  timeout), the Questrade token chain (its DB row only: no token value is read or decrypted) and the
  worker's heartbeat. `down` (HTTP 503) when the database check fails; `degraded` (200) when the token or the
  worker is not OK; else `ok`. The body never carries error text.
- **Meta:** the server's time, version and display zone, with the zone's current UTC offset from this
  process's zoneinfo, so the web can detect a browser whose time-zone data disagrees (Key decisions).

Registered under `/api` by `trader.api.routers.ROUTERS`.
"""

import time
import zoneinfo
from pathlib import Path
from typing import Literal

import structlog
import tzdata
from fastapi import APIRouter, Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trader.api import views
from trader.api.deps import Services
from trader.api.schemas import HealthOut, MetaOut, WorkerOut
from trader.notify.views import db_token_health
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("api.meta")

router = APIRouter(tags=["meta"])

DB_TIMEOUT_MS = 2000


def db_check(factory: sessionmaker[Session]) -> int:
    """Milliseconds for `SELECT 1` under a 2 s statement timeout; raises when the database is unusable."""
    t0 = time.perf_counter()
    with factory() as s:
        s.execute(text(f"SET LOCAL statement_timeout = {DB_TIMEOUT_MS}"))
        s.execute(text("SELECT 1")).scalar_one()
    return round((time.perf_counter() - t0) * 1000)


def _settings(services: Services) -> RuntimeSettings:
    try:
        return services.core.settings.load()
    except Exception as exc:
        log.warning("api.health_settings_unusable", error_type=type(exc).__name__)
        return RuntimeSettings()


@router.get("/health")
def health(services: Services, response: Response) -> HealthOut:
    core = services.core
    now = core.clock.now()
    try:
        latency: int | None = db_check(core.factory)
    except Exception as exc:
        log.warning("api.health_db_failed", error_type=type(exc).__name__)
        latency = None
    token = views.token_out(lambda: db_token_health(core.factory), now)
    try:
        worker = views.worker_out(core.factory, now, _settings(services).worker_heartbeat_stale_seconds)
    except Exception as exc:
        log.warning("api.health_worker_failed", error_type=type(exc).__name__)
        worker = WorkerOut(ok=False)
    db_ok = latency is not None
    status: Literal["ok", "degraded", "down"]
    if not db_ok:
        status = "down"
        response.status_code = 503
    elif not (token.ok and worker.ok):
        status = "degraded"
    else:
        status = "ok"
    return HealthOut(
        status=status,
        time=now,
        version=core.env.app_version,
        db_ok=db_ok,
        db_latency_ms=latency,
        token_ok=token.ok,
        token_age_hours=token.age_hours,
        worker_ok=worker.ok,
        worker_phase=worker.phase,
        worker_age_seconds=worker.age_seconds,
    )


def tz_iana_version(zone: str) -> str | None:
    """The pip `tzdata` release when zoneinfo reads `zone` from that package (no system file for it on the
    search path, as in the image where PYTHONTZPATH is empty), else None (the system's data is used)."""
    for directory in zoneinfo.TZPATH:
        try:
            if (Path(directory) / zone).is_file():
                return None
        except (OSError, ValueError):
            continue
    return str(tzdata.IANA_VERSION)


@router.get("/meta")
def meta(services: Services) -> MetaOut:
    env = services.core.env
    now = services.core.clock.now()
    offset = now.astimezone(zoneinfo.ZoneInfo(env.tz_display)).utcoffset()
    return MetaOut(
        server_time=now,
        app_env=env.app_env,
        version=env.app_version,
        tz_display=env.tz_display,
        tz_offset_minutes=int(offset.total_seconds() // 60) if offset is not None else 0,
        tz_iana_version=tz_iana_version(env.tz_display),
        public_base_url=env.public_base_url,
    )
