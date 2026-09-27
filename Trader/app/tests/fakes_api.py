"""Phase 4 test fakes for the API contracts (P4-T1). No network, no real subprocesses, no real sleeping.

- `test_core(factory, clock, **env)`: a Core on the test database with generated throwaway keys.
- `make_services(core, **overrides)`: an `ApiServices` with the real registry, kill switches and decider
  (wrapped by `RecordingDeciderFor`) and fakes for everything else; override any field by name.
- `FakeCredentialStore`, `FakeJobLauncher`, `FakeFeed`: the `CredentialStore`, `JobLauncher` and `ChangeFeed`
  protocols. `fake_quotes(prices)` and `fake_candles(candles)`: the `Quotes` and `CandleSource` callables.
- `fixed_plan(*events)`: a day-plan builder that returns the same events for every date.
- `FakeReplayLauncher` (P5-T1): the `ReplayLauncher` protocol; records `launch(run_id)`, `running()` is set by
  hand, and `fail_with` makes the next launch raise (a spawn failure). `make_services` uses one by default.
"""

import asyncio
import dataclasses
import secrets
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_telegram import RecordingNotifier
from trader import runtime
from trader.adapters.questrade.auth import AccessToken, TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.api.deps import ApiServices, FeedMessage
from trader.api.schemas import JobLaunchOut, ManualJob
from trader.bootstrap import Core
from trader.config import EnvSettings
from trader.crypto import Crypto
from trader.engine.killswitch import KillSwitches
from trader.engine.proposals import Decision, DecisionResult, Via
from trader.engine.scheduler import DayPlan, PlannedEvent
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import Candle
from trader.notify.views import Quotes
from trader.settings_store import SettingsStore
from trader.strategies.registry import StrategyRegistry

CAL = SessionCalendar()


def test_core(factory: sessionmaker[Session], clock: Clock, **env: Any) -> Core:
    """A Core over the test database. `env` overrides EnvSettings fields (e.g. `telegram_chat_id=42`)."""
    values: dict[str, Any] = {
        "database_url": SecretStr("postgresql+psycopg://unused"),
        "app_encryption_key": SecretStr(Fernet.generate_key().decode()),
        "session_secret": SecretStr(secrets.token_urlsafe(32)),
        "public_base_url": "https://testserver",
        "tz_display": "America/Edmonton",
        "anthropic_api_key": None,
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        **env,
    }
    settings = EnvSettings(_env_file=None, **values)
    return Core(
        env=settings,
        engine=factory.kw["bind"],
        factory=factory,
        crypto=Crypto(settings.app_encryption_key.get_secret_value()),
        clock=clock,
        calendar=CAL,
        settings=SettingsStore(factory, now=clock.now),
    )


test_core.__test__ = False  # type: ignore[attr-defined]  # a helper, not a test (pytest collects test_*)


# --- the decider --------------------------------------------------------------------------------------------


class RecordingDeciderFor:
    """`ApiServices.decider_for` over the REAL `runtime.build_decider` (the kill-switch entry guard included);
    records every call as (run_id, proposal_id, decision, via, actor)."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self.calls: list[tuple[int, int, Decision, Via, str]] = []
        self.run_ids: list[int] = []

    def __call__(self, run_id: int) -> Callable[[int, Decision, Via, str], DecisionResult]:
        self.run_ids.append(run_id)

        def decide(proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
            self.calls.append((run_id, proposal_id, decision, via, actor))
            return runtime.build_decider(self.core, run_id)(proposal_id, decision, via, actor)

        return decide


def recording_decider_for(core: Core) -> RecordingDeciderFor:
    return RecordingDeciderFor(core)


# --- the Questrade token store ------------------------------------------------------------------------------


class FakeCredentialStore:
    """A `CredentialStore`: `health_value` is what `health()` returns; `access_error`, when set, is raised by
    `access()` (e.g. `QuestradeAuthError("The refresh token was rejected")`); `seed()` records the tokens."""

    def __init__(self, health: TokenHealth | None = None) -> None:
        now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
        self.health_value = health or TokenHealth(True, now + timedelta(minutes=25), now, None)
        self.access_error: BaseException | None = None
        self.seeded: list[str] = []
        self.access_calls = 0

    def health(self) -> TokenHealth:
        return self.health_value

    def seed(self, refresh_token: str) -> None:
        self.seeded.append(refresh_token)

    def access(self) -> AccessToken:
        self.access_calls += 1
        if self.access_error is not None:
            raise self.access_error
        return AccessToken(
            "fake-access",
            "https://api01.iq.questrade.com/v1/",
            self.health_value.expires_at or datetime.max.replace(tzinfo=UTC),
        )


# --- quotes and candles -------------------------------------------------------------------------------------


def fake_quotes(prices: Mapping[int, Decimal]) -> Quotes:
    """A `Quotes` callable: a quote (last = the price) for each requested id that has a price."""

    async def quotes(symbol_ids: Sequence[int]) -> Mapping[int, QtQuote]:
        return {
            sid: QtQuote(sid, f"S{sid}", None, None, prices[sid], None, 0, None, 0, False, None)
            for sid in symbol_ids
            if sid in prices
        }

    return quotes


class _FakeCandles:
    def __init__(self, candles: Iterable[Candle]) -> None:
        self.candles = list(candles)
        self.calls: list[tuple[int, datetime, datetime]] = []
        self.error: BaseException | None = None

    async def __call__(self, symbol_id: int, start: datetime, end: datetime) -> list[Candle]:
        self.calls.append((symbol_id, start, end))
        if self.error is not None:
            raise self.error
        return list(self.candles)


def fake_candles(candles: Iterable[Candle]) -> _FakeCandles:
    """A `CandleSource` returning `candles` for any request; records calls; set `.error` to make it raise."""
    return _FakeCandles(candles)


# --- jobs ---------------------------------------------------------------------------------------------------


class FakeJobLauncher:
    """A `JobLauncher` that records launches; add a job name to `running_jobs` to make `running()` true."""

    def __init__(self) -> None:
        self.launches: list[tuple[ManualJob, date | None, bool, str]] = []
        self.running_jobs: set[str] = set()

    async def launch(
        self, job: ManualJob, session_date: date | None, force: bool, actor: str
    ) -> JobLaunchOut:
        self.launches.append((job, session_date, force, actor))
        return JobLaunchOut(job=job, session_date=session_date, launched=True, message=f"{job} started")

    def running(self, job: str) -> bool:
        return job in self.running_jobs


# --- the change feed ----------------------------------------------------------------------------------------


class FakeFeed:
    """A `ChangeFeed` you push messages into. Each subscriber gets every message pushed while it is
    subscribed; messages pushed while nobody is subscribed wait for the next subscriber. `close()` ends
    every current subscriber's iteration. `run(stop)` records its start and stop."""

    def __init__(self) -> None:
        self.pushed: list[FeedMessage] = []
        self.run_started = False
        self.run_stopped = False
        self._queues: list[asyncio.Queue[FeedMessage | None]] = []
        self._backlog: list[FeedMessage] = []

    def push(self, msg: FeedMessage) -> None:
        self.pushed.append(msg)
        if not self._queues:
            self._backlog.append(msg)
        for q in self._queues:
            q.put_nowait(msg)

    def close(self) -> None:
        for q in self._queues:
            q.put_nowait(None)

    @staticmethod
    async def _iterate(q: "asyncio.Queue[FeedMessage | None]") -> AsyncIterator[FeedMessage]:
        while True:
            msg = await q.get()
            if msg is None:
                return
            yield msg

    @asynccontextmanager
    async def _subscription(self) -> AsyncIterator[AsyncIterator[FeedMessage]]:
        q: asyncio.Queue[FeedMessage | None] = asyncio.Queue()
        for msg in self._backlog:
            q.put_nowait(msg)
        self._backlog.clear()
        self._queues.append(q)
        try:
            yield self._iterate(q)
        finally:
            self._queues.remove(q)

    def subscribe(self) -> AbstractAsyncContextManager[AsyncIterator[FeedMessage]]:
        return self._subscription()

    async def run(self, stop: asyncio.Event) -> None:
        self.run_started = True
        try:
            await stop.wait()
        finally:
            self.run_stopped = True

    def subscriber_count(self) -> int:
        return len(self._queues)


# --- replays (P5) -------------------------------------------------------------------------------------------


class FakeReplayLauncher:
    """A `ReplayLauncher` that records the run ids it was asked to launch. Set `is_running` to make
    `running()` true, or `fail_with` to make the next `launch` raise it (then it is cleared)."""

    def __init__(self, *, is_running: bool = False) -> None:
        self.launched: list[int] = []
        self.is_running = is_running
        self.fail_with: BaseException | None = None

    async def launch(self, run_id: int) -> None:
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc
        self.launched.append(run_id)

    def running(self) -> bool:
        return self.is_running


# --- the day plan -------------------------------------------------------------------------------------------


def fixed_plan(*events: PlannedEvent) -> Callable[[date], DayPlan]:
    """A plan builder: the calendar's session times for the date and the same `events` every session day."""

    def plan(d: date) -> DayPlan:
        if not CAL.is_session(d):
            return DayPlan(d, False, None, None, ())
        return DayPlan(
            d,
            True,
            CAL.session_open(d),
            CAL.session_close(d),
            tuple(sorted(events, key=lambda e: (e.at, e.key))),
        )

    return plan


# --- services -----------------------------------------------------------------------------------------------


def make_services(core: Core, **overrides: Any) -> ApiServices:
    """ApiServices for route tests: the real StrategyRegistry and KillSwitches on the test database, the real
    guarded decider (recorded), and fakes for the rest. Any field can be overridden by name."""
    services = ApiServices(
        core=core,
        registry=StrategyRegistry(core.factory, core.clock),
        killswitches=KillSwitches(core.factory, core.clock),
        credentials=FakeCredentialStore(),
        decider_for=recording_decider_for(core),
        notifier=RecordingNotifier(),
        telegram_configured=True,
        quotes=fake_quotes({}),
        candles=fake_candles([]),
        jobs=FakeJobLauncher(),
        feed=FakeFeed(),
        plan=fixed_plan(),
        fired=lambda d: set(),
        replays=FakeReplayLauncher(),
    )
    return dataclasses.replace(services, **overrides)
