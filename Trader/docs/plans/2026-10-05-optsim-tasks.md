# OPTSIM: task plan (T0 output)

Date: Mon 2026-10-05. Planner output for the options simulation. This is a specification: interfaces, data shapes, behaviour and acceptance tests. Builders write the code and the tests.

Sources: feature plan [`2026-10-05-options-simulation.md`](2026-10-05-options-simulation.md) (called "FP" below), wheel rules [`../specs/wheel-rules-spec.md`](../specs/wheel-rules-spec.md) (called "WS"), master plan Global Constraints. Where this file and FP differ, this file wins; every difference is listed in §8.

Reading order for a builder: §1 (rules), §2 (layout), §3 (contracts, all of it), then your own task in §5.

## 1. Global rules for every OPTSIM builder

1. **The stock engine is untouched.** No edit, rename or delete under `trader/engine`, `trader/broker`, `trader/strategies`, `trader/replay`, `trader/market`, `trader/decisions`, `trader/marks`, nor to `trader/runtime.py`, `trader/worker.py`, `trader/settings_store.py`, `trader/api/forms.py`, `trader/notify/*`, `tests/replay/golden/*`, or any existing test file. Importing from them is fine. `tests/options/test_stock_untouched.py` (T1) fails the build otherwise.
2. **Pre-existing files that MAY be edited, and by whom** (nobody else touches them; "additive" = new names only, no change to existing lines' behaviour):

| File | Task | Allowed change |
|---|---|---|
| `app/trader/db/models.py` | T1 | append the option models; add index `uq_runs_one_active_options` to `Run.__table_args__` |
| `app/trader/cli.py` | T1 | two lines: import and `register_options_cli(app)` |
| `app/pyproject.toml` | T1 | entry-point group `trader.option_strategies` with `wheel` |
| `app/trader/api/schemas.py` | T1 | append one "Options" section (§3.9) |
| `web/src/api/types.ts` | T1 | append the mirror of §3.9 (the existing `test_ts_contract.py` checks it) |
| `app/trader/api/deps.py` | T1 | `ApiServices.options: OptionApiServices \| None = None` (last field) |
| `app/trader/adapters/questrade/client.py` | T2 | additive: POST path and four methods; GET behaviour unchanged |
| `app/trader/adapters/finviz/scraper.py` | T8 | additive: one public method `snapshot` (the parser is a new file) |
| `app/trader/adapters/telegram/types.py`, `callbacks.py`, `bot.py` | T10 | additive: callback kind `prompt` |
| `web/src/App.tsx`, `web/src/layout/Layout.tsx`, web tests that pin the route or nav list | T15 | one route, one nav item, the options API provider |
| `app/trader/api/routers/__init__.py`, `app/trader/api/services.py` | T16 | register the router; build `OptionApiServices` |
| `docker/supervisord.conf`, `docker/crontab`, `docker/docker-compose.dev.yml`, `docker/docker-compose.prod.yml` | T16 | program `options-worker`; four cron lines; `stop_grace_period` |
| `app/tests/test_crontab.py`, `app/tests/test_docker_files.py`, `app/tests/api/test_web_client_contract.py` | T16 | expected cron lines and programs; also parse `optionsHttp.ts` |
| `docs/SPEC.md`, `Trader/README.md` | T17 | an Options section |

3. **Money** is `Decimal` in Python and `numeric(14,4)` in the database. Greeks, IV and ratios are `Decimal` too. Never `float`. An intent or request that receives a float raises `TypeError`.
4. **Time** only from the injected `Clock`. No `datetime.now()` or `date.today()` in `trader/`.
5. **No network in tests.** Questrade, FinViz and Telegram are faked. Database tests use the `db_factory` fixture (testcontainers) and carry the `db` marker.
6. **One book lock.** Every transaction that changes option cash, orders, structures or positions first takes `pg_advisory_xact_lock(hashtext('options_book:' || run_id))`. Fills, submits and the post-close job therefore never interleave.
7. **Tests while working are targeted**: `uv run --directory Trader/app pytest tests/options/test_x.py -q`, `npm --prefix Trader/web exec vitest run src/pages/options`. The full gate runs once, just before the commit: `bash Trader/build/gate.sh`.
8. **One command per Bash call.** A repo hook blocks `&&`, `;`, `||`, a leading `cd`, and `git -C`. Use `uv run --directory Trader/app ...` and `npm --prefix Trader/web ...`.
9. **Commits**: message prefix `OPTSIM-Tn:`; stage files by explicit path; trailer as in the master plan. Work in the worktree you were given; the orchestrator merges to `trunk`.
10. **A plug-in touches only its own package**, its own tables (`wheel_*` for the wheel) and `option_strategy_state`. The core (`trader/options`, the API, the web) never imports a plug-in and never names one.
11. **Progress**: `python3 Trader/build/progress.py report Tn builder "<what you are doing>" --pct N` at least every 4 minutes.

## 2. Package layout

```
app/trader/options/                 generic core (knows nothing about the wheel)
  types.py protocols.py settings.py account.py cli.py        T1
  market.py                                                   T3
  collateral.py                                               T4
  fill_model.py book.py broker.py valuation.py                T5
  lifecycle.py                                                T6
  facts.py                                                    T8
  prompts.py messages.py                                      T10
  worker.py __main__.py                                       T12
  runtime.py                                                  T16
app/trader/option_strategies/       plug-in framework and plug-ins
  base.py                                                     T1
  registry.py state.py host.py                                T7
  wheel/config.py inputs.py screen.py select.py evaluate.py stops.py calc.py alerts.py     T9
  wheel/strategy.py store.py panel.py screener.py             T11
app/trader/adapters/questrade/option_types.py                 T1   (client.py additions: T2)
app/trader/adapters/finviz/fundamentals.py                    T8
app/trader/jobs/options_refresh.py options_postclose.py       T13
app/trader/api/routers/options.py  api/options_views.py       T14
app/trader/db/migrations/versions/0011_options.py             T1
web/src/pages/Options.tsx  web/src/pages/options/*            T15
web/src/api/optionsClient.ts optionsHttp.ts                   T15
web/src/test/optionsFakeApi.ts optionsFixtures.ts             T15
app/tests/options/  app/tests/option_strategies/              each task adds its own files
app/tests/options/fakes.py factories.py toy_plugin.py contract_book.py test_stock_untouched.py   T1
app/tests/e2e/test_options_*.py                               T16
```

Changes from the layout proposed in the brief, with the reason found in the code:

| Change | Reason |
|---|---|
| Options settings live in `trader/options/settings.py` (`OptionSettings`, own store, same `settings` table, keys `options.*`), not in `RuntimeSettings` | `settings_store.py` is on the D2 decision path and its dump is the stock run's `params` snapshot. The stock store ignores unknown keys, so both stores share the table safely |
| The option API client is `optionsClient.ts` + `optionsHttp.ts` with its own provider, not new methods on `ApiClient` | about 60 existing web tests build a `FakeApiClient` that must implement every `ApiClient` method; a separate client leaves them untouched |
| Response schemas are appended to `api/schemas.py` and `types.ts` (T1), not put in a new module | the existing mirror test `test_ts_contract.py` then guards the API/web seam from wave 1 with no new test code |
| No `options-screen` job. The Saturday screen is `trader options-event wheel screen` | a job that names the wheel would put a plug-in into the core (rule 10) |
| No `run-options-worker.sh`. supervisord runs `python -m trader.options.worker` directly | the worker sleeps 30 s itself before exiting on a lost lock, so the Dockerfile is not touched |
| No "Wheel" tab and no wheel routes. A generic **Strategies** tab renders each plug-in's panel (§3.5) | "addable with no change to the core, API or web" (owner constraint; acceptance item 6) |

## 3. The contracts T1 delivers

Everything in this section is written by T1 and frozen when T1 merges. A later task that needs a change asks the orchestrator; it does not edit a T1 file.

### 3.1 Value types (`trader/options/types.py`)

Frozen, slotted dataclasses; `Literal` aliases for the enums. Exact names and values:

| Alias | Values |
|---|---|
| `Right` | `call`, `put` |
| `Side` | `buy`, `sell` |
| `Effect` | `open`, `close` |
| `Instrument` | `option`, `shares` |
| `OptOrderType` | `market`, `limit` |
| `Tif` | `day`, `gtc` |
| `OrderIntent` | `open`, `close`, `roll` |
| `OrderStatus` | `working`, `filled`, `cancelled`, `expired`, `rejected` |
| `StructureKind` | `long_call`, `long_put`, `csp`, `covered_call`, `debit_spread`, `credit_spread`, `iron_condor`, `calendar`, `diagonal`, `shares`, `custom` |
| `StructureState` | `open`, `closed` |
| `CloseReason` | `closed`, `expired`, `assigned`, `exercised`, `called_away`, `rolled`, `sold` |
| `LifecycleKind` | `expired`, `exercised`, `assigned`, `called_away`, `early_assignment`, `frozen` |
| `RejectReason` | `naked_short`, `insufficient_cash`, `position_cap`, `shares_committed`, `not_covered_after_close`, `nothing_to_close`, `unknown_contract`, `expired_contract`, `invalid_order`, `no_quote`, `structure_frozen`, `strategies_paused` |
| `NoFillReason` | `outside_hours`, `quote_missing`, `quote_delayed`, `quote_stale`, `halted`, `one_sided`, `crossed`, `zero_bid`, `limit_not_reached` |
| `PromptStatus` | `pending`, `answered`, `expired`, `cancelled` |
| constant `SOURCE_MANUAL` | `"manual"` (any other source is a strategy key) |

| Type | Fields |
|---|---|
| `ContractKey` | `underlying: str`, `expiry: date`, `strike: Decimal`, `right: Right` |
| `OptionContract` | `id: int`, `underlying: str`, `underlying_symbol_id: int`, `qt_symbol_id: int`, `root: str`, `expiry: date`, `strike: Decimal`, `right: Right`, `multiplier: int`, `is_monthly: bool`, `adjusted: bool` |
| `OptionQuote` | `contract_id`, `bid`, `ask`, `last` (Decimal or None), `bid_size`, `ask_size`, `volume`, `open_interest` (int or None), `iv` (decimal fraction, 0.35 = 35%), `delta` (signed), `gamma`, `theta`, `vega`, `last_trade_time`, `delay: int \| None`, `is_halted: bool`, `fetched_at: datetime` |
| `ExpiryInfo` | `expiry: date`, `dte: int` (calendar days from the session date), `is_monthly: bool`, `strikes: int` |
| `ChainStrike` | `strike: Decimal`, `call_id: int \| None`, `put_id: int \| None` (contract ids) |
| `LegSpec` (strategy side) | `instrument`, `side`, `ratio: int`, `underlying: str`, `contract: ContractKey \| None` |
| `OrderLeg` (resolved) | `leg_no: int`, `instrument`, `side`, `effect`, `ratio: int`, `underlying: str`, `contract_id: int \| None` |
| `OrderRequest` | `source`, `strategy_config_id: int \| None`, `intent`, `structure_id: int \| None`, `underlying`, `legs: tuple[OrderLeg, ...]`, `qty: int`, `order_type`, `net_limit: Decimal \| None`, `tif`, `walk: bool`, `take_profit_pct: Decimal \| None`, `reason: str` (at most 100 chars), `evidence: Mapping`, `submitted_by: str` |
| `OptOrderView` | `id`, the `OrderRequest` fields, `status`, `reject_reason`, `reject_detail`, `reserved_cash`, `submitted_at`, `closed_at`, `fill_net`, `fees` |
| `LegFill` | `leg_no`, `instrument`, `contract_id`, `side`, `effect`, `qty` (contracts or shares), `price`, `fee`, `quote: Mapping` |
| `OptionFillEvent` | `order_id`, `structure_id`, `source`, `strategy_config_id`, `intent`, `ts`, `qty`, `net_price`, `fees`, `legs: tuple[LegFill, ...]`, `realized_pnl: Decimal \| None` |
| `OptPositionView` | `id`, `structure_id`, `instrument`, `contract: OptionContract \| None`, `underlying`, `qty` (signed: short < 0), `avg_price`, `realized_pnl` |
| `StructureView` | `id`, `source`, `strategy_config_id`, `kind`, `underlying`, `state`, `close_reason`, `frozen`, `qty`, `entry_net`, `reserved_cash`, `take_profit_net`, `cover_structure_id`, `parent_structure_id`, `realized_pnl`, `fees_total`, `opened_at`, `closed_at`, `positions: tuple[OptPositionView, ...]`, `meta: Mapping` |
| `OptionAccountState` | `cash`, `reserved`, `free_cash` (= cash − reserved), `positions_value`, `equity` (the account value), `premium_collected`, `as_of`, `marks_complete: bool` |
| `CoverPair` | `short_leg_no`, `cover: Literal["cash","shares","long_leg"]`, `cover_ref: int \| None` (leg_no or shares structure id) |
| `CollateralDecision` | `accepted: bool`, `reject_reason`, `detail: str`, `kind: StructureKind`, `net_at_market: Decimal \| None`, `reserve_cash`, `max_loss: Decimal \| None`, `max_profit: Decimal \| None`, `breakevens: tuple[Decimal, ...]`, `fees`, `cash_after`, `free_cash_after`, `exposure_after`, `cap_limit`, `pairs: tuple[CoverPair, ...]`, `cover_structure_id: int \| None` |
| `SubmitResult` | `order: OptOrderView`, `decision: CollateralDecision` (a rejected order is stored with status `rejected`) |
| `LegQuote` | `leg_no`, `bid`, `ask`, `last`, `fetched_at`, `delay`, `is_halted`, `raw: Mapping` (the snapshot stored on the fill) |
| `FillDecision` / `NoFill` | `net_price`, `leg_prices: tuple[tuple[int, Decimal], ...]`, `fees`, `trigger: Literal["market","limit"]` / `reason: NoFillReason`, `detail` |
| `LifecycleEvent` | `id`, `structure_id`, `source`, `strategy_config_id`, `kind`, `session_date`, `ts`, `contract: OptionContract \| None`, `qty`, `strike`, `underlying_close`, `shares_delta`, `cash_delta`, `new_structure_id: int \| None` |
| `UnderlyingFacts` | every field of WS §3.1 by the spec's name (`eps_ttm`, `sma50_prior`, ...), each `Decimal \| None` (dates `date \| None`), plus `symbol_id`, `as_of: date`, `has_options: bool \| None`, `dividend_per_share`, `sources: Mapping[str, tuple[str, datetime]]` (field → source, fetched at) |
| `PromptChoice` | `code: str` (one of `a r y n c h o b s k w`), `label: str` |
| `OwnerPromptRequest` | `kind: str`, `scope_key: str`, `dedupe_key: str` (at most 150), `title`, `body`, `choices: tuple[PromptChoice, ...]`, `needs_text: bool`, `default_choice: str \| None`, `data: Mapping` |
| `PromptView` | `id`, `source`, the request fields, `status`, `asked_at`, `last_sent_at`, `send_count`, `answered_at`, `answer`, `answer_text`, `answered_via`, `delivered_at` |

**Price and quantity conventions** (every task uses these):

- One **unit** of an order is one set of its legs. `qty` is the number of units. An option leg's `ratio` is contracts per unit; a shares leg's `ratio` is shares per unit (100 for a buy-write).
- **Net price is per share, credit positive**: `net = Σ sell legs (price × ratio × m) − Σ buy legs (price × ratio × m)`, divided by 100, where `m` is the contract multiplier for an option leg and 1 for a shares leg. A `net_limit` of `0.45` means "receive at least 0.45"; `-1.20` means "pay at most 1.20".
- Cash moved by a fill = `net × 100 × qty` (positive = received) minus fees.
- `entry_net` of a structure and `take_profit_net` use the same sign.
- All prices are rounded to `options.tick_size` only when the system itself chooses a price (midpoint, walk); a price given by the owner is used as given.

### 3.2 Questrade option types (`trader/adapters/questrade/option_types.py`)

| Type | Fields |
|---|---|
| `QtChainStrike` | `strike: Decimal`, `call_id: int`, `put_id: int` |
| `QtChainRoot` | `root: str`, `multiplier: int`, `strikes: tuple[QtChainStrike, ...]` |
| `QtChainExpiry` | `expiry: date`, `roots: tuple[QtChainRoot, ...]` |
| `QtOptionQuote` | `symbol_id`, `symbol`, `underlying`, `underlying_id`, `bid`, `ask`, `last`, `bid_size`, `ask_size`, `volume`, `open_interest`, `iv_pct` (as Questrade sends it), `delta`, `gamma`, `theta`, `vega`, `rho`, `last_trade_time`, `delay: int \| None`, `is_halted`, `vwap`, `fetched_at`, `requested_at` |
| `QtSymbolDetails` | `symbol_id`, `symbol`, `description`, `security_type`, `listing_exchange`, `currency`, `has_options`, `eps`, `pe`, `market_cap`, `dividend`, `ex_date: date \| None`, `yield_pct`, `industry_sector`, `industry_group` |
| protocol `OptionQuoteClient` | `async option_chain(symbol_id: int) -> list[QtChainExpiry]`; `async option_quotes(ids: Sequence[int]) -> list[QtOptionQuote]`; `async option_quotes_filter(underlying_id: int, expiry: date, right: Right, min_strike: Decimal \| None = None, max_strike: Decimal \| None = None) -> list[QtOptionQuote]`; `async symbol_details(ids: Sequence[int]) -> dict[int, QtSymbolDetails]`; plus the existing `quotes`, `candles`, `symbols_by_names` |

### 3.3 Core protocols (`trader/options/protocols.py`)

| Protocol | Methods (async unless marked sync) |
|---|---|
| `OptionMarketView` | `expiries(underlying) -> list[ExpiryInfo]`; `strikes(underlying, expiry) -> list[ChainStrike]`; `contract(contract_id) -> OptionContract`; `find_contract(key: ContractKey) -> OptionContract \| None`; `quotes(contract_ids) -> dict[int, OptionQuote]`; `quotes_for_expiry(underlying, expiry, right, min_strike=None, max_strike=None) -> list[tuple[OptionContract, OptionQuote]]`; `underlying_quote(underlying) -> QtQuote \| None`; `facts(underlying) -> UnderlyingFacts \| None`; `daily_bars(underlying, start: date, end: date) -> list[Candle]`; sync `is_open(now) -> bool`; sync `is_monthly(expiry) -> bool` |
| `OptionBook` (sync, one per transaction) | `cash() -> Decimal`; `reserved() -> Decimal`; `add_structure(*, kind, source, strategy_config_id, underlying, qty, entry_net, reserved_cash, cover_structure_id, parent_structure_id, take_profit_net, meta, ts) -> int`; `apply(structure_id, instrument, contract_id, qty_delta: int, price: Decimal, ts) -> OptPositionView`; `move_cash(amount: Decimal, kind: Literal["buy","sell","fee"], ref: str, ts) -> None`; `set_reserved(structure_id, amount) -> None`; `close_structure(structure_id, close_reason, ts) -> None`; `freeze(structure_id, detail: str) -> None`; `structure(structure_id) -> StructureView`; `structures(*, open_only=True, source=None, underlying=None) -> list[StructureView]`; `record_lifecycle(*, structure_id, position_id, contract_id, kind, session_date, ts, underlying_close, strike, qty, shares_delta, cash_delta, detail) -> int \| None` (None: this event already exists) |
| `OptionBroker` | `preview(req) -> CollateralDecision`; `submit(req) -> SubmitResult`; `cancel(order_id, reason, actor) -> bool`; `reprice(order_id, net_limit, actor) -> bool`; `poll(now) -> list[OptionFillEvent]`; `walk(now) -> int`; `take_profits(now) -> list[int]`; `expire_day_orders(session_date, now) -> int`; `account() -> OptionAccountState`; `structures(*, source=None, open_only=True) -> list[StructureView]`; `orders(*, status=None, source=None, limit=200) -> list[OptOrderView]` |
| `CollateralEngine` (sync, pure) | `evaluate(req: OrderRequest, book: CollateralBook) -> CollateralDecision`, where `CollateralBook` is a dataclass: `account: OptionAccountState`, `structures`, `working_orders`, `contracts: Mapping[int, OptionContract]`, `quotes: Mapping[int, OptionQuote]`, `share_quotes: Mapping[str, QtQuote]`, `settings: OptionSettings`, `today: date` |
| `OptionFillModel` (sync, pure) | `assess(order: OptOrderView, quotes: Mapping[int, LegQuote], now, session_open, session_close, settings) -> FillDecision \| NoFill` (the mapping key is `leg_no`) |
| `FactsProvider` | `get(underlying) -> UnderlyingFacts \| None`; `refresh(underlyings: Sequence[str]) -> dict[str, UnderlyingFacts \| str]` (a string is the error for that ticker) |
| `PromptStore` (sync) | `ensure(run_id, source, req: OwnerPromptRequest) -> PromptView`; `get(prompt_id) -> PromptView \| None`; `by_key(dedupe_key) -> PromptView \| None`; `pending(source=None) -> list[PromptView]`; `answer(prompt_id, choice, *, text=None, via: Literal["telegram","web"], actor) -> AnswerResult` (`status`: `ok`, `already`, `unknown`, `invalid_choice`, `text_required`; `prompt`); `undelivered(source) -> list[PromptView]`; `mark_delivered(prompt_id)`; `due_for_send(now) -> list[PromptView]`; `mark_sent(prompt_id, now)`; `cancel(dedupe_key)` |
| `OptionRenderer` (sync, pure) | `fill(fill, structure) -> OutboundMessage`; `lifecycle(event) -> OutboundMessage`; `alert(source, kind, message, dedupe_key) -> OutboundMessage`; `prompt(prompt, buttons) -> OutboundMessage`; `summary(view: OptionSummaryView) -> OutboundMessage` |
| `StrategyHost` | `ensure_defaults()`; `due_events(session_date, now) -> list[tuple[str, OptionEvent]]`; `fire(strategy_key, event, *, force=False) -> JobOutcome`; `deliver_fill(fill)`; `deliver_lifecycle(event)`; `deliver_answers() -> int`; `sync_prompts() -> int`; `watch_underlyings() -> set[str]`; `panel(strategy_key) -> StrategyPanel`; `action(strategy_key, req, actor) -> PanelActionResult` |
| `OptionStrategyRegistryView` (sync) | `keys() -> list[str]`; `plugin_class(key)`; `json_schema(key)`; `current(key) -> OptionStrategyConfigView`; `update(key, *, params=None, enabled=None, actor) -> OptionStrategyConfigView` |
| dataclass `OptionApiServices` | `run_id: Callable[[], int \| None]`, `broker: OptionBroker`, `market: OptionMarketView`, `prompts: PromptStore`, `settings: OptionSettingsStore`, `registry: OptionStrategyRegistryView`, `host: StrategyHost` |
| dataclass `OptionSummaryView` | `session_date`, `account: OptionAccountState`, `day_change: Decimal \| None`, `fills: int`, `lifecycle: tuple[LifecycleEvent, ...]`, `open_structures: int`, `pending_prompts: int`, `by_source: Mapping[str, Decimal]` (realized today) |

### 3.4 Strategy framework (`trader/option_strategies/base.py`)

The stock framework's `ScheduledEvent` and `SessionOffset` are imported from `trader.strategies.base` and reused unchanged.

| Element | Shape |
|---|---|
| `OptionEvent` | `key: str`, `session_date: date`, `scheduled: bool` (False for a manual or cron-fired event) |
| `OptionStrategy` protocol, class attributes | `key` (pattern `^[a-z][a-z0-9_]{0,19}$`, equal to the entry-point name), `version: str`, `params_model: type[BaseModel]`, `manual_events: tuple[str, ...]`; constructed as `cls(params)` |
| `schedule(cal) -> list[ScheduledEvent]` | session-relative events; event key pattern `^[a-z][a-z0-9_]{0,19}$` |
| `async watch_underlyings(ctx) -> set[str]` | tickers whose facts and chains the refresh job keeps current |
| `async on_event(ctx, event: OptionEvent) -> list[OptionIntent]` | scheduled events, manual events, and the built-in key `postclose` (fired by the post-close job after expiry handling) |
| `async on_fill(ctx, fill: OptionFillEvent) -> list[OptionIntent]` | only this strategy's fills |
| `async on_lifecycle(ctx, event: LifecycleEvent) -> list[OptionIntent]` | only this strategy's structures |
| `async on_answer(ctx, prompt: PromptView) -> list[OptionIntent]` | an owner answer to one of this strategy's prompts |
| `async prompts(ctx) -> list[OwnerPromptRequest]` | the prompts that should be open now; the host upserts them by `dedupe_key` |
| `async panel(ctx) -> StrategyPanel` | what the Strategies tab shows (§3.5) |
| `async on_action(ctx, req: PanelActionRequest) -> PanelActionResult` | an owner action from the panel. It may change the plug-in's own tables. It never returns intents: the API process never trades for a strategy |
| `OptionStrategyContext` (dataclass) | `clock`, `calendar`, `session_date`, `run_id`, `strategy_key`, `config_id`, `params`, `settings: OptionSettings`, `market: OptionMarketView`, `account: OptionAccountState`, `structures: list[StructureView]` (this strategy's, open), `orders: list[OptOrderView]` (this strategy's, working), `state: StrategyState`, `factory: sessionmaker[Session]` (own tables only), `usd_cad_rate: Decimal`, `prompt(dedupe_key) -> PromptView \| None`, `equity_on(day: date) -> Decimal \| None`, `note(message, level="info", **data)`, `alert(kind, message, dedupe_key, **data)`; the collected `notes` and `alerts` lists |
| `StrategyState` protocol (sync) | `get(scope_key) -> dict \| None`; `put(scope_key, value: dict) -> None`; `delete(scope_key) -> None` |
| `OptionStrategyConfigView` | `id`, `strategy_key`, `version`, `revision`, `params: dict`, `enabled`, `created_at`, `created_by` |
| constant `ENTRY_POINT_GROUP` | `"trader.option_strategies"` |

Intents (`OptionIntent` is their union; each validates Decimals in `__post_init__`):

| Intent | Fields |
|---|---|
| `OpenStructure` | `legs: tuple[LegSpec, ...]`, `qty`, `order_type`, `net_limit`, `tif`, `reason`, `evidence: Mapping = {}`, `walk: bool = False`, `take_profit_pct: Decimal \| None = None` |
| `CloseStructure` | `structure_id`, `order_type`, `net_limit`, `reason`, `tif = "day"`, `walk = False`, `qty: int \| None = None` (None = all) |
| `RollStructure` | `structure_id`, `open_legs: tuple[LegSpec, ...]`, `order_type`, `net_limit`, `reason`, `evidence = {}`, `tif = "day"`, `walk = False`, `take_profit_pct = None` |
| `Reprice` | `order_id`, `net_limit` |
| `CancelOrder` | `order_id`, `reason` |
| `SellShares` | `structure_id` (a `shares` structure), `reason` (market order at the bid) |

Generic order features a strategy may ask for (so that no strategy logic lives in the worker):

- `walk=True` (limit orders only): if `net_limit` is None the broker starts at the midpoint net, rounded to the tick in the order's favour. Every `options.reprice_seconds` the worker moves the limit one tick toward the market net and never past it (WS §5.3).
- `take_profit_pct` (credit structures only): on fill the broker stores `take_profit_net = −entry_net × (1 − pct)`. On every mark pass, when the net to close at the market is at or better than that and no close order is working, the worker submits a closing limit order at `take_profit_net` with reason `take_profit` (FP §5.1 row 3).

### 3.5 Strategy panel (in `base.py`; rendered generically by the web)

| Type | Fields |
|---|---|
| `KeyValue` | `label`, `value: str`, `tone: Literal["ok","warn","bad"] \| None` |
| `PanelColumn` | `key`, `label`, `kind: Literal["text","money","number","date","badge","bool"]` |
| `PanelRow` | `id: str`, `cells: Mapping[str, Any]` (JSON values), `actions: tuple[str, ...]` (action keys offered on this row), `detail: tuple[KeyValue, ...]` (shown when the row is expanded) |
| `PanelTable` | `key`, `title`, `columns`, `rows`, `empty_text` |
| `PanelAction` | `key`, `label`, `kind: Literal["button","toggle","text","choice"]`, `confirm: bool`, `choices: tuple[str, ...]` |
| `StrategyPanel` | `summary: tuple[KeyValue, ...]`, `tables: tuple[PanelTable, ...]`, `actions: tuple[PanelAction, ...]` |
| `PanelActionRequest` | `action: str`, `row_id: str \| None`, `value: str \| bool \| None` |
| `PanelActionResult` | `ok: bool`, `message: str` |

### 3.6 The options run and account (`trader/options/account.py`, `cli.py`)

- A `runs` row with `mode = 'options'`, `status = 'active'`, `label = 'options'`, `params = {"options_settings": <OptionSettings dump>}`. New partial unique index `uq_runs_one_active_options` on `(mode) WHERE mode = 'options' AND status = 'active'`. `active_live_run_id` and `get_live_run` are untouched and keep returning the stock run.
- Its `sim_accounts` row: `currency = 'USD'`, `starting_cash = source_amount = cash`, `source_currency = 'USD'`, no FX. One `cash_ledger` row, kind `deposit`, ref `sim_account:<id>`, settled the same day.
- The cash ledger is reused through `trader.broker.ledger.Ledger.record` with kinds `buy` (cash out), `sell` (cash in), `fee`. Refs: `opt_fill:<fill id>`, `opt_life:<event id>`. Option cash is the ledger **total** (see risk R6).

| Function | Signature and behaviour |
|---|---|
| `active_options_run_id` | `(factory) -> int \| None` |
| `start_options_run` | `(factory, clock, calendar, settings: OptionSettings, *, cash: Decimal, actor: str) -> OptionsRun(run_id, account_id, retired_run_id)`. Retires the active options run (status `completed`) and starts a new one, in one transaction with one audit row `options_run.new`. Raises `OptionsRunRefused` when the active run has an open structure or a working order, or from 09:15 to 16:30 ET on a session day |
| `options_account` | `(factory, run_id) -> SimAccountInfo \| None` (the type from `trader.engine.runs`) |

CLI (`register_options_cli(app)` adds them; T1 writes all declarations, later tasks supply the targets):

| Command | Does | Target |
|---|---|---|
| `trader options-run new --cash 5000 --confirm` | `start_options_run`; prints the run id and cash; without `--confirm` prints what it would do | T1 |
| `trader options-check [--symbol F]` | read-only live check of chain, quotes and details | `trader.options.runtime.run_cli_command("check", ...)` |
| `trader options-refresh [--date] [--force]` | the refresh job | `run_cli_command("refresh", ...)` |
| `trader options-postclose [--date] [--force]` | the post-close job | `run_cli_command("postclose", ...)` |
| `trader options-event <strategy> <key> [--date] [--force]` and `trader options-event --due` | fire one strategy event, or every due and unfired one | `run_cli_command("event", ...)` |

`run_cli_command(name: str, *, session_date: date | None, force: bool, **kwargs) -> int` (exit code) is written by T16 in `trader/options/runtime.py`. Until then the four commands print "options runtime is not wired yet" and exit 1 (the import is done inside the command).

### 3.7 Migration 0011 (`0011_options.py`, revises 0010) and the models

Style as 0010: schema `trader`, `TS = DateTime(timezone=True)`, `MONEY = Numeric(14,4)`, a working `downgrade`. Ids are `bigint identity` unless stated. `RATIO = Numeric(12,6)`. Every table with `run_id` has an FK to `runs.id`. The app role's rights follow the same mechanism as earlier migrations.

| Table | Columns (type; constraint) | Keys and indexes |
|---|---|---|
| `option_contracts` | `id`; `underlying_symbol_id` (FK symbols, not null); `underlying` varchar(20); `qt_symbol_id` bigint not null; `root` varchar(20); `expiry` date; `strike` MONEY; `right` varchar(4), check in (`call`,`put`); `multiplier` int default 100; `is_monthly` bool; `adjusted` bool default false; `first_seen_at` TS | unique `qt_symbol_id`; unique (`underlying_symbol_id`,`expiry`,`strike`,`right`,`root`); index (`underlying`,`expiry`) |
| `option_chain_cache` | `underlying_symbol_id` (PK, FK); `fetched_at` TS; `chain` jsonb (list of `{expiry, root, multiplier, strikes: [{strike, call_id, put_id}]}` with Questrade ids); `expiries` int | |
| `option_quote_marks` | `contract_id` (PK, FK option_contracts); `fetched_at` TS; `bid`,`ask`,`last` MONEY null; `bid_size`,`ask_size` int null; `volume`,`open_interest` bigint null; `iv`,`delta`,`gamma`,`theta`,`vega` RATIO null; `last_trade_time` TS null; `delay` int null; `is_halted` bool; `underlying_price` MONEY null | |
| `underlying_facts` | `symbol_id` (FK); `as_of` date; `ticker` varchar(20); `security_type` varchar(30); `sector` varchar(60); `price` MONEY; `eps_ttm` MONEY; `eps_growth_yoy` RATIO; `debt_to_equity` Numeric(12,4); `book_value_per_share` MONEY; `market_cap_usd` Numeric(20,2); `sma50`,`sma50_prior`,`low_52w` MONEY; `sessions_since_52w_low` int; `rsi14` Numeric(8,4); `next_earnings_date`,`next_ex_dividend_date` date; `dividend_per_share` MONEY; `dividend_yield`,`payout_ratio`,`short_float` RATIO; `has_options` bool; `sources` jsonb not null; `fetched_at` TS. All facts nullable | PK (`symbol_id`,`as_of`) |
| `option_strategy_configs` | `id`; `strategy_key` varchar(30); `version` varchar(20); `revision` int; `params` jsonb not null; `enabled` bool; `created_at` TS; `created_by` varchar(50) | unique (`strategy_key`,`revision`) |
| `option_strategy_state` | `strategy_key` varchar(30); `scope_key` varchar(100); `value` jsonb not null; `version` int default 1; `updated_at` TS | PK (`strategy_key`,`scope_key`) |
| `opt_structures` | `id`; `run_id`; `source` varchar(30); `strategy_config_id` (FK option_strategy_configs, null); `kind` varchar(20); `underlying_symbol_id` (FK); `underlying` varchar(20); `state` varchar(8), check in (`open`,`closed`); `close_reason` varchar(12) null; `frozen` bool default false; `qty` int > 0; `entry_net` MONEY; `reserved_cash` MONEY default 0, check >= 0; `take_profit_net` MONEY null; `cover_structure_id`,`parent_structure_id` (FK self, null); `realized_pnl` MONEY default 0; `fees_total` MONEY default 0; `opened_at` TS; `closed_at` TS null; `meta` jsonb | index (`run_id`,`state`); index (`run_id`,`underlying_symbol_id`) |
| `opt_positions` | `id`; `run_id`; `structure_id` (FK); `instrument` varchar(6); `contract_id` (FK, null for shares); `underlying_symbol_id` (FK); `qty` int (signed); `avg_price` MONEY; `realized_pnl` MONEY default 0; `opened_at` TS; `closed_at` TS null | unique index (`structure_id`, coalesce(`contract_id`, 0)); check (`instrument` = 'option') = (`contract_id` is not null) |
| `opt_orders` | `id`; `run_id`; `source` varchar(30); `strategy_config_id` (FK, null); `intent` varchar(5); `structure_id` (FK opt_structures, null); `underlying_symbol_id` (FK); `order_type` varchar(6); `net_limit` MONEY null; `tif` varchar(3); `qty` int > 0; `status` varchar(10); `walk` bool default false; `walk_next_at` TS null; `take_profit_pct` Numeric(6,4) null; `reject_reason` varchar(30) null; `reject_detail` text null; `reason` varchar(100); `evidence` jsonb; `reserved_cash` MONEY default 0; `session_date` date; `submitted_at` TS; `submitted_by` varchar(50); `updated_at` TS; `closed_at` TS null; `fill_net` MONEY null; `fees` MONEY null | index (`run_id`,`status`); checks on `intent`, `order_type`, `tif`, `status` values; check (`order_type` = 'market') or `net_limit` is not null or `walk` |
| `opt_order_legs` | `id`; `order_id` (FK); `leg_no` smallint; `instrument` varchar(6); `contract_id` (FK, null); `underlying_symbol_id` (FK); `side` varchar(4); `effect` varchar(5); `ratio` int > 0 | unique (`order_id`,`leg_no`) |
| `opt_fills` | `id`; `run_id`; `order_id` (FK); `leg_id` (FK opt_order_legs); `structure_id` (FK); `ts` TS; `side` varchar(4); `qty` int > 0; `price` MONEY >= 0; `fee` MONEY >= 0; `quote` jsonb not null (bid, ask, last, sizes, iv, delta, open_interest, fetched_at); `usd_cad_rate` Numeric(12,6) | unique (`order_id`,`leg_id`); index (`run_id`,`ts`) |
| `opt_lifecycle_events` | `id`; `run_id`; `structure_id` (FK); `position_id` (FK); `contract_id` (FK, null); `kind` varchar(16); `session_date` date; `ts` TS; `underlying_close` MONEY null; `strike` MONEY null; `qty` int; `shares_delta` int default 0; `cash_delta` MONEY default 0; `new_structure_id` (FK, null); `detail` jsonb; `delivered_at` TS null | unique (`position_id`,`kind`,`session_date`); index (`run_id`,`session_date`) |
| `owner_prompts` | `id`; `run_id`; `source` varchar(30); `kind` varchar(30); `scope_key` varchar(60); `dedupe_key` varchar(150); `title` varchar(200); `body` text; `choices` jsonb; `needs_text` bool default false; `default_choice` varchar(1) null; `data` jsonb; `status` varchar(10); `asked_at` TS; `last_sent_at` TS null; `send_count` int default 0; `answered_at` TS null; `answer` varchar(1) null; `answer_text` text null; `answered_via` varchar(10) null; `answered_by` varchar(50) null; `delivered_at` TS null | unique `dedupe_key`; index (`run_id`,`status`) |
| `wheel_tickers` | `symbol_id` (PK, FK); `ticker` varchar(20); `status` varchar(10), check in (`candidate`,`approved`,`rejected`); `would_own` bool null; `ownership_reason` text null; `thesis_broken` bool default false; `security_type_override` varchar(30) null; `acknowledged_cautions` jsonb default `[]`; `last_verdict` varchar(30) null; `last_screen` jsonb null; `last_screened_at` TS null; `origin` varchar(10) (`screen`,`manual`); `created_at`,`updated_at` TS; `updated_by` varchar(50) | |
| `wheel_positions` | `id`; `run_id`; `symbol_id` (FK); `ticker`; `state` varchar(12), check in (`PUT_OPEN`,`SHARES_HELD`,`CALL_OPEN`,`NONE`); `put_structure_id`,`shares_structure_id`,`call_structure_id` (FK opt_structures, null); `contracts` int; `roll_count` int default 0; `total_put_premium`,`total_call_premium`,`dividends` MONEY default 0 (per share); `assignment_strike`,`net_cost` MONEY null; `entry` jsonb (WS §5.4 record); `fresh_cash_answer` bool null; `fresh_cash_at` TS null; `drawdown_review_at` TS null; `drawdown_review_text` text null; `fees` MONEY default 0; `opened_at` TS; `closed_at` TS null; `close_reason` varchar(30) null; `full_cycle_result` MONEY null | partial unique (`run_id`,`symbol_id`) where `closed_at` is null |
| `wheel_events` | `id`; `run_id`; `wheel_position_id` (FK, null); `symbol_id` (FK); `session_date` date; `ts` TS; `kind` varchar(10) (`screen`,`evaluate`,`action`,`alert`,`answer`,`lifecycle`,`review`); `action` varchar(40) null; `reason` text; `data` jsonb | partial unique (`wheel_position_id`,`session_date`) where `kind` = 'evaluate'; index (`run_id`,`session_date`) |

Reused unchanged: `runs`, `sim_accounts`, `cash_ledger`, `equity_snapshots`, `event_log` (source `options.<part>`, at most 50 chars, with the options run id), `notifications`, `job_runs`, `worker_heartbeats` (process `options-worker`), `telegram_callbacks` (kind `prompt`), `settings`, `audit_log`, `symbols`, `daily_candles`.

### 3.8 Settings (`trader/options/settings.py`)

`OptionSettings` is a frozen pydantic model, one field per key, every field with an `alias` equal to the key. `OptionSettingsStore(factory, now)` has `load() -> OptionSettings` and `set(key, value, actor) -> OptionSettings`, with the same validation, per-key repair and `audit_log` behaviour as `SettingsStore` (action `settings.set:<key>`).

| Key | Default | Bounds | Meaning |
|---|---|---|---|
| `options.starting_cash` | 5000 | > 0 | default cash of `options-run new` (D4) |
| `options.max_position_pct` | 0.50 | (0, 1] | cap per underlying, of account value (D9) |
| `options.fee_per_contract` | 0.99 | 0 to 10 | per contract, per leg, each way (P4) |
| `options.assignment_fee` | 0 | 0 to 100 | per assignment or exercise. **ASSUMPTION**: verify at T17 |
| `options.share_commission` | 0 | 0 to 100 | per shares leg |
| `options.quote_poll_seconds` | 5 | 1 to 60 | working-order poll |
| `options.mark_seconds` | 60 | 10 to 3600 | open-position marks |
| `options.snapshot_seconds` | 300 | 60 to 3600 | equity snapshot spacing in the session |
| `options.stale_quote_seconds` | 15 | 1 to 300 | a quote older than this (by `fetched_at`) never fills |
| `options.reprice_seconds` | 60 | 5 to 3600 | walk step interval |
| `options.tick_size` | 0.01 | 0.01 to 0.10 | **ASSUMPTION**: one tick for every contract |
| `options.itm_threshold` | 0.01 | 0 to 1 | in the money at expiry when the close is this far through the strike (P6) |
| `options.early_assignment_enabled` | true | | P5 |
| `options.max_legs` | 4 | 1 to 4 | per order |
| `options.max_contracts_per_order` | 10 | 1 to 100 | |
| `options.allow_market_orders` | true | | |
| `options.watchlist` | `[]` | tickers | underlyings kept fresh for the manual desk |
| `options.benchmark_ticker` | `SOFI` | ticker | P3; shown on the Account tab and used by strategies |
| `options.strategies_paused` | false | | true: the host fires no event and accepts no strategy intent |
| `options.prompt_repeat_hours` | 24 | 1 to 168 | P9 |
| `options.facts_max_age_hours` | 36 | 1 to 240 | older facts count as missing |
| `options.chain_cache_hours` | 24 | 1 to 168 | chain structure cache |
| `options.web_quote_cache_seconds` | 5 | 1 to 60 | chain quotes served to the web |
| `options.heartbeat_seconds` | 15 | 5 to 300 | options worker |
| `options.strike_touch_alerts` | true | | `STRIKE_TOUCHED` for every short option |

### 3.9 API schemas and routes

Appended to `trader/api/schemas.py` (all extend `ApiModel`; money and ratios are `Decimal`) and mirrored in `web/src/api/types.ts`. Existing models reused as they are: `Items`, `FieldOut`, `SettingOut`, `SettingsOut`, `SettingIn`, `StrategyIn`.

| Model | Fields |
|---|---|
| `OptContractOut` | `id`, `underlying`, `expiry`, `strike`, `right`, `multiplier`, `is_monthly`, `dte`, `label` (for example `F 2026-10-30 P 14.50`) |
| `OptQuoteOut` | `contract_id`, `bid`, `ask`, `last`, `bid_size`, `ask_size`, `volume`, `open_interest`, `iv`, `delta`, `gamma`, `theta`, `vega`, `fetched_at`, `stale: bool` |
| `OptExpiryOut` | `expiry`, `dte`, `is_monthly`, `strikes` |
| `OptChainOut` | `underlying`, `underlying_price`, `price_time`, `market_open`, `expiries: list[OptExpiryOut]` |
| `OptChainRowOut` | `strike`, `call_contract_id`, `put_contract_id`, `call: OptQuoteOut \| None`, `put: OptQuoteOut \| None` |
| `OptChainQuotesOut` | `underlying`, `expiry`, `underlying_price`, `fetched_at`, `market_open`, `rows: list[OptChainRowOut]` |
| `OptLegIn` | `instrument`, `contract_id: int \| None`, `side`, `effect`, `ratio` |
| `OptOrderIn` | `underlying`, `intent`, `structure_id: int \| None`, `legs: list[OptLegIn]`, `qty`, `order_type`, `net_limit: Decimal \| None`, `tif`, `walk: bool` |
| `OptPreviewOut` | `accepted`, `reject_reason`, `detail`, `kind`, `net_at_market`, `max_loss`, `max_profit`, `breakevens: list[Decimal]`, `fees`, `reserve_cash`, `cash_after`, `free_cash_after`, `exposure_after`, `cap_limit` |
| `OptLegOut` | `leg_no`, `instrument`, `contract: OptContractOut \| None`, `side`, `effect`, `ratio`, `fill_price: Decimal \| None`, `fill_quote: dict \| None` |
| `OptOrderOut` | `id`, `source`, `intent`, `structure_id`, `underlying`, `legs: list[OptLegOut]`, `qty`, `order_type`, `net_limit`, `tif`, `status`, `walk`, `reject_reason`, `reject_detail`, `reason`, `reserved_cash`, `submitted_at`, `closed_at`, `fill_net`, `fees` |
| `OptRepriceIn` | `net_limit` |
| `OptPositionOut` | `id`, `instrument`, `contract: OptContractOut \| None`, `qty`, `avg_price`, `mark: Decimal \| None`, `unrealized_pnl: Decimal \| None`, `delta: Decimal \| None` |
| `OptStructureOut` | `id`, `source`, `kind`, `underlying`, `state`, `close_reason`, `frozen`, `qty`, `entry_net`, `reserved_cash`, `take_profit_net`, `realized_pnl`, `unrealized_pnl`, `fees_total`, `opened_at`, `closed_at`, `dte: int \| None`, `positions: list[OptPositionOut]` |
| `OptSourceResultOut` | `source`, `open_structures`, `reserved`, `realized_pnl`, `unrealized_pnl`, `premium_collected` |
| `OptBenchmarkOut` | `ticker`, `since: date`, `benchmark_return: Decimal \| None`, `account_return: Decimal \| None` |
| `OptAccountOut` | `run_id: int \| None`, `started_at`, `starting_cash`, `cash`, `reserved`, `free_cash`, `positions_value`, `account_value`, `premium_collected`, `realized_pnl`, `unrealized_pnl`, `fees_total`, `max_position_pct`, `marks_as_of`, `marks_complete`, `worker_beat_at: datetime \| None`, `by_source: list[OptSourceResultOut]`, `benchmark: OptBenchmarkOut \| None` |
| `OptActivityOut` | `id: str`, `ts`, `kind` (`fill`,`lifecycle`,`decision`,`alert`,`prompt`,`order`), `source`, `underlying: str \| None`, `title`, `detail`, `level`, `structure_id: int \| None` |
| `OptPromptChoiceOut` | `code`, `label` |
| `OptPromptOut` | `id`, `source`, `kind`, `scope_key`, `title`, `body`, `choices: list[OptPromptChoiceOut]`, `needs_text`, `status`, `asked_at`, `answered_at`, `answer`, `answer_text`, `answered_via`, `data: dict` |
| `OptPromptAnswerIn` | `choice`, `text: str \| None` |
| `OptStrategyOut` | `key`, `version`, `enabled`, `revision`, `params: dict`, `schema: dict`, `fields: list[FieldOut]`, `updated_at`, `updated_by`, `open_structures`, `manual_events: list[str]` |
| `OptKeyValueOut`, `OptPanelColumnOut`, `OptPanelRowOut`, `OptPanelTableOut`, `OptPanelActionOut`, `OptPanelOut` | the §3.5 types field for field; `OptPanelOut` adds `strategy_key` |
| `OptPanelActionIn` / `OptPanelActionResultOut` | `action`, `row_id`, `value` / `ok`, `message` |

Routes (all under `/api/options`, signed-in user; every write needs the CSRF header; audit actor `web:<username>`):

| Method and path | Query or body | Response |
|---|---|---|
| GET `/account` | | `OptAccountOut` |
| GET `/positions` | `state` = `open` (default) or `closed`; `limit` | `Items[OptStructureOut]` |
| GET `/chain` | `underlying` | `OptChainOut` |
| GET `/chain/quotes` | `underlying`, `expiry` | `OptChainQuotesOut` |
| POST `/orders/preview` | `OptOrderIn` | `OptPreviewOut` |
| POST `/orders` | `OptOrderIn` | `OptOrderOut` (status `working` or `rejected`) |
| GET `/orders` | `status` = `working` (default) or `history`; `limit` | `Items[OptOrderOut]` |
| POST `/orders/{order_id}/cancel` | | `OptOrderOut` |
| POST `/orders/{order_id}/reprice` | `OptRepriceIn` | `OptOrderOut` |
| GET `/activity` | `limit` (default 100), `before` | `Items[OptActivityOut]` |
| GET `/prompts` | `status` = `pending` (default) or `all` | `Items[OptPromptOut]` |
| POST `/prompts/{prompt_id}/answer` | `OptPromptAnswerIn` | `OptPromptOut` |
| GET `/strategies` | | `Items[OptStrategyOut]` |
| PUT `/strategies/{key}` | `StrategyIn` | `OptStrategyOut` |
| GET `/strategies/{key}/panel` | | `OptPanelOut` |
| POST `/strategies/{key}/actions` | `OptPanelActionIn` | `OptPanelActionResultOut` |
| GET `/settings` | | `SettingsOut` (every item in group `Options`) |
| PUT `/settings/{key}` | `SettingIn` | `SettingOut` |

Errors use the existing `ApiError` shape: 404 `not_found` (unknown order, prompt, strategy, underlying or setting), 409 `no_options_run` (no active options run; `/settings` and `/strategies` still answer), 409 `conflict` (cancel or reprice of an order that is not working; answer to a prompt already answered), 422 `validation`, 503 `unavailable` (`ApiServices.options` is None, or Questrade cannot be reached for a chain). A collateral rejection is not an HTTP error: the order comes back with status `rejected`.

### 3.10 Fakes and test helpers (`tests/options/`)

| Helper | What it gives |
|---|---|
| `fakes.FakeOptionMarket` | an in-memory `OptionMarketView`: `add_underlying(ticker, price)`, `add_contract(...) -> OptionContract`, `set_quote(contract_id, bid, ask, **greeks)`, `set_facts(ticker, **fields)`, `set_bars(ticker, [...])`; quotes are stamped from the clock it is given; `open: bool` |
| `fakes.FakeBook` | the reference in-memory `OptionBook` (real arithmetic: signed quantities, average price, realized P&L, cash, reserved) |
| `fakes.FakeOptionBroker` | an `OptionBroker` over `FakeBook` with a pluggable `CollateralEngine` (default: accept, reserve 0): records every request; `fill(order_id, leg_prices)` and `fill_all_at_market()` produce `OptionFillEvent`s |
| `fakes.FakeQtOptions` | an `OptionQuoteClient`: chains, option quotes, details, plus share quotes and daily candles |
| `fakes.FakePromptStore`, `fakes.RecordingHost`, `fakes.FakeFacts` | in-memory `PromptStore`, `StrategyHost`, `FactsProvider` |
| `toy_plugin.ToyCallBuyer` | the acceptance-item-6 plug-in (key `toy_call`): on its event `toy_buy` (open+5m) it buys one call of the first expiry at least 30 days out, nearest the money, with a market order; it has a one-table panel and one action |
| `factories` | `add_options_run(session, cash=5000)`, `add_underlying`, `add_contract`, `add_structure`, `add_position`, `add_order`, `make_quote`, `make_leg`, `make_request`; the existing `tests/factories.add_symbol` is reused |
| `contract_book.BookContract` | a reusable test class of about 10 cases that any `OptionBook` must pass (T1 runs it against `FakeBook`; T5 against the database book) |

### 3.11 Registration points: who owns each

| Point | Owner | How later tasks plug in |
|---|---|---|
| CLI commands | T1 (`trader/options/cli.py`, two lines in `cli.py`) | they implement `run_cli_command` targets (T16 dispatches to T2, T12, T13 functions named in §5) |
| Models and migration 0011 | T1 | nobody adds a column; a missing one is a contract change |
| Settings keys | T1 | read `OptionSettings` |
| API schemas and TS types | T1 | T14 and T15 import them |
| `ApiServices.options` | T1 (field), T16 (value) | T14 reads it; tests build it from the fakes |
| Entry points | T1 (`wheel = "trader.option_strategies.wheel.strategy:WheelStrategy"`) | the target does not exist until T11; the registry skips a plug-in that fails to load, as the stock one does |
| Router registration, API services | T16 | T14 exports `router` |
| Web route, nav, providers | T15 | |
| supervisord, crontab, compose | T16 | T12 exports `python -m trader.options.worker` |
| Telegram callback kind | T10 | |

## 4. Dependency table, waves and planned minutes

| Task | Depends on | Wave | Planned min (build + check) |
|---|---|---|---|
| T1 Contracts | T0 | 1 | 100 |
| T2 Questrade option client | T1 | 2 | 40 |
| T3 Option market data | T1 | 2 | 45 |
| T4 Collateral engine | T1 | 2 | 50 |
| T5 Fill model, book, broker | T1 | 2 | 75 |
| T6 Lifecycle | T1 | 2 | 50 |
| T7 Registry, state, host | T1 | 2 | 50 |
| T8 Underlying facts | T1 | 2 | 55 |
| T9 Wheel rules (pure) | T1 | 2 | 70 |
| T10 Owner prompts and messages | T1 | 2 | 45 |
| T14 API | T1 | 2 | 60 |
| T15 Web | T1 | 2 | 90 |
| T11 Wheel plug-in | T7, T9, T10 | 3 | 70 |
| T12 Options worker | T3, T5, T7, T10 | 3 | 60 |
| T13 Jobs | T6, T7, T8, T10 | 3 | 50 |
| T16 Wiring, deploy files, end-to-end | T2 to T15 | 4 | 90 |
| T17 Deploy and live checks | T16 | 5 | 45 |

- **Wave 2 has 11 tasks and the cap is 8 builders.** Start T5, T9, T15, T7, T10, T6, T8, T3 first (they feed wave 3 or are long). Start T14, T4 and T2 as slots free up (first slots free at about minute 45); all three still finish before wave 3 does.
- Wave 3 tasks start as soon as their own dependencies are merged, not when the whole of wave 2 is.
- **Critical path**: T1 (100) → T9 (70) → T11 (70) → T16 (90) → T17 (45) = 375 min. T1 → T5 (75) → T12 (60) → T16 is 5 minutes shorter, so both chains are critical. FP named only the second.
- One full gate per task, just before its commit. T16 runs the gate twice at most (after wiring, after the end-to-end tests).

## 5. Tasks

Every task: tests go in new files only; reuse `tests/conftest.py` (`db_factory`, `pg_url`), `tests/factories.add_symbol`, `trader.market.clock.FixedClock`, `tests/fakes_telegram` (`FakeTelegramApi`, `RecordingNotifier`, `FakeIssuer`), `tests/api/conftest.make_client`, `tests/fakes_api.test_core`, and the T1 helpers of §3.10. "Table" in a test name means one parametrised test with one row per case.

### T1. Contracts

**Goal**: deliver everything in §3 so that eleven builders can work apart. No behaviour beyond the options run, the settings store, the fakes and the CLI shell. **Depends on**: T0.

**Owns**: `trader/options/__init__.py`, `types.py`, `protocols.py`, `settings.py`, `account.py`, `cli.py`; `trader/option_strategies/__init__.py`, `base.py`; `trader/adapters/questrade/option_types.py`; `trader/db/migrations/versions/0011_options.py`; `tests/options/__init__.py`, `fakes.py`, `factories.py`, `toy_plugin.py`, `contract_book.py`, `test_stock_untouched.py`, `test_contracts.py`, `test_account.py`, `test_settings.py`, `test_migration_0011.py`; and the T1 rows of the allow-list in §1.

**Interfaces**: §3 in full. Also in `types.py`: `net_price(legs, prices: Mapping[int, Decimal], multipliers: Mapping[int, int]) -> Decimal`, `round_tick(value, tick, favour: Literal["up","down"]) -> Decimal`, `dte(expiry, today) -> int`, `contract_label(contract) -> str`.

**Behaviour**:
- `test_stock_untouched.py`: the base is the environment variable `TRADER_OPTSIM_BASE`, else the parent of the first commit on HEAD's first-parent history whose subject starts `OPTSIM-T1:`; it skips only when neither exists. Git runs with the repository root as working directory (see risk R1). From base to HEAD: no change under the paths of §1 rule 1; no file that existed under `Trader/app/tests` at the base changed, except the three test files in the allow-list; `tests/replay/golden` unchanged; no existing job line of `docker/crontab` removed or altered (added lines are allowed). One assertion proves the diff helper sees a real change (`Trader/app/trader/options` is not empty), so the test can never pass by seeing nothing.
- `FakeBook` arithmetic (the reference for T5): `apply` adds a signed quantity; adding to a position re-averages the price; reducing realizes `(price − avg) × closed qty × multiplier` for a long and the reverse for a short; a position at zero gets `closed_at`; crossing through zero raises `ValueError`.

**Tests**:
- `test_migration_up_down_up`: 0011 applies, downgrades to 0010 and applies again.
- `test_models_match_the_migrated_schema`: every §3.7 table and column exists with the stated type and nullability.
- `test_table_constraints` (table): each unique key, check and partial index of §3.7 rejects a violating row.
- `test_one_active_options_run_beside_the_live_run`: a second active options run is refused; a live run is unaffected.
- `test_start_options_run_creates_account_deposit_and_audit`: 5,000 USD, one `deposit` ledger row, one audit row.
- `test_start_options_run_refusals` (table): open structure, working order, inside 09:15 to 16:30 ET.
- `test_live_run_lookup_ignores_the_options_run`: `active_live_run_id` and `get_live_run` still return the stock run.
- `test_setting_defaults_and_bounds` (table): every key of §3.8.
- `test_option_store_sets_audits_and_repairs`; `test_stock_store_ignores_option_keys_and_the_reverse`.
- `test_intents_and_requests_reject_floats`; `test_net_price_and_round_tick` (table, both signs).
- `test_fake_book_passes_the_book_contract`: `BookContract` on `FakeBook`.
- `test_fakes_and_toy_plugin_satisfy_their_protocols`.
- `test_cli_registers_the_option_commands`; `test_options_run_new_needs_confirm`; `test_unwired_commands_exit_1_with_a_clear_line`.
- `test_api_services_options_defaults_to_none`: every existing `make_services` call still works.
- the existing `test_ts_contract.py` passes with the new models and types.
- `test_stock_untouched` (above).

**Out of scope**: any service implementation; any edit outside the allow-list.

### T2. Questrade option client

**Goal**: chain, option quotes (by ids and by filter) and symbol details on the existing client, with the same pacing and retry rules for POST as for GET. **Depends on**: T1.

**Owns**: `trader/adapters/questrade/client.py` (additive), `trader/adapters/questrade/options_check.py`, `tests/adapters/test_questrade_options.py`.

**Interfaces**: the four `OptionQuoteClient` methods on `QuestradeClient`; `async live_check(client, symbol: str) -> dict` (used by `trader options-check`).

**Behaviour**:
- POST goes through the same token bucket (`market`), the same single forced refresh on 401, the same 429 pause and 5xx or transport back-off, the same package-401 rule. Retrying is safe: both POSTs are reads.
- `option_quotes`: ids in chunks of 100. `option_quotes_filter` body: `{"filters":[{"optionType":"Call"|"Put","underlyingId":..,"expiryDate":..,"minstrikePrice":..,"maxstrikePrice":..}]}`; the strike keys are left out when None; `expiryDate` is sent as the chain returned it.
- Parsing: `volatility` → `iv_pct` unchanged (T3 divides by 100); a null number stays None; `delay` None when absent; `fetched_at` and `requested_at` as `quotes()` sets them; chain `expiryDate` → the ET date; `symbol_details` reads `GET symbols?ids=` in chunks of 100, `exDate` → date or None.
- `live_check` reports: expiries found, one put quote near the money with its `delay`, whether the quote is real-time (delay 0 during the session), and the details fields present. It never prints a token.

**Tests** (respx, no network):
- `test_chain_parses_expiries_roots_and_strikes`; `test_quotes_by_ids_are_batched_by_100`; `test_filter_body_is_exact` (with and without strike bounds).
- `test_post_retries` (table): 401 then 200 with one forced refresh; 429 pauses the bucket; 5xx backs off; transport error retried; package 401 raised at once; five failures raise `QuestradeApiError`.
- `test_null_and_missing_fields_become_none`; `test_delay_missing_is_none`.
- `test_symbol_details_parse_and_chunking`; `test_ex_date_and_yield_parse`.
- `test_live_check_reports_delay_and_masks_secrets`.
- the existing Questrade client tests pass unchanged.

**Out of scope**: caching, the contract master (T3); any change to GET behaviour.

### T3. Option market data

**Goal**: the contract master, the chain cache, live option quotes and marks, and the calendar maths, behind `OptionMarketView`. **Depends on**: T1.

**Owns**: `trader/options/market.py`, `tests/options/test_market.py`.

**Interfaces**: `OptionMarketService(factory, clock, calendar, client: OptionQuoteClient, settings: Callable[[], OptionSettings], facts: FactsProvider | None = None)` implements `OptionMarketView`, plus `async refresh_chain(underlying, *, force=False) -> int` (contracts upserted), `async record_marks(contract_ids) -> int`, `async resolve_underlying(ticker) -> int` (symbol id; raises `UnknownUnderlying`), `monthly_expiry(year, month, calendar) -> date`.

**Behaviour**:
- An underlying is a `symbols` row; an unknown ticker is looked up with `symbols_by_names` and stored. No Questrade id, or `hasOptions` false: `UnknownUnderlying`.
- The chain is read from `option_chain_cache` while younger than `options.chain_cache_hours`; otherwise fetched, cached, and every contract upserted into `option_contracts`. `adjusted` is true when the multiplier is not 100 or the root differs from the ticker.
- `is_monthly`: the expiry equals the third Friday of its month, or the Thursday before when that Friday is not a session.
- `quotes` always fetches live and maps `iv = iv_pct / 100`, keeping `delta` signed. `quotes_for_expiry` uses the filter call. Neither judges staleness (the fill model does).
- `record_marks` upserts `option_quote_marks` with the underlying's last price.
- `is_open(now)`: a session day and `session_open <= now < session_close` (early closes follow the calendar; P7).
- `daily_bars` reads `daily_candles`, fetching and storing missing sessions first.
- `dte` is calendar days from the ET session date.

**Tests**:
- `test_chain_is_cached_and_refetched_after_ttl`; `test_contracts_upsert_is_idempotent`; `test_adjusted_contract_is_flagged`.
- `test_monthly_expiry` (table): normal month, Good Friday month (Thursday), a weekly, a LEAPS January.
- `test_quotes_map_iv_and_keep_signed_delta`; `test_quotes_for_expiry_uses_strike_bounds`.
- `test_unknown_or_optionless_underlying_raises`; `test_find_contract_by_key`.
- `test_record_marks_upserts_latest`; `test_is_open` (table: before open, open, at close, early close, holiday).
- `test_daily_bars_fill_gaps_once`; `test_dte_uses_the_et_date`.

**Out of scope**: facts (T8), fills, the web cache (T14).

### T4. Collateral engine

**Goal**: one pure function that accepts or rejects any order, pairs every short leg with cover, and says what to reserve (D5, FP §3.3). **Depends on**: T1.

**Owns**: `trader/options/collateral.py`, `tests/options/test_collateral.py`.

**Interfaces**: `DefaultCollateralEngine().evaluate(req, book) -> CollateralDecision` (§3.3). No I/O, no clock.

**Behaviour**, checked in this order; the first failure is the reject reason:

| Step | Rule | Reject reason |
|---|---|---|
| 1 | 1 to `options.max_legs` legs; `qty` 1 to `options.max_contracts_per_order`; one underlying; no repeated (instrument, contract, effect); a limit needs `net_limit` or `walk`; market orders allowed by setting | `invalid_order` |
| 2 | a strategy order while `options.strategies_paused` | `strategies_paused` |
| 3 | every contract is known and not past expiry | `unknown_contract`, `expired_contract` |
| 4 | a close or roll names an open, unfrozen structure of the same source | `structure_frozen`, `invalid_order` |
| 5 | each closing leg closes at most the structure's open quantity (working close orders counted) | `nothing_to_close` |
| 6 | shares being sold are not covering an open or working short call | `shares_committed` |
| 7 | after the closing legs, every remaining short leg of the structure is still covered | `not_covered_after_close` |
| 8 | every opening short leg gets one cover: a long leg of this order or structure (same right, long expiry >= short expiry, enough contracts, used once); else for a call, 100 free shares per contract held by the **same source**; else for a put, cash. A short call with no cover, or selling shares short | `naked_short` |
| 9 | every leg needed for pricing has a bid and an ask | `no_quote` |
| 10 | `free_cash_after >= 0`, where `free_cash_after = free_cash + net × 100 × qty − fees − reserve_cash`, with `net` = the limit for a limit order, the market net (buy at ask, sell at bid) otherwise | `insufficient_cash` |
| 11 | if the order raises the underlying's exposure: `exposure_after <= options.max_position_pct × account.equity` | `position_cap` |

- `reserve_cash` per unit = the largest loss at expiry of the option legs by intrinsic value, over the prices 0, every strike, and far above the highest strike, not counting premium; a share-covered call counts 0 and a cash-secured put counts strike × 100. A long leg that expires later than its short leg is valued at its intrinsic value on the short leg's expiry. This gives every row of FP §3.3: long option 0 (the order reserves the debit until it fills), cash-secured put strike × 100, covered call 0, debit spread 0, credit spread width × 100, iron condor the wider side, calendar or diagonal the wrong-side strike gap × 100.
- Exposure of an underlying = reserved cash of its open structures + cost of long options + shares at cost + reservations of working opening orders. Closing orders never fail the cap.
- `kind`: by the leg shape (single long, `csp`, `covered_call` including a buy-write, same-expiry two-leg `debit_spread` or `credit_spread` by the sign of the net, four-leg `iron_condor`, `calendar` (same strike), `diagonal`, `shares`, else `custom`). `max_loss`, `max_profit`, `breakevens` are filled for single legs, `csp`, `covered_call` and verticals; None otherwise.

**Tests** (one parametrised table each unless noted):
- `test_fp_table_rows`: the seven rows of FP §3.3, reserve and kind.
- `test_naked_rejections`: short call without shares; with shares already committed to another call; with shares of another source; spread whose long leg is the other right; long expiry before the short; short shares.
- `test_cash_rules`: put that fits exactly; one cent short; credit of a spread counted; reservation of a working order counted; market order priced at ask.
- `test_cap`: at the cap accepted; one cent over rejected; closing order over the cap accepted; assignment-sized shares counted at cost.
- `test_close_rules`: more than held; partial close leaving a short uncovered; selling covering shares; frozen structure; structure of another source.
- `test_invalid_orders`: every step-1 rule.
- `test_preview_numbers`: `csp`, long call, both verticals: max loss, max profit, breakevens, fees.
- `test_iron_condor_and_diagonal_reserve`; `test_kind_classification`.
- `test_same_input_same_output_and_no_io` (the module imports nothing from `trader.db`, `trader.adapters` or `httpx`).

**Out of scope**: persisting anything; quotes; the wheel's own cash test (WS test 7 is T9).

### T5. Fill model, book and broker

**Goal**: orders from submit to fill with conservative prices, all legs or none, the database book, the cash ledger and account valuation (D11, FP §3.2, §3.4). **Depends on**: T1.

**Owns**: `trader/options/fill_model.py`, `book.py`, `broker.py`, `valuation.py`, `tests/options/test_fill_model.py`, `test_book.py`, `test_broker.py`, `test_valuation.py`.

**Interfaces**: `QuoteFillModel` (`OptionFillModel`); `DbBook(session, run_id, ledger, clock)` (`OptionBook`); `SimOptionBroker(factory, clock, calendar, market, collateral, fill_model, settings, run_id, usd_cad_rate: Callable[[], Decimal])` (`OptionBroker`); `valuation.account_state(cash, reserved, structures, quotes, share_quotes, marks, now) -> OptionAccountState`; `valuation.close_net(structure, quotes) -> Decimal | None`.

**Behaviour**:
- Fill model, in order; the first that applies is the `NoFill` reason: `outside_hours` (not `open <= now < close`); per leg `quote_missing`; `quote_delayed` (`delay` is not 0; None counts as delayed); `halted`; `quote_stale` (`now − fetched_at > options.stale_quote_seconds`); `one_sided` (no bid or no ask); `crossed` (bid > ask); `zero_bid` (a sell leg's bid is 0). Leg price: buy at ask, sell at bid, shares legs the same from the share quote. Market: fills. Limit: fills when the market net >= `net_limit`, at the market net; else `limit_not_reached`. Fees: `options.fee_per_contract × ratio × qty` per option leg, `options.share_commission` per shares leg.
- `submit`: under the book lock, build the `CollateralBook`, evaluate, store the order (`rejected` with reason and detail, or `working` with `reserved_cash` = the decision's reserve plus any debit and fees). A walk order with no limit starts at the midpoint net rounded to the tick in its favour and sets `walk_next_at`.
- `poll(now)`: one quote fetch for every leg of every working order; orders in submit order. A fill re-runs the collateral check under the lock; if it now fails the order is cancelled with that reason. Otherwise, in one transaction: fill rows (one per leg, with the quote snapshot and the USD/CAD rate), ledger rows (`sell` or `buy` per leg, one `fee` row), positions through `DbBook.apply`, the structure created (`open`) or updated (`close`, `roll`), the structure's `reserved_cash` set from the decision, the order's own reservation released, `take_profit_net` stored, the structure closed when every position is zero (`closed`; `sold` for shares), `fill_net` and `fees` on the order. No partial fills.
- `walk(now)`: each working walk order whose `walk_next_at` has passed moves one tick toward the current market net, never past it. `reprice` by hand turns `walk` off. `cancel` and `reprice` act only on `working` orders.
- `take_profits(now)`: FP §5.1 row 3 as §3.4 describes; at most one working close order per structure.
- `expire_day_orders`: `day` orders of that session become `expired`; `gtc` orders stay. Released reservations follow.
- Valuation (WS §11 `account_value`): long options at the bid, short options at the ask, shares at the last trade; a missing live quote falls back to `option_quote_marks` and sets `marks_complete = False`. `premium_collected` = the credits of sell-to-open option fills.
- Nothing is held in memory between calls (acceptance item 8).

**Tests**:
- `test_no_fill_matrix` (table): the nine reasons, each with the smallest quote that causes it.
- `test_fill_prices` (table): single buy and sell; two-leg credit and debit; a buy-write; limit exactly at the market net fills; one cent better does not.
- `test_fees_per_contract_per_leg`.
- `test_db_book_passes_the_book_contract` (`BookContract`, `db`).
- `test_submit_stores_rejected_orders_with_reason`; `test_submit_reserves_and_poll_releases`.
- `test_multi_leg_fills_all_or_none`: one stale leg, nothing fills.
- `test_fill_writes_fills_ledger_positions_structure_in_one_transaction` (a forced failure leaves nothing).
- `test_collateral_recheck_at_fill_cancels`; `test_close_and_roll_update_the_structure`.
- `test_walk_steps_one_tick_and_stops_at_the_market`; `test_manual_reprice_stops_the_walk`.
- `test_take_profit_submits_one_close`; `test_day_orders_expire_gtc_stay`.
- `test_cancel_and_reprice_only_when_working`.
- `test_account_value_uses_liquidation_marks`; `test_missing_quote_uses_the_last_mark`.
- `test_cash_and_reserved_reconcile_to_the_ledger` after a scripted sequence.
- `test_two_brokers_on_one_run_do_not_double_fill` (the book lock).

**Out of scope**: expiry and assignment (T6); strategies; notifications; the polling loop (T12).

### T6. Lifecycle

**Goal**: what happens at expiry and on early assignment, written through `OptionBook` (D3, FP §3.5, P5, P6, P8). **Depends on**: T1.

**Owns**: `trader/options/lifecycle.py`, `tests/options/test_lifecycle.py`.

**Interfaces**: `LifecycleEngine(factory, clock, calendar, market, settings, run_id, book_factory: Callable[[Session], OptionBook], official_close: Callable[[str, date], Awaitable[Decimal | None]])` with `async run_expiry(session_date) -> list[LifecycleEvent]`, `async run_early_assignment(session_date) -> list[LifecycleEvent]`, `async check_adjustments() -> list[LifecycleEvent]`.

**Behaviour** (each structure in its own transaction under the book lock):

| Case at expiry (every open option position with expiry <= session date) | Outcome |
|---|---|
| no official close for the underlying | nothing changes; an error event; the position is retried on the next run |
| out of the money (P6: less than `options.itm_threshold` through the strike) | position closed at 0; kind `expired`; reserve released; the structure closes as `expired` when nothing is left |
| short put in the money | `assigned`: 100 shares per contract bought at the strike (`buy`), reserve released, the put structure closed as `assigned`, a new `shares` structure with the same source and config and `parent_structure_id`; `new_structure_id` on the event |
| short call in the money, covered by shares | `called_away`: the shares sold at the strike (`sell`), both structures closed as `called_away` |
| long option in the money, and the account can pay for or deliver the shares | `exercised`: shares bought (call) or sold (put) at the strike |
| any exercise or assignment that would leave short shares, or that free cash cannot pay for | settled in cash at intrinsic value against the official close (`detail.settled = "intrinsic"`); see risk R7 |
| several legs of one structure in the money | leg by leg; shares bought and sold the same night net out, so a fully in-the-money vertical ends with cash only |

- `options.assignment_fee` is charged once per assigned or exercised position.
- Early assignment (P5), run after expiry on session D: a short call covered by shares whose underlying's `next_ex_dividend_date` is the next session, in the money at D's close, with time value (last mark's midpoint minus intrinsic; no mark counts as 0) below `dividend_per_share`: kind `early_assignment`, handled as `called_away`. Off when `options.early_assignment_enabled` is false. Missing facts: no action.
- Adjustments (P8): a held contract that is `adjusted`, or no longer in a freshly fetched chain before its expiry: the structure is frozen, one `frozen` event and one alert. A frozen structure is skipped by expiry handling.
- Idempotent: `record_lifecycle` returns None for an event that exists, and then nothing else is done for that position.

**Tests** (`FakeBook`, `FakeOptionMarket`; table where a table exists):
- `test_expiry_outcomes` (table): the seven rows above.
- `test_itm_threshold_edges` (table): exactly at the strike, 0.01 through, 0.009 through, both rights.
- `test_assignment_creates_shares_structure_at_strike_and_releases_reserve`.
- `test_called_away_closes_both_structures_and_moves_cash`.
- `test_vertical_both_legs_itm_ends_in_cash_only`; `test_credit_spread_short_leg_only_itm`.
- `test_long_call_exercise_or_cash_settle_by_free_cash`.
- `test_assignment_fee_charged_once`; `test_missing_close_changes_nothing_and_is_retried`.
- `test_second_run_is_a_no_op`; `test_expired_last_week_is_caught_up`.
- `test_early_assignment` (table): fires; time value above the dividend; ex-date two sessions out; out of the money; disabled; no facts.
- `test_adjusted_contract_freezes_once_and_is_skipped`.

**Out of scope**: telling strategies or Telegram (T7 host, T13); getting the official close (T13 supplies the function).

### T7. Strategy registry, state and host

**Goal**: find plug-ins by entry point, keep their versioned settings and state, and run their hooks: build the context, turn intents into orders, record notes, alerts and prompts. **Depends on**: T1.

**Owns**: `trader/option_strategies/registry.py`, `state.py`, `host.py`, `tests/option_strategies/__init__.py`, `test_registry.py`, `test_state.py`, `test_host.py`.

**Interfaces**: `available()`, `load_plugin(name)`, `load_all()`, `PluginError`; `OptionStrategyRegistry(factory, clock, plugins=None)` with `keys`, `plugin_class`, `json_schema`, `ensure_defaults(actor="system")`, `current`, `update`, `instance`, `enabled`, `config_ids`, `config_key` (same meaning as the stock `StrategyRegistry`, without the replay scope); `DbStrategyState(factory, clock, strategy_key)`; `DefaultStrategyHost(factory, clock, calendar, registry, broker, market, prompts, settings, run_id: Callable[[], int | None], alert_sink: Callable[[str, str, str, str], Awaitable[None]])` (`StrategyHost`).

**Behaviour**:
- Registry: as `trader/strategies/registry.py` (advisory lock per key, a new revision and an audit row `option_strategy.update:<key>` per change, a broken plug-in logged and skipped). A plug-in must have every attribute and hook of §3.4 and a key equal to its entry-point name.
- `fire(strategy_key, event)`: runs inside `run_job_async` with job name `opt_event:<strategy>:<key>` and the event's session date, so an event fires once per session whoever asks (worker or cron); `force` re-runs it. Skipped with a note when `options.strategies_paused`, when the plug-in is disabled, or when there is no options run.
- Context: the plug-in sees only its own open structures and working orders, the whole account state, its own state store and its own prompts.
- Intents to orders: `OpenStructure` → `OrderRequest` (`intent=open`, legs resolved with `market.find_contract`, all effects `open`); `CloseStructure` → the structure's open positions reversed, effects `close`; `RollStructure` → those closing legs plus the new opening legs, `intent=roll`; `SellShares` → a market sell of the shares structure; `Reprice`, `CancelOrder` → the broker calls. An intent naming a structure or order of another source is dropped with an error event. An unknown contract or a rejected order is recorded as a note and does not stop the other intents.
- Notes → `event_log` (level as given, source `options.strategy.<key>`, the options run id). Alerts → an `event_log` warning and `alert_sink(source, kind, message, dedupe_key)`.
- `deliver_fill`, `deliver_lifecycle`: only to the plug-in whose key is the source; lifecycle events get `delivered_at`. `deliver_answers`: every answered, undelivered prompt of an enabled plug-in → `on_answer` → `mark_delivered`. A hook that raises is logged as an error event; the item stays undelivered and is retried on the next call.
- `sync_prompts`: `prompts(ctx)` of every enabled plug-in → `PromptStore.ensure`; a pending prompt of that source that is no longer requested is cancelled.
- `due_events(session_date, now)`: scheduled events whose time has passed and whose job has not succeeded for that session.
- Intents returned by any hook are applied the same way. `panel` and `action` build the same context and never submit orders.

**Tests**:
- `test_plugins_found_through_the_entry_point` (patched `entry_points`, the toy plug-in); `test_broken_or_mislabeled_plugin_is_skipped_and_logged`.
- `test_ensure_defaults_revision_1_and_version_bump`; `test_update_validates_audits_and_is_a_no_op_when_equal`.
- `test_state_store_get_put_delete_and_isolation_by_key`.
- `test_fire_runs_once_per_session_and_force_reruns`; `test_fire_skips_when_paused_disabled_or_no_run`.
- `test_context_holds_only_own_structures_and_orders`.
- `test_intent_mapping` (table): the six intents to requests or broker calls.
- `test_foreign_structure_intent_is_dropped`; `test_rejected_order_becomes_a_note_and_others_continue`.
- `test_notes_and_alerts_are_recorded_and_sent`.
- `test_fills_and_lifecycle_reach_only_the_owner`; `test_answers_delivered_once_and_retried_after_a_failure`.
- `test_sync_prompts_upserts_and_cancels`; `test_due_events_respect_job_runs`.
- `test_a_raising_hook_does_not_stop_other_plugins`.

**Out of scope**: the wheel; polling (T12); the registry's API view (T14 uses the protocol).

### T8. Underlying facts

**Goal**: the WS §3.1 fields per ticker and day, each with its source and age (FP §4.2). **Depends on**: T1.

**Owns**: `trader/options/facts.py`, `trader/adapters/finviz/fundamentals.py`, `trader/adapters/finviz/scraper.py` (one additive public method `snapshot(ticker, today_et) -> dict[str, str]`), `tests/options/test_facts.py`, `tests/adapters/test_finviz_fundamentals.py`, a fixture page under `tests/fixtures/finviz/`.

**Interfaces**: `fundamentals.parse_snapshot(html) -> dict[str, str]` (label → text of the quote page's snapshot table); `fundamentals.to_fundamentals(raw, today) -> FinvizFundamentals` (`eps_growth_yoy`, `debt_to_equity`, `book_value_per_share`, `payout_ratio`, `short_float`, `next_earnings_date`, `rsi14`, `market_cap_usd`, all optional); `FactsService(factory, clock, calendar, client: OptionQuoteClient, snapshots: Callable[[str, date], dict[str, str]], bars: Callable[[str, date, date], Awaitable[list[Candle]]], settings)` (`FactsProvider`); `classify_security(details: QtSymbolDetails) -> str`; `trend_inputs(bars, *, sma_period=50, slope_lookback=20, low_lookback_sessions=252) -> TrendInputs`.

**Behaviour**:

| Field | Source | Rule |
|---|---|---|
| `eps_ttm`, `market_cap_usd`, `dividend_per_share`, `next_ex_dividend_date`, `dividend_yield`, `sector`, `has_options` | Questrade `symbol_details` | `yield` ÷ 100; an ex-date in the past → None |
| `eps_growth_yoy`, `debt_to_equity`, `book_value_per_share`, `payout_ratio`, `short_float`, `next_earnings_date`, `rsi14` | FinViz snapshot labels `EPS Y/Y TTM`, `Debt/Eq`, `Book/sh`, `Payout`, `Short Float`, `Earnings`, `RSI (14)` | percentages → fractions; `-` → None; the earnings date through the existing `parse_earnings`. **ASSUMPTION**: label names, checked live at T17 |
| `sma50`, `sma50_prior`, `low_52w`, `sessions_since_52w_low` | daily bars (260 sessions) | `trend_inputs` with the default look-backs; fewer bars than needed → None |
| `price` | Questrade quote, last trade | |
| `security_type` | Questrade `securityType` and description | `Stock` → `STOCK`; a fund whose description holds a leveraged or inverse keyword (`2x`, `3x`, `ultra`, `leveraged`, `inverse`, `short`, `bear`) → `LEVERAGED_OR_INVERSE_ETF`; a fund in the broad-index list (SPY, IVV, VOO, VTI, QQQ, IWM, DIA) → `BROAD_INDEX_ETF`; any other fund → `SECTOR_ETF`. **ASSUMPTION** (FP §4.2) |

- One row per (symbol, session date), upserted; `sources` records source and fetch time per field. A source that fails leaves its fields None and the others stored; `refresh` returns the error text for a ticker only when every source failed.
- `get` returns the newest row not older than `options.facts_max_age_hours`, else None (the wheel then reports `MISSING_DATA`).
- FinViz politeness is the scraper's own (2 s, cache).

**Tests**:
- `test_parse_snapshot_fixture`; `test_to_fundamentals` (table: percent, dash, negative, `B`/`M` market caps, earnings `Oct 22 AMC`).
- `test_blocked_or_changed_page_raises_finviz_error`.
- `test_refresh_merges_three_sources_with_sources_map`; `test_one_source_down_keeps_the_rest`; `test_all_sources_down_returns_the_error`.
- `test_trend_inputs` (table: rising, flat, falling, a new low 5 sessions ago, too few bars).
- `test_classify_security` (table: stock, SPY, a sector fund, a 3x fund, an inverse fund).
- `test_get_respects_max_age`; `test_refresh_is_idempotent_per_day`; `test_past_ex_date_is_none`.
- the existing FinViz tests pass unchanged.

**Out of scope**: owner overrides and the screen itself (T9, T11); `parser.py`.

### T9. Wheel rules (pure)

**Goal**: WS §4 to §12 as pure functions with every number read from the params model. **Depends on**: T1.

**Owns**: `trader/option_strategies/wheel/__init__.py`, `config.py`, `inputs.py`, `screen.py`, `select.py`, `evaluate.py`, `stops.py`, `calc.py`, `alerts.py`, `tests/option_strategies/wheel/__init__.py`, `test_ws13_cases.py`, `test_screen.py`, `test_select.py`, `test_evaluate.py`, `test_stops_calc_alerts.py`, `test_config.py`.

**Interfaces**:
- `WheelParams` (pydantic): one field per WS §2 key, named `<section>_<key>` with sections `screen`, `entry`, `manage`, `cc` (covered call), `stops`; defaults as WS §2 except `entry_per_ticker_limit_pct_of_wheel_cash = 0.50` (P1) and `stops_benchmark_ticker = None` (None = use `options.benchmark_ticker`, P3). Every WS line marked ASSUMPTION has a field description starting `ASSUMPTION:`. Extra fields: `screen_cash_caution_remaining_pct = 0.05` (ASSUMPTION, WS test 7), `stops_drawdown_review_days = 30`, `screen_ror_dte_min = 30`, `screen_ror_dte_max = 45`, `entry_target_dte = 45`, `daily_event_offset = "open+60m"`, `walk_orders = True`, `market_screen_filters` (FinViz filter list), `market_screen_max_candidates = 10`.
- `inputs.py`: `OwnerInputs(would_own, ownership_reason, thesis_broken, acknowledged: frozenset[str])`, `OptionRow(contract: ContractKey, bid, ask, delta, iv, open_interest, is_monthly, dte)`, `AccountInputs(cash_usd, wheel_cash_usd, open_put_collateral_usd, new_positions_paused)`, `PutPosition`, `SharesPosition`, `CallPosition` (the WS §6 state with premiums, strike, expiry, contracts, roll count, net cost, fresh-cash answer and date, last review date).
- `screen.run(facts: UnderlyingFacts, owner, account, expiries: Sequence[ExpiryInfo], puts_by_expiry: Mapping[date, Sequence[OptionRow]], today, market_open: bool, cfg) -> ScreenResult(verdict, tests: tuple[TestResult(number, name, status, reason), ...], flags, expiry, reference, chosen, target_delta, breakeven, stale: bool)`; `screen.rank(results) -> list`.
- `select.expiry(...)`, `select.put(...)`, `select.call(net_cost, calls, expiries, next_earnings_date, today, cfg) -> CallSelection(action, row, reason)`.
- `evaluate.business_check(facts, owner, cfg) -> BusinessCheck(passes, failed)`; `evaluate.put(pos, facts, quote: OptionRow, next_monthly_puts, biz, today, cfg) -> Action`; `evaluate.shares(pos, facts, calls, expiries, biz, today, cfg) -> Action`; `evaluate.call(pos, facts, quote, biz, today, cfg) -> Action`. `Action(kind: ActionKind, reason, candidate: OptionRow | None, detail)`; `ActionKind` holds every action name of WS §8 and §9 plus `ROLL_PUT`, `RUN_FRESH_CASH_TEST`, `DRAWDOWN_REVIEW`, `PIN_RISK`, `REVIEW_BEFORE_EARNINGS`, `EARLY_ASSIGNMENT_RISK`.
- `stops.can_open(ticker_has_position, account, collateral, cfg) -> str | None`; `stops.benchmark_review(benchmark_return, account_return, cfg) -> bool`; `calc.*` one function per WS §11 line; `alerts.detect(previous, current, cfg) -> list[Alert(kind, message)]` for the eight WS §12 alerts.

**Behaviour**: exactly WS §3.5 to §12. Verdicts, test statuses, reasons `MISSING_DATA` and `NOT_APPLICABLE`, and action names are the spec's strings. The modules import only the standard library, pydantic, `trader.options.types` and each other (a static test checks it).

**Tests**:
- `test_ws13_cases` (table, ids `ws13_01` to `ws13_16`): the sixteen acceptance cases.
- `test_hard_tests` (table): tests 1 to 7, a PASS, a CAUTION and a FAIL row each, the notes of WS §4.2 included (relaxed sectors, ETF `NOT_APPLICABLE`, test 3 depending on test 2).
- `test_soft_tests` (table): tests 8 to 10, with the RoR scaling outside 30 to 45 DTE.
- `test_disqualifiers_and_verdict_ladder` (table): three disqualifiers, six verdict rows.
- `test_missing_data_is_caution_never_pass` (table: one row per nullable input).
- `test_expiry_selection` (table: WS §5.1 steps 1 to 4); `test_strike_selection` (table: §5.2 steps 1 to 4).
- `test_put_evaluation_rows` (table: §8 rows 1 to 8 and first-match order); `test_roll_branches` (table: §8.1).
- `test_call_selection_branches` (table: §9.3, with the liquidity removal); `test_shares_rows` (§9.4); `test_call_rows` (§9.5).
- `test_stop_rules` (table: WS §10); `test_calculations` (table: WS §11); `test_alerts` (table: WS §12).
- `test_ranking_never_uses_premium`; `test_stale_flag_when_market_closed`.
- `test_every_threshold_moves_a_result` (table: one row per numeric field of `WheelParams`: a scenario at the boundary flips when the field changes).
- `test_assumptions_are_labelled`; `test_rules_are_pure` (static imports).

**Out of scope**: any I/O, orders, prompts, tables (T11).

### T10. Owner prompts and option messages

**Goal**: store the questions for Stephen, send them with signed Telegram buttons, accept answers from Telegram and the web, repeat daily; and render every option message. **Depends on**: T1.

**Owns**: `trader/options/prompts.py`, `messages.py`, `trader/adapters/telegram/types.py`, `callbacks.py`, `bot.py` (additive), `tests/options/test_prompts.py`, `test_messages.py`, `tests/adapters/test_telegram_prompt_callback.py`.

**Interfaces**: `DbPromptStore(factory, clock)` (`PromptStore`); `PromptSender(store, notifier, issuer, renderer, chat_id, settings, clock)` with `async send_due(now) -> int`; `OptionMessages(public_base_url)` (`OptionRenderer`). Telegram: `CallbackKind` gains `prompt`; code `o`; ref = the prompt id (digits); actions = the choice codes `a r y n c h o b s k`.

**Behaviour**:
- Choice codes: `a` approve, acknowledge or accept; `r` reject; `y` yes; `n` no; `c` close; `h` hold; `o` roll; `b` buy to close; `s` sell; `k` skip; `w` write on the web. When `needs_text` is true, choices `a` and `w` need a non-empty text and are shown in Telegram as a link to `/options?prompt=<id>`, not as a button.
- `ensure`: insert by `dedupe_key`, or return the existing row unchanged. `answer`: `unknown` (no row), `already` (not pending), `invalid_choice`, `text_required`, else `ok` with the answer stored; the row lock makes two answers race safely.
- `send_due`: a pending prompt never sent, or last sent at least `options.prompt_repeat_hours` ago (P9): issue a nonce (no expiry), send one message of kind `alert` with dedupe key `opt:prompt:<id>:<n>`, then `mark_sent`. Nothing is sent when Telegram is not configured.
- Bot: a `prompt` tap calls `DbPromptStore(self.factory, self.clock).answer(..., via="telegram", actor="telegram:<id>")`, answers the tap with the chosen label, "Already answered", "Unknown prompt" or "Answer this one on the Options page", and removes the buttons. A failure releases the nonce. The `proposal`, `pause` and `journal` branches are unchanged.
- Messages: HTML-escaped, times in Mountain Time, existing `MessageKind` values only (`fill` for fills, `alert` for lifecycle, alerts and prompts, `daily_summary` for the summary). Dedupe keys `opt:fill:<order id>`, `opt:life:<event id>`, `opt:alert:<key>`, `opt:summary:<date>`. The summary's first line is the account value.

**Tests**:
- `test_ensure_is_idempotent_by_dedupe_key`; `test_answer_outcomes` (table: the five statuses); `test_two_answers_one_wins`.
- `test_text_choices_need_text_and_are_links_in_telegram`.
- `test_send_due_first_time_then_after_the_repeat_interval`; `test_answered_or_cancelled_prompts_are_not_sent`.
- `test_undelivered_and_mark_delivered`; `test_cancel_by_key`.
- `test_signer_accepts_prompt_data_and_rejects_bad_actions`; `test_prompt_data_fits_64_bytes`.
- `test_bot_prompt_tap_answers_and_removes_buttons`; `test_bot_second_tap_says_already_answered`; `test_bot_failure_releases_the_nonce`.
- the existing Telegram bot and callback tests pass unchanged.
- `test_messages` (table: fill single leg, fill spread, assigned, called away, expired, alert, prompt with buttons, summary): text and dedupe key; `test_dynamic_text_is_escaped`.

**Out of scope**: which prompts exist (the plug-ins decide); the web prompt UI (T15); `runtime.py`.

### T11. Wheel plug-in

**Goal**: the wheel as a plug-in: gather inputs, call T9, turn actions into intents (FP §5.1), prompts (FP §5.2), its tables, its panel, and the market-wide candidate screen (D8). **Depends on**: T7, T9, T10 (fakes for T3, T5, T8).

**Owns**: `trader/option_strategies/wheel/strategy.py`, `store.py`, `panel.py`, `screener.py`, `tests/option_strategies/wheel/test_strategy.py`, `test_store.py`, `test_panel.py`, `test_screener.py`.

**Interfaces**: `WheelStrategy(params: WheelParams)`: key `wheel`, version `1.0.0`, `manual_events = ("screen",)`, `schedule` = one event `opt_daily` at `params.daily_event_offset`. `store.WheelStore(factory, clock, run_id)` for the three `wheel_*` tables. `screener.market_candidates(screen: Callable[[str], list[str]], params, known: set[str]) -> list[str]`.

**Behaviour**:
- `opt_daily`, in this order: (1) every open wheel position is evaluated once per session (the `wheel_events` unique key) and its action applied; (2) alerts (WS §12) from yesterday's and today's inputs; (3) for approved tickers with no open wheel position, when not paused: screen, store the verdict, and open in rank order (WS §4.5) while WS test 7 still passes.
- Inputs: `cash_usd` and `wheel_cash_usd` = `ctx.account.cash`; `open_put_collateral_usd` = `ctx.account.reserved` (every reservation in the pool, manual ones included: FP §3.7); owner inputs and the security-type override from `wheel_tickers`; trend inputs recomputed from `market.daily_bars` when the look-back params differ from the defaults.
- "No open position in this ticker" (WS §5, §10) means no open **wheel** position (FP §9.5); a manual position on the same ticker does not block.

| Action | Intent or effect |
|---|---|
| open a put | `OpenStructure` (one short put, limit, `walk=params.walk_orders`, `take_profit_pct = manage_profit_target_pct_of_premium`, `tif=day`); evidence = the screen result |
| `CLOSE_PUT_PROFIT`, `CLOSE_PUT_TIME`, `CLOSE_PUT_BEFORE_EARNINGS`, `CLOSE_PUT_NOW`, `CLOSE_CALL_PROFIT`, `CLOSE_OR_EXPIRE_CALL` | `CloseStructure` (limit, walk) |
| `ROLL_PUT` | `RollStructure` with a `net_limit` above 0 (a roll is always a credit) |
| `SELL_CALL` | `OpenStructure` (one short call; the collateral engine pairs it with the wheel's shares) |
| `SELL_SHARES` | `SellShares`; `CLOSE_CALL_AND_SELL_SHARES`: close the call now, sell the shares from `on_fill` |
| `TAKE_ASSIGNMENT`, `HOLD_FOR_CALL_AWAY`, `HOLD`, `HOLD_UNCOVERED`, `SKIP_CALL_THIS_CYCLE` | no order; a `wheel_events` row with the reason |
| `RUN_FRESH_CASH_TEST`, `DRAWDOWN_REVIEW`, `PIN_RISK`, `REVIEW_BEFORE_EARNINGS`, `EARLY_ASSIGNMENT_RISK`, `NEEDS_REVIEW` cautions, new candidates | prompts, below |

| Prompt kind (dedupe key) | Choices | Until answered (P9) |
|---|---|---|
| `candidate` (`wheel:cand:<ticker>`) | `a` approve (text: why would you own it), `r` reject | not traded |
| `ack_caution` (`wheel:ack:<ticker>:<test>`) | `a` acknowledge (no text), `k` skip | no entry |
| `fresh_cash` (`wheel:fresh:<position>:<date due>`) | `y`, `n`; the ten test results in the body | no call sold; `n` → `SELL_SHARES` |
| `review_before_earnings` (`wheel:earn:<position>:<earnings date>`) | `c` close, `h` hold, `o` roll | hold |
| `pin_risk` (`wheel:pin:<position>`) | `b` buy to close, `a` accept | accept |
| `drawdown_review` (`wheel:dd:<position>:<date due>`) | `w` written reason (text), `s` sell | repeated daily |

- `ack_caution` uses `needs_text = False`. `on_answer` applies the answer the same day when the market is open, else on the next `opt_daily`.
- `on_fill`: put sold → a `wheel_positions` row (`PUT_OPEN`, the WS §5.4 record with the USD/CAD rate); put closed → `NONE`; roll filled → `roll_count + 1`, premium += the net credit; call sold → `CALL_OPEN`; call closed → `SHARES_HELD`; shares sold → `NONE` with the loss recorded.
- `on_lifecycle`: put `expired` → `NONE`; `assigned` → `SHARES_HELD`, `net_cost` per WS §9.1, fresh-cash test due today; call `expired` → `SHARES_HELD`; `called_away` or `early_assignment` → `NONE` with `full_cycle_result` (WS §11), and the ticker goes back to status `candidate` so that a new put needs a new approval (WS §9.6).
- Event `screen` (Saturday): `market_candidates` → new `wheel_tickers` rows (status `candidate`, origin `screen`), each screened and offered with a `candidate` prompt showing verdict and tests. At most `market_screen_max_candidates` new ones per run, ranked per WS §4.5.
- Event `postclose`: on the last session of a quarter, the WS §10 benchmark rule with `ctx.equity_on` and `market.daily_bars`; a trigger sets `new_positions_paused` (state scope `account`) and raises `BENCHMARK_REVIEW_DUE`.
- Panel: summary (positions by state, paused, cash, benchmark); tables `positions` (state, strike, expiry, net cost, roll count, next action and why), `tickers` (status, owner inputs, last verdict; row detail = the ten tests), `candidates`. Actions: `add_ticker` (text), `approve` (text), `reject`, `set_reason` (text), `thesis_broken` (toggle), `security_type` (choice), `ack_caution`, `rescreen`, `clear_pause` (confirm).
- Quotes while the market is closed are marked stale ("verify live") and never block (WS §3.5).

**Tests** (`FakeOptionMarket`, `FakeOptionBroker`, `FakePromptStore`, `db` for the wheel tables):
- `test_daily_opens_a_put_for_a_qualified_approved_ticker` (intent fields, evidence).
- `test_no_entry` (table: not approved, wheel position open, paused, unacknowledged caution, cash test fails, pending candidate prompt).
- `test_manual_position_on_the_ticker_does_not_block`; `test_manual_reservations_count_as_collateral`.
- `test_action_to_intent` (table: every row of the action table).
- `test_evaluated_once_per_session`.
- `test_fill_and_lifecycle_transitions` (table: the WS §6 edges).
- `test_net_cost_and_full_cycle_result_recorded`; `test_called_away_ticker_needs_new_approval`.
- `test_prompts_requested` (table: the six kinds, keys and choices); `test_answers_applied` (table: each choice).
- `test_unanswered_prompt_changes_nothing` (P9).
- `test_roll_is_net_credit_and_counts`; `test_fresh_cash_retest_after_30_days`.
- `test_saturday_screen_adds_capped_ranked_candidates`; `test_quarter_end_review_pauses_and_alerts`.
- `test_panel_tables_and_actions` (table: each action changes the right row, with audit fields).
- `test_stale_quotes_flagged_not_blocking`; `test_alerts_raised_once_per_day`.

**Out of scope**: the rules themselves (T9); the Saturday cron line (T16); anything in `trader/options`.

### T12. Options worker

**Goal**: the always-on process: poll quotes for working orders, fill, walk, mark, take profits, alert, fire strategy events, deliver answers, send prompts, beat. **Depends on**: T3, T5, T7, T10.

**Owns**: `trader/options/worker.py`, `trader/options/__main__.py`, `tests/options/test_worker.py`.

**Interfaces**: `OptionsWorkerDeps` (dataclass: `factory`, `engine`, `clock`, `calendar`, `settings`, `run_id`, `broker`, `record_marks`, `host`, `prompt_sender`, `notifier`, `renderer`, `sleep`); `OptionsWorker(deps)` with `async step() -> StepReport` and `async run(stop, *, once=False)`; `main(argv=None) -> int` (builds its deps with `trader.options.runtime.build_worker_deps`); `async run_event_cli(host, *, strategy, key, session_date, force, due) -> int`. Constants: lock name `trader.options_worker`, heartbeat process `options-worker`, exit codes 1 (setup failed), 2 (lock held, after sleeping 30 s), 3 (lock lost), 4 (the options run changed).

**Behaviour**:

| When | Step |
|---|---|
| every step | check the single-instance lock; read the active options run (none: phase `idle`, nothing else) |
| once per start | `host.ensure_defaults()` |
| in the session, every `options.quote_poll_seconds` | `host.due_events` → `host.fire`; `broker.poll` → for each fill: the fill message, then `host.deliver_fill` |
| every `options.reprice_seconds` | `broker.walk` |
| every `options.mark_seconds` | `record_marks` for every open contract; `broker.take_profits`; strike-touch alerts; an `equity_snapshots` row every `options.snapshot_seconds` |
| every step, in and out of the session | `host.deliver_answers`; `host.sync_prompts`; `prompt_sender.send_due`; heartbeat |
| first step after the close | `broker.expire_day_orders` |
| out of the session | one step every 30 s |

- Strike touched: an open short put whose underlying last trade is at or below the strike, or a short call at or above it: one alert per structure per session (dedupe key), when `options.strike_touch_alerts`.
- Each part runs on its own: a failure is logged as an event and the other parts still run; ten failures of one part in a row raise one critical event.
- Nothing needed after a restart is kept in memory: timers restart from "due now", fills and messages are protected by database keys.
- The snapshot row: `equity` = account value, `cash`, `settled_cash` = cash, `peak_equity` and `drawdown_pct` from the run's earlier snapshots.

**Tests** (fakes and `FixedClock`; `db` for the lock, heartbeat and snapshots):
- `test_idle_without_an_options_run`; `test_second_worker_gets_exit_2`; `test_lost_lock_stops_with_exit_3`.
- `test_session_step_order` (recording fakes): events, poll, deliver.
- `test_fill_is_notified_once_and_delivered`; `test_walk_and_marks_follow_their_intervals`.
- `test_take_profit_and_strike_touch_once_per_session`.
- `test_answers_and_prompts_run_outside_the_session`.
- `test_day_orders_expire_at_the_close_including_early_close`.
- `test_a_failing_part_does_not_stop_the_others`; `test_ten_failures_raise_one_critical_event`.
- `test_heartbeat_row_and_phases`; `test_snapshot_spacing_peak_and_drawdown`.
- `test_restart_mid_session_continues_without_duplicates`.
- `test_run_event_cli_due_and_named`.

**Out of scope**: `supervisord.conf` and composing the deps (T16); the stock worker.

### T13. Jobs

**Goal**: the morning refresh and the post-close job. **Depends on**: T6, T7, T8, T10.

**Owns**: `trader/jobs/options_refresh.py`, `trader/jobs/options_postclose.py`, `tests/jobs/test_options_refresh.py`, `tests/jobs/test_options_postclose.py`.

**Interfaces**: `RefreshDeps`, `PostcloseDeps` (dataclasses of the protocols they use); `async refresh_job(deps, session_date, *, force) -> JobOutcome` (job `options_refresh`); `async postclose_job(deps, session_date, *, force) -> JobOutcome` (job `options_postclose`); `async official_close(client, market, underlying, session_date) -> Decimal | None`.

**Behaviour**:
- Both run through `run_job_async`, do nothing on a day that is not a session, and skip with reason `no options run` when there is none.
- Refresh (08:15 ET): underlyings = `host.watch_underlyings()` ∪ underlyings of open structures ∪ `options.watchlist` ∪ the benchmark. Per underlying: facts, chain, daily bars up to 260 sessions; then `lifecycle.check_adjustments()`. One ticker failing is counted in the detail; the job fails only when all fail.
- Post-close (16:20 ET), each step safe to repeat: (1) `expire_day_orders`; (2) `run_expiry` with `official_close` (the share quote's regular-hours last trade, else the day's daily candle; see risk R8); (3) `run_early_assignment`; (4) each lifecycle event: its message, then `host.deliver_lifecycle`; (5) marks, account state, one `equity_snapshots` row at the session close; (6) `host.fire(key, OptionEvent("postclose", ...))` for every enabled plug-in; (7) `host.sync_prompts`, `prompt_sender.send_due`; (8) the summary message.
- A position whose close is missing makes the job `failed` after the other steps ran, so the in-process retry and a later manual run pick it up.

**Tests**:
- `test_refresh_union_of_underlyings`; `test_refresh_counts_failures_and_fails_only_when_all_fail`; `test_refresh_runs_adjustment_check`.
- `test_not_a_session_and_no_run_are_skips`.
- `test_postclose_step_order` (recording fakes); `test_postclose_twice_changes_nothing_more`.
- `test_official_close_prefers_regular_last_then_candle`.
- `test_lifecycle_events_are_messaged_and_delivered_once`.
- `test_snapshot_written_at_session_close`; `test_summary_sent_once_per_day`.
- `test_missing_close_fails_the_job_after_other_steps`; `test_postclose_event_reaches_every_enabled_plugin`.

**Out of scope**: `docker/crontab` and its test (T16); the CLI declarations (T1).

### T14. API

**Goal**: the routes of §3.9 over `OptionApiServices`. **Depends on**: T1 (fakes for everything else).

**Owns**: `trader/api/routers/options.py`, `trader/api/options_views.py`, `tests/api/test_options_account.py`, `test_options_trade.py`, `test_options_orders.py`, `test_options_prompts.py`, `test_options_strategies.py`, `test_options_settings.py`.

**Interfaces**: `router` (prefix `/options`, tag `options`); view builders in `options_views.py` (types to schemas). The router is not added to `ROUTERS` here (T16 does it); tests mount it with `make_client(services, router)`.

**Behaviour**:
- Every route reads `services.options`; None → 503. No active options run → 409 `no_options_run`, except `/settings` and `/strategies` (GET and PUT).
- `/account`: `broker.account()`, per-source results from the structures, the benchmark from `market.daily_bars` since the run started, `worker_beat_at` from the heartbeat row.
- `/chain`: expiries and the underlying price. `/chain/quotes`: one row per strike with both sides, cached per (underlying, expiry) for `options.web_quote_cache_seconds`; `stale` = older than `options.stale_quote_seconds` or the market is closed.
- Preview and submit build an `OrderRequest` with source `manual` and `submitted_by = web:<username>`; submit never fills (the worker does). Reprice and cancel only for `manual` orders; a strategy's order → 409.
- `/activity`: fills, lifecycle events, prompts and the options run's `event_log` rows (sources `options.*`), merged, newest first, with a stable string id per item.
- `/strategies`: as the stock `/strategies` route (fields from `model_fields_out`, 422 through `safe_field_message`). Panel and action through `services.options.host`.
- `/settings`: one `SettingOut` per `OptionSettings` field, group `Options`, through `model_fields_out(OptionSettings, by_alias=True)` and `OptionSettingsStore`.
- Prompt answer: `ok` → 200; `already` → 409; `unknown` → 404; `invalid_choice`, `text_required` → 422 without echoing the text.

**Tests** (`make_client`, `FakeOptionMarket`, `FakeOptionBroker`, `FakePromptStore`, `RecordingHost`):
- `test_every_route_needs_login_and_writes_need_csrf` (table over the 18 routes).
- `test_503_without_option_services`; `test_409_without_an_options_run_except_settings_and_strategies`.
- `test_account_numbers_and_by_source`; `test_positions_open_and_closed`.
- `test_chain_expiries`; `test_chain_quotes_rows_cache_and_stale_flag`; `test_unknown_underlying_404`.
- `test_preview_shows_the_collateral_verdict`; `test_submit_working_and_rejected_orders`; `test_submit_validation_422` (table).
- `test_orders_working_and_history`; `test_cancel_and_reprice_rules` (table: working, filled, strategy-owned, unknown).
- `test_activity_merges_and_orders_newest_first`.
- `test_prompts_list_and_answer_outcomes` (table).
- `test_strategies_list_update_and_validation`; `test_panel_and_action_pass_through`.
- `test_settings_list_put_and_bounds`; `test_unknown_setting_404`.
- `test_no_response_echoes_an_invalid_input`.

**Out of scope**: `ROUTERS`, `services.py`, anything in `web/`.

### T15. Web

**Goal**: the Options page with seven tabs, the ticket, the generic strategy panel and the prompts. **Depends on**: T1 (types and route table).

**Owns**: `web/src/pages/Options.tsx`, `web/src/pages/options/*` (`AccountTab`, `PositionsTab`, `TradeTab`, `ChainTable`, `Ticket`, `OrdersTab`, `StrategiesTab`, `PanelView`, `ActivityTab`, `SettingsTab`, `PromptsBanner`, `options.css`, their tests), `web/src/api/optionsClient.ts`, `optionsHttp.ts`, `web/src/test/optionsFakeApi.ts`, `optionsFixtures.ts`, and the T15 row of the allow-list.

**Interfaces**: `OptionsApiClient` with one method per route of §3.9: `optAccount`, `optPositions`, `optChain`, `optChainQuotes`, `optPreview`, `optSubmit`, `optOrders`, `optCancel`, `optReprice`, `optActivity`, `optPrompts`, `optAnswerPrompt`, `optStrategies`, `optPutStrategy`, `optPanel`, `optPanelAction`, `optSettings`, `optPutSetting`; `OPTIONS_API_METHODS`; `OptionsApiProvider`, `useOptionsApi`; `createOptionsHttpClient` in `optionsHttp.ts`, written with the same `get`/`post`/`put` helpers and the same source shape as `http.ts` (T16 points the route-contract test at it). Route `/options` (deep links `?tab=`, `?prompt=`); nav item "Options" after "Dashboard".

**Behaviour**:

| Tab | Shows and does |
|---|---|
| Account | account value as the headline; cash, reserved, free cash; premium collected as a secondary figure; per-source table; benchmark; a warning when marks are incomplete or the worker heartbeat is older than 2 minutes |
| Positions | structures grouped by underlying with legs, quantity, entry, mark, P&L, DTE, delta, reserve, source; Close opens the ticket prefilled with the closing legs |
| Trade | underlying box → expiries → chain (calls left, puts right; bid, ask, last, delta, IV, open interest, volume). Clicking an ask adds a buy leg, a bid a sell leg. Ticket: legs, quantity, market or limit, day or GTC, walk; shows net debit or credit in words, and from the preview: max loss, breakevens, reserve, cash after, and the verdict with its reject reason in plain words. Submit is disabled while the preview is rejected or out of date |
| Orders | working orders with cancel and reprice (manual ones only); history with each leg's fill quote |
| Strategies | one section per plug-in from `optStrategies`: enabled switch, the settings form from `fields` (ASSUMPTION descriptions visible), and its panel drawn by `PanelView` from `OptPanelOut` alone (no strategy name in the web code) |
| Activity | the merged feed with kind filters |
| Settings | the `options.*` settings, reusing `FieldInput` |

- `PromptsBanner` on every tab: pending prompts with their choices; a text box when the choice needs text.
- Refresh by polling (react-query `refetchInterval`): account, positions and orders every 5 s, chain quotes every 5 s on the open Trade tab, the rest every 30 s; all paused while the page is hidden. No new SSE topic (risk R10).
- Net prices are signed in the API and never shown signed: "credit 0.45", "debit 1.20". Money, dates and times use `lib/format`; times in Mountain Time.
- Touch targets and the mobile layout follow the existing pages.

**Tests** (Vitest, `optionsFakeApi`):
- `OptionsPage`: tab switching and deep links; the route is behind login; the nav item exists.
- `AccountTab`: headline is the account value; incomplete-marks and stale-worker warnings.
- `PositionsTab`: grouping; Close prefills the ticket with reversed legs.
- `ChainTable`: bid click adds a sell leg, ask click a buy leg; stale quotes marked.
- `Ticket`: debit and credit wording (table); preview shown; rejected preview disables Submit and shows the reason (table over `RejectReason`); a changed leg invalidates the preview; submit calls `optSubmit` once.
- `OrdersTab`: cancel and reprice call the API; strategy orders have no buttons; fill quotes shown in history.
- `PanelView`: renders any panel from fixtures (two different shapes, one of them the toy plug-in's); button, toggle, text and choice actions call `optPanelAction` with the row id; confirm dialog.
- `StrategiesTab`: enable switch; a 422 shows the field message; ASSUMPTION text visible.
- `PromptsBanner`: choice buttons; text required; 409 handled.
- `SettingsTab`: edit, bounds error.
- `optionsHttp`: every method requests the path and verb of §3.9 (table).
- `touchTargets` for the Options page; no existing web test changes except the pinned nav or route lists.

**Out of scope**: `http.ts`, `client.ts`, `fakeApi.ts`, `queryKeys.ts`, the SSE feed; anything in `app/`.

### T16. Wiring, deploy files and end-to-end

**Goal**: compose the real objects, register everything, and prove the whole feature on fake market data with a stepped clock (FP §7.1). **Depends on**: T2 to T15.

**Owns**: `trader/options/runtime.py`; the T16 rows of the allow-list; `tests/options/test_runtime.py`; `tests/e2e/options_world.py` (the scripted market and clock) and `tests/e2e/test_options_wheel_cycle.py`, `test_options_manual_spread.py`, `test_options_toy_plugin.py`, `test_options_restart.py`, `test_options_isolation.py`.

**Interfaces**: `async build_options(core, stack) -> OptionsRuntime` (client, market, facts, broker, lifecycle, registry, host, prompts, sender, renderer, notifier); `async build_worker_deps(core, stack) -> OptionsWorkerDeps`; `async build_api_services(core, stack) -> OptionApiServices | None`; `run_cli_command(name, *, session_date, force, **kwargs) -> int` for `check`, `refresh`, `postclose`, `event`.

**Behaviour**:
- Every options process builds its own `QuestradeClient` with `market_rps=2.0`, so the options side can never push the shared Questrade limit during the stock scan (risk R9).
- The USD/CAD rate is `core.settings.load().fx_cad_usd_rate` inverted, read at use.
- `services.py`: `options=` from `build_api_services`; a failure to build is logged and leaves it None (the stock pages keep working). `routers/__init__.py`: `options.router` added before `stream.router`.
- `supervisord.conf`: `[program:options-worker]`, `command=/app/.venv/bin/python -m trader.options.worker`, `priority=250`, `stopwaitsecs=30`, the same logging lines; compose `stop_grace_period: 150s`.
- `crontab` (new lines only): `15 8 * * 1-5 trader options-refresh`; `35 10 * * 1-5 trader options-event --due`; `20 16 * * 1-5 trader options-postclose`; `0 8 * * 6 trader options-event wheel screen`.
- `test_web_client_contract.py`: the client parser also reads `optionsHttp.ts` and `optionsClient.ts`; the rules stay the same (every client method has a real route, every route a client method, bodies match).

**Tests**:
- `test_runtime_builds_everything_and_cli_commands_dispatch` (fakes for the network).
- Wheel cycle (one scripted world, checked step by step): (a) screen → put sold by a walking limit that fills at the bid → the 50% take-profit closes it → `NONE`; (b) a second put → assigned at expiry → 100 shares at the strike with net cost per WS §9.1 → fresh-cash "yes" → call sold → called away → the full-cycle result of WS §11 → the ticker is a candidate again; (c) preference `ROLL_ONCE`: one roll for a net credit, then `TAKE_ASSIGNMENT`. (FP §7.1 items 1 and 5.)
- `test_books_reconcile_after_every_step`: cash = ledger total; reserved = the sum over open structures and working orders; account value = cash + liquidation marks; shares and cash reserved by one source never cover another (item 4).
- `test_manual_vertical_spread_through_expiry` (table: both legs out of the money, one in, both in).
- `test_no_naked_order_is_ever_accepted` (table of about ten attacks through the real API, broker and collateral engine; item 2).
- `test_fill_invariants_through_the_real_stack` (table: never above the bid for a sell or below the ask for a buy; no fill out of hours or on a stale, delayed, halted, one-sided or zero-bid quote; item 3).
- `test_toy_plugin_trades_and_shows_its_panel_with_no_core_change`: installed by a patched entry point; it buys its call; `/api/options/strategies/toy_call/panel` serves its panel (item 6).
- `test_restart_mid_session_loses_nothing`: stop the worker between a submit and its fill and between a prompt and its answer; a new worker finishes both, with no duplicate fill, message or prompt (item 8).
- `test_stock_run_is_untouched`: the stock live run's rows, `active_live_run_id`, the stock relay and the stock change feed see nothing of the options run (item 7, with `test_stock_untouched`).
- `test_prompt_tap_through_the_real_bot_answers_the_prompt`; `test_messages_and_summary_line` (texts for fill, assignment, prompt, summary).
- the edited `test_crontab.py`, `test_docker_files.py` and `test_web_client_contract.py` pass; the golden replay test passes unchanged.

**Out of scope**: live services (T17); new product behaviour (a gap found here goes back to the owning task as a fix).

### T17. Deploy and live checks

**Goal**: run it on trader-dev with 5,000 USD and prove the live parts during a session. **Depends on**: T16.

**Owns**: `docs/SPEC.md` (an Options section), `Trader/README.md`, a deploy note in the build state.

**Steps and pass criteria** (LIVE; dev credentials; deploy any time, then catch up missed cron jobs):

| Step | Pass |
|---|---|
| deploy with the existing `deploy.sh`; migration 0011 | `/api/meta` shows the new commit; three programs plus `options-worker` are RUNNING; the stock worker's heartbeat and live run are unchanged |
| `trader options-run new --cash 5000 --confirm` | one active options run, one 5,000 USD deposit |
| `trader options-check --symbol F` during the session | chain with expiries; a quote with `delay` 0; details present. If `delay` is not 0 in the session: stop and report (risk R5) |
| `trader options-refresh` | facts and chains for the watchlist; FinViz labels found (risk R11) |
| a manual one-contract order on the web, then its close | filled at the ask and closed at the bid; ledger and account agree; Telegram fill messages arrive |
| approve one ticker; wait for the 10:30 event or fire `trader options-event wheel opt_daily --force` | a `wheel_events` evaluation row; a put order or a recorded reason |
| a prompt | Telegram buttons answer it; the web shows it answered |
| `trader options-postclose` | a snapshot and one summary line |
| stock soak | the day's soak line is unchanged by this deploy |

**Tests**: none new; the full gate once before the deploy. **Out of scope**: prod; any change to trading rules.

## 6. Feature acceptance (FP §7.1) and where each item is proved

| Item | Proved by |
|---|---|
| 1 spec cases, thresholds from config | T9 `test_ws13_cases`, `test_every_threshold_moves_a_result` |
| 2 no uncovered short | T4 tables; T16 `test_no_naked_order_is_ever_accepted` |
| 3 conservative fills | T5 `test_no_fill_matrix`, `test_fill_prices`; T16 invariants |
| 4 reconciliation, no double cover | T5 reconcile test; T16 `test_books_reconcile_after_every_step` |
| 5 assignment and called away | T6 tables; T11 transitions; T16 wheel cycle |
| 6 toy plug-in | T7 entry-point test; T16 `test_toy_plugin_...` |
| 7 stock engine untouched | T1 `test_stock_untouched`; T16 `test_stock_run_is_untouched`; golden replay |
| 8 restart safety | T5 (no memory), T12 restart test, T16 restart test |

## 7. Risks and open points (the planner's defaults; nothing here waits for an answer)

| # | Finding | Chosen resolution |
|---|---|---|
| R1 | **The existing D2 diff test proves nothing.** `tests/live/test_d2_deploy_diff.py` runs `git diff` with `Trader/app` as working directory and repo-root-style paths, so every path matches no file and the diff is always empty. It passes today although `settings_store.py`, `engine`, `broker`, `market` and the crontab have changed since its base | OPTSIM does not rely on it and does not repair it (repairing it makes it fail on earlier, approved changes). T1 writes a correct test of its own. Stephen should decide separately what to do with the old one; if it is repaired, its crontab rule must allow added lines |
| R2 | Telegram buttons are handled by the bot inside the **stock worker** process, and only one process may poll a bot. Owner prompts therefore need three additive edits in `adapters/telegram` (T10) | accepted: an added branch, existing bot tests unchanged, no trading path touched. The alternative (web-only answers) drops a stated requirement |
| R3 | FP put the option settings in the global Settings group. That means editing `settings_store.py`, a D2 decision-path file whose dump is the stock run's snapshot | own model and store, same table, shown on the Options page (§2) |
| R4 | Wave 2 is 11 tasks; the cap is 8 | start order in §4; no loss on the critical path |
| R5 | Real-time option quotes in the session are still unverified (FP §2.2). If Questrade serves them delayed, nothing ever fills (`quote_delayed`) | checked at T17 before anything else; if delayed, stop and ask Stephen (a setting to accept delayed quotes would break D11 silently, so none is built) |
| R6 | The stock ledger settles sales T+1. Option premium is treated as usable at once: option cash is the ledger total | a documented simulation limit; brokers let option premium be reused the same day in practice |
| R7 | An exercise or assignment that would leave short shares or that cash cannot pay for (a long call without cash, the short leg of a call credit spread) | settled in cash at intrinsic value against the official close, recorded on the event; added to FP §8's limits |
| R8 | The official close: at 16:20 the daily candle may not be published under Stephen's data package | the share quote's regular-hours last trade first, the candle second; a missing close changes nothing and the job retries |
| R9 | Three processes (api, worker, options worker) plus cron now share one Questrade rate limit; each paces itself at 17 requests per second | option processes pace at 2 per second (T16) |
| R10 | Live web updates: a new SSE topic means editing `schemas.Topic`, `feed.py`, `queryKeys.ts` and their tests | polling on the Options page; a topic can be added later |
| R11 | Unverified data details: FinViz snapshot label names; whether Questrade's `dividend` is per payment; the assignment fee; the 0.01 tick for every contract; batch size 100 for option quotes | each is a setting or a labelled ASSUMPTION, checked live at T17 |
| R12 | Free text ("why would you own it", the drawdown review) cannot be collected with Telegram buttons | Telegram shows a link to the Options page for those choices |
| R13 | WS §5 says no open position "of any kind" in the ticker; FP §9.5 lets a manual position sit beside a wheel one | the wheel looks at wheel positions only; manual reservations still count in its cash test |
| R14 | WS §9.6 "never re-enter the same ticker automatically" | after shares are called away the ticker returns to `candidate` and needs a new approval |
| R15 | T1 is large (100 min) and everything waits on it | the orchestrator should give T1 its review at once and start wave 2 from the merged commit; a contract change after that goes through the orchestrator |
| R16 | The `wheel` entry point is declared in T1 but its class arrives in T11 | the registry skips it with one logged error until then; worktrees need `uv sync` (the gate runs it) for entry points to appear |
| R17 | `runs` rows with mode `options` can be named in the stock pages' `?run=<id>` | harmless: those pages show an empty run; nothing lists option runs there |
| R18 | The benchmark is one stock (SOFI), so the quarterly pause rule compares against one stock's quarter (FP P3 already notes it) | as decided; it is the setting `options.benchmark_ticker` |

## 8. Changes to the feature plan, and why

| FP | Change | Why |
|---|---|---|
| §3.1 interface | adds `on_answer`, `panel`, `on_action`, `watch_underlyings`, `manual_events`; adds intent `RollStructure`; adds order options `walk` and `take_profit_pct` | answers must act the same day; the roll is one two-leg order (FP §5.1) and needed a shape; repricing and the standing 50% close are generic order features, so the worker holds no wheel logic |
| §3.6 schedule | no `options-screen` command: `options-event wheel screen`; a cron backup `options-event --due` | keeps the wheel out of the core |
| §6 tabs | "Wheel" tab → generic "Strategies" tab drawn from each plug-in's panel; a seventh tab "Settings" | modularity (acceptance item 6); R3 |
| §6 settings | group "Options" on the Options page, not in the global Settings page | R3 |
| §7 T12, T13 | `supervisord.conf` and `crontab` move to T16 | their tests (`test_docker_files.py`, `test_crontab.py`) pin the exact programs and lines; one owner per file |
| §7 T7 | T7 also owns the strategy host; T12 and T13 depend on T7 and T10 | the worker and the post-close job both run plug-in hooks and send messages |
| §7 T8 | FinViz additions go in a new `fundamentals.py` plus one method on the scraper | smallest change to a file the stock nightly job uses |
| §7 critical path | T1 → T9 → T11 → T16 → T17 is as long as T1 → T5 → T12 → T16 → T17 | planned minutes in §4 |
| §3 principle 1 | "the existing D2 diff check, extended" → a new test | R1 |
| §8 limits | adds cash settlement at intrinsic (R7) and same-day use of premium (R6) | found while specifying T5 and T6 |
| §4.3 tables | the same tables as FP lists; `opt_structures` gains `cover_structure_id`, `parent_structure_id`, `take_profit_net`, `frozen`; `owner_prompts` gains delivery and send tracking | shares that cover a call are tracked explicitly (FP §7.1 item 4); P8; P9 |
