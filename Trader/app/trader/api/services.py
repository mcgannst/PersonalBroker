"""Composes `ApiServices` from Core for the API process (P4-T18): the strategy registry, kill switches, the
Questrade token store, the guarded decider, the notifier, cached quotes, 5-minute candles, the job launcher,
the change feed and the day plan.

- **Safety (contract refinement 3):** `decider_for` is `trader.runtime.build_decider`, the same
  `ProposalService.decide` with the kill-switch entry guard that the Telegram bot uses. The API constructs no
  other `ProposalService` (a test greps `trader/api`).
- **Nothing eager on the network:** the Questrade client is a `runtime.LazyQuestrade` (it connects on the
  first quote or candle); the Telegram client is built and entered on `stack` only when Telegram is
  configured (`runtime.open_telegram`). Everything opened is closed with `stack`.
- **Settings:** the quote-cache TTL and the change feed read the stored settings on every use through a
  quiet guard: an unusable row falls back to the last good settings (the defaults at first) with one log
  line per failure streak, and writes no event (the worker's `GuardedSettings` already alerts once).
"""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime

import structlog

from trader import runtime
from trader.api.deps import ApiServices
from trader.api.feed import PollingChangeFeed
from trader.api.launcher import SubprocessJobLauncher
from trader.api.quotes import CachedQuotes
from trader.bootstrap import Core
from trader.engine.killswitch import KillSwitches
from trader.market.data_service import MarketDataService
from trader.market.types import Candle, Interval
from trader.settings_store import RuntimeSettings
from trader.strategies.registry import StrategyRegistry

log = structlog.get_logger("api.services")

CANDLE_INTERVAL: Interval = "FiveMinutes"  # the position-detail chart (SPEC §12, Trades)


class QuietSettings:
    """`core.settings.load` that never raises: the last good settings (the defaults at first) on a failed
    read, logged once per failure streak. For the API's own loops (the change feed, the quote cache TTL);
    the trading paths (the decider) keep reading the store directly and fail closed."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._last_good: RuntimeSettings | None = None
        self._failing = False

    def __call__(self) -> RuntimeSettings:
        try:
            settings = self._core.settings.load()
        except Exception as exc:
            if not self._failing:
                self._failing = True
                log.warning("api.settings_unusable", error_type=type(exc).__name__)
            return self._last_good if self._last_good is not None else RuntimeSettings()
        if self._failing:
            self._failing = False
            log.info("api.settings_recovered")
        self._last_good = settings
        return settings


async def build_services(core: Core, stack: AsyncExitStack) -> ApiServices:
    """Everything the routers use, built once per API process; whatever it opens is closed with `stack`."""
    factory, clock = core.factory, core.clock
    registry = StrategyRegistry(factory, clock)
    await asyncio.to_thread(registry.ensure_defaults)  # once per process (the day plan needs them)
    settings = QuietSettings(core)
    data = MarketDataService(factory, clock, core.calendar, runtime.LazyQuestrade(core, stack))

    async def candles(symbol_id: int, start: datetime, end: datetime) -> list[Candle]:
        return await data.candles(symbol_id, start, end, CANDLE_INTERVAL)

    api = await runtime.open_telegram(core, stack)
    return ApiServices(
        core=core,
        registry=registry,
        killswitches=KillSwitches(factory, clock),
        credentials=runtime.questrade_auth(core),
        decider_for=lambda run_id: runtime.build_decider(core, run_id),
        notifier=runtime.build_notifier(core, api),
        telegram_configured=runtime.telegram_configured(core.env),
        quotes=CachedQuotes(data.quotes, clock, lambda: float(settings().web_quote_cache_seconds)),
        candles=candles,
        jobs=SubprocessJobLauncher(factory, clock, core.calendar),
        feed=PollingChangeFeed(factory, clock, settings),
        plan=runtime.plan_builder(core),
        fired=runtime.fired_for(core),
    )
