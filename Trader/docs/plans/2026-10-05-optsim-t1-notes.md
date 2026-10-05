# OPTSIM: T1 contract notes (read after the task plan's §3)

T1 was delivered in commit `5026ee4` (gate: 4810 passed, web 730). The code in `trader/options/`, `trader/option_strategies/base.py`, `trader/db/models.py` and `tests/options/` is now the contract: **where it differs from the task plan's §3, the code wins.** The differences and the things every later builder needs are listed here.

## Additions and sharpenings to the protocols (`trader/options/protocols.py`)

- `OptionBook.link_lifecycle(event_id, new_structure_id)`: sets `new_structure_id` on a lifecycle event. T5's database book implements it; T6 calls it after creating the shares structure.
- `OptionBook.move_cash(amount, kind, ref, ts, *, structure_id=None)`: with `structure_id` a `fee` is added to that structure's `fees_total`. `amount` is signed like the ledger (`sell` > 0, `buy` < 0, `fee` < 0), else `ValueError`.
- `OptionBook.close_structure` also sets the structure's `reserved_cash` to 0.
- A `StructureView` lists every position the structure ever had; a closed one stays with `qty` 0. Consumers filter on `qty != 0`.
- `OptionBook.apply` keeps the re-averaged price to 4 decimals (half up).
- `OptionBroker.order(order_id) -> OptOrderView | None` exists (T14's cancel and reprice routes use it).
- `UnknownUnderlying` and `UnknownContract` (both `LookupError`) live in `protocols.py`. T3 raises these; T14 catches them for 404. Do not define your own.
- `StrategyHost.ensure_defaults()` is async, like the rest of the host.

## Types and framework

- `net_price(legs, prices, multipliers)`: both mappings are keyed by `leg_no`. A shares leg always uses 1; an option leg missing from `multipliers` uses 100.
- `round_tick(value, tick, "up" | "down")` is ceiling or floor on the grid.
- `UnderlyingFacts`: `symbol_id`, `as_of`, `ticker` are required; every fact defaults to None. It has no `quote_time` or `market_open`.
- `OwnerPromptRequest` has defaults for `needs_text`, `default_choice`, `data`. Panel dataclasses have defaults for optional fields. `OptionEvent.scheduled` defaults to True.
- `OptionStrategyContext.prompt` and `.equity_on` are callable fields the host supplies. Notes reuse the stock `DecisionNote`; alerts are `StrategyAlert(kind, message, dedupe_key, data)`.
- Intents and `OrderRequest` raise `TypeError` on a float and `ValueError` on a reason over 100 characters. `ContractKey.strike` must be a `Decimal`.
- Constants: `KEY_PATTERN`, `POSTCLOSE_EVENT = "postclose"`, `PROMPT_CHOICE_CODES`.

## Settings and account

- `OptionSettings` field names are the key without `options.`. Seconds and hours are `int`.
- `OptionSettingsStore` reads only `options.*` rows. Exported: `OPTION_SETTING_KEYS`, `SETTINGS_GROUP = "Options"`.
- **`account.lock_book(session, run_id)` is the one book lock (rule 6).** T5, T6, T7, T10 call it; nobody writes the advisory-lock SQL themselves.
- `trader options-run new` without `--confirm` prints what it would do and exits 0.

## Schema (migration 0011; models in `trader/db/models.py`)

- Model names: `OptionContract`, `OptionChainCache`, `OptionQuoteMark`, `UnderlyingFacts`, `OptionStrategyConfig`, `OptionStrategyState`, `OptStructure`, `OptPosition`, `OptOrder`, `OptOrderLeg`, `OptFill`, `OptLifecycleEvent`, `OwnerPrompt`, `WheelTicker`, `WheelPosition`, `WheelEvent`. Two share a name with their value type: use `m.` for the models.
- `ck_opt_orders_limit` also allows `status = 'rejected'` (a rejected limit order with no limit can be stored).
- No check constraint on `wheel_tickers.origin`, nor on `kind` and `close_reason` values.
- Constraint and index names are listed in `tests/options/test_migration_0011.py`.
- Nobody adds a column. A missing one is a contract change: report it to the orchestrator.

## API schemas

- `OptStructureOut.unrealized_pnl` is `Decimal | None`; `OptAccountOut.started_at` and `marks_as_of` are nullable.
- `OptOrderIn`: 1 to 4 legs, `qty` 1 to 100, a ticker pattern for `underlying`, `net_limit` up to 4 decimals. `OptPromptAnswerIn.choice` is one lowercase letter.
- TypeScript literals are prefixed `Opt` (`OptRight`, `OptRejectReason`, ...), plus `OPT_REJECT_REASONS`. `Side` is the existing type.

## Gotchas

- **Never write the table names `option_quote_marks` or `mark_bars` as a string constant under `trader/`.** `tests/live/test_d2_static.py` flags `quote_marks` by substring. Use the ORM model `m.OptionQuoteMark`.
- **Existing tests that pin lists.** "No existing test file may change" has these exceptions, each recorded in `PINNED_TESTS` in `tests/options/test_stock_untouched.py` (a rule per file; anything else changing in them fails the test): the migration head pins (`tests/db/test_migration_0007.py` to `_0010.py`, `tests/api/test_system.py`) and `tests/test_phase4_contracts.py::test_api_services_fields`. **T16** will need two more entries when it registers the router: `tests/test_phase4_contracts.py` and `tests/test_phase5_contracts.py` assert `len(ROUTERS) == 20`, and phase 4 pins `ROUTER_ORDER`. A builder who finds another pinned list must add a `PINNED_TESTS` entry with the narrowest rule and say so in the final report; never loosen an existing test otherwise.
- `test_stock_untouched` finds its base from the parent of the first `OPTSIM-T1:` commit; `TRADER_OPTSIM_BASE=817c1f3` is the true pre-OPTSIM commit.

## Test helpers

- Fakes: `from tests.options.fakes import FakeOptionMarket, FakeBook, FakeOptionBroker, FakeQtOptions, FakePromptStore, RecordingHost, FakeFacts, FakeRegistry, CannedCollateral, accept, reject, make_option_services`.
  - `FakeOptionMarket(clock)` stamps a quote with the clock's time on each read; pass `fetched_at=` to `set_quote` for a stale one.
  - `FakeOptionBroker(market)` fills only when the test calls `fill(order_id, {leg_no: price})` or `fill_all_at_market()`, or sets `fill_on_poll = True`.
  - `FakeBook(cash, market.contracts)` exposes `ledger`, `lifecycle`, `order_reserved`, `position(...)`, `lifecycle_event(id)`.
  - `make_option_services(db_factory, clock, *plugins, run_id=1)` returns `(OptionApiServices, fakes)`; pass it to `make_services(core, options=services)`. `FakeRegistry` lets T14 build services before T7 exists.
- Factories: `tests.options.factories`: `add_options_run`, `add_underlying`, `add_contract`, `add_structure`, `add_position`, `add_order`, and the value builders `make_contract`, `make_quote`, `make_leg`, `make_request`. Constants `T0` (Tue 2026-10-06 10:00 ET), `SESSION`, `EXPIRY` (2026-11-20, a monthly, 45 days out).
- Book contract (T5): subclass `tests.options.contract_book.BookContract` with a `book_env` fixture returning `BookEnv(book, put, call, underlying="F")`: an empty book with 5,000 cash and two contracts of multiplier 100.
- Toy plug-in: `tests.options.toy_plugin:ToyCallBuyer`, key `toy_call`, event `toy_buy`, action `note`.
- CLI (T16): the four unwired commands call `trader.options.runtime.run_cli_command(name, session_date=..., force=..., **kwargs)`; kwargs are `symbol` for `check`, and `strategy`, `key`, `due` for `event`.
- The `wheel` entry point is declared; its class arrives in T11 (the registry skips a plug-in that fails to load).
