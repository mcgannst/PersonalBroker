"""The API's shared dependencies: the services every router uses (`ApiServices`), the protocols behind them,
the signed-in user, and the run a request reads (SPEC §11, §14).

Routers take what they need through FastAPI dependencies:
- `Services` (the app's `ApiServices`), `CurrentUser` (a valid session, else 401), `CsrfUser` (a valid
  session and the CSRF header, for every POST/PUT/DELETE except login).
- `live_run_id(services)` reads the live run on every call (never cached, so a new live run is seen at
  once); `resolve_run(services, run)` accepts `live` or an existing `runs.id` (a replay run), else 404.

`current_user` and `require_csrf` delegate to `trader.api.auth` (T4); tests override them
(`tests/api/conftest.py`).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal, Protocol

import structlog
from fastapi import Depends, Request

from trader.adapters.questrade.auth import AccessToken, TokenHealth
from trader.api.errors import ApiError
from trader.api.schemas import JobLaunchOut, ManualJob
from trader.bootstrap import Core
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.proposals import Decision, DecisionResult, Via
from trader.engine.runs import get_live_run
from trader.engine.scheduler import DayPlan
from trader.market.types import Candle
from trader.notify.types import Notifier
from trader.notify.views import Quotes
from trader.settings_store import RuntimeSettings
from trader.strategies.registry import StrategyRegistry

log = structlog.get_logger("api.deps")


@dataclass(frozen=True, slots=True)
class AuthUser:
    """The signed-in web user of one request."""

    id: int
    username: str
    session_id: int
    csrf_token: str


def actor(user: AuthUser) -> str:
    """The audit actor of a web action: `web:<username>`."""
    return f"web:{user.username}"


# --- protocols ----------------------------------------------------------------------------------------------


class CredentialStore(Protocol):
    """The Questrade token chain as the API uses it; `QuestradeAuth` satisfies it."""

    def health(self) -> TokenHealth: ...

    def seed(self, refresh_token: str) -> None: ...

    def access(self) -> AccessToken: ...


class JobLauncher(Protocol):
    """Starts a manual run of a CLI job (T9 `SubprocessJobLauncher`)."""

    async def launch(
        self, job: ManualJob, session_date: date | None, force: bool, actor: str
    ) -> JobLaunchOut: ...

    def running(self, job: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class FeedMessage:
    """One change-feed message: `hello`, `invalidate` (data `{"topics": [...]}`) or `events`
    (data `{"items": [EventOut as JSON, ...]}`)."""

    kind: Literal["hello", "invalidate", "events"]
    data: dict[str, Any]


class ChangeFeed(Protocol):
    """Live updates for SSE (T11 `PollingChangeFeed`). `subscribe()` is an async context manager yielding
    the subscriber's message iterator; leaving it always unsubscribes. `run(stop)` polls until `stop` is
    set."""

    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[FeedMessage]]: ...

    async def run(self, stop: asyncio.Event) -> None: ...

    def subscriber_count(self) -> int: ...


# 5-minute candles of one symbol between two UTC datetimes (the position-detail chart's fallback source).
CandleSource = Callable[[int, datetime, datetime], Awaitable[list[Candle]]]


@dataclass(frozen=True, slots=True)
class ApiServices:
    """Everything the routers use, built once per process by `trader.api.services.build_services` (T18)."""

    core: Core
    registry: StrategyRegistry
    killswitches: KillSwitches
    credentials: CredentialStore
    # run id -> ProposalService.decide with the kill-switch entry guard (runtime.build_decider). The ONLY
    # decision path of the API (contract refinement 3).
    decider_for: Callable[[int], Callable[[int, Decision, Via, str], DecisionResult]]
    notifier: Notifier
    telegram_configured: bool
    quotes: Quotes | None
    candles: CandleSource | None
    jobs: JobLauncher
    feed: ChangeFeed
    plan: Callable[[date], DayPlan]
    fired: Callable[[date], set[str]]


# --- dependencies -------------------------------------------------------------------------------------------


def get_services(request: Request) -> ApiServices:
    services = request.app.state.services
    if not isinstance(services, ApiServices):
        raise RuntimeError("app.state.services is not set")
    return services


Services = Annotated[ApiServices, Depends(get_services)]


def current_user(request: Request, services: Services) -> AuthUser:
    """The request's signed-in user (a valid session cookie), else ApiError 401."""
    from trader.api import auth  # auth imports this module

    return auth.authenticate(request, services)


CurrentUser = Annotated[AuthUser, Depends(current_user)]


def require_csrf(request: Request, user: CurrentUser) -> AuthUser:
    """`current_user` plus the CSRF check (header `X-CSRF-Token`, Origin), else ApiError 403."""
    from trader.api import auth

    return auth.check_csrf(request, user)


CsrfUser = Annotated[AuthUser, Depends(require_csrf)]


def _settings(services: ApiServices) -> RuntimeSettings:
    # The live run already exists after the first start; its settings snapshot is only written when it is
    # created. A corrupt settings row must not make every page fail (the Settings page is how it is fixed).
    try:
        return services.core.settings.load()
    except Exception as exc:
        log.warning("api.settings_unusable", error_type=type(exc).__name__)
        return RuntimeSettings()


def live_run_id(services: Services) -> int:
    """The live run's id, read now (created on first use, as every process does)."""
    core = services.core
    return get_live_run(core.factory, core.clock, _settings(services)).id


MAX_ID = 2**63 - 1  # the largest bigint: an id above it can't exist (and would fail in PostgreSQL)


def resolve_run(services: Services, run: str = "live") -> int:
    """`live` (the live run) or the id of an existing run (for example a replay), else 404. Only ASCII
    digits count as an id (`str.isdigit` also accepts "²" and Arabic-Indic digits), bounded to a bigint."""
    if run == "live":
        return live_run_id(services)
    if run.isascii() and run.isdigit() and len(run) <= 19 and 1 <= int(run) <= MAX_ID:
        with services.core.factory() as s:
            if s.get(m.Run, int(run)) is not None:
                return int(run)
    raise ApiError(404, "not_found", "Unknown run")
