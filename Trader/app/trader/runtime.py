"""Composition root for Phase 3: builds the notifier, renderer, callback signer, decider, session engine,
event firing and the worker from Core (SPEC §1, §9, §13), and the async bodies of the cron commands.

Processes talk only through PostgreSQL. The worker (`run_worker`) owns the Telegram bot and the relay; the
cron commands (`preopen_job`, `checkin_job`, `event_backup`, `postclose_job`) send their own messages
through the same Notifier. Everything that would call Questrade or build an engine does so lazily, so a
skipped backup or a holiday costs no Questrade call.

Safety wiring (P3-T12 orchestrator notes):
- The bot's decider is a ProposalService with `entry_blocked=KillSwitches.entry_guard()`, exactly as in
  `build_engine`: an entry approved by a tap while a kill switch is tripped (or /pause is on) is never
  submitted; exits, stops and cancels are never guarded.
- `run_worker` never takes the single-instance lock itself: `Worker.run` does (a second
  pg_try_advisory_lock from this process would be refused and the worker would exit 2).
- Every day plan (the worker's, the check-ins', the event backups') comes from `scheduler.live_day_plan`
  (enabled strategies plus exits-only disabled owners, BR-42), and `report_plan_problems` runs whenever a
  plan is built (it dedupes per session).

Questrade clients: the worker opens ONE lazily-connected client for the whole process and shares it with
every session's engine and with the /positions quotes (P2 `build_engine`: one client per process, so the
access-token cache is shared); each session's engine stack (the catalyst service's FinViz scraper and
Claude client) is closed before the next session's opens. A cron command opens one client, lazily, on its
own stack.

Fix round 1 (P3-T12 gauntlet):
- Settings read at startup and by the message paths (the worker's own loops, the bot, the relay, the
  commands, the pre-open, check-in and post-close jobs) go through `GuardedSettings`: an invalid or
  unreadable settings row falls back to the last good settings (the defaults at startup), writes ONE
  `error` event (source `settings`, relayed) per failure streak, and the process carries on. The trading
  paths (day plans, event firing, the engine, decisions) keep reading the store directly and so keep
  failing closed: no event fires on settings nobody chose.
- A new live run needs a worker restart: the bot, the relay and the commands are bound to the run id they
  were built with. `LiveRunWatch` re-reads the active live run at each session change and at most every
  LIVE_RUN_CHECK_SECONDS otherwise; when it changed, the worker fires nothing more, stops cleanly and
  `run_worker` returns EXIT_LIVE_RUN_CHANGED (4), so supervisord restarts it on the new run.

Phase 5 (P5-T17):
- Log mirror: every process installs `install_log_mirror` (worker, API, each CLI command, the replay process):
  its `error`/`critical` log lines are mirrored to `event_log` as `log.<process>` (never relayed). It is
  optional wiring: a failure to install is one warning line and the process carries on. The worker closes
  it after its shutdown (the relay's last pump included).
- Retries: the day-level jobs (`preopen_job`, `postclose_job`, `weekly_job`, and the `nightly` and
  `premarket` commands) run with `RetryPolicy.from_settings(...)`; the pre-open's retries stop at 09:28 ET
  (`PREOPEN_RETRY_DEADLINE`) so they never run into the open. The check-ins and event backups pass no
  policy: session events have their own retries (30/60/120 s).
- `weekly_job`: the Saturday weekly report (`trader weekly`), its commentary written through an
  `AsyncAnthropic` client (timeout 30 s, one retry, as the catalyst classifier) only when
  ANTHROPIC_API_KEY is set.

Phase 6 (P6-T11), the decision log (logging only, D2): the worker runs `DecisionsLoop` as its own task
(`decisions_loop`); the post-close runs the final pass (`decisions_final_pass`: record final, prune, the day's
summary for the "Decisions" line); the CLI calls `record_decisions_quietly` after a succeeded pre-market job
and after `trader event` fired an event. None of them can fail or change a job: every failure is one masked
warning, and all their database work runs in worker threads.
"""

import asyncio
import dataclasses
import functools
import os
import socket
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader import bootstrap
from trader.adapters.claude.reports import CommentaryWriter
from trader.adapters.questrade.auth import QuestradeAuth
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.adapters.telegram.api import PtbTelegramApi
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer
from trader.adapters.telegram.commands import CommandDeps, Commands
from trader.adapters.telegram.types import CallbackKind, CommandHandler, TelegramApi
from trader.bootstrap import Core
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.config import EnvSettings
from trader.db import models as m
from trader.db.session import session_scope
from trader.decisions.loop import DecisionsLoop, EventWriter, FinalPass, final_pass
from trader.decisions.recorder import LiveScanData, record_day
from trader.decisions.types import SOURCE as DECISIONS_SOURCE
from trader.decisions.types import RecorderDeps
from trader.engine.killswitch import KillSwitches
from trader.engine.orchestrator import Engine, build_engine
from trader.engine.proposals import Decision, DecisionResult, ProposalService, Via
from trader.engine.runs import get_live_run
from trader.engine.scheduler import (
    DayPlan,
    EventRunner,
    FireDeps,
    FireResult,
    fire_event,
    fired_keys,
    live_day_plan,
    report_plan_problems,
)
from trader.events import log_event
from trader.jobs.checkin import CheckinDeps, run_checkin
from trader.jobs.events import run_event_backup
from trader.jobs.postclose import PostcloseDeps, run_postclose
from trader.jobs.preopen import PreopenDeps, run_preopen
from trader.jobs.runner import JobOutcome, RetryPolicy, run_job_async
from trader.jobs.weekly import WeeklyDeps, run_weekly
from trader.logging_mirror import EventLogMirror, install_event_mirror
from trader.logging_setup import redact_text
from trader.market.clock import ET, et_date
from trader.market.data_service import MarketDataService, QuoteClient
from trader.market.types import Candle, Interval
from trader.notify.messages import MessageRenderer
from trader.notify.notifier import NullNotifier, TelegramNotifier
from trader.notify.relay import NotificationRelay
from trader.notify.types import Button, Buttons, Check, Notifier, OutboundMessage, PreopenView
from trader.notify.views import WORKER_PROCESS as WORKER_PROCESS  # re-exported (one definition, in views)
from trader.reports.weekly import WeekWindow
from trader.settings_store import RuntimeSettings
from trader.strategies.base import CatalystSource
from trader.strategies.registry import StrategyRegistry
from trader.worker import Worker, WorkerDeps, WorkerEngine

log = structlog.get_logger("runtime")

SESSION_END_JOB = "session_end"
TOKEN_SOURCE = "questrade.token"  # noqa: S105 (an event source name: the relay renders it as a token alert)
SETTINGS_SOURCE = "settings"
TEST_BUTTON_DATA = "test"  # unsigned: a running bot answers "Invalid button", as it should
MAX_EVENT_MESSAGE = 500
EXIT_LIVE_RUN_CHANGED = 4  # run_worker: the live run changed; supervisord restarts the worker on the new one
LIVE_RUN_CHECK_SECONDS = 60.0  # the worker re-reads the live run at least this often (clock seconds)
FINVIZ_CACHE_ENV = "TRADER_FINVIZ_CACHE_DIR"
# The retries of a day-level job never wait past these ET times of its session: the pre-market scan's
# stop before the 09:20 pre-open check starts, the pre-open's before the open (P5-T15 fix round 1).
PREMARKET_RETRY_DEADLINE = time(9, 18)
PREOPEN_RETRY_DEADLINE = time(9, 28)
CLAUDE_TIMEOUT_S = 30  # the Claude clients (catalysts, weekly commentary): a hung call fails fast
CLAUDE_MAX_RETRIES = 1


def finviz_cache_dir() -> Path:
    """The FinViz cache of every process (the nightly and pre-market commands and the session engines):
    $TRADER_FINVIZ_CACHE_DIR, else ~/.cache/trader/finviz. A private per-user cache (the scraper creates it
    0o700 and refuses one it doesn't own), never a shared /tmp path."""
    configured = os.environ.get(FINVIZ_CACHE_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".cache" / "trader" / "finviz"


def exit_code(exc: SystemExit) -> int:
    """A SystemExit as a process exit code (None is 0, a non-int is 1). Used by run_worker and
    `trader.worker.main`."""
    if exc.code is None:
        return 0
    return exc.code if isinstance(exc.code, int) else 1


# --- settings -----------------------------------------------------------------------------------------------


def settings_problem_text(exc: BaseException) -> str:
    """Why the stored settings can't be used: the invalid keys (never their values), else the error type."""
    if isinstance(exc, ValidationError):
        keys = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        return f"invalid stored settings ({', '.join(keys) or 'unknown key'})"
    return f"settings could not be read ({type(exc).__name__})"


class GuardedSettings:
    """`core.settings.load` for the paths that must keep going on a bad settings row (startup, messages,
    the worker's own loops): on a failed read, the last good settings, else the defaults, and ONE `error`
    event (source `settings`, relayed as an alert) per failure streak. `problem` says what is wrong while
    the streak lasts."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._last_good: RuntimeSettings | None = None
        self.problem: str | None = None

    def __call__(self) -> RuntimeSettings:
        try:
            settings = self._core.settings.load()
        except Exception as exc:
            if self.problem is None:
                self.problem = settings_problem_text(exc)
                fallback = "the last good settings" if self._last_good is not None else "the defaults"
                log.error("runtime.settings_unusable", problem=self.problem)
                _record_event(
                    self._core,
                    "error",
                    SETTINGS_SOURCE,
                    f"Runtime settings unusable: {self.problem}. Using {fallback} for status and messages; "
                    "session events wait for valid settings. Fix the row on the Settings page.",
                    {"problem": self.problem, "error_type": type(exc).__name__},
                )
            return self._last_good if self._last_good is not None else RuntimeSettings()
        if self.problem is not None:
            log.info("runtime.settings_recovered")
            self.problem = None
        self._last_good = settings
        return settings


def quiet_settings(core: Core) -> Callable[[], RuntimeSettings]:
    """`core.settings.load`, or silently the defaults when the stored row can't be used: no line and no event,
    because the command's own settings read (or the worker's GuardedSettings) is the one that reports it."""

    def load() -> RuntimeSettings:
        try:
            return core.settings.load()
        except Exception:
            return RuntimeSettings()

    return load


# --- the log mirror (P5-T17) --------------------------------------------------------------------------------


def install_log_mirror(
    core: Core,
    process: str,
    *,
    run_id: int | None = None,
    settings: Callable[[], RuntimeSettings] | None = None,
) -> EventLogMirror | None:
    """Mirror this process's `error`/`critical` log lines to `event_log` as `log.<process>` (T14), stamped
    with `run_id` (the replay process's own run; None elsewhere). The mirror uses `core.clock`, the wall
    clock, never a replay clock. None when `logging.mirror_level` is `off`. Optional wiring: it never
    raises; a failure is one warning line and the process carries on. `settings` defaults to
    `quiet_settings(core)` (defaults on an unusable row, no event); the worker passes its GuardedSettings."""
    try:
        loaded = (settings or quiet_settings(core))()
        return install_event_mirror(core.factory, core.clock, process, loaded, run_id=run_id)
    except Exception as exc:
        log.warning("runtime.log_mirror_not_installed", process=process, error_type=type(exc).__name__)
        return None


def close_log_mirror(mirror: EventLogMirror | None) -> None:
    """Flush and remove a mirror (bounded by its flush timeout). Never raises; None is a no-op."""
    if mirror is None:
        return
    try:
        mirror.close()
    except Exception as exc:
        log.warning("runtime.log_mirror_close_failed", error_type=type(exc).__name__)


def _record_event(
    core: Core, level: str, source: str, message: str, data: dict[str, Any], run_id: int | None = None
) -> None:
    """Write one event_log row. Never raises (a failure is one log line)."""
    try:
        with session_scope(core.factory) as s:
            log_event(s, core.clock, level, source, message[:MAX_EVENT_MESSAGE], data, run_id=run_id)
    except Exception as exc:
        log.error("runtime.event_not_recorded", source=source, error_type=type(exc).__name__)


# --- Telegram -----------------------------------------------------------------------------------------------


def telegram_configured(env: EnvSettings) -> bool:
    return (
        getattr(env, "telegram_bot_token", None) is not None
        and getattr(env, "telegram_chat_id", None) is not None
    )


def build_telegram_api(env: EnvSettings) -> TelegramApi:
    """The PTB-backed API client (tests replace this builder with a fake). Needs a configured bot."""
    if env.telegram_bot_token is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return PtbTelegramApi(env.telegram_bot_token)


async def _aclose_quietly(api: TelegramApi) -> None:
    try:
        await api.aclose()
    except Exception as exc:  # the type only: a Telegram client error can carry the token in a URL
        log.warning("runtime.telegram_close_failed", error_type=type(exc).__name__)


async def _enter_telegram_api(env: EnvSettings, stack: AsyncExitStack) -> TelegramApi:
    """Build the API client, register its (quiet) close on `stack`, and enter it when it is the PTB one."""
    api = build_telegram_api(env)
    stack.push_async_callback(_aclose_quietly, api)
    if isinstance(api, PtbTelegramApi):
        await api.__aenter__()
    return api


async def open_telegram(core: Core, stack: AsyncExitStack) -> TelegramApi | None:
    """The Telegram API client, closed with `stack`; None when Telegram isn't configured."""
    if not telegram_configured(core.env):
        return None
    return await _enter_telegram_api(core.env, stack)


def build_notifier(core: Core, api: TelegramApi | None = None) -> Notifier:
    """TelegramNotifier when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, else NullNotifier. Pass the
    `api` from `open_telegram` so it is closed with its stack; without one a new client is built."""
    chat_id = core.env.telegram_chat_id
    if not telegram_configured(core.env) or chat_id is None:
        return NullNotifier()
    return TelegramNotifier(api or build_telegram_api(core.env), chat_id, core.factory, core.clock)


def build_renderer(core: Core) -> MessageRenderer:
    return MessageRenderer(core.env.public_base_url, ZoneInfo(core.env.tz_display), clock=core.clock)


def build_signer(core: Core) -> CallbackSigner:
    return CallbackSigner.derive(core.env.session_secret.get_secret_value())


# --- the bot's decider --------------------------------------------------------------------------------------


def build_sim_broker(core: Core, run_id: int, settings: RuntimeSettings) -> SimBroker:
    """The live run's SimBroker, built exactly as `build_engine` builds it (no Questrade client: submitting
    and cancelling only touch the database, and SimBroker keeps no state in memory)."""
    return SimBroker(
        core.factory,
        core.clock,
        Ledger(core.calendar),
        QuoteFillModel(FillParams.from_settings(settings)),
        run_id,
        currency=settings.account_currency,
        calendar=core.calendar,
        settings=core.settings.load,
    )


def build_decider(
    core: Core, run_id: int, *, settings: Callable[[], RuntimeSettings] | None = None
) -> Callable[[int, Decision, Via, str], DecisionResult]:
    """ProposalService.decide over its own SimBroker, so a tap works whether or not a session engine is
    open (the row lock in `decide` keeps it consistent with the engine's own service). SAFETY: built with
    the kill-switch entry guard, as `build_engine` is, so a tapped entry is refused while a kill switch or
    /pause is active; stops, exits and cancels go through. The build-time settings read (fill parameters,
    currency) is guarded: `settings` (default: a new GuardedSettings) falls back to the defaults."""
    loaded = (settings or GuardedSettings(core))()
    killswitches = KillSwitches(core.factory, core.clock)
    service = ProposalService(
        core.factory,
        core.clock,
        core.settings,
        build_sim_broker(core, run_id, loaded),
        run_id,
        entry_blocked=killswitches.entry_guard(),
    )
    return service.decide


class SettingsCheckRenderer(MessageRenderer):
    """The MessageRenderer whose pre-open message also lists a failed `settings` check while `problem()`
    reports one (the pre-open job's deps have no other place for it)."""

    def __init__(self, core: Core, problem: Callable[[], str | None]) -> None:
        super().__init__(core.env.public_base_url, ZoneInfo(core.env.tz_display), clock=core.clock)
        self._problem = problem

    def preopen(self, v: PreopenView) -> OutboundMessage:
        problem = self._problem()
        if problem is not None:
            v = dataclasses.replace(v, checks=(*v.checks, settings_check(problem)))
        return super().preopen(v)


def settings_check(problem: str) -> Check:
    return Check(SETTINGS_SOURCE, False, "error", f"{problem}: using the defaults")


class NoTelegramIssuer:
    """The CallbackIssuer of a process without Telegram: nothing can be tapped, so no nonce is stored."""

    def issue(
        self,
        kind: CallbackKind,
        ref: str,
        actions: Sequence[str],
        chat_id: int,
        ttl_seconds: int | None,
    ) -> tuple[str, dict[str, str]]:
        return "", dict.fromkeys(actions, "")

    def bind(self, nonce: str, message_id: int) -> None:
        return None


# --- Questrade and the engine -------------------------------------------------------------------------------


def questrade_auth(core: Core) -> QuestradeAuth:
    return QuestradeAuth(core.factory, core.crypto, core.clock)


def questrade_client(core: Core) -> AbstractAsyncContextManager[QuoteClient]:
    """A Questrade market-data client as an async context manager (tests replace this with a fake)."""
    return QuestradeClient(questrade_auth(core), core.clock)


class LazyQuestrade:
    """A QuoteClient that opens its Questrade client on first use and enters it on `stack`, so code that
    never needs Questrade (a skipped backup, a holiday) never connects."""

    def __init__(self, core: Core, stack: AsyncExitStack) -> None:
        self._core = core
        self._stack = stack
        self._client: QuoteClient | None = None
        self._lock = asyncio.Lock()

    @property
    def opened(self) -> bool:
        return self._client is not None

    def rate_limit_remaining(self) -> dict[str, int] | None:
        """The open client's remaining Questrade requests per category (`market_data`, `account`), as its
        last responses reported them; None until the client is open (P4-T18: the worker's heartbeat)."""
        if self._client is None:
            return None
        remaining = getattr(self._client, "rate_limit_remaining", None) or {}
        return {str(category): int(n) for category, n in dict(remaining).items()}

    async def client(self) -> QuoteClient:
        async with self._lock:
            if self._client is None:
                self._client = await self._stack.enter_async_context(questrade_client(self._core))
            return self._client

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        return await (await self.client()).quotes(ids)

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        return await (await self.client()).candles(symbol_id, start, end, interval)

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        return await (await self.client()).candles_many(reqs)


async def open_catalysts(core: Core, stack: AsyncExitStack) -> CatalystSource:
    """The catalyst service (FinViz headlines, Claude when ANTHROPIC_API_KEY is set), closed with `stack`."""
    import anthropic

    from trader.adapters.claude.catalyst import CatalystClassifier, CatalystService, CatalystStore
    from trader.adapters.finviz.scraper import FinvizScraper

    settings = core.settings.load()
    finviz = stack.enter_context(
        FinvizScraper(
            min_interval_s=settings.finviz_min_interval_seconds,
            cache_dir=finviz_cache_dir(),
            cache_ttl_s=settings.finviz_cache_hours * 3600,
        )
    )
    classifier = None
    api_key = core.env.anthropic_api_key
    if api_key is not None:
        claude = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(), timeout=CLAUDE_TIMEOUT_S, max_retries=CLAUDE_MAX_RETRIES
        )
        stack.push_async_callback(claude.close)
        classifier = CatalystClassifier(claude, core.settings.load)
    return CatalystService(
        core.factory, core.clock, CatalystStore(core.factory, core.clock), classifier, finviz
    )


async def open_engine(core: Core, stack: AsyncExitStack, *, client: QuoteClient | None = None) -> Engine:
    """A session engine: its catalyst service is entered on `stack`, and so is its Questrade client unless
    a shared `client` is given (the worker shares one per process). The client connects lazily."""
    quote_client = client if client is not None else LazyQuestrade(core, stack)
    return build_engine(core, quote_client, await open_catalysts(core, stack))


class LazyEngine:
    """A WorkerEngine / EventRunner that opens the engine (on `stack`) the first time it is used, so a
    skipped backup or an early exit never builds one."""

    def __init__(self, core: Core, stack: AsyncExitStack, client: QuoteClient) -> None:
        self._core = core
        self._stack = stack
        self._client = client
        self._engine: Engine | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Engine:
        async with self._lock:
            if self._engine is None:
                self._engine = await open_engine(self._core, self._stack, client=self._client)
            return self._engine

    async def run_event(self, event_key: str, session_date: date) -> Any:
        return await (await self.get()).run_event(event_key, session_date)

    async def poll_quotes(self) -> Sequence[Any]:
        return await (await self.get()).poll_quotes()

    async def tick(self, now: datetime) -> None:
        await (await self.get()).tick(now)

    async def end_of_session(self, session_date: date) -> Sequence[Any]:
        return await (await self.get()).end_of_session(session_date)


def active_live_run_id(factory: sessionmaker[Session]) -> int | None:
    """The id of the active live run, read without creating one (get_live_run would)."""
    with factory() as s:
        run_id: int | None = s.execute(
            select(m.Run.id).where(m.Run.mode == "live", m.Run.status == "active")
        ).scalar_one_or_none()
    return run_id


class LiveRunWatch:
    """Notices that the active live run is no longer the one the worker was built for (the bot, the relay
    and the commands are bound to a run id, so following a new run needs a restart). `check` re-reads the
    run when forced (each session change) and at most every LIVE_RUN_CHECK_SECONDS otherwise; on a change
    it writes one `warning` event and sets `stop`, and `changed` stays True."""

    def __init__(self, core: Core, run_id: int, stop: asyncio.Event) -> None:
        self._core = core
        self.run_id = run_id
        self._stop = stop
        self._checked_at = core.clock.now()  # run_worker has just read it
        self.changed = False
        self.current: int | None = run_id

    def check(self, *, force: bool = False) -> bool:
        """True while the worker's run is still the active live run (and when it can't be read)."""
        if self.changed:
            return False
        now = self._core.clock.now()
        if not force and (now - self._checked_at).total_seconds() < LIVE_RUN_CHECK_SECONDS:
            return True
        self._checked_at = now
        try:
            current = active_live_run_id(self._core.factory)
        except Exception as exc:  # the database is down: every other part reports that
            log.warning("runtime.live_run_check_failed", error_type=type(exc).__name__)
            return True
        if current == self.run_id:
            return True
        self.changed, self.current = True, current
        log.warning("worker.live_run_changed", run_id=self.run_id, current=current)
        _record_event(
            self._core,
            "warning",
            "worker",
            f"The live run changed (worker built for run {self.run_id}, now {current}); "
            "the worker stops and is restarted on the new run.",
            {"run_id": self.run_id, "current": current},
        )
        self._stop.set()
        return False


class _IdleEngine:
    """The engine handed out once the live run changed: it does nothing while the worker stops."""

    async def run_event(self, event_key: str, session_date: date) -> Any:
        return None

    async def poll_quotes(self) -> Sequence[Any]:
        return []

    async def tick(self, now: datetime) -> None:
        return None

    async def end_of_session(self, session_date: date) -> Sequence[Any]:
        return []


class SessionEngines:
    """The worker's engines: one per session (rebuilt so settings changes apply, P2-T13); the previous
    session's stack is closed before the next session's engine opens. All share `client`. With a `watch`,
    a session change first re-reads the live run: when it changed, no engine is built (an idle one is
    returned while the worker stops)."""

    def __init__(self, core: Core, client: QuoteClient, *, watch: LiveRunWatch | None = None) -> None:
        self._core = core
        self._client = client
        self._watch = watch
        self._stack: AsyncExitStack | None = None
        self._engine: Engine | None = None
        self._day: date | None = None
        self._lock = asyncio.Lock()

    async def engine_for(self, day: date) -> Engine:
        async with self._lock:
            if self._engine is not None and self._day == day:
                return self._engine
            await self._close()
            if self._watch is not None and not self._watch.check(force=True):
                return _IdleEngine()  # type: ignore[return-value]
            stack = AsyncExitStack()
            try:
                engine = await open_engine(self._core, stack, client=self._client)
            except BaseException:
                await stack.aclose()
                raise
            self._stack, self._engine, self._day = stack, engine, day
            return engine

    async def current(self) -> EventRunner:
        """The engine of today's ET session (the runner `fire_event` builds lazily)."""
        return await self.engine_for(et_date(self._core.clock.now()))

    async def _close(self) -> None:
        stack, self._stack, self._engine, self._day = self._stack, None, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                log.warning("runtime.session_close_failed", error_type=type(exc).__name__)

    async def aclose(self) -> None:
        async with self._lock:
            await self._close()


# --- plans and event firing ---------------------------------------------------------------------------------


def plan_builder(core: Core) -> Callable[[date], DayPlan]:
    """The day plan of every process: `live_day_plan` (enabled strategies plus the safety events of disabled
    strategies that still own positions or working orders, BR-42), with its problems reported once per
    session (`report_plan_problems` dedupes). The strategy defaults are ensured once per builder, so a plan
    on a fresh database already knows the default strategies."""
    ensured = False

    def plan(session_date: date) -> DayPlan:
        nonlocal ensured
        settings = core.settings.load()
        run = get_live_run(core.factory, core.clock, settings)
        registry = StrategyRegistry(core.factory, core.clock)
        if not ensured:
            registry.ensure_defaults()
            ensured = True
        day = live_day_plan(core.factory, registry, run.id, core.calendar, session_date, settings)
        report_plan_problems(core.factory, core.clock, day)
        return day

    return plan


def fired_for(core: Core) -> Callable[[date], set[str]]:
    return functools.partial(fired_keys, core.factory)


def fire_deps(core: Core, runner_factory: Callable[[], Awaitable[EventRunner]]) -> FireDeps:
    return FireDeps(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=core.settings.load,
        plan=plan_builder(core),
        runner=runner_factory,
    )


def bind_fire(deps: FireDeps, *, force: bool = False) -> Callable[[str, date], Awaitable[FireResult]]:
    """`fire_event` bound to its deps and to `force` (the `fire` callables of the jobs take no force)."""

    async def fire(key: str, session_date: date) -> FireResult:
        return await fire_event(deps, key, session_date, force=force)

    return fire


# --- the worker ---------------------------------------------------------------------------------------------


class ForwardingCommands:
    """Breaks the bot <-> commands cycle: the bot is built with this handler, whose `target` is set once
    `Commands` (which needs the bot as its ProposalMessenger) exists."""

    def __init__(self) -> None:
        self.target: CommandHandler | None = None

    async def handle(self, text: str) -> list[OutboundMessage]:
        if self.target is None:
            return []
        return await self.target.handle(text)

    async def confirm_pause(self, action: str) -> str:
        if self.target is None:
            return "Not ready, try again."
        return await self.target.confirm_pause(action)


async def run_worker(once: bool = False) -> int:
    """Build everything from `build_core()` and run the worker (`--once`: one step and one relay pump, no
    bot). Returns the exit code: 0; 2 when another worker holds the lock; 3 when this one lost it; 4
    (EXIT_LIVE_RUN_CHANGED) when the live run changed under it (a restart builds everything on the new
    run). The single-instance lock is taken by `Worker.run`, never here. An invalid settings row does not
    stop it: GuardedSettings falls back to the defaults with one relayed `error` event. The log mirror
    (process `worker`) is installed first and closed last."""
    core = bootstrap.build_core()
    settings = GuardedSettings(core)
    mirror = install_log_mirror(core, WORKER_PROCESS, settings=settings)
    try:
        return await _run_worker(core, settings, once)
    finally:
        # After the worker's shutdown (the relay's last pump) and its stack's closing, so their error lines
        # are mirrored too; off the event loop (the close flushes, bounded by its timeout).
        await asyncio.to_thread(close_log_mirror, mirror)


async def _run_worker(core: Core, settings: GuardedSettings, once: bool) -> int:
    factory, clock = core.factory, core.clock
    run = get_live_run(factory, clock, settings())
    StrategyRegistry(factory, clock).ensure_defaults()
    stop = asyncio.Event()
    watch = LiveRunWatch(core, run.id, stop)
    async with AsyncExitStack() as stack:
        # The shared Questrade client lives on its own inner stack, entered first, so at exit the last
        # session's engine stack closes before the client it uses.
        client = LazyQuestrade(core, await stack.enter_async_context(AsyncExitStack()))
        engines = SessionEngines(core, client, watch=watch)
        stack.push_async_callback(engines.aclose)
        fdeps = fire_deps(core, engines.current)
        settled = fired_for(core)

        def fired(session_date: date) -> set[str]:
            # Read by every step in and after the session: the throttled live-run check rides on it.
            watch.check()
            return settled(session_date)

        data = MarketDataService(factory, clock, core.calendar, client)
        api = await open_telegram(core, stack)
        chat_id = core.env.telegram_chat_id
        relay: Callable[[], Awaitable[Any]] | None = None
        bot_run: Callable[[asyncio.Event], Awaitable[None]] | None = None
        if api is not None and chat_id is not None:
            render = build_renderer(core)
            signer = build_signer(core)
            issuer = DbCallbackIssuer(factory, clock, signer)
            forward = ForwardingCommands()
            bot = TelegramBot(
                api,
                chat_id,
                factory,
                clock,
                issuer,
                signer,
                build_decider(core, run.id, settings=settings),
                forward,
                render,
                run.id,
                settings=settings,
            )
            forward.target = Commands(
                CommandDeps(
                    factory=factory,
                    clock=clock,
                    calendar=core.calendar,
                    settings=settings,
                    killswitches=KillSwitches(factory, clock),
                    run_id=run.id,
                    chat_id=chat_id,
                    plan=fdeps.plan,
                    fired=fired,
                    token_health=questrade_auth(core).health,
                    quotes=data.quotes,
                    messenger=bot,
                    issuer=issuer,
                    render=render,
                )
            )
            notifier = TelegramNotifier(api, chat_id, factory, clock)
            relay = NotificationRelay(factory, clock, notifier, render, bot, run.id, settings=settings).pump
            bot_run = bot.run
        else:
            log.warning("worker.telegram_not_configured")
            _record_event(
                core,
                "warning",
                "worker",
                "Telegram not configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): the worker runs "
                "without the bot and the relay, so no approvals or alerts reach Telegram.",
                {},
            )
        fire_now = bind_fire(fdeps)

        async def fire(key: str, session_date: date) -> FireResult:
            if not watch.check():
                return FireResult(key, session_date, "skipped", {"reason": "live run changed; restarting"})
            return await fire_now(key, session_date)

        async def end_session(
            session_date: date, body: Callable[[], Awaitable[dict[str, Any]]]
        ) -> JobOutcome:
            if not watch.check(force=True):  # the restarted worker ends the session on the new run
                return JobOutcome("skipped", {"reason": "live run changed; restarting"})
            return await run_job_async(factory, clock, SESSION_END_JOB, session_date, body)

        def heartbeat_extra() -> dict[str, Any]:
            # The Questrade rate-limit numbers the System page shows (P4-T18), once the client is open.
            rate_limit = client.rate_limit_remaining()
            return {"rate_limit": rate_limit} if rate_limit is not None else {}

        run_id = run.id

        worker = Worker(
            WorkerDeps(
                factory=factory,
                clock=clock,
                calendar=core.calendar,
                settings=settings,
                engine_for=engines.engine_for,
                plan=fdeps.plan,
                fire=fire,
                fired=fired,
                relay=relay,
                bot=bot_run,
                end_session=end_session,
                process=WORKER_PROCESS,
                host=socket.gethostname(),
                heartbeat_extra=heartbeat_extra,
                decisions=decisions_loop(core, lambda: run_id),
            )
        )
        try:
            await worker.run(stop, once=once)
        except SystemExit as exc:  # 2: another worker runs; 3: this one lost its lock
            return exit_code(exc)
    return EXIT_LIVE_RUN_CHANGED if watch.changed else 0


# --- the decision log (P6-T11) ------------------------------------------------------------------------------


def recorder_deps(core: Core) -> RecorderDeps:
    """The live recorder's deps: the database-only scan data, the core clock and calendar, and
    `quiet_settings` (an unusable settings row gives the defaults, no event: the worker's GuardedSettings
    reports it)."""
    return RecorderDeps(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=quiet_settings(core),
        scan_data=LiveScanData(core.factory, core.calendar),
    )


def decisions_event_writer(core: Core) -> EventWriter:
    """The decision log's warning/info events: one `event_log` row, source `decisions` (levels `warning` and
    `info` are never relayed). Never raises."""

    def write(level: str, message: str, data: dict[str, Any], run_id: int | None) -> None:
        _record_event(core, level, DECISIONS_SOURCE, message, data, run_id)

    return write


def decisions_loop(core: Core, run_id: Callable[[], int | None]) -> DecisionsLoop:
    """The worker's decision log loop for its live run."""
    return DecisionsLoop(recorder_deps(core), run_id, event=decisions_event_writer(core))


def decisions_final_pass(core: Core, run_id: int) -> Callable[[date], Awaitable[FinalPass]]:
    """The post-close's final pass for `run_id` (record final, prune, the day's summary)."""

    async def run(session_date: date) -> FinalPass:
        return await final_pass(recorder_deps(core), run_id, session_date)

    return run


async def record_decisions_quietly(core: Core, session_date: date, *, final: bool = False) -> None:
    """One decision log pass for the active live run, best effort (the CLI calls it after a succeeded
    pre-market job, once its brief was handed off, and after `trader event` when an event fired). Never
    raises: a failure is one masked log line and one `warning` event (source `decisions`, never relayed).
    Every database step runs in a worker thread. No live run yet: nothing to record."""
    run_id: int | None = None
    try:
        run_id = await asyncio.to_thread(active_live_run_id, core.factory)
        if run_id is None:
            return
        await record_day(recorder_deps(core), run_id, session_date, final=final)
    except Exception as exc:
        reason = " ".join(redact_text(f"{type(exc).__name__}: {exc}").split())[:300]
        log.warning("runtime.decisions_not_recorded", session_date=session_date.isoformat(), error=reason)
        await asyncio.to_thread(
            _record_event,
            core,
            "warning",
            DECISIONS_SOURCE,
            f"decision log pass for {session_date.isoformat()} failed: {reason}",
            {"session_date": session_date.isoformat(), "error_type": type(exc).__name__},
            run_id,
        )


# --- cron jobs ----------------------------------------------------------------------------------------------


async def run_cli_job(
    core: Core,
    job: str,
    session_date: date,
    body: Callable[[], Awaitable[dict[str, Any]]],
    *,
    force: bool,
    retry: RetryPolicy | None = None,
) -> JobOutcome:
    """A cron job through `run_job_async` (advisory lock, skip when already succeeded unless `force`), with
    the day-level jobs' in-process retries when `retry` is given (SPEC §9)."""
    return await run_job_async(core.factory, core.clock, job, session_date, body, force=force, retry=retry)


def retry_deadline(session_date: date, at: time) -> datetime:
    """`at` (ET wall clock) on `session_date`, as the aware deadline of a `RetryPolicy`."""
    return datetime.combine(session_date, at, tzinfo=ET)


def day_job_retry(settings: RuntimeSettings) -> RetryPolicy:
    """The in-process retries of a day-level job without a deadline (nightly, postclose, weekly)."""
    return RetryPolicy.from_settings(settings)


def premarket_retry(settings: RuntimeSettings, session_date: date) -> RetryPolicy:
    """The pre-market scan's policy: its retries stop before the 09:20 pre-open check (09:18 ET)."""
    return RetryPolicy.from_settings(
        settings, deadline=retry_deadline(session_date, PREMARKET_RETRY_DEADLINE)
    )


async def preopen_job(core: Core, session_date: date, *, force: bool) -> JobOutcome:
    """The 09:20 pre-open check (sent directly, so it arrives even when the worker is down). An unusable
    settings row never stops it: the defaults are used, one `error` event is written, and the message and
    the job detail carry a failed `settings` check."""
    async with AsyncExitStack() as stack:
        client = LazyQuestrade(core, stack)
        api = await open_telegram(core, stack)
        auth = questrade_auth(core)
        settings = GuardedSettings(core)
        run = get_live_run(core.factory, core.clock, settings())

        async def token_check() -> None:
            await asyncio.to_thread(auth.access)

        deps = PreopenDeps(
            factory=core.factory,
            clock=core.clock,
            calendar=core.calendar,
            settings=settings,
            token_check=token_check,
            universe_status=MarketDataService(
                core.factory, core.clock, core.calendar, client
            ).universe_status,
            killswitches=KillSwitches(core.factory, core.clock),
            run_id=run.id,
            notifier=build_notifier(core, api),
            render=SettingsCheckRenderer(core, lambda: settings.problem),
        )

        async def body() -> dict[str, Any]:
            detail = await run_preopen(deps, session_date)
            if settings.problem is not None and "checks" in detail:
                detail["checks"].append(dataclasses.asdict(settings_check(settings.problem)))
                detail["ok"] = False
            return detail

        retry = RetryPolicy.from_settings(
            settings(), deadline=retry_deadline(session_date, PREOPEN_RETRY_DEADLINE)
        )
        return await run_cli_job(core, "preopen", session_date, body, force=force, retry=retry)


def checkin_job_name(at_label: str) -> str:
    return f"checkin@{at_label}"


async def checkin_job(core: Core, session_date: date, at_label: str, *, force: bool) -> JobOutcome:
    """An 11:30 / 13:30 check-in. Its backup `fire` never takes `--force` (that is a job re-run, not a
    licence to re-run events that already succeeded)."""
    async with AsyncExitStack() as stack:
        client = LazyQuestrade(core, stack)
        engine = LazyEngine(core, stack, client)
        api = await open_telegram(core, stack)
        fdeps = fire_deps(core, engine.get)
        settings = GuardedSettings(
            core
        )  # the status message; the backup `fire` reads the store (fails closed)
        run = get_live_run(core.factory, core.clock, settings())
        deps = CheckinDeps(
            factory=core.factory,
            clock=core.clock,
            calendar=core.calendar,
            settings=settings,
            run_id=run.id,
            notifier=build_notifier(core, api),
            render=build_renderer(core),
            plan=fdeps.plan,
            fired=fired_for(core),
            fire=bind_fire(fdeps),
            quotes=MarketDataService(core.factory, core.clock, core.calendar, client).quotes,
            token_health=questrade_auth(core).health,
            killswitches=KillSwitches(core.factory, core.clock),
        )
        return await run_cli_job(
            core,
            checkin_job_name(at_label),
            session_date,
            lambda: run_checkin(deps, session_date, at_label),
            force=force,
        )


class ForcedDueRejected(ValueError):
    """`event --due --force` would re-run every event that already succeeded today."""


async def event_backup(
    core: Core, key: str | None, session_date: date, *, due: bool = False, force: bool = False
) -> list[FireResult]:
    """`trader event KEY [--force]` / `trader event --due`: the cron backup of the worker's events. `force`
    is bound into `fire` (fire_event's own force) for a single key only."""
    if due and force:
        raise ForcedDueRejected("event --due --force is refused: it would re-run events that already ran")
    async with AsyncExitStack() as stack:
        engine = LazyEngine(core, stack, LazyQuestrade(core, stack))
        fdeps = fire_deps(core, engine.get)
        return await run_event_backup(
            bind_fire(fdeps, force=force),
            core.calendar,
            core.clock,
            key,
            session_date,
            due=due,
            plan=fdeps.plan,
            fired=fired_for(core),
        )


async def postclose_job(core: Core, session_date: date, *, force: bool) -> JobOutcome:
    """The 16:15 post-close job. The engine (and its Questrade client) is built only if the job body runs.
    Without Telegram nothing can be tapped, so no journal nonce is issued (NoTelegramIssuer)."""
    async with AsyncExitStack() as stack:
        client = LazyQuestrade(core, stack)
        api = await open_telegram(core, stack)
        settings = GuardedSettings(core)
        run = get_live_run(core.factory, core.clock, settings())
        engine: WorkerEngine = LazyEngine(core, stack, client)
        issuer = (
            DbCallbackIssuer(core.factory, core.clock, build_signer(core))
            if api is not None
            else NoTelegramIssuer()
        )
        deps = PostcloseDeps(
            factory=core.factory,
            clock=core.clock,
            calendar=core.calendar,
            settings=settings,
            engine=engine,
            data=MarketDataService(core.factory, core.clock, core.calendar, client),
            notifier=build_notifier(core, api),
            render=build_renderer(core),
            issuer=issuer,
            chat_id=core.env.telegram_chat_id or 0,
            run_id=run.id,
            decisions=decisions_final_pass(core, run.id),
        )
        return await run_cli_job(
            core,
            "postclose",
            session_date,
            lambda: run_postclose(deps, session_date),
            force=force,
            retry=RetryPolicy.from_settings(settings()),
        )


def claude_client_class() -> type[Any]:
    """The Anthropic async client class (tests replace this builder with a fake)."""
    import anthropic

    return anthropic.AsyncAnthropic


async def weekly_job(core: Core, week: WeekWindow, *, force: bool) -> JobOutcome:
    """The Saturday 09:00 weekly report (BR-61, SPEC §9): keyed by the week's last session date. The
    commentary's Claude client is built (timeout 30 s, one SDK retry, as the catalyst classifier's) only when
    ANTHROPIC_API_KEY is set; without it the report goes out with a note. The caller has checked that the
    week had a session."""
    if week.week_ending is None:
        raise ValueError("weekly_job needs a week with a session")
    week_ending = week.week_ending
    async with AsyncExitStack() as stack:
        api = await open_telegram(core, stack)
        settings = GuardedSettings(core)
        run = get_live_run(core.factory, core.clock, settings())
        writer: CommentaryWriter | None = None
        api_key = core.env.anthropic_api_key
        if api_key is not None:
            claude = claude_client_class()(
                api_key=api_key.get_secret_value(), timeout=CLAUDE_TIMEOUT_S, max_retries=CLAUDE_MAX_RETRIES
            )
            stack.push_async_callback(claude.close)
            writer = CommentaryWriter(claude, settings)
        deps = WeeklyDeps(
            factory=core.factory,
            clock=core.clock,
            calendar=core.calendar,
            settings=settings,
            writer=writer,
            notifier=build_notifier(core, api),
            render=build_renderer(core),
            run_id=run.id,
        )
        return await run_cli_job(
            core,
            "weekly",
            week_ending,
            lambda: run_weekly(deps, week),
            force=force,
            retry=RetryPolicy.from_settings(settings()),
        )


async def send_premarket_brief(core: Core, session_date: date, brief: str) -> None:
    """Send the pre-market brief once per session (dedupe `premarket:<date>`). Never raises. Without
    Telegram there is nobody to send it to (the brief is printed and stored in the job detail)."""
    if not telegram_configured(core.env):
        return
    try:
        async with AsyncExitStack() as stack:
            api = await open_telegram(core, stack)
            msg = build_renderer(core).premarket_brief(session_date, brief)
            await build_notifier(core, api).send(
                dataclasses.replace(msg, dedupe_key=f"premarket:{session_date.isoformat()}")
            )
    except Exception as exc:
        log.error("runtime.premarket_brief_not_sent", error_type=type(exc).__name__)


def record_token_failure(core: Core, error: BaseException) -> None:
    """An `error` event (source questrade.token) the relay turns into a token-failure alert. Never raises."""
    text = " ".join(redact_text(str(error)).split())[:MAX_EVENT_MESSAGE] or type(error).__name__
    _record_event(
        core,
        "error",
        TOKEN_SOURCE,
        f"Questrade token refresh failed: {text}",
        {"error": text, "error_type": type(error).__name__},
    )


# --- telegram-test ------------------------------------------------------------------------------------------


def telegram_test_message(now_label: str, *, buttons: bool) -> OutboundMessage:
    lines = [
        "<b>Trader test message (dev)</b>",
        f"Sent by <code>trader telegram-test</code> at {now_label}. No action needed.",
    ]
    rows: Buttons = ()
    if buttons:
        lines.append("The buttons below are a test: a running worker answers them with Invalid button.")
        rows = ((Button("Test A", TEST_BUTTON_DATA), Button("Test B", TEST_BUTTON_DATA)),)
    return OutboundMessage(kind="reply", text="\n".join(lines), buttons=rows)


async def telegram_test(env: EnvSettings, now_label: str, *, buttons: bool = False) -> int:
    """Send one clearly labelled test message through the configured bot; returns its message id. It only
    sends (never long-polls: a worker may be polling)."""
    chat_id = env.telegram_chat_id
    if not telegram_configured(env) or chat_id is None:
        raise RuntimeError("Telegram isn't configured")
    msg = telegram_test_message(now_label, buttons=buttons)
    async with AsyncExitStack() as stack:
        api = await _enter_telegram_api(env, stack)
        return await api.send_message(chat_id, msg.text, msg.buttons, msg.silent)
