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
"""

import asyncio
import dataclasses
import functools
import socket
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from trader import bootstrap
from trader.adapters.questrade.auth import QuestradeAuth
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.adapters.telegram.api import PtbTelegramApi
from trader.adapters.telegram.bot import TelegramBot
from trader.adapters.telegram.callbacks import CallbackSigner, DbCallbackIssuer
from trader.adapters.telegram.commands import CommandDeps, Commands
from trader.adapters.telegram.types import CommandHandler, TelegramApi
from trader.bootstrap import Core
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.config import EnvSettings
from trader.db.session import session_scope
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
from trader.jobs.runner import JobOutcome, run_job_async
from trader.logging_setup import redact_text
from trader.market.clock import et_date
from trader.market.data_service import MarketDataService, QuoteClient
from trader.market.types import Candle, Interval
from trader.notify.messages import MessageRenderer
from trader.notify.notifier import NullNotifier, TelegramNotifier
from trader.notify.relay import NotificationRelay
from trader.notify.types import Button, Buttons, Notifier, OutboundMessage
from trader.settings_store import RuntimeSettings
from trader.strategies.base import CatalystSource
from trader.strategies.registry import StrategyRegistry
from trader.worker import Worker, WorkerDeps, WorkerEngine

log = structlog.get_logger("runtime")

SESSION_END_JOB = "session_end"
WORKER_PROCESS = "worker"
TOKEN_SOURCE = "questrade.token"  # noqa: S105 (an event source name: the relay renders it as a token alert)
TEST_BUTTON_DATA = "test"  # unsigned: a running bot answers "Invalid button", as it should
MAX_EVENT_MESSAGE = 500


def finviz_cache_dir() -> Path:
    # A private per-user cache (the scraper creates it 0o700 and refuses one it doesn't own).
    return Path.home() / ".cache" / "trader" / "finviz"


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


async def open_telegram(core: Core, stack: AsyncExitStack) -> TelegramApi | None:
    """The Telegram API client, closed with `stack`; None when Telegram isn't configured."""
    if not telegram_configured(core.env):
        return None
    api = build_telegram_api(core.env)
    stack.push_async_callback(_aclose_quietly, api)
    if isinstance(api, PtbTelegramApi):
        await api.__aenter__()
    return api


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


def build_decider(core: Core, run_id: int) -> Callable[[int, Decision, Via, str], DecisionResult]:
    """ProposalService.decide over its own SimBroker, so a tap works whether or not a session engine is
    open (the row lock in `decide` keeps it consistent with the engine's own service). SAFETY: built with
    the kill-switch entry guard, as `build_engine` is, so a tapped entry is refused while a kill switch or
    /pause is active; stops, exits and cancels go through."""
    settings = core.settings.load()
    killswitches = KillSwitches(core.factory, core.clock)
    service = ProposalService(
        core.factory,
        core.clock,
        core.settings,
        build_sim_broker(core, run_id, settings),
        run_id,
        entry_blocked=killswitches.entry_guard(),
    )
    return service.decide


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
        claude = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value(), timeout=30, max_retries=1)
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


class SessionEngines:
    """The worker's engines: one per session (rebuilt so settings changes apply, P2-T13); the previous
    session's stack is closed before the next session's engine opens. All share `client`."""

    def __init__(self, core: Core, client: QuoteClient) -> None:
        self._core = core
        self._client = client
        self._stack: AsyncExitStack | None = None
        self._engine: Engine | None = None
        self._day: date | None = None
        self._lock = asyncio.Lock()

    async def engine_for(self, day: date) -> Engine:
        async with self._lock:
            if self._engine is not None and self._day == day:
                return self._engine
            await self._close()
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


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    return exc.code if isinstance(exc.code, int) else 1


async def run_worker(once: bool = False) -> int:
    """Build everything from `build_core()` and run the worker (`--once`: one step and one relay pump, no
    bot). Returns the exit code: 0, 2 when another worker holds the lock, 3 when this one lost it.
    The single-instance lock is taken by `Worker.run`, never here."""
    core = bootstrap.build_core()
    factory, clock = core.factory, core.clock
    settings = core.settings.load
    run = get_live_run(factory, clock, settings())
    StrategyRegistry(factory, clock).ensure_defaults()
    async with AsyncExitStack() as stack:
        client = LazyQuestrade(core, stack)
        engines = SessionEngines(core, client)
        stack.push_async_callback(engines.aclose)
        fdeps = fire_deps(core, engines.current)
        fired = fired_for(core)
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
                build_decider(core, run.id),
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

        async def end_session(
            session_date: date, body: Callable[[], Awaitable[dict[str, Any]]]
        ) -> JobOutcome:
            return await run_job_async(factory, clock, SESSION_END_JOB, session_date, body)

        worker = Worker(
            WorkerDeps(
                factory=factory,
                clock=clock,
                calendar=core.calendar,
                settings=settings,
                engine_for=engines.engine_for,
                plan=fdeps.plan,
                fire=bind_fire(fdeps),
                fired=fired,
                relay=relay,
                bot=bot_run,
                end_session=end_session,
                process=WORKER_PROCESS,
                host=socket.gethostname(),
            )
        )
        try:
            await worker.run(asyncio.Event(), once=once)
        except SystemExit as exc:  # 2: another worker runs; 3: this one lost its lock
            return _exit_code(exc)
    return 0


# --- cron jobs ----------------------------------------------------------------------------------------------


async def run_cli_job(
    core: Core,
    job: str,
    session_date: date,
    body: Callable[[], Awaitable[dict[str, Any]]],
    *,
    force: bool,
) -> JobOutcome:
    """A cron job through `run_job_async` (advisory lock, skip when already succeeded unless `force`)."""
    return await run_job_async(core.factory, core.clock, job, session_date, body, force=force)


async def preopen_job(core: Core, session_date: date, *, force: bool) -> JobOutcome:
    """The 09:20 pre-open check (sent directly, so it arrives even when the worker is down)."""
    async with AsyncExitStack() as stack:
        client = LazyQuestrade(core, stack)
        api = await open_telegram(core, stack)
        auth = questrade_auth(core)
        run = get_live_run(core.factory, core.clock, core.settings.load())

        async def token_check() -> None:
            await asyncio.to_thread(auth.access)

        deps = PreopenDeps(
            factory=core.factory,
            clock=core.clock,
            calendar=core.calendar,
            settings=core.settings.load,
            token_check=token_check,
            universe_status=MarketDataService(
                core.factory, core.clock, core.calendar, client
            ).universe_status,
            killswitches=KillSwitches(core.factory, core.clock),
            run_id=run.id,
            notifier=build_notifier(core, api),
            render=build_renderer(core),
        )
        return await run_cli_job(
            core, "preopen", session_date, lambda: run_preopen(deps, session_date), force=force
        )


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
        run = get_live_run(core.factory, core.clock, core.settings.load())
        deps = CheckinDeps(
            factory=core.factory,
            clock=core.clock,
            calendar=core.calendar,
            settings=core.settings.load,
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
    """The 16:15 post-close job. The engine (and its Questrade client) is built only if the job body runs."""
    async with AsyncExitStack() as stack:
        client = LazyQuestrade(core, stack)
        api = await open_telegram(core, stack)
        run = get_live_run(core.factory, core.clock, core.settings.load())
        engine: WorkerEngine = LazyEngine(core, stack, client)
        deps = PostcloseDeps(
            factory=core.factory,
            clock=core.clock,
            calendar=core.calendar,
            settings=core.settings.load,
            engine=engine,
            data=MarketDataService(core.factory, core.clock, core.calendar, client),
            notifier=build_notifier(core, api),
            render=build_renderer(core),
            issuer=DbCallbackIssuer(core.factory, core.clock, build_signer(core)),
            chat_id=core.env.telegram_chat_id or 0,
            run_id=run.id,
        )
        return await run_cli_job(
            core, "postclose", session_date, lambda: run_postclose(deps, session_date), force=force
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
    text = redact_text(str(error))[:MAX_EVENT_MESSAGE]
    try:
        with session_scope(core.factory) as s:
            log_event(
                s,
                core.clock,
                "error",
                TOKEN_SOURCE,
                f"Questrade token refresh failed: {text}",
                {"error": text, "error_type": type(error).__name__},
            )
    except Exception as exc:
        log.error("runtime.token_failure_not_recorded", error_type=type(exc).__name__)


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
        api = build_telegram_api(env)
        stack.push_async_callback(_aclose_quietly, api)
        if isinstance(api, PtbTelegramApi):
            await api.__aenter__()
        return await api.send_message(chat_id, msg.text, msg.buttons, msg.silent)
