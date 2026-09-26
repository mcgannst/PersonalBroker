# Technical Specification — Trader Simulation Platform

| | |
|---|---|
| **Document** | SPEC v0.1 (draft for review) |
| **Implements** | [`BRD.md`](BRD.md) |
| **Date** | 2026-09-26 |

Items marked **⚠ VERIFY** are assumptions that the Phase 0 spikes must confirm before any code depends on them.

---

## 1. Architecture overview

```
                      ┌──────────────── Docker container: trader ────────────────┐
  Cloudflare Tunnel   │                                                           │
  (cloudflared,       │  supervisord                                              │
   separate) ────────►│   ├─ api        uvicorn  FastAPI  (REST + serves React)   │
                      │   ├─ worker     python -m trader.worker  (market-hours     │
                      │   │             loop: fill simulator, Telegram bot,        │
                      │   │             order monitor)                             │
                      │   └─ cron       supercronic  (scheduled jobs → CLI)        │
                      │                                                           │
                      │  trader/  (Python package: engine, strategies, adapters)  │
                      └───────┬───────────────┬──────────────┬──────────────┬─────┘
                              │               │              │              │
                        PostgreSQL      Questrade API    FinViz (HTTP)   Claude API
                        (external)      (read-only)      scrape          Telegram API
```

**Processes in the container (managed by supervisord):**

| Process | Role | Runs |
|---|---|---|
| `api` | FastAPI REST API + static React build; session auth | Always |
| `worker` | Long-running loop: polls quotes for working orders, simulates fills, runs the Telegram bot (long polling), expires proposals, sends the intraday events the worker schedules itself | Always; active work only during the market session |
| `cron` | [supercronic](https://github.com/aptible/supercronic) runs the `trader` CLI commands on schedule. It's a cron built for containers that passes env vars and logs to stdout | Always |

Scheduled jobs and the worker talk to each other **only through PostgreSQL** (tables plus `LISTEN/NOTIFY`), so any process can restart without losing state.

## 2. Technology choices

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| API | FastAPI + Pydantic v2 |
| DB access | SQLAlchemy 2.x (Core + ORM), Alembic migrations, psycopg 3 |
| HTTP clients | httpx (async), tenacity (retries) |
| Scraping | httpx + selectolax (HTML parse); polite rate limit |
| Scheduling | supercronic, `CRON_TZ=America/New_York` |
| Market calendar | `exchange_calendars` (NYSE, TSX): holidays and early closes |
| Data | pandas / numpy for indicator maths |
| AI | `anthropic` Python SDK; model is configurable (default `claude-sonnet-5`, with `claude-haiku-4-5-20251001` as a cheaper option) |
| Telegram | `python-telegram-bot` v21 (long polling, inline keyboards) |
| Web | React 18 + TypeScript + Vite; TanStack Query; Recharts for charts; built in a multi-stage Docker build and served by FastAPI |
| Auth | Argon2 password hash; signed HTTP-only session cookie; optional TOTP; plus Cloudflare Access in front |
| Tests | pytest, pytest-asyncio, respx (HTTP mocks), testcontainers-postgres; Vitest for the UI |
| Logging | structlog, JSON to stdout, and mirrored to the `event_log` table for the UI |

## 3. Repository layout

```
Trader/
├── docs/                     BRD.md, SPEC.md
├── reports/                  research reports
├── app/
│   ├── pyproject.toml
│   ├── trader/
│   │   ├── config.py         settings (env + DB-backed runtime settings)
│   │   ├── db/               models.py, session.py, migrations/ (alembic)
│   │   ├── adapters/
│   │   │   ├── questrade/    auth.py, client.py, models.py
│   │   │   ├── finviz/       scraper.py, parser.py
│   │   │   ├── claude/       catalyst.py, reports.py
│   │   │   └── telegram/     bot.py
│   │   ├── market/           calendar.py, clock.py, indicators.py, data_service.py
│   │   ├── strategies/       base.py, registry.py, orb_sip.py, spy_overlay.py
│   │   ├── engine/           orchestrator.py, risk.py, proposals.py, killswitch.py
│   │   ├── broker/           base.py, sim_broker.py, fill_model.py, ledger.py
│   │   ├── replay/           runner.py, candle_fill_model.py
│   │   ├── reports/          metrics.py, daily.py, weekly.py, export.py
│   │   ├── jobs/             one module per scheduled job
│   │   ├── api/              main.py, routers/*, auth.py, schemas.py
│   │   ├── worker.py
│   │   └── cli.py            `trader <job>` entry points
│   └── tests/
├── web/                      React app (Vite)
└── docker/
    ├── Dockerfile            multi-stage: web build → python runtime
    ├── supervisord.conf
    ├── crontab
    └── compose.example.yml   trader + cloudflared (Postgres external)
```

## 3a. Key concepts

| Term | Meaning |
|---|---|
| **Run** | A single simulation context: the one continuous `live` run, or one `replay` run. Every trading record carries a `run_id`, so replay results never mix with live ones |
| **Clock** | Gives the current time. `RealClock` is used for live runs. `ReplayClock` steps through past timestamps. All logic takes the time from the clock and never calls `datetime.now()` directly |
| **Intent** | What a strategy wants: enter, exit, or cancel |
| **Proposal** | An intent after the risk manager has approved it. It waits for approval, or is approved automatically |
| **Order** | A simulated order at the simulated broker |
| **Fill** | An order execution with a price, quantity, and the quote that caused it |

## 4. External integrations

### 4.1 Questrade API (read-only)

**Authentication.** OAuth refresh-token flow for a personal app.
- `GET https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token=<token>` returns an `access_token`, a new `refresh_token`, `api_server` and `expires_in`.
- **The refresh token can only be used once.** The new one must be saved (encrypted in `api_credentials`) in the same transaction, before the access token is used.
- One `QuestradeAuth` service owns the token. It uses a Postgres advisory lock so that two processes never refresh at the same time.
- A daily cron job refreshes the token even on weekends, so it doesn't expire (⚠ VERIFY: expiry after about 7 days without use).
- Initial setup: paste the refresh token from the Questrade API Centre into Settings → Questrade.

**Endpoints used.** All ⚠ VERIFY exact paths and parameters in Phase 0.

| Purpose | Endpoint |
|---|---|
| Look up a symbol's ID | `GET /v1/symbols/search?prefix=` and `GET /v1/symbols?names=` |
| Quotes (bid/ask/last/volume) | `GET /v1/markets/quotes?ids=` (batch) |
| Candles | `GET /v1/markets/candles/{id}?startTime=&endTime=&interval=OneMinute|FiveMinutes|OneDay` |

**Rate limits.** Questrade returns HTTP 429 with rate-limit headers when a limit is exceeded. The client uses a token-bucket limiter set to the documented per-second and per-hour limits for market-data calls (⚠ VERIFY the numbers), plus exponential backoff on 429 and 5xx responses.

**⚠ VERIFY in Phase 0:**
1. Whether quotes are real-time or delayed for US and TSX stocks on Stephen's account. This decides whether quote-based fills are valid.
2. The maximum number of candles per request. The Claude connector capped results at 40, but the direct API may allow more.
3. How far back intraday history goes (this limits replay).
4. Whether a quote includes the pre-market last price.

### 4.2 FinViz scraper

- **Universe (nightly):** the screener URL is built from settings, for example `https://finviz.com/screener.ashx?v=111&f=sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa`. It pages through the results 20 rows at a time and parses the ticker, price, volume and sector.
- **Pre-market (08:00 ET):** candidate tickers come from (a) a FinViz "news today / earnings today" screen and (b) a Questrade quote check on the universe for pre-market change ≥ 3% (⚠ VERIFY pre-market data). Headlines come from each ticker's FinViz quote page.
- **Politeness:** at most 1 request per 2 seconds, a browser User-Agent, results cached for 12 hours, and backoff when blocked.
- **Isolation:** all HTML parsing lives in `finviz/parser.py`, with tests against saved HTML samples. A parse failure triggers an alert and falls back to the previous night's universe.
- **Manual fallback:** the web app can upload a CSV watchlist.

### 4.3 Claude API: catalyst classification

- **Input:** ticker, company name, up to 10 headlines with timestamps, the gap %, and the earnings date.
- **Output** (JSON-schema structured output):

```json
{ "catalyst_type": "earnings_beat|earnings_miss|guidance|analyst_action|m_and_a|regulatory|contract|offering|rumour|none",
  "direction": "bullish|bearish|neutral",
  "quality": 0-100,
  "is_confirmed": true,
  "reason": "≤ 30 words" }
```

- **Budget:** a daily cap on calls and tokens (`claude.daily_budget_usd`). If it's exceeded, classification is skipped with `catalyst_type=unknown` and the system alerts.
- **Where it's used:** the pre-market scan (all candidates), and at 9:35 for any top-20 names not already classified.
- **Weekly report:** Claude writes 150–300 words of commentary from the metrics, following strict instructions not to invent numbers.

### 4.4 Telegram bot

- Long polling runs inside `worker`. **Only messages from the configured `telegram.chat_id` are accepted.**
- **Messages:** new proposal (with **✅ Approve / ❌ Reject** buttons), fill, stop hit, overlay decision, flatten, kill-switch trip, job failure, token failure, daily summary (with a **Rules followed? Yes / No** button), weekly report link.
- A button press calls the same `ProposalService.decide()` that the web app calls. Both routes do exactly the same thing. The first decision wins, and any later decision gets an "already decided" reply.
- Messages link to the web app URL for details.

## 5. Strategy plug-in framework

### 5.1 Interface

```python
class Strategy(Protocol):
    key: str                      # "orb_sip"
    version: str                  # "1.0.0"
    kind: Literal["entry", "overlay"]
    params_model: type[BaseModel] # pydantic model → validated settings + UI form

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]: ...
    def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]: ...
    def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]: ...
```

- **`StrategyContext`** gives read-only access to: the clock, the data service (quotes, candles, indicators), cached universe and candidate data, open positions and orders for this strategy, account state, and the strategy's settings.
- **`Intent`** is one of `EnterLong(symbol, order_type, stop/limit, stop_loss, reason, evidence)`, `Exit(position_id, order_type, reason)`, or `Cancel(order_id, reason)`. **Strategies never size positions or place orders.** The risk manager and the broker do that.
- **Registry:** plug-ins are found through the `trader.strategies` entry point. `strategy_configs` rows turn each one on or off and hold its settings, versioned so every trade records the exact settings used.
- **Scheduled events** are defined relative to the session (`open+5m`, `close-30m`, `close-10m`), so early-close days work automatically.

### 5.2 Plug-in: `orb_sip` (ORB on Stocks in Play), version 1.0.0

| Setting | Default | Notes |
|---|---|---|
| `price_min` / `price_max` | 5 / 50 | |
| `min_avg_volume` | 1,000,000 | 14-day |
| `min_atr` | 0.50 | 14-day ATR |
| `rvol_min` | 1.00 | opening 5-min bar vs 14-day average of the same bar |
| `top_n` | 20 | ranking pool |
| `max_positions` | 1 | per day |
| `require_catalyst` | true | `quality ≥ catalyst_min_quality` and `direction != bearish` |
| `catalyst_min_quality` | 50 | |
| `stop_atr_fraction` | 0.10 | |
| `entry_offset` | 0.01 | buy stop = opening-range high + offset |
| `entry_cancel_at` | `open+120m` | 11:30 ET; `null` = work all day (the paper's rule) |
| `exit_at` | `close-10m` | flatten |
| `doji_body_pct_max` | 0.10 | body/range ≤ 10% counts as a doji → skip |

**Event `open+5m` (9:35:05 ET):**
1. For every symbol in the universe, fetch the 9:30–9:35 five-minute bar (batched and rate-limited).
2. `rvol = bar.volume / avg_open_bar_volume_14d` (from the nightly cache).
3. Keep names with `rvol ≥ rvol_min`, sort by rvol descending, and take the top `top_n`.
4. Remove any name whose candle is bearish or a doji, or whose price or ATR is outside the limits.
5. Add catalysts, classifying any name that has no catalyst yet. Apply `require_catalyst`.
6. Emit `EnterLong` for the highest-ranked survivor: a buy stop at `bar.high + entry_offset`, with `stop_loss = entry − stop_atr_fraction × ATR14`.
7. Save every ranked candidate to `candidates` along with the reason each one was rejected, for later review.

**`on_fill` (entry filled):** emit a protective **stop** order `Exit(order_type=stop, stop=stop_loss)`.

**Event `entry_cancel_at`:** emit `Cancel` for an entry that hasn't filled.

**Event `exit_at`:** emit `Exit(market)` for any open position.

### 5.3 Plug-in: `spy_overlay`, version 1.0.0 (kind = overlay)

| Setting | Default |
|---|---|
| `decision_at` | `close-30m` (15:30 ET) |
| `benchmark` | SPY |
| `signal` | `rest_of_day` = SPY return from the prior close to now |

**Event `decision_at`:** if the signal is ≤ 0, emit `Exit(market, reason="overlay_negative")` for every open position from entry strategies. Otherwise emit nothing (hold into the close), and log the decision either way.

## 6. Engine pipeline

```
Strategy.on_event / on_fill
        │ Intent
        ▼
RiskManager ── rejects? → log + (alert if relevant)
        │ sized, checked
        ▼
ProposalService ── approval_mode = manual? → status=pending, Telegram + UI, await decision/expiry
        │ approved (or auto)
        ▼
SimBroker.submit(order) → working order
        │
Worker fill loop (quotes) → Fill → Ledger/Positions → Strategy.on_fill
```

### 6.1 RiskManager
- **Sizing:**

  ```
  risk_$ = equity × risk_pct
  shares_risk = floor(risk_$ / (entry − stop_loss))
  shares_cash = floor(buying_power / (entry × (1 + slippage_buffer)))
  shares = min(shares_risk, shares_cash)
  ```

  Reject the trade if shares is 0.
- **Checks, in order:**
  1. Kill switch not tripped.
  2. Daily realized + unrealized P&L above the daily loss limit.
  3. `max_positions` not exceeded.
  4. Market open and not within `no_entry_before_close` (default 30 min).
  5. The settled-cash rule, when `cash_account_mode=true`: buying power = **settled** cash only.
  6. The symbol's market is enabled.
- **Exits and cancels are never blocked,** so a kill switch can't trap an open position.

### 6.2 ProposalService

| Field | Values |
|---|---|
| `status` | `pending → approved → submitted` · `pending → rejected` · `pending → expired` · `auto_approved → submitted` |
| `expires_at` | entries: `now + proposal_ttl_entry` (default 5 min); protective stops: `proposal_ttl_stop` (default 3 min); exits: `proposal_ttl_exit` (default 5 min) |

- In manual mode, a **protective stop** that expires triggers an **escalating alert** every 60 seconds (configurable), and the position stays unprotected. That's deliberate, because it rehearses what real trading without bracket orders would be like. `unprotected_seconds` is recorded.
- In manual mode, a **flatten** that expires triggers an escalation alert. Setting `auto_flatten_on_expiry` (default **true** in simulation) auto-submits the flatten when it expires, so no position is ever held overnight (BR-42).
- The approval-mode setting is **global** and stored in `settings`. Every change is written to the audit log.

### 6.3 Kill switches (`killswitch.py`)
Checked before every entry proposal and after every fill.

| Switch | Default | Trip effect |
|---|---|---|
| `daily_loss_pct` | 5% | Block entries until the next session (resets automatically) |
| `max_drawdown_pct` | 15% from peak equity | Block entries until re-enabled manually |
| `expectancy_min_trades` / `expectancy_threshold_R` | 50 / 0.0 | After N closed trades, block entries if expectancy ≤ threshold; manual re-enable |

When a switch trips, the system sends a Telegram alert and writes to `kill_switch_events`. Re-enabling is done in the web app, requires typing a reason, and is audit-logged.

## 7. Simulated broker and fill model

### 7.1 Order types
`market`, `limit`, `stop`, `stop_limit`; time in force `day` (and `gtc` for completeness); the side is `buy` or `sell`, long only. Every order is also cancelled at the end of the session.

### 7.2 Quote-based fill model (live)
The worker polls quotes for symbols with working orders every `quote_poll_seconds` (default 2 s, batched). For each quote `q` (bid, ask, last, time):

| Order | Trigger | Fill price |
|---|---|---|
| Buy market | immediately | `ask + slip` |
| Sell market | immediately | `bid − slip` |
| Buy stop | `last ≥ stop` or `ask ≥ stop` | `max(stop, ask) + slip` |
| Sell stop | `last ≤ stop` or `bid ≤ stop` | `min(stop, bid) − slip` |
| Buy stop-limit | as buy stop, then fill only if `ask + slip ≤ limit` | `ask + slip` |
| Limit buy / sell | `ask ≤ limit` / `bid ≥ limit` | the limit price |

- `slip = max(slippage_min, slippage_bps × price)`, default `$0.01` / `5 bps`, configurable.
- **Stale quotes:** if `now − q.time > stale_quote_seconds` (default 10), don't fill. Flag a `stale_quote` event and alert if it persists.
- **Partial fills:** out of scope. The full quantity fills (the order sizes are small).
- **Fees** per fill: commission (default $0), ECN (only if the direct-route flag is set), and the SEC fee on sells at `0.0000206 × value` (configurable).
- Every fill stores the triggering quote (bid, ask, last, time) in `fills.quote_snapshot` for auditing.

### 7.3 Ledger
- The `cash_ledger` records every movement with `trade_date`, `settle_date = next trading day` (T+1, based on the exchange calendar), currency and amount.
- **Settled cash** = entries where `settle_date ≤ today`. **Buying power** = settled cash when `cash_account_mode`, otherwise total cash.
- Account currency is USD by default. A one-time CAD→USD conversion at start uses a configurable FX rate and fee (default 1.5%).

### 7.4 Candle-based fill model (replay)
It's used because past quotes aren't available. It uses 1-minute candles in time order. A buy stop triggers when `bar.high ≥ stop` and fills at `max(stop, bar.open) + slip + half_spread_estimate`. A sell stop triggers when `bar.low ≤ stop` and fills at `min(stop, bar.open) − slip − half_spread_estimate`. If one bar touches both the entry and the stop, assume the **worst case** (stopped out). `half_spread_estimate` defaults to 5 bps.

## 8. Replay mode

- **Input:** date range, strategy settings snapshot, fill-model settings, starting capital. Started from the web app or with `trader replay --from --to`.
- **Execution:** creates a `runs` row with `mode=replay`, then steps a `ReplayClock` through each session, running the same strategies, risk manager and ledger. Approvals are **always automatic** in replay.
- **Data:**
  - It uses cached candles first and fetches from Questrade what's missing.
  - It rebuilds each past day's universe from the stored nightly snapshots. Where no snapshot exists, it uses the current universe as a stand-in. **That introduces survivorship bias, so the result is labelled "biased universe".**
- **Output:** the same trades, fills and metrics tables, filtered by `run_id`. The UI compares a replay against the live run.
- **Determinism:** the same inputs must give the same results. Replay makes no Claude calls; it uses stored catalysts or treats every catalyst as `unknown` (setting `replay_catalyst_mode`).

## 9. Schedule

All times are **ET**, from supercronic with `CRON_TZ=America/New_York`. Every job first checks the exchange calendar and does nothing on a market holiday. Events tied to the session times (the entry at 9:35, cancel at 11:30, overlay at 15:30, flatten at 15:50) are fired by the **worker** from `Strategy.schedule()`, so early closes (13:00 ET) work automatically. Cron only handles the day-level jobs and triggers the worker's intraday events as a backup.

| ET | MT | Job | Command |
|---|---|---|---|
| 02:00 daily | 00:00 | Questrade token keep-alive | `trader token-refresh` |
| 20:00 Sun–Thu | 18:00 | Nightly: FinViz universe → symbol IDs → daily candles, ATR14 → opening-bar history (14 d) → cache | `trader nightly` |
| 08:00 Mon–Fri | 06:00 | Pre-market scan: gappers/news, headlines, Claude catalysts, watchlist → Telegram brief | `trader premarket` |
| 09:20 Mon–Fri | 07:20 | Pre-open check: token, data freshness, kill-switch state, worker heartbeat | `trader preopen` |
| 09:35:05 | 07:35 | **ORB event** (worker; cron backup at 09:36 if the event didn't fire) | `trader event orb_open` |
| 11:30 / 13:30 | 09:30 / 11:30 | Check-ins: status push; entry-cancel event at 11:30 | `trader checkin` |
| 15:30 | 13:30 | SPY overlay (worker) | — |
| 15:50 | 13:50 | Flatten (worker); cron backup at 15:55 | `trader event flatten` |
| 16:15 Mon–Fri | 14:15 | Post-close: end-of-day orders, journal, metrics, equity snapshot, daily summary | `trader postclose` |
| Sat 09:00 | 07:00 | Weekly report + Claude commentary | `trader weekly` |

Each job records a `job_runs` row with status, start and end time, and any error. Jobs can safely be re-run: they're keyed by `(job, session_date)`.

## 10. Data model (PostgreSQL, schema `trader`)

Timestamps are `timestamptz` in UTC. Money is `numeric(14,4)`. Primary keys are `bigint identity` unless noted.

| Table | Key columns | Purpose |
|---|---|---|
| `settings` | key (pk), value jsonb, updated_at, updated_by | Runtime settings (approval_mode, capital, markets, slippage, TTLs…) |
| `api_credentials` | provider (pk), refresh_token_enc, access_token_enc, api_server, expires_at, last_refresh_at | Questrade tokens (encrypted with `APP_ENCRYPTION_KEY`) |
| `runs` | id, mode (`live`/`replay`), started_at, params jsonb, status, label | Keeps live and replay results separate |
| `sim_accounts` | id, run_id, currency, starting_cash, fx_rate, fx_fee | One per run |
| `strategy_configs` | id, strategy_key, version, params jsonb, enabled, created_at | Versioned settings; trades reference the config id |
| `symbols` | id, ticker, exchange, questrade_id, currency, name | Symbol master |
| `universe_snapshots` | session_date, symbol_id, price, avg_volume, atr14, source | Nightly FinViz universe (kept for replay) |
| `daily_candles` | symbol_id, date, o,h,l,c, volume, vwap | Cache |
| `intraday_candles` | symbol_id, ts, interval, o,h,l,c, volume, vwap | Cache (5m, 1m); partitioned by month |
| `open_bar_stats` | symbol_id, session_date, avg_open_vol_14d, atr14 | Precomputed each night |
| `catalysts` | id, symbol_id, session_date, headlines jsonb, type, direction, quality, confirmed, reason, model, cost_usd | Claude output |
| `candidates` | id, run_id, session_date, strategy_key, symbol_id, rvol, rank, candle jsonb, passed bool, reject_reason | Every ranked name, including rejected ones |
| `signals` | id, run_id, strategy_config_id, symbol_id, ts, intent jsonb, evidence jsonb | What the strategy wanted |
| `proposals` | id, run_id, signal_id, kind (entry/stop/exit/cancel), order_spec jsonb, qty, status, created_at, expires_at, decided_at, decided_via (telegram/web/auto), decision_latency_ms | Approval workflow |
| `orders` | id, run_id, proposal_id, symbol_id, side, type, qty, stop, limit, tif, status, submitted_at, closed_at | Simulated orders |
| `fills` | id, order_id, ts, qty, price, fees jsonb, quote_snapshot jsonb, slippage | Executions |
| `positions` | id, run_id, symbol_id, strategy_config_id, qty, avg_price, opened_at, closed_at, stop_order_id, unprotected_seconds | Position life cycle |
| `trades` | id, run_id, position_id, entry_price, exit_price, qty, pnl, pnl_R, planned_risk, exit_reason, slippage_total | Round trips (for metrics) |
| `cash_ledger` | id, run_id, ts, trade_date, settle_date, currency, amount, kind, ref | Cash and T+1 settlement |
| `equity_snapshots` | run_id, ts, equity, cash, settled_cash, peak_equity, drawdown_pct | Equity curve |
| `journal` | run_id, session_date, rules_followed bool, notes, answered_via | Daily adherence |
| `kill_switch_events` | id, run_id, switch, tripped_at, value, reset_at, reset_reason | Kill-switch history |
| `job_runs` | id, job, session_date, started_at, finished_at, status, error | Operations |
| `event_log` | id, ts, level, source, run_id, message, data jsonb | Timeline for the UI |
| `users` | id, username, password_hash, totp_secret_enc | Single user |
| `audit_log` | id, ts, actor, action, before jsonb, after jsonb | Settings and approval changes |

**Views:** `v_daily_pnl`, `v_trade_metrics` (per run: count, win rate, average win/loss R, expectancy, profit factor, max drawdown, average slippage, adherence %).

## 11. REST API (FastAPI, prefix `/api`)

All endpoints need a session cookie, except `/api/auth/login` and `/api/health`.

| Method & path | Purpose |
|---|---|
| `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Session auth (with optional TOTP) |
| `GET /health` | Liveness: DB, token age, worker heartbeat |
| `GET /dashboard?run=live` | Today: session phase, next events, candidates, pending proposals, positions, P&L, kill-switch state |
| `GET /proposals?status=pending` · `POST /proposals/{id}/approve` · `POST /proposals/{id}/reject` | Approvals |
| `GET /candidates?date=` | Ranked list with the reason each name was rejected |
| `GET /orders`, `GET /fills`, `GET /positions`, `GET /trades?run=&from=&to=` | Trading history |
| `GET /metrics?run=&from=&to=` · `GET /equity?run=` | Performance |
| `GET /journal` · `PUT /journal/{date}` | Adherence |
| `GET /killswitch` · `POST /killswitch/{switch}/reset` | Kill switches |
| `GET /settings` · `PUT /settings/{key}` | Runtime settings (validated) |
| `GET /strategies` · `PUT /strategies/{key}` | Plug-in list, settings form schema (JSON Schema from pydantic), enable/disable |
| `POST /replays` · `GET /replays` · `GET /replays/{id}` | Replay runs |
| `GET /jobs` · `POST /jobs/{job}/run` | Job history, manual trigger |
| `GET /events?since=` | Event timeline |
| `GET /stream` | Server-Sent Events for live dashboard updates |
| `POST /credentials/questrade` | Paste the initial refresh token |
| `GET /export/trades.csv` | CSV export |

## 12. Web application (React)

| Page | Contents |
|---|---|
| **Dashboard** | Session timeline (pre-market → close) showing completed and next steps in MT; the approval-mode badge; pending approvals with countdowns and Approve/Reject buttons; open position (price, P&L, stop, unprotected time); today's P&L; kill-switch lights; latest events |
| **Candidates** | Pre-market watchlist with catalyst cards; the 9:35 relative-volume ranking table (passed and rejected, with reasons) |
| **Trades** | Trade list with details: signal evidence → proposal → decision → order → fill quote; a 5-min chart with entry, stop and exit marked |
| **Performance** | Equity curve, drawdown, R-multiple histogram, metrics tiles (expectancy, win rate, profit factor, slippage, adherence), and a filter by run or date range |
| **Journal** | Daily rules-followed record and notes |
| **Replay** | Start a replay (date range, settings), see progress, see results, compare with live |
| **Settings** | Approval mode toggle; capital/currency/markets; fill model and slippage; TTLs; kill-switch thresholds; strategy plug-in settings (form generated from the JSON Schema); Claude budget; Telegram test button |
| **System** | Job runs, API token status and last refresh, worker heartbeat, error log, rate-limit usage |

- The layout works on a phone (for approving away from home). Times are shown in **America/Edmonton**.
- Live updates use SSE.

## 13. Configuration and secrets

**Environment variables** (from `.env`, never baked into the image):

| Var | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://trader:…@host:5432/trader` |
| `APP_ENCRYPTION_KEY` | Fernet key for tokens and TOTP secrets |
| `SESSION_SECRET` | Cookie signing |
| `ANTHROPIC_API_KEY` | Claude |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Bot and the one authorized chat |
| `PUBLIC_BASE_URL` | Tunnel URL used in Telegram links |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD_INITIAL` | First-run user creation only |
| `TZ_DISPLAY` | `America/Edmonton` |

**Runtime settings** (the `settings` table, editable in the UI) include `approval_mode` (`manual`|`auto`), `starting_cash`, `account_currency`, `markets_enabled`, `cash_account_mode`, `risk_pct`, `quote_poll_seconds`, `slippage_*`, `stale_quote_seconds`, `proposal_ttl_*`, `auto_flatten_on_expiry`, `killswitch.*`, `claude.model`, and `claude.daily_budget_usd`.

## 14. Security

- **Network:** the container listens only on an internal Docker network. `cloudflared` publishes it, **Cloudflare Access** (email one-time code or IdP) guards the hostname, and the app login is a second layer.
- **Cookies:** HttpOnly, Secure, SameSite=Strict; CSRF token on changes; login rate limiting and lockout.
- **Secrets:** tokens and secrets are encrypted at rest with Fernet, and nothing sensitive is logged.
- **Telegram:** chat-ID allow-list; the callback data contains a signed proposal ID plus a nonce.
- **Container:** runs as a non-root user; read-only root filesystem except `/tmp`; minimal base image; pinned dependencies.
- **Audit:** every approval, setting change, kill-switch reset and credential change is written to `audit_log`.

## 15. Deployment

- **Image:** multi-stage Dockerfile. `node:22-alpine` builds `web/` → `python:3.12-slim` runtime with the app, the static build, supercronic and supervisord.
- **Compose** (`compose.example.yml`): `trader` + `cloudflared`. Postgres is external, reached through `DATABASE_URL`.
- **Start-up:** `alembic upgrade head` → create the admin user if missing → supervisord.
- **Health check:** `GET /api/health`.
- **Backups:** handled on the Postgres side (Stephen's existing process).

## 16. Testing strategy

| Level | What |
|---|---|
| Unit | Indicators (ATR, relative volume, doji), sizing, kill switches, every fill-model rule, T+1 settlement across weekends and holidays, the proposal state machine |
| Adapters | Questrade client against recorded responses (respx), including token rotation and 429 backoff; the FinViz parser against saved HTML; the Claude output schema validation |
| Strategy | `orb_sip` and `spy_overlay` against hand-built candle scenarios (breakout, no fill, stop hit, doji, bearish candle, early close) |
| Integration | Postgres in testcontainers: a full simulated day from the nightly job to post-close with a fake clock and fake data |
| Replay determinism | The same inputs give identical trades (a golden-file test) |
| UI | Vitest component tests; a Playwright smoke test (login → dashboard → approve) |

## 17. Phase 0 spikes (before building)

| # | Spike | Pass criteria |
|---|---|---|
| S1 | Questrade personal-app token refresh and rotation | Refresh works 3 times in a row, and the stored token stays valid |
| S2 | Quote freshness | The quote timestamp during market hours is within 2 s of the current time for US names (records whether quotes are delayed) |
| S3 | Candle limits and history depth | The maximum candles per request, and the earliest 1-min/5-min history available |
| S4 | Universe scan timing | Fetching 9:30–9:35 bars for the universe at 9:35 finishes in under 60 s within the rate limits |
| S5 | FinViz scrape | Universe and news pages parse correctly; note any blocking |
| S6 | Telegram inline approval round-trip | Button → callback → DB update in under 2 s |

## 18. Open items for Stephen

1. PostgreSQL host and version, and whether a dedicated `trader` database and user is fine.
2. The Docker host: which Proxmox VM or LXC.
3. The Cloudflare hostname to use, and your Cloudflare Access identity method (email one-time code, or Google).
4. The Claude daily budget cap. The suggested default is US$1/day. I'll confirm model pricing before the build.
