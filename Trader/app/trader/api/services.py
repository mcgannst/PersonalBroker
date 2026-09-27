"""Composes `ApiServices` from Core for the API process (P4-T18): the strategy registry, kill switches, the
Questrade token store, the guarded decider, the notifier, cached quotes, 5-minute candles, the job launcher,
the change feed and the day plan.

- **Safety (contract refinement 3):** `decider_for` is `trader.runtime.build_decider`, the same
  `ProposalService.decide` with the kill-switch entry guard that the Telegram bot uses. The API constructs no
  other `ProposalService` (a test scans `trader/api`).
- **Nothing eager on the network:** the Questrade client is a `runtime.LazyQuestrade` (it connects on the
  first quote or candle); the Telegram client is built and entered on `stack` only when Telegram is
  configured (`runtime.open_telegram`). Everything opened is closed with `stack`.
- **Settings (fix round 1):** ONE shared quiet guard (`QuietSettings`) serves the quote-cache TTL, the change
  feed's poll interval and the decider's build-time read. An unusable row falls back to the last good
  settings (the defaults at first) with one log line per failure streak, and writes no event: the worker's
  `GuardedSettings` alerts once per streak, so a web click never writes a relayed `settings` alert. A read
  on the event loop never touches the database: the guard keeps its value for `SETTINGS_MAX_AGE_S` and
  refreshes a stale one in a worker thread (`asyncio.to_thread`) while serving the cached value. A read from
  a worker thread (a sync route) refreshes in place.
- **Decisions on a bad row:** `ProposalService.decide` still reads the store directly and fails closed. The
  API answers that failure with a 503 `settings_unreadable` (the invalid keys are logged, never the values),
  not an unhandled 500.
"""

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import datetime

import structlog
from pydantic import ValidationError

from trader import runtime
from trader.api.deps import ApiServices
from trader.api.errors import ApiError
from trader.api.feed import PollingChangeFeed
from trader.api.launcher import SubprocessJobLauncher
from trader.api.quotes import CachedQuotes
from trader.bootstrap import Core
from trader.engine.killswitch import KillSwitches
from trader.engine.proposals import Decision, DecisionResult, Via
from trader.market.data_service import MarketDataService
from trader.market.types import Candle, Interval
from trader.settings_store import RuntimeSettings
from trader.strategies.registry import StrategyRegistry

log = structlog.get_logger("api.services")

CANDLE_INTERVAL: Interval = "FiveMinutes"  # the position-detail chart (SPEC §12, Trades)
SETTINGS_MAX_AGE_S = 5.0  # a settings change reaches the API's loops within this (monotonic seconds)
SETTINGS_UNREADABLE = "settings_unreadable"

Decide = Callable[[int, Decision, Via, str], DecisionResult]


class QuietSettings:
    """`core.settings.load` that never raises and never does database work on the event loop: the last good
    settings (the defaults at first) on a failed read, logged once per failure streak, no event. The value is
    kept for `max_age` seconds. On the event loop a stale value is refreshed in a worker thread in the
    background (one at a time) and the cached one is returned. Off the loop (a sync route's thread) a stale
    value is refreshed in place. `refresh()` loads now (call it through `asyncio.to_thread` from async code).
    For the API's own reads (the change feed, the quote cache TTL, the decider's build-time read); the
    trading path (`ProposalService.decide`) keeps reading the store directly and fails closed."""

    def __init__(
        self,
        core: Core,
        *,
        max_age: float = SETTINGS_MAX_AGE_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._core = core
        self._max_age = max_age
        self._monotonic = monotonic
        self._current = RuntimeSettings()
        self._last_good: RuntimeSettings | None = None
        self._loaded_at: float | None = None
        self._failing = False
        self._lock = threading.Lock()
        self._refreshing = False
        self._tasks: set[asyncio.Task[None]] = set()

    def _stale(self) -> bool:
        return self._loaded_at is None or self._monotonic() - self._loaded_at >= self._max_age

    def refresh(self) -> RuntimeSettings:
        """Read the store now (sync database work: never call it on the event loop)."""
        try:
            settings = self._core.settings.load()
        except Exception as exc:
            with self._lock:
                if not self._failing:
                    self._failing = True
                    log.warning(
                        "api.settings_unusable",
                        problem=runtime.settings_problem_text(exc),
                        error_type=type(exc).__name__,
                    )
                self._current = self._last_good if self._last_good is not None else RuntimeSettings()
                self._loaded_at = self._monotonic()
                return self._current
        with self._lock:
            if self._failing:
                self._failing = False
                log.info("api.settings_recovered")
            self._last_good = self._current = settings
            self._loaded_at = self._monotonic()
            return settings

    async def _refresh_in_thread(self) -> None:
        try:
            await asyncio.to_thread(self.refresh)
        finally:
            self._refreshing = False

    def __call__(self) -> RuntimeSettings:
        if not self._stale():
            return self._current
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # a worker thread: refreshing here blocks no loop
            return self.refresh()
        if not self._refreshing:
            self._refreshing = True
            task = loop.create_task(self._refresh_in_thread())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return self._current


def guarded_decider(core: Core, settings: Callable[[], RuntimeSettings]) -> Callable[[int], Decide]:
    """`decider_for`: `runtime.build_decider` with the shared quiet settings guard (no relayed alert per
    click). A decision that fails because the stored settings are unusable is a 503 `settings_unreadable`
    (only the invalid keys are logged, never the stored values)."""

    def decider_for(run_id: int) -> Decide:
        decide = runtime.build_decider(core, run_id, settings=settings)

        def guarded(proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
            try:
                return decide(proposal_id, decision, via, actor)
            except ValidationError as exc:
                log.warning("api.decide_settings_unreadable", problem=runtime.settings_problem_text(exc))
                raise ApiError(
                    503,
                    SETTINGS_UNREADABLE,
                    "Settings unreadable: fix the stored settings on the Settings page, then try again",
                ) from None

        return guarded

    return decider_for


async def build_services(core: Core, stack: AsyncExitStack) -> ApiServices:
    """Everything the routers use, built once per API process; whatever it opens is closed with `stack`."""
    factory, clock = core.factory, core.clock
    registry = StrategyRegistry(factory, clock)
    await asyncio.to_thread(registry.ensure_defaults)  # once per process (the day plan needs them)
    settings = QuietSettings(core)
    await asyncio.to_thread(settings.refresh)  # primed off the loop
    data = MarketDataService(factory, clock, core.calendar, runtime.LazyQuestrade(core, stack))

    async def candles(symbol_id: int, start: datetime, end: datetime) -> list[Candle]:
        return await data.candles(symbol_id, start, end, CANDLE_INTERVAL)

    api = await runtime.open_telegram(core, stack)
    return ApiServices(
        core=core,
        registry=registry,
        killswitches=KillSwitches(factory, clock),
        credentials=runtime.questrade_auth(core),
        decider_for=guarded_decider(core, settings),
        notifier=runtime.build_notifier(core, api),
        telegram_configured=runtime.telegram_configured(core.env),
        quotes=CachedQuotes(data.quotes, clock, lambda: float(settings().web_quote_cache_seconds)),
        candles=candles,
        jobs=SubprocessJobLauncher(factory, clock, core.calendar),
        feed=PollingChangeFeed(factory, clock, settings),
        plan=runtime.plan_builder(core),
        fired=runtime.fired_for(core),
    )
