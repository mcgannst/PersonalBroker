# Phase 2: Engine Implementation Plan

> **For agentic workers:** Run under the gauntlet process in [`2026-09-26-build-master-plan.md`](2026-09-26-build-master-plan.md) (§4–§6). Read its **Global Constraints** and **Review Focus** first, then this plan's own Global Constraints reminder and Review Focus below; they apply to every task here. Steps use checkbox (`- [ ]`) syntax: tick each one in this file as you complete it and commit the file with your code.

**Goal:** A tested engine that turns strategy intents into sized, approved, simulated orders: a strategy plug-in framework with `orb_sip` 1.0.0 and `spy_overlay` 1.0.0, a risk manager with kill switches, a proposal service with manual and automatic approval, a quote-based fill model, a simulated broker with positions, trades and a T+1 cash ledger, Claude catalyst classification with a daily budget, and the pre-market job, proven end to end by one full simulated day.

**Architecture:** New packages `trader/broker/` (value types, fill model, ledger, simulated broker), `trader/strategies/` (framework, registry, plug-ins), `trader/engine/` (runs, risk, kill switches, proposals, orchestrator) and `trader/adapters/claude/` sit on top of the Phase 1 data layer. The database stays synchronous SQLAlchemy; market data (Questrade) and strategy callbacks are `async`. Every trading row carries a `run_id`. Strategies never touch the database or size orders: they return intents and write candidates and notes into their context, and the engine persists them.

**Tech stack:** Python 3.12 via uv; SQLAlchemy 2, Alembic, psycopg 3; pydantic v2; exchange_calendars; httpx; `anthropic` 1.x (new in this phase, with `httpx2` for its test transport); pytest, pytest-asyncio, respx, testcontainers.

**Spec:** [`../BRD.md`](../BRD.md) BR-03, BR-05, BR-10–13, BR-20–23, BR-40–42; [`../SPEC.md`](../SPEC.md) §3a, §4.2 (pre-market), §4.3, §5, §6, §7.1–7.3, §10 (trading tables and views), §13 (runtime settings); [`../../spikes/README.md`](../../spikes/README.md) (FinViz filter codes `news_date_today`, `earningsdate_today`).

**Task IDs for the board:** P2-T1, P2-T2, P2-T3, P2-T4, P2-T5, P2-T6, P2-T7, P2-T8, P2-T9, P2-T10, P2-T11, P2-T12, P2-T13, P2-T14, P2-T15.

## Task list, dependencies and parallel lanes

| ID | Task | Depends on | Lane |
|---|---|---|---|
| P2-T1 | Migration 0002: trading tables, views, ledger trigger; test factories | P1 (all accepted) | A |
| P2-T2 | Runs, sim account and the full runtime settings set | T1 | A |
| P2-T3 | Ledger with T+1 settlement | T1, T2 | B |
| P2-T4 | Broker value types and the quote-based fill model | T2 | C |
| P2-T5 | Simulated broker: orders, positions, trades, equity | T3, T4 | B |
| P2-T6 | Strategy framework, registry, entry points, strategy test fakes | T1, T2, T4 | C |
| P2-T7 | Market data service | T6 | C |
| P2-T8 | `orb_sip` plug-in 1.0.0 | T6, T7 | C |
| P2-T9 | `spy_overlay` plug-in 1.0.0 | T6, T7 | D |
| P2-T10 | Risk manager and kill switches | T5, T6 | B |
| P2-T11 | Proposal service | T5, T10 | B |
| P2-T12 | Claude catalyst classifier, store and service | T2 | E |
| P2-T13 | Engine orchestrator | T8, T9, T10, T11, T12 | A |
| P2-T14 | Pre-market job and `premarket` CLI | T7, T12 | E |
| P2-T15 | Integration: one full simulated day | T13, T14 | A |

After T2, lanes B (T3 → T5 → T10 → T11, T10 once T6 is also in, because it imports the intent types from `trader.strategies.base`), C (T4 → T6 → T7 → T8), D (T9, once T7 is in) and E (T12 → T14, T14 once T7 is in) run in parallel. They touch disjoint files except `pyproject.toml` (T6 adds the entry-point table after `[project.scripts]`, T12 adds one dependency line; the hunks don't overlap) and `uv.lock` (T12 only).

**Changes from the master-plan outline (§7.2), with reasons:**
- **T4 creates `trader/broker/types.py`** (order spec, fees, fill decision, fill event, position/order/account views) and T6 depends on T4. The outline had these types in T5's `base.py`, but the fill model (T4) and the strategy framework (T6, `on_fill(ctx, fill)`) both need them before T5 exists. T5's `base.py` holds only the `Broker` protocol and `BrokerRejected`.
- **T6 also creates `tests/strategies/fakes.py`** (fake market data and catalysts) so that T8 and T9, which run in parallel, share one harness.
- **T10 depends on T6 as well as T5** (the outline listed only T5): the risk manager evaluates the `EnterLong`/`Exit`/`Cancel` intents that T6 defines.
- **T7 creates `tests/fakes_questrade.py`**, a fake Questrade client reused by T13 and T15.
- **T12 also holds the catalyst store and `CatalystService`** (the object strategies call as `ctx.catalysts`), so neither T6 nor T13 needs Claude details. T13 needs no extra file for it.
- **No task was merged or dropped.**

**Contracts refined (master plan §7.1, which now records these refined shapes), names and meaning kept:**
- `Strategy.on_event` and `Strategy.on_fill` are `async def`. The market-data contract (`MarketDataService`, P2-T7) is async because the Questrade client is async, and strategies read market data inside `on_event`. `schedule(cal)` stays synchronous. P3-T1 and P5 must `await` them.
- Strategy plug-ins are constructed with their validated params (`OrbSip(params)`), and the protocol has a `params` attribute, because `schedule(cal)` has no params argument but `orb_sip`'s event times (`entry_cancel_at`, `exit_at`) are settings.
- `Exit.order_type` is `Literal["market", "stop"]` (the `Exit` contract has no limit price).
- `Broker.submit(spec, session=None)` and `Broker.cancel(order_id, reason, session=None)` take an optional SQLAlchemy session so the proposal service can decide and submit in one transaction (a crash can't leave an approved proposal with no order). Omitting `session` behaves exactly as the contract.
- `Broker.end_of_session(session_date) -> list[int]` returns the cancelled order IDs.
- `ProposalService.create/decide/expire_due` return ORM `Proposal` rows (detached). `DecisionResult` also carries `order_id`.
- `FillModel` is a `Protocol` in `trader.broker.types` with two methods that both accept `market: QtQuote | Candle`: `evaluate(order, market, now) -> FillDecision | None` (the contract) and `assess(order, market, now) -> FillDecision | NoFill` (the same decision with the reason for not filling, which the broker needs to log stale quotes). `SimBroker` is typed to the `FillModel` protocol, not to `QuoteFillModel`, so P5-T2's `CandleFillModel` plugs into the same broker. Phase 2's only implementation, `QuoteFillModel`, raises `TypeError` for a `Candle`. `SimBroker.on_quotes` is the Phase 2 path; P5 adds `SimBroker.on_candles(candles: Mapping[int, Candle], now) -> list[FillEvent]`, which runs the same per-order loop with a candle in place of the quote (documented in T5, not built now). Phase 2 behaviour is unchanged.
- **Assumption (staleness):** staleness uses `QtQuote.last_trade_time`, because Questrade quotes carry no separate quote timestamp (P1-T7 `QtQuote`). This may over-flag quiet stocks whose last trade is older than `stale_quote_seconds` while the bid/ask is live, so such orders wait instead of filling (the safe direction). It will be checked live in market hours (Phase 6, S2 recheck). A quote is also unusable when `is_halted` or `delay > 0`.
- **Entry cutoff in the broker (BR-42, overnight hold):** the risk manager already refuses new entries at or after `session_close − no_entry_before_close_minutes` (T10 check 4), but an entry order approved earlier can still be working then. `SimBroker` therefore refuses to fill an entry (buy-to-open) order at or after that cutoff: it cancels the order with reason `"entry cutoff"` and logs a `warning` event (T5). And when `auto_flatten_on_expiry` is on, an expired `cancel` proposal auto-executes like an expired flatten, so an unanswered "cancel the entry" can't leave the order working into the close (T11).
- `MarketDataService.quotes()` returns quotes keyed by `trader.symbols.id` with `QtQuote.symbol_id` rewritten to that ID. Inside the engine every `symbol_id` is a database ID; Questrade IDs stay at the client boundary.
- `MarketDataService` keeps the five contract methods and adds `universe_status(session_date)` (the nightly job's fallback/stale verdict, P1-T9 ruling), `prior_close`, `prior_closes` and `symbol_ids`. `opening_bars` takes an optional `symbol_ids` filter.
- `orb_sip` gains one setting beyond the original SPEC §5.2 table, `stale_universe: "skip" | "trade"` (default `skip`): the orchestrator's P1-T9 ruling says a stale fallback universe skips entries by default. SPEC §5.2 now lists it.
- SPEC §10 now uses the real column names from migration 0002: `orders.order_type`, `orders.stop_price`, `orders.limit_price` and `trades.pnl_r`.
- `claude.model` accepts `claude-sonnet-5` (default) or `claude-haiku-4-5`. SPEC §2 names the cheaper model with a date suffix; the undated alias is the current ID. Costs are computed from a per-model price table ($2/$10 and $1/$5 per million input/output tokens).

Every command below runs from the repository root of your worktree unless it says otherwise. "Run from `Trader/app`" means `uv --directory Trader/app run ...`.

## File map

| Path (under `Trader/app/`) | Responsibility | Task |
|---|---|---|
| `trader/db/models.py` | Add 15 trading ORM models | T1 |
| `trader/db/migrations/versions/0002_trading.py` | Trading tables, views `v_trade_metrics`, `v_daily_pnl`, cash-ledger append-only trigger | T1 |
| `tests/factories.py` | `add_symbol`, `add_run`, `add_strategy_config` test helpers | T1 |
| `tests/db/test_migration_0002.py` | Migration 0002 tests | T1 |
| `trader/settings_store.py` | Add every Phase 2 runtime setting (alias keys) | T2 |
| `trader/engine/__init__.py`, `trader/engine/runs.py` | Live run, sim account, starting balance and FX | T2 |
| `tests/test_runtime_settings_phase2.py`, `tests/engine/__init__.py`, `tests/engine/test_runs.py` | T2 tests | T2 |
| `trader/broker/__init__.py`, `trader/broker/ledger.py` | Cash ledger, T+1 settlement, balances | T3 |
| `tests/broker/__init__.py`, `tests/broker/test_ledger.py` | T3 tests | T3 |
| `trader/broker/types.py`, `trader/broker/fill_model.py` | Value types; SPEC §7.2 fill rules | T4 |
| `tests/broker/test_fill_model.py` | T4 tests | T4 |
| `trader/broker/base.py`, `trader/broker/sim_broker.py` | Broker protocol; simulated broker | T5 |
| `tests/broker/test_sim_broker.py` | T5 tests | T5 |
| `trader/market/types.py` | Add `UniverseMember`, `OpenBarStats`, `OpeningBars` | T6 |
| `trader/strategies/__init__.py`, `trader/strategies/base.py`, `trader/strategies/registry.py` | Framework and registry | T6 |
| `pyproject.toml` | `[project.entry-points."trader.strategies"]` | T6 |
| `tests/strategies/__init__.py`, `tests/strategies/fakes.py`, `tests/strategies/demo_plugin.py`, `tests/strategies/test_framework.py` | T6 tests and shared fakes | T6 |
| `trader/market/data_service.py` | `MarketDataService` | T7 |
| `tests/fakes_questrade.py`, `tests/market/test_data_service.py` | Fake Questrade client; T7 tests | T7 |
| `trader/strategies/orb_sip.py` | ORB plug-in | T8 |
| `tests/strategies/scenarios/__init__.py`, `tests/strategies/scenarios/test_orb_sip_scenarios.py` | Hand-built scenarios | T8 |
| `trader/strategies/spy_overlay.py`, `tests/strategies/test_spy_overlay.py` | Overlay plug-in and tests | T9 |
| `trader/engine/risk.py`, `trader/engine/killswitch.py` | Sizing, checks, kill switches | T10 |
| `tests/engine/test_risk.py`, `tests/engine/test_killswitch.py` | T10 tests | T10 |
| `trader/engine/proposals.py`, `tests/engine/test_proposals.py` | Proposal service and tests | T11 |
| `trader/adapters/claude/__init__.py`, `trader/adapters/claude/catalyst.py` | Classifier, store, service | T12 |
| `tests/adapters/test_claude_catalyst.py` | T12 tests | T12 |
| `trader/engine/orchestrator.py`, `tests/engine/test_orchestrator.py` | Engine and tests | T13 |
| `trader/jobs/premarket.py`, `trader/cli.py` (add `premarket`), `tests/jobs/test_premarket.py`, `tests/test_cli.py` (add a smoke test) | Pre-market job | T14 |
| `tests/integration/__init__.py`, `tests/integration/test_simulated_day.py` | Full day | T15 |

## Global Constraints (reminder)

The master plan's **Global Constraints** apply to every task here, word for word: trunk only, Python 3.12 with uv, SPEC §3 layout, PostgreSQL schema `trader` with DDL only in migrations, `timestamptz` in UTC, `numeric(14,4)` and `Decimal` for money (never `float`), time only from a `Clock`, no secrets printed or committed, no network in unit or adapter tests, testcontainers for DB tests, the `check.sh` gate before every commit, commit messages starting `P2-Tn: ` and ending with the `Co-Authored-By` trailer, files staged by explicit path. Phase-specific additions:

- **Migrations:** new migrations are numbered `0002` onwards, name the schema explicitly (`schema="trader"` / `trader.` in raw SQL) and store every timestamp as `timestamptz`. The ORM models and the migration must stay identical (P1's `test_models_match_migrated_schema` and `test_alembic_check_through_env_sees_no_changes` must keep passing).
- **Runtime settings:** every new `RuntimeSettings` field declares `alias="<db key>"` (not `validation_alias`), even when the key equals the field name, because `_DB_KEYS` and `model_dump(by_alias=True)` read `alias`. No `model_validator` on `RuntimeSettings`: the store validates keys one at a time.
- **Claude:** tests never reach the network. The classifier takes an injected client; the SDK-shape test gives the real `anthropic.AsyncAnthropic` (1.x, which uses `httpx2`, so `respx` can't see it) a fake `httpx2.MockTransport`. The default model is `claude-sonnet-5`.
- **Prices** are quantized to 4 dp with `ROUND_HALF_UP` (`Q4 = Decimal("0.0001")`).
- **IDs:** inside the engine, `symbol_id` always means `trader.symbols.id`.
- **Assumption (quote staleness):** a quote's age is `now − QtQuote.last_trade_time`, because Questrade quotes carry no separate quote timestamp. This may over-flag quiet stocks as stale (their orders wait rather than fill). No code works around it; it is re-checked live in market hours in Phase 6 (S2 recheck).
- Phase 1 interfaces used here, read from trunk: `trader.events.log_event`, `trader.jobs.runner.run_job`, `trader.market.repository.upsert_intraday_candles` and `trader.jobs.nightly.run_nightly` (P1-T9). If P1-T9's fix rounds renamed any of them, use the trunk names and note it in your report.

## Review Focus

The five engine failure modes most likely to hurt Stephen, most likely first. Each is pinned by a named test in the task that owns the code.

1. **A fill on a stale, halted, delayed or one-sided quote** (quote older than `stale_quote_seconds`, `is_halted`, `delay > 0`, no bid/ask). Expected: no fill, the order keeps working, a `stale_quote` event is logged once and escalated if it persists. [P2-T4: `test_stale_quote_never_fills`, `test_unusable_quotes_never_fill`; P2-T5: `test_stale_quote_keeps_order_working_and_logs_once`]
2. **A double decision on one proposal** (Telegram and the web at the same moment, a tap after expiry, a repeated auto path). Expected: exactly one decision and one order; every later call gets `already_decided=True`. [P2-T11: `test_concurrent_decisions_first_wins`, `test_decide_after_expiry_is_already_decided`; P2-T5: `test_order_fills_only_once`]
3. **T+1 settlement across weekends and holidays** (Friday trades, the Wednesday before Thanksgiving, Christmas Eve). Expected: sale proceeds count as settled only on the next trading session; buys reduce settled cash at once. [P2-T3: `test_settle_date_skips_weekend_and_holidays`, `test_sale_proceeds_settle_next_session`]
4. **A position left open at the close** (a manual-mode flatten that expires, an entry that fills late, a missed flatten). Expected: an expired flatten (and, the same way, an expired cancel) auto-submits when `auto_flatten_on_expiry` is on; the broker cancels an entry order instead of filling it at or after `session_close − no_entry_before_close_minutes` ("entry cutoff"); end of session flags any open position loudly; the simulated day ends flat. [P2-T5: `test_an_entry_that_would_fill_late_is_cancelled_instead`; P2-T11: `test_expired_flatten_auto_submits`, `test_an_entry_that_would_fill_late_is_cancelled_instead`; P2-T13: `test_end_of_session_flags_open_position`; P2-T15: `test_full_day_ends_flat`]
5. **A kill switch blocking an exit or a protective stop.** Expected: exits, stops and cancels pass every check even with every switch tripped and outside market hours; only entries are blocked. [P2-T10: `test_exits_and_cancels_pass_when_everything_is_tripped`; P2-T13: `test_protective_stop_placed_while_kill_switch_tripped`]

---

### Task P2-T1: Migration 0002: trading tables, views, ledger trigger; test factories

**Files:**
- Modify: `Trader/app/trader/db/models.py` (append the trading models; add imports), `Trader/app/tests/gauntlet/test_p1_t2_breaker.py` (one line: it pins the head revision to `0001`)
- Create: `Trader/app/trader/db/migrations/versions/0002_trading.py`, `Trader/app/tests/factories.py`, `Trader/app/tests/db/test_migration_0002.py`

**Interfaces:**
- Consumes: `trader.db.models.Base`, `SCHEMA`, `Money`, `TS`, `Symbol` (P1-T2); fixtures `migrated_engine`, `db_factory`, `pg_url` and `alembic_config` in `tests/conftest.py` (P1-T2). `db_factory` truncates every table in `Base.metadata`, so the new tables are cleaned between tests automatically.
- Produces (ORM classes in `trader.db.models`; column names exactly as below; every trading row has `run_id NOT NULL REFERENCES trader.runs(id)`):
  - `Run` (`runs`): `id`, `mode` (`live`/`replay`), `started_at`, `params` jsonb, `status` (`active`/`completed`/`failed`), `label`. Partial unique index `uq_runs_one_active_live` on `(mode) WHERE mode = 'live' AND status = 'active'`.
  - `SimAccount` (`sim_accounts`): `id`, `run_id` (unique), `currency`, `starting_cash`, `source_amount`, `source_currency`, `fx_rate` numeric(12,6), `fx_fee` numeric(6,4), `created_at`.
  - `StrategyConfig` (`strategy_configs`): `id`, `strategy_key`, `version` (plug-in version), `revision` (1, 2, … per key; unique with the key), `params` jsonb, `enabled`, `created_at`, `created_by`.
  - `Catalyst` (`catalysts`): `id`, `symbol_id`, `session_date` (unique together), `headlines` jsonb, `gap_pct` numeric(10,4), `earnings_date`, `catalyst_type` (DB column `type`), `direction`, `quality`, `confirmed`, `reason`, `model`, `cost_usd` numeric(10,6), `input_tokens`, `output_tokens`, `classified_at` (NULL = not classified), `created_at`.
  - `Candidate` (`candidates`): `id`, `run_id`, `session_date`, `strategy_key`, `symbol_id` (unique together), `rvol` numeric(12,4), `rank`, `candle` jsonb, `passed`, `reject_reason`, `data` jsonb, `created_at`.
  - `Signal` (`signals`): `id`, `run_id`, `strategy_config_id`, `symbol_id` (nullable), `session_date`, `event_key`, `ts`, `intent` jsonb, `evidence` jsonb.
  - `Proposal` (`proposals`): `id`, `run_id`, `signal_id`, `kind` (`entry`/`stop`/`exit`/`cancel`), `order_spec` jsonb, `qty`, `status` (`pending`/`approved`/`rejected`/`expired`/`auto_approved`/`submitted`/`failed`), `created_at`, `expires_at`, `decided_at`, `decided_via` (`telegram`/`web`/`auto`), `decided_by`, `decision_latency_ms`, `position_id`, `cancel_order_id`, `order_id`, `sizing` jsonb, `expired_at`, `escalated_at`, `escalations`, `error`.
  - `Order` (`orders`): `id`, `run_id`, `proposal_id`, `position_id`, `strategy_config_id`, `symbol_id`, `side`, `order_type`, `purpose` (`entry`/`stop`/`exit`), `qty`, `stop_price`, `limit_price`, `stop_loss`, `tif`, `status` (`working`/`filled`/`cancelled`), `reason`, `session_date`, `submitted_at`, `closed_at`, `cancel_reason`, `stale_since`, `stale_alerted`.
  - `Fill` (`fills`): `id`, `run_id`, `order_id` (unique), `ts`, `qty`, `price`, `fees` jsonb, `quote_snapshot` jsonb, `slippage` (per share).
  - `Position` (`positions`): `id`, `run_id`, `symbol_id`, `strategy_config_id`, `qty`, `avg_price`, `stop_loss`, `planned_risk`, `session_date`, `opened_at`, `closed_at`, `entry_order_id`, `stop_order_id`, `unprotected_since`, `unprotected_seconds`.
  - `Trade` (`trades`): `id`, `run_id`, `position_id` (unique), `symbol_id`, `session_date`, `entry_price`, `exit_price`, `qty`, `pnl` (net of fees), `pnl_r` numeric(10,4), `planned_risk`, `exit_reason`, `slippage_total`, `fees_total`, `opened_at`, `closed_at`.
  - `CashLedger` (`cash_ledger`): `id`, `run_id`, `ts`, `trade_date`, `settle_date`, `currency`, `amount`, `kind` (`deposit`/`buy`/`sell`/`fee`), `ref`. Rows can't be updated or deleted (trigger `cash_ledger_append_only`).
  - `EquitySnapshot` (`equity_snapshots`): PK (`run_id`, `ts`), `equity`, `cash`, `settled_cash`, `peak_equity`, `drawdown_pct` numeric(8,4).
  - `Journal` (`journal`): PK (`run_id`, `session_date`), `rules_followed`, `notes`, `answered_via`, `updated_at`.
  - `KillSwitchEvent` (`kill_switch_events`): `id`, `run_id`, `switch`, `session_date`, `tripped_at`, `value` numeric(14,6), `threshold` numeric(14,6), `reset_at`, `reset_reason`, `reset_by`.
  - Views `trader.v_trade_metrics` (per run: `trades`, `wins`, `win_rate`, `avg_win_r`, `avg_loss_r`, `expectancy_r`, `profit_factor`, `avg_slippage`, `max_drawdown_pct`, `adherence_pct`; a win is `pnl > 0`) and `trader.v_daily_pnl` (`run_id`, `session_date`, `trades`, `realized_pnl`, `fees`).
  - `tests.factories`: `T0`, `add_symbol(s, ticker="AAA", *, questrade_id=None, exchange="NASDAQ", currency="USD", name=None) -> int`, `add_run(s, *, mode="live", status="active", started_at=T0, label=None) -> int`, `add_strategy_config(s, key="orb_sip", *, version="1.0.0", revision=1, params=None, enabled=True, created_at=T0) -> int`. Each flushes and returns the new ID; the caller commits.

Cross-row references between `proposals`, `orders` and `positions` (`position_id`, `order_id`, `cancel_order_id`, `entry_order_id`, `stop_order_id`) are plain `bigint` columns without foreign keys, to avoid circular foreign keys. Every other reference has a foreign key.

- [x] **Step 1: Write the failing tests**

`Trader/app/tests/factories.py`:
```python
"""Tiny row builders for DB tests. Each flushes and returns the new primary key; the caller commits."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from trader.db import models as m

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def add_symbol(
    s: Session,
    ticker: str = "AAA",
    *,
    questrade_id: int | None = None,
    exchange: str = "NASDAQ",
    currency: str = "USD",
    name: str | None = None,
) -> int:
    sym = m.Symbol(
        ticker=ticker, exchange=exchange, questrade_id=questrade_id, currency=currency, name=name or f"{ticker} Inc"
    )
    s.add(sym)
    s.flush()
    return sym.id


def add_run(
    s: Session, *, mode: str = "live", status: str = "active", started_at: datetime = T0, label: str | None = None
) -> int:
    run = m.Run(mode=mode, started_at=started_at, params={}, status=status, label=label)
    s.add(run)
    s.flush()
    return run.id


def add_strategy_config(
    s: Session,
    key: str = "orb_sip",
    *,
    version: str = "1.0.0",
    revision: int = 1,
    params: dict[str, Any] | None = None,
    enabled: bool = True,
    created_at: datetime = T0,
) -> int:
    cfg = m.StrategyConfig(
        strategy_key=key,
        version=version,
        revision=revision,
        params=params or {},
        enabled=enabled,
        created_at=created_at,
        created_by="test",
    )
    s.add(cfg)
    s.flush()
    return cfg.id
```

`Trader/app/tests/db/test_migration_0002.py`:
```python
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0, add_run, add_strategy_config, add_symbol
from trader.db import models as m

pytestmark = pytest.mark.db

TRADING_TABLES = {
    "runs",
    "sim_accounts",
    "strategy_configs",
    "catalysts",
    "candidates",
    "signals",
    "proposals",
    "orders",
    "fills",
    "positions",
    "trades",
    "cash_ledger",
    "equity_snapshots",
    "journal",
    "kill_switch_events",
}
RUN_SCOPED = TRADING_TABLES - {"runs", "strategy_configs", "catalysts"}


def test_every_trading_table_exists(migrated_engine: Engine) -> None:
    assert TRADING_TABLES <= set(inspect(migrated_engine).get_table_names(schema="trader"))


def test_views_exist(migrated_engine: Engine) -> None:
    assert {"v_trade_metrics", "v_daily_pnl"} <= set(inspect(migrated_engine).get_view_names(schema="trader"))


@pytest.mark.parametrize("table", sorted(RUN_SCOPED))
def test_run_id_on_every_trading_row(migrated_engine: Engine, table: str) -> None:
    insp = inspect(migrated_engine)
    cols = {c["name"]: c for c in insp.get_columns(table, schema="trader")}
    assert "run_id" in cols and cols["run_id"]["nullable"] is False
    fks = insp.get_foreign_keys(table, schema="trader")
    assert any(fk["constrained_columns"] == ["run_id"] and fk["referred_table"] == "runs" for fk in fks)


def test_keys(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert insp.get_pk_constraint("equity_snapshots", schema="trader")["constrained_columns"] == ["run_id", "ts"]
    assert insp.get_pk_constraint("journal", schema="trader")["constrained_columns"] == ["run_id", "session_date"]
    uniques = {
        t: [u["column_names"] for u in insp.get_unique_constraints(t, schema="trader")]
        for t in ("sim_accounts", "strategy_configs", "catalysts", "candidates", "fills", "trades")
    }
    assert ["run_id"] in uniques["sim_accounts"]
    assert ["strategy_key", "revision"] in uniques["strategy_configs"]
    assert ["symbol_id", "session_date"] in uniques["catalysts"]
    assert ["run_id", "session_date", "strategy_key", "symbol_id"] in uniques["candidates"]
    assert ["order_id"] in uniques["fills"]
    assert ["position_id"] in uniques["trades"]


def test_only_one_active_live_run(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        add_run(s)
        add_run(s, mode="replay")
        add_run(s, mode="replay")
        add_run(s, status="completed")
        s.commit()
        with pytest.raises(IntegrityError):
            add_run(s)


def _trade(s: Session, run_id: int, sym: int, cfg: int, pnl: str, r: str, day: date) -> None:
    pos = m.Position(
        run_id=run_id,
        symbol_id=sym,
        strategy_config_id=cfg,
        qty=10,
        avg_price=Decimal("10"),
        stop_loss=Decimal("9"),
        planned_risk=Decimal("10"),
        session_date=day,
        opened_at=T0,
        closed_at=T0 + timedelta(hours=1),
        entry_order_id=1,
        stop_order_id=None,
        unprotected_since=None,
        unprotected_seconds=0,
    )
    s.add(pos)
    s.flush()
    s.add(
        m.Trade(
            run_id=run_id,
            position_id=pos.id,
            symbol_id=sym,
            session_date=day,
            entry_price=Decimal("10"),
            exit_price=Decimal("10") + Decimal(pnl) / 10,
            qty=10,
            pnl=Decimal(pnl),
            pnl_r=Decimal(r),
            planned_risk=Decimal("10"),
            exit_reason="test",
            slippage_total=Decimal("0.02"),
            fees_total=Decimal("0.01"),
            opened_at=T0,
            closed_at=T0 + timedelta(hours=1),
        )
    )


def test_v_trade_metrics_hand_made_trades(db_factory: sessionmaker[Session]) -> None:
    d1, d2 = date(2026, 10, 5), date(2026, 10, 6)
    with db_factory() as s:
        run = add_run(s)
        other = add_run(s, mode="replay")
        sym = add_symbol(s)
        cfg = add_strategy_config(s)
        for pnl, r, day in (("20", "2", d1), ("-10", "-1", d1), ("-10", "-1", d2), ("15", "1.5", d2)):
            _trade(s, run, sym, cfg, pnl, r, day)
        for i, dd in enumerate(("0.0200", "0.0500", "0.0100")):
            s.add(
                m.EquitySnapshot(
                    run_id=run,
                    ts=T0 + timedelta(minutes=i),
                    equity=Decimal("700"),
                    cash=Decimal("700"),
                    settled_cash=Decimal("700"),
                    peak_equity=Decimal("720"),
                    drawdown_pct=Decimal(dd),
                )
            )
        for day, followed in ((d1, True), (d2, False), (date(2026, 10, 7), None)):
            s.add(m.Journal(run_id=run, session_date=day, rules_followed=followed, notes=None, answered_via=None))
        s.commit()
        row = s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": run}).mappings().one()
        empty = s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": other}).mappings().one()
        daily = (
            s.execute(
                text("SELECT session_date, trades, realized_pnl FROM trader.v_daily_pnl ORDER BY session_date")
            )
            .mappings()
            .all()
        )
    assert row["trades"] == 4 and row["wins"] == 2
    assert row["win_rate"] == Decimal("0.5000")
    assert row["avg_win_r"] == Decimal("1.7500")
    assert row["avg_loss_r"] == Decimal("-1.0000")
    assert row["expectancy_r"] == Decimal("0.3750")
    assert row["profit_factor"] == Decimal("1.7500")
    assert row["avg_slippage"] == Decimal("0.0200")
    assert row["max_drawdown_pct"] == Decimal("0.0500")
    assert row["adherence_pct"] == Decimal("0.5000")
    assert empty["trades"] == 0 and empty["win_rate"] is None and empty["expectancy_r"] is None
    assert [(r["session_date"], r["trades"], r["realized_pnl"]) for r in daily] == [
        (d1, 2, Decimal("10.0000")),
        (d2, 2, Decimal("5.0000")),
    ]


def test_cash_ledger_trigger_exists(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        names = conn.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'trader.cash_ledger'::regclass AND NOT tgisinternal"
            )
        ).scalars()
        assert set(names) == {"cash_ledger_append_only"}


def test_timestamps_are_timestamptz(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        bad = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'trader' AND data_type = 'timestamp without time zone'"
            )
        ).all()
    assert bad == []


def test_created_at_round_trips_in_utc(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id = add_run(s, started_at=datetime.fromisoformat("2026-10-06T09:30:00-04:00"))
        s.commit()
        started = s.get(m.Run, run_id)
        assert started is not None
    assert started.started_at.utcoffset() == timedelta(0)
    assert started.started_at.hour == 13
```

- [x] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/db/test_migration_0002.py -q`
Expected: FAIL. The table tests fail because the tables don't exist; the others with `AttributeError: module 'trader.db.models' has no attribute 'Run'` (or `Position`).

- [x] **Step 3: Add the trading models to `trader/db/models.py`**

Change the import block at the top of `Trader/app/trader/db/models.py` to:
```python
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
```
and change the module docstring to `"""ORM models (SPEC §10). Phase 1 tables, then the Phase 2 trading tables (migration 0002)."""`.

Append at the end of the file:
```python


# --- Phase 2: trading (migration 0002). Every trading row carries run_id (SPEC §3a). -------------------
RUN_FK = "trader.runs.id"
SYMBOL_FK = "trader.symbols.id"
CONFIG_FK = "trader.strategy_configs.id"


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index(
            "uq_runs_one_active_live",
            "mode",
            unique=True,
            postgresql_where=text("mode = 'live' AND status = 'active'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))  # live | replay
    started_at: Mapped[datetime] = mapped_column(TS)
    params: Mapped[Any] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20))  # active | completed | failed
    label: Mapped[str | None] = mapped_column(String(200))


class SimAccount(Base):
    __tablename__ = "sim_accounts"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK), unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    starting_cash: Mapped[Decimal] = mapped_column(Money)  # in the account currency, after FX and fee
    source_amount: Mapped[Decimal] = mapped_column(Money)
    source_currency: Mapped[str] = mapped_column(String(3))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    fx_fee: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    created_at: Mapped[datetime] = mapped_column(TS)


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"
    __table_args__ = (UniqueConstraint("strategy_key", "revision", name="uq_strategy_configs_key_revision"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    params: Mapped[Any] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(String(50))


class Catalyst(Base):
    __tablename__ = "catalysts"
    __table_args__ = (UniqueConstraint("symbol_id", "session_date", name="uq_catalysts_symbol_session"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    session_date: Mapped[date] = mapped_column(Date)
    headlines: Mapped[Any] = mapped_column(JSONB, nullable=False)
    gap_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    earnings_date: Mapped[date | None] = mapped_column(Date)
    catalyst_type: Mapped[str] = mapped_column("type", String(20))
    direction: Mapped[str] = mapped_column(String(10))
    quality: Mapped[int | None] = mapped_column(Integer)
    confirmed: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(60))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    classified_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "session_date", "strategy_key", "symbol_id", name="uq_candidates_run_session_strategy_symbol"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    session_date: Mapped[date] = mapped_column(Date)
    strategy_key: Mapped[str] = mapped_column(String(50))
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    rvol: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    rank: Mapped[int | None] = mapped_column(Integer)
    candle: Mapped[Any] = mapped_column(JSONB, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    reject_reason: Mapped[str | None] = mapped_column(String(100))
    data: Mapped[Any] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_run_session", "run_id", "session_date"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    strategy_config_id: Mapped[int] = mapped_column(ForeignKey(CONFIG_FK))
    symbol_id: Mapped[int | None] = mapped_column(ForeignKey(SYMBOL_FK))
    session_date: Mapped[date] = mapped_column(Date)
    event_key: Mapped[str] = mapped_column(String(50))
    ts: Mapped[datetime] = mapped_column(TS)
    intent: Mapped[Any] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[Any] = mapped_column(JSONB, nullable=False)


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (Index("ix_proposals_run_status", "run_id", "status"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    signal_id: Mapped[int] = mapped_column(ForeignKey("trader.signals.id"))
    kind: Mapped[str] = mapped_column(String(10))  # entry | stop | exit | cancel
    order_spec: Mapped[Any] = mapped_column(JSONB, nullable=False)
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS)
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    decided_via: Mapped[str | None] = mapped_column(String(10))  # telegram | web | auto
    decided_by: Mapped[str | None] = mapped_column(String(50))
    decision_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    cancel_order_id: Mapped[int | None] = mapped_column(BigInteger)
    order_id: Mapped[int | None] = mapped_column(BigInteger)
    sizing: Mapped[Any] = mapped_column(JSONB, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(TS)
    escalated_at: Mapped[datetime | None] = mapped_column(TS)
    escalations: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_run_status", "run_id", "status"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("trader.proposals.id"))
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    strategy_config_id: Mapped[int | None] = mapped_column(ForeignKey(CONFIG_FK))
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    order_type: Mapped[str] = mapped_column(String(12))  # market | limit | stop | stop_limit
    purpose: Mapped[str] = mapped_column(String(10))  # entry | stop | exit
    qty: Mapped[int] = mapped_column(Integer)
    stop_price: Mapped[Decimal | None] = mapped_column(Money)
    limit_price: Mapped[Decimal | None] = mapped_column(Money)
    stop_loss: Mapped[Decimal | None] = mapped_column(Money)
    tif: Mapped[str] = mapped_column(String(3))  # day | gtc
    status: Mapped[str] = mapped_column(String(12))  # working | filled | cancelled
    reason: Mapped[str] = mapped_column(String(100))
    session_date: Mapped[date] = mapped_column(Date)
    submitted_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    cancel_reason: Mapped[str | None] = mapped_column(String(100))
    stale_since: Mapped[datetime | None] = mapped_column(TS)
    stale_alerted: Mapped[bool] = mapped_column(Boolean)


class Fill(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    order_id: Mapped[int] = mapped_column(ForeignKey("trader.orders.id"), unique=True)
    ts: Mapped[datetime] = mapped_column(TS)
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Money)
    fees: Mapped[Any] = mapped_column(JSONB, nullable=False)
    quote_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    slippage: Mapped[Decimal] = mapped_column(Money)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_run_closed", "run_id", "closed_at"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    strategy_config_id: Mapped[int | None] = mapped_column(ForeignKey(CONFIG_FK))
    qty: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[Decimal] = mapped_column(Money)
    stop_loss: Mapped[Decimal | None] = mapped_column(Money)
    planned_risk: Mapped[Decimal | None] = mapped_column(Money)
    session_date: Mapped[date] = mapped_column(Date)
    opened_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    entry_order_id: Mapped[int] = mapped_column(BigInteger)
    stop_order_id: Mapped[int | None] = mapped_column(BigInteger)
    unprotected_since: Mapped[datetime | None] = mapped_column(TS)
    unprotected_seconds: Mapped[int] = mapped_column(Integer)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    position_id: Mapped[int] = mapped_column(ForeignKey("trader.positions.id"), unique=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey(SYMBOL_FK))
    session_date: Mapped[date] = mapped_column(Date)
    entry_price: Mapped[Decimal] = mapped_column(Money)
    exit_price: Mapped[Decimal] = mapped_column(Money)
    qty: Mapped[int] = mapped_column(Integer)
    pnl: Mapped[Decimal] = mapped_column(Money)
    pnl_r: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    planned_risk: Mapped[Decimal | None] = mapped_column(Money)
    exit_reason: Mapped[str] = mapped_column(String(50))
    slippage_total: Mapped[Decimal] = mapped_column(Money)
    fees_total: Mapped[Decimal] = mapped_column(Money)
    opened_at: Mapped[datetime] = mapped_column(TS)
    closed_at: Mapped[datetime] = mapped_column(TS)


class CashLedger(Base):
    __tablename__ = "cash_ledger"
    __table_args__ = (Index("ix_cash_ledger_run_settle", "run_id", "settle_date"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    ts: Mapped[datetime] = mapped_column(TS)
    trade_date: Mapped[date] = mapped_column(Date)
    settle_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Money)
    kind: Mapped[str] = mapped_column(String(10))  # deposit | buy | sell | fee
    ref: Mapped[str] = mapped_column(String(100))


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    equity: Mapped[Decimal] = mapped_column(Money)
    cash: Mapped[Decimal] = mapped_column(Money)
    settled_cash: Mapped[Decimal] = mapped_column(Money)
    peak_equity: Mapped[Decimal] = mapped_column(Money)
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4))


class Journal(Base):
    __tablename__ = "journal"
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rules_followed: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    answered_via: Mapped[str | None] = mapped_column(String(20))
    updated_at: Mapped[datetime | None] = mapped_column(TS)


class KillSwitchEvent(Base):
    __tablename__ = "kill_switch_events"
    __table_args__ = (Index("ix_kill_switch_events_run", "run_id", "switch"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(RUN_FK))
    switch: Mapped[str] = mapped_column(String(30))
    session_date: Mapped[date] = mapped_column(Date)
    tripped_at: Mapped[datetime] = mapped_column(TS)
    value: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    reset_at: Mapped[datetime | None] = mapped_column(TS)
    reset_reason: Mapped[str | None] = mapped_column(Text)
    reset_by: Mapped[str | None] = mapped_column(String(50))
```

- [x] **Step 4: Write migration `0002_trading.py`**

`Trader/app/trader/db/migrations/versions/0002_trading.py`:
```python
"""Phase 2 trading tables, views and the cash-ledger append-only trigger.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

S = "trader"
MONEY = sa.Numeric(14, 4)
TS = sa.DateTime(timezone=True)


def _fk(table: str) -> sa.ForeignKey:
    return sa.ForeignKey(f"{S}.{table}.id")


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _run_id(unique: bool = False) -> sa.Column[int]:
    return sa.Column("run_id", sa.BigInteger, _fk("runs"), nullable=False, unique=unique)


V_TRADE_METRICS = """
CREATE VIEW trader.v_trade_metrics AS
WITH t AS (
    SELECT run_id,
           count(*) AS trades,
           count(*) FILTER (WHERE pnl > 0) AS wins,
           avg(pnl_r) FILTER (WHERE pnl > 0) AS avg_win_r,
           avg(pnl_r) FILTER (WHERE pnl <= 0) AS avg_loss_r,
           avg(pnl_r) AS expectancy_r,
           sum(pnl) FILTER (WHERE pnl > 0) AS gross_win,
           -sum(pnl) FILTER (WHERE pnl < 0) AS gross_loss,
           avg(slippage_total) AS avg_slippage
    FROM trader.trades
    GROUP BY run_id
), d AS (
    SELECT run_id, max(drawdown_pct) AS max_drawdown_pct FROM trader.equity_snapshots GROUP BY run_id
), j AS (
    SELECT run_id,
           count(*) FILTER (WHERE rules_followed) AS followed,
           count(rules_followed) AS answered
    FROM trader.journal
    GROUP BY run_id
)
SELECT r.id AS run_id,
       coalesce(t.trades, 0) AS trades,
       coalesce(t.wins, 0) AS wins,
       CASE WHEN t.trades > 0 THEN round(t.wins::numeric / t.trades, 4) END AS win_rate,
       round(t.avg_win_r, 4) AS avg_win_r,
       round(t.avg_loss_r, 4) AS avg_loss_r,
       round(t.expectancy_r, 4) AS expectancy_r,
       CASE WHEN t.gross_loss > 0 THEN round(coalesce(t.gross_win, 0) / t.gross_loss, 4) END AS profit_factor,
       round(t.avg_slippage, 4) AS avg_slippage,
       d.max_drawdown_pct,
       CASE WHEN j.answered > 0 THEN round(j.followed::numeric / j.answered, 4) END AS adherence_pct
FROM trader.runs r
LEFT JOIN t ON t.run_id = r.id
LEFT JOIN d ON d.run_id = r.id
LEFT JOIN j ON j.run_id = r.id
"""

V_DAILY_PNL = """
CREATE VIEW trader.v_daily_pnl AS
SELECT run_id, session_date, count(*) AS trades, sum(pnl) AS realized_pnl, sum(fees_total) AS fees
FROM trader.trades
GROUP BY run_id, session_date
"""

APPEND_ONLY_FN = f"""
CREATE FUNCTION {S}.cash_ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'trader.cash_ledger is append-only (% refused)', TG_OP;
END
$$
"""

APPEND_ONLY_TRIGGER = f"""
CREATE TRIGGER cash_ledger_append_only BEFORE UPDATE OR DELETE ON {S}.cash_ledger
FOR EACH ROW EXECUTE FUNCTION {S}.cash_ledger_append_only()
"""


def upgrade() -> None:
    op.create_table(
        "runs",
        _id(),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("label", sa.String(200)),
        schema=S,
    )
    op.create_index(
        "uq_runs_one_active_live",
        "runs",
        ["mode"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("mode = 'live' AND status = 'active'"),
    )
    op.create_table(
        "sim_accounts",
        _id(),
        _run_id(unique=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("starting_cash", MONEY, nullable=False),
        sa.Column("source_amount", MONEY, nullable=False),
        sa.Column("source_currency", sa.String(3), nullable=False),
        sa.Column("fx_rate", sa.Numeric(12, 6)),
        sa.Column("fx_fee", sa.Numeric(6, 4)),
        sa.Column("created_at", TS, nullable=False),
        schema=S,
    )
    op.create_table(
        "strategy_configs",
        _id(),
        sa.Column("strategy_key", sa.String(50), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("created_by", sa.String(50), nullable=False),
        sa.UniqueConstraint("strategy_key", "revision", name="uq_strategy_configs_key_revision"),
        schema=S,
    )
    op.create_table(
        "catalysts",
        _id(),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("headlines", JSONB, nullable=False),
        sa.Column("gap_pct", sa.Numeric(10, 4)),
        sa.Column("earnings_date", sa.Date),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("quality", sa.Integer),
        sa.Column("confirmed", sa.Boolean),
        sa.Column("reason", sa.Text),
        sa.Column("model", sa.String(60)),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("classified_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("symbol_id", "session_date", name="uq_catalysts_symbol_session"),
        schema=S,
    )
    op.create_table(
        "candidates",
        _id(),
        _run_id(),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("strategy_key", sa.String(50), nullable=False),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("rvol", sa.Numeric(12, 4)),
        sa.Column("rank", sa.Integer),
        sa.Column("candle", JSONB),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("reject_reason", sa.String(100)),
        sa.Column("data", JSONB),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint(
            "run_id", "session_date", "strategy_key", "symbol_id", name="uq_candidates_run_session_strategy_symbol"
        ),
        schema=S,
    )
    op.create_table(
        "signals",
        _id(),
        _run_id(),
        sa.Column("strategy_config_id", sa.BigInteger, _fk("strategy_configs"), nullable=False),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols")),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("event_key", sa.String(50), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("intent", JSONB, nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        schema=S,
    )
    op.create_index("ix_signals_run_session", "signals", ["run_id", "session_date"], schema=S)
    op.create_table(
        "proposals",
        _id(),
        _run_id(),
        sa.Column("signal_id", sa.BigInteger, _fk("signals"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("order_spec", JSONB, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("decided_at", TS),
        sa.Column("decided_via", sa.String(10)),
        sa.Column("decided_by", sa.String(50)),
        sa.Column("decision_latency_ms", sa.BigInteger),
        sa.Column("position_id", sa.BigInteger),
        sa.Column("cancel_order_id", sa.BigInteger),
        sa.Column("order_id", sa.BigInteger),
        sa.Column("sizing", JSONB),
        sa.Column("expired_at", TS),
        sa.Column("escalated_at", TS),
        sa.Column("escalations", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
        schema=S,
    )
    op.create_index("ix_proposals_run_status", "proposals", ["run_id", "status"], schema=S)
    op.create_table(
        "orders",
        _id(),
        _run_id(),
        sa.Column("proposal_id", sa.BigInteger, _fk("proposals")),
        sa.Column("position_id", sa.BigInteger),
        sa.Column("strategy_config_id", sa.BigInteger, _fk("strategy_configs")),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("order_type", sa.String(12), nullable=False),
        sa.Column("purpose", sa.String(10), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("stop_price", MONEY),
        sa.Column("limit_price", MONEY),
        sa.Column("stop_loss", MONEY),
        sa.Column("tif", sa.String(3), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("submitted_at", TS, nullable=False),
        sa.Column("closed_at", TS),
        sa.Column("cancel_reason", sa.String(100)),
        sa.Column("stale_since", TS),
        sa.Column("stale_alerted", sa.Boolean, nullable=False),
        schema=S,
    )
    op.create_index("ix_orders_run_status", "orders", ["run_id", "status"], schema=S)
    op.create_table(
        "fills",
        _id(),
        _run_id(),
        sa.Column("order_id", sa.BigInteger, _fk("orders"), nullable=False, unique=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("fees", JSONB, nullable=False),
        sa.Column("quote_snapshot", JSONB, nullable=False),
        sa.Column("slippage", MONEY, nullable=False),
        schema=S,
    )
    op.create_table(
        "positions",
        _id(),
        _run_id(),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("strategy_config_id", sa.BigInteger, _fk("strategy_configs")),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("avg_price", MONEY, nullable=False),
        sa.Column("stop_loss", MONEY),
        sa.Column("planned_risk", MONEY),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("closed_at", TS),
        sa.Column("entry_order_id", sa.BigInteger, nullable=False),
        sa.Column("stop_order_id", sa.BigInteger),
        sa.Column("unprotected_since", TS),
        sa.Column("unprotected_seconds", sa.Integer, nullable=False),
        schema=S,
    )
    op.create_index("ix_positions_run_closed", "positions", ["run_id", "closed_at"], schema=S)
    op.create_table(
        "trades",
        _id(),
        _run_id(),
        sa.Column("position_id", sa.BigInteger, _fk("positions"), nullable=False, unique=True),
        sa.Column("symbol_id", sa.BigInteger, _fk("symbols"), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("entry_price", MONEY, nullable=False),
        sa.Column("exit_price", MONEY, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("pnl", MONEY, nullable=False),
        sa.Column("pnl_r", sa.Numeric(10, 4)),
        sa.Column("planned_risk", MONEY),
        sa.Column("exit_reason", sa.String(50), nullable=False),
        sa.Column("slippage_total", MONEY, nullable=False),
        sa.Column("fees_total", MONEY, nullable=False),
        sa.Column("opened_at", TS, nullable=False),
        sa.Column("closed_at", TS, nullable=False),
        schema=S,
    )
    op.create_table(
        "cash_ledger",
        _id(),
        _run_id(),
        sa.Column("ts", TS, nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("settle_date", sa.Date, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("ref", sa.String(100), nullable=False),
        schema=S,
    )
    op.create_index("ix_cash_ledger_run_settle", "cash_ledger", ["run_id", "settle_date"], schema=S)
    op.execute(APPEND_ONLY_FN)
    op.execute(APPEND_ONLY_TRIGGER)
    op.create_table(
        "equity_snapshots",
        sa.Column("run_id", sa.BigInteger, _fk("runs"), primary_key=True),
        sa.Column("ts", TS, primary_key=True),
        sa.Column("equity", MONEY, nullable=False),
        sa.Column("cash", MONEY, nullable=False),
        sa.Column("settled_cash", MONEY, nullable=False),
        sa.Column("peak_equity", MONEY, nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(8, 4), nullable=False),
        schema=S,
    )
    op.create_table(
        "journal",
        sa.Column("run_id", sa.BigInteger, _fk("runs"), primary_key=True),
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("rules_followed", sa.Boolean),
        sa.Column("notes", sa.Text),
        sa.Column("answered_via", sa.String(20)),
        sa.Column("updated_at", TS),
        schema=S,
    )
    op.create_table(
        "kill_switch_events",
        _id(),
        _run_id(),
        sa.Column("switch", sa.String(30), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("tripped_at", TS, nullable=False),
        sa.Column("value", sa.Numeric(14, 6)),
        sa.Column("threshold", sa.Numeric(14, 6)),
        sa.Column("reset_at", TS),
        sa.Column("reset_reason", sa.Text),
        sa.Column("reset_by", sa.String(50)),
        schema=S,
    )
    op.create_index("ix_kill_switch_events_run", "kill_switch_events", ["run_id", "switch"], schema=S)
    op.execute(V_TRADE_METRICS)
    op.execute(V_DAILY_PNL)


def downgrade() -> None:
    op.execute(f"DROP VIEW {S}.v_daily_pnl")
    op.execute(f"DROP VIEW {S}.v_trade_metrics")
    for table in (
        "kill_switch_events",
        "journal",
        "equity_snapshots",
        "cash_ledger",
        "trades",
        "positions",
        "fills",
        "orders",
        "proposals",
        "signals",
        "candidates",
        "catalysts",
        "strategy_configs",
        "sim_accounts",
        "runs",
    ):
        op.drop_table(table, schema=S)  # drops their indexes and the cash_ledger trigger too
    op.execute(f"DROP FUNCTION {S}.cash_ledger_append_only()")
```

If `test_models_match_migrated_schema` reports a difference, the model and the migration disagree: fix whichever side differs from the Interfaces list above (nullability, a named constraint or index, a type). Do not filter the comparison.

- [x] **Step 5: Stop a Phase 1 gauntlet test from pinning the head revision**

`tests/gauntlet/test_p1_t2_breaker.py::test_upgrade_at_head_is_a_noop` ends with `assert after[0] == ["0001"]`, which every new migration breaks. Replace that one line with a check against the script directory's head, so it keeps testing what it meant (upgrading at head changes nothing):
```python
    from alembic.script import ScriptDirectory

    assert after[0] == [ScriptDirectory.from_config(cfg).get_current_head()]
```

- [x] **Step 6: Run the new and the Phase 1 migration tests**

Run: `uv --directory Trader/app run pytest tests/db tests/gauntlet/test_p1_t2_breaker.py -q`
Expected: all pass, including P1's `test_models_match_migrated_schema`, `test_alembic_check_through_env_sees_no_changes`, `test_downgrade_and_upgrade_again` and `test_upgrade_at_head_is_a_noop`.

- [x] **Step 7: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/db/models.py Trader/app/trader/db/migrations/versions/0002_trading.py Trader/app/tests/factories.py Trader/app/tests/db/test_migration_0002.py Trader/app/tests/gauntlet/test_p1_t2_breaker.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T1: migration 0002 with trading tables, metric views and append-only cash ledger" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

- [x] **Step 8: LIVE: apply migration 0002 to `trader_dev`**

From your worktree (the env file lives only in the main checkout; `--env-file` is resolved from `Trader/app`):
`uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev alembic upgrade head`
Expected: `Running upgrade 0001 -> 0002`. Then run `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev alembic current` → `0002 (head)`. (`env.py` reads `MIGRATION_DATABASE_URL`, the owner role, from that file.) Whether the app role `trader_dev_app` can write the new tables is checked by P2-T14's LIVE step, the first one that writes them.

---

### Task P2-T2: Runs, sim account and the full runtime settings set

**Files:**
- Modify: `Trader/app/trader/settings_store.py` (add imports and the Phase 2 fields)
- Create: `Trader/app/trader/engine/__init__.py`, `Trader/app/trader/engine/runs.py`, `Trader/app/tests/test_runtime_settings_phase2.py`, `Trader/app/tests/engine/__init__.py`, `Trader/app/tests/engine/test_runs.py`

**Interfaces:**
- Consumes: `RuntimeSettings`, `SettingsStore` (P1-T3; keys are aliases, validated one key at a time); models `Run`, `SimAccount`, `CashLedger` (P2-T1); `session_scope` (P1-T2); `Clock`, `et_date` (P1-T4).
- Produces:
  - New `RuntimeSettings` fields (field name → DB key, default, bounds). All `Decimal` fields are finite (`allow_inf_nan=False`).

    | Field | DB key | Default | Bounds |
    |---|---|---|---|
    | `starting_cash: Decimal` | `starting_cash` | `720` | > 0, ≤ 10,000,000 |
    | `starting_cash_currency: Literal["USD","CAD"]` | `starting_cash_currency` | `"USD"` | |
    | `account_currency: Literal["USD","CAD"]` | `account_currency` | `"USD"` | |
    | `fx_cad_usd_rate: Decimal` | `fx.cad_usd_rate` | `0.72` | > 0, ≤ 2 |
    | `fx_fee_pct: Decimal` | `fx.fee_pct` | `0.015` | 0–0.10 |
    | `cash_account_mode: bool` | `cash_account_mode` | `True` | |
    | `risk_pct: Decimal` | `risk_pct` | `0.02` | > 0, ≤ 0.10 |
    | `slippage_buffer: Decimal` | `slippage_buffer` | `0.005` | 0–0.05 |
    | `no_entry_before_close_minutes: int` | `no_entry_before_close_minutes` | `30` | 0–390 |
    | `quote_poll_seconds: float` | `quote_poll_seconds` | `2.0` | 1–60 |
    | `stale_quote_seconds: float` | `stale_quote_seconds` | `10.0` | 1–300 |
    | `slippage_min: Decimal` | `slippage_min` | `0.01` | 0–1 |
    | `slippage_bps: Decimal` | `slippage_bps` | `5` | 0–100 |
    | `fees_commission: Decimal` | `fees.commission` | `0` | 0–100 |
    | `fees_direct_route: bool` | `fees.direct_route` | `False` | |
    | `fees_ecn_per_share: Decimal` | `fees.ecn_per_share` | `0.0035` | 0–1 |
    | `fees_sec_rate: Decimal` | `fees.sec_rate` | `0.0000206` | 0–0.001 |
    | `proposal_ttl_entry_seconds: int` | `proposal_ttl_entry_seconds` | `300` | 30–3600 |
    | `proposal_ttl_stop_seconds: int` | `proposal_ttl_stop_seconds` | `180` | 30–3600 |
    | `proposal_ttl_exit_seconds: int` | `proposal_ttl_exit_seconds` | `300` | 30–3600 |
    | `stop_escalation_seconds: int` | `stop_escalation_seconds` | `60` | 10–3600 |
    | `auto_flatten_on_expiry: bool` | `auto_flatten_on_expiry` | `True` | |
    | `killswitch_daily_loss_pct: Decimal` | `killswitch.daily_loss_pct` | `0.05` | > 0, ≤ 1 |
    | `killswitch_max_drawdown_pct: Decimal` | `killswitch.max_drawdown_pct` | `0.15` | > 0, ≤ 1 |
    | `killswitch_expectancy_min_trades: int` | `killswitch.expectancy_min_trades` | `50` | 1–10,000 |
    | `killswitch_expectancy_threshold_r: Decimal` | `killswitch.expectancy_threshold_r` | `0` | −10–10 |
    | `claude_model: Literal["claude-sonnet-5","claude-haiku-4-5"]` | `claude.model` | `"claude-sonnet-5"` | |
    | `claude_daily_budget_usd: Decimal` | `claude.daily_budget_usd` | `1.00` | 0–100 |
    | `claude_premarket_max_candidates: int` | `claude.premarket_max_candidates` | `50` | 0–500 |
    | `premarket_gap_min_pct: Decimal` | `premarket.gap_min_pct` | `0.03` | > 0, ≤ 1 |
    | `premarket_news_filter: str` | `premarket.news_filter` | `"news_date_today"` | FinViz filter pattern |
    | `premarket_earnings_filter: str` | `premarket.earnings_filter` | `"earningsdate_today"` | FinViz filter pattern |

    The proposal TTL for a `cancel` proposal is `proposal_ttl_exit_seconds`. Percentages are fractions (`0.02` = 2%). `slippage_bps` is basis points (`5` = 0.05%).
  - `trader.engine.runs`: `RunInfo(id, mode, started_at, label)`; `SimAccountInfo(id, run_id, currency, starting_cash, source_amount, source_currency, fx_rate, fx_fee)`; `StartingBalance(amount, fx_rate, fx_fee)`; `starting_balance(settings) -> StartingBalance`; `ensure_sim_account(session, run_id, settings, now) -> SimAccountInfo` (creates the account and its one `deposit` ledger row once; the deposit settles on its own trade date); `get_live_run(factory, clock, settings) -> RunInfo` (creates the one active live run and its account, or returns the existing one; safe under concurrency via the partial unique index); `sim_account(factory, run_id) -> SimAccountInfo | None`.

- [x] **Step 1: Write the failing settings tests**

`Trader/app/tests/test_runtime_settings_phase2.py`:
```python
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from trader.settings_store import RuntimeSettings, SettingsStore

UNALIASED_PHASE1_FIELDS = {"approval_mode", "markets_enabled"}  # the only fields whose key is their name
PHASE2_KEYS = {
    "starting_cash",
    "starting_cash_currency",
    "account_currency",
    "fx.cad_usd_rate",
    "fx.fee_pct",
    "cash_account_mode",
    "risk_pct",
    "slippage_buffer",
    "no_entry_before_close_minutes",
    "quote_poll_seconds",
    "stale_quote_seconds",
    "slippage_min",
    "slippage_bps",
    "fees.commission",
    "fees.direct_route",
    "fees.ecn_per_share",
    "fees.sec_rate",
    "proposal_ttl_entry_seconds",
    "proposal_ttl_stop_seconds",
    "proposal_ttl_exit_seconds",
    "stop_escalation_seconds",
    "auto_flatten_on_expiry",
    "killswitch.daily_loss_pct",
    "killswitch.max_drawdown_pct",
    "killswitch.expectancy_min_trades",
    "killswitch.expectancy_threshold_r",
    "claude.model",
    "claude.daily_budget_usd",
    "claude.premarket_max_candidates",
    "premarket.gap_min_pct",
    "premarket.news_filter",
    "premarket.earnings_filter",
}


def test_phase2_defaults() -> None:
    s = RuntimeSettings()
    assert s.starting_cash == Decimal("720") and s.account_currency == "USD"
    assert s.starting_cash_currency == "USD" and s.fx_fee_pct == Decimal("0.015")
    assert s.cash_account_mode is True
    assert s.risk_pct == Decimal("0.02")
    assert (s.slippage_min, s.slippage_bps) == (Decimal("0.01"), Decimal("5"))
    assert (s.proposal_ttl_entry_seconds, s.proposal_ttl_stop_seconds, s.proposal_ttl_exit_seconds) == (300, 180, 300)
    assert s.killswitch_daily_loss_pct == Decimal("0.05")
    assert s.killswitch_max_drawdown_pct == Decimal("0.15")
    assert s.killswitch_expectancy_min_trades == 50
    assert s.killswitch_expectancy_threshold_r == Decimal("0")
    assert s.no_entry_before_close_minutes == 30
    assert s.quote_poll_seconds == 2.0 and s.stale_quote_seconds == 10.0
    assert s.auto_flatten_on_expiry is True
    assert s.claude_model == "claude-sonnet-5"
    assert s.claude_daily_budget_usd == Decimal("1.00")
    assert s.claude_premarket_max_candidates == 50
    assert s.fees_sec_rate == Decimal("0.0000206") and s.fees_direct_route is False
    assert s.premarket_gap_min_pct == Decimal("0.03")


def test_every_phase2_field_has_an_alias_key() -> None:
    fields = RuntimeSettings.model_fields
    missing = [name for name, f in fields.items() if f.alias is None and name not in UNALIASED_PHASE1_FIELDS]
    assert missing == []
    assert PHASE2_KEYS <= {f.alias for f in fields.values()}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("risk_pct", "0"),
        ("risk_pct", "0.5"),
        ("risk_pct", "NaN"),
        ("starting_cash", "-1"),
        ("starting_cash_currency", "EUR"),
        ("slippage_bps", "101"),
        ("stale_quote_seconds", 0.5),
        ("proposal_ttl_entry_seconds", 10),
        ("killswitch.daily_loss_pct", "0"),
        ("killswitch.expectancy_min_trades", 0),
        ("claude.model", "gpt-4"),
        ("claude.daily_budget_usd", "Infinity"),
        ("premarket.news_filter", "news date"),
    ],
)
def test_invalid_values_rejected(key: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({key: value})


def test_round_trips_through_json() -> None:
    s = RuntimeSettings()
    assert RuntimeSettings.model_validate(s.model_dump(mode="json", by_alias=True)) == s


@pytest.mark.db
def test_store_sets_dotted_phase2_key(db_factory: sessionmaker[Session]) -> None:
    store = SettingsStore(db_factory, now=lambda: datetime(2026, 10, 6, 12, 0, tzinfo=UTC))
    store.set("killswitch.daily_loss_pct", "0.04", actor="stephen")
    store.set("claude.model", "claude-haiku-4-5", actor="stephen")
    loaded = store.load()
    assert loaded.killswitch_daily_loss_pct == Decimal("0.04")
    assert loaded.claude_model == "claude-haiku-4-5"
```

- [x] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/test_runtime_settings_phase2.py -q`
Expected: FAIL with `AttributeError: 'RuntimeSettings' object has no attribute 'starting_cash'`.

- [x] **Step 3: Add the Phase 2 fields to `RuntimeSettings`**

In `Trader/app/trader/settings_store.py` add `from decimal import Decimal` to the imports, then add below `Ticker = ...`:
```python
Currency = Literal["USD", "CAD"]
ClaudeModel = Literal["claude-sonnet-5", "claude-haiku-4-5"]
```
Add these fields to `RuntimeSettings`, after `open_bar_lookback_sessions` and before the validators:
```python
    # --- Phase 2: account and FX (SPEC §7.3, BR-22)
    starting_cash: Decimal = Field(
        Decimal("720"), gt=0, le=Decimal("10000000"), allow_inf_nan=False, alias="starting_cash"
    )
    starting_cash_currency: Currency = Field("USD", alias="starting_cash_currency")
    account_currency: Currency = Field("USD", alias="account_currency")
    fx_cad_usd_rate: Decimal = Field(
        Decimal("0.72"), gt=0, le=Decimal("2"), allow_inf_nan=False, alias="fx.cad_usd_rate"
    )
    fx_fee_pct: Decimal = Field(Decimal("0.015"), ge=0, le=Decimal("0.10"), allow_inf_nan=False, alias="fx.fee_pct")
    cash_account_mode: bool = Field(True, alias="cash_account_mode")
    # --- risk (SPEC §6.1, BR-40)
    risk_pct: Decimal = Field(Decimal("0.02"), gt=0, le=Decimal("0.10"), allow_inf_nan=False, alias="risk_pct")
    slippage_buffer: Decimal = Field(
        Decimal("0.005"), ge=0, le=Decimal("0.05"), allow_inf_nan=False, alias="slippage_buffer"
    )
    no_entry_before_close_minutes: int = Field(30, ge=0, le=390, alias="no_entry_before_close_minutes")
    # --- fill model (SPEC §7.2)
    quote_poll_seconds: float = Field(2.0, ge=1.0, le=60, allow_inf_nan=False, alias="quote_poll_seconds")
    stale_quote_seconds: float = Field(10.0, ge=1.0, le=300, allow_inf_nan=False, alias="stale_quote_seconds")
    slippage_min: Decimal = Field(Decimal("0.01"), ge=0, le=Decimal("1"), allow_inf_nan=False, alias="slippage_min")
    slippage_bps: Decimal = Field(Decimal("5"), ge=0, le=Decimal("100"), allow_inf_nan=False, alias="slippage_bps")
    fees_commission: Decimal = Field(
        Decimal("0"), ge=0, le=Decimal("100"), allow_inf_nan=False, alias="fees.commission"
    )
    fees_direct_route: bool = Field(False, alias="fees.direct_route")
    fees_ecn_per_share: Decimal = Field(
        Decimal("0.0035"), ge=0, le=Decimal("1"), allow_inf_nan=False, alias="fees.ecn_per_share"
    )
    fees_sec_rate: Decimal = Field(
        Decimal("0.0000206"), ge=0, le=Decimal("0.001"), allow_inf_nan=False, alias="fees.sec_rate"
    )
    # --- proposals (SPEC §6.2)
    proposal_ttl_entry_seconds: int = Field(300, ge=30, le=3600, alias="proposal_ttl_entry_seconds")
    proposal_ttl_stop_seconds: int = Field(180, ge=30, le=3600, alias="proposal_ttl_stop_seconds")
    proposal_ttl_exit_seconds: int = Field(300, ge=30, le=3600, alias="proposal_ttl_exit_seconds")
    stop_escalation_seconds: int = Field(60, ge=10, le=3600, alias="stop_escalation_seconds")
    auto_flatten_on_expiry: bool = Field(True, alias="auto_flatten_on_expiry")
    # --- kill switches (SPEC §6.3, BR-41)
    killswitch_daily_loss_pct: Decimal = Field(
        Decimal("0.05"), gt=0, le=Decimal("1"), allow_inf_nan=False, alias="killswitch.daily_loss_pct"
    )
    killswitch_max_drawdown_pct: Decimal = Field(
        Decimal("0.15"), gt=0, le=Decimal("1"), allow_inf_nan=False, alias="killswitch.max_drawdown_pct"
    )
    killswitch_expectancy_min_trades: int = Field(50, ge=1, le=10000, alias="killswitch.expectancy_min_trades")
    killswitch_expectancy_threshold_r: Decimal = Field(
        Decimal("0"), ge=Decimal("-10"), le=Decimal("10"), allow_inf_nan=False, alias="killswitch.expectancy_threshold_r"
    )
    # --- Claude (SPEC §4.3)
    claude_model: ClaudeModel = Field("claude-sonnet-5", alias="claude.model")
    claude_daily_budget_usd: Decimal = Field(
        Decimal("1.00"), ge=0, le=Decimal("100"), allow_inf_nan=False, alias="claude.daily_budget_usd"
    )
    claude_premarket_max_candidates: int = Field(50, ge=0, le=500, alias="claude.premarket_max_candidates")
    # --- pre-market scan (SPEC §4.2)
    premarket_gap_min_pct: Decimal = Field(
        Decimal("0.03"), gt=0, le=Decimal("1"), allow_inf_nan=False, alias="premarket.gap_min_pct"
    )
    premarket_news_filter: str = Field(
        "news_date_today", pattern=FINVIZ_FILTERS_PATTERN, alias="premarket.news_filter"
    )
    premarket_earnings_filter: str = Field(
        "earningsdate_today", pattern=FINVIZ_FILTERS_PATTERN, alias="premarket.earnings_filter"
    )
```
Also update the comment above `model_config` to: `# The DB key of a setting is its alias. Every field added after Phase 1 declares alias= (even when the key equals the field name), because _DB_KEYS and model_dump(by_alias=True) are built from alias. No model_validator: keys are validated one at a time.`

- [x] **Step 4: Run the settings tests, including Phase 1's**

Run: `uv --directory Trader/app run pytest tests/test_runtime_settings_phase2.py tests/db/test_settings_store.py -q`
Expected: all pass. (`Decimal` values are stored as JSON strings by `model_dump(mode="json")`, which validates back to `Decimal`.)

- [x] **Step 5: Write the failing run and sim-account tests**

`Trader/app/tests/engine/__init__.py`: empty file.

`Trader/app/tests/engine/test_runs.py`:
```python
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.engine.runs import get_live_run, sim_account, starting_balance
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))  # Tue 08:00 ET


def test_starting_balance_same_currency_has_no_fx() -> None:
    bal = starting_balance(RuntimeSettings())
    assert bal.amount == Decimal("720.0000") and bal.fx_rate is None and bal.fx_fee is None


def test_starting_balance_cad_to_usd_applies_rate_and_fee() -> None:
    s = RuntimeSettings(starting_cash=Decimal("1000"), starting_cash_currency="CAD", fx_cad_usd_rate=Decimal("0.73"))
    bal = starting_balance(s)
    assert bal.amount == Decimal("719.0500")  # 1000 x 0.73 x (1 - 0.015)
    assert bal.fx_rate == Decimal("0.730000") and bal.fx_fee == Decimal("0.015")


def test_starting_balance_usd_to_cad_uses_the_inverse_rate() -> None:
    s = RuntimeSettings(
        starting_cash=Decimal("730"), account_currency="CAD", fx_cad_usd_rate=Decimal("0.73"), fx_fee_pct=Decimal("0")
    )
    assert starting_balance(s).amount == Decimal("1000.0000")


@pytest.mark.db
def test_live_run_created_once_and_reused(db_factory: sessionmaker[Session]) -> None:
    first = get_live_run(db_factory, CLOCK, RuntimeSettings())
    second = get_live_run(db_factory, CLOCK, RuntimeSettings())
    assert first.id == second.id and first.mode == "live"
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Run)).scalar_one() == 1
        params = s.execute(select(m.Run.params)).scalar_one()
    assert params["settings"]["risk_pct"] == "0.02"


@pytest.mark.db
def test_concurrent_get_live_run_creates_one_run_and_one_deposit(db_factory: sessionmaker[Session]) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = set(pool.map(lambda _: get_live_run(db_factory, CLOCK, RuntimeSettings()).id, range(8)))
    assert len(ids) == 1
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.SimAccount)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(m.CashLedger)).scalar_one() == 1


@pytest.mark.db
def test_cad_account_deposit_applies_fx_once(db_factory: sessionmaker[Session]) -> None:
    s_cad = RuntimeSettings(starting_cash=Decimal("1000"), starting_cash_currency="CAD", fx_cad_usd_rate=Decimal("0.73"))
    run = get_live_run(db_factory, CLOCK, s_cad)
    get_live_run(db_factory, CLOCK, s_cad)
    acct = sim_account(db_factory, run.id)
    assert acct is not None
    assert acct.currency == "USD" and acct.starting_cash == Decimal("719.0500")
    assert acct.source_amount == Decimal("1000.0000") and acct.source_currency == "CAD"
    assert acct.fx_rate == Decimal("0.730000") and acct.fx_fee == Decimal("0.0150")
    with db_factory() as s:
        rows = s.execute(select(m.CashLedger)).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "deposit" and rows[0].amount == Decimal("719.0500")
    assert rows[0].trade_date == rows[0].settle_date == date(2026, 10, 6)
```

- [x] **Step 6: Run to see them fail**

Run: `uv --directory Trader/app run pytest tests/engine/test_runs.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.engine'`.

- [x] **Step 7: Implement `trader/engine/runs.py`**

`Trader/app/trader/engine/__init__.py`: empty file.

`Trader/app/trader/engine/runs.py`:
```python
"""The live run and its simulated account (SPEC §3a, §7.3, BR-22)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock, et_date
from trader.settings_store import RuntimeSettings

Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
LIVE_WHERE = text("mode = 'live' AND status = 'active'")


@dataclass(frozen=True, slots=True)
class RunInfo:
    id: int
    mode: str
    started_at: datetime
    label: str | None


@dataclass(frozen=True, slots=True)
class SimAccountInfo:
    id: int
    run_id: int
    currency: str
    starting_cash: Decimal
    source_amount: Decimal
    source_currency: str
    fx_rate: Decimal | None
    fx_fee: Decimal | None


@dataclass(frozen=True, slots=True)
class StartingBalance:
    amount: Decimal
    fx_rate: Decimal | None
    fx_fee: Decimal | None


def starting_balance(settings: RuntimeSettings) -> StartingBalance:
    """`starting_cash` converted once into the account currency, less the conversion fee (SPEC §7.3)."""
    src, dst = settings.starting_cash_currency, settings.account_currency
    if src == dst:
        return StartingBalance(settings.starting_cash.quantize(Q4, ROUND_HALF_UP), None, None)
    rate = settings.fx_cad_usd_rate if (src, dst) == ("CAD", "USD") else Decimal(1) / settings.fx_cad_usd_rate
    amount = (settings.starting_cash * rate * (1 - settings.fx_fee_pct)).quantize(Q4, ROUND_HALF_UP)
    return StartingBalance(amount, rate.quantize(Q6, ROUND_HALF_UP), settings.fx_fee_pct)


def _info(acct: m.SimAccount) -> SimAccountInfo:
    return SimAccountInfo(
        id=acct.id,
        run_id=acct.run_id,
        currency=acct.currency,
        starting_cash=acct.starting_cash,
        source_amount=acct.source_amount,
        source_currency=acct.source_currency,
        fx_rate=acct.fx_rate,
        fx_fee=acct.fx_fee,
    )


def ensure_sim_account(session: Session, run_id: int, settings: RuntimeSettings, now: datetime) -> SimAccountInfo:
    """Create the run's account and its deposit exactly once. A concurrent creator makes this wait for its
    commit and then do nothing, so the FX conversion and fee are applied once."""
    bal = starting_balance(settings)
    new_id = session.execute(
        pg_insert(m.SimAccount)
        .values(
            run_id=run_id,
            currency=settings.account_currency,
            starting_cash=bal.amount,
            source_amount=settings.starting_cash,
            source_currency=settings.starting_cash_currency,
            fx_rate=bal.fx_rate,
            fx_fee=bal.fx_fee,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[m.SimAccount.run_id])
        .returning(m.SimAccount.id)
    ).scalar_one_or_none()
    if new_id is not None:
        day = et_date(now)
        session.add(
            m.CashLedger(
                run_id=run_id,
                ts=now,
                trade_date=day,
                settle_date=day,  # a deposit is settled cash at once
                currency=settings.account_currency,
                amount=bal.amount,
                kind="deposit",
                ref=f"sim_account:{new_id}",
            )
        )
        session.flush()
    acct = session.execute(select(m.SimAccount).where(m.SimAccount.run_id == run_id)).scalar_one()
    return _info(acct)


def get_live_run(factory: sessionmaker[Session], clock: Clock, settings: RuntimeSettings) -> RunInfo:
    """The one active live run, created with its account on first use (prod starts a fresh one, §15.1)."""
    now = clock.now()
    with session_scope(factory) as s:
        s.execute(
            pg_insert(m.Run)
            .values(
                mode="live",
                started_at=now,
                params={"settings": settings.model_dump(mode="json", by_alias=True)},
                status="active",
                label="live",
            )
            .on_conflict_do_nothing(index_elements=[m.Run.mode], index_where=LIVE_WHERE)
        )
        run = s.execute(select(m.Run).where(m.Run.mode == "live", m.Run.status == "active")).scalar_one()
        ensure_sim_account(s, run.id, settings, now)
        return RunInfo(run.id, run.mode, run.started_at, run.label)


def sim_account(factory: sessionmaker[Session], run_id: int) -> SimAccountInfo | None:
    with factory() as s:
        acct = s.execute(select(m.SimAccount).where(m.SimAccount.run_id == run_id)).scalar_one_or_none()
        return _info(acct) if acct is not None else None
```

- [x] **Step 8: Run the tests, the gate, commit and push**

Run: `uv --directory Trader/app run pytest tests/engine/test_runs.py tests/test_runtime_settings_phase2.py -q` → all pass.
Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/settings_store.py Trader/app/trader/engine/__init__.py Trader/app/trader/engine/runs.py Trader/app/tests/test_runtime_settings_phase2.py Trader/app/tests/engine/__init__.py Trader/app/tests/engine/test_runs.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T2: Phase 2 runtime settings, live run and sim account with one-time FX" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T3: Ledger with T+1 settlement

**Files:**
- Create: `Trader/app/trader/broker/__init__.py`, `Trader/app/trader/broker/ledger.py`, `Trader/app/tests/broker/__init__.py`, `Trader/app/tests/broker/test_ledger.py`

**Interfaces:**
- Consumes: `CashLedger` model and the `cash_ledger_append_only` trigger (P2-T1); `SessionCalendar.next_session` (P1-T4); `tests.factories.add_run` (P2-T1).
- Produces (`trader.broker.ledger`):
  - `LedgerKind = Literal["deposit", "buy", "sell", "fee"]`.
  - `CashBalances(total: Decimal, settled: Decimal)` with `buying_power(cash_account_mode: bool) -> Decimal` (settled when the mode is on, else total).
  - `Ledger(calendar: SessionCalendar)`: `settle_date(trade_date: date) -> date` (the next trading session: T+1 on the exchange calendar); `record(session, *, run_id: int, ts: datetime, trade_date: date, amount: Decimal, kind: LedgerKind, ref: str, currency: str = "USD") -> int` (flushes and returns the row id; a deposit settles on its trade date; deposits and sells must be positive, buys and fees negative, else `ValueError`); `balances(session, run_id: int, today: date) -> CashBalances`.
  - **Settled cash rule:** `settled = sum(credits with settle_date <= today) + sum(every debit)`. A purchase reduces settled cash immediately (you can only spend settled money once), while sale proceeds count only from their settle date. `total` is the sum of every row.
  - There is no update or delete method; the database refuses both.

- [x] **Step 1: Write the failing tests**

`Trader/app/tests/broker/__init__.py`: empty file.

`Trader/app/tests/broker/test_ledger.py`:
```python
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run
from trader.broker.ledger import CashBalances, Ledger
from trader.db import models as m
from trader.market.calendar import SessionCalendar

LEDGER = Ledger(SessionCalendar())
TS = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("trade", "settle"),
    [
        (date(2026, 10, 6), date(2026, 10, 7)),  # Tue -> Wed
        (date(2026, 10, 2), date(2026, 10, 5)),  # Fri -> Mon
        (date(2026, 11, 25), date(2026, 11, 27)),  # Wed before Thanksgiving -> Fri (an early-close session)
        (date(2026, 12, 24), date(2026, 12, 28)),  # Christmas Eve (early close) -> Mon, Christmas is Fri
        (date(2026, 12, 31), date(2027, 1, 4)),  # New Year's Eve Thu -> Mon (Jan 1 is a holiday)
    ],
)
def test_settle_date_skips_weekend_and_holidays(trade: date, settle: date) -> None:
    assert LEDGER.settle_date(trade) == settle


def test_buying_power_follows_cash_account_mode() -> None:
    bal = CashBalances(total=Decimal("1009.99"), settled=Decimal("499.99"))
    assert bal.buying_power(cash_account_mode=True) == Decimal("499.99")
    assert bal.buying_power(cash_account_mode=False) == Decimal("1009.99")


def _record(s: Session, run: int, day: date, amount: str, kind: str) -> int:
    return LEDGER.record(s, run_id=run, ts=TS, trade_date=day, amount=Decimal(amount), kind=kind, ref=f"test:{kind}")


@pytest.mark.db
def test_sale_proceeds_settle_next_session(db_factory: sessionmaker[Session]) -> None:
    thu, fri, sat, mon = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5)
    with db_factory() as s:
        run = add_run(s)
        _record(s, run, thu, "1000", "deposit")
        _record(s, run, fri, "-500", "buy")
        _record(s, run, fri, "510", "sell")
        _record(s, run, fri, "-0.01", "fee")
        s.commit()
        on_fri = LEDGER.balances(s, run, fri)
        on_sat = LEDGER.balances(s, run, sat)
        on_mon = LEDGER.balances(s, run, mon)
    assert on_fri == CashBalances(total=Decimal("1009.9900"), settled=Decimal("499.9900"))
    assert on_sat.settled == Decimal("499.9900")  # a weekend doesn't settle anything
    assert on_mon == CashBalances(total=Decimal("1009.9900"), settled=Decimal("1009.9900"))


@pytest.mark.db
def test_deposit_settles_on_its_trade_date(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        row_id = _record(s, run, date(2026, 10, 3), "720", "deposit")  # a Saturday
        s.commit()
        row = s.get(m.CashLedger, row_id)
        assert row is not None and row.settle_date == date(2026, 10, 3)
        assert LEDGER.balances(s, run, date(2026, 10, 3)).settled == Decimal("720.0000")


@pytest.mark.db
def test_balances_are_per_run(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        a, b = add_run(s), add_run(s, mode="replay")
        _record(s, a, date(2026, 10, 1), "100", "deposit")
        _record(s, b, date(2026, 10, 1), "999", "deposit")
        s.commit()
        assert LEDGER.balances(s, a, date(2026, 10, 1)).total == Decimal("100.0000")


@pytest.mark.parametrize(("amount", "kind"), [("-1", "deposit"), ("-1", "sell"), ("1", "buy"), ("1", "fee"), ("0", "buy")])
@pytest.mark.db
def test_record_rejects_the_wrong_sign(db_factory: sessionmaker[Session], amount: str, kind: str) -> None:
    with db_factory() as s:
        run = add_run(s)
        with pytest.raises(ValueError, match="sign"):
            _record(s, run, date(2026, 10, 1), amount, kind)


@pytest.mark.db
def test_ledger_rows_are_append_only(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        _record(s, run, date(2026, 10, 1), "100", "deposit")
        s.commit()
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(update(m.CashLedger).values(amount=Decimal("1000000")))
        s.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(delete(m.CashLedger))
        s.rollback()
        assert not hasattr(LEDGER, "update") and not hasattr(LEDGER, "delete")
```

- [x] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/broker/test_ledger.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.broker'`.

- [x] **Step 3: Implement `trader/broker/ledger.py`**

`Trader/app/trader/broker/__init__.py`: empty file.

`Trader/app/trader/broker/ledger.py`:
```python
"""Cash ledger with T+1 settlement (SPEC §7.3, BR-21).

Rows are append-only: the database refuses UPDATE and DELETE (trigger cash_ledger_append_only, migration
0002). Settled cash counts every debit at once but a credit only from its settle date, so money from a sale
can't be spent until it settles, and settled money can't be spent twice.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trader.db import models as m
from trader.market.calendar import SessionCalendar

LedgerKind = Literal["deposit", "buy", "sell", "fee"]
_POSITIVE: frozenset[str] = frozenset({"deposit", "sell"})


@dataclass(frozen=True, slots=True)
class CashBalances:
    total: Decimal
    settled: Decimal

    def buying_power(self, cash_account_mode: bool) -> Decimal:
        return self.settled if cash_account_mode else self.total


class Ledger:
    def __init__(self, calendar: SessionCalendar) -> None:
        self._cal = calendar

    def settle_date(self, trade_date: date) -> date:
        """T+1: the next trading session on the exchange calendar (skips weekends and holidays)."""
        return self._cal.next_session(trade_date)

    def record(
        self,
        session: Session,
        *,
        run_id: int,
        ts: datetime,
        trade_date: date,
        amount: Decimal,
        kind: LedgerKind,
        ref: str,
        currency: str = "USD",
    ) -> int:
        if (amount > 0) != (kind in _POSITIVE) or amount == 0:
            raise ValueError(f"wrong sign for a {kind} of {amount}: deposits and sells are > 0, buys and fees < 0")
        settle = trade_date if kind == "deposit" else self.settle_date(trade_date)
        row = m.CashLedger(
            run_id=run_id,
            ts=ts,
            trade_date=trade_date,
            settle_date=settle,
            currency=currency,
            amount=amount,
            kind=kind,
            ref=ref,
        )
        session.add(row)
        session.flush()
        return row.id

    def balances(self, session: Session, run_id: int, today: date) -> CashBalances:
        amount = m.CashLedger.amount
        total, settled = session.execute(
            select(
                func.coalesce(func.sum(amount), 0),
                func.coalesce(
                    func.sum(amount).filter(or_(m.CashLedger.settle_date <= today, amount < 0)),
                    0,
                ),
            ).where(m.CashLedger.run_id == run_id)
        ).one()
        return CashBalances(total=Decimal(total), settled=Decimal(settled))
```

- [x] **Step 4: Run the tests**

Run: `uv --directory Trader/app run pytest tests/broker/test_ledger.py -q`
Expected: all pass. (`Decimal("1009.9900") == Decimal("1009.99")` is true, so the equality checks don't depend on scale.)

- [x] **Step 5: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/broker/__init__.py Trader/app/trader/broker/ledger.py Trader/app/tests/broker/__init__.py Trader/app/tests/broker/test_ledger.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T3: cash ledger with T+1 settlement on the exchange calendar" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T4: Broker value types and the quote-based fill model

**Files:**
- Create: `Trader/app/trader/broker/__init__.py` (empty; P2-T3 may already have created the same empty file), `Trader/app/trader/broker/types.py`, `Trader/app/trader/broker/fill_model.py`, `Trader/app/tests/broker/__init__.py` (empty; same note), `Trader/app/tests/broker/test_fill_model.py`

**Interfaces:**
- Consumes: `QtQuote` (P1-T7: `symbol_id, symbol, bid, ask, last, last_regular, volume, last_trade_time, delay: int | None, is_halted, vwap`; `delay` is `None` when Questrade omits it); `Candle` (P1-T4); `RuntimeSettings` fill fields (P2-T2).
- Produces (`trader.broker.types`, all frozen dataclasses with `slots=True`, prices `Decimal`):
  - `Side = Literal["buy","sell"]`, `OrderType = Literal["market","limit","stop","stop_limit"]`, `Purpose = Literal["entry","stop","exit"]`, `TimeInForce = Literal["day","gtc"]`, `Q4 = Decimal("0.0001")`.
  - `OrderSpec(symbol_id: int, side: Side, order_type: OrderType, qty: int, stop: Decimal | None = None, limit: Decimal | None = None, tif: TimeInForce = "day", purpose: Purpose = "entry", position_id: int | None = None, proposal_id: int | None = None, strategy_config_id: int | None = None, stop_loss: Decimal | None = None, reason: str = "")`. Construction raises `ValueError` for qty ≤ 0, a stop/stop-limit without a positive `stop`, a limit/stop-limit without a positive `limit`, a buy that isn't an entry or an entry that isn't a buy (long only), or a sell without `position_id`. `to_json() -> dict[str, Any]` (Decimals as strings) and `OrderSpec.from_json(d) -> OrderSpec`.
  - `Fees(commission=0, ecn=0, sec=0)` with `total` property, `to_json()`, `Fees.from_json(d)`.
  - `FillDecision(price: Decimal, qty: int, slippage: Decimal, fees: Fees, quote_snapshot: dict[str, Any], trigger: str)` (`slippage` is per share); `NoFill(reason: str, detail: str = "")`; reasons: `halted`, `delayed_quote`, `stale_quote`, `no_ask`, `no_bid`, `not_triggered`, `above_limit`, `below_limit`.
  - `FillModel` protocol (master plan §7.1 contract, refined): `evaluate(order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | None` and `assess(order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | NoFill` (the same decision, with the reason when it doesn't fill). `SimBroker` (T5) depends on this protocol only, so P5-T2's `CandleFillModel` can implement it for replay.
  - `FillEvent(fill_id, order_id, run_id, symbol_id, side, purpose, qty, price, ts, position_id, strategy_config_id, stop_loss, proposal_id, trade_id=None, pnl=None)`; `Fill = FillEvent` (the name SPEC §5.1 uses in `on_fill`).
  - `PositionView(id, symbol_id, strategy_config_id, qty, avg_price, stop_loss, opened_at, session_date, stop_order_id, unprotected_since, unprotected_seconds)`; `OrderView(id, symbol_id, side, order_type, purpose, qty, stop, limit, status, position_id, strategy_config_id, proposal_id, submitted_at)`; `AccountState(total_cash, settled_cash, buying_power, positions_value, equity)`.
- Produces (`trader.broker.fill_model`):
  - `FillParams(slippage_min=0.01, slippage_bps=5, stale_quote_seconds=10.0, commission=0, ecn_per_share=0.0035, direct_route=False, sec_fee_rate=0.0000206)` and `FillParams.from_settings(s: RuntimeSettings) -> FillParams`.
  - `QuoteFillModel(params: FillParams)`: `slip(price) -> Decimal` (`max(slippage_min, slippage_bps/10000 × price)`, 4 dp half-up); `fees(side, qty, price) -> Fees` (commission per fill; ECN per share only when `direct_route`; SEC fee on sells only, `sec_fee_rate × value`); `assess(order, market, now) -> FillDecision | NoFill` and `evaluate(order, market, now) -> FillDecision | None` (both satisfy `FillModel`; both raise `TypeError` for a `Candle`, since candle fills are P5-T2's); `quote_snapshot(quote, now) -> dict[str, Any]` (module function).
  - **Assumption (staleness):** the quote's age is `now − QtQuote.last_trade_time`, because Questrade quotes carry no separate quote timestamp. A quiet stock whose last trade is older than `stale_quote_seconds` is therefore treated as stale even if its bid/ask is live, so its orders wait (the safe direction). No code change works around this; it is re-checked live in market hours in Phase 6 (the S2 recheck).
  - Rules (SPEC §7.2), after the quote is found usable (not halted, `delay == 0` (a `None` delay is unknown and counts as delayed), `last_trade_time` present and `now − last_trade_time ≤ stale_quote_seconds`; non-positive prices count as missing):

    | Order | Trigger | Price | Slippage/share |
    |---|---|---|---|
    | buy market | always (needs ask) | `ask + slip(ask)` | `slip(ask)` |
    | sell market | always (needs bid) | `bid − slip(bid)` | `slip(bid)` |
    | buy stop | `last ≥ stop` or `ask ≥ stop` | `max(stop, ask) + slip(max(stop, ask))` | same slip |
    | sell stop | `last ≤ stop` or `bid ≤ stop` | `min(stop, bid) − slip(min(stop, bid))` | same slip |
    | buy stop-limit | as buy stop, then only if `ask + slip(ask) ≤ limit` (else `above_limit`) | `ask + slip(ask)` | `slip(ask)` |
    | sell stop-limit | as sell stop, then only if `bid − slip(bid) ≥ limit` (else `below_limit`) | `bid − slip(bid)` | `slip(bid)` |
    | buy limit / sell limit | `ask ≤ limit` / `bid ≥ limit` | the limit | 0 |

- [x] **Step 1: Write the failing tests**

`Trader/app/tests/broker/test_fill_model.py`:
```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.types import FillDecision, FillModel, NoFill, OrderSpec
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
MODEL = QuoteFillModel(FillParams())


def q(
    bid: str | None = "9.99",
    ask: str | None = "10.00",
    last: str | None = "10.00",
    age: float | None = 1.0,
    *,
    symbol_id: int = 1,
    halted: bool = False,
    delay: int | None = 0,
) -> QtQuote:
    return QtQuote(
        symbol_id=symbol_id,
        symbol="AAA",
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        last=Decimal(last) if last is not None else None,
        last_regular=None,
        volume=100_000,
        last_trade_time=NOW - timedelta(seconds=age) if age is not None else None,
        delay=delay,
        is_halted=halted,
        vwap=None,
    )


def buy(order_type: str = "market", stop: str | None = None, limit: str | None = None, qty: int = 100) -> OrderSpec:
    return OrderSpec(
        1,
        "buy",
        order_type,  # type: ignore[arg-type]
        qty,
        stop=Decimal(stop) if stop else None,
        limit=Decimal(limit) if limit else None,
    )


def sell(order_type: str = "market", stop: str | None = None, limit: str | None = None, qty: int = 100) -> OrderSpec:
    return OrderSpec(
        1,
        "sell",
        order_type,  # type: ignore[arg-type]
        qty,
        stop=Decimal(stop) if stop else None,
        limit=Decimal(limit) if limit else None,
        purpose="stop" if order_type == "stop" else "exit",
        position_id=7,
    )


def filled(order: OrderSpec, quote: QtQuote) -> FillDecision:
    out = MODEL.evaluate(order, quote, NOW)
    assert isinstance(out, FillDecision), MODEL.assess(order, quote, NOW)
    return out


def test_buy_market_fills_at_ask_plus_min_slippage() -> None:
    d = filled(buy(), q(ask="10.00"))
    assert (d.price, d.slippage, d.trigger, d.qty) == (Decimal("10.0100"), Decimal("0.0100"), "market", 100)


def test_sell_market_fills_at_bid_minus_slippage() -> None:
    assert filled(sell(), q(bid="9.99")).price == Decimal("9.9800")


def test_bps_slippage_dominates_on_higher_prices() -> None:
    d = filled(buy(), q(ask="50.00", last="50.00"))
    assert d.slippage == Decimal("0.0250") and d.price == Decimal("50.0250")


def test_buy_stop_waits_for_the_trigger() -> None:
    out = MODEL.assess(buy("stop", stop="10.10"), q(ask="10.05", last="10.00"), NOW)
    assert out == NoFill("not_triggered")


def test_buy_stop_triggered_by_last() -> None:
    assert filled(buy("stop", stop="10.00"), q(ask="9.99", last="10.00")).price == Decimal("10.0100")


def test_buy_stop_gap_fills_at_the_ask() -> None:
    assert filled(buy("stop", stop="10.00"), q(ask="10.20", last="10.18")).price == Decimal("10.2100")


def test_sell_stop_triggered_by_bid_fills_below_it() -> None:
    assert filled(sell("stop", stop="10.00"), q(bid="9.80", ask="9.81", last="9.85")).price == Decimal("9.7900")


def test_sell_stop_triggered_by_last() -> None:
    assert filled(sell("stop", stop="10.00"), q(bid="10.01", ask="10.02", last="9.99")).price == Decimal("9.9900")


def test_buy_stop_limit_fills_inside_the_limit() -> None:
    d = filled(buy("stop_limit", stop="10.00", limit="10.05"), q(ask="10.03", last="10.02"))
    assert d.price == Decimal("10.0400") and d.trigger == "stop_limit"


def test_buy_stop_limit_refused_above_the_limit() -> None:
    out = MODEL.assess(buy("stop_limit", stop="10.00", limit="10.05"), q(ask="10.05", last="10.04"), NOW)
    assert isinstance(out, NoFill) and out.reason == "above_limit"


def test_buy_stop_limit_not_triggered() -> None:
    out = MODEL.assess(buy("stop_limit", stop="10.00", limit="10.05"), q(ask="9.95", last="9.94"), NOW)
    assert out == NoFill("not_triggered")


def test_sell_stop_limit_refused_below_the_limit() -> None:
    out = MODEL.assess(sell("stop_limit", stop="10.00", limit="9.95"), q(bid="9.94", last="9.94"), NOW)
    assert isinstance(out, NoFill) and out.reason == "below_limit"


def test_limit_orders_fill_at_the_limit_without_slippage() -> None:
    d = filled(buy("limit", limit="10.00"), q(ask="9.98"))
    assert (d.price, d.slippage) == (Decimal("10.00"), Decimal("0"))
    assert MODEL.evaluate(buy("limit", limit="10.00"), q(ask="10.01"), NOW) is None
    assert filled(sell("limit", limit="10.00"), q(bid="10.02")).price == Decimal("10.00")
    assert MODEL.evaluate(sell("limit", limit="10.00"), q(bid="9.99"), NOW) is None


def test_stale_quote_never_fills() -> None:
    """Review Focus 1: a quote older than stale_quote_seconds never fills, whatever the order."""
    for order in (buy(), sell(), buy("stop", stop="9.00"), sell("stop", stop="11.00")):
        out = MODEL.assess(order, q(age=10.5), NOW)
        assert isinstance(out, NoFill) and out.reason == "stale_quote"
        assert MODEL.evaluate(order, q(age=10.5), NOW) is None
    assert MODEL.evaluate(buy(), q(age=10.0), NOW) is not None  # exactly at the limit is still fresh
    assert MODEL.assess(buy(), q(age=None), NOW) == NoFill("stale_quote", "the quote has no time")


@pytest.mark.parametrize(
    ("order", "quote", "reason"),
    [
        (buy(), q(halted=True), "halted"),
        (buy(), q(delay=15), "delayed_quote"),
        (buy(), q(delay=None), "delayed_quote"),
        (buy(), q(ask=None), "no_ask"),
        (buy(), q(ask="0"), "no_ask"),
        (sell(), q(bid=None), "no_bid"),
    ],
)
def test_unusable_quotes_never_fill(order: OrderSpec, quote: QtQuote, reason: str) -> None:
    """Review Focus 1: halted, delayed or one-sided quotes never fill."""
    out = MODEL.assess(order, quote, NOW)
    assert isinstance(out, NoFill) and out.reason == reason


def test_sec_fee_on_sells_only() -> None:
    d = filled(sell(), q(bid="10.00"))  # 100 x 9.99 = 999.00 x 0.0000206 = 0.0205794
    assert d.fees.sec == Decimal("0.0206") and d.fees.total == Decimal("0.0206")
    assert filled(buy(), q()).fees.total == 0


def test_ecn_only_with_direct_route_and_commission_per_fill() -> None:
    m = QuoteFillModel(FillParams(direct_route=True, commission=Decimal("1")))
    d = m.evaluate(buy(), q(), NOW)
    assert d is not None
    assert (d.fees.commission, d.fees.ecn, d.fees.total) == (Decimal("1"), Decimal("0.3500"), Decimal("1.3500"))


def test_quote_snapshot_is_returned() -> None:
    d = filled(buy(), q(bid="9.99", ask="10.00", last="10.00"))
    assert d.quote_snapshot["bid"] == "9.99" and d.quote_snapshot["ask"] == "10.00"
    assert d.quote_snapshot["time"] == (NOW - timedelta(seconds=1)).isoformat()
    assert d.quote_snapshot["evaluated_at"] == NOW.isoformat()


def test_wrong_symbol_raises() -> None:
    with pytest.raises(ValueError, match="symbol"):
        MODEL.assess(buy(), q(symbol_id=2), NOW)


def test_candles_are_not_this_models_job() -> None:
    c = Candle(NOW, NOW + timedelta(minutes=1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), 1, None)
    with pytest.raises(TypeError):
        MODEL.evaluate(buy(), c, NOW)
    with pytest.raises(TypeError):
        MODEL.assess(buy(), c, NOW)


def test_quote_model_satisfies_the_fill_model_protocol() -> None:
    model: FillModel = MODEL  # mypy checks the protocol; the broker is typed to FillModel, not QuoteFillModel
    assert model.evaluate(buy(), q(), NOW) is not None


def test_params_from_settings() -> None:
    s = RuntimeSettings(slippage_min=Decimal("0.02"), stale_quote_seconds=5.0, fees_direct_route=True)
    p = FillParams.from_settings(s)
    assert (p.slippage_min, p.stale_quote_seconds, p.direct_route) == (Decimal("0.02"), 5.0, True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": "buy", "order_type": "market", "qty": 0},
        {"side": "buy", "order_type": "stop", "qty": 1},
        {"side": "buy", "order_type": "limit", "qty": 1},
        {"side": "buy", "order_type": "market", "qty": 1, "purpose": "exit"},
        {"side": "sell", "order_type": "market", "qty": 1, "purpose": "exit"},
        {"side": "sell", "order_type": "market", "qty": 1, "purpose": "entry", "position_id": 1},
    ],
)
def test_order_spec_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        OrderSpec(symbol_id=1, **kwargs)  # type: ignore[arg-type]


def test_order_spec_json_round_trip() -> None:
    spec = OrderSpec(1, "buy", "stop", 10, stop=Decimal("10.01"), stop_loss=Decimal("9.91"), strategy_config_id=3)
    assert OrderSpec.from_json(spec.to_json()) == spec
    assert spec.to_json()["stop"] == "10.01"
```

- [x] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/broker/test_fill_model.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.broker.fill_model'` (or `trader.broker` if P2-T3 hasn't landed yet).

- [x] **Step 3: Implement `trader/broker/types.py`**

Create `Trader/app/trader/broker/__init__.py` (empty) if it doesn't exist.

`Trader/app/trader/broker/types.py`:
```python
"""Broker value types shared by the fill model, the simulated broker, strategies and the engine."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, Self

from trader.adapters.questrade.models import QtQuote
from trader.market.types import Candle

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit"]
Purpose = Literal["entry", "stop", "exit"]
TimeInForce = Literal["day", "gtc"]
Q4 = Decimal("0.0001")
ZERO = Decimal("0")


def dec_str(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def str_dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


@dataclass(frozen=True, slots=True)
class OrderSpec:
    symbol_id: int
    side: Side
    order_type: OrderType
    qty: int
    stop: Decimal | None = None
    limit: Decimal | None = None
    tif: TimeInForce = "day"
    purpose: Purpose = "entry"
    position_id: int | None = None
    proposal_id: int | None = None
    strategy_config_id: int | None = None
    stop_loss: Decimal | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.order_type in ("stop", "stop_limit") and (self.stop is None or self.stop <= 0):
            raise ValueError(f"a {self.order_type} order needs a positive stop price")
        if self.order_type in ("limit", "stop_limit") and (self.limit is None or self.limit <= 0):
            raise ValueError(f"a {self.order_type} order needs a positive limit price")
        if (self.side == "buy") != (self.purpose == "entry"):
            raise ValueError("long only: entries are buys; stops and exits are sells")
        if self.side == "sell" and self.position_id is None:
            raise ValueError("a sell must name the position it closes")

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "side": self.side,
            "order_type": self.order_type,
            "qty": self.qty,
            "stop": dec_str(self.stop),
            "limit": dec_str(self.limit),
            "tif": self.tif,
            "purpose": self.purpose,
            "position_id": self.position_id,
            "proposal_id": self.proposal_id,
            "strategy_config_id": self.strategy_config_id,
            "stop_loss": dec_str(self.stop_loss),
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Self:
        return cls(
            symbol_id=int(d["symbol_id"]),
            side=d["side"],
            order_type=d["order_type"],
            qty=int(d["qty"]),
            stop=str_dec(d.get("stop")),
            limit=str_dec(d.get("limit")),
            tif=d.get("tif", "day"),
            purpose=d.get("purpose", "entry"),
            position_id=d.get("position_id"),
            proposal_id=d.get("proposal_id"),
            strategy_config_id=d.get("strategy_config_id"),
            stop_loss=str_dec(d.get("stop_loss")),
            reason=d.get("reason", ""),
        )


@dataclass(frozen=True, slots=True)
class Fees:
    commission: Decimal = ZERO
    ecn: Decimal = ZERO
    sec: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.commission + self.ecn + self.sec

    def to_json(self) -> dict[str, str]:
        return {
            "commission": str(self.commission),
            "ecn": str(self.ecn),
            "sec": str(self.sec),
            "total": str(self.total),
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Self:
        return cls(Decimal(str(d["commission"])), Decimal(str(d["ecn"])), Decimal(str(d["sec"])))


@dataclass(frozen=True, slots=True)
class FillDecision:
    price: Decimal
    qty: int
    slippage: Decimal  # per share, always >= 0 (a cost)
    fees: Fees
    quote_snapshot: dict[str, Any]
    trigger: str


@dataclass(frozen=True, slots=True)
class NoFill:
    reason: str
    detail: str = ""


class FillModel(Protocol):
    """Decides whether a working order fills against one piece of market data (master plan §7.1).

    QuoteFillModel (P2-T4) fills from quotes; CandleFillModel (P5-T2) will fill from candles for replay.
    An implementation raises TypeError for a market type it doesn't handle.
    """

    def evaluate(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | None: ...

    def assess(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | NoFill: ...


@dataclass(frozen=True, slots=True)
class FillEvent:
    fill_id: int
    order_id: int
    run_id: int
    symbol_id: int
    side: Side
    purpose: Purpose
    qty: int
    price: Decimal
    ts: datetime
    position_id: int
    strategy_config_id: int | None
    stop_loss: Decimal | None
    proposal_id: int | None
    trade_id: int | None = None
    pnl: Decimal | None = None


Fill = FillEvent  # the name SPEC §5.1 uses in Strategy.on_fill


@dataclass(frozen=True, slots=True)
class PositionView:
    id: int
    symbol_id: int
    strategy_config_id: int | None
    qty: int
    avg_price: Decimal
    stop_loss: Decimal | None
    opened_at: datetime
    session_date: date
    stop_order_id: int | None
    unprotected_since: datetime | None
    unprotected_seconds: int


@dataclass(frozen=True, slots=True)
class OrderView:
    id: int
    symbol_id: int
    side: Side
    order_type: OrderType
    purpose: Purpose
    qty: int
    stop: Decimal | None
    limit: Decimal | None
    status: str
    position_id: int | None
    strategy_config_id: int | None
    proposal_id: int | None
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class AccountState:
    total_cash: Decimal
    settled_cash: Decimal
    buying_power: Decimal
    positions_value: Decimal
    equity: Decimal
```

- [x] **Step 4: Implement `trader/broker/fill_model.py`**

```python
"""Quote-based fill model for live simulation (SPEC §7.2, BR-20).

Assumption: staleness uses QtQuote.last_trade_time, because a Questrade quote carries no separate quote
timestamp (P1-T7). This may over-flag quiet stocks as stale; it is re-checked live in Phase 6 (S2 recheck).
QuoteFillModel implements the FillModel protocol; candle fills for replay are a separate model (P5-T2).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import Q4, ZERO, Fees, FillDecision, NoFill, OrderSpec, Side, dec_str
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

Priced = tuple[Decimal, Decimal, str]  # price, slippage per share, trigger


@dataclass(frozen=True, slots=True)
class FillParams:
    slippage_min: Decimal = Decimal("0.01")
    slippage_bps: Decimal = Decimal("5")
    stale_quote_seconds: float = 10.0
    commission: Decimal = ZERO
    ecn_per_share: Decimal = Decimal("0.0035")
    direct_route: bool = False
    sec_fee_rate: Decimal = Decimal("0.0000206")

    @classmethod
    def from_settings(cls, s: RuntimeSettings) -> "FillParams":
        return cls(
            slippage_min=s.slippage_min,
            slippage_bps=s.slippage_bps,
            stale_quote_seconds=s.stale_quote_seconds,
            commission=s.fees_commission,
            ecn_per_share=s.fees_ecn_per_share,
            direct_route=s.fees_direct_route,
            sec_fee_rate=s.fees_sec_rate,
        )


def _positive(v: Decimal | None) -> Decimal | None:
    return v if v is not None and v > 0 else None


def quote_snapshot(q: QtQuote, now: datetime) -> dict[str, Any]:
    return {
        "symbol_id": q.symbol_id,
        "symbol": q.symbol,
        "bid": dec_str(q.bid),
        "ask": dec_str(q.ask),
        "last": dec_str(q.last),
        "time": q.last_trade_time.isoformat() if q.last_trade_time else None,
        "delay": q.delay,
        "halted": q.is_halted,
        "evaluated_at": now.isoformat(),
    }


class QuoteFillModel:
    def __init__(self, params: FillParams) -> None:
        self._p = params

    def slip(self, price: Decimal) -> Decimal:
        return max(self._p.slippage_min, self._p.slippage_bps / Decimal(10000) * price).quantize(Q4, ROUND_HALF_UP)

    def fees(self, side: Side, qty: int, price: Decimal) -> Fees:
        ecn = (self._p.ecn_per_share * qty).quantize(Q4, ROUND_HALF_UP) if self._p.direct_route else ZERO
        sec = (self._p.sec_fee_rate * price * qty).quantize(Q4, ROUND_HALF_UP) if side == "sell" else ZERO
        return Fees(commission=self._p.commission, ecn=ecn, sec=sec)

    def evaluate(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | None:
        out = self.assess(order, market, now)
        return out if isinstance(out, FillDecision) else None

    def assess(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | NoFill:
        if not isinstance(market, QtQuote):
            raise TypeError("QuoteFillModel fills from quotes; candle fills belong to the replay model (P5-T2)")
        quote = market
        if quote.symbol_id != order.symbol_id:
            raise ValueError(f"a quote for symbol {quote.symbol_id} can't fill an order for symbol {order.symbol_id}")
        if quote.is_halted:
            return NoFill("halted")
        if quote.delay is None or quote.delay > 0:  # None: Questrade omitted it, so never assume real-time
            return NoFill("delayed_quote", f"delay={quote.delay}")
        if quote.last_trade_time is None:
            return NoFill("stale_quote", "the quote has no time")
        age = (now - quote.last_trade_time).total_seconds()
        if age > self._p.stale_quote_seconds:
            return NoFill("stale_quote", f"the quote is {age:.1f}s old")
        bid, ask, last = _positive(quote.bid), _positive(quote.ask), _positive(quote.last)
        priced: Priced | NoFill
        if order.side == "buy":
            if ask is None:
                return NoFill("no_ask")
            priced = self._buy(order, ask, last)
        else:
            if bid is None:
                return NoFill("no_bid")
            priced = self._sell(order, bid, last)
        if isinstance(priced, NoFill):
            return priced
        price, slippage, trigger = priced
        price = price.quantize(Q4, ROUND_HALF_UP)
        return FillDecision(
            price=price,
            qty=order.qty,
            slippage=slippage,
            fees=self.fees(order.side, order.qty, price),
            quote_snapshot=quote_snapshot(quote, now),
            trigger=trigger,
        )

    def _buy(self, o: OrderSpec, ask: Decimal, last: Decimal | None) -> Priced | NoFill:
        if o.order_type == "market":
            s = self.slip(ask)
            return ask + s, s, "market"
        if o.order_type == "limit":
            assert o.limit is not None
            return (o.limit, ZERO, "limit") if ask <= o.limit else NoFill("not_triggered")
        assert o.stop is not None
        if not ((last is not None and last >= o.stop) or ask >= o.stop):
            return NoFill("not_triggered")
        if o.order_type == "stop":
            ref = max(o.stop, ask)
            s = self.slip(ref)
            return ref + s, s, "stop"
        assert o.limit is not None
        s = self.slip(ask)
        if ask + s > o.limit:
            return NoFill("above_limit", f"ask {ask} + slippage {s} > limit {o.limit}")
        return ask + s, s, "stop_limit"

    def _sell(self, o: OrderSpec, bid: Decimal, last: Decimal | None) -> Priced | NoFill:
        if o.order_type == "market":
            s = self.slip(bid)
            return bid - s, s, "market"
        if o.order_type == "limit":
            assert o.limit is not None
            return (o.limit, ZERO, "limit") if bid >= o.limit else NoFill("not_triggered")
        assert o.stop is not None
        if not ((last is not None and last <= o.stop) or bid <= o.stop):
            return NoFill("not_triggered")
        if o.order_type == "stop":
            ref = min(o.stop, bid)
            s = self.slip(ref)
            return ref - s, s, "stop"
        assert o.limit is not None
        s = self.slip(bid)
        if bid - s < o.limit:
            return NoFill("below_limit", f"bid {bid} - slippage {s} < limit {o.limit}")
        return bid - s, s, "stop_limit"
```

- [x] **Step 5: Run the tests**

Run: `uv --directory Trader/app run pytest tests/broker/test_fill_model.py -q`
Expected: all pass. If `test_sell_stop_limit_refused_below_the_limit` reports `not_triggered`, check the fixture: bid 9.94 ≤ stop 10.00 triggers, and 9.94 − 0.01 = 9.93 < 9.95.

- [x] **Step 6: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/broker/__init__.py Trader/app/trader/broker/types.py Trader/app/trader/broker/fill_model.py Trader/app/tests/broker/__init__.py Trader/app/tests/broker/test_fill_model.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T4: broker value types and the SPEC 7.2 quote fill model with stale-quote guard" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T5: Simulated broker: orders, positions, trades, equity

**Files:**
- Create: `Trader/app/trader/broker/base.py`, `Trader/app/trader/broker/sim_broker.py`, `Trader/app/tests/broker/test_sim_broker.py`

**Interfaces:**
- Consumes: `Ledger`, `CashBalances` (P2-T3); `OrderSpec`, `FillDecision`, `NoFill`, `Fees`, `FillEvent`, `FillModel`, `PositionView`, `OrderView`, `AccountState`, `Q4` (P2-T4); `QuoteFillModel` (P2-T4, tests only); `SessionCalendar.is_session/session_close` (P1-T4); `RuntimeSettings.no_entry_before_close_minutes` (P2-T2); models `Order`, `Fill`, `Position`, `Trade`, `EquitySnapshot`, `EventLog` (P2-T1, P1-T2); `log_event` (P1-T9); `session_scope` (P1-T2); `et_date` (P1-T4); `get_live_run` (P2-T2, tests); `tests.factories` (P2-T1).
- Produces:
  - `trader.broker.base`: `BrokerRejected(ValueError)`; `Broker` protocol: `submit(spec: OrderSpec, session: Session | None = None) -> int`, `cancel(order_id: int, reason: str, session: Session | None = None) -> bool`, `on_quotes(quotes: Sequence[QtQuote], now: datetime) -> list[FillEvent]`, `end_of_session(session_date: date) -> list[int]`.
  - `trader.broker.sim_broker.SimBroker(factory, clock, ledger: Ledger, fill_model: FillModel, run_id: int, currency: str = "USD", *, calendar: SessionCalendar | None = None, settings: Callable[[], RuntimeSettings] | None = None)` implementing `Broker`. `fill_model` is typed to the `FillModel` protocol (P2-T4), not to `QuoteFillModel`. `calendar` defaults to `SessionCalendar()`; `settings` is read when an entry's cutoff is checked (the engine passes `SettingsStore.load`, so a change applies at once) and defaults to `RuntimeSettings()` defaults. Plus: `entry_cutoff(day: date) -> datetime | None` (`session_close(day) − no_entry_before_close_minutes`; `None` when `day` isn't a session), `open_positions() -> list[PositionView]`, `working_orders() -> list[OrderView]`, `working_symbol_ids() -> list[int]`, `account_state(today: date, marks: Mapping[int, Decimal], cash_account_mode: bool) -> AccountState` (a position without a mark is valued at its average price), `snapshot_equity(now: datetime, account: AccountState) -> None` (one `equity_snapshots` row per `(run_id, ts)`, peak carried forward, drawdown 4 dp).
  - Behaviour: `submit` creates a `working` order and returns its id. Sells must name an open position of this run with exactly its quantity: larger is refused (long only), smaller is refused (no partial exits; partial fills are out of scope, SPEC §7.2). A `stop` order becomes the position's `stop_order_id` (a previous working stop is cancelled as "replaced") and ends its unprotected interval. `cancel` returns `False` unless the order is working; cancelling a position's stop restarts its unprotected interval. `on_quotes` locks this run's working orders for the quoted symbols (`FOR UPDATE SKIP LOCKED`, oldest first), so a concurrent caller can't fill one order twice. A fill writes, in one transaction: the `fills` row (with the quote snapshot), the order status, ledger rows (principal, then fees if any), and either a new position (entry) or the closed position, its `trades` row (pnl net of entry and exit fees, `pnl_r = pnl / planned_risk`, `planned_risk = (entry fill − stop_loss) × qty`, `slippage_total = (entry + exit slippage) × qty`) and the cancellation of every other working order on that position ("position closed"). An unusable quote (`stale_quote`, `halted`, `delayed_quote`, `no_ask`, `no_bid`) sets `orders.stale_since` and logs one warning; if it persists 60 s, one error event is logged and `stale_alerted` is set; a later usable quote clears `stale_since`. **Entry cutoff (BR-42, overnight hold):** before the fill model is asked, a working entry (buy-to-open) order met at or after `entry_cutoff(et_date(now))`, on a non-session day, or on a later session than its own is never filled: it is cancelled with reason `"entry cutoff"` and one `warning` event is logged (it could never fill legally, so it isn't left working). Stops and exits are never refused. `end_of_session` cancels every working order of the run with `session_date <= session_date`.
  - **P5 path (documented, not built in Phase 2):** P5-T2 adds `on_candles(candles: Mapping[int, Candle], now: datetime) -> list[FillEvent]`, which locks the same working orders and runs the same loop (entry cutoff, then `self._fill_model.assess(spec, candle, now)`, then `_no_fill` / `_fill`) with a `CandleFillModel`. Because `SimBroker` depends only on the `FillModel` protocol, nothing else in the broker changes.

- [x] **Step 1: Write the failing tests**

`Trader/app/tests/broker/test_sim_broker.py`:
```python
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.adapters.questrade.models import QtQuote
from trader.broker.base import BrokerRejected
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # Tue 09:40 ET
DAY = date(2026, 10, 6)


@dataclass
class Env:
    broker: SimBroker
    clock: FixedClock
    run_id: int
    sym: int
    cfg: int
    factory: sessionmaker[Session]


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T)
    run = get_live_run(db_factory, clock, RuntimeSettings())  # deposit 720 USD
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        s.commit()
    broker = SimBroker(db_factory, clock, Ledger(CAL), QuoteFillModel(FillParams()), run.id)
    return Env(broker, clock, run.id, sym, cfg, db_factory)


def quote(env: Env, bid: str, ask: str, last: str, age: float = 1.0) -> QtQuote:
    return QtQuote(
        symbol_id=env.sym,
        symbol="AAA",
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(last),
        last_regular=None,
        volume=1000,
        last_trade_time=env.clock.now() - timedelta(seconds=age),
        delay=0,
        is_halted=False,
        vwap=None,
    )


def entry(env: Env, qty: int = 50) -> int:
    spec = OrderSpec(
        env.sym,
        "buy",
        "stop",
        qty,
        stop=Decimal("10.00"),
        stop_loss=Decimal("9.90"),
        strategy_config_id=env.cfg,
        reason="orb_breakout",
    )
    return env.broker.submit(spec)


def fill_entry(env: Env) -> int:
    entry(env)
    (ev,) = env.broker.on_quotes([quote(env, "10.01", "10.02", "10.02")], env.clock.now())
    return ev.position_id


def stop_for(env: Env, position_id: int, qty: int = 50) -> int:
    return env.broker.submit(
        OrderSpec(
            env.sym,
            "sell",
            "stop",
            qty,
            stop=Decimal("9.90"),
            purpose="stop",
            position_id=position_id,
            reason="protective_stop",
        )
    )


def market_exit(env: Env, position_id: int, qty: int = 50) -> int:
    return env.broker.submit(
        OrderSpec(env.sym, "sell", "market", qty, purpose="exit", position_id=position_id, reason="flatten_close")
    )


def test_submit_works_then_fills_and_opens_a_position(env: Env) -> None:
    oid = entry(env)
    assert [o.id for o in env.broker.working_orders()] == [oid]
    assert env.broker.working_symbol_ids() == [env.sym]
    assert env.broker.on_quotes([quote(env, "9.93", "9.95", "9.94")], T) == []
    (ev,) = env.broker.on_quotes([quote(env, "10.01", "10.02", "10.02")], T)
    assert (ev.order_id, ev.purpose, ev.qty, ev.price) == (oid, "entry", 50, Decimal("10.0300"))
    assert ev.stop_loss == Decimal("9.90") and ev.strategy_config_id == env.cfg and ev.trade_id is None
    (pos,) = env.broker.open_positions()
    assert (pos.id, pos.qty, pos.avg_price, pos.stop_loss) == (ev.position_id, 50, Decimal("10.0300"), Decimal("9.90"))
    assert pos.unprotected_since == T and pos.stop_order_id is None
    with env.factory() as s:
        fill = s.execute(select(m.Fill)).scalar_one()
        planned = s.get(m.Position, ev.position_id)
        ledger = s.execute(select(m.CashLedger.kind, m.CashLedger.amount).order_by(m.CashLedger.id)).all()
    assert fill.quote_snapshot["ask"] == "10.02" and fill.slippage == Decimal("0.0100")
    assert planned is not None and planned.planned_risk == Decimal("6.5000")  # (10.03 - 9.90) x 50
    assert ledger == [("deposit", Decimal("720.0000")), ("buy", Decimal("-501.5000"))]
    assert env.broker.working_orders() == []


def test_round_trip_writes_a_trade_with_pnl_and_r(env: Env) -> None:
    pid = fill_entry(env)
    stop_for(env, pid)
    (ev,) = env.broker.on_quotes([quote(env, "9.85", "9.86", "9.86")], T)
    # sell stop: min(9.90, 9.85) - 0.01 = 9.84; SEC fee 50 x 9.84 = 492 x 0.0000206 = 0.0101
    assert ev.price == Decimal("9.8400") and ev.purpose == "stop" and ev.trade_id is not None
    with env.factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
        pos = s.get(m.Position, pid)
    assert (trade.entry_price, trade.exit_price, trade.qty) == (Decimal("10.0300"), Decimal("9.8400"), 50)
    assert trade.pnl == Decimal("-9.5101")  # (9.84 - 10.03) x 50 - 0.0101
    assert trade.pnl_r == Decimal("-1.4631")  # -9.5101 / 6.5
    assert trade.fees_total == Decimal("0.0101")
    assert trade.slippage_total == Decimal("1.0000")  # (0.01 + 0.01) x 50
    assert trade.exit_reason == "protective_stop" and trade.session_date == DAY
    assert pos is not None and pos.closed_at == T
    assert ev.pnl == trade.pnl
    assert env.broker.open_positions() == []


def test_cancel_only_affects_working_orders(env: Env) -> None:
    oid = entry(env)
    assert env.broker.cancel(oid, "entry_cancel_at") is True
    assert env.broker.cancel(oid, "again") is False
    assert env.broker.cancel(999_999, "unknown") is False
    with env.factory() as s:
        order = s.get(m.Order, oid)
    assert order is not None and order.status == "cancelled" and order.cancel_reason == "entry_cancel_at"
    pid = fill_entry(env)
    with env.factory() as s:
        filled = s.execute(select(m.Order.id).where(m.Order.position_id == pid)).scalar_one()
    assert env.broker.cancel(filled, "too late") is False


def test_sells_must_match_the_position_long_only(env: Env) -> None:
    pid = fill_entry(env)
    with pytest.raises(BrokerRejected, match="long only"):
        market_exit(env, pid, qty=51)
    with pytest.raises(BrokerRejected, match="partial"):
        market_exit(env, pid, qty=49)
    with pytest.raises(BrokerRejected, match="not open"):
        market_exit(env, 999_999)
    market_exit(env, pid)
    env.broker.on_quotes([quote(env, "10.10", "10.11", "10.10")], T)
    with pytest.raises(BrokerRejected, match="not open"):
        market_exit(env, pid)


def test_end_of_session_cancels_every_working_order(env: Env) -> None:
    pid = fill_entry(env)
    stop_id = stop_for(env, pid)
    other = entry(env, qty=5)
    assert sorted(env.broker.end_of_session(DAY)) == sorted([stop_id, other])
    assert env.broker.working_orders() == []
    with env.factory() as s:
        reasons = set(s.execute(select(m.Order.cancel_reason).where(m.Order.status == "cancelled")).scalars())
    assert reasons == {"end_of_session"}


def test_stale_quote_keeps_order_working_and_logs_once(env: Env) -> None:
    """Review Focus 1: a stale quote never fills; one warning, then one error if it persists 60 s."""
    oid = entry(env)
    stale = quote(env, "10.50", "10.51", "10.50", age=30)
    assert env.broker.on_quotes([stale], T) == []
    assert env.broker.on_quotes([stale], T + timedelta(seconds=2)) == []
    with env.factory() as s:
        order = s.get(m.Order, oid)
        levels = s.execute(select(m.EventLog.level).where(m.EventLog.message.contains("quote"))).scalars().all()
    assert order is not None and order.status == "working" and order.stale_since == T
    assert levels == ["warning"]
    env.broker.on_quotes([stale], T + timedelta(seconds=61))
    env.broker.on_quotes([stale], T + timedelta(seconds=63))
    with env.factory() as s:
        levels = s.execute(select(m.EventLog.level).where(m.EventLog.message.contains("quote"))).scalars().all()
        order = s.get(m.Order, oid)
    assert levels == ["warning", "error"] and order is not None and order.stale_alerted is True
    env.clock.set(T + timedelta(seconds=70))
    assert env.broker.on_quotes([quote(env, "9.90", "9.91", "9.90")], env.clock.now()) == []
    with env.factory() as s:
        order = s.get(m.Order, oid)
    assert order is not None and order.stale_since is None and order.status == "working"


def test_an_entry_that_would_fill_late_is_cancelled_instead(env: Env) -> None:
    """Review Focus 4 (BR-42): from the no-entry cutoff on, an entry is cancelled instead of filled."""
    cutoff = CAL.session_close(DAY) - timedelta(minutes=30)  # 15:30 ET with the default 30 minutes
    assert env.broker.entry_cutoff(DAY) == cutoff and env.broker.entry_cutoff(date(2026, 10, 4)) is None  # Sunday
    env.clock.set(cutoff - timedelta(seconds=1))
    pid = fill_entry(env)  # one second before the cutoff an entry still fills
    late = entry(env, qty=5)
    env.clock.set(cutoff)
    market_exit(env, pid)  # exits are never refused
    fills = env.broker.on_quotes([quote(env, "10.01", "10.02", "10.02")], cutoff)  # would trigger the buy stop
    assert [f.purpose for f in fills] == ["exit"]
    with env.factory() as s:
        order = s.get(m.Order, late)
        levels = s.execute(select(m.EventLog.level).where(m.EventLog.message.contains("entry cutoff"))).scalars().all()
    assert order is not None and order.status == "cancelled" and order.cancel_reason == "entry cutoff"
    assert levels == ["warning"] and env.broker.open_positions() == [] and env.broker.working_orders() == []


def test_the_entry_cutoff_follows_the_settings(env: Env) -> None:
    settings = RuntimeSettings(no_entry_before_close_minutes=60)
    broker = SimBroker(env.factory, env.clock, Ledger(CAL), QuoteFillModel(FillParams()), env.run_id,
                       calendar=CAL, settings=lambda: settings)
    assert broker.entry_cutoff(DAY) == CAL.session_close(DAY) - timedelta(minutes=60)


def test_order_fills_only_once(env: Env) -> None:
    """Review Focus 2: repeated or concurrent quote batches fill an order once."""
    entry(env)
    q = quote(env, "10.01", "10.02", "10.02")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: env.broker.on_quotes([q], T), range(4)))
    assert sum(len(r) for r in results) == 1
    assert env.broker.on_quotes([q], T) == []
    with env.factory() as s:
        assert s.execute(select(func.count()).select_from(m.Fill)).scalar_one() == 1


def test_market_exit_fill_cancels_the_protective_stop(env: Env) -> None:
    pid = fill_entry(env)
    stop_id = stop_for(env, pid)
    market_exit(env, pid)
    (ev,) = env.broker.on_quotes([quote(env, "10.20", "10.21", "10.20")], T)
    assert ev.purpose == "exit" and ev.price == Decimal("10.1900")
    with env.factory() as s:
        stop = s.get(m.Order, stop_id)
    assert stop is not None and stop.status == "cancelled" and stop.cancel_reason == "position closed"


def test_unprotected_seconds_are_recorded(env: Env) -> None:
    pid = fill_entry(env)  # at T, no stop yet
    env.clock.set(T + timedelta(seconds=30))
    stop_id = stop_for(env, pid)
    (pos,) = env.broker.open_positions()
    assert pos.unprotected_seconds == 30 and pos.unprotected_since is None and pos.stop_order_id == stop_id
    env.clock.set(T + timedelta(seconds=40))
    env.broker.cancel(stop_id, "manual")
    (pos,) = env.broker.open_positions()
    assert pos.unprotected_since == T + timedelta(seconds=40) and pos.stop_order_id is None
    env.clock.set(T + timedelta(seconds=100))
    market_exit(env, pid)
    env.broker.on_quotes([quote(env, "10.20", "10.21", "10.20")], env.clock.now())
    with env.factory() as s:
        closed = s.get(m.Position, pid)
    assert closed is not None and closed.unprotected_seconds == 90  # 30 before the stop + 60 after it went


def test_a_new_stop_replaces_the_old_one(env: Env) -> None:
    pid = fill_entry(env)
    first = stop_for(env, pid)
    second = stop_for(env, pid)
    assert [o.id for o in env.broker.working_orders()] == [second]
    with env.factory() as s:
        old = s.get(m.Order, first)
    assert old is not None and old.cancel_reason == "replaced by a new stop"


def test_account_state_and_equity_snapshots(env: Env) -> None:
    fill_entry(env)  # 50 @ 10.03, cash 720 - 501.50
    acct = env.broker.account_state(DAY, {env.sym: Decimal("10.50")}, cash_account_mode=True)
    assert acct.total_cash == Decimal("218.5000") and acct.settled_cash == Decimal("218.5000")
    assert acct.positions_value == Decimal("525.0000") and acct.equity == Decimal("743.5000")
    assert acct.buying_power == Decimal("218.5000")
    no_mark = env.broker.account_state(DAY, {}, cash_account_mode=False)
    assert no_mark.positions_value == Decimal("501.5000")
    env.broker.snapshot_equity(T, acct)
    lower = env.broker.account_state(DAY, {env.sym: Decimal("9.00")}, cash_account_mode=True)
    env.broker.snapshot_equity(T + timedelta(minutes=1), lower)
    env.broker.snapshot_equity(T + timedelta(minutes=1), lower)  # same ts twice: one row
    with env.factory() as s:
        snaps = s.execute(select(m.EquitySnapshot).order_by(m.EquitySnapshot.ts)).scalars().all()
    assert [(x.equity, x.peak_equity, x.drawdown_pct) for x in snaps] == [
        (Decimal("743.5000"), Decimal("743.5000"), Decimal("0.0000")),
        (Decimal("668.5000"), Decimal("743.5000"), Decimal("0.1009")),  # 75 / 743.5
    ]
```

- [x] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/broker/test_sim_broker.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.broker.base'`.

- [x] **Step 3: Implement `trader/broker/base.py`**

```python
"""The broker seam (master plan §7.1): the simulated broker now, a live adapter one day (BRD O6)."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import FillEvent, OrderSpec


class BrokerRejected(ValueError):
    """The broker refused an order: long-only rule, unknown or closed position, wrong symbol."""


class Broker(Protocol):
    def submit(self, spec: OrderSpec, session: Session | None = None) -> int: ...
    def cancel(self, order_id: int, reason: str, session: Session | None = None) -> bool: ...
    def on_quotes(self, quotes: Sequence[QtQuote], now: datetime) -> list[FillEvent]: ...
    def end_of_session(self, session_date: date) -> list[int]: ...
```

- [x] **Step 4: Implement `trader/broker/sim_broker.py`**

```python
"""Simulated broker (SPEC §7, BR-20, BR-21, BR-23): orders, fills, positions, trades and cash.

Each fill is one transaction: fill row, order status, ledger rows, position and trade. Working orders are
locked FOR UPDATE SKIP LOCKED while quotes are applied, so two callers never fill one order twice.
An entry met at or after the no-entry cutoff (close - no_entry_before_close_minutes) is cancelled, never
filled (BR-42). The broker depends on the FillModel protocol only; P5-T2 adds on_candles() for replay,
running the same per-order loop with a CandleFillModel.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.models import QtQuote
from trader.broker.base import BrokerRejected
from trader.broker.ledger import Ledger
from trader.broker.types import (
    Q4,
    ZERO,
    AccountState,
    Fees,
    FillDecision,
    FillEvent,
    FillModel,
    NoFill,
    OrderSpec,
    OrderView,
    PositionView,
)
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.settings_store import RuntimeSettings

UNUSABLE_QUOTE = frozenset({"stale_quote", "halted", "delayed_quote", "no_ask", "no_bid"})
STALE_ALERT_AFTER = timedelta(seconds=60)
ENTRY_CUTOFF = "entry cutoff"
SOURCE = "broker"


def position_view(p: m.Position) -> PositionView:
    return PositionView(
        id=p.id,
        symbol_id=p.symbol_id,
        strategy_config_id=p.strategy_config_id,
        qty=p.qty,
        avg_price=p.avg_price,
        stop_loss=p.stop_loss,
        opened_at=p.opened_at,
        session_date=p.session_date,
        stop_order_id=p.stop_order_id,
        unprotected_since=p.unprotected_since,
        unprotected_seconds=p.unprotected_seconds,
    )


def order_view(o: m.Order) -> OrderView:
    return OrderView(
        id=o.id,
        symbol_id=o.symbol_id,
        side=o.side,  # type: ignore[arg-type]
        order_type=o.order_type,  # type: ignore[arg-type]
        purpose=o.purpose,  # type: ignore[arg-type]
        qty=o.qty,
        stop=o.stop_price,
        limit=o.limit_price,
        status=o.status,
        position_id=o.position_id,
        strategy_config_id=o.strategy_config_id,
        proposal_id=o.proposal_id,
        submitted_at=o.submitted_at,
    )


def _spec(o: m.Order) -> OrderSpec:
    return OrderSpec(
        symbol_id=o.symbol_id,
        side=o.side,  # type: ignore[arg-type]
        order_type=o.order_type,  # type: ignore[arg-type]
        qty=o.qty,
        stop=o.stop_price,
        limit=o.limit_price,
        tif=o.tif,  # type: ignore[arg-type]
        purpose=o.purpose,  # type: ignore[arg-type]
        position_id=o.position_id,
        proposal_id=o.proposal_id,
        strategy_config_id=o.strategy_config_id,
        stop_loss=o.stop_loss,
        reason=o.reason,
    )


def _end_unprotected(pos: m.Position, now: datetime) -> None:
    if pos.unprotected_since is not None:
        pos.unprotected_seconds += int((now - pos.unprotected_since).total_seconds())
        pos.unprotected_since = None


class SimBroker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        ledger: Ledger,
        fill_model: FillModel,
        run_id: int,
        currency: str = "USD",
        *,
        calendar: SessionCalendar | None = None,
        settings: Callable[[], RuntimeSettings] | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._ledger = ledger
        self._fill_model = fill_model
        self.run_id = run_id
        self._currency = currency
        self._cal = calendar if calendar is not None else SessionCalendar()
        self._settings: Callable[[], RuntimeSettings] = settings if settings is not None else RuntimeSettings

    def entry_cutoff(self, day: date) -> datetime | None:
        """The moment entries stop filling on `day`: session close - no_entry_before_close_minutes (BR-42)."""
        if not self._cal.is_session(day):
            return None
        minutes = self._settings().no_entry_before_close_minutes
        return self._cal.session_close(day) - timedelta(minutes=minutes)

    @contextmanager
    def _session(self, session: Session | None) -> Iterator[Session]:
        if session is not None:
            yield session
        else:
            with session_scope(self._factory) as s:
                yield s

    def _log(self, s: Session, level: str, message: str, data: dict[str, Any]) -> None:
        log_event(s, self._clock, level, SOURCE, message, data, run_id=self.run_id)

    # --- orders -------------------------------------------------------------------------------------
    def submit(self, spec: OrderSpec, session: Session | None = None) -> int:
        with self._session(session) as s:
            return self._submit(s, spec, self._clock.now())

    def _submit(self, s: Session, spec: OrderSpec, now: datetime) -> int:
        pos: m.Position | None = None
        if spec.position_id is not None:
            pos = s.get(m.Position, spec.position_id, with_for_update=True, populate_existing=True)
            if pos is None or pos.run_id != self.run_id or pos.closed_at is not None:
                raise BrokerRejected(f"position {spec.position_id} is not open")
            if spec.symbol_id != pos.symbol_id:
                raise BrokerRejected(f"order symbol {spec.symbol_id} doesn't match position symbol {pos.symbol_id}")
            if spec.qty > pos.qty:
                raise BrokerRejected(f"a sell of {spec.qty} exceeds the position of {pos.qty} (long only)")
            if spec.qty < pos.qty:
                raise BrokerRejected(f"partial exits are not supported: sell all {pos.qty} shares")
        order = m.Order(
            run_id=self.run_id,
            proposal_id=spec.proposal_id,
            position_id=spec.position_id,
            strategy_config_id=spec.strategy_config_id or (pos.strategy_config_id if pos else None),
            symbol_id=spec.symbol_id,
            side=spec.side,
            order_type=spec.order_type,
            purpose=spec.purpose,
            qty=spec.qty,
            stop_price=spec.stop,
            limit_price=spec.limit,
            stop_loss=spec.stop_loss,
            tif=spec.tif,
            status="working",
            reason=spec.reason,
            session_date=et_date(now),
            submitted_at=now,
            stale_alerted=False,
        )
        s.add(order)
        s.flush()
        if spec.purpose == "stop" and pos is not None:
            if pos.stop_order_id is not None:
                old = s.get(m.Order, pos.stop_order_id, with_for_update=True)
                if old is not None and old.status == "working":
                    self._close_order(old, now, "replaced by a new stop")
            pos.stop_order_id = order.id
            _end_unprotected(pos, now)
        self._log(s, "info", f"order {order.id} working", {"order_id": order.id, **spec.to_json()})
        return order.id

    def cancel(self, order_id: int, reason: str, session: Session | None = None) -> bool:
        now = self._clock.now()
        with self._session(session) as s:
            order = s.get(m.Order, order_id, with_for_update=True, populate_existing=True)
            if order is None or order.run_id != self.run_id or order.status != "working":
                return False
            self._close_order(order, now, reason)
            if order.purpose == "stop" and order.position_id is not None:
                pos = s.get(m.Position, order.position_id, with_for_update=True)
                if pos is not None and pos.closed_at is None and pos.stop_order_id == order.id:
                    pos.stop_order_id = None
                    pos.unprotected_since = now
            self._log(s, "info", f"order {order_id} cancelled", {"order_id": order_id, "reason": reason})
            return True

    @staticmethod
    def _close_order(order: m.Order, now: datetime, reason: str) -> None:
        order.status, order.closed_at, order.cancel_reason = "cancelled", now, reason

    def end_of_session(self, session_date: date) -> list[int]:
        now = self._clock.now()
        with session_scope(self._factory) as s:
            orders = (
                s.execute(
                    select(m.Order)
                    .where(
                        m.Order.run_id == self.run_id,
                        m.Order.status == "working",
                        m.Order.session_date <= session_date,
                    )
                    .order_by(m.Order.id)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            for order in orders:
                self._close_order(order, now, "end_of_session")
                if order.purpose == "stop" and order.position_id is not None:
                    pos = s.get(m.Position, order.position_id)
                    if pos is not None and pos.closed_at is None and pos.stop_order_id == order.id:
                        pos.stop_order_id = None
                        pos.unprotected_since = now
            ids = [o.id for o in orders]
            if ids:
                self._log(s, "info", f"end of session: cancelled {len(ids)} orders", {"order_ids": ids})
            return ids

    # --- quotes and fills -------------------------------------------------------------------------------
    def on_quotes(self, quotes: Sequence[QtQuote], now: datetime) -> list[FillEvent]:
        by_symbol = {q.symbol_id: q for q in quotes}
        if not by_symbol:
            return []
        events: list[FillEvent] = []
        with session_scope(self._factory) as s:
            orders = (
                s.execute(
                    select(m.Order)
                    .where(
                        m.Order.run_id == self.run_id,
                        m.Order.status == "working",
                        m.Order.symbol_id.in_(list(by_symbol)),
                    )
                    .order_by(m.Order.id)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            today = et_date(now)
            cutoff: datetime | None = None
            cutoff_read = False
            for order in orders:
                if order.status != "working":  # cancelled earlier in this batch (position closed)
                    continue
                if order.purpose == "entry":
                    if not cutoff_read:  # read the settings once per batch, and only when an entry is working
                        cutoff, cutoff_read = self.entry_cutoff(today), True
                    if cutoff is None or now >= cutoff or order.session_date != today:
                        self._refuse_late_entry(s, order, now, cutoff)
                        continue
                outcome = self._fill_model.assess(_spec(order), by_symbol[order.symbol_id], now)
                if isinstance(outcome, NoFill):
                    self._no_fill(s, order, outcome, now)
                    continue
                events.append(self._fill(s, order, outcome, now))
        return events

    def _refuse_late_entry(self, s: Session, order: m.Order, now: datetime, cutoff: datetime | None) -> None:
        self._close_order(order, now, ENTRY_CUTOFF)
        self._log(
            s,
            "warning",
            f"order {order.id}: entry cutoff reached, cancelled instead of filled (no overnight hold)",
            {
                "order_id": order.id,
                "reason": ENTRY_CUTOFF,
                "cutoff": cutoff.isoformat() if cutoff else None,
                "order_session": order.session_date.isoformat(),
            },
        )

    def _no_fill(self, s: Session, order: m.Order, outcome: NoFill, now: datetime) -> None:
        if outcome.reason not in UNUSABLE_QUOTE:
            order.stale_since = None
            return
        data = {"order_id": order.id, "reason": outcome.reason, "detail": outcome.detail}
        if order.stale_since is None:
            order.stale_since = now
            self._log(s, "warning", f"order {order.id}: unusable quote ({outcome.reason}), not filling", data)
        elif not order.stale_alerted and now - order.stale_since >= STALE_ALERT_AFTER:
            order.stale_alerted = True
            self._log(s, "error", f"order {order.id}: unusable quote for over 60 s ({outcome.reason})", data)

    def _fill(self, s: Session, order: m.Order, d: FillDecision, now: datetime) -> FillEvent:
        fill = m.Fill(
            run_id=self.run_id,
            order_id=order.id,
            ts=now,
            qty=d.qty,
            price=d.price,
            fees=d.fees.to_json(),
            quote_snapshot=d.quote_snapshot,
            slippage=d.slippage,
        )
        s.add(fill)
        s.flush()
        order.status, order.closed_at, order.stale_since = "filled", now, None
        trade_date = et_date(now)
        value = (d.price * d.qty).quantize(Q4, ROUND_HALF_UP)
        ref = f"fill:{fill.id}"
        trade: m.Trade | None = None
        if order.side == "buy":
            self._ledger.record(
                s, run_id=self.run_id, ts=now, trade_date=trade_date, amount=-value, kind="buy", ref=ref,
                currency=self._currency,
            )
            planned = (d.price - order.stop_loss) * d.qty if order.stop_loss is not None else None
            pos = m.Position(
                run_id=self.run_id,
                symbol_id=order.symbol_id,
                strategy_config_id=order.strategy_config_id,
                qty=d.qty,
                avg_price=d.price,
                stop_loss=order.stop_loss,
                planned_risk=planned.quantize(Q4, ROUND_HALF_UP) if planned is not None and planned > 0 else None,
                session_date=trade_date,
                opened_at=now,
                entry_order_id=order.id,
                stop_order_id=None,
                unprotected_since=now,
                unprotected_seconds=0,
            )
            s.add(pos)
            s.flush()
            order.position_id = pos.id
        else:
            self._ledger.record(
                s, run_id=self.run_id, ts=now, trade_date=trade_date, amount=value, kind="sell", ref=ref,
                currency=self._currency,
            )
            found = s.get(m.Position, order.position_id, with_for_update=True) if order.position_id else None
            if found is None:
                raise BrokerRejected(f"order {order.id} sells position {order.position_id}, which doesn't exist")
            pos = found
            trade = self._close_position(s, pos, order, d, now, trade_date)
        if d.fees.total > 0:
            self._ledger.record(
                s, run_id=self.run_id, ts=now, trade_date=trade_date, amount=-d.fees.total, kind="fee",
                ref=f"{ref}:fees", currency=self._currency,
            )
        self._log(
            s,
            "info",
            f"order {order.id} filled: {order.side} {d.qty} @ {d.price}",
            {"order_id": order.id, "fill_id": fill.id, "price": str(d.price), "trigger": d.trigger},
        )
        return FillEvent(
            fill_id=fill.id,
            order_id=order.id,
            run_id=self.run_id,
            symbol_id=order.symbol_id,
            side=order.side,  # type: ignore[arg-type]
            purpose=order.purpose,  # type: ignore[arg-type]
            qty=d.qty,
            price=d.price,
            ts=now,
            position_id=pos.id,
            strategy_config_id=pos.strategy_config_id,
            stop_loss=pos.stop_loss,
            proposal_id=order.proposal_id,
            trade_id=trade.id if trade else None,
            pnl=trade.pnl if trade else None,
        )

    def _close_position(
        self, s: Session, pos: m.Position, order: m.Order, d: FillDecision, now: datetime, trade_date: date
    ) -> m.Trade:
        entry_fill = s.execute(select(m.Fill).where(m.Fill.order_id == pos.entry_order_id)).scalar_one()
        fees_total = Fees.from_json(entry_fill.fees).total + d.fees.total
        pnl = ((d.price - pos.avg_price) * pos.qty - fees_total).quantize(Q4, ROUND_HALF_UP)
        pnl_r = (pnl / pos.planned_risk).quantize(Q4, ROUND_HALF_UP) if pos.planned_risk else None
        _end_unprotected(pos, now)
        pos.closed_at = now
        trade = m.Trade(
            run_id=self.run_id,
            position_id=pos.id,
            symbol_id=pos.symbol_id,
            session_date=trade_date,
            entry_price=pos.avg_price,
            exit_price=d.price,
            qty=pos.qty,
            pnl=pnl,
            pnl_r=pnl_r,
            planned_risk=pos.planned_risk,
            exit_reason=order.reason or order.purpose,
            slippage_total=((entry_fill.slippage + d.slippage) * pos.qty).quantize(Q4, ROUND_HALF_UP),
            fees_total=fees_total,
            opened_at=pos.opened_at,
            closed_at=now,
        )
        s.add(trade)
        s.flush()
        others = s.execute(
            select(m.Order)
            .where(m.Order.position_id == pos.id, m.Order.status == "working", m.Order.id != order.id)
            .with_for_update()
        ).scalars()
        for other in others:
            self._close_order(other, now, "position closed")
        return trade

    # --- read side ----------------------------------------------------------------------------------------
    def open_positions(self) -> list[PositionView]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Position)
                .where(m.Position.run_id == self.run_id, m.Position.closed_at.is_(None))
                .order_by(m.Position.id)
            ).scalars()
            return [position_view(p) for p in rows]

    def working_orders(self) -> list[OrderView]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Order).where(m.Order.run_id == self.run_id, m.Order.status == "working").order_by(m.Order.id)
            ).scalars()
            return [order_view(o) for o in rows]

    def working_symbol_ids(self) -> list[int]:
        return sorted({o.symbol_id for o in self.working_orders()})

    def account_state(self, today: date, marks: Mapping[int, Decimal], cash_account_mode: bool) -> AccountState:
        with self._factory() as s:
            bal = self._ledger.balances(s, self.run_id, today)
        value = sum(
            ((marks.get(p.symbol_id) or p.avg_price) * p.qty for p in self.open_positions()), ZERO
        ).quantize(Q4, ROUND_HALF_UP)
        total = bal.total.quantize(Q4, ROUND_HALF_UP)
        settled = bal.settled.quantize(Q4, ROUND_HALF_UP)
        return AccountState(
            total_cash=total,
            settled_cash=settled,
            buying_power=settled if cash_account_mode else total,
            positions_value=value,
            equity=total + value,
        )

    def snapshot_equity(self, now: datetime, account: AccountState) -> None:
        with session_scope(self._factory) as s:
            prev = s.execute(
                select(func.max(m.EquitySnapshot.peak_equity)).where(m.EquitySnapshot.run_id == self.run_id)
            ).scalar_one()
            peak = max(account.equity, prev) if prev is not None else account.equity
            dd = ((peak - account.equity) / peak).quantize(Q4, ROUND_HALF_UP) if peak > 0 else ZERO
            stmt = pg_insert(m.EquitySnapshot).values(
                run_id=self.run_id,
                ts=now,
                equity=account.equity,
                cash=account.total_cash,
                settled_cash=account.settled_cash,
                peak_equity=peak,
                drawdown_pct=dd,
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[m.EquitySnapshot.run_id, m.EquitySnapshot.ts],
                    set_={
                        k: stmt.excluded[k] for k in ("equity", "cash", "settled_cash", "peak_equity", "drawdown_pct")
                    },
                )
            )
```

- [x] **Step 5: Run the tests**

Run: `uv --directory Trader/app run pytest tests/broker -q`
Expected: all pass. If `test_order_fills_only_once` shows 2 fills, the `with_for_update(skip_locked=True)` is missing from `on_quotes`.

- [x] **Step 6: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/broker/base.py Trader/app/trader/broker/sim_broker.py Trader/app/tests/broker/test_sim_broker.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T5: simulated broker with positions, trades, T+1 cash, unprotected time and equity snapshots" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T6: Strategy framework, registry, entry points, strategy test fakes

**Files:**
- Modify: `Trader/app/trader/market/types.py` (add four dataclasses), `Trader/app/pyproject.toml` (entry-point table)
- Create: `Trader/app/trader/strategies/__init__.py`, `Trader/app/trader/strategies/base.py`, `Trader/app/trader/strategies/registry.py`, `Trader/app/tests/strategies/__init__.py`, `Trader/app/tests/strategies/fakes.py`, `Trader/app/tests/strategies/demo_plugin.py`, `Trader/app/tests/strategies/test_framework.py`

**Interfaces:**
- Consumes: `SessionCalendar.session_open/session_close` (P1-T4; raise `ValueError` on a non-session); `Candle`, `Interval` (P1-T4); `QtQuote` (P1-T7); `AccountState`, `OrderView`, `PositionView`, `Fill`, `dec_str` (P2-T4); `StrategyConfig`, `AuditLog` models (P2-T1, P1-T2); `session_scope` (P1-T2); `tests.factories` (P2-T1).
- Produces:
  - `trader.market.types` additions (frozen, slots): `UniverseMember(symbol_id, ticker, name, price, avg_volume, atr14, source)`; `UniverseStatus(source: str | None, fallback_from: date | None, stale: bool, age_sessions: int | None)`; `OpenBarStats(symbol_id, avg_open_vol_14d, atr14)`; `OpeningBars(bars: dict[int, Candle], missing: dict[int, str])`.
  - `trader.strategies.base`:
    - `SessionOffset(anchor: Literal["open","close"], seconds: int)`: `SessionOffset.parse(text)` accepts `open`, `close`, `open+5m`, `close-30m`, `open+5m5s` (else `ValueError`); `.resolve(cal, session) -> datetime` (UTC; follows early closes; `ValueError` on a non-session); `str()` gives the canonical text.
    - `ScheduledEvent(key: str, at: SessionOffset)`.
    - Intents (frozen dataclasses, master-plan contract): `EnterLong(symbol_id, order_type, stop, limit, stop_loss, reason, evidence)`, `Exit(position_id, order_type: Literal["market","stop"], stop, reason)`, `Cancel(order_id, reason)`; `Intent = EnterLong | Exit | Cancel`; `intent_to_json(intent) -> dict[str, Any]` (`"type"` is `enter_long`/`exit`/`cancel`, Decimals as strings).
    - `CandidateRecord(symbol_id, rvol, rank, candle, passed=False, reject_reason=None, data={})` (mutable); `DecisionNote(message, level="info", data={})`.
    - Protocols: `MarketDataView` (async `universe`, `universe_status`, `open_bar_stats`, `opening_bars`, `quotes`, `candles`, `prior_close`, `symbol_ids`; P2-T7 implements it); `CatalystInfo` (read-only `catalyst_type`, `direction`, `quality`, `classified`); `CatalystSource` (`async get(symbol_ids, session_date) -> Mapping[int, CatalystInfo]`; P2-T12 implements it).
    - `StrategyContext(clock, calendar, session_date, data, catalysts, params, strategy_config_id, positions, working_orders, account, entries_today=0, candidates=[], notes=[])` with `note(message, level="info", **data)`. For an `entry` strategy `positions`/`working_orders` are its own; for an `overlay` strategy `positions` are every open position of the entry strategies (the engine decides, P2-T13).
    - `Strategy` protocol (SPEC §5.1, refined: `on_event`/`on_fill` are `async`): read-only `key`, `version`, `kind: Literal["entry","overlay"]`, `params_model: type[BaseModel]`, `params: BaseModel`; `schedule(cal) -> list[ScheduledEvent]`; `async on_event(ctx, event) -> list[Intent]`; `async on_fill(ctx, fill: Fill) -> list[Intent]`. Plug-in classes take their validated params in the constructor: `Plugin(params: ParamsModel | None = None)`.
  - `trader.strategies.registry`: `ENTRY_POINT_GROUP = "trader.strategies"`; `PluginError(Exception)`; `available() -> dict[str, EntryPoint]`; `load_plugin(name) -> type[Any]` (checks the class has the protocol members and `cls.key == name`); `load_all() -> dict[str, type[Any]]`; `StrategyConfigView(id, strategy_key, version, revision, params: dict[str, Any], enabled, created_at)`; `StrategyRegistry(factory, clock, plugins: Mapping[str, type[Any]] | None = None)` (default: `load_all()`) with `keys()`, `plugin_class(key)`, `json_schema(key)`, `ensure_defaults(actor="system")` (revision 1 with the model's defaults, enabled; a new revision when the plug-in version changed), `current(key) -> StrategyConfigView` (`KeyError` before `ensure_defaults`), `update(key, *, params=None, enabled=None, actor) -> StrategyConfigView` (merges, validates with the plug-in's pydantic model, `pydantic.ValidationError` on bad params, writes a new revision plus an `audit_log` row `strategy.update:<key>`; a no-change update returns the current revision), `instance(key) -> tuple[Strategy, StrategyConfigView]`, `enabled() -> list[tuple[Strategy, StrategyConfigView]]`, `config_ids(key) -> set[int]` (every revision), `config_key(config_id) -> str | None`.
  - `pyproject.toml`: entry points `orb_sip = "trader.strategies.orb_sip:OrbSip"` and `spy_overlay = "trader.strategies.spy_overlay:SpyOverlay"` (the modules arrive in P2-T8/T9; `available()` lists names without importing, and nothing loads them before T13).
  - `tests/strategies/fakes.py`: `CAL`, `SESSION` (Tue 2026-10-06), `NOW` (09:35:05 ET), `bar(...)`, `quote(...)`, `FakeCatalyst`, `FakeCatalysts`, `FakeData`, `make_ctx(...)`, `position(...)`, `working_entry(...)` for P2-T8 and P2-T9.

- [x] **Step 1: Add the market data types**

In `Trader/app/trader/market/types.py` change `from datetime import datetime` to `from datetime import date, datetime` and append:
```python


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One row of a session's universe (universe_snapshots joined to symbols); symbol_id is symbols.id."""

    symbol_id: int
    ticker: str
    name: str | None
    price: Decimal | None
    avg_volume: int | None
    atr14: Decimal | None
    source: str  # finviz | fallback | manual


@dataclass(frozen=True, slots=True)
class UniverseStatus:
    """Where a session's universe came from. `stale` is the nightly job's verdict on a fallback universe."""

    source: str | None  # None: no universe stored for the session
    fallback_from: date | None
    stale: bool
    age_sessions: int | None


@dataclass(frozen=True, slots=True)
class OpenBarStats:
    symbol_id: int
    avg_open_vol_14d: Decimal | None  # None with too few opening bars (P1-T9)
    atr14: Decimal | None


@dataclass(frozen=True, slots=True)
class OpeningBars:
    bars: dict[int, Candle]
    missing: dict[int, str]  # symbol_id -> reason, e.g. "no_bar_at_open" (Review Focus 4: reported, not raised)
```

- [x] **Step 2: Write the failing framework tests and the shared fakes**

`Trader/app/tests/strategies/__init__.py`: empty file.

`Trader/app/tests/strategies/demo_plugin.py`:
```python
"""A minimal plug-in the registry tests load through a patched entry point."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trader.broker.types import Fill
from trader.market.calendar import SessionCalendar
from trader.strategies.base import Intent, ScheduledEvent, SessionOffset, StrategyContext


class DemoParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    threshold: int = Field(5, ge=1, le=10)
    at: str = "open+15m"


class DemoStrategy:
    key = "demo"
    version = "0.1.0"
    kind: Literal["entry", "overlay"] = "entry"
    params_model = DemoParams

    def __init__(self, params: DemoParams | None = None) -> None:
        self.params = params or DemoParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return [ScheduledEvent("demo_event", SessionOffset.parse(self.params.at))]

    async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
        return []

    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
        return []


class Mislabeled(DemoStrategy):
    key = "not_demo"
```

`Trader/app/tests/strategies/fakes.py`:
```python
"""Shared strategy-test fakes: market data and catalysts held in plain dicts (no DB, no network)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import AccountState, OrderView, PositionView
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.types import Candle, Interval, OpenBarStats, OpeningBars, UniverseMember, UniverseStatus
from trader.strategies.base import StrategyContext

CAL = SessionCalendar()
SESSION = date(2026, 10, 6)  # a Tuesday
NOW = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET


def bar(o: str, h: str, low: str, c: str, volume: int, start: datetime | None = None) -> Candle:
    start = start or CAL.session_open(SESSION)
    return Candle(start, start + timedelta(minutes=5), Decimal(o), Decimal(h), Decimal(low), Decimal(c), volume, None)


def quote(symbol_id: int, bid: str, ask: str, last: str, at: datetime = NOW, age: float = 1.0) -> QtQuote:
    return QtQuote(
        symbol_id=symbol_id,
        symbol=f"S{symbol_id}",
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(last),
        last_regular=None,
        volume=100_000,
        last_trade_time=at - timedelta(seconds=age),
        delay=0,
        is_halted=False,
        vwap=None,
    )


@dataclass
class FakeCatalyst:
    catalyst_type: str = "earnings_beat"
    direction: str = "bullish"
    quality: int | None = 80
    classified: bool = True


class FakeCatalysts:
    def __init__(self, by_symbol: dict[int, FakeCatalyst] | None = None) -> None:
        self.by_symbol = by_symbol or {}
        self.requested: list[list[int]] = []

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, FakeCatalyst]:
        self.requested.append(list(symbol_ids))
        return {s: self.by_symbol[s] for s in symbol_ids if s in self.by_symbol}


@dataclass
class FakeData:
    members: list[UniverseMember] = field(default_factory=list)
    stats: dict[int, OpenBarStats] = field(default_factory=dict)
    bars: dict[int, Candle] = field(default_factory=dict)
    missing: dict[int, str] = field(default_factory=dict)
    quote_map: dict[int, QtQuote] = field(default_factory=dict)
    closes: dict[int, Decimal] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)
    status: UniverseStatus = field(default_factory=lambda: UniverseStatus("finviz", None, False, None))

    def add(
        self,
        symbol_id: int,
        ticker: str,
        opening: Candle | None,
        *,
        avg_open_vol: str | None = "1000",
        atr: str | None = "1.00",
        price: str = "20",
        avg_volume: int | None = 2_000_000,
    ) -> None:
        self.members.append(
            UniverseMember(
                symbol_id,
                ticker,
                f"{ticker} Inc",
                Decimal(price),
                avg_volume,
                Decimal(atr) if atr else None,
                "finviz",
            )
        )
        self.stats[symbol_id] = OpenBarStats(
            symbol_id, Decimal(avg_open_vol) if avg_open_vol else None, Decimal(atr) if atr else None
        )
        self.ids[ticker] = symbol_id
        if opening is not None:
            self.bars[symbol_id] = opening
        else:
            self.missing[symbol_id] = "no_bar_at_open"

    async def universe(self, session_date: date) -> list[UniverseMember]:
        return list(self.members)

    async def universe_status(self, session_date: date) -> UniverseStatus:
        return self.status

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        return dict(self.stats)

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        ids = list(symbol_ids) if symbol_ids is not None else [m.symbol_id for m in self.members]
        return OpeningBars(
            {i: self.bars[i] for i in ids if i in self.bars},
            {i: self.missing[i] for i in ids if i in self.missing},
        )

    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        return {i: self.quote_map[i] for i in symbol_ids if i in self.quote_map}

    async def candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]:
        return []

    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None:
        return self.closes.get(symbol_id)

    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]:
        return {t: self.ids[t] for t in tickers if t in self.ids}


ACCOUNT = AccountState(Decimal("720"), Decimal("720"), Decimal("720"), Decimal("0"), Decimal("720"))


def make_ctx(
    data: FakeData,
    params: BaseModel,
    catalysts: FakeCatalysts | None = None,
    *,
    positions: Sequence[PositionView] = (),
    orders: Sequence[OrderView] = (),
    entries_today: int = 0,
    now: datetime = NOW,
    session: date = SESSION,
    config_id: int = 1,
) -> StrategyContext:
    return StrategyContext(
        clock=FixedClock(now),
        calendar=CAL,
        session_date=session,
        data=data,
        catalysts=catalysts or FakeCatalysts(),
        params=params,
        strategy_config_id=config_id,
        positions=list(positions),
        working_orders=list(orders),
        account=ACCOUNT,
        entries_today=entries_today,
    )


def position(pid: int, symbol_id: int, *, qty: int = 10, stop_loss: str = "19.90", config_id: int = 1) -> PositionView:
    return PositionView(
        id=pid,
        symbol_id=symbol_id,
        strategy_config_id=config_id,
        qty=qty,
        avg_price=Decimal("20.00"),
        stop_loss=Decimal(stop_loss),
        opened_at=NOW,
        session_date=SESSION,
        stop_order_id=None,
        unprotected_since=NOW,
        unprotected_seconds=0,
    )


def working_entry(oid: int, symbol_id: int, *, config_id: int = 1) -> OrderView:
    return OrderView(
        id=oid,
        symbol_id=symbol_id,
        side="buy",
        order_type="stop",
        purpose="entry",
        qty=10,
        stop=Decimal("20.01"),
        limit=None,
        status="working",
        position_id=None,
        strategy_config_id=config_id,
        proposal_id=None,
        submitted_at=NOW,
    )
```

`Trader/app/tests/strategies/test_framework.py`:
```python
import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import EntryPoint
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.strategies.demo_plugin import DemoParams, DemoStrategy
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.strategies import registry as reg
from trader.strategies.base import Cancel, EnterLong, Exit, SessionOffset, intent_to_json
from trader.strategies.registry import PluginError, StrategyRegistry

CAL = SessionCalendar()
CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    ("text", "anchor", "seconds"),
    [("open+5m", "open", 300), ("close-30m", "close", -1800), ("open+5m5s", "open", 305), ("open", "open", 0),
     ("close-10m", "close", -600), ("open+120m", "open", 7200)],
)
def test_session_offset_parses_and_prints(text: str, anchor: str, seconds: int) -> None:
    off = SessionOffset.parse(text)
    assert (off.anchor, off.seconds) == (anchor, seconds)
    assert str(off) == text


@pytest.mark.parametrize("text", ["noon+5m", "open+5", "open+5m60s", "close*2m", "", "open +5m", "open+5000m"])
def test_session_offset_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        SessionOffset.parse(text)


def test_offsets_resolve_on_a_normal_day() -> None:
    day = date(2026, 10, 6)  # EDT: open 13:30Z, close 20:00Z
    assert SessionOffset.parse("open+5m5s").resolve(CAL, day) == datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)
    assert SessionOffset.parse("close-30m").resolve(CAL, day) == datetime(2026, 10, 6, 19, 30, tzinfo=UTC)
    assert SessionOffset.parse("close-10m").resolve(CAL, day) == datetime(2026, 10, 6, 19, 50, tzinfo=UTC)


def test_offsets_follow_an_early_close() -> None:
    day = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close = 18:00Z (EST)
    assert SessionOffset.parse("close-10m").resolve(CAL, day) == datetime(2026, 11, 27, 17, 50, tzinfo=UTC)
    assert SessionOffset.parse("close-30m").resolve(CAL, day) == datetime(2026, 11, 27, 17, 30, tzinfo=UTC)
    assert SessionOffset.parse("open+5m5s").resolve(CAL, day) == datetime(2026, 11, 27, 14, 35, 5, tzinfo=UTC)


def test_offsets_refuse_a_holiday() -> None:
    with pytest.raises(ValueError):
        SessionOffset.parse("open+5m").resolve(CAL, date(2026, 11, 26))  # Thanksgiving


def test_intents_are_frozen_and_serialise() -> None:
    e = EnterLong(7, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {"rvol": "3.2"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.symbol_id = 8  # type: ignore[misc]
    assert intent_to_json(e) == {
        "type": "enter_long",
        "symbol_id": 7,
        "order_type": "stop",
        "stop": "20.01",
        "limit": None,
        "stop_loss": "19.91",
        "reason": "orb_breakout",
    }
    assert intent_to_json(Exit(3, "market", None, "flatten_close"))["type"] == "exit"
    assert intent_to_json(Cancel(9, "entry_cancel_at")) == {"type": "cancel", "order_id": 9, "reason": "entry_cancel_at"}


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, *pairs: tuple[str, str]) -> None:
    eps = [EntryPoint(name=n, value=v, group=reg.ENTRY_POINT_GROUP) for n, v in pairs]
    monkeypatch.setattr(reg, "entry_points", lambda group: [e for e in eps if e.group == group])


def test_plugins_are_found_through_the_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, ("demo", "tests.strategies.demo_plugin:DemoStrategy"))
    assert list(reg.available()) == ["demo"]
    assert reg.load_all() == {"demo": DemoStrategy}


def test_a_plugin_keyed_differently_from_its_entry_point_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, ("demo", "tests.strategies.demo_plugin:Mislabeled"))
    with pytest.raises(PluginError, match="not_demo"):
        reg.load_plugin("demo")
    with pytest.raises(PluginError, match="no strategy plug-in"):
        reg.load_plugin("missing")


def test_the_real_plugins_are_declared() -> None:
    assert {"orb_sip", "spy_overlay"} <= set(reg.available())


@pytest.fixture
def registry(db_factory: sessionmaker[Session]) -> StrategyRegistry:
    r = StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoStrategy})
    r.ensure_defaults()
    return r


def _revisions(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return int(s.execute(select(func.count()).select_from(m.StrategyConfig)).scalar_one())


@pytest.mark.db
def test_defaults_are_revision_one_and_created_once(db_factory: sessionmaker[Session], registry: StrategyRegistry) -> None:
    registry.ensure_defaults()
    cfg = registry.current("demo")
    assert (cfg.revision, cfg.version, cfg.enabled) == (1, "0.1.0", True)
    assert cfg.params == {"threshold": 5, "at": "open+15m"}
    assert _revisions(db_factory) == 1


@pytest.mark.db
def test_each_settings_change_is_a_new_audited_revision(
    db_factory: sessionmaker[Session], registry: StrategyRegistry
) -> None:
    v2 = registry.update("demo", params={"threshold": 7}, actor="stephen")
    assert v2.revision == 2 and v2.params == {"threshold": 7, "at": "open+15m"}
    same = registry.update("demo", params={"threshold": 7}, actor="stephen")
    assert same.id == v2.id  # no change, no new revision
    off = registry.update("demo", enabled=False, actor="stephen")
    assert off.revision == 3 and off.enabled is False
    assert registry.enabled() == []
    assert v2.id in registry.config_ids("demo") and off.id in registry.config_ids("demo")
    assert len(registry.config_ids("demo")) == 3
    assert registry.config_key(v2.id) == "demo"
    with db_factory() as s:
        audits = s.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars().all()
    assert [a.action for a in audits] == ["strategy.update:demo", "strategy.update:demo"]
    assert audits[0].before["params"]["threshold"] == 5 and audits[0].after["params"]["threshold"] == 7


@pytest.mark.db
@pytest.mark.parametrize("bad", [{"threshold": 11}, {"threshold": "lots"}, {"unknown": 1}])
def test_invalid_params_are_rejected(
    db_factory: sessionmaker[Session], registry: StrategyRegistry, bad: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        registry.update("demo", params=bad, actor="stephen")
    assert _revisions(db_factory) == 1


@pytest.mark.db
def test_instance_carries_validated_params(registry: StrategyRegistry) -> None:
    registry.update("demo", params={"at": "close-30m"}, actor="stephen")
    strategy, cfg = registry.instance("demo")
    assert isinstance(strategy.params, DemoParams) and strategy.params.at == "close-30m"
    assert strategy.schedule(CAL)[0].at == SessionOffset.parse("close-30m")
    assert cfg.revision == 2 and registry.json_schema("demo")["properties"]["threshold"]["maximum"] == 10


@pytest.mark.db
def test_current_before_defaults_is_a_key_error(db_factory: sessionmaker[Session]) -> None:
    with pytest.raises(KeyError):
        StrategyRegistry(db_factory, CLOCK, plugins={"demo": DemoStrategy}).current("demo")
```

- [x] **Step 3: Run to see them fail**

Run: `uv --directory Trader/app run pytest tests/strategies/test_framework.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.strategies'`.

- [x] **Step 4: Implement `trader/strategies/base.py`**

`Trader/app/trader/strategies/__init__.py`: empty file.

`Trader/app/trader/strategies/base.py`:
```python
"""Strategy plug-in framework (SPEC §5.1, BR-10).

Strategies return intents. They never size positions, place orders or touch the database: ranked candidates
and decision notes go into the context, and the engine (P2-T13) persists them. on_event and on_fill are async
because market data (Questrade) is async.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import AccountState, Fill, OrderView, PositionView, dec_str
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import Candle, Interval, OpenBarStats, OpeningBars, UniverseMember, UniverseStatus

_OFFSET = re.compile(r"^(open|close)(?:([+-])(\d{1,3})m(?:(\d{1,2})s)?)?$")
MAX_OFFSET_SECONDS = 12 * 3600


@dataclass(frozen=True, slots=True)
class SessionOffset:
    """A time relative to a session's open or close, so early closes work automatically (SPEC §5.1)."""

    anchor: Literal["open", "close"]
    seconds: int

    @classmethod
    def parse(cls, text: str) -> Self:
        m = _OFFSET.match(text)
        if m is None:
            raise ValueError(f"not a session offset: {text!r} (e.g. 'open+5m', 'close-30m', 'open+5m5s')")
        name, sign, minutes, secs = m.groups()
        if secs is not None and int(secs) >= 60:
            raise ValueError(f"seconds must be below 60 in {text!r}")
        total = int(minutes or 0) * 60 + int(secs or 0)
        if total > MAX_OFFSET_SECONDS:
            raise ValueError(f"offset {text!r} is longer than a session")
        anchor: Literal["open", "close"] = "open" if name == "open" else "close"
        return cls(anchor, -total if sign == "-" else total)

    def resolve(self, cal: SessionCalendar, session: date) -> datetime:
        base = cal.session_open(session) if self.anchor == "open" else cal.session_close(session)
        return base + timedelta(seconds=self.seconds)

    def __str__(self) -> str:
        if self.seconds == 0:
            return self.anchor
        minutes, secs = divmod(abs(self.seconds), 60)
        return f"{self.anchor}{'-' if self.seconds < 0 else '+'}{minutes}m" + (f"{secs}s" if secs else "")


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    key: str
    at: SessionOffset


@dataclass(frozen=True)
class EnterLong:
    symbol_id: int
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    stop: Decimal | None
    limit: Decimal | None
    stop_loss: Decimal
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Exit:
    position_id: int
    order_type: Literal["market", "stop"]
    stop: Decimal | None
    reason: str


@dataclass(frozen=True)
class Cancel:
    order_id: int
    reason: str


Intent = EnterLong | Exit | Cancel


def intent_to_json(intent: Intent) -> dict[str, Any]:
    if isinstance(intent, EnterLong):
        return {
            "type": "enter_long",
            "symbol_id": intent.symbol_id,
            "order_type": intent.order_type,
            "stop": dec_str(intent.stop),
            "limit": dec_str(intent.limit),
            "stop_loss": dec_str(intent.stop_loss),
            "reason": intent.reason,
        }
    if isinstance(intent, Exit):
        return {
            "type": "exit",
            "position_id": intent.position_id,
            "order_type": intent.order_type,
            "stop": dec_str(intent.stop),
            "reason": intent.reason,
        }
    return {"type": "cancel", "order_id": intent.order_id, "reason": intent.reason}


@dataclass
class CandidateRecord:
    """One ranked name and why it was rejected (SPEC §5.2 step 7). Persisted to `candidates` by the engine."""

    symbol_id: int
    rvol: Decimal | None
    rank: int | None
    candle: dict[str, Any] | None
    passed: bool = False
    reject_reason: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecisionNote:
    message: str
    level: str = "info"
    data: dict[str, Any] = field(default_factory=dict)


class MarketDataView(Protocol):
    async def universe(self, session_date: date) -> list[UniverseMember]: ...
    async def universe_status(self, session_date: date) -> UniverseStatus: ...
    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]: ...
    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars: ...
    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]: ...
    async def candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]: ...
    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None: ...
    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]: ...


class CatalystInfo(Protocol):
    @property
    def catalyst_type(self) -> str: ...
    @property
    def direction(self) -> str: ...
    @property
    def quality(self) -> int | None: ...
    @property
    def classified(self) -> bool: ...


class CatalystSource(Protocol):
    async def get(self, symbol_ids: Sequence[int], session_date: date) -> Mapping[int, CatalystInfo]: ...


@dataclass
class StrategyContext:
    clock: Clock
    calendar: SessionCalendar
    session_date: date
    data: MarketDataView
    catalysts: CatalystSource
    params: BaseModel
    strategy_config_id: int
    positions: list[PositionView]
    working_orders: list[OrderView]
    account: AccountState
    entries_today: int = 0
    candidates: list[CandidateRecord] = field(default_factory=list)
    notes: list[DecisionNote] = field(default_factory=list)

    def note(self, message: str, level: str = "info", **data: Any) -> None:
        self.notes.append(DecisionNote(message, level, data))


@runtime_checkable
class Strategy(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def kind(self) -> Literal["entry", "overlay"]: ...
    @property
    def params_model(self) -> type[BaseModel]: ...
    @property
    def params(self) -> BaseModel: ...

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]: ...
    async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]: ...
    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]: ...
```

- [x] **Step 5: Implement `trader/strategies/registry.py`**

```python
"""Strategy plug-ins found through the `trader.strategies` entry point (SPEC §5.1, BR-10).

Settings live in strategy_configs, one row per revision. Every change is a new revision, so each signal
records the exact settings (and plug-in version) that produced it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import EntryPoint, entry_points
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock
from trader.strategies.base import Strategy

ENTRY_POINT_GROUP = "trader.strategies"
_REQUIRED = ("key", "version", "kind", "params_model", "schedule", "on_event", "on_fill")


class PluginError(Exception):
    """A plug-in is missing or doesn't satisfy the Strategy protocol."""


def available() -> dict[str, EntryPoint]:
    """Declared plug-ins by name. Nothing is imported until load_plugin()."""
    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def load_plugin(name: str) -> type[Any]:
    eps = available()
    if name not in eps:
        raise PluginError(f"no strategy plug-in named {name!r}")
    cls = eps[name].load()
    missing = [a for a in _REQUIRED if not hasattr(cls, a)]
    if missing:
        raise PluginError(f"plug-in {name!r} lacks {missing}")
    if cls.key != name:
        raise PluginError(f"entry point {name!r} loads a plug-in keyed {cls.key!r}")
    if not (isinstance(cls.params_model, type) and issubclass(cls.params_model, BaseModel)):
        raise PluginError(f"plug-in {name!r}: params_model must be a pydantic model")
    return cast(type[Any], cls)


def load_all() -> dict[str, type[Any]]:
    return {name: load_plugin(name) for name in sorted(available())}


@dataclass(frozen=True, slots=True)
class StrategyConfigView:
    id: int
    strategy_key: str
    version: str
    revision: int
    params: dict[str, Any]
    enabled: bool
    created_at: datetime


def _view(row: m.StrategyConfig) -> StrategyConfigView:
    return StrategyConfigView(
        row.id, row.strategy_key, row.version, row.revision, dict(row.params), row.enabled, row.created_at
    )


def _snapshot(row: m.StrategyConfig) -> dict[str, Any]:
    return {"revision": row.revision, "version": row.version, "params": row.params, "enabled": row.enabled}


class StrategyRegistry:
    def __init__(
        self, factory: sessionmaker[Session], clock: Clock, plugins: Mapping[str, type[Any]] | None = None
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._plugins = dict(plugins) if plugins is not None else load_all()

    def keys(self) -> list[str]:
        return sorted(self._plugins)

    def plugin_class(self, key: str) -> type[Any]:
        if key not in self._plugins:
            raise KeyError(f"unknown strategy {key!r}")
        return self._plugins[key]

    def json_schema(self, key: str) -> dict[str, Any]:
        schema: dict[str, Any] = self.plugin_class(key).params_model.model_json_schema()
        return schema

    @staticmethod
    def _lock(s: Session, key: str) -> None:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"strategy_configs:{key}"})

    @staticmethod
    def _latest(s: Session, key: str) -> m.StrategyConfig | None:
        return s.execute(
            select(m.StrategyConfig)
            .where(m.StrategyConfig.strategy_key == key)
            .order_by(m.StrategyConfig.revision.desc())
            .limit(1)
        ).scalar_one_or_none()

    def ensure_defaults(self, actor: str = "system") -> None:
        now = self._clock.now()
        for key in self.keys():
            cls = self.plugin_class(key)
            with session_scope(self._factory) as s:
                self._lock(s, key)
                latest = self._latest(s, key)
                if latest is not None and latest.version == cls.version:
                    continue
                params = (
                    cls.params_model().model_dump(mode="json")
                    if latest is None
                    else cls.params_model.model_validate(latest.params).model_dump(mode="json")
                )
                s.add(
                    m.StrategyConfig(
                        strategy_key=key,
                        version=cls.version,
                        revision=1 if latest is None else latest.revision + 1,
                        params=params,
                        enabled=True if latest is None else latest.enabled,
                        created_at=now,
                        created_by=actor,
                    )
                )

    def current(self, key: str) -> StrategyConfigView:
        self.plugin_class(key)
        with self._factory() as s:
            row = self._latest(s, key)
            if row is None:
                raise KeyError(f"strategy {key!r} has no settings yet: call ensure_defaults() first")
            return _view(row)

    def update(
        self,
        key: str,
        *,
        params: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        actor: str,
    ) -> StrategyConfigView:
        cls = self.plugin_class(key)
        now = self._clock.now()
        with session_scope(self._factory) as s:
            self._lock(s, key)
            latest = self._latest(s, key)
            if latest is None:
                raise KeyError(f"strategy {key!r} has no settings yet: call ensure_defaults() first")
            merged = {**latest.params, **(params or {})}
            validated = cls.params_model.model_validate(merged).model_dump(mode="json")  # ValidationError
            new_enabled = latest.enabled if enabled is None else enabled
            if validated == latest.params and new_enabled == latest.enabled and latest.version == cls.version:
                return _view(latest)
            row = m.StrategyConfig(
                strategy_key=key,
                version=cls.version,
                revision=latest.revision + 1,
                params=validated,
                enabled=new_enabled,
                created_at=now,
                created_by=actor,
            )
            s.add(row)
            s.flush()
            s.add(
                m.AuditLog(
                    ts=now,
                    actor=actor,
                    action=f"strategy.update:{key}",
                    before=_snapshot(latest),
                    after=_snapshot(row),
                )
            )
            return _view(row)

    def instance(self, key: str) -> tuple[Strategy, StrategyConfigView]:
        cfg = self.current(key)
        cls = self.plugin_class(key)
        return cast(Strategy, cls(cls.params_model.model_validate(cfg.params))), cfg

    def enabled(self) -> list[tuple[Strategy, StrategyConfigView]]:
        return [self.instance(k) for k in self.keys() if self.current(k).enabled]

    def config_ids(self, key: str) -> set[int]:
        with self._factory() as s:
            return set(s.execute(select(m.StrategyConfig.id).where(m.StrategyConfig.strategy_key == key)).scalars())

    def config_key(self, config_id: int) -> str | None:
        with self._factory() as s:
            return s.execute(
                select(m.StrategyConfig.strategy_key).where(m.StrategyConfig.id == config_id)
            ).scalar_one_or_none()
```

- [x] **Step 6: Declare the entry points**

In `Trader/app/pyproject.toml`, directly after the `[project.scripts]` table, add:
```toml

[project.entry-points."trader.strategies"]
orb_sip = "trader.strategies.orb_sip:OrbSip"
spy_overlay = "trader.strategies.spy_overlay:SpyOverlay"
```
Run: `uv --directory Trader/app sync --reinstall-package trader` so the installed metadata carries them.

- [x] **Step 7: Run the tests**

Run: `uv --directory Trader/app run pytest tests/strategies/test_framework.py -q`
Expected: all pass. `test_the_real_plugins_are_declared` only lists names; the modules arrive in P2-T8/T9.

- [x] **Step 8: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/market/types.py Trader/app/pyproject.toml Trader/app/trader/strategies/__init__.py Trader/app/trader/strategies/base.py Trader/app/trader/strategies/registry.py Trader/app/tests/strategies/__init__.py Trader/app/tests/strategies/fakes.py Trader/app/tests/strategies/demo_plugin.py Trader/app/tests/strategies/test_framework.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T6: strategy framework, session offsets, intents and the versioned plug-in registry" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T7: Market data service

**Files:**
- Create: `Trader/app/trader/market/data_service.py`, `Trader/app/tests/fakes_questrade.py`, `Trader/app/tests/market/test_data_service.py`

**Interfaces:**
- Consumes: `QuestradeClient.quotes/candles/candles_many` and `QuestradeApiError` (P1-T7; `candles()` raises `ValueError` for a window over 20,000 bars); `CandleRequest`, `QtQuote`, `QtSymbol` (P1-T7); `opening_bar` (P1-T8); `repository.upsert_intraday_candles`, `upsert_daily_candles` (P1-T9); models `UniverseSnapshot`, `Symbol`, `OpenBarStat`, `IntradayCandle`, `DailyCandle`, `JobRun` (P1-T2); the nightly job's `job_runs.detail` keys `source`, `fallback_from`, `fallback_stale`, `fallback_age_sessions` (P1-T9 fix round; re-read `trader/jobs/nightly.py` on trunk and use its exact key names); `UniverseMember`, `UniverseStatus`, `OpenBarStats`, `OpeningBars`, `MarketDataView` (P2-T6).
- Produces:
  - `trader.market.data_service.QuoteClient` protocol (the three client methods above).
  - `MarketDataService(factory, clock, calendar, client: QuoteClient)` implementing `MarketDataView`: `universe(session_date)` (DB; sorted by ticker; empty when the nightly job hasn't run), `universe_status(session_date)` (from the latest succeeded `nightly` job run for that session, else the snapshot rows' source; `UniverseStatus(None, None, False, None)` when there is no universe), `open_bar_stats(session_date)` (DB), `symbol_ids(tickers)` (DB), `quotes(symbol_ids) -> dict[int, QtQuote]` (always Questrade; re-keyed to DB ids), `opening_bars(session_date, symbol_ids=None) -> OpeningBars` (the cached 5-minute bar at the open first; the rest in one `candles_many` batch, which the client's rate limiter paces; each fetched, complete bar is cached; per-symbol reasons `no_questrade_id`, `questrade_error: HTTP <n>`, `no_bar_at_open`, `bar_not_complete`), `candles(symbol_id, start, end, interval)` (intraday: the cache when it holds every bar of the window, else Questrade then cache; `OneDay`: Questrade), `prior_close(symbol_id, session_date) -> Decimal | None` (`daily_candles` for the previous session, else Questrade `OneDay`, cached), `prior_closes(symbol_ids, session_date) -> dict[int, Decimal]` (DB only, for the pre-market scan).
  - `tests/fakes_questrade.py`: `FakeQuestrade` (in-memory symbols, quotes, bars and per-id errors; records `calls` as `(method, count)`), used again by P2-T13 and P2-T15.

- [ ] **Step 1: Write the fake client and the failing tests**

`Trader/app/tests/fakes_questrade.py`:
```python
"""An in-memory Questrade client for tests. IDs here are Questrade symbol IDs, as at the real boundary."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtQuote, QtSymbol
from trader.market.types import Candle, Interval


class FakeQuestrade:
    def __init__(self) -> None:
        self.symbols: dict[str, QtSymbol] = {}
        self.quote_map: dict[int, QtQuote] = {}
        self.bars: dict[tuple[int, str], list[Candle]] = {}
        self.errors: dict[int, int] = {}
        self.calls: list[tuple[str, int]] = []

    def add_symbol(self, ticker: str, qt_id: int, *, currency: str = "USD", exchange: str = "NASDAQ") -> QtSymbol:
        sym = QtSymbol(qt_id, ticker, exchange, currency, f"{ticker} Inc", True, True)
        self.symbols[ticker] = sym
        return sym

    def set_quote(self, qt_id: int, bid: str, ask: str, last: str, at: datetime, *, age: float = 1.0) -> None:
        ticker = next((t for t, s in self.symbols.items() if s.symbol_id == qt_id), f"Q{qt_id}")
        self.quote_map[qt_id] = QtQuote(
            symbol_id=qt_id,
            symbol=ticker,
            bid=Decimal(bid),
            ask=Decimal(ask),
            last=Decimal(last),
            last_regular=Decimal(last),
            volume=100_000,
            last_trade_time=at - timedelta(seconds=age),
            delay=0,
            is_halted=False,
            vwap=None,
        )

    def add_bars(self, qt_id: int, interval: Interval, candles: Sequence[Candle]) -> None:
        self.bars.setdefault((qt_id, interval), []).extend(candles)

    def _candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]:
        if symbol_id in self.errors:
            raise QuestradeApiError(self.errors[symbol_id], "fake error")
        found = [c for c in self.bars.get((symbol_id, interval), []) if start <= c.start < end]
        return sorted(found, key=lambda c: c.start)

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        self.calls.append(("symbols_by_names", len(names)))
        return {n: self.symbols[n] for n in names if n in self.symbols}

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        self.calls.append(("quotes", len(ids)))
        return [self.quote_map[i] for i in ids if i in self.quote_map]

    async def candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]:
        self.calls.append(("candles", 1))
        return self._candles(symbol_id, start, end, interval)

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        self.calls.append(("candles_many", len(reqs)))
        out: dict[CandleRequest, list[Candle] | QuestradeApiError] = {}
        for r in reqs:
            try:
                out[r] = self._candles(r.symbol_id, r.start, r.end, r.interval)
            except QuestradeApiError as exc:
                out[r] = exc
        return out
```

`Trader/app/tests/market/test_data_service.py`:
```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.db import models as m
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle, UniverseStatus

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
PREV = date(2026, 10, 5)
OPEN = CAL.session_open(DAY)  # 13:30Z
AFTER_BAR = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)


def c5(start: datetime, volume: int = 5000, close: str = "21.40") -> Candle:
    return Candle(start, start + timedelta(minutes=5), Decimal("21.00"), Decimal("21.50"), Decimal("20.90"),
                  Decimal(close), volume, None)


@pytest.fixture
def ids(db_factory: sessionmaker[Session]) -> dict[str, int]:
    """AAA/BBB/CCC have Questrade ids 101-103; DDD has none."""
    out: dict[str, int] = {}
    with db_factory() as s:
        for i, t in enumerate(["AAA", "BBB", "CCC"]):
            out[t] = add_symbol(s, t, questrade_id=101 + i)
        out["DDD"] = add_symbol(s, "DDD")
        for sid in out.values():
            s.add(m.UniverseSnapshot(session_date=DAY, symbol_id=sid, price=Decimal("20"), avg_volume=2_000_000,
                                     atr14=Decimal("1.0000"), source="finviz"))
            s.add(m.OpenBarStat(symbol_id=sid, session_date=DAY, avg_open_vol_14d=Decimal("1000.00"),
                                atr14=Decimal("1.0000")))
        s.commit()
    return out


def service(factory: sessionmaker[Session], qt: FakeQuestrade, now: datetime = AFTER_BAR) -> MarketDataService:
    return MarketDataService(factory, FixedClock(now), CAL, qt)


async def test_universe_and_stats_come_from_the_cache(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    qt = FakeQuestrade()
    svc = service(db_factory, qt)
    members = await svc.universe(DAY)
    assert [x.ticker for x in members] == ["AAA", "BBB", "CCC", "DDD"]
    assert members[0].atr14 == Decimal("1.0000") and members[0].source == "finviz"
    stats = await svc.open_bar_stats(DAY)
    assert stats[ids["AAA"]].avg_open_vol_14d == Decimal("1000.00")
    assert await svc.universe(date(2026, 10, 7)) == []
    assert await svc.symbol_ids(["AAA", "ZZZ"]) == {"AAA": ids["AAA"]}
    assert qt.calls == []


async def test_universe_status_reads_the_nightly_detail(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    svc = service(db_factory, FakeQuestrade())
    assert await svc.universe_status(DAY) == UniverseStatus("finviz", None, False, None)
    with db_factory() as s:
        s.add(m.JobRun(job="nightly", session_date=DAY, started_at=OPEN, finished_at=OPEN, status="succeeded",
                       error=None, detail={"source": "fallback", "fallback_from": "2026-09-30",
                                           "fallback_stale": True, "fallback_age_sessions": 4}))
        s.commit()
    assert await svc.universe_status(DAY) == UniverseStatus("fallback", date(2026, 9, 30), True, 4)
    assert await svc.universe_status(date(2026, 10, 7)) == UniverseStatus(None, None, False, None)


async def test_opening_bars_use_the_cache_first(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5(OPEN)])
        s.commit()
    qt = FakeQuestrade()
    got = await service(db_factory, qt).opening_bars(DAY, [ids["AAA"]])
    assert got.bars[ids["AAA"]].volume == 5000 and got.missing == {}
    assert qt.calls == []


async def test_opening_bars_fetch_the_rest_in_one_batch_and_cache_them(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    with db_factory() as s:
        repo.upsert_intraday_candles(s, ids["AAA"], "5m", [c5(OPEN)])
        s.commit()
    qt = FakeQuestrade()
    qt.add_bars(102, "FiveMinutes", [c5(OPEN, volume=7000), c5(OPEN + timedelta(minutes=5))])
    got = await service(db_factory, qt).opening_bars(DAY)  # the whole universe
    assert set(got.bars) == {ids["AAA"], ids["BBB"]}
    assert got.bars[ids["BBB"]].volume == 7000
    assert got.missing == {ids["CCC"]: "no_bar_at_open", ids["DDD"]: "no_questrade_id"}
    assert qt.calls == [("candles_many", 2)]  # BBB and CCC only, in one batch
    with db_factory() as s:
        cached = s.execute(
            select(m.IntradayCandle.volume).where(m.IntradayCandle.symbol_id == ids["BBB"])
        ).scalars().all()
    assert cached == [7000]


async def test_opening_bars_report_api_errors_per_symbol(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    """Review Focus 4: a failing symbol is reported with a reason, never raised."""
    qt = FakeQuestrade()
    qt.errors[103] = 500
    qt.add_bars(101, "FiveMinutes", [c5(OPEN)])
    got = await service(db_factory, qt).opening_bars(DAY, [ids["AAA"], ids["CCC"]])
    assert set(got.bars) == {ids["AAA"]}
    assert got.missing == {ids["CCC"]: "questrade_error: HTTP 500"}


async def test_an_incomplete_opening_bar_is_neither_used_nor_cached(
    db_factory: sessionmaker[Session], ids: dict[str, int]
) -> None:
    qt = FakeQuestrade()
    qt.add_bars(101, "FiveMinutes", [c5(OPEN)])
    early = datetime(2026, 10, 6, 13, 34, 0, tzinfo=UTC)
    got = await service(db_factory, qt, now=early).opening_bars(DAY, [ids["AAA"]])
    assert got.bars == {} and got.missing == {ids["AAA"]: "bar_not_complete"}
    with db_factory() as s:
        assert s.execute(select(m.IntradayCandle)).first() is None


async def test_quotes_are_keyed_by_database_id(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    qt = FakeQuestrade()
    qt.add_symbol("AAA", 101)
    qt.set_quote(101, "21.00", "21.02", "21.01", AFTER_BAR)
    got = await service(db_factory, qt).quotes([ids["AAA"], ids["DDD"]])
    assert list(got) == [ids["AAA"]]
    assert got[ids["AAA"]].symbol_id == ids["AAA"] and got[ids["AAA"]].ask == Decimal("21.02")
    assert qt.calls == [("quotes", 1)]


async def test_prior_close_from_the_cache_then_questrade(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    with db_factory() as s:
        s.add(m.DailyCandle(symbol_id=ids["AAA"], date=PREV, open=Decimal("20"), high=Decimal("21"),
                            low=Decimal("19"), close=Decimal("20.50"), volume=1, vwap=None))
        s.commit()
    qt = FakeQuestrade()
    prev_start = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 00:00 ET
    qt.add_bars(102, "OneDay", [Candle(prev_start, prev_start + timedelta(days=1), Decimal("30"), Decimal("31"),
                                       Decimal("29"), Decimal("30.25"), 1, None)])
    svc = service(db_factory, qt)
    assert await svc.prior_close(ids["AAA"], DAY) == Decimal("20.50")
    assert qt.calls == []
    assert await svc.prior_close(ids["BBB"], DAY) == Decimal("30.25")
    assert await svc.prior_close(ids["CCC"], DAY) is None
    assert await svc.prior_closes([ids["AAA"], ids["BBB"], ids["CCC"]], DAY) == {
        ids["AAA"]: Decimal("20.50"),
        ids["BBB"]: Decimal("30.25"),  # cached by the fallback above
    }


async def test_candles_come_from_the_cache_when_complete(db_factory: sessionmaker[Session], ids: dict[str, int]) -> None:
    qt = FakeQuestrade()
    bars = [c5(OPEN + timedelta(minutes=5 * i)) for i in range(3)]
    qt.add_bars(101, "FiveMinutes", bars)
    svc = service(db_factory, qt)
    end = OPEN + timedelta(minutes=15)
    first = await svc.candles(ids["AAA"], OPEN, end, "FiveMinutes")
    second = await svc.candles(ids["AAA"], OPEN, end, "FiveMinutes")
    assert [c.start for c in first] == [c.start for c in second] == [b.start for b in bars]
    assert qt.calls == [("candles", 1)]  # the second read was served from the cache
```

`Trader/app/tests/market/__init__.py` already exists (P1-T4).

- [ ] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/market/test_data_service.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.market.data_service'`.

- [ ] **Step 3: Implement `trader/market/data_service.py`**

```python
"""Market data for strategies and jobs (master plan §7.1): the DB cache first, Questrade second.

Every symbol_id here is trader.symbols.id. Questrade IDs stay at the client boundary: quotes() rewrites
QtQuote.symbol_id to the database ID. Missing data is reported per symbol, never raised (Review Focus 4).
"""

import dataclasses
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.db import models as m
from trader.db.session import session_scope
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.market.indicators import opening_bar
from trader.market.types import (
    INTERVAL_CODES,
    Candle,
    Interval,
    OpenBarStats,
    OpeningBars,
    UniverseMember,
    UniverseStatus,
)

OPENING_BAR = timedelta(minutes=5)
STEP: dict[Interval, timedelta] = {
    "OneMinute": timedelta(minutes=1),
    "FiveMinutes": timedelta(minutes=5),
    "FifteenMinutes": timedelta(minutes=15),
    "OneHour": timedelta(hours=1),
    "OneDay": timedelta(days=1),
}


class QuoteClient(Protocol):
    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]: ...
    async def candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]: ...
    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]: ...


def _from_row(row: m.IntradayCandle, step: timedelta) -> Candle:
    return Candle(row.ts, row.ts + step, row.open, row.high, row.low, row.close, row.volume, row.vwap)


class MarketDataService:
    def __init__(
        self, factory: sessionmaker[Session], clock: Clock, calendar: SessionCalendar, client: QuoteClient
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cal = calendar
        self._client = client

    # --- cache-only reads ---------------------------------------------------------------------------------
    async def universe(self, session_date: date) -> list[UniverseMember]:
        with self._factory() as s:
            rows = s.execute(
                select(m.UniverseSnapshot, m.Symbol)
                .join(m.Symbol, m.Symbol.id == m.UniverseSnapshot.symbol_id)
                .where(m.UniverseSnapshot.session_date == session_date)
                .order_by(m.Symbol.ticker)
            ).all()
        return [
            UniverseMember(sym.id, sym.ticker, sym.name, snap.price, snap.avg_volume, snap.atr14, snap.source)
            for snap, sym in rows
        ]

    async def universe_status(self, session_date: date) -> UniverseStatus:
        with self._factory() as s:
            detail = s.execute(
                select(m.JobRun.detail)
                .where(m.JobRun.job == "nightly", m.JobRun.session_date == session_date, m.JobRun.status == "succeeded")
                .order_by(m.JobRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            source = s.execute(
                select(m.UniverseSnapshot.source).where(m.UniverseSnapshot.session_date == session_date).limit(1)
            ).scalar_one_or_none()
        if source is None:
            return UniverseStatus(None, None, False, None)
        if not isinstance(detail, dict):
            return UniverseStatus(source, None, False, None)
        fallback_from = detail.get("fallback_from")
        age = detail.get("fallback_age_sessions")
        return UniverseStatus(
            source=str(detail.get("source", source)),
            fallback_from=date.fromisoformat(fallback_from) if fallback_from else None,
            stale=bool(detail.get("fallback_stale", False)),
            age_sessions=int(age) if age is not None else None,
        )

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        with self._factory() as s:
            rows = s.execute(select(m.OpenBarStat).where(m.OpenBarStat.session_date == session_date)).scalars()
            return {r.symbol_id: OpenBarStats(r.symbol_id, r.avg_open_vol_14d, r.atr14) for r in rows}

    async def symbol_ids(self, tickers: Sequence[str]) -> dict[str, int]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Symbol.ticker, m.Symbol.id).where(m.Symbol.ticker.in_(list(tickers))).order_by(m.Symbol.id)
            ).all()
        out: dict[str, int] = {}
        for ticker, sid in rows:
            out.setdefault(ticker, sid)
        return out

    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]:
        prev = self._cal.previous_session(session_date)
        with self._factory() as s:
            rows = s.execute(
                select(m.DailyCandle.symbol_id, m.DailyCandle.close).where(
                    m.DailyCandle.date == prev, m.DailyCandle.symbol_id.in_(list(symbol_ids))
                )
            ).all()
        return {sid: close for sid, close in rows}

    def _questrade_ids(self, symbol_ids: Sequence[int]) -> dict[int, int]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Symbol.id, m.Symbol.questrade_id).where(
                    m.Symbol.id.in_(list(symbol_ids)), m.Symbol.questrade_id.is_not(None)
                )
            ).all()
        return {sid: int(qid) for sid, qid in rows}

    # --- live or fetched reads ----------------------------------------------------------------------------
    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]:
        qids = self._questrade_ids(symbol_ids)
        if not qids:
            return {}
        back = {qid: sid for sid, qid in qids.items()}
        out: dict[int, QtQuote] = {}
        for q in await self._client.quotes(list(qids.values())):
            if q.symbol_id in back:
                sid = back[q.symbol_id]
                out[sid] = dataclasses.replace(q, symbol_id=sid)
        return out

    async def opening_bars(self, session_date: date, symbol_ids: Sequence[int] | None = None) -> OpeningBars:
        if symbol_ids is None:
            symbol_ids = [x.symbol_id for x in await self.universe(session_date)]
        ids = list(dict.fromkeys(symbol_ids))
        open_ = self._cal.session_open(session_date)
        with self._factory() as s:
            cached = s.execute(
                select(m.IntradayCandle).where(
                    m.IntradayCandle.interval == "5m",
                    m.IntradayCandle.ts == open_,
                    m.IntradayCandle.symbol_id.in_(ids),
                )
            ).scalars()
            bars: dict[int, Candle] = {r.symbol_id: _from_row(r, OPENING_BAR) for r in cached}
        missing: dict[int, str] = {}
        need = [sid for sid in ids if sid not in bars]
        qids = self._questrade_ids(need)
        for sid in need:
            if sid not in qids:
                missing[sid] = "no_questrade_id"
        reqs = {sid: CandleRequest(qids[sid], open_, open_ + OPENING_BAR, "FiveMinutes") for sid in need if sid in qids}
        results = await self._client.candles_many(list(reqs.values())) if reqs else {}
        now = self._clock.now()
        fetched: dict[int, Candle] = {}
        for sid, req in reqs.items():
            result = results[req]
            if isinstance(result, QuestradeApiError):
                missing[sid] = f"questrade_error: HTTP {result.status}"
                continue
            found = opening_bar(result, self._cal, session_date)
            if found is None:
                missing[sid] = "no_bar_at_open"
            elif found.end > now:
                missing[sid] = "bar_not_complete"
            else:
                fetched[sid] = found
        if fetched:
            with session_scope(self._factory) as s:
                for sid, found in fetched.items():
                    repo.upsert_intraday_candles(s, sid, "5m", [found])
        bars.update(fetched)
        return OpeningBars(bars, missing)

    async def candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]:
        qids = self._questrade_ids([symbol_id])
        step = STEP[interval]
        if interval != "OneDay":
            code = INTERVAL_CODES[interval]
            expected = int((end - start) / step)
            with self._factory() as s:
                rows = s.execute(
                    select(m.IntradayCandle)
                    .where(
                        m.IntradayCandle.symbol_id == symbol_id,
                        m.IntradayCandle.interval == code,
                        m.IntradayCandle.ts >= start,
                        m.IntradayCandle.ts < end,
                    )
                    .order_by(m.IntradayCandle.ts)
                ).scalars()
                cached = [_from_row(r, step) for r in rows]
            if expected > 0 and len(cached) >= expected:
                return cached
        if symbol_id not in qids:
            return []
        fetched = await self._client.candles(qids[symbol_id], start, end, interval)
        if interval != "OneDay" and fetched:
            with session_scope(self._factory) as s:
                repo.upsert_intraday_candles(s, symbol_id, INTERVAL_CODES[interval], fetched)
        return fetched

    async def prior_close(self, symbol_id: int, session_date: date) -> Decimal | None:
        prev = self._cal.previous_session(session_date)
        with self._factory() as s:
            close = s.execute(
                select(m.DailyCandle.close).where(m.DailyCandle.symbol_id == symbol_id, m.DailyCandle.date == prev)
            ).scalar_one_or_none()
        if close is not None:
            return close
        qids = self._questrade_ids([symbol_id])
        if symbol_id not in qids:
            return None
        start = datetime.combine(prev, time(0), tzinfo=ET)
        end = datetime.combine(session_date, time(0), tzinfo=ET)
        try:
            daily = await self._client.candles(qids[symbol_id], start, end, "OneDay")
        except QuestradeApiError:
            return None
        bars = [c for c in daily if et_date(c.start) == prev]
        if not bars:
            return None
        with session_scope(self._factory) as s:
            repo.upsert_daily_candles(s, symbol_id, bars[-1:])
        return bars[-1].close
```

- [ ] **Step 4: Run the tests**

Run: `uv --directory Trader/app run pytest tests/market/test_data_service.py -q`
Expected: all pass.

- [ ] **Step 5: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/market/data_service.py Trader/app/tests/fakes_questrade.py Trader/app/tests/market/test_data_service.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T7: market data service (cache first, Questrade second, per-symbol missing reasons)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T8: `orb_sip` plug-in 1.0.0

**Files:**
- Create: `Trader/app/trader/strategies/orb_sip.py`, `Trader/app/tests/strategies/scenarios/__init__.py`, `Trader/app/tests/strategies/scenarios/test_orb_sip_scenarios.py`

**Interfaces:**
- Consumes: `SessionOffset`, `ScheduledEvent`, `EnterLong`, `Exit`, `Cancel`, `CandidateRecord`, `CatalystInfo`, `StrategyContext` (P2-T6); `rvol`, `is_doji`, `is_bearish` (P1-T8: `rvol` returns `None` without a usable average; the candle checks raise `ValueError` when `high < low`); `UniverseStatus` (P2-T6/T7); `Q4`, `Fill`, `OrderSpec`, `FillEvent` (P2-T4); `QuoteFillModel`, `FillParams` (P2-T4, scenario harness); `load_plugin` (P2-T6); `tests/strategies/fakes.py` (P2-T6).
- Produces (`trader.strategies.orb_sip`):
  - Event keys `ORB_EVENT = "orb_open"` at `ORB_AT = "open+5m5s"` (9:35:05 ET), `CANCEL_EVENT = "entry_cancel"` at `entry_cancel_at` (omitted when `None`), `FLATTEN_EVENT = "flatten"` at `exit_at`. The cron backups in P3 call `trader event orb_open` and `trader event flatten`.
  - `OrbSipParams` (frozen, `extra="forbid"`; SPEC §5.2 defaults): `price_min=5`, `price_max=50` (min < max), `min_avg_volume=1_000_000`, `min_atr=0.50`, `rvol_min=1.00`, `top_n=20`, `max_positions=1`, `require_catalyst=True`, `catalyst_min_quality=50`, `stop_atr_fraction=0.10`, `entry_offset=0.01`, `entry_cancel_at="open+120m"` (or `None`), `exit_at="close-10m"`, `doji_body_pct_max=0.10`, and `stale_universe: Literal["skip","trade"] = "skip"` (P1-T9 ruling: the SPEC is silent, so a stale fallback universe skips entries by default).
  - `OrbSip(params: OrbSipParams | None = None)`: `key="orb_sip"`, `version="1.0.0"`, `kind="entry"`.
  - `orb_open`: no entry when `entries_today >= max_positions` (a note says why) or when the universe is a stale fallback and `stale_universe == "skip"` (an error note). Otherwise: opening bars for the universe; `rvol = bar.volume / avg_open_vol_14d`; keep `rvol >= rvol_min`, sort by rvol descending (ticker breaks ties), take `top_n` and record each as a `CandidateRecord` with rank 1..n; reject reasons in this order: `malformed_bar`, `bearish_candle`, `doji`, `price_out_of_range` (on the bar's close), `atr_missing`, `atr_below_min`, `avg_volume_below_min`; then one `ctx.catalysts.get(...)` call for the survivors; `catalyst_missing` (none, unclassified, `none` or `unknown` type), `catalyst_bearish`, `catalyst_low_quality`; survivors beyond the free slots get `lower_rank`. Each selected name emits `EnterLong(symbol_id, "stop", stop=bar.high + entry_offset, limit=None, stop_loss=entry − stop_atr_fraction × ATR14, reason="orb_breakout", evidence=...)`. Evidence and candidate `data` carry `ticker, rvol, rank, direction, atr14, price, avg_volume, avg_open_vol_14d, catalyst`, plus `entry`, `stop_loss` and `candle` for the selected name (BR-13; the engine adds the size). Symbols without an opening bar or without a volume baseline are listed in notes (Review Focus 4), not ranked.
  - `entry_cancel`: `Cancel(order_id, "entry_cancel_at")` for each working entry order. `flatten`: `Cancel(..., "flatten_close")` for each working entry, then `Exit(position_id, "market", None, "flatten_close")` for each open position.
  - `on_fill`: an entry fill with a `stop_loss` gives `Exit(position_id, "stop", stop_loss, "protective_stop")`; any other fill gives `[]`.

- [ ] **Step 1: Write the failing scenario tests**

`Trader/app/tests/strategies/scenarios/__init__.py`: empty file.

`Trader/app/tests/strategies/scenarios/test_orb_sip_scenarios.py`:
```python
"""Hand-built ORB scenarios (SPEC §16): rankings, rejects, and fills through the real quote fill model."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.strategies.fakes import (
    CAL,
    NOW,
    SESSION,
    FakeCatalyst,
    FakeCatalysts,
    FakeData,
    bar,
    make_ctx,
    position,
    quote,
    working_entry,
)
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.types import FillDecision, FillEvent, OrderSpec
from trader.market.types import UniverseStatus
from trader.strategies.base import Cancel, EnterLong, Exit, ScheduledEvent
from trader.strategies.orb_sip import CANCEL_EVENT, FLATTEN_EVENT, ORB_EVENT, OrbSip, OrbSipParams
from trader.strategies.registry import load_plugin

MODEL = QuoteFillModel(FillParams())
AAA, BBB, CCC, DDD, EEE = 1, 2, 3, 4, 5
BULL = bar("21.00", "21.50", "20.90", "21.40", 5000)  # rvol 5 against an average of 1000


def orb_event(strategy: OrbSip) -> ScheduledEvent:
    return next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)


def event(strategy: OrbSip, key: str) -> ScheduledEvent:
    return next(e for e in strategy.schedule(CAL) if e.key == key)


def standard() -> tuple[FakeData, FakeCatalysts]:
    data = FakeData()
    data.add(AAA, "AAA", BULL)
    data.add(BBB, "BBB", bar("30.00", "30.60", "29.90", "30.50", 3000))
    data.add(CCC, "CCC", bar("10.00", "10.40", "9.95", "10.30", 800))  # rvol 0.8: not ranked
    cats = FakeCatalysts({AAA: FakeCatalyst(), BBB: FakeCatalyst()})
    return data, cats


async def run_orb(data: FakeData, cats: FakeCatalysts, params: OrbSipParams | None = None, **ctx_kw: object):  # type: ignore[no-untyped-def]
    strategy = OrbSip(params)
    ctx = make_ctx(data, strategy.params, cats, **ctx_kw)  # type: ignore[arg-type]
    intents = await strategy.on_event(ctx, orb_event(strategy))
    return strategy, ctx, intents


def first_fill(spec: OrderSpec, quotes: Sequence[QtQuote]) -> FillDecision | None:
    for q in quotes:
        d = MODEL.evaluate(spec, q, q.last_trade_time + timedelta(seconds=1))  # type: ignore[operator]
        if d is not None:
            return d
    return None


def entry_spec(intent: EnterLong, qty: int = 10) -> OrderSpec:
    return OrderSpec(intent.symbol_id, "buy", intent.order_type, qty, stop=intent.stop, stop_loss=intent.stop_loss)


def as_event(spec: OrderSpec, d: FillDecision, position_id: int = 1) -> FillEvent:
    return FillEvent(1, 1, 1, spec.symbol_id, spec.side, spec.purpose, d.qty, d.price, NOW, position_id, 1,
                     spec.stop_loss, None)


async def test_breakout_fills_and_places_a_protective_stop() -> None:
    data, cats = standard()
    strategy, ctx, intents = await run_orb(data, cats)
    (e,) = intents
    assert isinstance(e, EnterLong) and e.symbol_id == AAA and e.order_type == "stop"
    assert e.stop == Decimal("21.5100") and e.stop_loss == Decimal("21.4100")  # 21.51 - 0.10 x 1.00
    # BR-13: the rule values behind the signal
    assert e.evidence["rvol"] == "5.0000" and e.evidence["direction"] == "bullish"
    assert e.evidence["atr14"] == "1.00" and e.evidence["entry"] == "21.5100"
    assert e.evidence["stop_loss"] == "21.4100" and e.evidence["candle"]["high"] == "21.50"
    spec = entry_spec(e)
    at = NOW + timedelta(minutes=1)
    assert first_fill(spec, [quote(AAA, "21.43", "21.45", "21.44", at)]) is None  # no breakout yet
    d = first_fill(spec, [quote(AAA, "21.52", "21.55", "21.53", at)])
    assert d is not None and d.price == Decimal("21.5608")  # max(21.51, 21.55) + 0.0108
    (stop,) = await strategy.on_fill(ctx, as_event(spec, d))
    assert stop == Exit(1, "stop", Decimal("21.4100"), "protective_stop")


async def test_no_breakout_is_cancelled_at_the_entry_cancel_time() -> None:
    data, cats = standard()
    strategy, ctx, (e,) = await run_orb(data, cats)
    spec = entry_spec(e)
    quotes = [quote(AAA, "21.40", "21.42", "21.41", NOW + timedelta(minutes=i)) for i in range(1, 60)]
    assert first_fill(spec, quotes) is None
    cancel_event = event(strategy, CANCEL_EVENT)
    assert cancel_event.at.resolve(CAL, SESSION) == datetime(2026, 10, 6, 15, 30, tzinfo=UTC)  # 11:30 ET
    later = make_ctx(data, strategy.params, cats, orders=[working_entry(77, AAA)])
    assert await strategy.on_event(later, cancel_event) == [Cancel(77, "entry_cancel_at")]


async def test_stop_hit_after_the_entry() -> None:
    data, cats = standard()
    strategy, ctx, (e,) = await run_orb(data, cats)
    d = first_fill(entry_spec(e), [quote(AAA, "21.52", "21.55", "21.53")])
    assert d is not None
    (stop,) = await strategy.on_fill(ctx, as_event(entry_spec(e), d))
    assert isinstance(stop, Exit) and stop.stop is not None
    stop_spec = OrderSpec(AAA, "sell", "stop", 10, stop=stop.stop, purpose="stop", position_id=1)
    assert first_fill(stop_spec, [quote(AAA, "21.45", "21.46", "21.45")]) is None
    hit = first_fill(stop_spec, [quote(AAA, "21.38", "21.40", "21.39")])
    assert hit is not None and hit.price == Decimal("21.3693")  # min(21.41, 21.38) - 0.0107


@pytest.mark.parametrize(
    ("aaa_bar", "reason"),
    [
        (bar("21.00", "21.50", "20.90", "21.02", 5000), "doji"),  # body 0.02 / range 0.60
        (bar("21.40", "21.50", "20.90", "21.00", 5000), "bearish_candle"),
        (bar("21.00", "20.50", "20.90", "21.40", 5000), "malformed_bar"),  # high < low
    ],
)
async def test_bad_opening_candles_are_skipped(aaa_bar: object, reason: str) -> None:
    data, cats = standard()
    data.bars[AAA] = aaa_bar  # type: ignore[assignment]
    _, ctx, (e,) = await run_orb(data, cats)
    assert e.symbol_id == BBB  # the next-ranked name is chosen instead
    rejected = {c.symbol_id: c.reject_reason for c in ctx.candidates}
    assert rejected[AAA] == reason


async def test_price_atr_and_volume_limits() -> None:
    data = FakeData()
    data.add(AAA, "AAA", bar("54.00", "55.50", "53.90", "55.40", 5000))
    data.add(BBB, "BBB", bar("30.00", "30.60", "29.90", "30.50", 4000), atr="0.40")
    data.add(CCC, "CCC", bar("10.00", "10.40", "9.95", "10.30", 3000), avg_volume=500_000)
    data.add(DDD, "DDD", bar("12.00", "12.40", "11.95", "12.30", 2000), atr=None)
    cats = FakeCatalysts({i: FakeCatalyst() for i in (AAA, BBB, CCC, DDD)})
    _, ctx, intents = await run_orb(data, cats)
    assert intents == []
    assert {c.symbol_id: c.reject_reason for c in ctx.candidates} == {
        AAA: "price_out_of_range",
        BBB: "atr_below_min",
        CCC: "avg_volume_below_min",
        DDD: "atr_missing",
    }
    assert cats.requested == []  # nothing survived the screen, so no catalyst lookups


async def test_catalyst_missing_bearish_or_weak_is_rejected() -> None:
    data = FakeData()
    for sid, t in ((AAA, "AAA"), (BBB, "BBB"), (DDD, "DDD"), (EEE, "EEE")):
        data.add(sid, t, bar("21.00", "21.50", "20.90", "21.40", 6000 - sid * 100))
    cats = FakeCatalysts({
        BBB: FakeCatalyst(direction="bearish"),
        DDD: FakeCatalyst(quality=30),
        EEE: FakeCatalyst(catalyst_type="unknown", quality=None, classified=False),
    })
    _, ctx, intents = await run_orb(data, cats)
    assert intents == []
    assert {c.symbol_id: c.reject_reason for c in ctx.candidates} == {
        AAA: "catalyst_missing",
        BBB: "catalyst_bearish",
        DDD: "catalyst_low_quality",
        EEE: "catalyst_missing",
    }
    _, _, relaxed = await run_orb(data, cats, OrbSipParams(require_catalyst=False))
    assert [i.symbol_id for i in relaxed] == [AAA]  # type: ignore[union-attr]


async def test_only_screen_survivors_are_sent_for_catalysts() -> None:
    data, cats = standard()
    data.bars[AAA] = bar("21.40", "21.50", "20.90", "21.00", 5000)  # bearish
    await run_orb(data, cats)
    assert cats.requested == [[BBB]]


async def test_every_ranked_candidate_is_saved_with_its_reason() -> None:
    data, cats = standard()
    data.add(DDD, "DDD", bar("15.00", "15.40", "14.95", "15.30", 2000))
    cats.by_symbol[DDD] = FakeCatalyst()
    _, ctx, (e,) = await run_orb(data, cats, OrbSipParams(top_n=2))
    assert e.symbol_id == AAA
    assert [(c.symbol_id, c.rank, c.passed, c.reject_reason) for c in ctx.candidates] == [
        (AAA, 1, True, None),
        (BBB, 2, False, "lower_rank"),
    ]
    assert ctx.candidates[0].rvol == Decimal("5.0000") and ctx.candidates[0].candle is not None


async def test_missing_bars_and_baselines_are_noted_not_raised() -> None:
    """Review Focus 4: no 9:30 bar or no volume history skips the symbol with a recorded reason."""
    data, cats = standard()
    data.add(DDD, "DDD", None)
    data.add(EEE, "EEE", bar("15.00", "15.40", "14.95", "15.30", 9000), avg_open_vol=None)
    _, ctx, (e,) = await run_orb(data, cats)
    assert e.symbol_id == AAA  # EEE would rank first on volume but has no baseline
    missing = next(n for n in ctx.notes if "no opening bar" in n.message)
    assert missing.data["missing"] == {str(DDD): "no_bar_at_open"} and missing.level == "warning"
    baseline = next(n for n in ctx.notes if "baseline" in n.message)
    assert baseline.data["symbol_ids"] == [EEE]
    assert all(c.symbol_id not in (DDD, EEE) for c in ctx.candidates)


async def test_no_new_entry_once_max_positions_is_used() -> None:
    data, cats = standard()
    _, ctx, intents = await run_orb(data, cats, entries_today=1)
    assert intents == [] and ctx.candidates == []
    assert "max_positions" in ctx.notes[0].message


async def test_a_stale_fallback_universe_skips_entries_by_default() -> None:
    data, cats = standard()
    data.status = UniverseStatus("fallback", date(2026, 9, 30), True, 4)
    _, ctx, intents = await run_orb(data, cats)
    assert intents == [] and ctx.notes[0].level == "error" and "stale" in ctx.notes[0].message
    _, ctx2, traded = await run_orb(data, cats, OrbSipParams(stale_universe="trade"))
    assert [i.symbol_id for i in traded] == [AAA]  # type: ignore[union-attr]
    assert any(n.level == "warning" and "fallback" in n.message for n in ctx2.notes)


def test_an_early_close_moves_the_flatten() -> None:
    s = OrbSip()
    day = date(2026, 11, 27)  # 13:00 ET close; EST
    times = {e.key: e.at.resolve(CAL, day) for e in s.schedule(CAL)}
    assert times == {
        ORB_EVENT: datetime(2026, 11, 27, 14, 35, 5, tzinfo=UTC),
        CANCEL_EVENT: datetime(2026, 11, 27, 16, 30, tzinfo=UTC),
        FLATTEN_EVENT: datetime(2026, 11, 27, 17, 50, tzinfo=UTC),  # 12:50 ET
    }
    assert {e.key for e in OrbSip(OrbSipParams(entry_cancel_at=None)).schedule(CAL)} == {ORB_EVENT, FLATTEN_EVENT}


async def test_flatten_exits_positions_and_cancels_working_entries() -> None:
    data, cats = standard()
    s = OrbSip()
    ctx = make_ctx(data, s.params, cats, positions=[position(5, AAA)], orders=[working_entry(9, BBB)])
    assert await s.on_event(ctx, event(s, FLATTEN_EVENT)) == [
        Cancel(9, "flatten_close"),
        Exit(5, "market", None, "flatten_close"),
    ]


async def test_non_entry_fills_need_nothing() -> None:
    data, cats = standard()
    s = OrbSip()
    ctx = make_ctx(data, s.params, cats)
    stop_fill = FillEvent(2, 2, 1, AAA, "sell", "stop", 10, Decimal("21.37"), NOW, 1, 1, Decimal("21.41"), None, 3)
    assert await s.on_fill(ctx, stop_fill) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"price_min": "50", "price_max": "5"},
        {"exit_at": "close-10"},
        {"entry_cancel_at": "noon"},
        {"top_n": 0},
        {"surprise": 1},
        {"stale_universe": "maybe"},
    ],
)
def test_invalid_params_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OrbSipParams.model_validate(bad)


def test_the_plugin_loads_through_its_entry_point() -> None:
    cls = load_plugin("orb_sip")
    assert cls is OrbSip and cls.version == "1.0.0" and cls.kind == "entry"
```

- [ ] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/strategies/scenarios -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.strategies.orb_sip'`.

- [ ] **Step 3: Implement `trader/strategies/orb_sip.py`**

```python
"""orb_sip 1.0.0: the 5-minute Opening Range Breakout on Stocks in Play (SPEC §5.2, BR-04, BR-11, BR-13)."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.broker.types import Q4, Fill
from trader.market.calendar import SessionCalendar
from trader.market.indicators import is_bearish, is_doji, rvol
from trader.market.types import Candle, UniverseMember
from trader.strategies.base import (
    Cancel,
    CandidateRecord,
    CatalystInfo,
    EnterLong,
    Exit,
    Intent,
    ScheduledEvent,
    SessionOffset,
    StrategyContext,
)

ORB_EVENT = "orb_open"
CANCEL_EVENT = "entry_cancel"
FLATTEN_EVENT = "flatten"
ORB_AT = "open+5m5s"  # 9:35:05 ET: five seconds after the opening bar closes
NO_CATALYST = frozenset({"none", "unknown"})


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def _candle_json(c: Candle) -> dict[str, Any]:
    return {
        "start": c.start.isoformat(),
        "open": str(c.open),
        "high": str(c.high),
        "low": str(c.low),
        "close": str(c.close),
        "volume": c.volume,
    }


def _catalyst_json(cat: CatalystInfo | None) -> dict[str, Any] | None:
    if cat is None:
        return None
    return {
        "type": cat.catalyst_type,
        "direction": cat.direction,
        "quality": cat.quality,
        "classified": cat.classified,
    }


class OrbSipParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    price_min: Decimal = Field(Decimal("5"), gt=0, allow_inf_nan=False)
    price_max: Decimal = Field(Decimal("50"), gt=0, allow_inf_nan=False)
    min_avg_volume: int = Field(1_000_000, ge=0)
    min_atr: Decimal = Field(Decimal("0.50"), ge=0, allow_inf_nan=False)
    rvol_min: Decimal = Field(Decimal("1.00"), ge=0, allow_inf_nan=False)
    top_n: int = Field(20, ge=1, le=200)
    max_positions: int = Field(1, ge=1, le=10)
    require_catalyst: bool = True
    catalyst_min_quality: int = Field(50, ge=0, le=100)
    stop_atr_fraction: Decimal = Field(Decimal("0.10"), gt=0, le=Decimal("5"), allow_inf_nan=False)
    entry_offset: Decimal = Field(Decimal("0.01"), ge=0, le=Decimal("5"), allow_inf_nan=False)
    entry_cancel_at: str | None = "open+120m"
    exit_at: str = "close-10m"
    doji_body_pct_max: Decimal = Field(Decimal("0.10"), ge=0, le=Decimal("1"), allow_inf_nan=False)
    stale_universe: Literal["skip", "trade"] = "skip"

    @field_validator("entry_cancel_at", "exit_at")
    @classmethod
    def _offset(cls, v: str | None) -> str | None:
        if v is not None:
            SessionOffset.parse(v)
        return v

    @model_validator(mode="after")
    def _price_band(self) -> Self:
        if self.price_min >= self.price_max:
            raise ValueError("price_min must be below price_max")
        return self


class OrbSip:
    key = "orb_sip"
    version = "1.0.0"
    kind: Literal["entry", "overlay"] = "entry"
    params_model = OrbSipParams

    def __init__(self, params: OrbSipParams | None = None) -> None:
        self.params = params or OrbSipParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        events = [ScheduledEvent(ORB_EVENT, SessionOffset.parse(ORB_AT))]
        if self.params.entry_cancel_at is not None:
            events.append(ScheduledEvent(CANCEL_EVENT, SessionOffset.parse(self.params.entry_cancel_at)))
        events.append(ScheduledEvent(FLATTEN_EVENT, SessionOffset.parse(self.params.exit_at)))
        return events

    async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
        if event.key == ORB_EVENT:
            return await self._orb(ctx)
        entries = [o for o in ctx.working_orders if o.purpose == "entry"]
        if event.key == CANCEL_EVENT:
            return [Cancel(o.id, "entry_cancel_at") for o in entries]
        if event.key == FLATTEN_EVENT:
            cancels: list[Intent] = [Cancel(o.id, "flatten_close") for o in entries]
            exits: list[Intent] = [Exit(p.id, "market", None, "flatten_close") for p in ctx.positions]
            return cancels + exits
        return []

    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
        if fill.purpose != "entry" or fill.stop_loss is None:
            return []
        return [Exit(fill.position_id, "stop", fill.stop_loss, "protective_stop")]

    # --- the 9:35 scan ----------------------------------------------------------------------------------
    async def _orb(self, ctx: StrategyContext) -> list[Intent]:
        p = self.params
        slots = p.max_positions - ctx.entries_today
        if slots <= 0:
            ctx.note("orb: max_positions already used today", entries_today=ctx.entries_today)
            return []
        status = await ctx.data.universe_status(ctx.session_date)
        if status.stale and p.stale_universe == "skip":
            ctx.note(
                "orb: the universe is a stale fallback; no entries today",
                level="error",
                fallback_from=str(status.fallback_from),
                age_sessions=status.age_sessions,
            )
            return []
        if status.source == "fallback":
            ctx.note(
                "orb: trading on a fallback universe",
                level="warning",
                fallback_from=str(status.fallback_from),
                stale=status.stale,
            )
        universe = await ctx.data.universe(ctx.session_date)
        if not universe:
            ctx.note("orb: no universe for this session", level="error")
            return []
        members = {u.symbol_id: u for u in universe}
        stats = await ctx.data.open_bar_stats(ctx.session_date)
        opening = await ctx.data.opening_bars(ctx.session_date, list(members))
        if opening.missing:
            ctx.note(
                f"orb: {len(opening.missing)} symbols have no opening bar",
                level="warning",
                missing={str(k): v for k, v in sorted(opening.missing.items())},
            )
        scored: list[tuple[Decimal, str, int, Candle]] = []
        no_baseline: list[int] = []
        for sid, candle in opening.bars.items():
            st = stats.get(sid)
            r = rvol(candle.volume, st.avg_open_vol_14d if st else None)
            if r is None:
                no_baseline.append(sid)
            elif r >= p.rvol_min:
                scored.append((r, members[sid].ticker, sid, candle))
        if no_baseline:
            ctx.note("orb: no opening-volume baseline", symbol_ids=sorted(no_baseline))
        scored.sort(key=lambda t: (-t[0], t[1]))

        records: list[CandidateRecord] = []
        survivors: list[CandidateRecord] = []
        for rank, (r, ticker, sid, candle) in enumerate(scored[: p.top_n], start=1):
            member = members[sid]
            st = stats.get(sid)
            atr14 = st.atr14 if st is not None and st.atr14 is not None else member.atr14
            rec = CandidateRecord(
                symbol_id=sid,
                rvol=r,
                rank=rank,
                candle=_candle_json(candle),
                data={
                    "ticker": ticker,
                    "rvol": str(r),
                    "rank": rank,
                    "direction": self._direction(candle),
                    "atr14": _s(atr14),
                    "price": str(candle.close),
                    "avg_volume": member.avg_volume,
                    "avg_open_vol_14d": _s(st.avg_open_vol_14d if st else None),
                },
            )
            rec.reject_reason = self._screen(candle, atr14, member)
            records.append(rec)
            if rec.reject_reason is None:
                survivors.append(rec)

        catalysts = await ctx.catalysts.get([x.symbol_id for x in survivors], ctx.session_date) if survivors else {}
        intents: list[Intent] = []
        for rec in survivors:
            cat = catalysts.get(rec.symbol_id)
            rec.data["catalyst"] = _catalyst_json(cat)
            reason = self._catalyst_reason(cat)
            if reason is None and len(intents) >= slots:
                reason = "lower_rank"
            if reason is not None:
                rec.reject_reason = reason
                continue
            candle = opening.bars[rec.symbol_id]
            atr14 = Decimal(str(rec.data["atr14"]))
            entry = (candle.high + p.entry_offset).quantize(Q4, ROUND_HALF_UP)
            stop_loss = (entry - p.stop_atr_fraction * atr14).quantize(Q4, ROUND_HALF_UP)
            rec.passed = True
            rec.data.update(entry=str(entry), stop_loss=str(stop_loss))
            evidence = {**rec.data, "candle": rec.candle}
            intents.append(EnterLong(rec.symbol_id, "stop", entry, None, stop_loss, "orb_breakout", evidence))
        ctx.candidates.extend(records)
        ctx.note(
            "orb: ranked",
            ranked=len(records),
            selected=[x.data["ticker"] for x in records if x.passed],
        )
        return intents

    def _direction(self, c: Candle) -> str:
        try:
            if is_bearish(c):
                return "bearish"
            return "doji" if is_doji(c, self.params.doji_body_pct_max) else "bullish"
        except ValueError:
            return "malformed"

    def _screen(self, c: Candle, atr14: Decimal | None, member: UniverseMember) -> str | None:
        p = self.params
        try:
            if is_bearish(c):
                return "bearish_candle"
            if is_doji(c, p.doji_body_pct_max):
                return "doji"
        except ValueError:
            return "malformed_bar"
        if not p.price_min <= c.close <= p.price_max:
            return "price_out_of_range"
        if atr14 is None:
            return "atr_missing"
        if atr14 < p.min_atr:
            return "atr_below_min"
        if member.avg_volume is None or member.avg_volume < p.min_avg_volume:
            return "avg_volume_below_min"
        return None

    def _catalyst_reason(self, cat: CatalystInfo | None) -> str | None:
        p = self.params
        if not p.require_catalyst:
            return None
        if cat is None or not cat.classified or cat.catalyst_type in NO_CATALYST:
            return "catalyst_missing"
        if cat.direction == "bearish":
            return "catalyst_bearish"
        if cat.quality is None or cat.quality < p.catalyst_min_quality:
            return "catalyst_low_quality"
        return None
```

- [ ] **Step 4: Run the scenarios**

Run: `uv --directory Trader/app run pytest tests/strategies -q`
Expected: all pass. In `test_catalyst_missing_bearish_or_weak_is_rejected` the volumes (5900, 5800, 5600, 5500) fix the rank order AAA, BBB, DDD, EEE.

- [ ] **Step 5: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/strategies/orb_sip.py Trader/app/tests/strategies/scenarios/__init__.py Trader/app/tests/strategies/scenarios/test_orb_sip_scenarios.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T8: orb_sip 1.0.0 plug-in with ranked candidates, reject reasons and evidence" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T9: `spy_overlay` plug-in 1.0.0

**Files:**
- Create: `Trader/app/trader/strategies/spy_overlay.py`, `Trader/app/tests/strategies/test_spy_overlay.py`

**Interfaces:**
- Consumes: `SessionOffset`, `ScheduledEvent`, `Exit`, `StrategyContext` (P2-T6: for an overlay, `ctx.positions` holds every open position of the entry strategies); `MarketDataView.symbol_ids/prior_close/quotes` (P2-T6/T7); `TICKER_PATTERN` (P1-T3); `tests/strategies/fakes.py`, `load_plugin` (P2-T6).
- Produces (`trader.strategies.spy_overlay`):
  - `DECISION_EVENT = "overlay_decision"`.
  - `SpyOverlayParams` (frozen, `extra="forbid"`): `decision_at="close-30m"` (a valid offset), `benchmark="SPY"` (ticker pattern), `signal: Literal["rest_of_day"] = "rest_of_day"`.
  - `SpyOverlay(params=None)`: `key="spy_overlay"`, `version="1.0.0"`, `kind="overlay"`; `schedule` gives one `overlay_decision` event at `decision_at`. On the event: `ret = (last − prior close) / prior close` for the benchmark (last trade, or last regular-hours trade if `last` is missing; a halted quote counts as missing). `ret <= 0` → `Exit(position_id, "market", None, "overlay_negative")` for every position in `ctx.positions`; `ret > 0` → nothing. A note `overlay: decision` records `decision`, `spy_return`, `prior_close`, `price` and the position IDs either way. Missing data (unknown benchmark, no prior close, no quote) holds, with a warning or error note: the flatten at `close-10m` still closes everything. `on_fill` returns `[]`.

- [ ] **Step 1: Write the failing tests**

`Trader/app/tests/strategies/test_spy_overlay.py`:
```python
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.strategies.fakes import CAL, NOW, SESSION, FakeData, make_ctx, position, quote
from trader.broker.types import FillEvent
from trader.strategies.base import Exit
from trader.strategies.registry import load_plugin
from trader.strategies.spy_overlay import DECISION_EVENT, SpyOverlay, SpyOverlayParams

SPY = 99


def data_with_spy(last: str | None, prior: str | None = "500.00") -> FakeData:
    d = FakeData()
    d.ids["SPY"] = SPY
    if prior is not None:
        d.closes[SPY] = Decimal(prior)
    if last is not None:
        d.quote_map[SPY] = quote(SPY, last, last, last)
    return d


async def decide(d: FakeData, positions: list[int]) -> tuple[list[object], dict[str, object], str]:
    s = SpyOverlay()
    ctx = make_ctx(d, s.params, positions=[position(p, 1) for p in positions])
    (event,) = s.schedule(CAL)
    intents = await s.on_event(ctx, event)
    note = next(n for n in ctx.notes if n.message.startswith("overlay"))
    return list(intents), note.data, note.level


async def test_negative_spy_exits_every_entry_position() -> None:
    intents, data, _ = await decide(data_with_spy("497.50"), [5, 6])
    assert intents == [Exit(5, "market", None, "overlay_negative"), Exit(6, "market", None, "overlay_negative")]
    assert data["decision"] == "exit" and data["spy_return"] == "-0.005000"
    assert data["position_ids"] == [5, 6]


async def test_a_flat_spy_counts_as_negative() -> None:
    intents, data, _ = await decide(data_with_spy("500.00"), [5])
    assert intents == [Exit(5, "market", None, "overlay_negative")] and data["decision"] == "exit"


async def test_positive_spy_holds_and_still_logs_the_decision() -> None:
    intents, data, level = await decide(data_with_spy("502.00"), [5])
    assert intents == [] and data["decision"] == "hold" and level == "info"
    assert data["spy_return"] == "0.004000" and data["prior_close"] == "500.00" and data["price"] == "502.00"


@pytest.mark.parametrize(("last", "prior"), [(None, "500.00"), ("497.00", None)])
async def test_missing_data_holds_with_a_warning(last: str | None, prior: str | None) -> None:
    intents, data, level = await decide(data_with_spy(last, prior), [5])
    assert intents == [] and data["decision"] == "hold" and level == "warning"


async def test_unknown_benchmark_holds_with_an_error() -> None:
    d = FakeData()
    intents, data, level = await decide(d, [5])
    assert intents == [] and level == "error" and data["decision"] == "hold"


def test_decision_time_is_close_minus_30m_even_on_early_closes() -> None:
    (event,) = SpyOverlay().schedule(CAL)
    assert event.key == DECISION_EVENT
    assert event.at.resolve(CAL, SESSION) == datetime(2026, 10, 6, 19, 30, tzinfo=UTC)  # 15:30 ET
    assert event.at.resolve(CAL, date(2026, 11, 27)) == datetime(2026, 11, 27, 17, 30, tzinfo=UTC)  # 12:30 ET


async def test_fills_need_nothing_from_the_overlay() -> None:
    s = SpyOverlay()
    ctx = make_ctx(FakeData(), s.params)
    fill = FillEvent(1, 1, 1, 1, "sell", "exit", 10, Decimal("20"), NOW, 5, 1, None, None, 2)
    assert await s.on_fill(ctx, fill) == []


@pytest.mark.parametrize("bad", [{"decision_at": "15:30"}, {"benchmark": "spy"}, {"signal": "vwap"}, {"x": 1}])
def test_invalid_params_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SpyOverlayParams.model_validate(bad)


def test_the_plugin_loads_through_its_entry_point() -> None:
    cls = load_plugin("spy_overlay")
    assert cls is SpyOverlay and cls.kind == "overlay" and cls.version == "1.0.0"
```

- [ ] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/strategies/test_spy_overlay.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.strategies.spy_overlay'`.

- [ ] **Step 3: Implement `trader/strategies/spy_overlay.py`**

```python
"""spy_overlay 1.0.0 (kind = overlay): hold into the close or exit at 15:30 ET (SPEC §5.3, BR-12)."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trader.broker.types import Fill
from trader.market.calendar import SessionCalendar
from trader.settings_store import TICKER_PATTERN
from trader.strategies.base import Exit, Intent, ScheduledEvent, SessionOffset, StrategyContext

DECISION_EVENT = "overlay_decision"
Q6 = Decimal("0.000001")


class SpyOverlayParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_at: str = "close-30m"
    benchmark: str = Field("SPY", pattern=TICKER_PATTERN)
    signal: Literal["rest_of_day"] = "rest_of_day"  # SPY return from the prior close to now

    @field_validator("decision_at")
    @classmethod
    def _offset(cls, v: str) -> str:
        SessionOffset.parse(v)
        return v


class SpyOverlay:
    key = "spy_overlay"
    version = "1.0.0"
    kind: Literal["entry", "overlay"] = "overlay"
    params_model = SpyOverlayParams

    def __init__(self, params: SpyOverlayParams | None = None) -> None:
        self.params = params or SpyOverlayParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return [ScheduledEvent(DECISION_EVENT, SessionOffset.parse(self.params.decision_at))]

    async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
        if event.key != DECISION_EVENT:
            return []
        bench = self.params.benchmark
        position_ids = [p.id for p in ctx.positions]
        sid = (await ctx.data.symbol_ids([bench])).get(bench)
        if sid is None:
            ctx.note("overlay: benchmark unknown, holding", level="error", decision="hold", benchmark=bench,
                     position_ids=position_ids)
            return []
        prior = await ctx.data.prior_close(sid, ctx.session_date)
        q = (await ctx.data.quotes([sid])).get(sid)
        price = None if q is None or q.is_halted else (q.last or q.last_regular)
        if prior is None or prior <= 0 or price is None:
            ctx.note(
                "overlay: no benchmark data, holding",
                level="warning",
                decision="hold",
                prior_close=None if prior is None else str(prior),
                price=None if price is None else str(price),
                position_ids=position_ids,
            )
            return []
        ret = ((price - prior) / prior).quantize(Q6, ROUND_HALF_UP)
        decision = "exit" if ret <= 0 else "hold"
        ctx.note(
            "overlay: decision",
            decision=decision,
            spy_return=str(ret),
            prior_close=str(prior),
            price=str(price),
            position_ids=position_ids,
        )
        if decision == "hold":
            return []
        return [Exit(pid, "market", None, "overlay_negative") for pid in position_ids]

    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
        return []
```

- [ ] **Step 4: Run the tests**

Run: `uv --directory Trader/app run pytest tests/strategies/test_spy_overlay.py -q`
Expected: all pass. `-2.50 / 500.00 = -0.005` prints as `-0.005000` at 6 dp.

- [ ] **Step 5: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/strategies/spy_overlay.py Trader/app/tests/strategies/test_spy_overlay.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T9: spy_overlay 1.0.0 plug-in (exit on a non-positive SPY day at close-30m)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T10: Risk manager and kill switches

**Files:**
- Create: `Trader/app/trader/engine/risk.py`, `Trader/app/trader/engine/killswitch.py`, `Trader/app/tests/engine/test_risk.py`, `Trader/app/tests/engine/test_killswitch.py`

**Interfaces:**
- Consumes: `RuntimeSettings` (P2-T2: `risk_pct`, `slippage_buffer`, `no_entry_before_close_minutes`, `cash_account_mode`, `markets_enabled`, `killswitch_*`); `Market` (P1-T3); `AccountState`, `OrderSpec`, `OrderView`, `PositionView` (P2-T4); `EnterLong`, `Exit`, `Cancel`, `Intent` (P2-T6); `SessionCalendar` (P1-T4); models `KillSwitchEvent`, `EquitySnapshot`, `SimAccount`, `Trade`, `AuditLog` (P2-T1); `log_event` (P1-T9); `get_live_run` (P2-T2, tests).
- Produces:
  - `trader.engine.risk`: `ProposalKind = Literal["entry","stop","exit","cancel"]`; `RiskContext(now, session_date, settings, account, positions: Mapping[int, PositionView], orders: Mapping[int, OrderView], strategy_config_id, blocking_switch=None, daily_pnl_pct=0, entries_today=0, max_positions=1, symbol_market=None, reference_price=None)`; `SizedOrder(intent, kind, qty, spec: OrderSpec | None, cancel_order_id=None, position_id=None, sizing={})`; `Rejection(intent, check, reason, detail={})`; `RiskManager(calendar).evaluate(intent, ctx) -> SizedOrder | Rejection` (master-plan contract).
    - Entries, checks in SPEC §6.1 order, first failure wins (`check` names): 1 `kill_switch` (`blocking_switch` set), 2 `daily_loss` (`daily_pnl_pct <= −killswitch_daily_loss_pct`), 3 `max_positions` (`entries_today >= max_positions`), 4 `market_hours` (a session, and `open <= now < close − no_entry_before_close_minutes`), 5 `settled_cash` (`account.buying_power <= 0`; buying power is settled cash in cash-account mode), 6 `market_enabled` (`symbol_market in markets_enabled`). Then sizing: `entry` = the stop price (stop), the limit (limit, stop-limit) or `reference_price` (market); `risk_$ = equity × risk_pct`; `shares_risk = floor(risk_$ / (entry − stop_loss))`; `shares_cash = floor(buying_power / (entry × (1 + slippage_buffer)))`; `shares = min(...)`; 0 → `zero_shares`. A missing entry price or `stop_loss >= entry` → `invalid`. `sizing` records every input and `limited_by` (`risk` or `cash`) as strings.
    - Exits and cancels skip every check (Review Focus 5). `Exit` → `SizedOrder(kind="stop" if order_type == "stop" else "exit", qty=position.qty, spec=sell OrderSpec for the whole position)`; `Cancel` → `SizedOrder(kind="cancel", qty=order.qty, spec=None, cancel_order_id=...)`. An unknown or closed position, or an order that isn't working, → `Rejection(check="invalid")`.
  - `trader.engine.killswitch`: `SWITCHES = ("daily_loss_pct", "max_drawdown_pct", "expectancy", "manual_pause")`; `KillSwitchInputs(start_equity, equity, peak_equity, closed_trades, expectancy_r)` with `daily_pnl_pct` and `drawdown_pct` properties (6 dp); `ActiveSwitch(switch, event_id, tripped_at, value)`; `KillSwitches(factory, clock)`: `active(run_id, session_date) -> list[ActiveSwitch]` (unreset trips; a `daily_loss_pct` trip counts only on its own session, so it resets automatically next session), `blocking(run_id, session_date) -> str | None`, `inputs(run_id, session_date, account, session_open) -> KillSwitchInputs` (start of day = the last equity snapshot before the open, else the sim account's starting cash; peak = the highest of stored peaks, starting cash and current equity; trades and average `pnl_r` from `trades`), `evaluate(run_id, session_date, inputs, settings) -> list[str]` (trips each switch whose threshold is crossed and that isn't already active: `kill_switch_events` row plus an `error` event), `pause(run_id, session_date, actor) -> bool`, `resume(run_id, actor) -> bool` (lifts only `manual_pause`), `reset(run_id, switch, reason, actor) -> None` (needs a non-blank reason; not for `manual_pause`; `ValueError` when nothing is tripped). Pause, resume and reset write `audit_log` (`killswitch.pause`, `killswitch.resume`, `killswitch.reset:<switch>`).

- [ ] **Step 1: Write the failing risk tests**

`Trader/app/tests/engine/test_risk.py`:
```python
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from trader.broker.types import AccountState, OrderView, PositionView
from trader.engine.risk import Rejection, RiskContext, RiskManager, SizedOrder
from trader.market.calendar import SessionCalendar
from trader.settings_store import RuntimeSettings
from trader.strategies.base import Cancel, EnterLong, Exit

CAL = SessionCalendar()
RISK = RiskManager(CAL)
DAY = date(2026, 10, 6)
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # 09:40 ET
ENTRY = EnterLong(7, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})


def account(equity: str = "720", buying_power: str = "720") -> AccountState:
    return AccountState(Decimal(buying_power), Decimal(buying_power), Decimal(buying_power), Decimal(0), Decimal(equity))


POS = PositionView(3, 7, 1, 35, Decimal("20.02"), Decimal("19.91"), T, DAY, None, T, 0)
ORDER = OrderView(9, 7, "buy", "stop", "entry", 35, Decimal("20.01"), None, "working", None, 1, 4, T)


def ctx(**over: Any) -> RiskContext:
    base = RiskContext(
        now=T,
        session_date=DAY,
        settings=RuntimeSettings(),
        account=account(),
        positions={POS.id: POS},
        orders={ORDER.id: ORDER},
        strategy_config_id=1,
        symbol_market="US",
    )
    return replace(base, **over)


def test_sizing_is_the_smaller_of_risk_and_cash_shares() -> None:
    out = RISK.evaluate(ENTRY, ctx())
    assert isinstance(out, SizedOrder) and out.kind == "entry"
    # risk: 720 x 2% = 14.40 / 0.10 = 144; cash: 720 / (20.01 x 1.005) = 35.8 -> 35
    assert out.qty == 35
    assert out.sizing["shares_risk"] == "144" and out.sizing["shares_cash"] == "35"
    assert out.sizing["limited_by"] == "cash" and out.sizing["risk_dollars"] == "14.40"
    assert out.spec is not None
    assert (out.spec.side, out.spec.order_type, out.spec.qty, out.spec.stop) == ("buy", "stop", 35, Decimal("20.01"))
    assert out.spec.stop_loss == Decimal("19.91") and out.spec.strategy_config_id == 1


def test_risk_limited_when_cash_is_plentiful() -> None:
    out = RISK.evaluate(ENTRY, ctx(account=account(equity="720", buying_power="100000")))
    assert isinstance(out, SizedOrder) and out.qty == 144 and out.sizing["limited_by"] == "risk"


ALL_BAD: dict[str, Any] = {
    "blocking_switch": "max_drawdown_pct",
    "daily_pnl_pct": Decimal("-0.06"),
    "entries_today": 1,
    "now": datetime(2026, 10, 6, 19, 45, tzinfo=UTC),  # 15:45 ET, inside the last 30 minutes
    "account": account(buying_power="0"),
    "symbol_market": "TSX",
}
ORDER_OF_CHECKS = ["kill_switch", "daily_loss", "max_positions", "market_hours", "settled_cash", "market_enabled"]


def test_checks_run_in_spec_order() -> None:
    bad = dict(ALL_BAD)
    fixes: dict[str, Any] = {
        "blocking_switch": None,
        "daily_pnl_pct": Decimal("-0.01"),
        "entries_today": 0,
        "now": T,
        "account": account(),
        "symbol_market": "US",
    }
    for check, key in zip(ORDER_OF_CHECKS, list(fixes), strict=True):
        out = RISK.evaluate(ENTRY, ctx(**bad))
        assert isinstance(out, Rejection) and out.check == check, (check, out)
        bad[key] = fixes[key]
    assert isinstance(RISK.evaluate(ENTRY, ctx(**bad)), SizedOrder)


def test_zero_shares_is_rejected() -> None:
    out = RISK.evaluate(ENTRY, ctx(account=account(buying_power="15")))
    assert isinstance(out, Rejection) and out.check == "zero_shares" and out.detail["shares_cash"] == "0"


@pytest.mark.parametrize(
    ("now", "ok"),
    [
        (datetime(2026, 10, 6, 13, 29, 59, tzinfo=UTC), False),
        (datetime(2026, 10, 6, 13, 30, tzinfo=UTC), True),
        (datetime(2026, 10, 6, 19, 29, 59, tzinfo=UTC), True),
        (datetime(2026, 10, 6, 19, 30, tzinfo=UTC), False),
    ],
)
def test_entry_window_boundaries(now: datetime, ok: bool) -> None:
    assert isinstance(RISK.evaluate(ENTRY, ctx(now=now)), SizedOrder) is ok


def test_entry_window_follows_an_early_close_and_holidays() -> None:
    early = date(2026, 11, 27)  # close 18:00Z, so no entries from 17:30Z
    assert isinstance(RISK.evaluate(ENTRY, ctx(session_date=early, now=datetime(2026, 11, 27, 17, 29, tzinfo=UTC))),
                      SizedOrder)
    late = RISK.evaluate(ENTRY, ctx(session_date=early, now=datetime(2026, 11, 27, 17, 30, tzinfo=UTC)))
    assert isinstance(late, Rejection) and late.check == "market_hours"
    holiday = RISK.evaluate(ENTRY, ctx(session_date=date(2026, 11, 26), now=datetime(2026, 11, 26, 15, 0, tzinfo=UTC)))
    assert isinstance(holiday, Rejection) and holiday.check == "market_hours"


def test_enabled_markets() -> None:
    both = RuntimeSettings(markets_enabled=["US", "TSX"])
    assert isinstance(RISK.evaluate(ENTRY, ctx(symbol_market="TSX", settings=both)), SizedOrder)
    assert isinstance(RISK.evaluate(ENTRY, ctx(symbol_market=None)), Rejection)


def test_market_entries_need_a_reference_price_and_a_stop_below_it() -> None:
    market = EnterLong(7, "market", None, None, Decimal("19.91"), "test", {})
    out = RISK.evaluate(market, ctx())
    assert isinstance(out, Rejection) and out.check == "invalid"
    sized = RISK.evaluate(market, ctx(reference_price=Decimal("20.00")))
    assert isinstance(sized, SizedOrder) and sized.qty == 35
    upside_down = EnterLong(7, "stop", Decimal("20.01"), None, Decimal("20.50"), "test", {})
    bad = RISK.evaluate(upside_down, ctx())
    assert isinstance(bad, Rejection) and bad.check == "invalid"


def test_exits_and_cancels_pass_when_everything_is_tripped() -> None:
    """Review Focus 5: no check can trap an open position."""
    tripped = ctx(**ALL_BAD, session_date=date(2026, 11, 26))  # also a holiday, after hours
    flatten = RISK.evaluate(Exit(POS.id, "market", None, "flatten_close"), tripped)
    assert isinstance(flatten, SizedOrder) and flatten.kind == "exit" and flatten.qty == 35
    assert flatten.spec is not None and flatten.spec.side == "sell" and flatten.spec.position_id == POS.id
    stop = RISK.evaluate(Exit(POS.id, "stop", Decimal("19.91"), "protective_stop"), tripped)
    assert isinstance(stop, SizedOrder) and stop.kind == "stop" and stop.spec is not None
    assert stop.spec.stop == Decimal("19.91") and stop.spec.purpose == "stop"
    cancel = RISK.evaluate(Cancel(ORDER.id, "entry_cancel_at"), tripped)
    assert isinstance(cancel, SizedOrder) and cancel.kind == "cancel" and cancel.cancel_order_id == ORDER.id


def test_exits_and_cancels_of_unknown_things_are_invalid() -> None:
    assert isinstance(RISK.evaluate(Exit(999, "market", None, "x"), ctx()), Rejection)
    assert isinstance(RISK.evaluate(Cancel(999, "x"), ctx()), Rejection)
    stopless = RISK.evaluate(Exit(POS.id, "stop", None, "x"), ctx())
    assert isinstance(stopless, Rejection) and stopless.check == "invalid"
```

- [ ] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/engine/test_risk.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.engine.risk'`.

- [ ] **Step 3: Implement `trader/engine/risk.py`**

```python
"""Risk manager (SPEC §6.1, BR-40): sizes entries and runs the six checks in SPEC order.

Exits and cancels are never blocked, so a kill switch can't trap an open position (Review Focus 5).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from trader.broker.types import AccountState, OrderSpec, OrderView, PositionView
from trader.market.calendar import SessionCalendar
from trader.settings_store import Market, RuntimeSettings
from trader.strategies.base import Cancel, EnterLong, Exit, Intent

ProposalKind = Literal["entry", "stop", "exit", "cancel"]


@dataclass(frozen=True, slots=True)
class RiskContext:
    now: datetime
    session_date: date
    settings: RuntimeSettings
    account: AccountState
    positions: Mapping[int, PositionView]
    orders: Mapping[int, OrderView]
    strategy_config_id: int
    blocking_switch: str | None = None
    daily_pnl_pct: Decimal = Decimal(0)
    entries_today: int = 0
    max_positions: int = 1
    symbol_market: Market | None = None
    reference_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SizedOrder:
    intent: Intent
    kind: ProposalKind
    qty: int
    spec: OrderSpec | None
    cancel_order_id: int | None = None
    position_id: int | None = None
    sizing: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Rejection:
    intent: Intent
    check: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def _floor(v: Decimal) -> int:
    return int(v.to_integral_value(rounding=ROUND_FLOOR))


class RiskManager:
    def __init__(self, calendar: SessionCalendar) -> None:
        self._cal = calendar

    def evaluate(self, intent: Intent, ctx: RiskContext) -> SizedOrder | Rejection:
        if isinstance(intent, Cancel):
            return self._cancel(intent, ctx)
        if isinstance(intent, Exit):
            return self._exit(intent, ctx)
        return self._entry(intent, ctx)

    @staticmethod
    def _cancel(intent: Cancel, ctx: RiskContext) -> SizedOrder | Rejection:
        order = ctx.orders.get(intent.order_id)
        if order is None or order.status != "working":
            return Rejection(intent, "invalid", f"order {intent.order_id} is not working")
        return SizedOrder(intent, "cancel", order.qty, None, cancel_order_id=order.id, position_id=order.position_id)

    @staticmethod
    def _exit(intent: Exit, ctx: RiskContext) -> SizedOrder | Rejection:
        pos = ctx.positions.get(intent.position_id)
        if pos is None:
            return Rejection(intent, "invalid", f"position {intent.position_id} is not open")
        kind: Literal["stop", "exit"] = "stop" if intent.order_type == "stop" else "exit"
        try:
            spec = OrderSpec(
                pos.symbol_id,
                "sell",
                intent.order_type,
                pos.qty,
                stop=intent.stop,
                purpose=kind,
                position_id=pos.id,
                strategy_config_id=pos.strategy_config_id,
                reason=intent.reason,
            )
        except ValueError as exc:
            return Rejection(intent, "invalid", str(exc))
        return SizedOrder(intent, kind, pos.qty, spec, position_id=pos.id)

    def _entry(self, intent: EnterLong, ctx: RiskContext) -> SizedOrder | Rejection:
        s = ctx.settings
        if ctx.blocking_switch is not None:
            return Rejection(intent, "kill_switch", f"kill switch {ctx.blocking_switch} is tripped")
        if ctx.daily_pnl_pct <= -s.killswitch_daily_loss_pct:
            return Rejection(
                intent, "daily_loss", f"today's P&L {ctx.daily_pnl_pct} is at the -{s.killswitch_daily_loss_pct} limit"
            )
        if ctx.entries_today >= ctx.max_positions:
            return Rejection(intent, "max_positions", f"{ctx.entries_today} of {ctx.max_positions} entries used today")
        if not self._cal.is_session(ctx.session_date):
            return Rejection(intent, "market_hours", f"{ctx.session_date} is not a trading session")
        open_ = self._cal.session_open(ctx.session_date)
        cutoff = self._cal.session_close(ctx.session_date) - timedelta(minutes=s.no_entry_before_close_minutes)
        if not open_ <= ctx.now < cutoff:
            return Rejection(
                intent,
                "market_hours",
                f"entries are allowed from the open until "
                f"{s.no_entry_before_close_minutes} minutes before the close",
            )
        buying_power = ctx.account.buying_power
        if buying_power <= 0:
            what = "settled cash" if s.cash_account_mode else "cash"
            return Rejection(intent, "settled_cash", f"no {what} to buy with")
        if ctx.symbol_market is None or ctx.symbol_market not in s.markets_enabled:
            return Rejection(intent, "market_enabled", f"market {ctx.symbol_market} is not enabled")

        if intent.order_type == "stop":
            entry = intent.stop
        elif intent.order_type in ("limit", "stop_limit"):
            entry = intent.limit
        else:
            entry = ctx.reference_price
        if entry is None or entry <= 0:
            return Rejection(intent, "invalid", "no entry price to size from")
        per_share = entry - intent.stop_loss
        if per_share <= 0:
            return Rejection(intent, "invalid", f"stop_loss {intent.stop_loss} is not below the entry {entry}")
        risk_dollars = ctx.account.equity * s.risk_pct
        shares_risk = _floor(risk_dollars / per_share)
        shares_cash = _floor(buying_power / (entry * (1 + s.slippage_buffer)))
        shares = min(shares_risk, shares_cash)
        sizing = {
            "equity": str(ctx.account.equity),
            "buying_power": str(buying_power),
            "risk_pct": str(s.risk_pct),
            "risk_dollars": str(risk_dollars),
            "entry": str(entry),
            "stop_loss": str(intent.stop_loss),
            "per_share_risk": str(per_share),
            "slippage_buffer": str(s.slippage_buffer),
            "shares_risk": str(shares_risk),
            "shares_cash": str(shares_cash),
            "shares": str(max(shares, 0)),
            "limited_by": "risk" if shares_risk <= shares_cash else "cash",
            "cash_account_mode": s.cash_account_mode,
        }
        if shares <= 0:
            return Rejection(intent, "zero_shares", "the position size rounds to zero shares", sizing)
        spec = OrderSpec(
            intent.symbol_id,
            "buy",
            intent.order_type,
            shares,
            stop=intent.stop,
            limit=intent.limit,
            purpose="entry",
            strategy_config_id=ctx.strategy_config_id,
            stop_loss=intent.stop_loss,
            reason=intent.reason,
        )
        return SizedOrder(intent, "entry", shares, spec, sizing=sizing)
```

- [ ] **Step 4: Run the risk tests**

Run: `uv --directory Trader/app run pytest tests/engine/test_risk.py -q`
Expected: all pass.

- [ ] **Step 5: Write the failing kill-switch tests**

`Trader/app/tests/engine/test_killswitch.py`:
```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.broker.types import AccountState
from trader.db import models as m
from trader.engine.killswitch import KillSwitches, KillSwitchInputs
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY, NEXT = date(2026, 10, 6), date(2026, 10, 7)
T = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
S = RuntimeSettings()


def inputs(start: str = "720", equity: str = "720", peak: str = "720", trades: int = 0, exp: str | None = None) -> KillSwitchInputs:
    return KillSwitchInputs(Decimal(start), Decimal(equity), Decimal(peak), trades, Decimal(exp) if exp else None)


@pytest.fixture
def ks(db_factory: sessionmaker[Session]) -> tuple[KillSwitches, int]:
    clock = FixedClock(T)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    return KillSwitches(db_factory, clock), run.id


def test_inputs_properties() -> None:
    i = inputs(start="720", equity="680", peak="800")
    assert i.daily_pnl_pct == Decimal("-0.055556") and i.drawdown_pct == Decimal("0.150000")


def test_daily_loss_trips_and_resets_next_session(db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]) -> None:
    switches, run = ks
    assert switches.evaluate(run, DAY, inputs(equity="684.01"), S) == []  # -4.999%
    assert switches.evaluate(run, DAY, inputs(equity="684"), S) == ["daily_loss_pct"]  # exactly -5%
    assert switches.blocking(run, DAY) == "daily_loss_pct"
    assert switches.blocking(run, NEXT) is None  # resets automatically next session
    with db_factory() as s:
        ev = s.execute(select(m.KillSwitchEvent)).scalar_one()
        alert = s.execute(select(m.EventLog).where(m.EventLog.source == "killswitch")).scalar_one()
    assert ev.switch == "daily_loss_pct" and ev.session_date == DAY and ev.value == Decimal("0.050000")
    assert ev.threshold == Decimal("0.050000") and alert.level == "error"


def test_an_active_switch_is_not_tripped_twice(db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]) -> None:
    switches, run = ks
    assert switches.evaluate(run, DAY, inputs(equity="650", peak="650"), S) == ["daily_loss_pct"]
    assert switches.evaluate(run, DAY, inputs(equity="640", peak="640"), S) == []
    with db_factory() as s:
        assert len(s.execute(select(m.KillSwitchEvent)).scalars().all()) == 1


def test_drawdown_needs_a_manual_reset_with_a_reason(
    db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]
) -> None:
    switches, run = ks
    assert switches.evaluate(run, DAY, inputs(start="680", equity="680", peak="800"), S) == ["max_drawdown_pct"]
    assert switches.blocking(run, NEXT) == "max_drawdown_pct"  # survives the session change
    with pytest.raises(ValueError, match="reason"):
        switches.reset(run, "max_drawdown_pct", "   ", actor="stephen")
    switches.reset(run, "max_drawdown_pct", "reviewed the losing streak", actor="stephen")
    assert switches.blocking(run, NEXT) is None
    with pytest.raises(ValueError, match="not tripped"):
        switches.reset(run, "max_drawdown_pct", "again", actor="stephen")
    with db_factory() as s:
        audit = s.execute(select(m.AuditLog)).scalar_one()
        ev = s.execute(select(m.KillSwitchEvent)).scalar_one()
    assert audit.action == "killswitch.reset:max_drawdown_pct" and audit.actor == "stephen"
    assert ev.reset_reason == "reviewed the losing streak" and ev.reset_by == "stephen" and ev.reset_at == T


@pytest.mark.parametrize(("trades", "exp", "trips"), [(50, "0", True), (50, "-0.2", True), (49, "-1", False), (60, "0.05", False)])
def test_expectancy_switch_after_n_trades(ks: tuple[KillSwitches, int], trades: int, exp: str, trips: bool) -> None:
    switches, run = ks
    assert (switches.evaluate(run, DAY, inputs(trades=trades, exp=exp), S) == ["expectancy"]) is trips


def test_manual_pause_blocks_entries_and_resume_lifts_only_the_pause(
    db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]
) -> None:
    switches, run = ks
    assert switches.pause(run, DAY, actor="telegram") is True
    assert switches.pause(run, DAY, actor="telegram") is False  # already paused
    assert switches.blocking(run, NEXT) == "manual_pause"
    switches.evaluate(run, DAY, inputs(start="680", equity="680", peak="800"), S)
    assert switches.resume(run, actor="telegram") is True
    assert switches.blocking(run, DAY) == "max_drawdown_pct"  # /resume never lifts an automatic switch
    assert switches.resume(run, actor="telegram") is False
    with pytest.raises(ValueError, match="resume"):
        switches.reset(run, "manual_pause", "reason", actor="stephen")
    with db_factory() as s:
        actions = [a for a in s.execute(select(m.AuditLog.action).order_by(m.AuditLog.id)).scalars()]
    assert actions == ["killswitch.pause", "killswitch.resume"]


def test_inputs_come_from_snapshots_account_and_trades(
    db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]
) -> None:
    switches, run = ks
    session_open = CAL.session_open(DAY)
    acct = AccountState(Decimal("690"), Decimal("690"), Decimal("690"), Decimal("0"), Decimal("690"))
    first = switches.inputs(run, DAY, acct, session_open)
    assert (first.start_equity, first.peak_equity, first.closed_trades, first.expectancy_r) == (
        Decimal("720.0000"), Decimal("720.0000"), 0, None)
    with db_factory() as s:
        for ts, eq, peak in ((session_open - timedelta(hours=20), "700", "750"), (session_open - timedelta(hours=1), "710", "750")):
            s.add(m.EquitySnapshot(run_id=run, ts=ts, equity=Decimal(eq), cash=Decimal(eq), settled_cash=Decimal(eq),
                                   peak_equity=Decimal(peak), drawdown_pct=Decimal("0")))
        sym, cfg = add_symbol(s), add_strategy_config(s)
        pos = m.Position(run_id=run, symbol_id=sym, strategy_config_id=cfg, qty=1, avg_price=Decimal("1"),
                         stop_loss=None, planned_risk=None, session_date=DAY, opened_at=T, closed_at=T,
                         entry_order_id=1, stop_order_id=None, unprotected_since=None, unprotected_seconds=0)
        s.add(pos)
        s.flush()
        s.add(m.Trade(run_id=run, position_id=pos.id, symbol_id=sym, session_date=DAY, entry_price=Decimal("1"),
                      exit_price=Decimal("1"), qty=1, pnl=Decimal("-1"), pnl_r=Decimal("-0.5"), planned_risk=None,
                      exit_reason="t", slippage_total=Decimal("0"), fees_total=Decimal("0"), opened_at=T, closed_at=T))
        s.commit()
    later = switches.inputs(run, DAY, acct, session_open)
    assert later.start_equity == Decimal("710.0000") and later.peak_equity == Decimal("750.0000")
    assert later.closed_trades == 1 and later.expectancy_r == Decimal("-0.5000")
```

- [ ] **Step 6: Run to see them fail**

Run: `uv --directory Trader/app run pytest tests/engine/test_killswitch.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.engine.killswitch'`.

- [ ] **Step 7: Implement `trader/engine/killswitch.py`**

```python
"""Kill switches (SPEC §6.3, BR-41): checked before every entry proposal and after every fill.

daily_loss_pct blocks entries until the next session (it resets by itself); max_drawdown_pct and expectancy
need a manual reset with a reason; manual_pause is set and lifted by /pause and /resume. None of them ever
blocks an exit, a stop or a cancel (the risk manager only consults them for entries).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.types import AccountState
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings

SWITCHES = ("daily_loss_pct", "max_drawdown_pct", "expectancy", "manual_pause")
Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
SOURCE = "killswitch"


@dataclass(frozen=True, slots=True)
class KillSwitchInputs:
    start_equity: Decimal
    equity: Decimal
    peak_equity: Decimal
    closed_trades: int
    expectancy_r: Decimal | None

    @property
    def daily_pnl_pct(self) -> Decimal:
        if self.start_equity <= 0:
            return Decimal(0)
        return ((self.equity - self.start_equity) / self.start_equity).quantize(Q6, ROUND_HALF_UP)

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal(0)
        return ((self.peak_equity - self.equity) / self.peak_equity).quantize(Q6, ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ActiveSwitch:
    switch: str
    event_id: int
    tripped_at: datetime
    value: Decimal | None


class KillSwitches:
    def __init__(self, factory: sessionmaker[Session], clock: Clock) -> None:
        self._factory = factory
        self._clock = clock

    @staticmethod
    def _open_rows(s: Session, run_id: int, switch: str | None = None) -> list[m.KillSwitchEvent]:
        q = select(m.KillSwitchEvent).where(m.KillSwitchEvent.run_id == run_id, m.KillSwitchEvent.reset_at.is_(None))
        if switch is not None:
            q = q.where(m.KillSwitchEvent.switch == switch)
        return list(s.execute(q.order_by(m.KillSwitchEvent.id).with_for_update()).scalars())

    def active(self, run_id: int, session_date: date) -> list[ActiveSwitch]:
        with self._factory() as s:
            rows = s.execute(
                select(m.KillSwitchEvent)
                .where(m.KillSwitchEvent.run_id == run_id, m.KillSwitchEvent.reset_at.is_(None))
                .order_by(m.KillSwitchEvent.id)
            ).scalars()
            return [
                ActiveSwitch(r.switch, r.id, r.tripped_at, r.value)
                for r in rows
                if r.switch != "daily_loss_pct" or r.session_date == session_date
            ]

    def blocking(self, run_id: int, session_date: date) -> str | None:
        active = self.active(run_id, session_date)
        return active[0].switch if active else None

    def inputs(self, run_id: int, session_date: date, account: AccountState, session_open: datetime) -> KillSwitchInputs:
        with self._factory() as s:
            start = s.execute(
                select(m.EquitySnapshot.equity)
                .where(m.EquitySnapshot.run_id == run_id, m.EquitySnapshot.ts < session_open)
                .order_by(m.EquitySnapshot.ts.desc())
                .limit(1)
            ).scalar_one_or_none()
            starting_cash = s.execute(
                select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == run_id)
            ).scalar_one_or_none()
            stored_peak = s.execute(
                select(func.max(m.EquitySnapshot.peak_equity)).where(m.EquitySnapshot.run_id == run_id)
            ).scalar_one()
            count, avg_r = s.execute(
                select(func.count(m.Trade.id), func.avg(m.Trade.pnl_r)).where(m.Trade.run_id == run_id)
            ).one()
        start_equity = start if start is not None else (starting_cash if starting_cash is not None else account.equity)
        peak = max(v for v in (stored_peak, starting_cash, account.equity) if v is not None)
        return KillSwitchInputs(
            start_equity=start_equity,
            equity=account.equity,
            peak_equity=peak,
            closed_trades=int(count),
            expectancy_r=Decimal(avg_r).quantize(Q4, ROUND_HALF_UP) if avg_r is not None else None,
        )

    def evaluate(
        self, run_id: int, session_date: date, inputs: KillSwitchInputs, settings: RuntimeSettings
    ) -> list[str]:
        active = {a.switch for a in self.active(run_id, session_date)}
        crossed: list[tuple[str, Decimal, Decimal]] = []
        loss = -inputs.daily_pnl_pct
        if "daily_loss_pct" not in active and loss >= settings.killswitch_daily_loss_pct:
            crossed.append(("daily_loss_pct", loss, settings.killswitch_daily_loss_pct))
        if "max_drawdown_pct" not in active and inputs.drawdown_pct >= settings.killswitch_max_drawdown_pct:
            crossed.append(("max_drawdown_pct", inputs.drawdown_pct, settings.killswitch_max_drawdown_pct))
        if (
            "expectancy" not in active
            and inputs.closed_trades >= settings.killswitch_expectancy_min_trades
            and inputs.expectancy_r is not None
            and inputs.expectancy_r <= settings.killswitch_expectancy_threshold_r
        ):
            crossed.append(("expectancy", inputs.expectancy_r, settings.killswitch_expectancy_threshold_r))
        if not crossed:
            return []
        now = self._clock.now()
        with session_scope(self._factory) as s:
            for switch, value, threshold in crossed:
                s.add(
                    m.KillSwitchEvent(
                        run_id=run_id,
                        switch=switch,
                        session_date=session_date,
                        tripped_at=now,
                        value=value,
                        threshold=threshold,
                    )
                )
                data: dict[str, Any] = {
                    "switch": switch,
                    "value": str(value),
                    "threshold": str(threshold),
                    "equity": str(inputs.equity),
                    "closed_trades": inputs.closed_trades,
                }
                log_event(s, self._clock, "error", SOURCE, f"kill switch {switch} tripped: entries blocked", data, run_id)
        return [c[0] for c in crossed]

    def pause(self, run_id: int, session_date: date, actor: str) -> bool:
        now = self._clock.now()
        with session_scope(self._factory) as s:
            if self._open_rows(s, run_id, "manual_pause"):
                return False
            s.add(m.KillSwitchEvent(run_id=run_id, switch="manual_pause", session_date=session_date, tripped_at=now))
            s.add(m.AuditLog(ts=now, actor=actor, action="killswitch.pause", before=None, after={"run_id": run_id}))
            log_event(s, self._clock, "warning", SOURCE, "manual pause: entries blocked", {"actor": actor}, run_id)
            return True

    def resume(self, run_id: int, actor: str) -> bool:
        now = self._clock.now()
        with session_scope(self._factory) as s:
            rows = self._open_rows(s, run_id, "manual_pause")
            if not rows:
                return False
            for r in rows:
                r.reset_at, r.reset_reason, r.reset_by = now, "resume", actor
            s.add(m.AuditLog(ts=now, actor=actor, action="killswitch.resume", before={"run_id": run_id}, after=None))
            log_event(s, self._clock, "info", SOURCE, "manual pause lifted", {"actor": actor}, run_id)
            return True

    def reset(self, run_id: int, switch: str, reason: str, actor: str) -> None:
        if switch == "manual_pause":
            raise ValueError("a manual pause is lifted with resume, not reset")
        if switch not in SWITCHES:
            raise ValueError(f"unknown switch {switch!r}")
        if not reason.strip():
            raise ValueError("a kill-switch reset needs a reason")
        now = self._clock.now()
        with session_scope(self._factory) as s:
            rows = self._open_rows(s, run_id, switch)
            if not rows:
                raise ValueError(f"kill switch {switch} is not tripped")
            before = [{"id": r.id, "tripped_at": r.tripped_at.isoformat()} for r in rows]
            for r in rows:
                r.reset_at, r.reset_reason, r.reset_by = now, reason.strip(), actor
            s.add(
                m.AuditLog(
                    ts=now, actor=actor, action=f"killswitch.reset:{switch}", before={"trips": before},
                    after={"reason": reason.strip()},
                )
            )
            log_event(s, self._clock, "warning", SOURCE, f"kill switch {switch} reset", {"reason": reason.strip()}, run_id)
```

- [ ] **Step 8: Run the tests**

Run: `uv --directory Trader/app run pytest tests/engine/test_risk.py tests/engine/test_killswitch.py -q`
Expected: all pass. In `test_daily_loss_trips_and_resets_next_session`, `(684 − 720) / 720 = −0.05` exactly, which trips (`>=`), while `684.01` gives `−0.049986`.

- [ ] **Step 9: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/engine/risk.py Trader/app/trader/engine/killswitch.py Trader/app/tests/engine/test_risk.py Trader/app/tests/engine/test_killswitch.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T10: risk manager (sizing and SPEC 6.1 checks) and kill switches with manual reset" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T11: Proposal service

**Files:**
- Create: `Trader/app/trader/engine/proposals.py`, `Trader/app/tests/engine/test_proposals.py`

**Interfaces:**
- Consumes: `SizedOrder`, `ProposalKind` (P2-T10); `Broker`, `BrokerRejected` (P2-T5), `SimBroker` (tests); `OrderSpec` (P2-T4); `SettingsStore` (P1-T3: `load()`, `set()` writes `audit_log`); models `Proposal`, `Signal`, `Position`, `AuditLog` (P2-T1); `log_event` (P1-T9); `get_live_run` (P2-T2), factories (P2-T1).
- Produces (`trader.engine.proposals`):
  - `DecisionResult(status: str, already_decided: bool, order_id: int | None = None)`.
  - `ProposalService(factory, clock, settings: SettingsStore, broker: Broker, run_id: int)`:
    - `create(signal_id, sized: SizedOrder, kind: ProposalKind) -> Proposal` (ORM row, detached): `pending` with `expires_at = now + TTL(kind)` (entry `proposal_ttl_entry_seconds`, stop `proposal_ttl_stop_seconds`, exit and cancel `proposal_ttl_exit_seconds`); in `auto` mode it is `auto_approved` and executed at once (`decided_via="auto"`, latency 0). `order_spec` is the spec JSON, or `{"cancel_order_id", "reason"}` for a cancel.
    - `decide(proposal_id, decision: Literal["approve","reject"], via: Literal["telegram","web","auto"], actor: str) -> DecisionResult`: locks the row; the first decision wins, any later one (or one after `expires_at`, which expires it first) returns `already_decided=True` with the current status. Approve executes in the same transaction: submit (or cancel) through the broker with the session, so the proposal is `submitted` with its `order_id`, or `failed` with `error` if the broker refuses. Stores `decided_at`, `decided_via`, `decided_by`, `decision_latency_ms`, and writes `audit_log` `proposal.approve` / `proposal.reject`.
    - `expire_due(now) -> list[Proposal]`: every pending proposal of the run with `expires_at <= now` becomes `expired` (`expired_at` set). A protective stop logs an `error` event (the position stays unprotected; `escalated_at`, `escalations = 1`). An exit with `auto_flatten_on_expiry` on is submitted at once (`decided_via="auto"`, `decided_by="auto_flatten_on_expiry"`, final status `submitted`); with it off, an `error` escalation is logged. A cancel with `auto_flatten_on_expiry` on is executed the same way (the working order is cancelled, `decided_by="auto_flatten_on_expiry"`, final status `submitted`, or `failed` if the order is no longer working), because an unanswered "cancel this entry" must not leave a buy working into the close (BR-42); with it off, a cancel just expires. Entries just expire.
    - `escalate_unprotected(now) -> list[Proposal]`: re-alerts (an `error` event, `escalations += 1`) every `stop_escalation_seconds` for each expired stop proposal whose position is still open with no stop order.
    - `set_approval_mode(mode: Literal["manual","auto"], actor: str) -> None`: through `SettingsStore.set`, which audits the change (SPEC §6.2).
    - Proposal status values: `pending`, `approved` (transient inside the transaction), `rejected`, `expired`, `auto_approved` (transient), `submitted`, `failed`.

- [ ] **Step 1: Write the failing tests**

`Trader/app/tests/engine/test_proposals.py`:
```python
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.engine.proposals import ProposalService
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import Cancel, EnterLong, Exit

pytestmark = pytest.mark.db
CAL = SessionCalendar()
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)


@dataclass
class Env:
    svc: ProposalService
    broker: SimBroker
    store: SettingsStore
    clock: FixedClock
    factory: sessionmaker[Session]
    run_id: int
    sym: int
    cfg: int
    signal_id: int


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        sig = m.Signal(run_id=run.id, strategy_config_id=cfg, symbol_id=sym, session_date=T.date(),
                       event_key="orb_open", ts=T, intent={}, evidence={})
        s.add(sig)
        s.commit()
        signal_id = sig.id
    store = SettingsStore(db_factory, now=clock.now)
    broker = SimBroker(db_factory, clock, Ledger(CAL), QuoteFillModel(FillParams()), run.id)
    return Env(ProposalService(db_factory, clock, store, broker, run.id), broker, store, clock, db_factory, run.id,
               sym, cfg, signal_id)


def entry_sized(env: Env) -> SizedOrder:
    intent = EnterLong(env.sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
    spec = OrderSpec(env.sym, "buy", "stop", 10, stop=Decimal("20.01"), stop_loss=Decimal("19.91"),
                     strategy_config_id=env.cfg, reason="orb_breakout")
    return SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10"})


def open_position(env: Env) -> int:
    env.broker.submit(OrderSpec(env.sym, "buy", "market", 10, stop_loss=Decimal("19.00"), strategy_config_id=env.cfg))
    q = QtQuote(env.sym, "AAA", Decimal("20.00"), Decimal("20.01"), Decimal("20.00"), None, 1000,
                env.clock.now() - timedelta(seconds=1), 0, False, None)
    (ev,) = env.broker.on_quotes([q], env.clock.now())
    return ev.position_id


def exit_sized(env: Env, pid: int, order_type: Literal["market", "stop"] = "market") -> SizedOrder:
    kind: Literal["stop", "exit"] = "stop" if order_type == "stop" else "exit"
    stop = Decimal("19.00") if order_type == "stop" else None
    spec = OrderSpec(env.sym, "sell", order_type, 10, stop=stop, purpose=kind, position_id=pid,
                     reason="protective_stop" if kind == "stop" else "flatten_close")
    return SizedOrder(Exit(pid, order_type, stop, spec.reason), kind, 10, spec, position_id=pid)


def test_a_manual_entry_waits_as_pending(env: Env) -> None:
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    assert p.status == "pending" and p.expires_at == T + timedelta(minutes=5) and p.order_id is None
    assert p.order_spec["stop"] == "20.01" and p.sizing == {"shares": "10"}
    assert env.broker.working_orders() == []


def test_approve_submits_the_order_and_records_the_latency(env: Env) -> None:
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    env.clock.set(T + timedelta(seconds=42))
    r = env.svc.decide(p.id, "approve", "telegram", "stephen")
    assert (r.status, r.already_decided) == ("submitted", False) and r.order_id is not None
    (order,) = env.broker.working_orders()
    assert order.id == r.order_id and order.proposal_id == p.id
    with env.factory() as s:
        row = s.get(m.Proposal, p.id)
        audit = s.execute(select(m.AuditLog)).scalar_one()
    assert row is not None and row.decided_via == "telegram" and row.decided_by == "stephen"
    assert row.decision_latency_ms == 42_000 and row.decided_at == T + timedelta(seconds=42)
    assert audit.action == "proposal.approve" and audit.actor == "stephen"


def test_reject_places_nothing(env: Env) -> None:
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    r = env.svc.decide(p.id, "reject", "web", "stephen")
    assert (r.status, r.already_decided, r.order_id) == ("rejected", False, None)
    assert env.broker.working_orders() == []


def test_the_second_decision_gets_already_decided(env: Env) -> None:
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    env.svc.decide(p.id, "approve", "telegram", "stephen")
    again = env.svc.decide(p.id, "reject", "web", "stephen")
    assert (again.status, again.already_decided) == ("submitted", True)
    assert len(env.broker.working_orders()) == 1


def test_concurrent_decisions_first_wins(env: Env) -> None:
    """Review Focus 2: Telegram and the web at the same moment give one decision and at most one order."""
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    calls = [("approve", "telegram"), ("reject", "web"), ("approve", "web"), ("approve", "telegram")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda c: env.svc.decide(p.id, c[0], c[1], "stephen"), calls))  # type: ignore[arg-type]
    assert sum(not r.already_decided for r in results) == 1
    assert len({r.status for r in results}) == 1
    assert len(env.broker.working_orders()) == (1 if results[0].status == "submitted" else 0)


def test_decide_after_expiry_is_already_decided(env: Env) -> None:
    """Review Focus 2: a tap after the TTL expires the proposal instead of submitting it."""
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    env.clock.set(T + timedelta(minutes=5))
    r = env.svc.decide(p.id, "approve", "telegram", "stephen")
    assert (r.status, r.already_decided) == ("expired", True)
    assert env.broker.working_orders() == []


def test_expiry_follows_the_kind_and_ttl(env: Env) -> None:
    pid = open_position(env)
    entry = env.svc.create(env.signal_id, entry_sized(env), "entry")
    stop = env.svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    assert stop.expires_at == T + timedelta(minutes=3)
    assert env.svc.expire_due(T + timedelta(seconds=179)) == []
    assert [x.id for x in env.svc.expire_due(T + timedelta(seconds=180))] == [stop.id]
    assert [x.id for x in env.svc.expire_due(T + timedelta(seconds=300))] == [entry.id]
    with env.factory() as s:
        statuses = {r.id: r.status for r in s.execute(select(m.Proposal)).scalars()}
    assert statuses == {entry.id: "expired", stop.id: "expired"}


def test_auto_mode_approves_at_once_and_the_mode_change_is_audited(env: Env) -> None:
    env.svc.set_approval_mode("auto", actor="stephen")
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    assert p.status == "submitted" and p.decided_via == "auto" and p.decision_latency_ms == 0
    assert p.order_id is not None and len(env.broker.working_orders()) == 1
    with env.factory() as s:
        audit = s.execute(select(m.AuditLog)).scalar_one()
    assert audit.action == "settings.set:approval_mode" and audit.after == {"value": "auto"}


def test_an_expired_protective_stop_escalates_and_counts_unprotected_time(env: Env) -> None:
    pid = open_position(env)  # opened at T, unprotected from then
    stop = env.svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    env.svc.expire_due(T + timedelta(seconds=180))
    assert env.svc.escalate_unprotected(T + timedelta(seconds=200)) == []  # 60 s between alerts
    (again,) = env.svc.escalate_unprotected(T + timedelta(seconds=240))
    assert again.id == stop.id and again.escalations == 2
    with env.factory() as s:
        errors = s.execute(select(m.EventLog.message).where(m.EventLog.level == "error")).scalars().all()
    assert len(errors) == 2 and all("unprotected" in e or "no stop" in e for e in errors)
    env.clock.set(T + timedelta(seconds=600))
    flat = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    env.svc.decide(flat.id, "approve", "telegram", "stephen")
    q = QtQuote(env.sym, "AAA", Decimal("20.10"), Decimal("20.11"), Decimal("20.10"), None, 1000,
                env.clock.now() - timedelta(seconds=1), 0, False, None)
    env.broker.on_quotes([q], env.clock.now())
    with env.factory() as s:
        pos = s.get(m.Position, pid)
    assert pos is not None and pos.closed_at is not None and pos.unprotected_seconds == 600
    assert env.svc.escalate_unprotected(T + timedelta(seconds=900)) == []  # closed: no more alerts


def test_expired_flatten_auto_submits(env: Env) -> None:
    """Review Focus 4: an unanswered flatten must not leave the position open overnight."""
    pid = open_position(env)
    flat = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    assert expired.id == flat.id and expired.status == "submitted"
    assert expired.decided_via == "auto" and expired.decided_by == "auto_flatten_on_expiry"
    assert expired.expired_at == T + timedelta(minutes=5) and expired.order_id is not None
    assert [o.id for o in env.broker.working_orders()] == [expired.order_id]


def test_expired_flatten_escalates_when_auto_flatten_is_off(env: Env) -> None:
    env.store.set("auto_flatten_on_expiry", False, actor="stephen")
    pid = open_position(env)
    env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    assert expired.status == "expired" and env.broker.working_orders() == []
    with env.factory() as s:
        levels = s.execute(select(m.EventLog.level).where(m.EventLog.source == "proposals")).scalars().all()
    assert "error" in levels


def test_an_entry_that_would_fill_late_is_cancelled_instead(env: Env) -> None:
    """Review Focus 4 (BR-42): an unanswered cancel of a working entry executes on expiry, like a flatten."""
    order_id = env.broker.submit(entry_sized(env).spec)  # type: ignore[arg-type]
    sized = SizedOrder(Cancel(order_id, "entry_cancel_at"), "cancel", 10, None, cancel_order_id=order_id)
    p = env.svc.create(env.signal_id, sized, "cancel")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    assert expired.id == p.id and expired.status == "submitted" and expired.expired_at == T + timedelta(minutes=5)
    assert expired.decided_via == "auto" and expired.decided_by == "auto_flatten_on_expiry"
    assert env.broker.working_orders() == []  # the entry can no longer fill late
    with env.factory() as s:
        order = s.get(m.Order, order_id)
    assert order is not None and order.status == "cancelled" and order.cancel_reason == "entry_cancel_at"


def test_an_expired_cancel_just_expires_when_auto_flatten_is_off(env: Env) -> None:
    env.store.set("auto_flatten_on_expiry", False, actor="stephen")
    order_id = env.broker.submit(entry_sized(env).spec)  # type: ignore[arg-type]
    sized = SizedOrder(Cancel(order_id, "entry_cancel_at"), "cancel", 10, None, cancel_order_id=order_id)
    env.svc.create(env.signal_id, sized, "cancel")
    (expired,) = env.svc.expire_due(T + timedelta(minutes=5))
    assert expired.status == "expired" and [o.id for o in env.broker.working_orders()] == [order_id]


def test_a_cancel_proposal_cancels_the_order(env: Env) -> None:
    order_id = env.broker.submit(entry_sized(env).spec)  # type: ignore[arg-type]
    sized = SizedOrder(Cancel(order_id, "entry_cancel_at"), "cancel", 10, None, cancel_order_id=order_id)
    p = env.svc.create(env.signal_id, sized, "cancel")
    assert p.order_spec == {"cancel_order_id": order_id, "reason": "entry_cancel_at"}
    assert env.svc.decide(p.id, "approve", "web", "stephen").status == "submitted"
    assert env.broker.working_orders() == []


def test_a_broker_refusal_marks_the_proposal_failed(env: Env) -> None:
    pid = open_position(env)
    first = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    second = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    env.svc.decide(first.id, "approve", "web", "stephen")
    q = QtQuote(env.sym, "AAA", Decimal("20.10"), Decimal("20.11"), Decimal("20.10"), None, 1000,
                env.clock.now() - timedelta(seconds=1), 0, False, None)
    env.broker.on_quotes([q], env.clock.now())  # the position is now closed
    r = env.svc.decide(second.id, "approve", "web", "stephen")
    assert (r.status, r.already_decided, r.order_id) == ("failed", False, None)
    with env.factory() as s:
        row = s.get(m.Proposal, second.id)
    assert row is not None and row.error is not None and "not open" in row.error


def test_kind_must_match_the_sized_order(env: Env) -> None:
    with pytest.raises(ValueError):
        env.svc.create(env.signal_id, entry_sized(env), "exit")
```

- [ ] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/engine/test_proposals.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.engine.proposals'`.

- [ ] **Step 3: Implement `trader/engine/proposals.py`**

```python
"""Proposal workflow (SPEC §6.2, BR-30, BR-31, BR-33).

pending → approved → submitted · pending → rejected · pending → expired · auto_approved → submitted.
decide() locks the row, so the first decision wins and every later one gets already_decided (Review Focus 2).
An approved proposal is submitted to the broker in the same transaction as the decision.
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.base import Broker, BrokerRejected
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.risk import ProposalKind, SizedOrder
from trader.events import log_event
from trader.market.clock import Clock
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import Cancel

Decision = Literal["approve", "reject"]
Via = Literal["telegram", "web", "auto"]
SOURCE = "proposals"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    status: str
    already_decided: bool
    order_id: int | None = None


def _ttl(kind: str, s: RuntimeSettings) -> timedelta:
    seconds = {
        "entry": s.proposal_ttl_entry_seconds,
        "stop": s.proposal_ttl_stop_seconds,
        "exit": s.proposal_ttl_exit_seconds,
        "cancel": s.proposal_ttl_exit_seconds,
    }[kind]
    return timedelta(seconds=seconds)


class ProposalService:
    def __init__(
        self, factory: sessionmaker[Session], clock: Clock, settings: SettingsStore, broker: Broker, run_id: int
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._settings = settings
        self._broker = broker
        self.run_id = run_id

    def _log(self, s: Session, level: str, message: str, p: m.Proposal, **extra: Any) -> None:
        data = {"proposal_id": p.id, "kind": p.kind, "status": p.status, "position_id": p.position_id, **extra}
        log_event(s, self._clock, level, SOURCE, message, data, run_id=self.run_id)

    def set_approval_mode(self, mode: Literal["manual", "auto"], actor: str) -> None:
        self._settings.set("approval_mode", mode, actor)  # the settings store writes the audit row

    def create(self, signal_id: int, sized: SizedOrder, kind: ProposalKind) -> m.Proposal:
        if kind != sized.kind:
            raise ValueError(f"kind {kind!r} doesn't match the sized order's kind {sized.kind!r}")
        settings = self._settings.load()
        now = self._clock.now()
        if isinstance(sized.intent, Cancel):
            order_spec: dict[str, Any] = {"cancel_order_id": sized.cancel_order_id, "reason": sized.intent.reason}
        else:
            order_spec = sized.spec.to_json() if sized.spec is not None else {}
        with session_scope(self._factory) as s:
            p = m.Proposal(
                run_id=self.run_id,
                signal_id=signal_id,
                kind=kind,
                order_spec=order_spec,
                qty=sized.qty,
                status="pending",
                created_at=now,
                expires_at=now + _ttl(kind, settings),
                position_id=sized.position_id,
                cancel_order_id=sized.cancel_order_id,
                sizing=sized.sizing or None,
                escalations=0,
            )
            s.add(p)
            s.flush()
            if settings.approval_mode == "auto":
                p.status, p.decided_at, p.decided_via, p.decided_by = "auto_approved", now, "auto", "auto"
                p.decision_latency_ms = 0
                self._execute(s, p)
            self._log(s, "info", f"proposal {p.id} ({kind}) {p.status}", p, expires_at=p.expires_at.isoformat())
            return p

    def decide(self, proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
        settings = self._settings.load()
        now = self._clock.now()
        with session_scope(self._factory) as s:
            p = s.execute(
                select(m.Proposal)
                .where(m.Proposal.id == proposal_id, m.Proposal.run_id == self.run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if p is None:
                raise KeyError(f"proposal {proposal_id} not found")
            if p.status != "pending":
                return DecisionResult(p.status, True, p.order_id)
            if now >= p.expires_at:
                self._expire(s, p, now, settings)
                return DecisionResult(p.status, True, p.order_id)
            p.decided_at, p.decided_via, p.decided_by = now, via, actor
            p.decision_latency_ms = int((now - p.created_at).total_seconds() * 1000)
            if decision == "reject":
                p.status = "rejected"
            else:
                p.status = "approved"
                self._execute(s, p)
            s.add(
                m.AuditLog(
                    ts=now,
                    actor=actor,
                    action=f"proposal.{decision}",
                    before={"proposal_id": p.id, "status": "pending"},
                    after={"status": p.status, "via": via, "order_id": p.order_id},
                )
            )
            self._log(s, "info", f"proposal {p.id} {decision}d via {via}: {p.status}", p, actor=actor)
            return DecisionResult(p.status, False, p.order_id)

    def expire_due(self, now: datetime) -> list[m.Proposal]:
        settings = self._settings.load()
        with session_scope(self._factory) as s:
            rows = list(
                s.execute(
                    select(m.Proposal)
                    .where(m.Proposal.run_id == self.run_id, m.Proposal.status == "pending", m.Proposal.expires_at <= now)
                    .order_by(m.Proposal.id)
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            for p in rows:
                self._expire(s, p, now, settings)
            return rows

    def escalate_unprotected(self, now: datetime) -> list[m.Proposal]:
        interval = timedelta(seconds=self._settings.load().stop_escalation_seconds)
        out: list[m.Proposal] = []
        with session_scope(self._factory) as s:
            rows = s.execute(
                select(m.Proposal)
                .where(
                    m.Proposal.run_id == self.run_id,
                    m.Proposal.kind == "stop",
                    m.Proposal.status == "expired",
                    m.Proposal.escalated_at <= now - interval,
                )
                .order_by(m.Proposal.id)
                .with_for_update(skip_locked=True)
            ).scalars()
            for p in rows:
                pos = s.get(m.Position, p.position_id) if p.position_id is not None else None
                if pos is None or pos.closed_at is not None or pos.stop_order_id is not None:
                    continue
                p.escalations += 1
                p.escalated_at = now
                since = pos.unprotected_since
                unprotected = int((now - since).total_seconds()) + pos.unprotected_seconds if since else None
                self._log(s, "error", f"position {pos.id} is still unprotected (alert {p.escalations})", p,
                          unprotected_seconds=unprotected)
                out.append(p)
        return out

    # --- internals -----------------------------------------------------------------------------------------
    def _execute(self, s: Session, p: m.Proposal) -> None:
        try:
            if p.kind == "cancel":
                order_id = p.cancel_order_id
                if order_id is None or not self._broker.cancel(order_id, str(p.order_spec.get("reason", "")), session=s):
                    raise BrokerRejected(f"order {order_id} is no longer working")
            else:
                spec = dataclasses.replace(OrderSpec.from_json(p.order_spec), proposal_id=p.id)
                p.order_id = self._broker.submit(spec, session=s)
            p.status = "submitted"
        except BrokerRejected as exc:
            p.status, p.error = "failed", str(exc)
            self._log(s, "error", f"proposal {p.id} could not be executed: {exc}", p)

    def _expire(self, s: Session, p: m.Proposal, now: datetime, settings: RuntimeSettings) -> None:
        p.status, p.expired_at = "expired", now
        if p.kind == "stop":
            p.escalated_at, p.escalations = now, 1
            self._log(s, "error", f"protective stop proposal {p.id} expired: position {p.position_id} has no stop", p)
        elif p.kind in ("exit", "cancel") and settings.auto_flatten_on_expiry:
            # BR-42: an unanswered flatten, or an unanswered cancel of a working entry, executes by itself
            p.decided_at, p.decided_via, p.decided_by = now, "auto", "auto_flatten_on_expiry"
            self._execute(s, p)
            self._log(s, "warning", f"{p.kind} proposal {p.id} expired and was executed automatically", p)
        elif p.kind == "exit":
            p.escalated_at, p.escalations = now, 1
            self._log(s, "error", f"exit proposal {p.id} expired: position {p.position_id} is still open", p)
        else:
            self._log(s, "info", f"proposal {p.id} ({p.kind}) expired", p)
```

- [ ] **Step 4: Run the tests**

Run: `uv --directory Trader/app run pytest tests/engine/test_proposals.py -q`
Expected: all pass. In `test_an_expired_protective_stop_escalates_and_counts_unprotected_time`, the position has no stop for its whole 600 s life, so `unprotected_seconds == 600`.

- [ ] **Step 5: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/engine/proposals.py Trader/app/tests/engine/test_proposals.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T11: proposal service (first decision wins, TTL expiry, auto flatten, unprotected-stop escalation)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T12: Claude catalyst classifier, store and service

**Files:**
- Modify: `Trader/app/pyproject.toml` (add the `anthropic` dependency), `Trader/app/uv.lock` (via `uv add`)
- Create: `Trader/app/trader/adapters/claude/__init__.py`, `Trader/app/trader/adapters/claude/catalyst.py`, `Trader/app/tests/adapters/test_claude_catalyst.py`

**Interfaces:**
- Consumes: `RuntimeSettings.claude_model`, `claude_daily_budget_usd` (P2-T2); `Headline(ts, title, source, url)` and `FinvizError` (P1-T5; `FinvizScraper.news(ticker, today_et) -> list[Headline]`); models `Catalyst`, `Symbol` (P2-T1, P1-T2); `log_event` (P1-T9); `et_date` (P1-T4); `tests.factories.add_symbol` (P2-T1).
- Produces (`trader.adapters.claude.catalyst`):
  - `CATALYST_TYPES`, `DIRECTIONS`, `PRICES_PER_MTOK = {"claude-sonnet-5": (2, 10), "claude-haiku-4-5": (1, 5)}` (USD per million input/output tokens), `MAX_HEADLINES = 10`, `OVER_CAP = "not classified (over cap)"`.
  - `CatalystResult` (pydantic, SPEC §4.3 output: `catalyst_type`, `direction`, `quality` 0–100, `is_confirmed`, `reason` trimmed to 30 words); `CATALYST_SCHEMA` (the JSON schema sent as `output_config.format`); `SYSTEM_PROMPT`; `build_prompt(inp) -> str`.
  - `CatalystInput(ticker, company, headlines: tuple[Headline, ...], gap_pct: Decimal | None, earnings_date: date | None)`.
  - `Classification(status: Literal["classified","budget_exceeded","error"], result: CatalystResult | None, model: str, input_tokens=0, output_tokens=0, cost_usd=0, error=None)`; `cost_usd(model, input_tokens, output_tokens) -> Decimal` (6 dp).
  - `CatalystClassifier(client: Any, settings: Callable[[], RuntimeSettings])`: `async classify(inp, spent_usd: Decimal) -> Classification`. `client` is an `anthropic.AsyncAnthropic` (or a test double with `messages.create`). It makes no call when `spent_usd >= claude_daily_budget_usd`. The request is `messages.create(model=<claude.model>, max_tokens=1024, thinking={"type": "disabled"}, system=SYSTEM_PROMPT, messages=[user prompt], output_config={"format": {"type": "json_schema", "schema": CATALYST_SCHEMA}})`. Any exception, a `stop_reason` other than `end_turn`, or output that fails validation gives `status="error"` (with the cost of the call if one was made).
  - `StoredCatalyst(symbol_id, catalyst_type, direction, quality, confirmed, reason, model, cost_usd, classified)`: satisfies the strategies' `CatalystInfo` protocol (P2-T6).
  - `CatalystStore(factory, clock)`: `spent(session_date) -> Decimal` (sum of `cost_usd` for the session), `get(symbol_ids, session_date) -> dict[int, StoredCatalyst]`, `save(symbol_id, session_date, *, headlines, gap_pct, earnings_date, classification: Classification | None, note: str | None = None) -> StoredCatalyst` (upsert on `(symbol_id, session_date)`; an unclassified result is stored as `catalyst_type="unknown"`, `direction="neutral"` with the reason; a classified row is never overwritten; costs and tokens accumulate).
  - `CatalystRequest(symbol_id, ticker, company="", gap_pct=None, earnings_date=None)`; `HeadlineSource` protocol (`news(ticker, today_et) -> list[Headline]`).
  - `CatalystService(factory, clock, store, classifier: CatalystClassifier | None, headlines: HeadlineSource | None, *, max_concurrency=4)`: `async get(symbol_ids, session_date) -> dict[int, StoredCatalyst]` (implements `CatalystSource`: stored first, classifying only names without a classified row, which is the 9:35 path); `async classify_many(requests, session_date) -> dict[int, StoredCatalyst]` (headlines fetched one at a time, because FinViz is polite and the scraper isn't thread-safe; Claude calls run up to `max_concurrency` at once; the budget is re-read before each call, so it can be overshot by at most `max_concurrency − 1` calls; a budget stop logs an `error` event `claude.catalyst`); `mark_unclassified(requests, session_date, note) -> dict[int, StoredCatalyst]`. With no classifier or no headline source, names are stored as `unknown` with the note `claude not configured`. A headline failure (`FinvizError`) stores `unknown` with `headlines unavailable: ...` and makes no Claude call.

- [x] **Step 1: Add the dependencies**

Run: `uv --directory Trader/app add "anthropic>=1.0"` then `uv --directory Trader/app add --dev "httpx2>=2.0"`.
Expected: `pyproject.toml` gains `anthropic` in `dependencies` and `httpx2` in the dev group, and `uv.lock` is updated. `anthropic` 1.x does its HTTP through `httpx2` (the maintained fork of `httpx`, published by Pydantic), not `httpx`, so `respx` can't see its requests: the SDK-shape test below fakes the transport with `httpx2.MockTransport` instead. `httpx2` arrives with `anthropic`; it is declared in the dev group because the test imports it.

- [x] **Step 2: Write the failing tests**

`Trader/app/tests/adapters/test_claude_catalyst.py`:
```python
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from trader.adapters.claude.catalyst import (
    CATALYST_SCHEMA,
    OVER_CAP,
    CatalystClassifier,
    CatalystInput,
    CatalystRequest,
    CatalystResult,
    CatalystService,
    CatalystStore,
    Classification,
    cost_usd,
)
from trader.adapters.finviz.parser import Headline
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

DAY = date(2026, 10, 6)
CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))  # 08:00 ET
GOOD = {
    "catalyst_type": "earnings_beat",
    "direction": "bullish",
    "quality": 82,
    "is_confirmed": True,
    "reason": "Q3 EPS beat estimates by 12% and the company raised full-year guidance.",
}
HEADLINE = Headline(datetime(2026, 10, 6, 11, 5, tzinfo=UTC), "AAA beats Q3 estimates, raises guidance", "Reuters",
                    "https://example.com/a")
INPUT = CatalystInput("AAA", "AAA Inc", (HEADLINE,), Decimal("0.0520"), DAY)


def reply(payload: dict[str, Any] | str, *, tin: int = 1000, tout: int = 100, stop: str = "end_turn") -> Any:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
        stop_reason=stop,
    )


class FakeMessages:
    def __init__(self, replies: Sequence[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClient:
    def __init__(self, *replies: Any) -> None:
        self.messages = FakeMessages(replies)


def classifier(client: Any, **settings: Any) -> CatalystClassifier:
    s = RuntimeSettings(**settings)
    return CatalystClassifier(client, lambda: s)


async def test_structured_output_is_parsed_and_costed() -> None:
    client = FakeClient(reply(GOOD))
    c = await classifier(client).classify(INPUT, Decimal("0"))
    assert c.status == "classified" and c.result is not None
    assert (c.result.catalyst_type, c.result.direction, c.result.quality) == ("earnings_beat", "bullish", 82)
    assert c.model == "claude-sonnet-5" and (c.input_tokens, c.output_tokens) == (1000, 100)
    assert c.cost_usd == Decimal("0.003000")  # 1000 x $2/M + 100 x $10/M
    (kw,) = client.messages.calls
    assert kw["model"] == "claude-sonnet-5" and kw["thinking"] == {"type": "disabled"}
    assert kw["output_config"] == {"format": {"type": "json_schema", "schema": CATALYST_SCHEMA}}
    prompt = kw["messages"][0]["content"]
    assert "AAA" in prompt and "beats Q3 estimates" in prompt and "+5.20%" in prompt and "2026-10-06" in prompt


async def test_the_model_comes_from_settings() -> None:
    client = FakeClient(reply(GOOD))
    c = await classifier(client, claude_model="claude-haiku-4-5").classify(INPUT, Decimal("0"))
    assert client.messages.calls[0]["model"] == "claude-haiku-4-5"
    assert c.cost_usd == Decimal("0.001500") == cost_usd("claude-haiku-4-5", 1000, 100)


@pytest.mark.parametrize(
    "payload",
    [
        {**GOOD, "quality": 150},
        {**GOOD, "catalyst_type": "bogus"},
        {**GOOD, "direction": "up"},
        {k: v for k, v in GOOD.items() if k != "reason"},
        "not json",
    ],
)
async def test_invalid_output_is_an_error_but_still_costed(payload: dict[str, Any] | str) -> None:
    c = await classifier(FakeClient(reply(payload))).classify(INPUT, Decimal("0"))
    assert c.status == "error" and c.result is None and c.cost_usd == Decimal("0.003000")


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
async def test_a_refusal_or_truncation_is_an_error(stop: str) -> None:
    c = await classifier(FakeClient(reply(GOOD, stop=stop))).classify(INPUT, Decimal("0"))
    assert c.status == "error" and stop in (c.error or "")


async def test_an_api_exception_is_an_error() -> None:
    c = await classifier(FakeClient(RuntimeError("overloaded"))).classify(INPUT, Decimal("0"))
    assert c.status == "error" and c.cost_usd == 0 and "overloaded" in (c.error or "")


def test_reason_is_trimmed_to_30_words() -> None:
    r = CatalystResult.model_validate({**GOOD, "reason": " ".join(f"w{i}" for i in range(45))})
    assert len(r.reason.split()) == 30


async def test_the_budget_stops_calls() -> None:
    client = FakeClient(reply(GOOD))
    c = await classifier(client).classify(INPUT, Decimal("1.00"))  # default budget US$1.00
    assert c.status == "budget_exceeded" and client.messages.calls == []


async def test_the_real_sdk_accepts_the_request_shape() -> None:
    """No network: a fake httpx2 transport answers the SDK. Catches SDK drift in the request parameters."""
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": json.dumps(GOOD)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 100},
    }
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/messages"
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=body)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    sdk = anthropic.AsyncAnthropic(
        api_key="test-key-not-real", base_url="https://claude.test", max_retries=0, http_client=http
    )
    c = await classifier(sdk).classify(INPUT, Decimal("0"))
    await sdk.close()
    assert c.status == "classified" and c.cost_usd == Decimal("0.003000"), c.error
    (req,) = sent
    assert req["model"] == "claude-sonnet-5" and req["output_config"]["format"]["type"] == "json_schema"
    assert req["thinking"] == {"type": "disabled"}


# --- store and service (database) ---------------------------------------------------------------------------


class FakeHeadlines:
    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.calls: list[tuple[str, date]] = []

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        self.calls.append((ticker, today_et))
        if ticker in self.failing:
            raise FinvizBlocked("HTTP 403")
        return [HEADLINE, Headline(HEADLINE.ts - timedelta(days=1), f"{ticker} older news", "PR", "https://x")]


def _classified(model: str = "claude-sonnet-5") -> Classification:
    return Classification("classified", CatalystResult.model_validate(GOOD), model, 1000, 100, Decimal("0.003"))


@pytest.mark.db
def test_store_protects_classified_rows_and_accumulates_cost(db_factory: sessionmaker[Session]) -> None:
    store = CatalystStore(db_factory, CLOCK)
    with db_factory() as s:
        aaa, bbb = add_symbol(s, "AAA"), add_symbol(s, "BBB")
        s.commit()
    err = Classification("error", None, "claude-sonnet-5", 10, 0, Decimal("0.001"), "bad output")
    first = store.save(bbb, DAY, headlines=[HEADLINE], gap_pct=None, earnings_date=None, classification=err)
    assert (first.catalyst_type, first.direction, first.classified, first.reason) == ("unknown", "neutral", False, "bad output")
    store.save(bbb, DAY, headlines=[HEADLINE], gap_pct=None, earnings_date=None, classification=_classified())
    saved = store.save(aaa, DAY, headlines=[HEADLINE], gap_pct=Decimal("0.05"), earnings_date=DAY,
                       classification=_classified())
    again = store.save(aaa, DAY, headlines=[], gap_pct=None, earnings_date=None, classification=None, note=OVER_CAP)
    assert saved.classified and again.classified and again.catalyst_type == "earnings_beat"
    assert store.get([bbb], DAY)[bbb].classified is True
    assert store.spent(DAY) == Decimal("0.007000")  # 0.001 + 0.003 + 0.003
    with db_factory() as s:
        row = s.execute(select(m.Catalyst).where(m.Catalyst.symbol_id == aaa)).scalar_one()
    assert row.headlines[0]["title"] == HEADLINE.title and row.gap_pct == Decimal("0.0500") and row.quality == 82


@pytest.fixture
def symbols(db_factory: sessionmaker[Session]) -> dict[str, int]:
    with db_factory() as s:
        out = {t: add_symbol(s, t) for t in ("AAA", "BBB", "CCC")}
        s.commit()
    return out


@pytest.mark.db
async def test_get_classifies_only_the_missing_names(db_factory: sessionmaker[Session], symbols: dict[str, int]) -> None:
    store = CatalystStore(db_factory, CLOCK)
    store.save(symbols["AAA"], DAY, headlines=[], gap_pct=None, earnings_date=None, classification=_classified())
    client, heads = FakeClient(reply(GOOD)), FakeHeadlines()
    svc = CatalystService(db_factory, CLOCK, store, classifier(client), heads)
    got = await svc.get([symbols["AAA"], symbols["BBB"]], DAY)
    assert set(got) == {symbols["AAA"], symbols["BBB"]} and all(c.classified for c in got.values())
    assert heads.calls == [("BBB", DAY)] and len(client.messages.calls) == 1
    assert "BBB Inc" in client.messages.calls[0]["messages"][0]["content"]
    assert await svc.get([symbols["BBB"]], DAY) == {symbols["BBB"]: got[symbols["BBB"]]}  # no second call
    assert len(client.messages.calls) == 1


@pytest.mark.db
async def test_the_budget_marks_unknown_and_alerts(db_factory: sessionmaker[Session], symbols: dict[str, int]) -> None:
    store = CatalystStore(db_factory, CLOCK)
    client = FakeClient(reply(GOOD), reply(GOOD), reply(GOOD))
    svc = CatalystService(db_factory, CLOCK, store, classifier(client, claude_daily_budget_usd=Decimal("0.004")),
                          FakeHeadlines(), max_concurrency=1)
    reqs = [CatalystRequest(symbols[t], t, f"{t} Inc") for t in ("AAA", "BBB", "CCC")]
    got = await svc.classify_many(reqs, DAY)
    assert len(client.messages.calls) == 2  # spent 0, then 0.003 < 0.004, then 0.006 >= 0.004
    assert got[symbols["CCC"]].catalyst_type == "unknown" and not got[symbols["CCC"]].classified
    with db_factory() as s:
        alerts = s.execute(select(m.EventLog).where(m.EventLog.source == "claude.catalyst")).scalars().all()
    assert [a.level for a in alerts] == ["error"] and "budget" in alerts[0].message


@pytest.mark.db
async def test_headline_failures_mark_unknown_without_calling_claude(
    db_factory: sessionmaker[Session], symbols: dict[str, int]
) -> None:
    store = CatalystStore(db_factory, CLOCK)
    client = FakeClient(reply(GOOD))
    svc = CatalystService(db_factory, CLOCK, store, classifier(client), FakeHeadlines(frozenset({"BBB"})))
    got = await svc.classify_many([CatalystRequest(symbols["AAA"], "AAA"), CatalystRequest(symbols["BBB"], "BBB")], DAY)
    assert got[symbols["AAA"]].classified and not got[symbols["BBB"]].classified
    assert (got[symbols["BBB"]].reason or "").startswith("headlines unavailable")
    assert len(client.messages.calls) == 1


@pytest.mark.db
async def test_over_cap_and_unconfigured_names_are_unknown(db_factory: sessionmaker[Session], symbols: dict[str, int]) -> None:
    store = CatalystStore(db_factory, CLOCK)
    store.save(symbols["AAA"], DAY, headlines=[], gap_pct=None, earnings_date=None, classification=_classified())
    svc = CatalystService(db_factory, CLOCK, store, None, None)
    marked = svc.mark_unclassified([CatalystRequest(symbols["AAA"], "AAA"), CatalystRequest(symbols["BBB"], "BBB")],
                                   DAY, OVER_CAP)
    assert marked[symbols["AAA"]].classified  # a classified row is never overwritten
    assert marked[symbols["BBB"]].reason == OVER_CAP and marked[symbols["BBB"]].catalyst_type == "unknown"
    got = await svc.get([symbols["CCC"]], DAY)
    assert got[symbols["CCC"]].reason == "claude not configured"
```

- [x] **Step 3: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/adapters/test_claude_catalyst.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.adapters.claude'`.

- [x] **Step 4: Implement `trader/adapters/claude/catalyst.py`**

`Trader/app/trader/adapters/claude/__init__.py`: empty file.

`Trader/app/trader/adapters/claude/catalyst.py`:
```python
"""Claude catalyst classification (SPEC §4.3, BR-03, BR-05), its store, and the service strategies call.

The Anthropic client is injected (tests never reach the network). Every call's cost is stored, and the daily
budget (claude.daily_budget_usd, per session) stops further calls: the name is stored as `unknown` and an
error event is logged. Replay (P5) passes classifier=None so it never calls Claude.
"""

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.parser import Headline
from trader.adapters.finviz.scraper import FinvizError
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock, et_date
from trader.settings_store import RuntimeSettings

CatalystType = Literal[
    "earnings_beat",
    "earnings_miss",
    "guidance",
    "analyst_action",
    "m_and_a",
    "regulatory",
    "contract",
    "offering",
    "rumour",
    "none",
]
Direction = Literal["bullish", "bearish", "neutral"]
CATALYST_TYPES: tuple[str, ...] = (
    "earnings_beat",
    "earnings_miss",
    "guidance",
    "analyst_action",
    "m_and_a",
    "regulatory",
    "contract",
    "offering",
    "rumour",
    "none",
)
DIRECTIONS: tuple[str, ...] = ("bullish", "bearish", "neutral")
PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}
MAX_HEADLINES = 10
MAX_REASON_WORDS = 30
MAX_TOKENS = 1024
OVER_CAP = "not classified (over cap)"
NOT_CONFIGURED = "claude not configured"
SOURCE = "claude.catalyst"
Q6 = Decimal("0.000001")


class CatalystResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    catalyst_type: CatalystType
    direction: Direction
    quality: int = Field(ge=0, le=100)
    is_confirmed: bool
    reason: str

    @field_validator("reason")
    @classmethod
    def _thirty_words(cls, v: str) -> str:
        return " ".join(v.split()[:MAX_REASON_WORDS])


CATALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "catalyst_type": {"type": "string", "enum": list(CATALYST_TYPES)},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "quality": {"type": "integer", "description": "0-100: how strong, specific and confirmed the catalyst is"},
        "is_confirmed": {"type": "boolean"},
        "reason": {"type": "string", "description": "At most 30 words"},
    },
    "required": ["catalyst_type", "direction", "quality", "is_confirmed", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You classify the news catalyst behind a US stock's pre-market move, for a day-trading simulator. "
    "Use only the headlines given; some may be about other companies, so ignore those. "
    "catalyst_type is 'none' when no headline explains a move. direction is the likely price impact. "
    "quality is 0-100: 80 or more for a confirmed, company-specific, material event; 50-79 for plausible but "
    "weaker news; below 50 for vague, stale, or unconfirmed news. is_confirmed is true only when a headline "
    "states the event as fact. reason is at most 30 words and must not invent facts or numbers."
)


@dataclass(frozen=True, slots=True)
class CatalystInput:
    ticker: str
    company: str
    headlines: tuple[Headline, ...]
    gap_pct: Decimal | None
    earnings_date: date | None


def build_prompt(inp: CatalystInput) -> str:
    gap = f"{inp.gap_pct * 100:+.2f}%" if inp.gap_pct is not None else "unknown"
    lines = [
        f"Ticker: {inp.ticker}",
        f"Company: {inp.company or 'unknown'}",
        f"Pre-market gap: {gap}",
        f"Earnings date: {inp.earnings_date.isoformat() if inp.earnings_date else 'none known'}",
        "Headlines (newest first, UTC):",
    ]
    newest = sorted(inp.headlines, key=lambda h: h.ts, reverse=True)[:MAX_HEADLINES]
    lines += [f"- {h.ts.isoformat()} [{h.source}] {h.title}" for h in newest] or ["- (none)"]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Classification:
    status: Literal["classified", "budget_exceeded", "error"]
    result: CatalystResult | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    error: str | None = None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    price_in, price_out = PRICES_PER_MTOK[model]
    return ((input_tokens * price_in + output_tokens * price_out) / Decimal(1_000_000)).quantize(Q6, ROUND_HALF_UP)


class CatalystClassifier:
    def __init__(self, client: Any, settings: Callable[[], RuntimeSettings]) -> None:
        self._client = client
        self._settings = settings

    async def classify(self, inp: CatalystInput, spent_usd: Decimal) -> Classification:
        s = self._settings()
        model = s.claude_model
        if spent_usd >= s.claude_daily_budget_usd:
            return Classification(
                "budget_exceeded", None, model, error=f"daily budget US${s.claude_daily_budget_usd} reached"
            )
        try:
            resp = await self._client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "disabled"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_prompt(inp)}],
                output_config={"format": {"type": "json_schema", "schema": CATALYST_SCHEMA}},
            )
        except Exception as exc:  # the SDK raises many error types; any failure means "unknown"
            return Classification("error", None, model, error=f"{type(exc).__name__}: {exc}"[:300])
        usage = getattr(resp, "usage", None)
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        cost = cost_usd(model, tin, tout)
        stop = getattr(resp, "stop_reason", None)
        if stop != "end_turn":
            return Classification("error", None, model, tin, tout, cost, f"stop_reason={stop}")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        try:
            result = CatalystResult.model_validate_json(text or "")
        except ValidationError as exc:
            return Classification("error", None, model, tin, tout, cost, f"invalid output: {exc.error_count()} errors")
        return Classification("classified", result, model, tin, tout, cost)


@dataclass(frozen=True, slots=True)
class StoredCatalyst:
    symbol_id: int
    catalyst_type: str
    direction: str
    quality: int | None
    confirmed: bool | None
    reason: str | None
    model: str | None
    cost_usd: Decimal
    classified: bool


def _stored(row: m.Catalyst) -> StoredCatalyst:
    return StoredCatalyst(
        row.symbol_id,
        row.catalyst_type,
        row.direction,
        row.quality,
        row.confirmed,
        row.reason,
        row.model,
        row.cost_usd,
        row.classified_at is not None,
    )


def _headlines_json(headlines: Iterable[Headline]) -> list[dict[str, str]]:
    return [{"ts": h.ts.isoformat(), "title": h.title, "source": h.source, "url": h.url} for h in headlines]


class CatalystStore:
    def __init__(self, factory: sessionmaker[Session], clock: Clock) -> None:
        self._factory = factory
        self._clock = clock

    def spent(self, session_date: date) -> Decimal:
        with self._factory() as s:
            total = s.execute(
                select(func.coalesce(func.sum(m.Catalyst.cost_usd), 0)).where(m.Catalyst.session_date == session_date)
            ).scalar_one()
        return Decimal(total)

    def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, StoredCatalyst]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Catalyst).where(m.Catalyst.session_date == session_date, m.Catalyst.symbol_id.in_(list(symbol_ids)))
            ).scalars()
            return {r.symbol_id: _stored(r) for r in rows}

    def save(
        self,
        symbol_id: int,
        session_date: date,
        *,
        headlines: Iterable[Headline],
        gap_pct: Decimal | None,
        earnings_date: date | None,
        classification: Classification | None,
        note: str | None = None,
    ) -> StoredCatalyst:
        now = self._clock.now()
        res = classification.result if classification else None
        # Core insert: keys are column names. The ORM attribute catalyst_type maps to the column "type".
        values: dict[str, Any] = {
            "symbol_id": symbol_id,
            "session_date": session_date,
            "headlines": _headlines_json(headlines),
            "gap_pct": gap_pct,
            "earnings_date": earnings_date,
            "type": res.catalyst_type if res else "unknown",
            "direction": res.direction if res else "neutral",
            "quality": res.quality if res else None,
            "confirmed": res.is_confirmed if res else None,
            "reason": res.reason if res else (note or (classification.error if classification else None)),
            "model": classification.model if classification else None,
            "cost_usd": classification.cost_usd if classification else Decimal(0),
            "input_tokens": classification.input_tokens if classification else 0,
            "output_tokens": classification.output_tokens if classification else 0,
            "classified_at": now if res else None,
            "created_at": now,
        }
        stmt = pg_insert(m.Catalyst).values(**values)
        replace = ("headlines", "gap_pct", "earnings_date", "type", "direction", "quality", "confirmed",
                   "reason", "model", "classified_at")
        set_: dict[str, Any] = {k: stmt.excluded[k] for k in replace}
        set_["cost_usd"] = m.Catalyst.cost_usd + stmt.excluded["cost_usd"]
        set_["input_tokens"] = func.coalesce(m.Catalyst.input_tokens, 0) + stmt.excluded["input_tokens"]
        set_["output_tokens"] = func.coalesce(m.Catalyst.output_tokens, 0) + stmt.excluded["output_tokens"]
        with session_scope(self._factory) as s:
            s.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_catalysts_symbol_session",
                    set_=set_,
                    where=m.Catalyst.classified_at.is_(None),  # a classified row is never overwritten
                )
            )
        return self.get([symbol_id], session_date)[symbol_id]


@dataclass(frozen=True, slots=True)
class CatalystRequest:
    symbol_id: int
    ticker: str
    company: str = ""
    gap_pct: Decimal | None = None
    earnings_date: date | None = None


class HeadlineSource(Protocol):
    def news(self, ticker: str, today_et: date) -> list[Headline]: ...


class CatalystService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        store: CatalystStore,
        classifier: CatalystClassifier | None,
        headlines: HeadlineSource | None,
        *,
        max_concurrency: int = 4,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._store = store
        self._classifier = classifier
        self._headlines = headlines
        self._max = max_concurrency

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, StoredCatalyst]:
        wanted = list(dict.fromkeys(symbol_ids))
        stored = self._store.get(wanted, session_date)
        todo = [sid for sid in wanted if sid not in stored or not stored[sid].classified]
        if todo:
            stored.update(await self.classify_many(self._requests(todo, session_date), session_date))
        return {sid: stored[sid] for sid in wanted if sid in stored}

    def _requests(self, symbol_ids: Sequence[int], session_date: date) -> list[CatalystRequest]:
        with self._factory() as s:
            names = {sid: (t, n) for sid, t, n in s.execute(
                select(m.Symbol.id, m.Symbol.ticker, m.Symbol.name).where(m.Symbol.id.in_(list(symbol_ids)))
            ).all()}
            known = {sid: (g, e) for sid, g, e in s.execute(
                select(m.Catalyst.symbol_id, m.Catalyst.gap_pct, m.Catalyst.earnings_date).where(
                    m.Catalyst.session_date == session_date, m.Catalyst.symbol_id.in_(list(symbol_ids))
                )
            ).all()}
        return [
            CatalystRequest(sid, names[sid][0], names[sid][1] or "", *known.get(sid, (None, None)))
            for sid in symbol_ids
            if sid in names
        ]

    def mark_unclassified(
        self, requests: Sequence[CatalystRequest], session_date: date, note: str
    ) -> dict[int, StoredCatalyst]:
        return {
            r.symbol_id: self._store.save(
                r.symbol_id, session_date, headlines=(), gap_pct=r.gap_pct, earnings_date=r.earnings_date,
                classification=None, note=note,
            )
            for r in requests
        }

    async def classify_many(
        self, requests: Sequence[CatalystRequest], session_date: date
    ) -> dict[int, StoredCatalyst]:
        existing = self._store.get([r.symbol_id for r in requests], session_date)
        out = {sid: c for sid, c in existing.items() if c.classified}
        pending = [r for r in requests if r.symbol_id not in out]
        if not pending:
            return out
        if self._classifier is None or self._headlines is None:
            out.update(self.mark_unclassified(pending, session_date, NOT_CONFIGURED))
            return out
        classifier, source = self._classifier, self._headlines
        today = et_date(self._clock.now())
        inputs: list[tuple[CatalystRequest, tuple[Headline, ...]]] = []
        for r in pending:  # one at a time: FinViz politeness, and the scraper isn't thread-safe
            try:
                found = await asyncio.to_thread(source.news, r.ticker, today)
            except FinvizError as exc:
                out.update(self.mark_unclassified([r], session_date, f"headlines unavailable: {exc}"))
                continue
            inputs.append((r, tuple(sorted(found, key=lambda h: h.ts, reverse=True)[:MAX_HEADLINES])))
        gate = asyncio.Semaphore(self._max)
        budget_lock = asyncio.Lock()

        async def one(r: CatalystRequest, heads: tuple[Headline, ...]) -> None:
            async with gate:
                async with budget_lock:
                    spent = self._store.spent(session_date)
                c = await classifier.classify(CatalystInput(r.ticker, r.company, heads, r.gap_pct, r.earnings_date), spent)
                out[r.symbol_id] = self._store.save(
                    r.symbol_id, session_date, headlines=heads, gap_pct=r.gap_pct, earnings_date=r.earnings_date,
                    classification=c,
                )
                if c.status != "classified":
                    self._event(
                        "error" if c.status == "budget_exceeded" else "warning",
                        f"Claude daily budget reached: {r.ticker} not classified"
                        if c.status == "budget_exceeded"
                        else f"catalyst for {r.ticker} not classified: {c.error}",
                        {"ticker": r.ticker, "status": c.status, "spent_usd": str(spent)},
                    )

        await asyncio.gather(*(one(r, heads) for r, heads in inputs))
        return out

    def _event(self, level: str, message: str, data: dict[str, Any]) -> None:
        with session_scope(self._factory) as s:
            log_event(s, self._clock, level, SOURCE, message, data)
```

`CatalystResult.model_validate_json` rejects an extra or a missing field, a quality outside 0–100, an unknown type or direction, and text that isn't JSON; all of those become `status="error"`.

- [x] **Step 5: Run the tests**

Run: `uv --directory Trader/app run pytest tests/adapters/test_claude_catalyst.py -q`
Expected: all pass. If `test_the_real_sdk_accepts_the_request_shape` fails, its message shows `c.error`: a `TypeError` naming a parameter means the installed SDK doesn't accept the documented request shape, so check `uv --directory Trader/app pip show anthropic` is 1.x before changing any code.

- [x] **Step 6: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/pyproject.toml Trader/app/uv.lock Trader/app/trader/adapters/claude/__init__.py Trader/app/trader/adapters/claude/catalyst.py Trader/app/tests/adapters/test_claude_catalyst.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T12: Claude catalyst classifier with structured output, per-call cost, daily budget and store" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T13: Engine orchestrator

**Files:**
- Create: `Trader/app/trader/engine/orchestrator.py`, `Trader/app/tests/engine/test_orchestrator.py`

**Interfaces:**
- Consumes: everything above. `StrategyRegistry` (`enabled`, `instance`, `config_ids`, `config_key`, `keys`, `plugin_class`), `StrategyContext`, `intent_to_json`, `EnterLong`, `MarketDataView`, `CatalystSource` (P2-T6); `MarketDataService`, `QuoteClient` (P2-T7); `OrbSip` event keys (P2-T8), `SpyOverlay` (P2-T9); `RiskManager`, `RiskContext`, `Rejection` (P2-T10); `KillSwitches` (P2-T10); `ProposalService` (P2-T11); `SimBroker`, `Ledger`, `QuoteFillModel`, `FillParams` (P2-T3–T5); `get_live_run` (P2-T2); `Core` (P1-T6 `trader.bootstrap`); `log_event` (P1-T9); `FakeQuestrade` (P2-T7), `FakeCatalysts`/`FakeCatalyst` (P2-T6) in tests.
- Produces (`trader.engine.orchestrator`):
  - `IntentOutcome(signal_id, intent, status, proposal_id=None, rejection=None)` (`status` is the proposal's status, or `rejected_by_risk`); `EventResult(event_key, session_date, strategies=[], outcomes=[])`.
  - `Engine(*, factory, clock, calendar, settings: SettingsStore, registry, data: MarketDataView, catalysts: CatalystSource, broker: SimBroker, proposals: ProposalService, risk: RiskManager, killswitches: KillSwitches, run_id: int)`:
    - `async run_event(event_key, session_date) -> EventResult`: for every enabled strategy whose `schedule()` has that key, builds the context, calls `on_event`, persists its candidates (upsert on `(run_id, session_date, strategy_key, symbol_id)`) and notes (`event_log`, source `strategy.<key>`), then handles each intent. P3-T1's `fire_event(key, session_date)` calls this.
    - Handling an intent: save a `signals` row (run, strategy config revision id, symbol, event key, `intent_to_json`, evidence), build the `RiskContext` (for an entry: evaluate the kill switches first, then `blocking`, `daily_pnl_pct`, `entries_today` for that strategy, `max_positions` from its params, the symbol's market from its currency, the ask as the reference price for market entries), evaluate, and either record the rejection on the signal (`evidence.rejection`) with a `warning` event (source `risk`), or record `evidence.sizing` and create the proposal.
    - `async on_quotes(quotes, now) -> list[FillEvent]` and `async poll_quotes() -> list[FillEvent]` (quotes for the symbols with working orders, then `on_quotes`). After each fill: an equity snapshot, a kill-switch evaluation, then `on_fill` of the strategy that owns the position (by `strategy_config_id`, even if that strategy has since been disabled) and its intents handled with event key `fill:<fill_id>`.
    - `async tick(now) -> None`: `expire_due(now)` then `escalate_unprotected(now)`.
    - `async end_of_session(session_date) -> list[PositionView]`: cancels working orders, snapshots equity, and returns positions still open, logging a `critical` event when there are any (BR-42 safety net).
  - `build_engine(core: Core, client: QuoteClient, catalysts: CatalystSource) -> Engine`: the live run, a registry with defaults, the market-data service, a sim broker with fill params from settings and the entry cutoff read live from the settings store (`calendar=core.calendar, settings=core.settings.load`). P3's worker uses it; the worker rebuilds it each session so fill settings changes apply.
  - Contexts: an entry strategy sees its own open positions and working orders (every revision of its config); an overlay sees every open position of the entry strategies. `entries_today` counts the strategy's entry proposals for the session in `pending`, `approved`, `auto_approved` or `submitted`, so a re-fired event can't open a second position.

- [ ] **Step 1: Write the failing tests**

`Trader/app/tests/engine/test_orchestrator.py`:
```python
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from tests.strategies.fakes import FakeCatalyst, FakeCatalysts
from trader.bootstrap import Core
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.config import EnvSettings
from trader.crypto import Crypto
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.orchestrator import Engine, build_engine
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle
from trader.settings_store import SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
OPEN = CAL.session_open(DAY)
T_ORB = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)  # 09:35:05 ET


def five(o: str, h: str, low: str, c: str, v: int) -> Candle:
    return Candle(OPEN, OPEN + timedelta(minutes=5), Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None)


@dataclass
class World:
    engine: Engine
    clock: FixedClock
    fq: FakeQuestrade
    ids: dict[str, int]
    run_id: int
    registry: StrategyRegistry
    proposals: ProposalService
    killswitches: KillSwitches
    broker: SimBroker
    factory: sessionmaker[Session]


def build(factory: sessionmaker[Session], *, auto: bool) -> World:
    clock = FixedClock(T_ORB)
    store = SettingsStore(factory, now=clock.now)
    if auto:
        store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    run = get_live_run(factory, clock, settings)
    ids: dict[str, int] = {}
    with factory() as s:
        for i, t in enumerate(("AAA", "BBB")):
            ids[t] = add_symbol(s, t, questrade_id=101 + i)
            s.add(m.UniverseSnapshot(session_date=DAY, symbol_id=ids[t], price=Decimal("21"), avg_volume=2_000_000,
                                     atr14=Decimal("1.0000"), source="finviz"))
            s.add(m.OpenBarStat(symbol_id=ids[t], session_date=DAY, avg_open_vol_14d=Decimal("1000.00"),
                                atr14=Decimal("1.0000")))
        ids["SPY"] = add_symbol(s, "SPY", questrade_id=199, exchange="ARCA")
        s.add(m.DailyCandle(symbol_id=ids["SPY"], date=date(2026, 10, 5), open=Decimal("499"), high=Decimal("501"),
                            low=Decimal("498"), close=Decimal("500.00"), volume=1, vwap=None))
        s.commit()
    fq = FakeQuestrade()
    for t, q in (("AAA", 101), ("BBB", 102), ("SPY", 199)):
        fq.add_symbol(t, q)
    fq.add_bars(101, "FiveMinutes", [five("21.00", "21.50", "20.90", "21.40", 5000)])
    fq.add_bars(102, "FiveMinutes", [five("30.00", "30.60", "29.90", "30.50", 3000)])
    registry = StrategyRegistry(factory, clock)
    registry.ensure_defaults()
    broker = SimBroker(factory, clock, Ledger(CAL), QuoteFillModel(FillParams.from_settings(settings)), run.id,
                       calendar=CAL, settings=store.load)
    proposals = ProposalService(factory, clock, store, broker, run.id)
    ks = KillSwitches(factory, clock)
    engine = Engine(
        factory=factory,
        clock=clock,
        calendar=CAL,
        settings=store,
        registry=registry,
        data=MarketDataService(factory, clock, CAL, fq),
        catalysts=FakeCatalysts({ids["AAA"]: FakeCatalyst()}),
        broker=broker,
        proposals=proposals,
        risk=RiskManager(CAL),
        killswitches=ks,
        run_id=run.id,
    )
    return World(engine, clock, fq, ids, run.id, registry, proposals, ks, broker, factory)


async def fill_entry(w: World) -> None:
    await w.engine.run_event("orb_open", DAY)
    w.clock.set(T_ORB + timedelta(seconds=55))
    w.fq.set_quote(101, "21.52", "21.55", "21.53", w.clock.now())
    assert len(await w.engine.poll_quotes()) == 1


async def test_intent_to_fill_to_protective_stop_end_to_end(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    res = await w.engine.run_event("orb_open", DAY)
    assert res.strategies == ["orb_sip"]
    (out,) = res.outcomes
    assert out.status == "submitted" and out.proposal_id is not None
    (order,) = w.broker.working_orders()
    # 720 / (21.51 x 1.005) = 33.3 -> 33 shares (cash-limited; risk allows 144)
    assert (order.symbol_id, order.order_type, order.stop, order.qty) == (w.ids["AAA"], "stop", Decimal("21.5100"), 33)
    with db_factory() as s:
        cands = {c.symbol_id: (c.passed, c.reject_reason, c.run_id) for c in s.execute(select(m.Candidate)).scalars()}
        signal = s.execute(select(m.Signal)).scalar_one()
        notes = s.execute(select(m.EventLog).where(m.EventLog.source == "strategy.orb_sip")).scalars().all()
    assert cands == {w.ids["AAA"]: (True, None, w.run_id), w.ids["BBB"]: (False, "catalyst_missing", w.run_id)}
    assert signal.strategy_config_id == w.registry.current("orb_sip").id and signal.event_key == "orb_open"
    assert signal.evidence["rvol"] == "5.0000" and signal.evidence["sizing"]["shares"] == "33"
    assert signal.intent["type"] == "enter_long" and notes

    w.clock.set(T_ORB + timedelta(seconds=55))
    w.fq.set_quote(101, "21.52", "21.55", "21.53", w.clock.now())
    (fill,) = await w.engine.poll_quotes()
    assert fill.price == Decimal("21.5608") and fill.purpose == "entry"
    (stop,) = w.broker.working_orders()
    assert (stop.purpose, stop.order_type, stop.stop, stop.qty) == ("stop", "stop", Decimal("21.4100"), 33)
    with db_factory() as s:
        kinds = [(p.kind, p.status) for p in s.execute(select(m.Proposal).order_by(m.Proposal.id)).scalars()]
        snaps = s.execute(select(func.count()).select_from(m.EquitySnapshot)).scalar_one()
    assert kinds == [("entry", "submitted"), ("stop", "submitted")] and snaps == 1


async def test_manual_mode_waits_for_a_decision(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=False)
    (out,) = (await w.engine.run_event("orb_open", DAY)).outcomes
    assert out.status == "pending" and w.broker.working_orders() == []
    assert out.proposal_id is not None
    w.proposals.decide(out.proposal_id, "approve", "web", "stephen")
    assert len(w.broker.working_orders()) == 1


async def test_a_risk_rejection_is_logged_and_kept_on_the_signal(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    w.killswitches.pause(w.run_id, DAY, actor="stephen")
    (out,) = (await w.engine.run_event("orb_open", DAY)).outcomes
    assert out.status == "rejected_by_risk" and out.rejection is not None and out.rejection.check == "kill_switch"
    with db_factory() as s:
        signal = s.execute(select(m.Signal)).scalar_one()
        proposals = s.execute(select(func.count()).select_from(m.Proposal)).scalar_one()
        warn = s.execute(select(m.EventLog).where(m.EventLog.source == "risk")).scalar_one()
    assert signal.evidence["rejection"]["check"] == "kill_switch" and proposals == 0 and warn.level == "warning"


async def test_protective_stop_placed_while_kill_switch_tripped(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 5: a tripped switch blocks entries only; the stop for a filled entry still goes in."""
    w = build(db_factory, auto=True)
    await w.engine.run_event("orb_open", DAY)
    w.killswitches.pause(w.run_id, DAY, actor="stephen")
    w.clock.set(T_ORB + timedelta(seconds=55))
    w.fq.set_quote(101, "21.52", "21.55", "21.53", w.clock.now())
    await w.engine.poll_quotes()
    assert w.killswitches.blocking(w.run_id, DAY) == "manual_pause"
    (stop,) = w.broker.working_orders()
    assert stop.purpose == "stop"


async def test_rerunning_the_orb_event_adds_nothing(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    await w.engine.run_event("orb_open", DAY)
    again = await w.engine.run_event("orb_open", DAY)
    assert again.outcomes == []
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Proposal)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(m.Candidate)).scalar_one() == 2


async def test_end_of_session_flags_open_position(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 4: a position still open at the close is cancelled-around and reported loudly."""
    w = build(db_factory, auto=True)
    await fill_entry(w)
    w.clock.set(datetime(2026, 10, 6, 20, 0, tzinfo=UTC))
    still_open = await w.engine.end_of_session(DAY)
    assert len(still_open) == 1 and w.broker.working_orders() == []
    with db_factory() as s:
        alarm = s.execute(select(m.EventLog).where(m.EventLog.level == "critical")).scalar_one()
    assert "still open" in alarm.message and alarm.run_id == w.run_id


async def test_the_overlay_exits_entry_positions_on_a_down_day(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    await fill_entry(w)
    w.clock.set(datetime(2026, 10, 6, 19, 30, tzinfo=UTC))  # 15:30 ET
    w.fq.set_quote(199, "495.00", "495.02", "495.01", w.clock.now())
    res = await w.engine.run_event("overlay_decision", DAY)
    assert res.strategies == ["spy_overlay"]
    (out,) = res.outcomes
    assert out.status == "submitted"
    with db_factory() as s:
        exit_signal = s.execute(select(m.Signal).where(m.Signal.event_key == "overlay_decision")).scalar_one()
    assert exit_signal.strategy_config_id == w.registry.current("spy_overlay").id
    assert sorted(o.purpose for o in w.broker.working_orders()) == ["exit", "stop"]


async def test_tick_expires_due_proposals(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=False)
    (out,) = (await w.engine.run_event("orb_open", DAY)).outcomes
    await w.engine.tick(T_ORB + timedelta(minutes=5))
    with db_factory() as s:
        p = s.get(m.Proposal, out.proposal_id)
    assert p is not None and p.status == "expired"


async def test_build_engine_wires_a_live_run(db_factory: sessionmaker[Session], migrated_engine: SqlEngine) -> None:
    clock = FixedClock(T_ORB)
    key = Fernet.generate_key().decode()
    env = EnvSettings(
        database_url="postgresql+psycopg://unused",
        migration_database_url="postgresql+psycopg://unused",
        app_encryption_key=key,
        session_secret="test-session-secret",
    )
    core = Core(env, migrated_engine, db_factory, Crypto(key), clock, CAL, SettingsStore(db_factory, now=clock.now))
    engine = build_engine(core, FakeQuestrade(), FakeCatalysts())
    assert engine.run_id == get_live_run(db_factory, clock, core.settings.load()).id
    with db_factory() as s:
        keys = set(s.execute(select(m.StrategyConfig.strategy_key)).scalars())
    assert keys == {"orb_sip", "spy_overlay"}
```

- [ ] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/engine/test_orchestrator.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.engine.orchestrator'`.

- [ ] **Step 3: Implement `trader/engine/orchestrator.py`**

```python
"""Engine orchestrator (SPEC §6): strategy → intent → risk → proposal → broker → fill → on_fill.

Strategies get a StrategyContext and return intents. The engine saves every signal with the strategy config
revision that produced it, sizes and checks it, turns it into a proposal (auto-approved or waiting for
Stephen), and feeds each fill back to the strategy that owns the position. It also persists the candidates
and notes strategies record.
"""

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import QtQuote
from trader.bootstrap import Core
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import AccountState, FillEvent, PositionView
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches, KillSwitchInputs
from trader.engine.proposals import ProposalService
from trader.engine.risk import Rejection, RiskContext, RiskManager
from trader.engine.runs import get_live_run
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.market.data_service import MarketDataService, QuoteClient
from trader.settings_store import Market, RuntimeSettings, SettingsStore
from trader.strategies.base import (
    CatalystSource,
    EnterLong,
    Exit,
    Intent,
    MarketDataView,
    Strategy,
    StrategyContext,
    intent_to_json,
)
from trader.strategies.registry import StrategyConfigView, StrategyRegistry

LIVE_ENTRY_STATUSES = ("pending", "approved", "auto_approved", "submitted")
SOURCE = "engine"


def _jsonable(value: Any) -> Any:
    """Decimals, dates and other odd values become strings so JSONB accepts them."""
    return json.loads(json.dumps(value, default=str))


@dataclass(frozen=True, slots=True)
class IntentOutcome:
    signal_id: int
    intent: Intent
    status: str
    proposal_id: int | None = None
    rejection: Rejection | None = None


@dataclass
class EventResult:
    event_key: str
    session_date: date
    strategies: list[str] = field(default_factory=list)
    outcomes: list[IntentOutcome] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        *,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        settings: SettingsStore,
        registry: StrategyRegistry,
        data: MarketDataView,
        catalysts: CatalystSource,
        broker: SimBroker,
        proposals: ProposalService,
        risk: RiskManager,
        killswitches: KillSwitches,
        run_id: int,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cal = calendar
        self._settings = settings
        self.registry = registry
        self._data = data
        self._catalysts = catalysts
        self.broker = broker
        self.proposals = proposals
        self._risk = risk
        self.killswitches = killswitches
        self.run_id = run_id

    # --- public entry points ---------------------------------------------------------------------------
    async def run_event(self, event_key: str, session_date: date) -> EventResult:
        result = EventResult(event_key, session_date)
        for strategy, cfg in self.registry.enabled():
            event = next((e for e in strategy.schedule(self._cal) if e.key == event_key), None)
            if event is None:
                continue
            ctx = await self._context(strategy, cfg, session_date)
            intents = await strategy.on_event(ctx, event)
            self._persist(ctx, strategy, session_date)
            result.strategies.append(strategy.key)
            result.outcomes += await self._handle(strategy, cfg, intents, session_date, event_key)
        return result

    async def on_quotes(self, quotes: Sequence[QtQuote], now: datetime) -> list[FillEvent]:
        fills = self.broker.on_quotes(quotes, now)
        for fill in fills:
            await self._after_fill(fill)
        return fills

    async def poll_quotes(self) -> list[FillEvent]:
        ids = self.broker.working_symbol_ids()
        if not ids:
            return []
        quotes = await self._data.quotes(ids)
        return await self.on_quotes(list(quotes.values()), self._clock.now())

    async def tick(self, now: datetime) -> None:
        self.proposals.expire_due(now)
        self.proposals.escalate_unprotected(now)

    async def end_of_session(self, session_date: date) -> list[PositionView]:
        self.broker.end_of_session(session_date)
        still_open = self.broker.open_positions()
        if still_open:
            with session_scope(self._factory) as s:
                log_event(
                    s,
                    self._clock,
                    "critical",
                    SOURCE,
                    f"{len(still_open)} positions still open at the end of the session (BR-42)",
                    {"position_ids": [p.id for p in still_open]},
                    run_id=self.run_id,
                )
        self.broker.snapshot_equity(self._clock.now(), await self._account(session_date))
        return still_open

    # --- fills ---------------------------------------------------------------------------------------------
    async def _after_fill(self, fill: FillEvent) -> None:
        session_date = et_date(fill.ts)
        settings = self._settings.load()
        account = await self._account(session_date, settings)
        self.broker.snapshot_equity(fill.ts, account)
        self._check_switches(session_date, account, settings)
        key = self.registry.config_key(fill.strategy_config_id) if fill.strategy_config_id is not None else None
        if key is None:
            return
        strategy, cfg = self.registry.instance(key)  # even if disabled since: its open position still needs care
        ctx = await self._context(strategy, cfg, session_date, account)
        intents = await strategy.on_fill(ctx, fill)
        self._persist(ctx, strategy, session_date)
        await self._handle(strategy, cfg, intents, session_date, f"fill:{fill.fill_id}")

    # --- context and account --------------------------------------------------------------------------
    async def _account(self, session_date: date, settings: RuntimeSettings | None = None) -> AccountState:
        settings = settings or self._settings.load()
        positions = self.broker.open_positions()
        marks: dict[int, Decimal] = {}
        if positions:
            try:
                quotes = await self._data.quotes(sorted({p.symbol_id for p in positions}))
            except QuestradeApiError:
                quotes = {}
            marks = {sid: q.last for sid, q in quotes.items() if q.last is not None}
        return self.broker.account_state(session_date, marks, settings.cash_account_mode)

    def _check_switches(self, session_date: date, account: AccountState, settings: RuntimeSettings) -> KillSwitchInputs:
        session_open = self._cal.session_open(session_date) if self._cal.is_session(session_date) else self._clock.now()
        inputs = self.killswitches.inputs(self.run_id, session_date, account, session_open)
        self.killswitches.evaluate(self.run_id, session_date, inputs, settings)
        return inputs

    def _entry_config_ids(self) -> set[int]:
        ids: set[int] = set()
        for key in self.registry.keys():
            if self.registry.plugin_class(key).kind == "entry":
                ids |= self.registry.config_ids(key)
        return ids

    def _entries_today(self, config_ids: set[int], session_date: date) -> int:
        if not config_ids:
            return 0
        with self._factory() as s:
            return int(
                s.execute(
                    select(func.count(m.Proposal.id))
                    .join(m.Signal, m.Signal.id == m.Proposal.signal_id)
                    .where(
                        m.Proposal.run_id == self.run_id,
                        m.Proposal.kind == "entry",
                        m.Proposal.status.in_(LIVE_ENTRY_STATUSES),
                        m.Signal.session_date == session_date,
                        m.Signal.strategy_config_id.in_(config_ids),
                    )
                ).scalar_one()
            )

    async def _context(
        self, strategy: Strategy, cfg: StrategyConfigView, session_date: date, account: AccountState | None = None
    ) -> StrategyContext:
        own = self.registry.config_ids(strategy.key)
        visible_ids = self._entry_config_ids() if strategy.kind == "overlay" else own
        positions = [p for p in self.broker.open_positions() if p.strategy_config_id in visible_ids]
        orders = [o for o in self.broker.working_orders() if o.strategy_config_id in own]
        return StrategyContext(
            clock=self._clock,
            calendar=self._cal,
            session_date=session_date,
            data=self._data,
            catalysts=self._catalysts,
            params=strategy.params,
            strategy_config_id=cfg.id,
            positions=positions,
            working_orders=orders,
            account=account or await self._account(session_date),
            entries_today=self._entries_today(own, session_date),
        )

    def _persist(self, ctx: StrategyContext, strategy: Strategy, session_date: date) -> None:
        if not ctx.candidates and not ctx.notes:
            return
        now = self._clock.now()
        with session_scope(self._factory) as s:
            for c in ctx.candidates:
                stmt = pg_insert(m.Candidate).values(
                    run_id=self.run_id,
                    session_date=session_date,
                    strategy_key=strategy.key,
                    symbol_id=c.symbol_id,
                    rvol=c.rvol,
                    rank=c.rank,
                    candle=_jsonable(c.candle),
                    passed=c.passed,
                    reject_reason=c.reject_reason,
                    data=_jsonable(c.data),
                    created_at=now,
                )
                s.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_candidates_run_session_strategy_symbol",
                        set_={
                            k: stmt.excluded[k]
                            for k in ("rvol", "rank", "candle", "passed", "reject_reason", "data", "created_at")
                        },
                    )
                )
            for n in ctx.notes:
                log_event(s, self._clock, n.level, f"strategy.{strategy.key}", n.message, _jsonable(n.data), self.run_id)

    # --- intents --------------------------------------------------------------------------------------
    async def _handle(
        self,
        strategy: Strategy,
        cfg: StrategyConfigView,
        intents: Sequence[Intent],
        session_date: date,
        event_key: str,
    ) -> list[IntentOutcome]:
        if not intents:
            return []
        settings = self._settings.load()
        out: list[IntentOutcome] = []
        for intent in intents:
            now = self._clock.now()
            signal_id = self._save_signal(cfg, intent, now, session_date, event_key)
            ctx = await self._risk_context(strategy, cfg, intent, session_date, settings, now)
            decision = self._risk.evaluate(intent, ctx)
            if isinstance(decision, Rejection):
                self._amend_signal(signal_id, "rejection", {
                    "check": decision.check, "reason": decision.reason, "detail": decision.detail,
                }, level="warning", message=f"risk rejected signal {signal_id}: {decision.check}: {decision.reason}")
                out.append(IntentOutcome(signal_id, intent, "rejected_by_risk", None, decision))
                continue
            if decision.sizing:
                self._amend_signal(signal_id, "sizing", decision.sizing)
            proposal = self.proposals.create(signal_id, decision, decision.kind)
            out.append(IntentOutcome(signal_id, intent, proposal.status, proposal.id))
        return out

    def _save_signal(
        self, cfg: StrategyConfigView, intent: Intent, now: datetime, session_date: date, event_key: str
    ) -> int:
        with session_scope(self._factory) as s:
            if isinstance(intent, EnterLong):
                symbol_id: int | None = intent.symbol_id
                evidence: dict[str, Any] = _jsonable(dict(intent.evidence))
            elif isinstance(intent, Exit):
                pos = s.get(m.Position, intent.position_id)
                symbol_id = pos.symbol_id if pos else None
                evidence = {"reason": intent.reason}
            else:
                order = s.get(m.Order, intent.order_id)
                symbol_id = order.symbol_id if order else None
                evidence = {"reason": intent.reason}
            sig = m.Signal(
                run_id=self.run_id,
                strategy_config_id=cfg.id,
                symbol_id=symbol_id,
                session_date=session_date,
                event_key=event_key,
                ts=now,
                intent=intent_to_json(intent),
                evidence=evidence,
            )
            s.add(sig)
            s.flush()
            return sig.id

    def _amend_signal(
        self, signal_id: int, key: str, value: dict[str, Any], level: str | None = None, message: str | None = None
    ) -> None:
        with session_scope(self._factory) as s:
            sig = s.get(m.Signal, signal_id, with_for_update=True)
            assert sig is not None
            sig.evidence = {**sig.evidence, key: _jsonable(value)}
            if level is not None and message is not None:
                log_event(s, self._clock, level, "risk", message, {"signal_id": signal_id, key: _jsonable(value)},
                          self.run_id)

    def _market_of(self, symbol_id: int) -> Market | None:
        with self._factory() as s:
            currency = s.execute(select(m.Symbol.currency).where(m.Symbol.id == symbol_id)).scalar_one_or_none()
        if currency == "USD":
            return "US"
        if currency == "CAD":
            return "TSX"
        return None

    async def _risk_context(
        self,
        strategy: Strategy,
        cfg: StrategyConfigView,
        intent: Intent,
        session_date: date,
        settings: RuntimeSettings,
        now: datetime,
    ) -> RiskContext:
        account = await self._account(session_date, settings)
        base = RiskContext(
            now=now,
            session_date=session_date,
            settings=settings,
            account=account,
            positions={p.id: p for p in self.broker.open_positions()},
            orders={o.id: o for o in self.broker.working_orders()},
            strategy_config_id=cfg.id,
        )
        if not isinstance(intent, EnterLong):
            return base  # exits, stops and cancels are never checked (Review Focus 5)
        inputs = self._check_switches(session_date, account, settings)
        reference = None
        if intent.order_type == "market":
            q = (await self._data.quotes([intent.symbol_id])).get(intent.symbol_id)
            reference = q.ask if q else None
        return dataclasses.replace(
            base,
            blocking_switch=self.killswitches.blocking(self.run_id, session_date),
            daily_pnl_pct=inputs.daily_pnl_pct,
            entries_today=self._entries_today(self.registry.config_ids(strategy.key), session_date),
            max_positions=int(getattr(strategy.params, "max_positions", 1)),
            symbol_market=self._market_of(intent.symbol_id),
            reference_price=reference,
        )


def build_engine(core: Core, client: QuoteClient, catalysts: CatalystSource) -> Engine:
    """The live engine. P3's worker builds one per session, so fill-model settings apply from the next."""
    settings = core.settings.load()
    run = get_live_run(core.factory, core.clock, settings)
    registry = StrategyRegistry(core.factory, core.clock)
    registry.ensure_defaults()
    broker = SimBroker(
        core.factory,
        core.clock,
        Ledger(core.calendar),
        QuoteFillModel(FillParams.from_settings(settings)),
        run.id,
        currency=settings.account_currency,
        calendar=core.calendar,
        settings=core.settings.load,  # the entry cutoff follows no_entry_before_close_minutes live
    )
    return Engine(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=core.settings,
        registry=registry,
        data=MarketDataService(core.factory, core.clock, core.calendar, client),
        catalysts=catalysts,
        broker=broker,
        proposals=ProposalService(core.factory, core.clock, core.settings, broker, run.id),
        risk=RiskManager(core.calendar),
        killswitches=KillSwitches(core.factory, core.clock),
        run_id=run.id,
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv --directory Trader/app run pytest tests/engine -q`
Expected: all pass. The strategies are loaded through the real entry points (P2-T6), so `uv sync --reinstall-package trader` must have run after P2-T6.

- [ ] **Step 5: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/engine/orchestrator.py Trader/app/tests/engine/test_orchestrator.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T13: engine orchestrator (intent to risk to proposal to broker, fills back to on_fill)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

### Task P2-T14: Pre-market job and `premarket` CLI

**Files:**
- Create: `Trader/app/trader/jobs/premarket.py`, `Trader/app/tests/jobs/test_premarket.py`
- Modify: `Trader/app/trader/cli.py` (add `premarket`), `Trader/app/tests/test_cli.py` (add the `premarket` smoke test)

**Interfaces:**
- Consumes: `FinvizScraper.screen(filters, view=111, signal=None) -> ScreenerPage` and `.news(ticker, today_et)` (P1-T5; `ScreenerPage(total, header, rows, bad_rows=0)`, rows are dicts with `"Ticker"`; failures raise `FinvizError`); `to_questrade_ticker` (P1-T5); `MarketDataService.universe`, `quotes`, `prior_closes` (P2-T7); `CatalystService.classify_many`, `mark_unclassified`, `CatalystRequest`, `OVER_CAP`, `CatalystStore`, `CatalystClassifier` (P2-T12); `RuntimeSettings` (`universe_finviz_filters`, `premarket_*`, `claude_premarket_max_candidates`; P1-T3/P2-T2); `OVERLAY_SYMBOL` (P1-T3); `run_job` (P1-T9: returns `skipped` for a session that already succeeded, or while another run holds the job's lock); `log_event` (P1-T9); `FakeQuestrade` (P2-T7) in tests.
- Produces (`trader.jobs.premarket`):
  - `PremarketScreens` protocol (`screen(filters, view=111, signal=None) -> ScreenerPage`); `PremarketData` protocol (`universe`, `quotes`, `prior_closes`).
  - `PremarketDeps(factory, clock, finviz: PremarketScreens, data: PremarketData, catalysts: CatalystService, settings: RuntimeSettings)`.
  - `PremarketCandidate(symbol_id, ticker, company, gap_pct: Decimal | None, sources: tuple[str, ...])` (`sources` ⊆ `{"earnings","gap","news"}`).
  - `async run_premarket(deps, session_date) -> dict[str, Any]`: candidates are universe names (never SPY) flagged by the FinViz news screen (`<universe filters>,<premarket.news_filter>`), the earnings screen (`<universe filters>,<premarket.earnings_filter>`), or a pre-market gap `|last / prior close − 1| >= premarket.gap_min_pct` from Questrade quotes. A failed screen is recorded in `screen_errors` and the scan carries on with the rest (gaps alone at worst). They are ranked by absolute gap (unknown gaps last, then by ticker); the top `claude.premarket_max_candidates` get headlines and a Claude classification; the rest are stored as `unknown` with `OVER_CAP`. Returns `{"session_date", "candidates", "classified", "over_cap": [tickers], "screen_errors", "brief"}`. It raises `RuntimeError` when the session has no universe (the nightly job didn't run). Re-running for the same session makes no new Claude calls (classified rows are kept) and `run_job` skips a succeeded session anyway.
  - `format_brief(session_date, top, catalysts, over, screen_errors) -> str`: one line per candidate (`AAA +5.00% [gap, news] earnings_beat, bullish, quality 82: <reason>`), then `Not classified (over cap): ...` and `FinViz screens failed: ...` lines when relevant. Sending it on Telegram is P3 (the Notifier); here it is printed and stored in `job_runs.detail`.
  - CLI `trader premarket [--date YYYY-MM-DD] [--force]`: today's ET session by default; does nothing on a non-session day; `ANTHROPIC_API_KEY` unset means names are stored `unknown` ("claude not configured").

- [ ] **Step 1: Write the failing tests**

`Trader/app/tests/jobs/test_premarket.py`:
```python
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_symbol
from tests.fakes_questrade import FakeQuestrade
from trader.adapters.claude.catalyst import OVER_CAP, CatalystClassifier, CatalystService, CatalystStore
from trader.adapters.finviz.parser import Headline, ScreenerPage
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.db import models as m
from trader.jobs.premarket import PremarketDeps, run_premarket
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY, PREV = date(2026, 10, 6), date(2026, 10, 5)
CLOCK = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))  # 08:00 ET
GOOD = {"catalyst_type": "earnings_beat", "direction": "bullish", "quality": 82, "is_confirmed": True,
        "reason": "Beat estimates and raised guidance."}
# ticker -> (Questrade id, pre-market last); every prior close is 20.00
BOOK = {"AAA": (101, "21.00"), "BBB": (102, "19.00"), "CCC": (103, "20.30"), "DDD": (104, "20.10"),
        "EEE": (105, "20.05"), "SPY": (199, "21.00")}


class FakeFinviz:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.screens: list[str] = []
        self.news_calls: list[str] = []

    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage:
        self.screens.append(filters)
        if self.fail:
            raise FinvizBlocked("HTTP 403")
        tickers = ["AAA", "CCC", "FFF"] if "news_date_today" in filters else ["DDD"]
        return ScreenerPage(len(tickers), ["No.", "Ticker"], [{"No.": str(i), "Ticker": t} for i, t in enumerate(tickers)])

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        self.news_calls.append(ticker)
        return [Headline(datetime(2026, 10, 6, 11, 0, tzinfo=UTC), f"{ticker} headline", "Reuters", "https://x")]


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(GOOD))],
                               usage=SimpleNamespace(input_tokens=500, output_tokens=50), stop_reason="end_turn")


@pytest.fixture
def seeded(db_factory: sessionmaker[Session]) -> dict[str, int]:
    ids: dict[str, int] = {}
    with db_factory() as s:
        for t, (qid, _) in BOOK.items():
            ids[t] = add_symbol(s, t, questrade_id=qid)
            s.add(m.UniverseSnapshot(session_date=DAY, symbol_id=ids[t], price=Decimal("20"), avg_volume=2_000_000,
                                     atr14=Decimal("1"), source="finviz"))
            s.add(m.DailyCandle(symbol_id=ids[t], date=PREV, open=Decimal("20"), high=Decimal("20.5"),
                                low=Decimal("19.5"), close=Decimal("20.00"), volume=1, vwap=None))
        s.commit()
    return ids


def deps(factory: sessionmaker[Session], finviz: FakeFinviz, messages: FakeMessages, **settings: Any) -> PremarketDeps:
    s = RuntimeSettings(**settings)
    fq = FakeQuestrade()
    for t, (qid, last) in BOOK.items():
        fq.add_symbol(t, qid)
        fq.set_quote(qid, last, last, last, CLOCK.now())
    client = SimpleNamespace(messages=messages)
    service = CatalystService(factory, CLOCK, CatalystStore(factory, CLOCK), CatalystClassifier(client, lambda: s),
                              finviz)
    return PremarketDeps(factory, CLOCK, finviz, MarketDataService(factory, CLOCK, CAL, fq), service, s)


async def test_candidates_come_from_screens_and_gaps(db_factory: sessionmaker[Session], seeded: dict[str, int]) -> None:
    finviz, msgs = FakeFinviz(), FakeMessages()
    out = await run_premarket(deps(db_factory, finviz, msgs), DAY)
    assert out["candidates"] == 4 and out["classified"] == 4 and out["over_cap"] == [] and out["screen_errors"] == []
    # ranked by |gap|: AAA +5% and BBB -5% (tie -> ticker), then CCC +1.5%, DDD +0.5%; EEE isn't flagged
    assert finviz.news_calls == ["AAA", "BBB", "CCC", "DDD"]
    assert finviz.screens == [
        "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa,news_date_today",
        "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa,earningsdate_today",
    ]
    lines = out["brief"].splitlines()
    assert lines[0] == "Pre-market brief for 2026-10-06: 4 candidates"
    assert lines[1].startswith("AAA +5.00% [gap, news] earnings_beat, bullish, quality 82")
    assert lines[2].startswith("BBB -5.00% [gap]")
    assert lines[4].startswith("DDD +0.50% [earnings]")
    with db_factory() as s:
        rows = {r.symbol_id: r for r in s.execute(select(m.Catalyst)).scalars()}
    assert rows[seeded["DDD"]].earnings_date == DAY and rows[seeded["AAA"]].earnings_date is None
    assert rows[seeded["AAA"]].gap_pct == Decimal("0.0500") and rows[seeded["AAA"]].headlines[0]["title"] == "AAA headline"
    assert seeded["SPY"] not in rows and seeded["EEE"] not in rows


async def test_only_the_top_n_are_classified(db_factory: sessionmaker[Session], seeded: dict[str, int]) -> None:
    msgs = FakeMessages()
    out = await run_premarket(deps(db_factory, FakeFinviz(), msgs, claude_premarket_max_candidates=2), DAY)
    assert out["classified"] == 2 and out["over_cap"] == ["CCC", "DDD"] and len(msgs.calls) == 2
    assert "Not classified (over cap): CCC, DDD" in out["brief"]
    with db_factory() as s:
        ccc = s.execute(select(m.Catalyst).where(m.Catalyst.symbol_id == seeded["CCC"])).scalar_one()
    assert ccc.catalyst_type == "unknown" and ccc.reason == OVER_CAP


async def test_rerunning_the_scan_makes_no_new_claude_calls(db_factory: sessionmaker[Session], seeded: dict[str, int]) -> None:
    msgs = FakeMessages()
    d = deps(db_factory, FakeFinviz(), msgs)
    await run_premarket(d, DAY)
    await run_premarket(d, DAY)
    assert len(msgs.calls) == 4
    with db_factory() as s:
        assert s.execute(select(func.count()).select_from(m.Catalyst)).scalar_one() == 4


async def test_a_finviz_failure_falls_back_to_gaps(db_factory: sessionmaker[Session], seeded: dict[str, int]) -> None:
    out = await run_premarket(deps(db_factory, FakeFinviz(fail=True), FakeMessages()), DAY)
    assert out["candidates"] == 2 and len(out["screen_errors"]) == 2  # AAA and BBB by gap alone
    assert "FinViz screens failed: news: HTTP 403" in out["brief"]


async def test_no_universe_is_an_error(db_factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeError, match="nightly"):
        await run_premarket(deps(db_factory, FakeFinviz(), FakeMessages()), DAY)


async def test_the_cost_of_every_call_is_stored(db_factory: sessionmaker[Session], seeded: dict[str, int]) -> None:
    await run_premarket(deps(db_factory, FakeFinviz(), FakeMessages()), DAY)
    store = CatalystStore(db_factory, CLOCK)
    assert store.spent(DAY) == Decimal("0.006000")  # 4 x (500 x $2/M + 50 x $10/M)
    assert store.spent(DAY + timedelta(days=1)) == 0
```

- [ ] **Step 2: Run to see it fail**

Run: `uv --directory Trader/app run pytest tests/jobs/test_premarket.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.jobs.premarket'`.

- [ ] **Step 3: Implement `trader/jobs/premarket.py`**

```python
"""Pre-market scan (SPEC §4.2, §4.3, §9 at 08:00 ET; BR-03, BR-05).

Candidates: universe names on the FinViz news or earnings screen, or gapping at least premarket.gap_min_pct
on Questrade's pre-market quotes. The biggest movers (by |gap|, up to claude.premarket_max_candidates) get
headlines and a Claude classification; the rest are stored as "not classified (over cap)".
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.claude.catalyst import OVER_CAP, CatalystRequest, CatalystService, StoredCatalyst
from trader.adapters.finviz.parser import ScreenerPage, to_questrade_ticker
from trader.adapters.finviz.scraper import FinvizError
from trader.adapters.questrade.models import QtQuote
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock
from trader.market.types import UniverseMember
from trader.settings_store import OVERLAY_SYMBOL, RuntimeSettings

Q4 = Decimal("0.0001")
SOURCE = "job.premarket"


class PremarketScreens(Protocol):
    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage: ...


class PremarketData(Protocol):
    async def universe(self, session_date: date) -> list[UniverseMember]: ...
    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]: ...
    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]: ...


@dataclass
class PremarketDeps:
    factory: sessionmaker[Session]
    clock: Clock
    finviz: PremarketScreens
    data: PremarketData
    catalysts: CatalystService
    settings: RuntimeSettings


@dataclass(frozen=True, slots=True)
class PremarketCandidate:
    symbol_id: int
    ticker: str
    company: str
    gap_pct: Decimal | None
    sources: tuple[str, ...]


def _rank_key(c: PremarketCandidate) -> tuple[int, Decimal, str]:
    return (0, -abs(c.gap_pct), c.ticker) if c.gap_pct is not None else (1, Decimal(0), c.ticker)


def format_brief(
    session_date: date,
    top: Sequence[PremarketCandidate],
    catalysts: Mapping[int, StoredCatalyst],
    over: Sequence[PremarketCandidate],
    screen_errors: Sequence[str],
) -> str:
    lines = [f"Pre-market brief for {session_date.isoformat()}: {len(top) + len(over)} candidates"]
    for c in top:
        gap = f"{c.gap_pct * 100:+.2f}%" if c.gap_pct is not None else "gap n/a"
        cat = catalysts.get(c.symbol_id)
        if cat is not None and cat.classified:
            desc = f"{cat.catalyst_type}, {cat.direction}, quality {cat.quality}: {cat.reason}"
        else:
            desc = f"unknown ({cat.reason if cat is not None else 'not classified'})"
        lines.append(f"{c.ticker} {gap} [{', '.join(c.sources)}] {desc}")
    if over:
        lines.append("Not classified (over cap): " + ", ".join(c.ticker for c in over))
    if screen_errors:
        lines.append("FinViz screens failed: " + "; ".join(screen_errors))
    if not top and not over:
        lines.append("No gappers or news today.")
    return "\n".join(lines)


async def run_premarket(deps: PremarketDeps, session_date: date) -> dict[str, Any]:
    s = deps.settings
    universe = [u for u in await deps.data.universe(session_date) if u.ticker != OVERLAY_SYMBOL]
    if not universe:
        raise RuntimeError(f"no universe for {session_date}: the nightly job must run first")
    by_ticker = {u.ticker: u for u in universe}
    flagged: dict[str, set[str]] = {}
    screen_errors: list[str] = []
    for source, extra in (("news", s.premarket_news_filter), ("earnings", s.premarket_earnings_filter)):
        try:
            page = await asyncio.to_thread(deps.finviz.screen, f"{s.universe_finviz_filters},{extra}")
        except FinvizError as exc:
            screen_errors.append(f"{source}: {exc}")
            continue
        for row in page.rows:
            ticker = to_questrade_ticker(row.get("Ticker", ""))
            if ticker in by_ticker:
                flagged.setdefault(ticker, set()).add(source)

    ids = [u.symbol_id for u in universe]
    quotes = await deps.data.quotes(ids)
    closes = await deps.data.prior_closes(ids, session_date)
    gaps: dict[int, Decimal] = {}
    for u in universe:
        q, prev = quotes.get(u.symbol_id), closes.get(u.symbol_id)
        price = q.last if q is not None else None
        if price is not None and price > 0 and prev is not None and prev > 0:
            gaps[u.symbol_id] = ((price - prev) / prev).quantize(Q4, ROUND_HALF_UP)
            if abs(gaps[u.symbol_id]) >= s.premarket_gap_min_pct:
                flagged.setdefault(u.ticker, set()).add("gap")

    candidates = sorted(
        (
            PremarketCandidate(
                by_ticker[t].symbol_id, t, by_ticker[t].name or "", gaps.get(by_ticker[t].symbol_id), tuple(sorted(src))
            )
            for t, src in flagged.items()
        ),
        key=_rank_key,
    )
    cap = s.claude_premarket_max_candidates
    top, over = candidates[:cap], candidates[cap:]

    def request(c: PremarketCandidate) -> CatalystRequest:
        earnings = session_date if "earnings" in c.sources else None
        return CatalystRequest(c.symbol_id, c.ticker, c.company, c.gap_pct, earnings)

    catalysts = await deps.catalysts.classify_many([request(c) for c in top], session_date)
    deps.catalysts.mark_unclassified([request(c) for c in over], session_date, OVER_CAP)
    brief = format_brief(session_date, top, catalysts, over, screen_errors)
    detail: dict[str, Any] = {
        "session_date": session_date.isoformat(),
        "candidates": len(candidates),
        "classified": sum(1 for c in catalysts.values() if c.classified),
        "over_cap": [c.ticker for c in over],
        "screen_errors": screen_errors,
        "brief": brief,
    }
    with session_scope(deps.factory) as sess:
        log_event(sess, deps.clock, "info", SOURCE, f"pre-market brief for {session_date}",
                  {k: v for k, v in detail.items() if k != "brief"})
    return detail
```

- [ ] **Step 4: Run the tests**

Run: `uv --directory Trader/app run pytest tests/jobs/test_premarket.py -q`
Expected: all pass.

- [ ] **Step 5: Add the `premarket` command**

Add to `Trader/app/trader/cli.py` (after `nightly`):
```python
@app.command()
def premarket(
    date_: str | None = typer.Option(None, "--date", help="Session YYYY-MM-DD (default: today in ET)"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Pre-market scan: gappers and news, headlines, Claude catalysts, brief (SPEC §9, 08:00 ET)."""
    import asyncio
    from datetime import date as date_cls
    from pathlib import Path
    from typing import Any

    import anthropic

    from trader.adapters.claude.catalyst import CatalystClassifier, CatalystService, CatalystStore
    from trader.adapters.finviz.scraper import FinvizScraper
    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.adapters.questrade.client import QuestradeClient
    from trader.bootstrap import build_core
    from trader.jobs.premarket import PremarketDeps, run_premarket
    from trader.jobs.runner import run_job
    from trader.market.clock import et_date
    from trader.market.data_service import MarketDataService

    core = build_core()
    settings = core.settings.load()
    session_date = date_cls.fromisoformat(date_) if date_ else et_date(core.clock.now())
    if not core.calendar.is_session(session_date):
        typer.echo(f"premarket {session_date}: not a trading session, nothing to do")
        return
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)
    api_key = core.env.anthropic_api_key
    cache_dir = Path.home() / ".cache" / "trader" / "finviz"

    with FinvizScraper(
        min_interval_s=settings.finviz_min_interval_seconds,
        cache_dir=cache_dir,
        cache_ttl_s=settings.finviz_cache_hours * 3600,
    ) as finviz:

        def job() -> dict[str, Any]:
            async def go() -> dict[str, Any]:
                claude = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value()) if api_key else None
                try:
                    async with QuestradeClient(auth, core.clock) as qt:
                        classifier = CatalystClassifier(claude, core.settings.load) if claude else None
                        service = CatalystService(
                            core.factory, core.clock, CatalystStore(core.factory, core.clock), classifier, finviz
                        )
                        data = MarketDataService(core.factory, core.clock, core.calendar, qt)
                        deps = PremarketDeps(core.factory, core.clock, finviz, data, service, settings)
                        return await run_premarket(deps, session_date)
                finally:
                    if claude is not None:
                        await claude.close()

            return asyncio.run(go())

        out = run_job(core.factory, core.clock, "premarket", session_date, job, force=force)
    if out.status == "succeeded":
        typer.echo(out.detail["brief"])
    else:
        typer.echo(f"premarket {session_date}: {out.status} {out.error or ''}")
    if out.status == "failed":
        raise typer.Exit(1)
```

Add a CLI smoke test to `Trader/app/tests/test_cli.py`:
```python
def test_premarket_command_is_registered() -> None:
    from typer.testing import CliRunner

    from trader.cli import app

    result = CliRunner().invoke(app, ["premarket", "--help"])
    assert result.exit_code == 0 and "--date" in result.output
```

- [ ] **Step 6: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/trader/jobs/premarket.py Trader/app/trader/cli.py Trader/app/tests/jobs/test_premarket.py Trader/app/tests/test_cli.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T14: pre-market scan (news, earnings, gaps, top-N Claude catalysts, brief) and premarket CLI" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

- [ ] **Step 7: LIVE: run the pre-market scan against `trader_dev`**

Needs a session day and the nightly job's universe for it (P1-T9). From your worktree, before 09:30 ET on a trading day (or any time with `--date` set to a session the nightly job prepared):
`uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev trader premarket`
Expected within ~2 minutes: the brief, first line `Pre-market brief for <date>: N candidates`, then one line per candidate. This is also the first write to the Phase 2 tables by the app role `trader_dev_app`: a `permission denied for table catalysts` error means the migration role's default privileges don't cover new tables, so escalate (§5.4) with the error text. Run it a second time: `premarket <date>: skipped`. Record N, the number classified and `claude.catalyst` cost (`SELECT sum(cost_usd) FROM trader.catalysts WHERE session_date = '<date>'` through the dev database) in your activity-log entry. If `ANTHROPIC_API_KEY` isn't in `.env.dev`, every line says `unknown (claude not configured)`: record that and escalate for the key rather than editing `.env.dev`.

---

### Task P2-T15: Integration: one full simulated day

**Files:**
- Create: `Trader/app/tests/integration/__init__.py`, `Trader/app/tests/integration/test_simulated_day.py`

**Interfaces:**
- Consumes: `run_nightly`, `NightlyDeps` (P1-T9: `NightlyDeps(factory, clock, calendar, finviz, market, settings)`; `finviz.universe(filters) -> list[UniverseRow]`; `market.symbols_by_names`, `market.candles_many`); `run_premarket`, `PremarketDeps` (P2-T14); `Engine` and its collaborators (P2-T13); `CatalystService`, `CatalystStore`, `CatalystClassifier` (P2-T12); `Ledger` (P2-T3); `FakeQuestrade` (P2-T7); the `v_trade_metrics` view (P2-T1).
- Produces: `tests/integration/test_simulated_day.py` with `test_full_day_ends_flat` (Review Focus 4) and `test_stop_out_day_ends_flat`. No production code: if a step fails, the fix belongs in the task that owns the failing code, so report it rather than patching here.

The day (Tue 2026-10-06, auto approval, fake clock, fake FinViz/Questrade/Claude, a real database):

| Time (ET) | Step | Expected |
|---|---|---|
| Mon 20:00 | `run_nightly` | universe AAA, BBB, SPY; ATR14 = 1.0000; opening-bar average 1000 |
| 08:00 | `run_premarket` | AAA gaps +5% and is on the news screen: classified `earnings_beat`, bullish, 82 |
| 09:35:05 | `run_event("orb_open")` | AAA rvol 5 ranked first and selected; BBB rejected `catalyst_missing` (classified `none` at 9:35); buy stop 33 @ 21.51, stop loss 21.41, auto-submitted |
| 09:36 | quote AAA 21.52/21.55 | entry fills at 21.5608; protective stop 21.41 submitted |
| 15:30 | `run_event("overlay_decision")`, SPY +0.4% | hold, decision logged |
| 15:50 | `run_event("flatten")`, quote AAA 21.90/21.92 | market exit fills at 21.8890; the stop is cancelled |
| 16:00 | `end_of_session` | nothing open |

Money: buy 33 × 21.5608 = 711.5064; sell 33 × 21.8890 = 722.3370; SEC fee 0.0149; pnl = 0.3282 × 33 − 0.0149 = 10.8157; planned risk (21.5608 − 21.41) × 33 = 4.9764; R = 2.1734; slippage (0.0108 + 0.0110) × 33 = 0.7194. Cash: 730.8157 total; settled on the day 720 − 711.5064 − 0.0149 = 8.4787; settled next session 730.8157.

- [ ] **Step 1: Write the integration test**

`Trader/app/tests/integration/__init__.py`: empty file.

`Trader/app/tests/integration/test_simulated_day.py`:
```python
"""One full simulated day, nightly job to flatten, with a fake clock and fake data (SPEC §16, BR-42)."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.fakes_questrade import FakeQuestrade
from trader.adapters.claude.catalyst import CatalystClassifier, CatalystService, CatalystStore
from trader.adapters.finviz.parser import Headline, ScreenerPage, UniverseRow
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.orchestrator import Engine
from trader.engine.proposals import ProposalService
from trader.engine.risk import RiskManager
from trader.engine.runs import get_live_run
from trader.jobs.nightly import NightlyDeps, run_nightly
from trader.jobs.premarket import PremarketDeps, run_premarket
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.data_service import MarketDataService
from trader.market.types import Candle
from trader.settings_store import SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
NEXT = date(2026, 10, 7)
QT = {"AAA": 101, "BBB": 102, "SPY": 199}


def et(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


def daily(d: date, low: str, high: str, close: str) -> Candle:
    start = datetime.combine(d, time(0), tzinfo=ET)
    return Candle(start, start + timedelta(days=1), Decimal(close), Decimal(high), Decimal(low), Decimal(close),
                  1_500_000, None)


def opening(d: date, o: str, h: str, low: str, c: str, v: int) -> Candle:
    start = CAL.session_open(d)
    return Candle(start, start + timedelta(minutes=5), Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None)


class FakeFinviz:
    """Universe, screens and headlines."""

    def universe(self, filters: str) -> list[UniverseRow]:
        return [UniverseRow(t, f"{t} Inc", "Tech", "Software", Decimal("20"), 2_000_000) for t in ("AAA", "BBB")]

    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage:
        tickers = ["AAA"] if "news_date_today" in filters else []
        return ScreenerPage(len(tickers), ["Ticker"], [{"Ticker": t} for t in tickers])

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        return [Headline(et(7, 0), f"{ticker} news", "Reuters", "https://example.com")]


class FakeClaude:
    """AAA: a strong bullish catalyst. Anything else: no catalyst."""

    def __init__(self) -> None:
        self.messages = self
        self.tickers: list[str] = []

    async def create(self, **kwargs: Any) -> Any:
        prompt = kwargs["messages"][0]["content"]
        ticker = prompt.splitlines()[0].removeprefix("Ticker: ")
        self.tickers.append(ticker)
        good = ticker == "AAA"
        payload = {
            "catalyst_type": "earnings_beat" if good else "none",
            "direction": "bullish" if good else "neutral",
            "quality": 82 if good else 10,
            "is_confirmed": good,
            "reason": "Beat and raise." if good else "No company news.",
        }
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                               usage=SimpleNamespace(input_tokens=500, output_tokens=50), stop_reason="end_turn")


@dataclass
class Day:
    clock: FixedClock
    fq: FakeQuestrade
    engine: Engine
    finviz: FakeFinviz
    claude: FakeClaude
    catalysts: CatalystService
    data: MarketDataService
    store: SettingsStore
    run_id: int
    factory: sessionmaker[Session]


def setup_day(factory: sessionmaker[Session]) -> Day:
    clock = FixedClock(et(20, 0, day=date(2026, 10, 5)))  # Monday evening: the nightly job
    store = SettingsStore(factory, now=clock.now)
    store.set("approval_mode", "auto", actor="test")
    settings = store.load()
    fq = FakeQuestrade()
    for t, q in QT.items():
        fq.add_symbol(t, q)
    for d in CAL.sessions_before(DAY, 20):
        fq.add_bars(QT["AAA"], "OneDay", [daily(d, "19.50", "20.50", "20.00")])
        fq.add_bars(QT["BBB"], "OneDay", [daily(d, "19.50", "20.50", "20.00")])
        fq.add_bars(QT["SPY"], "OneDay", [daily(d, "499.50", "500.50", "500.00")])
    for d in CAL.sessions_before(DAY, 14):
        for t in ("AAA", "BBB"):
            fq.add_bars(QT[t], "FiveMinutes", [opening(d, "20.00", "20.10", "19.90", "20.05", 1000)])
    run = get_live_run(factory, clock, settings)
    finviz, claude = FakeFinviz(), FakeClaude()
    data = MarketDataService(factory, clock, CAL, fq)
    catalysts = CatalystService(factory, clock, CatalystStore(factory, clock),
                                CatalystClassifier(claude, store.load), finviz)
    registry = StrategyRegistry(factory, clock)
    registry.ensure_defaults()
    broker = SimBroker(factory, clock, Ledger(CAL), QuoteFillModel(FillParams.from_settings(settings)), run.id,
                       calendar=CAL, settings=store.load)
    engine = Engine(
        factory=factory,
        clock=clock,
        calendar=CAL,
        settings=store,
        registry=registry,
        data=data,
        catalysts=catalysts,
        broker=broker,
        proposals=ProposalService(factory, clock, store, broker, run.id),
        risk=RiskManager(CAL),
        killswitches=KillSwitches(factory, clock),
        run_id=run.id,
    )
    return Day(clock, fq, engine, finviz, claude, catalysts, data, store, run.id, factory)


async def morning(d: Day) -> None:
    """Nightly, pre-market, the 9:35 scan and the entry fill."""
    detail = await run_nightly(NightlyDeps(d.factory, d.clock, CAL, d.finviz, d.fq, d.store.load()), DAY)
    assert detail["source"] == "finviz" and detail["universe"] == 3

    d.clock.set(et(8, 0))
    d.fq.set_quote(QT["AAA"], "20.99", "21.01", "21.00", d.clock.now())  # +5% gap
    d.fq.set_quote(QT["BBB"], "20.09", "20.11", "20.10", d.clock.now())
    brief = await run_premarket(PremarketDeps(d.factory, d.clock, d.finviz, d.data, d.catalysts, d.store.load()), DAY)
    assert brief["candidates"] == 1 and brief["classified"] == 1 and d.claude.tickers == ["AAA"]

    d.clock.set(et(9, 35, 5))
    d.fq.add_bars(QT["AAA"], "FiveMinutes", [opening(DAY, "21.00", "21.50", "20.90", "21.40", 5000)])
    d.fq.add_bars(QT["BBB"], "FiveMinutes", [opening(DAY, "20.00", "20.40", "19.95", "20.30", 3000)])
    res = await d.engine.run_event("orb_open", DAY)
    (out,) = res.outcomes
    assert out.status == "submitted" and d.claude.tickers == ["AAA", "BBB"]  # BBB classified at 9:35
    (entry,) = d.engine.broker.working_orders()
    assert (entry.stop, entry.qty) == (Decimal("21.5100"), 33)

    d.clock.set(et(9, 36))
    d.fq.set_quote(QT["AAA"], "21.52", "21.55", "21.53", d.clock.now())
    (fill,) = await d.engine.poll_quotes()
    assert fill.price == Decimal("21.5608")
    (stop,) = d.engine.broker.working_orders()
    assert (stop.purpose, stop.stop) == ("stop", Decimal("21.4100"))
    await d.engine.tick(d.clock.now())


async def test_full_day_ends_flat(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 4 and BR-42: signal, auto-approval, fill, stop, overlay hold, flatten, nothing held."""
    d = setup_day(db_factory)
    await morning(d)

    d.clock.set(et(15, 30))
    d.fq.set_quote(QT["SPY"], "501.99", "502.01", "502.00", d.clock.now())  # +0.4%: hold
    d.fq.set_quote(QT["AAA"], "21.70", "21.72", "21.71", d.clock.now())
    overlay = await d.engine.run_event("overlay_decision", DAY)
    assert overlay.strategies == ["spy_overlay"] and overlay.outcomes == []

    d.clock.set(et(15, 50))
    flatten = await d.engine.run_event("flatten", DAY)
    assert [o.status for o in flatten.outcomes] == ["submitted"]
    d.clock.set(et(15, 50, 2))
    d.fq.set_quote(QT["AAA"], "21.90", "21.92", "21.91", d.clock.now())
    (exit_fill,) = await d.engine.poll_quotes()
    assert exit_fill.price == Decimal("21.8890") and exit_fill.trade_id is not None

    d.clock.set(et(16, 0))
    assert await d.engine.end_of_session(DAY) == []
    assert d.engine.broker.open_positions() == [] and d.engine.broker.working_orders() == []

    with db_factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
        kinds = [(p.kind, p.status, p.decided_via) for p in s.execute(select(m.Proposal).order_by(m.Proposal.id)).scalars()]
        stop = s.execute(select(m.Order).where(m.Order.purpose == "stop")).scalar_one()
        cands = {c.reject_reason for c in s.execute(select(m.Candidate)).scalars()}
        ledger = [(r.kind, r.amount) for r in s.execute(select(m.CashLedger).order_by(m.CashLedger.id)).scalars()]
        decision = s.execute(
            select(m.EventLog).where(m.EventLog.source == "strategy.spy_overlay", m.EventLog.message == "overlay: decision")
        ).scalar_one()
        metrics = s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": d.run_id}).mappings().one()
        on_day = Ledger(CAL).balances(s, d.run_id, DAY)
        next_day = Ledger(CAL).balances(s, d.run_id, NEXT)
        fills = s.execute(select(m.Fill).order_by(m.Fill.id)).scalars().all()
    assert (trade.entry_price, trade.exit_price, trade.qty) == (Decimal("21.5608"), Decimal("21.8890"), 33)
    assert trade.pnl == Decimal("10.8157") and trade.pnl_r == Decimal("2.1734")
    assert trade.planned_risk == Decimal("4.9764") and trade.slippage_total == Decimal("0.7194")
    assert trade.fees_total == Decimal("0.0149") and trade.exit_reason == "flatten_close"
    assert kinds == [("entry", "submitted", "auto"), ("stop", "submitted", "auto"), ("exit", "submitted", "auto")]
    assert stop.status == "cancelled" and stop.cancel_reason == "position closed"
    assert cands == {None, "catalyst_missing"}
    assert decision.data["decision"] == "hold" and decision.data["spy_return"] == "0.004000"
    assert ledger == [
        ("deposit", Decimal("720.0000")),
        ("buy", Decimal("-711.5064")),
        ("sell", Decimal("722.3370")),
        ("fee", Decimal("-0.0149")),
    ]
    assert on_day.total == Decimal("730.8157") and on_day.settled == Decimal("8.4787")
    assert next_day.settled == Decimal("730.8157")
    assert metrics["trades"] == 1 and metrics["win_rate"] == Decimal("1.0000")
    assert metrics["expectancy_r"] == Decimal("2.1734")
    assert fills[0].quote_snapshot["ask"] == "21.55" and fills[1].quote_snapshot["bid"] == "21.90"


async def test_stop_out_day_ends_flat(db_factory: sessionmaker[Session]) -> None:
    d = setup_day(db_factory)
    await morning(d)

    d.clock.set(et(10, 5))
    d.fq.set_quote(QT["AAA"], "21.38", "21.40", "21.39", d.clock.now())
    (stopped,) = await d.engine.poll_quotes()
    assert stopped.purpose == "stop" and stopped.price == Decimal("21.3693")  # min(21.41, 21.38) - 0.0107

    d.clock.set(et(15, 50))
    assert (await d.engine.run_event("flatten", DAY)).outcomes == []  # nothing left to flatten
    d.clock.set(et(16, 0))
    assert await d.engine.end_of_session(DAY) == []
    with db_factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
        positions = s.execute(select(m.Position)).scalars().all()
    assert trade.exit_reason == "protective_stop" and trade.pnl < 0
    assert all(p.closed_at is not None for p in positions)
```

- [ ] **Step 2: Run it**

Run: `uv --directory Trader/app run pytest tests/integration -q`
Expected: 2 passed. This task adds no production code, so it passes as soon as it's written if T1–T14 are right. If it fails, don't patch it here: find the owning task from the failing step (nightly → P1-T9, pre-market → P2-T14, the 9:35 scan → P2-T8/T13, fills and ledger → P2-T4/T5, proposals → P2-T11) and report it as a finding with the exact assertion, so the orchestrator reopens that task.

- [ ] **Step 3: Run the gate, commit and push**

Run: `uv --directory Trader/app run ruff format .` then `bash Trader/app/scripts/check.sh` → all pass.
```bash
git add Trader/app/tests/integration/__init__.py Trader/app/tests/integration/test_simulated_day.py Trader/docs/plans/2026-09-27-phase-2-engine.md
git commit -m "P2-T15: integration test of one full simulated day, nightly to flatten, ending flat" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase --autostash origin trunk
git push origin HEAD:trunk
```

---

## Self-review (done by the plan author)

- **Spec coverage (Phase 2 scope):**
  - BR-03 (pre-market scan, catalysts recorded) → T14, T12. BR-05 (Claude type, quality, reason) → T12. BR-10 (plug-ins, versioned settings, no engine change) → T6 (entry points, `strategy_configs` revisions). BR-11 → T8. BR-12 → T9. BR-13 (rule values on every signal) → T8 evidence plus T13 `evidence.sizing`. BR-20 (quote fills, slippage) → T4. BR-21 (cash, positions, T+1, settled-cash rule) → T3, T5, T10 check 5. BR-22 (capital, currency, FX cost, markets) → T2, T10 check 6. BR-23 (audit trail) → T5 (fills with quote snapshots), T11 (`audit_log` for decisions), T13 (signals). BR-40 → T10. BR-41 → T10 (daily loss, drawdown, expectancy; manual reset with reason). BR-42 → T8 flatten, T5 entry cutoff in the broker (a late entry is cancelled, never filled), T11 auto-flatten (and auto-cancel) on expiry, T13 end-of-session alarm, T15.
  - SPEC §3a (runs, clock, intents, proposals, orders, fills) → T1, T2, T6. §4.2 pre-market → T14. §4.3 (schema output, budget, cap, 9:35 classification) → T12, T14, T8/T13 via `CatalystService.get`. §5.1 → T6 (refined: async callbacks). §5.2 → T8 (all 7 steps; `stale_universe` added per the P1-T9 ruling). §5.3 → T9. §6.1 → T10. §6.2 (states, TTLs, unprotected escalation, auto flatten, audited approval mode) → T11. §6.3 → T10. §7.1 order types and end-of-session cancel → T4, T5. §7.2 every table row, stale quotes, fees, snapshot → T4, T5. §7.3 → T2, T3. §10 trading tables and both views → T1. §13 runtime settings → T2.
  - Out of this phase on purpose: Telegram messages and the scheduler/worker loop (P3: the engine exposes `run_event`, `poll_quotes`, `tick`, `end_of_session`), the candle fill model and replay run creation (P5), the weekly report's Claude commentary (P5, `adapters/claude/reports.py`), `users` (P4).
- **Placeholders:** none. Two conditional notes give exact fixes: T1 Step 4 (which side to fix if the model/migration comparison differs) and T12 Step 5 (confirm the installed SDK is 1.x if the SDK-shape test fails).
- **Type consistency:** `OrderSpec`, `FillEvent`/`Fill`, `PositionView`, `OrderView`, `AccountState` (T4) are used unchanged by T5, T6, T8–T13. `SizedOrder.kind` and `ProposalKind` (T10) match `ProposalService.create(..., kind)` (T11). `CatalystInfo` (T6) is satisfied by `StoredCatalyst` (T12) and `FakeCatalyst`. `MarketDataView` (T6) is implemented by `MarketDataService` (T7) and `FakeData`; `prior_closes` is on the service only (T14 uses the service). Event keys `orb_open`, `entry_cancel`, `flatten`, `overlay_decision` are defined in T8/T9 and used by T13/T15. DB symbol IDs everywhere; Questrade IDs only inside `FakeQuestrade` and at `MarketDataService`'s client boundary.
- **Review Focus tests:** 1 → T4 `test_stale_quote_never_fills`, `test_unusable_quotes_never_fill`; T5 `test_stale_quote_keeps_order_working_and_logs_once`. 2 → T11 `test_concurrent_decisions_first_wins`, `test_decide_after_expiry_is_already_decided`; T5 `test_order_fills_only_once`. 3 → T3 `test_settle_date_skips_weekend_and_holidays`, `test_sale_proceeds_settle_next_session`. 4 → T5 `test_an_entry_that_would_fill_late_is_cancelled_instead`; T11 `test_expired_flatten_auto_submits`, `test_an_entry_that_would_fill_late_is_cancelled_instead`; T13 `test_end_of_session_flags_open_position`; T15 `test_full_day_ends_flat`. 5 → T10 `test_exits_and_cancels_pass_when_everything_is_tripped`; T13 `test_protective_stop_placed_while_kill_switch_tripped`. The master plan's items 2 (early closes) and 4 (missing candle) also have Phase 2 tests: T6 `test_offsets_follow_an_early_close`, T8 `test_an_early_close_moves_the_flatten`, T10 `test_entry_window_follows_an_early_close_and_holidays`, T7 `test_opening_bars_report_api_errors_per_symbol`, T8 `test_missing_bars_and_baselines_are_noted_not_raised`.
- **Code checked before hand-off:** every code block in this plan was laid over trunk at `4f65a89` (Phase 1 complete, P1-REVIEW accepted) in a scratch copy outside the repo and run through the gate: `ruff check`, `ruff format` and `mypy trader` clean, and 564 tests passed, including every Phase 2 test, the integration day, and the P1 gauntlet suites for T2–T9. That run found and fixed four things now in the plan: the catalyst upsert must key the `type` column by its column name; `anthropic` 1.x uses `httpx2`, so the SDK-shape test uses `httpx2.MockTransport`, not `respx`; P1's `test_upgrade_at_head_is_a_noop` pinned head `0001` (T1 Step 5); and a few type and line-length fixes. Not run there: `tests/test_build_scripts.py` and the P1-T1 breaker, which load `Trader/build/` from outside `Trader/app`.
- **Fix round (attempt 2), re-checked:** after the plan-review fixes (T10 depends on T6; the broker's entry cutoff and the auto-executed expired cancel for BR-42; the `FillModel` protocol with `assess`; the staleness assumption; `tests/test_cli.py` in T14's files), every code block from P2-T2 onwards was laid over trunk at `c36f03e` (P2-T1 built and accepted) in a scratch copy of the whole `Trader/` tree outside the repo, with `anthropic` and `httpx2` added as T12 says. `check.sh` passed: `ruff check`, `ruff format --check` and `mypy trader` clean, 585 tests passed (including `tests/test_build_scripts.py` this time).
- **P1-T9 fix-round inputs folded in:** the stale fallback universe (`universe_status` in T7, `stale_universe="skip"` in T8), `avg_open_vol_14d` being `NULL` with too few bars (T8 notes and skips the symbol), `run_job` returning `skipped` under the advisory lock (T14 prints the status), and `QtQuote.delay` being `None` when unknown (T4 treats it as delayed). Builders re-read `trader/jobs/nightly.py` for the exact detail keys.
