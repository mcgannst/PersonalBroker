# Options simulation (OPTSIM): plan

Status: draft for Stephen's review, Mon 2026-10-05. Nothing is built yet.

Rules for the wheel: [`../specs/wheel-rules-spec.md`](../specs/wheel-rules-spec.md) (Stephen's "Wheel Strategy Rules Spec" v1.0, copied into the repo unchanged). Where that spec and an earlier answer in the planning conversation differ, the spec wins (§2.3).

This is a specification plan (master plan §6.6): tasks, interfaces, behaviour and acceptance tests in words. Builders write the code and the tests.

## 1. Goal

Simulate buying and selling puts and calls in real time, with real Questrade option quotes, recording every order, fill and position in PostgreSQL instead of sending anything to the broker. Two ways to trade from the first version:

- **By hand**: an option chain and order ticket in the web app.
- **By strategy**: option strategies are plug-ins. The wheel is the first one, following the rules spec.

Adding a second strategy later must need only a new plug-in package and its tests: no change to the broker, the collateral rules, the expiry handling, the worker, the API or the web pages.

## 2. Decisions

### 2.1 From Stephen (planning conversation, 2026-10-05)

| # | Decision |
|---|---|
| D1 | Manual orders and automated strategies are both in the first version |
| D2 | Position types: long calls and puts, cash-secured puts, covered calls, and multi-leg orders (spreads) |
| D3 | Positions are held for days up to LEAPS expiries. No same-day close rule. Expiry, exercise and assignment are simulated, and assigned shares are held overnight |
| D4 | A separate simulated options account, starting at **5,000 USD** |
| D5 | Covered and defined-risk positions only. No naked short options, no margin model |
| D6 | The wheel is the first automated strategy |
| D7 | Every simulated option order fills without an approval step |
| D8 | Wheel candidates: the screener scans the market and sends new candidates to Telegram; Stephen adds them to or rejects them from the approved list |
| D9 | At most 50% of the account per position to start (a setting) |
| D10 | Manual orders are entered in the web app only |
| D11 | Conservative fills: a buy fills at the ask, a sell at the bid, a limit only when the market reaches it. No midpoint fills |
| D12 | Modular: other strategies can be added later without touching the rest |

### 2.2 Checked on 2026-10-05 (read-only test with Trader-dev's own Questrade app)

- `GET symbols/{id}/options` returns the chain: 16 expiries for F, out to 2029-01-19 (LEAPS), strikes with a call id and a put id each, multiplier 100.
- `POST markets/quotes/options` returns quotes by contract ids and by filter (underlying, expiry, type, strike range): bid, ask, sizes, last trade and its time, volume, **open interest, IV (`volatility`, in percent), delta, gamma, theta, vega, rho**, `delay` 0, `isHalted`.
- `GET symbols/{id}` adds per underlying: `eps`, `pe`, `marketCap`, `dividend`, `exDate`, `yield`, `industrySector`, `securityType`, `hasOptions`.
- After the close the quotes still answer, with the last bid and ask. Whether option quotes are real-time during the session (delay 0 was reported after hours) is task T2's first check.

### 2.3 Where the rules spec changes an earlier answer

| Topic | Earlier answer | Rules spec (used) |
|---|---|---|
| Put delta | 0.25 to 0.35 (wheel-evaluator skill) | 0.20 to 0.30; 0.20 when size or trend is a caution or IV is above 0.40 |
| Managing a put | close at 50% of premium, else hold to expiry | also: business check, earnings exits, pin risk, and a time exit at 21 DTE (close if out of the money, take assignment if in the money) |
| Covered call strike | never below the assigned strike | never below **net cost** (strike minus put premium collected) |
| Tests | eight | ten (seven hard, three soft) with the spec's verdict ladder |
| Who acts | every order fills automatically (D7) | "the app recommends, the user acts" |

The last row is resolved this way: in the simulation, the wheel's recommended action becomes a simulated order automatically (D7). The spec's **owner inputs** stay with Stephen (§6.4).

### 2.4 Defaults chosen by the planner (each is a setting). Confirmed by Stephen 2026-10-05: P3 changed to SOFI, P5 and P9 confirmed

| # | Default | Why |
|---|---|---|
| P1 | `per_ticker_limit_pct_of_wheel_cash` = 0.50 | D9 |
| P2 | `itm_at_time_exit_preference` = ASSIGN | Stephen chose "accept assignment" |
| P3 | `benchmark_ticker` = **SOFI** (Stephen, 2026-10-05) | Stephen's choice. Note: the spec describes the benchmark as a broad index fund; SOFI is a single stock, so the quarterly pause rule (spec §10) compares the wheel account with one stock's quarter. It is a setting |
| P4 | Option fee 0.99 USD per contract, each way (setting) | Questrade's published option pricing; verify the currency in T2 |
| P5 | Early assignment is simulated in one case only: a short call that is in the money at the close on the day before the ex-dividend date, with less time value left than the dividend, is assigned that night. Every other assignment happens at expiry | American options can be assigned any time; this is the common case and the one the spec alerts on. A documented limit of the simulation |
| P6 | An option expires in the money when the underlying's official close is 0.01 or more through the strike: long options are exercised, short ones assigned | the clearing house's automatic-exercise rule |
| P7 | Options trade 09:30 to 16:00 ET only. Orders are day orders or good-till-cancelled | regular option hours |
| P8 | A stock split or other contract adjustment on a held contract freezes that position and raises an alert; it is not simulated | rare, complex, and matters only for long-dated holdings |
| P9 | When an owner answer is needed and none has come by the next session's scan, the wheel does nothing new on that ticker and repeats the Telegram prompt once a day | the spec gives these choices to the owner; the simulation never guesses them |

## 3. Architecture

```
                         existing, unchanged                         new
  ┌───────────────────────────────────────────────┐   ┌───────────────────────────────────────────────┐
  │ worker (stock run, ORB)   sim_broker   engine  │   │ options-worker (own process, own lock)         │
  └───────────────────────────────────────────────┘   │   OptionMarketData  ── chains, quotes, greeks  │
                                                        │   OptionBroker      ── orders, fills, ledger  │
        shared, read-only use by the new code           │   CollateralEngine  ── covered / defined-risk │
  QuestradeClient (extended), calendar, clock,          │   Lifecycle         ── expiry, assignment     │
  settings store, notifier, Telegram callbacks,         │   OptionStrategy plug-ins                     │
  cash_ledger and equity_snapshots (per run)            │     └─ wheel/  (rules engine = the spec)      │
                                                        │   Manual order service (web ticket)           │
                                                        └───────────────────────────────────────────────┘
```

Principles:

1. **The stock engine is not touched.** The ORB run, its worker, its tables and its replay golden files stay as they are, so this work is not a trading change for the soak. A static test fails the build if any file under `trader/engine`, `trader/broker`, `trader/strategies` or `trader/replay` changes in an OPTSIM commit (the existing D2 diff check, extended).
2. **A separate book.** The options account is a `runs` row with mode `options` (one active at a time), its own `sim_accounts` row, and its own rows in `cash_ledger` and `equity_snapshots`: one Options pool shared by manual trades and strategies (§3.7). `active_live_run_id` keeps returning the stock run.
3. **Generic core, strategy plug-ins.** The broker, collateral engine and lifecycle know nothing about the wheel. A strategy sees a read-only context and returns intents.
4. **Pure rules.** The wheel's screen, selection and daily evaluation are pure functions (inputs in, verdict or action out, no I/O, no clock), so the spec's acceptance cases run as plain unit tests.

### 3.1 The option strategy interface (the modularity contract)

| Element | Shape |
|---|---|
| `OptionStrategy` | `key`, `version`, `params_model` (pydantic, renders the settings form), `schedule(cal) -> events` (session-relative, like the stock framework), `on_event(ctx, event) -> intents`, `on_fill(ctx, fill) -> intents`, `on_lifecycle(ctx, event) -> intents` (expired, assigned, exercised, called away), `prompts(ctx) -> owner prompts` |
| `OptionStrategyContext` | clock, calendar, session date, `OptionMarketView` (chain, quotes, underlying facts), account state (cash, reserved collateral, equity), this strategy's positions, working orders and stored state, its params, `note()` for the decision log |
| Intents | `OpenStructure(legs, order_type, net_limit, tif, reason, evidence)`, `CloseStructure(structure_id, order_type, net_limit, reason)`, `Reprice(order_id, net_limit)`, `Cancel(order_id, reason)`, `SellShares(position_id, reason)` |
| Leg | contract (underlying, expiry, strike, right) or the underlying's shares; side; ratio |
| Registry | discovered by entry point group `trader.option_strategies`; `option_strategy_configs` rows hold enabled flag and versioned params; every order records the config id it came from |
| Strategy state | `option_strategy_state` (strategy key, scope key, JSON, version): a plug-in's own durable state without a migration per strategy |

A strategy never sizes against cash, never checks coverage and never fills anything: the collateral engine and the broker do. A second plug-in (for example credit spreads) ships as one package plus an entry point.

### 3.2 Orders, structures and positions

- An **order** has one or more legs and one net price. Types: market, limit (net debit or net credit). All legs fill together from one quotes pass, or none do.
- A **structure** groups what was opened together (a single put, a vertical spread, a covered call's short call) and carries the strategy key, or `manual`. P&L, collateral and the web pages work per structure.
- **Positions** are signed quantities per contract, plus share lots, in the options book only.
- Closing is another order against the structure's legs. Partial closes of a multi-leg structure are allowed only when what remains is still covered (the collateral engine decides).

### 3.3 Collateral engine (D5)

Accepts or rejects an order by pairing every short leg with cover, then reserves cash for the worst case. Reject reason `naked_short` when a short leg has no cover.

| Position | Cover | Cash reserved |
|---|---|---|
| Long call or put | none needed | the debit |
| Cash-secured put | cash | strike × 100 |
| Covered call | 100 shares per contract, not already covering another call | none |
| Debit spread (long leg covers the short leg: same right, long expiry ≥ short expiry, long strike on the protective side) | the long leg | the net debit |
| Credit spread | the long leg | width × 100 (the credit is added to cash, the width is reserved) |
| Iron condor and other combinations | each short leg paired once | the largest single-side loss |
| Calendar or diagonal | long expiry ≥ short expiry | debit, plus the strike gap × 100 when the long strike is on the wrong side |

Also enforced here: the per-position cap (`options.max_position_pct`, default 0.50 of account equity, by reserved cash or debit per underlying) and "cash never below zero" (no margin). The engine is one module with a table-driven test file; strategies and the manual ticket both go through it, and the ticket shows its result before submit.

### 3.4 Fill model (D11)

Per leg from the live option quote: a buy at the ask, a sell at the bid. A market order fills on the first usable quote. A limit order fills when the net of the legs' ask/bid prices is at or better than the net limit; the fill price is that net (never better than the market gave). No fill when: outside 09:30 to 16:00 ET, the quote is delayed, halted, older than `options.stale_quote_seconds`, one-sided, crossed, or a sell leg's bid is 0. Fees: `options.fee_per_contract` per contract per leg. Every fill stores each leg's quote (bid, ask, last, sizes, IV, delta, open interest, fetch time).

Marks for account value use liquidation prices, as the spec's `account_value` does: long options at the bid, short options at the ask, shares at the last trade.

### 3.5 Lifecycle

A post-close job on every session, for contracts expiring that day: decide in or out of the money from the underlying's official close (P6); exercise longs and assign shorts (shares and cash move at the strike, collateral is released); expire the rest at zero. Then the early-assignment check (P5). Each outcome is a lifecycle event handed to the owning strategy's `on_lifecycle` and written to the activity feed and Telegram. A structure whose legs resolve differently (a spread with one leg in the money) is handled leg by leg, with shares bought and sold at the two strikes the same night.

### 3.6 Processes and schedule (ET)

| When | What | Command |
|---|---|---|
| always | `options-worker`: polls quotes for working orders (every `options.quote_poll_seconds`, default 5) and open positions (every `options.mark_seconds`, default 60), fills, marks, alerts (`STRIKE_TOUCHED`), fires strategy events | supervisord program, own advisory lock, own heartbeat |
| 08:15 Mon-Fri | refresh underlying facts and chains for approved, held and candidate tickers | `trader options-refresh` |
| 10:30 Mon-Fri | the strategies' daily event: the wheel screens, evaluates each open position once, opens what is allowed | worker event `opt_daily` (cron backup 10:35) |
| 16:20 Mon-Fri | expiry and assignment, early-assignment check, end-of-day marks, equity snapshot, options summary to Telegram | `trader options-postclose` |
| Sat 08:00 | market-wide wheel screen for new candidates (D8) | `trader options-screen` |
| quarter end | benchmark review (spec §10) | inside `options-postclose` |

The stock crontab lines are not changed.

### 3.7 One Options pool (Stephen, 2026-10-05)

The options account is one pool of 5,000 USD, separate from the stock simulation's account. Manual trades and the wheel share it: the same cash, the same collateral check, the same per-position cap (`options.max_position_pct`, 0.50 of the account's value). The spec's `cash_usd` and `wheel_cash_usd` are both this account's cash, and `open_put_collateral_usd` counts every cash-secured put in it, manual ones included, so the wheel can never rely on cash a manual position has reserved (and the reverse). Each structure still records its source (`manual` or the strategy key), so results are reported per source and in total. Splitting the cash by source later would be a ledger column and a settings change; it is not built now.

## 4. Data

### 4.1 Questrade client additions (same pacing, 401 and 429 handling as today)

`option_chain(symbol_id)`, `option_quotes(ids)` (POST, batched), `option_quotes_filter(underlying, expiry, right, strike range)`, `symbol_details(ids)`. The POST path needs the client's retry logic, which today covers GET only.

### 4.2 Where the rules spec's inputs come from (spec §3.1, §3.2)

| Field | Source | Note |
|---|---|---|
| price, quote time, market open | Questrade quotes, session calendar | |
| eps_ttm, market cap, dividend yield, ex-dividend date, sector, security type | Questrade `symbols/{id}` | sector mapping to the spec's relaxed sectors is a small table (T8) |
| eps growth, debt to equity, book value per share, payout ratio, short float, next earnings date, RSI(14) | FinViz quote page (the existing scraper, new fields) | a missing field makes its test CAUTION `MISSING_DATA`, as the spec says |
| sma50, sma50 20 sessions ago, 52-week low and sessions since | daily candles (existing cache; needs 260 sessions of history for held and approved tickers) | computed, not scraped |
| leveraged or inverse ETF, broad or sector ETF | FinViz/Questrade description keywords plus an owner override per ticker | **ASSUMPTION**: no feed states this directly |
| bid, ask, delta, IV, open interest | Questrade option quotes | IV arrives in percent and is stored as a decimal |
| is_monthly | third Friday of the month (the Thursday when that Friday is a holiday) | computed |
| USD/CAD rate | the existing `fx.cad_usd_rate` setting, stored on every transaction | the spec asks for the rate per transaction |

### 4.3 New tables (migration 0011, schema `trader`)

| Table | Purpose |
|---|---|
| `option_contracts` | contract master: underlying symbol, Questrade id, root, expiry, strike, right, multiplier, monthly flag |
| `option_chain_cache`, `option_quote_marks` | the last chain per underlying; the latest quote and greeks per watched contract |
| `underlying_facts` | the spec §3.1 fields per ticker and date, with each field's source and age |
| `opt_orders`, `opt_order_legs` | orders with status, type, net limit, time in force, source (`manual` or a strategy config id), reject reason |
| `opt_fills` | one row per filled leg with the quote snapshot, fee and USD/CAD rate |
| `opt_structures` | groups of legs opened together: kind, strategy key, state, reserved collateral, realized P&L |
| `opt_positions` | signed quantity and average price per contract or share lot, per structure |
| `opt_lifecycle_events` | expired, exercised, assigned, called away, early assignment |
| `option_strategy_configs`, `option_strategy_state` | plug-in settings (versioned) and plug-in state |
| `wheel_tickers` | the approved list and owner inputs: status (candidate, approved, rejected), `would_own`, `ownership_reason`, `thesis_broken`, security-type override, acknowledged cautions |
| `wheel_positions`, `wheel_events` | the wheel state machine per ticker (spec §6) with roll count, premiums, net cost, fresh-cash answers and dates, drawdown reviews; every daily evaluation and its action |
| `owner_prompts` | questions waiting for Stephen: kind, ticker, choices, asked, answered, answer |

`cash_ledger`, `equity_snapshots`, `event_log`, `notifications` and `job_runs` are reused with the options run id.

## 5. The wheel plug-in

Package `trader/option_strategies/wheel/`: `config.py` (the spec's §2 block as the params model, every number a setting, each ASSUMPTION labelled in the form), `inputs.py` (typed inputs), `screen.py` (§4), `select.py` (§5.1, §5.2, §9.3), `evaluate.py` (§7 to §9), `stops.py` (§10), `calc.py` (§11), `alerts.py` (§12), `strategy.py` (the plug-in: gathers inputs, calls the pure functions, turns actions into intents and prompts).

### 5.1 From recommendation to simulated order

| Spec action | In the simulation |
|---|---|
| open a put (§5) | `OpenStructure`: sell-to-open limit at the midpoint, then `Reprice` one tick lower per `options.reprice_seconds` (default 60), never below the bid (§5.3). With D11 it fills when the limit meets the bid. Unfilled by the close: cancelled, re-evaluated next session |
| `CLOSE_PUT_PROFIT`, `CLOSE_PUT_TIME`, `CLOSE_PUT_BEFORE_EARNINGS`, `CLOSE_PUT_NOW`, `CLOSE_CALL_PROFIT`, `CLOSE_OR_EXPIRE_CALL` | `CloseStructure`: buy-to-close limit at the midpoint, repriced up one tick at a time to the ask |
| the standing 50% buy-to-close (§5.3) | not a resting order: the worker checks `ask <= premium × (1 − target)` on every mark and closes then. Same result, and no stale resting order |
| `ROLL_PUT` | one two-leg order (buy the current put, sell the candidate) for a net credit |
| `TAKE_ASSIGNMENT`, `HOLD_FOR_CALL_AWAY`, `HOLD`, `HOLD_UNCOVERED`, `SKIP_CALL_THIS_CYCLE` | no order; recorded with the reason |
| `SELL_CALL` | `OpenStructure` covered call, same order guidance |
| `SELL_SHARES`, `CLOSE_CALL_AND_SELL_SHARES` | `SellShares` at the market (bid), after closing the call |
| alerts (§12) | Telegram messages and the options page's alert list |

### 5.2 Owner inputs (spec §3.3, §9.2, §8 rows 4 and 5, §9.4 row 3)

Asked on Telegram with buttons (the existing signed-callback mechanism) and on the options page; stored in `owner_prompts` and on the wheel position.

| Prompt | Choices | Until answered (P9) |
|---|---|---|
| New candidate from the Saturday screen (D8) | Approve (then "why would you own it?", one sentence, required) or Reject | not traded |
| `NEEDS_REVIEW`: a hard-test caution | Acknowledge or Skip, per caution | no entry |
| Fresh-cash test (assignment day, before every call, every 30 days) | Yes or No, with the ten test results shown | no call is sold; a No is `SELL_SHARES` |
| `REVIEW_BEFORE_EARNINGS` | Close, Hold or Roll | hold |
| `PIN_RISK` (expiry day) | Buy to close or Accept | accept |
| `DRAWDOWN_REVIEW` | a written reason, or Sell | repeat daily |
| `thesis_broken` | a switch on the ticker's row in the web app | false |

### 5.3 Spec open decisions (spec §14) as this plan treats them

1 and 2: P1 and P3. 3: P2. 4 (manual override of a rejected ticker): not built; the manual ticket can still sell a put on any ticker as a `manual` structure, outside the wheel. 5 (send orders to the broker): no; simulation only. 6 (fresh-cash test uses the business check plus the owner's answer): as the spec has it. 7: every ASSUMPTION is a labelled setting.

## 6. Manual trading (web app)

New page **Options** with tabs:

| Tab | Contents |
|---|---|
| Account | account value (liquidation marks, the headline number per spec §11), cash, reserved collateral, free cash, premium collected (secondary), benchmark comparison |
| Positions | structures grouped by underlying: legs, quantity, entry, mark, P&L, DTE, delta, collateral, source (manual or strategy); Close button opens the ticket prefilled |
| Trade | pick an underlying, then an expiry; the chain shows calls and puts with bid, ask, last, delta, IV, open interest, volume. Clicking a bid or ask adds a leg. The ticket shows net debit or credit at the market, max loss, breakevens, collateral the order would reserve, cash after, and the collateral engine's verdict before Submit. Market or limit; day or good-till-cancelled |
| Orders | working orders with cancel and reprice; history with each leg's fill quote |
| Wheel | approved list with owner inputs, candidates with verdict and the ten tests, positions with state, net cost, roll count, next action and why; pending prompts |
| Activity | fills, lifecycle events, strategy decisions, alerts |

API under `/api/options/...` for each tab, plus the strategy settings forms from each plug-in's params model. Settings group "Options" for the generic settings.

## 7. Tasks

Contract-first, one owner per file, fakes for everything not yet built. Width up to 8 builders. Critical path: T1 → T5 → T12 → T16 → T17 (5 steps).

| ID | Task | Depends on | Owns |
|---|---|---|---|
| T1 | **Contracts**: value types (contract, leg, order, fill, structure, intents, lifecycle event, market view), the `OptionStrategy` protocol and registry interface, migration 0011, the options run mode and account, settings keys, fakes (market data, broker, strategy), the "stock engine untouched" test | none | `trader/options/types.py`, `protocols.py`, migration, fakes |
| T2 | Questrade option client: chain, quotes by id and by filter, symbol details, POST with retries; a live check command; confirms real-time quotes in session and the fee currency | T1 | `adapters/questrade/options.py` |
| T3 | Option market data service: contract master, chain cache, quote marks, staleness, monthly-expiry maths | T1 | `options/market.py` |
| T4 | Collateral engine with its table-driven tests (every row of §3.3, naked rejections, the cap, partial closes); manual and strategy orders are checked against the same account cash, and shares covering one call can't cover another whatever its source | T1 | `options/collateral.py` |
| T5 | Fill model and option broker: orders, all-or-none multi-leg fills, ledger entries, fees, structures and positions, reprice and cancel | T1 | `options/fill_model.py`, `broker.py` |
| T6 | Lifecycle: expiry, exercise, assignment, early assignment (P5), share lots, events | T1 | `options/lifecycle.py` |
| T7 | Strategy registry, config store, state store, event schedule | T1 | `option_strategies/registry.py` |
| T8 | Underlying facts: Questrade details, FinViz fields, candle-derived trend inputs, security-type classification, field ages | T1 | `options/facts.py`, FinViz parser additions |
| T9 | Wheel rules, pure: config, screen, select, evaluate, stops, calc, alerts. The spec's 16 acceptance cases are the first tests, then one test per table row | T1 | `option_strategies/wheel/*` except `strategy.py` |
| T10 | Owner prompts: store, Telegram buttons, answers, daily repeat | T1 | `options/prompts.py`, bot handlers |
| T11 | Wheel plug-in: inputs from T3/T8, calls T9, intents (§5.1), prompts (§5.2), wheel tables, market-wide candidate screen | T7, T9, T10 (fakes for T3, T5, T8) | `option_strategies/wheel/strategy.py`, `screener.py` |
| T12 | Options worker: polling, fills, marks, strategy events, alerts, heartbeat, lock | T3, T5, T7 | `options/worker.py`, supervisord, run script |
| T13 | Jobs: refresh, post-close (lifecycle, marks, snapshot, summary, benchmark review), Saturday screen; crontab lines | T6, T8 | `jobs/options_*.py`, crontab |
| T14 | API: account, positions, chain, ticket preview and submit, orders, wheel, prompts, activity, strategy settings | T1 (fakes) | `api/routers/options.py` |
| T15 | Web: the Options page and its six tabs | T14 contract | `web/src/pages/options/*` |
| T16 | Wiring and end-to-end: a seeded wheel cycle from screen to called-away on fake market data with a stepped clock (put sold, 50% close; put assigned, call sold, called away; roll once); a manual vertical spread through expiry; messages, summary line | T2 to T15 | `runtime` wiring, `tests/e2e/test_options_*` |
| T17 | Deploy to trader-dev, create the options run at 5,000 USD, live checks during a session (chain, a manual one-contract order filled and closed, the wheel's first daily evaluation), docs (SPEC, README) | T16 | deploy notes |

Waves: T1 alone; then T2 to T10 and T14 together (8 wide after T1, T15 joining when T14's contract is merged); then T11, T12, T13; then T16; then T17. Each task goes through the usual check (verifier, breaker, reviewers) with the full gate once per wave.

### 7.1 Acceptance, whole feature

1. The spec's 16 acceptance cases pass against the pure wheel rules, and every threshold is read from the config (a test changes each one and sees the result move).
2. No order that leaves a short leg uncovered is ever accepted, by hand or from a strategy (the breaker's main target).
3. A sell never fills above the bid and a buy never below the ask; nothing fills outside option hours or on a stale, delayed, halted, one-sided or zero-bid quote.
4. Cash plus reserved collateral always reconciles to the ledger; cash or shares reserved by one structure are never counted for another, whether manual or from a strategy; account value uses liquidation marks.
5. A put assigned at expiry becomes 100 shares at the strike with net cost per the spec; a covered call called away records the full-cycle result (spec §11).
6. A toy second strategy (a test-only plug-in that buys one call) installs through the entry point and trades with no change outside its own package.
7. The stock engine's golden replay and the "untouched" test pass on every OPTSIM commit; the soak is not reset by this work.
8. Stopping and restarting the options worker mid-session loses no order, fill or prompt.

## 8. Limits of the simulation (to keep in view when reading results)

- Fills assume the displayed bid or ask size is available for the whole order. Realistic for 1 to 5 contracts on liquid chains, optimistic on thin ones; the liquidity test and the ticket's open-interest display are the guard.
- Early assignment is simulated only before ex-dividend dates (P5).
- Contract adjustments are not simulated (P8).
- No replay mode for options in this version: historical option quotes are not stored by Questrade's API. The quotes this system records from now on can feed a later replay.
- Results are in USD with the USD/CAD rate recorded per transaction; no tax treatment.

## 9. Answers from Stephen (2026-10-05)

1. An unanswered owner prompt: the wheel does nothing new on that ticker and repeats the prompt daily (P9 confirmed).
2. Early assignment only before ex-dividend dates is enough for now (P5 confirmed).
3. Benchmark: SOFI (P3).
4. **One Options pool of 5,000 USD**, shared by manual trades and the wheel, separate from the stock account (§3.7). (An earlier reading of this answer as separate pools per source was a misunderstanding and has been removed.)
5. A manual position may sit alongside a wheel position on the same ticker; only a second wheel position is blocked.
