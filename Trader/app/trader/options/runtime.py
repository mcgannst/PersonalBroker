"""Composition root of the options simulation (OPTSIM task plan T16): builds the real option services from
`Core`, once per process, for the three kinds of process that use them.

- the options worker (`python -m trader.options.worker`): `build_worker_deps(core, stack)`;
- the cron commands (`trader options-check | options-refresh | options-postclose | options-event`):
  `run_cli_command(name, session_date=..., force=..., **kwargs)`;
- the API process: `build_api_services(core, stack)` gives `ApiServices.options`.

The stock runtime (`trader.runtime`) is imported, never changed: the Questrade token store, the Telegram
client, the notifier, the callback signer, the FinViz cache directory and the log mirror are its builders.

Wiring rules:
- **Questrade pacing (risk R9).** Every options process opens its OWN `QuestradeClient` with
  `market_rps=OPTION_MARKET_RPS` (2 requests per second), so the options side can never push the shared
  Questrade limit during the stock scan. The client connects on its first request.
- **USD/CAD.** `fx.cad_usd_rate` (USD per CAD, the stock setting) inverted, read at use. A stock settings
  row that can't be read falls back to the last good rate (the default at first), with one warning per
  failure streak: the rate is recorded on a fill, it never decides one.
- **The run.** `SimOptionBroker` is built for one run id. `RunBoundBroker` builds it for whatever run
  `run_id()` names at each call, so the API process follows `trader options-run new` without a restart.
  The worker passes the run it started with (it exits with code 4 when the active run changes, T12).
- **Messages.** The notifier sends directly (no relay loop in these processes). Prompt buttons are signed
  with the same key as the stock worker's bot, which is the process that receives the taps (T10).
- **FinViz** is opened on the first snapshot only (the morning refresh), with the stock cache directory.
- **No options run:** everything still builds. The worker idles; the jobs skip; the API answers 409.
"""

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import structlog
import typer

from trader import bootstrap
from trader import runtime as stock
from trader.adapters.finviz.scraper import FinvizScraper
from trader.adapters.questrade.client import QuestradeClient
from trader.adapters.questrade.option_types import OptionQuoteClient
from trader.adapters.questrade.options_check import live_check
from trader.adapters.telegram.callbacks import DbCallbackIssuer
from trader.adapters.telegram.types import CallbackIssuer
from trader.bootstrap import Core
from trader.jobs.options_postclose import LifecycleFactory, PostcloseDeps, lifecycle_factory, postclose_job
from trader.jobs.options_refresh import RefreshDeps, refresh_job
from trader.jobs.runner import JobOutcome
from trader.logging_setup import redact_text
from trader.market.clock import et_date
from trader.market.types import Candle
from trader.notify.types import Notifier
from trader.option_strategies.host import DefaultStrategyHost, NoOptionsRun
from trader.option_strategies.registry import OptionStrategyRegistry
from trader.options.account import active_options_run_id
from trader.options.broker import SimOptionBroker
from trader.options.collateral import DefaultCollateralEngine
from trader.options.facts import FactsService
from trader.options.fill_model import QuoteFillModel
from trader.options.market import OptionMarketService
from trader.options.messages import OptionMessages
from trader.options.prompts import DbPromptStore, PromptSender
from trader.options.protocols import OptionApiServices, OptionBroker
from trader.options.settings import OptionSettingsStore
from trader.options.types import (
    CollateralDecision,
    OptionAccountState,
    OptionFillEvent,
    OptOrderView,
    OrderRequest,
    OrderStatus,
    StructureView,
    SubmitResult,
)
from trader.options.worker import HEARTBEAT_PROCESS, OptionsWorkerDeps, run_event_cli
from trader.settings_store import RuntimeSettings

log = structlog.get_logger("options.runtime")

OPTION_MARKET_RPS = 2.0  # risk R9: the stock processes pace themselves at 17 per second
CRON_PROCESS = "cron"  # the log mirror's process name for the commands, as the stock commands use
RATE_PLACES = Decimal("0.000001")  # opt_fills.usd_cad_rate is numeric(12,6)
MAX_LINE = 1000
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
COMMANDS = ("check", "refresh", "postclose", "event")

RunId = Callable[[], int | None]


# --- the pieces ---------------------------------------------------------------------------------------------


def questrade_client(core: Core) -> AbstractAsyncContextManager[OptionQuoteClient]:
    """This process's own Questrade client for the options side, paced at OPTION_MARKET_RPS (tests replace
    this builder with a fake)."""
    return QuestradeClient(stock.questrade_auth(core), core.clock, market_rps=OPTION_MARKET_RPS)


class LazySnapshots:
    """`FinvizScraper.snapshot`, the scraper opened on the first call (only the morning refresh asks for a
    snapshot; the facts service calls it from a worker thread, one ticker at a time). `close()` closes the
    scraper if one was opened."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._scraper: FinvizScraper | None = None
        self._lock = threading.Lock()

    def _open(self) -> FinvizScraper:
        settings = stock.quiet_settings(self._core)()
        return FinvizScraper(
            min_interval_s=settings.finviz_min_interval_seconds,
            cache_dir=stock.finviz_cache_dir(),
            cache_ttl_s=settings.finviz_cache_hours * 3600,
        )

    def __call__(self, ticker: str, today_et: date) -> dict[str, str]:
        with self._lock:
            if self._scraper is None:
                self._scraper = self._open().__enter__()
            scraper = self._scraper
        return scraper.snapshot(ticker, today_et)

    def close(self) -> None:
        with self._lock:
            scraper, self._scraper = self._scraper, None
        if scraper is not None:
            scraper.__exit__(None, None, None)


def finviz_snapshots(core: Core, stack: AsyncExitStack) -> Callable[[str, date], dict[str, str]]:
    """The FinViz snapshot source of the facts service, closed with `stack` (tests replace this builder)."""
    snapshots = LazySnapshots(core)
    stack.callback(snapshots.close)
    return snapshots


class UsdCadRate:
    """CAD per USD: the stock setting `fx.cad_usd_rate` (USD per CAD) inverted, read at each call. On a
    settings row that can't be read: the last good rate, else the default's, one warning per streak."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._last_good: Decimal | None = None
        self._failing = False

    def __call__(self) -> Decimal:
        try:
            usd_per_cad = self._core.settings.load().fx_cad_usd_rate
        except Exception as exc:
            if not self._failing:
                self._failing = True
                log.warning("options.fx_rate_unreadable", problem=stock.settings_problem_text(exc))
            usd_per_cad = self._last_good or RuntimeSettings().fx_cad_usd_rate
        else:
            self._failing = False
            self._last_good = usd_per_cad
        return (Decimal(1) / usd_per_cad).quantize(RATE_PLACES)


class RunBoundBroker:
    """`OptionBroker` for the options run `run_id()` names at each call: `build(run)` makes the real broker
    the first time a run is seen (the broker itself keeps nothing in memory). Without a run every method
    raises `NoOptionsRun`; callers check for a run first (the API answers 409, the worker idles, the jobs
    skip)."""

    def __init__(self, build: Callable[[int], OptionBroker], run_id: RunId) -> None:
        self._build = build
        self._run_id = run_id
        self._built: tuple[int, OptionBroker] | None = None

    def _broker(self) -> OptionBroker:
        run = self._run_id()
        if run is None:
            raise NoOptionsRun("there is no active options run")
        if self._built is None or self._built[0] != run:
            self._built = (run, self._build(run))
        return self._built[1]

    async def preview(self, req: OrderRequest) -> CollateralDecision:
        return await self._broker().preview(req)

    async def submit(self, req: OrderRequest) -> SubmitResult:
        return await self._broker().submit(req)

    async def cancel(self, order_id: int, reason: str, actor: str) -> bool:
        return await self._broker().cancel(order_id, reason, actor)

    async def reprice(self, order_id: int, net_limit: Decimal, actor: str) -> bool:
        return await self._broker().reprice(order_id, net_limit, actor)

    async def poll(self, now: datetime) -> list[OptionFillEvent]:
        return await self._broker().poll(now)

    async def walk(self, now: datetime) -> int:
        return await self._broker().walk(now)

    async def take_profits(self, now: datetime) -> list[int]:
        return await self._broker().take_profits(now)

    async def expire_day_orders(self, session_date: date, now: datetime) -> int:
        return await self._broker().expire_day_orders(session_date, now)

    async def account(self) -> OptionAccountState:
        return await self._broker().account()

    async def structures(self, *, source: str | None = None, open_only: bool = True) -> list[StructureView]:
        return await self._broker().structures(source=source, open_only=open_only)

    async def orders(
        self, *, status: OrderStatus | None = None, source: str | None = None, limit: int = 200
    ) -> list[OptOrderView]:
        return await self._broker().orders(status=status, source=source, limit=limit)

    async def order(self, order_id: int) -> OptOrderView | None:
        return await self._broker().order(order_id)


class OffLoopOptionMarket(OptionMarketService):
    """The API's option market service: its database steps run in a worker thread, so the API's event loop
    never waits on the database for a chain or a quote; the Questrade calls stay on the loop."""

    async def _db[T](self, step: Callable[..., T], *args: Any) -> T:
        return await asyncio.to_thread(step, *args)


@dataclass(frozen=True)
class OptionsRuntime:
    """Every option service of one process, built by `build_options`."""

    core: Core
    run_id: RunId
    settings: OptionSettingsStore
    client: OptionQuoteClient
    market: OptionMarketService
    facts: FactsService
    broker: OptionBroker
    registry: OptionStrategyRegistry
    host: DefaultStrategyHost
    prompts: DbPromptStore
    sender: PromptSender
    renderer: OptionMessages
    notifier: Notifier
    lifecycle: LifecycleFactory


async def build_options(
    core: Core,
    stack: AsyncExitStack,
    *,
    run_id: RunId | None = None,
    notifier: Notifier | None = None,
    off_loop: bool = False,
) -> OptionsRuntime:
    """Build the option services; whatever is opened is closed with `stack`.

    `run_id` answers the options run to act on (default: the active one, read at each call). `notifier`
    (default: this process's own Telegram notifier, a NullNotifier when Telegram is not configured) sends
    the option messages. `off_loop` runs the market service's database steps in worker threads (the API)."""
    factory, clock, calendar = core.factory, core.clock, core.calendar
    active: RunId = run_id if run_id is not None else (lambda: active_options_run_id(factory))
    settings = OptionSettingsStore(factory, clock.now)
    client = await stack.enter_async_context(questrade_client(core))

    market_class = OffLoopOptionMarket if off_loop else OptionMarketService
    holder: list[OptionMarketService] = []

    async def bars(ticker: str, start: date, end: date) -> list[Candle]:
        # The facts service reads its bars through the same market instance (one fetch, one cache).
        return await holder[0].daily_bars(ticker, start, end)

    facts = FactsService(factory, clock, calendar, client, finviz_snapshots(core, stack), bars, settings.load)
    market = market_class(factory, clock, calendar, client, settings.load, facts)
    holder.append(market)

    rate = UsdCadRate(core)
    collateral = DefaultCollateralEngine()
    fill_model = QuoteFillModel()

    def broker_for(run: int) -> OptionBroker:
        return SimOptionBroker(factory, clock, calendar, market, collateral, fill_model, settings, run, rate)

    broker = RunBoundBroker(broker_for, active)

    if notifier is None:
        notifier = stock.build_notifier(core, await stock.open_telegram(core, stack))
    sending = notifier
    renderer = OptionMessages(core.env.public_base_url)
    prompts = DbPromptStore(factory, clock)
    configured = stock.telegram_configured(core.env)
    issuer: CallbackIssuer = (
        DbCallbackIssuer(factory, clock, stock.build_signer(core)) if configured else stock.NoTelegramIssuer()
    )
    sender = PromptSender(
        prompts,
        sending,
        issuer,
        renderer,
        core.env.telegram_chat_id if configured else None,
        settings.load,
        clock,
    )

    async def alert_sink(source: str, kind: str, message: str, dedupe_key: str) -> None:
        await sending.send(renderer.alert(source, kind, message, dedupe_key))

    registry = OptionStrategyRegistry(factory, clock)
    host = DefaultStrategyHost(
        factory,
        clock,
        calendar,
        registry,
        broker,
        market,
        prompts,
        settings,
        active,
        alert_sink,
        usd_cad_rate=rate,
    )
    return OptionsRuntime(
        core=core,
        run_id=active,
        settings=settings,
        client=client,
        market=market,
        facts=facts,
        broker=broker,
        registry=registry,
        host=host,
        prompts=prompts,
        sender=sender,
        renderer=renderer,
        notifier=sending,
        lifecycle=lifecycle_factory(factory, clock, calendar, market),
    )


# --- the worker ---------------------------------------------------------------------------------------------


async def build_worker_deps(core: Core, stack: AsyncExitStack) -> OptionsWorkerDeps:
    """The options worker's dependencies, bound to the options run that is active now (`run_id` None when
    there is none: the worker then idles and never calls the broker). The process's log mirror
    (`log.options-worker`) is installed here and closed with `stack`."""
    mirror = stock.install_log_mirror(core, HEARTBEAT_PROCESS)
    stack.callback(stock.close_log_mirror, mirror)
    run = await asyncio.to_thread(active_options_run_id, core.factory)
    rt = await build_options(core, stack, run_id=lambda: run)
    return OptionsWorkerDeps(
        factory=core.factory,
        engine=core.engine,
        clock=core.clock,
        calendar=core.calendar,
        settings=rt.settings.load,
        run_id=run,
        broker=rt.broker,
        record_marks=rt.market.record_marks,
        host=rt.host,
        prompt_sender=rt.sender,
        notifier=rt.notifier,
        renderer=rt.renderer,
    )


# --- the jobs -----------------------------------------------------------------------------------------------


def refresh_deps(rt: OptionsRuntime) -> RefreshDeps:
    core = rt.core
    return RefreshDeps(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=rt.settings.load,
        run_id=rt.run_id,
        broker=rt.broker,
        market=rt.market,
        facts=rt.facts,
        host=rt.host,
        notifier=rt.notifier,
        renderer=rt.renderer,
        retry=stock.day_job_retry(stock.quiet_settings(core)()),
    )


def postclose_deps(rt: OptionsRuntime) -> PostcloseDeps:
    core = rt.core
    return PostcloseDeps(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=rt.settings.load,
        run_id=rt.run_id,
        broker=rt.broker,
        market=rt.market,
        host=rt.host,
        registry=rt.registry,
        prompts=rt.prompts,
        prompt_sender=rt.sender,
        notifier=rt.notifier,
        renderer=rt.renderer,
        retry=stock.day_job_retry(stock.quiet_settings(core)()),
    )


# --- the API ------------------------------------------------------------------------------------------------


async def build_api_services(
    core: Core, stack: AsyncExitStack, *, notifier: Notifier | None = None
) -> OptionApiServices | None:
    """`ApiServices.options`. A failure to build is logged and gives None: the option routes then answer
    503 and the stock pages keep working. The plug-ins' first settings rows are written here too, so the
    Strategies tab lists them before the worker's first step."""
    try:
        rt = await build_options(core, stack, notifier=notifier, off_loop=True)
        await asyncio.to_thread(rt.registry.ensure_defaults)
    except Exception as exc:
        log.error(
            "options.api_services_not_built",
            error_type=type(exc).__name__,
            error=redact_text(str(exc))[:MAX_LINE],
        )
        return None
    return OptionApiServices(
        run_id=rt.run_id,
        broker=rt.broker,
        market=rt.market,
        prompts=rt.prompts,
        settings=rt.settings,
        registry=rt.registry,
        host=rt.host,
    )


# --- the command line ---------------------------------------------------------------------------------------


def _masked(text: str) -> str:
    """Any text as one masked line: secrets redacted, whitespace collapsed, length capped."""
    flat = " ".join(redact_text(text).split())
    return flat if len(flat) <= MAX_LINE else flat[: MAX_LINE - 1] + "…"


def _one_line(exc: BaseException) -> str:
    message = str(exc).strip()
    return _masked(f"{type(exc).__name__}: {message}" if message else type(exc).__name__)


def _report(name: str, day: date, out: JobOutcome) -> int:
    """One result line for a job, as the stock commands print it; the exit code."""
    if out.status == "succeeded":
        typer.echo(f"{name} {day}: succeeded {_masked(json.dumps(dict(out.detail), default=str))}")
        return EXIT_OK
    if out.status == "skipped":
        typer.echo(f"{name} {day}: skipped ({_masked(str(out.detail.get('reason', 'no reason given')))})")
        return EXIT_OK
    typer.echo(f"{name} {day}: failed: {_masked(out.error or 'unknown error')}", err=True)
    return EXIT_FAILED


async def _check(core: Core, symbol: str) -> int:
    """`trader options-check`: read-only. Prints the report as JSON; exit 0 only when the chain, a put
    quote and the details all came back (`real_time` says whether that quote was live)."""
    async with questrade_client(core) as client:
        report = await live_check(client, symbol)
    typer.echo(json.dumps(report, indent=2, default=str))
    return EXIT_OK if report.get("ok") else EXIT_FAILED


async def _refresh(rt: OptionsRuntime, day: date, force: bool, kwargs: dict[str, Any]) -> int:
    return _report("options-refresh", day, await refresh_job(refresh_deps(rt), day, force=force))


async def _postclose(rt: OptionsRuntime, day: date, force: bool, kwargs: dict[str, Any]) -> int:
    return _report("options-postclose", day, await postclose_job(postclose_deps(rt), day, force=force))


async def _event(rt: OptionsRuntime, day: date, force: bool, kwargs: dict[str, Any]) -> int:
    return await run_event_cli(
        rt.host,
        strategy=kwargs.get("strategy"),
        key=kwargs.get("key"),
        session_date=day,
        force=force,
        due=bool(kwargs.get("due", False)),
        now=rt.core.clock.now(),
    )


_JOBS: dict[str, Callable[[OptionsRuntime, date, bool, dict[str, Any]], Awaitable[int]]] = {
    "refresh": _refresh,
    "postclose": _postclose,
    "event": _event,
}


def default_session_date(core: Core, name: str, kwargs: dict[str, Any]) -> date:
    """The session a command runs for when no `--date` is given: today's ET date. A NAMED strategy event
    (`options-event <strategy> <key>`, e.g. the Saturday market screen) fired on a day that is not a
    session runs for the latest session on or before today, the date the deploy catch-up tool also pins
    it to. Every other command keeps today and skips by itself on a day that is not a session."""
    today = et_date(core.clock.now())
    if name != "event" or kwargs.get("due") or kwargs.get("strategy") is None:
        return today
    try:
        return today if core.calendar.is_session(today) else core.calendar.previous_session(today)
    except ValueError:  # outside the calendar's range
        return today


async def _command(
    core: Core, name: str, session_date: date | None, force: bool, kwargs: dict[str, Any]
) -> int:
    if name == "check":
        return await _check(core, str(kwargs.get("symbol") or "F").strip().upper())
    day = session_date if session_date is not None else default_session_date(core, name, kwargs)
    async with AsyncExitStack() as stack:
        rt = await build_options(core, stack)
        # A plug-in's first settings row, should neither the worker nor the API have started yet: the jobs
        # read the enabled plug-ins, and one without a row would be skipped with an error event.
        await rt.host.ensure_defaults()
        return await _JOBS[name](rt, day, force, kwargs)


def run_cli_command(
    name: str, *, session_date: date | None = None, force: bool = False, **kwargs: Any
) -> int:
    """The target of `trader options-check | options-refresh | options-postclose | options-event` (the
    declarations are in `trader.options.cli`). `kwargs`: `symbol` for `check`; `strategy`, `key`, `due`
    for `event`. `session_date` None means `default_session_date`. Returns the exit code: 0 done or skipped, 1
    failed, 2 a bad command or event name. A failure is one masked line on stderr, never a traceback."""
    label = f"options-{name}"
    if name not in COMMANDS:
        typer.echo(f"{label}: unknown options command", err=True)
        return EXIT_USAGE
    try:
        core = bootstrap.build_core()
    except Exception as exc:
        typer.echo(f"{label}: failed: {_one_line(exc)}", err=True)
        return EXIT_FAILED
    mirror = stock.install_log_mirror(core, CRON_PROCESS)
    try:
        return asyncio.run(_command(core, name, session_date, force, dict(kwargs)))
    except Exception as exc:
        typer.echo(f"{label}: failed: {_one_line(exc)}", err=True)
        return EXIT_FAILED
    finally:
        stock.close_log_mirror(mirror)


__all__: Sequence[str] = (
    "OPTION_MARKET_RPS",
    "OptionsRuntime",
    "RunBoundBroker",
    "build_api_services",
    "build_options",
    "build_worker_deps",
    "postclose_deps",
    "questrade_client",
    "refresh_deps",
    "run_cli_command",
)
