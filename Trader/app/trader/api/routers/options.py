"""The Options page's routes, all under `/api/options` (OPTSIM task plan §3.9, T14).

Everything other than a few reads comes from `ApiServices.options` (`OptionApiServices`): the broker, the
market view, the prompts, the option settings, the strategy registry and the strategy host.

- Every route needs a signed-in session; every POST and PUT needs the CSRF header. The audit actor of a
  change is `web:<username>`.
- `ApiServices.options` is None (not wired, or it failed to build): 503 `unavailable`.
- No active options run: 409 `no_options_run`, except `/settings` and `/strategies` (GET and PUT).
- An order from the ticket has source `manual`. Submitting never fills: the options worker does. A
  collateral rejection is not an HTTP error: the order comes back with status `rejected`. Cancel and
  reprice act only on a working `manual` order (409 otherwise).
- The API process never trades for a strategy: a panel action goes to the plug-in's `on_action` through
  the host, which returns a message and no orders.
- A 4xx never echoes a submitted value.

This router is registered in `trader.api.routers.ROUTERS` by the wiring task (T16).

The async routes await the broker, the market view and the host on the event loop and run their own
database reads in a worker thread; the routes that only use sync services are plain functions (FastAPI
runs those in its thread pool).
"""

from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

import anyio.to_thread
import httpx
import structlog
from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import AwareDatetime, ValidationError

from trader.adapters.questrade.auth import QuestradeAuthError
from trader.adapters.questrade.client import QuestradeApiError
from trader.api import options_views as v
from trader.api.deps import MAX_ID, ApiServices, CsrfUser, Services, actor, current_user
from trader.api.errors import ApiError
from trader.api.routers.strategies import safe_field_message
from trader.api.schemas import (
    Items,
    OptAccountOut,
    OptActivityOut,
    OptBenchmarkOut,
    OptChainOut,
    OptChainQuotesOut,
    OptOrderIn,
    OptOrderOut,
    OptPanelActionIn,
    OptPanelActionResultOut,
    OptPanelOut,
    OptPreviewOut,
    OptPromptAnswerIn,
    OptPromptOut,
    OptRepriceIn,
    OptStrategyOut,
    OptStructureOut,
    SettingIn,
    SettingOut,
    SettingsOut,
    StrategyIn,
)
from trader.market.clock import et_date
from trader.option_strategies.base import PanelActionRequest
from trader.options.protocols import OptionApiServices, UnknownContract, UnknownUnderlying
from trader.options.settings import OptionSettings
from trader.options.types import (
    SOURCE_MANUAL,
    OptionAccountState,
    OptionContract,
    OptionQuote,
    OptOrderView,
    OrderLeg,
    OrderRequest,
    OrderStatus,
    StructureView,
)

log = structlog.get_logger("api.options")

# Every route needs a signed-in session (SPEC §14); the writes also take `CsrfUser`.
router = APIRouter(prefix="/options", tags=["options"], dependencies=[Depends(current_user)])

MANUAL_REASON = "manual"  # opt_orders.reason of an order from the ticket
CANCEL_REASON = "cancelled from the web"
CLOSED_STATUSES: tuple[OrderStatus, ...] = ("filled", "cancelled", "expired", "rejected")
CHAIN_CACHE_ATTR = "options_chain_cache"  # app.state: (underlying, expiry) -> ChainSnapshot
# What a market read can fail with when Questrade can't be reached (503; an optional read goes without).
UPSTREAM_ERRORS = (QuestradeApiError, QuestradeAuthError, httpx.HTTPError, TimeoutError, OSError)

Limit = Annotated[int, Query(ge=1, le=500)]
Underlying = Annotated[str, Query(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")]
OrderId = Annotated[int, Path(ge=1, le=MAX_ID)]
PromptId = Annotated[int, Path(ge=1, le=MAX_ID)]
Key = Annotated[str, Path(min_length=1, max_length=60)]


# --- dependencies -------------------------------------------------------------------------------------------


def option_services(services: Services) -> OptionApiServices:
    if services.options is None:
        raise ApiError(503, "unavailable", "Options are not available")
    return services.options


Options = Annotated[OptionApiServices, Depends(option_services)]


def options_run_id(options: Options) -> int:
    """The active options run, read now; 409 `no_options_run` when there is none."""
    run_id = options.run_id()
    if run_id is None:
        raise ApiError(409, "no_options_run", "There is no active options run")
    return run_id


RunId = Annotated[int, Depends(options_run_id)]


# --- helpers ------------------------------------------------------------------------------------------------


def _not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"Unknown {what}")


def _invalid(field: str, msg: str) -> ApiError:
    return ApiError(422, "validation", "Invalid request", [{"loc": ["body", field], "msg": msg}])


@contextmanager
def market_errors() -> Iterator[None]:
    """An unknown underlying or contract is a 404; Questrade out of reach is a 503."""
    try:
        yield
    except UnknownUnderlying:
        raise _not_found("underlying") from None
    except UnknownContract:
        raise _not_found("contract") from None
    except UPSTREAM_ERRORS as exc:
        log.warning("api.options_market_unavailable", error_type=type(exc).__name__)
        raise ApiError(503, "unavailable", "Market data is not available right now") from None


def load_settings(options: OptionApiServices) -> OptionSettings:
    """The option settings; the defaults while a stored row is invalid (the Settings tab repairs it)."""
    try:
        return options.settings.load()
    except ValidationError:
        log.warning("api.options_settings_unusable")
        return OptionSettings()


async def load_marks(options: OptionApiServices, structures: Iterable[StructureView]) -> v.Marks:
    """Quotes for the open option positions and last prices for the open share positions. Marks are
    optional: a failed read leaves them out (the position then shows no mark)."""
    live = [p for s in structures if s.state == "open" for p in v.open_positions(s)]
    contract_ids = sorted({p.contract.id for p in live if p.contract is not None})
    tickers = sorted({p.underlying for p in live if p.contract is None})
    quotes: dict[int, OptionQuote] = {}
    shares = {}
    try:
        if contract_ids:
            quotes = await options.market.quotes(contract_ids)
        for ticker in tickers:
            share = await options.market.underlying_quote(ticker)
            if share is not None and share.last is not None:
                shares[ticker] = share.last
    except (LookupError, *UPSTREAM_ERRORS) as exc:
        log.warning("api.options_marks_failed", error_type=type(exc).__name__)
    return v.Marks(quotes, shares)


async def _orders_out(
    services: ApiServices, options: OptionApiServices, orders: list[OptOrderView]
) -> list[OptOrderOut]:
    contracts: dict[int, OptionContract] = {}
    for contract_id in sorted({leg.contract_id for o in orders for leg in o.legs if leg.contract_id}):
        try:
            contracts[contract_id] = await options.market.contract(contract_id)
        except UnknownContract:
            continue  # the leg is shown without its contract
    filled = [o.id for o in orders if o.status == "filled"]
    fills = await anyio.to_thread.run_sync(v.leg_fills_blocking, services.core.factory, filled)
    today = et_date(services.core.clock.now())
    return [v.order_out(o, contracts, fills, today) for o in orders]


def order_request(body: OptOrderIn, submitted_by: str) -> OrderRequest:
    """The ticket as a `manual` OrderRequest, or 422 for a ticket that names no clear order. Everything
    else (leg count, quantity, cover, cash) is the collateral engine's verdict."""
    for n, leg in enumerate(body.legs):
        if (leg.instrument == "option") != (leg.contract_id is not None):
            raise ApiError(
                422,
                "validation",
                "Invalid request",
                [
                    {
                        "loc": ["body", "legs", n, "contract_id"],
                        "msg": "An option leg names a contract; a shares leg never does",
                    }
                ],
            )
    if body.order_type == "market" and body.net_limit is not None:
        raise _invalid("net_limit", "A market order has no limit")
    if body.order_type == "market" and body.walk:
        raise _invalid("walk", "Only a limit order can walk")
    if body.order_type == "limit" and body.net_limit is None and not body.walk:
        raise _invalid("net_limit", "A limit order needs a limit, or walk")
    if body.intent != "open" and body.structure_id is None:
        raise _invalid("structure_id", "A close or roll names the structure it acts on")
    return OrderRequest(
        source=SOURCE_MANUAL,
        strategy_config_id=None,
        intent=body.intent,
        structure_id=body.structure_id,
        underlying=body.underlying,
        legs=tuple(
            OrderLeg(n, leg.instrument, leg.side, leg.effect, leg.ratio, body.underlying, leg.contract_id)
            for n, leg in enumerate(body.legs, start=1)
        ),
        qty=body.qty,
        order_type=body.order_type,
        net_limit=body.net_limit,
        tif=body.tif,
        walk=body.walk,
        take_profit_pct=None,
        reason=MANUAL_REASON,
        evidence={},
        submitted_by=submitted_by,
    )


# --- account and positions ----------------------------------------------------------------------------------


async def _benchmark(
    options: OptionApiServices,
    settings: OptionSettings,
    facts: v.RunFacts,
    state: OptionAccountState,
    today: date,
) -> OptBenchmarkOut | None:
    if facts.started_at is None:
        return None
    since = et_date(facts.started_at)
    try:
        bars = await options.market.daily_bars(settings.benchmark_ticker, since, today)
    except (LookupError, *UPSTREAM_ERRORS) as exc:  # the comparison is optional: show it without numbers
        log.warning("api.options_benchmark_failed", error_type=type(exc).__name__)
        bars = []
    return v.benchmark_out(
        settings.benchmark_ticker, since, bars, equity=state.equity, starting_cash=facts.starting_cash
    )


@router.get("/account")
async def get_account(services: Services, options: Options, run_id: RunId) -> OptAccountOut:
    today = et_date(services.core.clock.now())
    state = await options.broker.account()
    structures = await options.broker.structures(open_only=False)
    marks = await load_marks(options, structures)
    facts = await anyio.to_thread.run_sync(v.run_facts_blocking, services.core.factory, run_id)
    settings = await anyio.to_thread.run_sync(load_settings, options)
    return v.account_out(
        run_id=run_id,
        state=state,
        structures=structures,
        marks=marks,
        facts=facts,
        settings=settings,
        benchmark=await _benchmark(options, settings, facts, state, today),
        today=today,
    )


@router.get("/positions")
async def get_positions(
    services: Services,
    options: Options,
    run_id: RunId,
    state: Literal["open", "closed"] = "open",
    limit: Limit = 200,
) -> Items[OptStructureOut]:
    """The run's structures: the open ones (oldest first) with marks, or the closed ones, newest first."""
    today = et_date(services.core.clock.now())
    if state == "open":
        structures = await options.broker.structures(open_only=True)
        marks = await load_marks(options, structures)
    else:
        closed = [s for s in await options.broker.structures(open_only=False) if s.state == "closed"]
        structures = sorted(closed, key=lambda s: (s.closed_at or s.opened_at, s.id), reverse=True)
        marks = v.Marks()
    return Items[OptStructureOut](items=[v.structure_out(s, marks, today) for s in structures[:limit]])


# --- the chain ----------------------------------------------------------------------------------------------


@router.get("/chain")
async def get_chain(
    services: Services, options: Options, run_id: RunId, underlying: Underlying
) -> OptChainOut:
    with market_errors():
        expiries = await options.market.expiries(underlying)
        share = await options.market.underlying_quote(underlying)
    return v.chain_out(
        underlying,
        expiries,
        underlying_price=None if share is None else share.last,
        price_time=None if share is None else (share.last_trade_time or share.fetched_at),
        market_open=options.market.is_open(services.core.clock.now()),
    )


def _chain_cache(request: Request) -> dict[tuple[str, date], v.ChainSnapshot]:
    cache: dict[tuple[str, date], v.ChainSnapshot] | None = getattr(request.app.state, CHAIN_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(request.app.state, CHAIN_CACHE_ATTR, cache)
    return cache


def _fresh(snap: v.ChainSnapshot, now: datetime, ttl: timedelta) -> bool:
    return timedelta(0) <= now - snap.fetched_at < ttl


async def _fetch_chain(
    options: OptionApiServices, underlying: str, expiry: date, now: datetime
) -> v.ChainSnapshot:
    with market_errors():
        strikes = await options.market.strikes(underlying, expiry)
        if not strikes:
            raise _not_found("expiry")
        quotes: dict[int, OptionQuote] = {}
        for right in ("call", "put"):
            for contract, quote in await options.market.quotes_for_expiry(underlying, expiry, right):
                quotes[contract.id] = quote
        share = await options.market.underlying_quote(underlying)
    return v.ChainSnapshot(
        underlying=underlying,
        expiry=expiry,
        underlying_price=None if share is None else share.last,
        fetched_at=now,
        strikes=tuple(strikes),
        quotes=quotes,
    )


@router.get("/chain/quotes")
async def get_chain_quotes(
    request: Request,
    services: Services,
    options: Options,
    run_id: RunId,
    underlying: Underlying,
    expiry: date,
) -> OptChainQuotesOut:
    """One row per strike with both sides. The fetch is cached per (underlying, expiry) for
    `options.web_quote_cache_seconds`; `stale` is worked out again on every answer."""
    now = services.core.clock.now()
    settings = await anyio.to_thread.run_sync(load_settings, options)
    ttl = timedelta(seconds=settings.web_quote_cache_seconds)
    cache = _chain_cache(request)
    snap = cache.get((underlying, expiry))
    if snap is None or not _fresh(snap, now, ttl):
        snap = await _fetch_chain(options, underlying, expiry, now)
        for key in [k for k, old in cache.items() if not _fresh(old, now, ttl)]:
            del cache[key]
        cache[(underlying, expiry)] = snap
    return v.chain_quotes_out(
        snap,
        now=now,
        market_open=options.market.is_open(now),
        stale_seconds=settings.stale_quote_seconds,
    )


# --- orders -------------------------------------------------------------------------------------------------


@router.post("/orders/preview")
async def preview_order(body: OptOrderIn, user: CsrfUser, options: Options, run_id: RunId) -> OptPreviewOut:
    """The collateral engine's verdict on the ticket, before Submit. Nothing is stored."""
    req = order_request(body, actor(user))
    with market_errors():
        decision = await options.broker.preview(req)
    return v.preview_out(decision)


@router.post("/orders")
async def submit_order(
    body: OptOrderIn, user: CsrfUser, services: Services, options: Options, run_id: RunId
) -> OptOrderOut:
    """Submit the ticket: the order comes back `working` (the worker fills it) or `rejected`."""
    req = order_request(body, actor(user))
    with market_errors():
        result = await options.broker.submit(req)
    return (await _orders_out(services, options, [result.order]))[0]


@router.get("/orders")
async def get_orders(
    services: Services,
    options: Options,
    run_id: RunId,
    status: Literal["working", "history"] = "working",
    limit: Limit = 200,
) -> Items[OptOrderOut]:
    """The working orders, or the closed ones (filled, cancelled, expired, rejected); newest first."""
    if status == "working":
        orders = await options.broker.orders(status="working", limit=limit)
    else:
        orders = []
        for closed in CLOSED_STATUSES:
            orders.extend(await options.broker.orders(status=closed, limit=limit))
        orders.sort(key=lambda o: (o.closed_at or o.submitted_at, o.id), reverse=True)
        orders = orders[:limit]
    return Items[OptOrderOut](items=await _orders_out(services, options, orders))


async def _working_manual_order(options: OptionApiServices, order_id: int) -> OptOrderView:
    order = await options.broker.order(order_id)
    if order is None:
        raise _not_found("order")
    if order.source != SOURCE_MANUAL:
        raise ApiError(409, "conflict", "A strategy's order can't be changed by hand")
    if order.status != "working":
        raise ApiError(409, "conflict", "The order is no longer working")
    return order


async def _order_after(services: ApiServices, options: OptionApiServices, order_id: int) -> OptOrderOut:
    order = await options.broker.order(order_id)
    if order is None:
        raise _not_found("order")
    return (await _orders_out(services, options, [order]))[0]


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: OrderId, user: CsrfUser, services: Services, options: Options, run_id: RunId
) -> OptOrderOut:
    await _working_manual_order(options, order_id)
    if not await options.broker.cancel(order_id, CANCEL_REASON, actor(user)):
        raise ApiError(409, "conflict", "The order is no longer working")
    return await _order_after(services, options, order_id)


@router.post("/orders/{order_id}/reprice")
async def reprice_order(
    order_id: OrderId,
    body: OptRepriceIn,
    user: CsrfUser,
    services: Services,
    options: Options,
    run_id: RunId,
) -> OptOrderOut:
    await _working_manual_order(options, order_id)
    if not await options.broker.reprice(order_id, body.net_limit, actor(user)):
        raise ApiError(409, "conflict", "The order is no longer working")
    return await _order_after(services, options, order_id)


# --- activity -----------------------------------------------------------------------------------------------


@router.get("/activity")
def get_activity(
    services: Services,
    options: Options,
    run_id: RunId,
    limit: Limit = 100,
    before: AwareDatetime | None = None,
) -> Items[OptActivityOut]:
    """Fills, lifecycle events, prompts and the run's `options.*` events, newest first. `before` (a time
    with its zone) pages back: only items older than it."""
    items = v.activity_blocking(services.core.factory, run_id, before=before, limit=limit)
    return Items[OptActivityOut](items=items)


# --- prompts ------------------------------------------------------------------------------------------------


@router.get("/prompts")
def get_prompts(
    services: Services,
    options: Options,
    run_id: RunId,
    status: Literal["pending", "all"] = "pending",
) -> Items[OptPromptOut]:
    """The owner's open questions (from the prompt store), or every prompt of the run; newest first."""
    if status == "all":
        return Items[OptPromptOut](items=v.all_prompts_blocking(services.core.factory, run_id))
    pending = sorted(options.prompts.pending(), key=lambda p: (p.asked_at, p.id), reverse=True)
    return Items[OptPromptOut](items=[v.prompt_out(p) for p in pending])


@router.post("/prompts/{prompt_id}/answer")
def answer_prompt(
    prompt_id: PromptId, body: OptPromptAnswerIn, user: CsrfUser, options: Options, run_id: RunId
) -> OptPromptOut:
    """Record the owner's answer. The worker hands it to the strategy that asked."""
    result = options.prompts.answer(prompt_id, body.choice, text=body.text, via="web", actor=actor(user))
    if result.status == "invalid_choice":
        raise _invalid("choice", "Not one of this question's choices")
    if result.status == "text_required":
        raise _invalid("text", "This answer needs a text")
    if result.status == "already":
        raise ApiError(409, "conflict", "The question is already answered or closed")
    if result.status == "unknown" or result.prompt is None:
        raise _not_found("prompt")
    return v.prompt_out(result.prompt)


# --- strategies ---------------------------------------------------------------------------------------------


async def _open_counts(options: OptionApiServices) -> Counter[str]:
    """Open structures per source; empty without an options run."""
    run_id = await anyio.to_thread.run_sync(options.run_id)
    if run_id is None:
        return Counter()
    return Counter(s.source for s in await options.broker.structures(open_only=True))


def _strategy_blocking(options: OptionApiServices, key: str, open_structures: int) -> OptStrategyOut | None:
    registry = options.registry
    try:
        cfg = registry.current(key)
    except KeyError:  # no settings row yet: the worker's ensure_defaults() writes revision 1
        log.warning("api.option_strategy_without_settings", strategy=key)
        return None
    # The protocol declares these as properties; on the plug-in class they are plain class attributes.
    cls: Any = registry.plugin_class(key)
    return v.strategy_out(
        cfg,
        params_model=cls.params_model,
        schema=registry.json_schema(key),
        manual_events=cls.manual_events,
        open_structures=open_structures,
    )


def _strategies_blocking(options: OptionApiServices, counts: Counter[str]) -> list[OptStrategyOut]:
    found = (_strategy_blocking(options, key, counts[key]) for key in options.registry.keys())
    return [item for item in found if item is not None]


@router.get("/strategies")
async def get_strategies(options: Options) -> Items[OptStrategyOut]:
    counts = await _open_counts(options)
    items = await anyio.to_thread.run_sync(_strategies_blocking, options, counts)
    return Items[OptStrategyOut](items=items)


def _update_blocking(options: OptionApiServices, key: str, body: StrategyIn, by: str) -> None:
    if key not in options.registry.keys():
        raise _not_found("strategy")
    if not body.params and body.enabled is None:
        raise ApiError(
            422,
            "validation",
            "Nothing to change",
            fields=[{"loc": ["body"], "msg": "Send params or enabled"}],
        )
    try:
        options.registry.update(key, params=body.params, enabled=body.enabled, actor=by)
    except ValidationError as exc:
        fields = [
            {"loc": ["params", *e["loc"]], "msg": safe_field_message(e["msg"], [e.get("input")])}
            for e in exc.errors()
        ]
        raise ApiError(422, "validation", "Invalid strategy settings", fields=fields) from None
    except KeyError:
        raise ApiError(409, "conflict", "The strategy has no settings yet") from None


@router.put("/strategies/{key}")
async def put_strategy(key: Key, body: StrategyIn, user: CsrfUser, options: Options) -> OptStrategyOut:
    """A new revision of the plug-in's settings (validated by its params model, audited by the registry)."""
    await anyio.to_thread.run_sync(_update_blocking, options, key, body, actor(user))
    counts = await _open_counts(options)
    out = await anyio.to_thread.run_sync(_strategy_blocking, options, key, counts[key])
    if out is None:
        raise ApiError(409, "conflict", "The strategy has no settings yet")
    return out


async def _known_strategy(options: OptionApiServices, key: str) -> None:
    if key not in await anyio.to_thread.run_sync(options.registry.keys):
        raise _not_found("strategy")


@router.get("/strategies/{key}/panel")
async def get_panel(key: Key, options: Options, run_id: RunId) -> OptPanelOut:
    """The plug-in's own panel, drawn generically by the web."""
    await _known_strategy(options, key)
    try:
        panel = await options.host.panel(key)
    except KeyError:
        raise _not_found("strategy") from None
    return v.panel_out(key, panel)


@router.post("/strategies/{key}/actions")
async def post_action(
    key: Key, body: OptPanelActionIn, user: CsrfUser, options: Options, run_id: RunId
) -> OptPanelActionResultOut:
    """An owner action from the panel, handed to the plug-in. It never places an order."""
    await _known_strategy(options, key)
    req = PanelActionRequest(action=body.action, row_id=body.row_id, value=body.value)
    try:
        result = await options.host.action(key, req, actor(user))
    except KeyError:
        raise _not_found("strategy") from None
    return OptPanelActionResultOut(ok=result.ok, message=result.message)


# --- settings -----------------------------------------------------------------------------------------------


@router.get("/settings")
def get_settings(services: Services, options: Options) -> SettingsOut:
    """Every `options.*` setting (group `Options`), in declaration order."""
    return SettingsOut(items=v.settings_blocking(services.core.factory))


@router.put("/settings/{key}")
def put_setting(
    key: Key, body: SettingIn, user: CsrfUser, services: Services, options: Options
) -> SettingOut:
    if key not in v.SETTING_FIELDS:
        raise _not_found("setting")
    try:
        options.settings.set(key, body.value, actor(user))
    except ValidationError as exc:
        fields = [
            {"loc": list(e["loc"]), "msg": safe_field_message(e["msg"], [body.value])}
            for e in exc.errors(include_input=False)
        ]
        raise ApiError(422, "validation", "Invalid value", fields=fields) from None
    return v.setting_blocking(services.core.factory, key)
