# Technical Specification — Trader Simulation Platform

| | |
|---|---|
| **Document** | SPEC v1.0 (approved) |
| **Implements** | [`BRD.md`](BRD.md) |
| **Date** | 2026-09-26 (v0.4: separate dev and prod environments) |
| **Approved** | Stephen McGann, 2026-09-26 |

Items marked **⚠ VERIFY** are assumptions that the Phase 0 spikes must confirm before any code depends on them.

---

## 1. Architecture overview

```
                      ┌──────────────── Docker container: trader ────────────────┐
  Home LAN only       │                                                           │
  → Nginx Proxy Mgr   │  supervisord                                              │
  (access list) ─────►│   ├─ api        uvicorn  FastAPI  (REST + serves React)   │
                      │   ├─ worker     python -m trader.worker  (market-hours     │
                      │   │             loop: fill simulator, Telegram bot,        │
                      │   │             order monitor)                             │
                      │   └─ cron       supercronic  (scheduled jobs → CLI)        │
                      │                                                           │
                      │  trader/  (Python package: engine, strategies, adapters)  │
                      └───────┬───────────────┬──────────────┬──────────────┬─────┘
                              │               │              │              │
                        PostgreSQL      Questrade API    FinViz (HTTP)   Claude API
                        (shared with     (read-only,      scrape          Telegram API
                        FinanceTracker)  own app)
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
| Auth | Argon2 password hash; signed HTTP-only session cookie; optional TOTP. Network access limited to the home LAN by an NPM access list |
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
    ├── docker-compose.prod.yml   trader service; networks: internal + external 'proxy'
    └── deploy.sh             build amd64 on the Mac → ship → recreate (FinanceTracker pattern)
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
- **Trader must use its own Questrade API personal app and token chain, not FinanceTracker's.** FinanceTracker (`backend/app/core/questrade.py`) already keeps a rotating refresh-token chain alive. Because every exchange invalidates the previous refresh token, two apps sharing one chain would break each other's connection. ⚠ VERIFY in S1 that a second personal app has its own independent token chain.
- `GET https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token=<token>` returns an `access_token`, a new `refresh_token`, `api_server` and `expires_in`.
- **The refresh token can only be used once.** The new one must be saved (encrypted in `api_credentials`) in the same transaction, before the access token is used.
- One `QuestradeAuth` service owns the token. It **reuses FinanceTracker's proven refresh logic** (`core/questrade.py`), ported to this codebase:
  - `SELECT … FOR UPDATE` on the credentials row, with `populate_existing()` so a waiting process sees the token the first one just rotated.
  - Re-check freshness under the lock, and skip the exchange if another process already refreshed.
  - Refresh 120 s before expiry.
  - A forced refresh after an HTTP 401 has a 90 s cooldown. A failed refresh has a 60 s cooldown.
  - The rotated token is committed before it's used; if that commit fails, log it as critical.
  - This matters more in Trader than in FinanceTracker, because three processes (api, worker, cron) share the token.
- A daily cron job refreshes the token even on weekends, so it doesn't expire (⚠ VERIFY: expiry after about 7 days without use).
- Initial setup: paste the refresh token from the Questrade API Centre into Settings → Questrade.

**Endpoints used.** All ⚠ VERIFY exact paths and parameters in Phase 0.

| Purpose | Endpoint |
|---|---|
| Look up a symbol's ID | `GET /v1/symbols/search?prefix=` and `GET /v1/symbols?names=` |
| Quotes (bid/ask/last/volume) | `GET /v1/markets/quotes?ids=` (batch) |
| Candles | `GET /v1/markets/candles/{id}?startTime=&endTime=&interval=OneMinute|FiveMinutes|OneDay` |

**Rate limits.** Questrade returns HTTP 429 with rate-limit headers when a limit is exceeded. The client uses a token-bucket limiter set to the documented limits, plus exponential backoff on 429 and 5xx responses. Questrade's rate-limiting page (checked 2026-09-26) gives:

| Category | Calls | Per second | Per hour |
|---|---|---|---|
| Account | time, accounts, positions, balances, executions, orders | 30 | 30,000 |
| Market data | markets, quotes, candles, symbols, options | 20 | 15,000 |

The docs don't say whether these limits apply per app or per login. ⚠ VERIFY in S1/S4 using the `X-RateLimit-Remaining` header: if they're per login, FinanceTracker's calls count against Trader's budget.

**⚠ VERIFY in Phase 0:**
1. Whether quotes are real-time or delayed for US and TSX stocks on Stephen's account. This decides whether quote-based fills are valid. *S2 (weekend): all quotes report `delay: 0`; confirm timestamps during market hours.*
2. ~~The maximum number of candles per request~~: **20,000** ✅ (S3).
3. ~~How far back intraday history goes~~: **about 3 months** (back to 2026-06-26 when tested); daily candles go back 10 years ✅ (S3). Intraday candles include extended hours (04:00–20:00 ET), so strategies must filter to regular hours.
4. Whether a quote includes the pre-market last price. *Quotes have both `lastTradePrice` and `lastTradePriceTrHrs` (regular hours only); check `lastTradePrice` before 09:30 on a weekday.*

### 4.2 FinViz scraper

- **Universe (nightly):** the screener URL is built from settings, for example `https://finviz.com/screener.ashx?v=111&f=ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa`. **`ind_stocksonly` excludes ETFs and other funds** (decided 2026-09-26: 542 stocks instead of 695 when tested). SPY is fetched separately for the overlay. It pages through the results 20 rows at a time and parses the ticker, price, volume and sector. FinViz ignores unknown filter codes and returns the unfiltered list, so a result count equal to the unfiltered count is treated as an error. Ticker share classes are mapped to Questrade's form (`BF-B` → `BF.B`).
- **Pre-market (08:00 ET):** candidate tickers come from (a) FinViz news and earnings screens and (b) a Questrade quote check on the universe for pre-market change ≥ 3% (⚠ VERIFY pre-market data). The news screen is "news today" (`news_date_today`). The earnings window is **"reported after yesterday's close OR before today's open"** (Stephen's decision, 2026-09-27): FinViz can't OR two values of one filter in a single screen (it silently ignores `earningsdate_yesterdayafter|todaybefore`, verified live 2026-09-27), so the job runs two screens, `earningsdate_yesterdayafter` and `earningsdate_todaybefore`, and unions them. Each of `premarket.news_filter` and `premarket.earnings_filter` holds one or more `|`-separated filter lists, one screen per list; a failed screen is reported on its own and the others still count. A name from the "after yesterday's close" screen gets the previous session as its earnings date; any other earnings match gets today. Headlines come from each ticker's FinViz quote page. The screens are fetched fresh on every run (never from the page cache); quote pages are cached per ET day. A screen whose count equals the universe filters' own count means FinViz ignored the extra filter, and counts as a failed screen.
- **Empty screens (P2-T14 fix round, 2026-09-27):** a screen that matches nothing is a valid, empty result only when FinViz's page is verifiably empty: not blocked, its result count reads exactly 0 ("0 Total", or "#1 / 0 Total"), it has no rows, and it has either no results table (FinViz's real zero-match page has none) or one whose header has a Ticker column. A missing count, a count above 0 without a table or rows, and a table without Ticker are still errors. The nightly universe never accepts an empty result.
- **Politeness:** at most 1 request per 2 seconds (`finviz.min_interval_seconds`, never below 2), measured from the end of the previous request, failed or not; a browser User-Agent; results cached for 12 hours (`finviz.cache_hours`); and backoff when blocked. A screener request that gets HTTP 429 or 503 is retried after 30 s, then 90 s, then fails as blocked. HTTP 403 is never retried. News (quote-page) requests aren't retried: the caller skips that ticker. Nothing that fails validation is cached.
- **Failures:** HTTP 403/429/503, an empty body or a bot-check page count as *blocked*; other non-2xx or transport errors, a layout change, a result count that doesn't match the rows, an empty universe, and ignored filters all raise an error. The scraper never returns an empty or partial universe.
- **Isolation:** all HTML parsing lives in `finviz/parser.py`, with tests against saved HTML samples.
- **Fallback:** when FinViz fails, the nightly job logs an error event and reuses the most recent stored universe (its snapshot rows get `source = fallback`). The job detail reports the date that universe originally came from FinViz (`fallback_from`, following a fallback of a fallback back to the FinViz date) and its age in sessions. A fallback older than `universe.fallback_stale_after_sessions` (default 3) is still used, but an extra error event ("fallback universe too old") is logged and `fallback_stale: true` is set; the strategy decides whether to trade on it. With no stored universe at all, the job fails.
- **Degenerate results fail the job:** the nightly job writes nothing for the session (and keeps an earlier good run of it) and is marked failed, with an error event giving the counts, when the resolved universe is empty (not counting `universe.extra_symbols`), more than 5% of the wanted tickers don't resolve to a Questrade symbol, or more than 5% of the candle requests fail.
- **Manual fallback:** the web app can upload a CSV watchlist.

### 4.3 Claude API: catalyst classification

- **Input:** ticker, company name, up to 10 headlines with timestamps, the gap %, and the earnings date (from the pre-market earnings screens, §4.2: the previous session for a report after yesterday's close, today for one before today's open; none when the name wasn't on an earnings screen).
- **Output** (JSON-schema structured output):

```json
{ "catalyst_type": "earnings_beat|earnings_miss|guidance|analyst_action|m_and_a|regulatory|contract|offering|rumour|none",
  "direction": "bullish|bearish|neutral",
  "quality": 0-100,
  "is_confirmed": true,
  "reason": "≤ 30 words" }
```

- **Budget:** a daily cap on calls and tokens (`claude.daily_budget_usd`). If it's exceeded, classification is skipped with `catalyst_type=unknown` and the system alerts.
- **Where it's used:** the pre-market scan, and at 9:35 for any top-20 names not already classified.
- **Pre-market cap:** the pre-market scan classifies only the top `claude.premarket_max_candidates` candidates (default 50), ranked by absolute gap %. Candidates beyond the cap get `catalyst_type=unknown` and are listed in the Telegram brief as "not classified (over cap)". This keeps the biggest movers inside the daily budget on heavy news days.
- **Weekly report:** Claude writes 150–300 words of commentary from the metrics, following strict instructions not to invent numbers.

### 4.4 Telegram bot

- Long polling runs inside `worker`. **Only messages from the configured `telegram.chat_id` are accepted.**
- **Messages:** new proposal (with **✅ Approve / ❌ Reject** buttons), fill, stop hit, overlay decision, flatten, kill-switch trip, job failure, token failure, daily summary (with a **Rules followed? Yes / No** button), weekly report link.
- A button press calls the same `ProposalService.decide()` that the web app calls. Both routes do exactly the same thing. The first decision wins, and any later decision gets an "already decided" reply.
- **Messages are self-contained.** Each includes the key details (ticker, quantity, prices, stop, P&L, reason), because the web app is only reachable at home. A web link is added for use at home.
- **Commands** (for use away from home; all read-only except `/pause` and `/resume`):

| Command | Returns / does |
|---|---|
| `/status` | Session phase, next scheduled event, approval mode, kill-switch state, Questrade token health |
| `/positions` | Open positions: ticker, qty, entry, last price, stop, unrealized P&L, unprotected time |
| `/pnl` | Today's realized and unrealized P&L; week-to-date; equity and drawdown from the peak |
| `/pending` | Pending proposals, re-sent with their Approve/Reject buttons |
| `/pause` | **Blocks new entry proposals** (a manual kill switch). Exits, stops and flattening still work. Asks for confirmation. Audit-logged |
| `/resume` | Lifts a `/pause` only. It **does not** reset a tripped automatic kill switch; that is done only in the web app, on purpose |
| `/help` | Lists the commands |

- Commands from any chat other than `telegram.chat_id` are ignored and logged.

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
| `stale_universe` | `skip` | `skip` \| `trade`: what to do when the nightly job flagged the fallback universe as stale (§4.2, `fallback_stale`). `skip` emits no entries that session and logs an error note (orchestrator ruling in P1-T9) |

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

A fourth switch, **`manual_pause`**, is set by Telegram `/pause` or the web app. It blocks entries until `/resume` or the web app clears it.

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
- **Settled cash** = every debit (buys and fees) counted immediately, plus every credit (deposits and sale proceeds) from its `settle_date` (`settle_date ≤ today`). This is the conservative rule: money spent is gone at once, while sale proceeds can't be spent again until they settle, so settled cash is never spent twice. **Buying power** = settled cash when `cash_account_mode`, otherwise total cash.
- The simulated broker checks buying power again when an entry fills: if `price × qty + fees` exceeds it, the entry is cancelled ("insufficient buying power"), never filled.
- `cash_ledger` is append-only: the database refuses UPDATE, DELETE and TRUNCATE.
- Account currency is USD by default. A one-time CAD→USD conversion at start uses a configurable FX rate and fee (default 1.5%).

### 7.4 Candle-based fill model (replay)
It's used because past quotes aren't available. It uses 1-minute candles in time order. A buy stop triggers when `bar.high ≥ stop` and fills at `max(stop, bar.open) + slip + half_spread_estimate`. A sell stop triggers when `bar.low ≤ stop` and fills at `min(stop, bar.open) − slip − half_spread_estimate`. If one bar touches both the entry and the stop, assume the **worst case** (stopped out). `half_spread_estimate` defaults to 5 bps.

## 8. Replay mode

- **Input:** date range, strategy settings snapshot, fill-model settings, starting capital. Started from the web app or with `trader replay --from --to`.
- **Execution:** creates a `runs` row with `mode=replay`, then steps a `ReplayClock` through each session, running the same strategies, risk manager and ledger. Approvals are **always automatic** in replay.
- **Data:**
  - It uses archived candles first and fetches from Questrade what's missing. Questrade keeps only about 3 months of intraday candles (S3), so older days depend on the **candle archive** (below).
  - **Candle archive** (decided 2026-09-26): each session, Trader keeps (a) the 9:30–9:35 five-minute bar for every universe symbol, which the 9:35 scan already fetches, and (b) 1-minute regular-hours candles for that day's top 20 candidates plus SPY, saved by the post-close job. About 8,500 rows a day. This lets replay rank the universe and simulate fills for any day since launch. A new strategy that would pick different stocks can only be replayed over Questrade's rolling ~3 months.
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
| 12:32 / 15:32 Mon–Fri | 10:32 / 13:32 | Backup of the overlay decision (close − 30 min: 12:30 on early-close days, 15:30 otherwise); fires only due, unsettled events, so on a normal day 12:32 does nothing | `trader event --due` |
| 15:50 | 13:50 | Flatten (worker); cron backups at 15:55 and 15:58, and 12:55 and 12:58 for early-close days (on a normal day the 12:5x runs are too early and do nothing). The second backup retries a failed first one before the close (BR-42) | `trader event flatten` |
| 16:15 Mon–Fri | 14:15 | Post-close: end-of-day orders, journal, metrics, equity snapshot, **candle archive** (1-min RTH bars for the top 20 + SPY), daily summary | `trader postclose` |
| Sat 09:00 | 07:00 | Weekly report + Claude commentary; runs although Saturday is not a session; reports on the Monday–Friday week just ended and is keyed by that week's last session date; it does nothing when that week had no session | `trader weekly` |

The day-level jobs (nightly, premarket, preopen, postclose, weekly) retry a failed run in-process up to `jobs.retry_attempts` times in all, waiting `jobs.retry_delay_seconds` and then twice that; an intermediate failure is a `warning` event, and only the final failure is an `error` event (one Telegram alert per job and session). Events keep their own retries (30/60/120 s). No crontab lines are added for retries. A retry never waits past its job's deadline: 09:18 ET for the pre-market scan and 09:28 ET for the pre-open check, so neither runs into the next job or the open; the failure that stops the retries is then the final one.

Each job records a `job_runs` row with status, start and end time, and any error. Jobs can safely be re-run: they're keyed by `(job, session_date)`. A run that already succeeded is skipped unless forced, and a PostgreSQL advisory lock on `(job, session_date)` makes a second start while one is running a skip (`already running`).

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
| `candle_archive` | symbol_id, interval (`1m`/`5m`), start_ts, open, high, low, close, volume, vwap; PK (symbol_id, interval, start_ts) | Candles kept beyond Questrade's ~3-month limit, for replay (§8) |
| `daily_candles` | symbol_id, date, o,h,l,c, volume, vwap | Cache |
| `intraday_candles` | symbol_id, ts, interval, o,h,l,c, volume, vwap | Cache (5m, 1m); partitioned by month |
| `open_bar_stats` | symbol_id, session_date, avg_open_vol_14d, atr14 | Precomputed each night |
| `catalysts` | id, symbol_id, session_date, headlines jsonb, type, direction, quality, confirmed, reason, model, cost_usd | Claude output |
| `candidates` | id, run_id, session_date, strategy_key, symbol_id, rvol, rank, candle jsonb, passed bool, reject_reason | Every ranked name, including rejected ones |
| `signals` | id, run_id, strategy_config_id, symbol_id, ts, intent jsonb, evidence jsonb | What the strategy wanted |
| `proposals` | id, run_id, signal_id, kind (entry/stop/exit/cancel), order_spec jsonb, qty, status, created_at, expires_at, decided_at, decided_via (telegram/web/auto), decision_latency_ms | Approval workflow |
| `orders` | id, run_id, proposal_id, symbol_id, side, order_type, qty, stop_price, limit_price, tif, status, submitted_at, closed_at | Simulated orders |
| `fills` | id, order_id, ts, qty, price, fees jsonb, quote_snapshot jsonb, slippage | Executions |
| `positions` | id, run_id, symbol_id, strategy_config_id, qty, avg_price, opened_at, closed_at, stop_order_id, unprotected_seconds | Position life cycle |
| `trades` | id, run_id, position_id, entry_price, exit_price, qty, pnl, pnl_r, planned_risk, exit_reason, slippage_total | Round trips (for metrics) |
| `cash_ledger` | id, run_id, ts, trade_date, settle_date, currency, amount, kind, ref | Cash and T+1 settlement |
| `equity_snapshots` | run_id, ts, equity, cash, settled_cash, peak_equity, drawdown_pct | Equity curve |
| `journal` | run_id, session_date, rules_followed bool, notes, answered_via | Daily adherence |
| `kill_switch_events` | id, run_id, switch, tripped_at, value, reset_at, reset_reason | Kill-switch history |
| `job_runs` | id, job, session_date, started_at, finished_at, status, error, detail jsonb | Operations |
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
| `DATABASE_URL` | `postgresql+psycopg://trader_app:…@192.168.68.86:5432/trader` (non-owner app role) |
| `MIGRATION_DATABASE_URL` | Owner role `trader_owner`; used only by `alembic upgrade` at start-up |
| `APP_ENCRYPTION_KEY` | Fernet key for tokens and TOTP secrets |
| `SESSION_SECRET` | Cookie signing |
| `ANTHROPIC_API_KEY` | Claude |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Bot and the one authorized chat |
| `QUESTRADE_REFRESH_TOKEN` | Optional; read only by `trader questrade-seed` to start the token chain in `api_credentials` (never used after seeding) |
| `PUBLIC_BASE_URL` | `https://trader.sunspinner.ca` (resolves on the home LAN only), used in Telegram links |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD_INITIAL` | First-run user creation only |
| `TZ_DISPLAY` | `America/Edmonton` |

**Runtime settings** (the `settings` table, editable in the UI) include `approval_mode` (`manual`|`auto`), `starting_cash`, `account_currency`, `markets_enabled`, `cash_account_mode`, `risk_pct`, `quote_poll_seconds`, `slippage_*`, `stale_quote_seconds`, `proposal_ttl_*`, `auto_flatten_on_expiry`, `killswitch.*`, `claude.model`, `claude.daily_budget_usd`, and `claude.premarket_max_candidates` (default 50).

Data-layer settings (Phase 1):

| Key | Default | Allowed | Purpose |
|---|---|---|---|
| `universe.finviz_filters` | `ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa` | comma-separated tokens of `[a-z0-9_.]` | FinViz screener filters for the nightly universe (§4.2) |
| `universe.extra_symbols` | `["SPY"]` | tickers, no duplicates, must include `SPY` | Always added to the universe (SPY for the overlay) |
| `universe.fallback_stale_after_sessions` | `3` | 1–10 | A fallback universe older than this is flagged stale (§4.2) |
| `finviz.min_interval_seconds` | `2.0` | 2–60 | Spacing between FinViz requests |
| `finviz.cache_hours` | `12.0` | 0–168 | FinViz page cache lifetime |
| `open_bar.lookback_sessions` | `14` | 5–30 | Sessions averaged for the opening-bar volume (`avg_open_vol_14d`) |

Engine settings (Phase 2; `trader/settings_store.py` is the source of truth):

| Key | Default | Allowed | Purpose |
|---|---|---|---|
| `starting_cash` | `720` | > 0, ≤ 10,000,000 | Sim account's starting cash, in `starting_cash_currency` |
| `starting_cash_currency`, `account_currency` | `USD`, `USD` | `USD` \| `CAD` | CAD starting cash is converted once at `fx.cad_usd_rate` less `fx.fee_pct` (§7.3) |
| `fx.cad_usd_rate` | `0.72` | > 0, ≤ 2 | CAD → USD conversion rate |
| `fx.fee_pct` | `0.015` | 0–0.10 | Questrade's FX fee |
| `cash_account_mode` | `true` | bool | Buying power is settled cash (T+1) when on, total cash when off |
| `risk_pct` | `0.02` | > 0, ≤ 0.10 | Equity risked per trade (§6.1) |
| `slippage_buffer` | `0.005` | 0–0.05 | Cash-sizing headroom: shares ≤ buying power / (entry × (1 + buffer)) |
| `no_entry_before_close_minutes` | `30` | 0–390 | No entry from this long before the close (BR-42); the broker cancels later entries |
| `quote_poll_seconds` | `2.0` | 1–60 | Quote polling interval for working orders |
| `stale_quote_seconds` | `10.0` | 1–300 | A quote older than this never fills (§7.2) |
| `slippage_min`, `slippage_bps` | `0.01`, `5` | 0–1, 0–100 | Slippage = max(min, bps × price) |
| `fees.commission`, `fees.direct_route`, `fees.ecn_per_share`, `fees.sec_rate` | `0`, `false`, `0.0035`, `0.0000206` | see code | Commission, ECN fee when direct-routed, SEC fee on sells |
| `proposal_ttl_entry_seconds`, `proposal_ttl_stop_seconds`, `proposal_ttl_exit_seconds` | `300`, `180`, `300` | 30–3600 | Proposal TTLs by kind; cancels use the exit TTL (§6.2) |
| `stop_escalation_seconds` | `60` | 10–3600 | Unprotected-position escalation after a protective stop expires |
| `auto_flatten_on_expiry` | `true` | bool | An expired exit or cancel proposal executes automatically |
| `killswitch.daily_loss_pct`, `killswitch.max_drawdown_pct` | `0.05`, `0.15` | > 0, ≤ 1 | Kill-switch thresholds (§6.3) |
| `killswitch.expectancy_min_trades`, `killswitch.expectancy_threshold_r` | `50`, `0` | 1–10000, −10–10 | Expectancy kill switch |
| `claude.model` | `claude-sonnet-5` | `claude-sonnet-5` \| `claude-haiku-4-5` | Catalyst classifier model |
| `claude.daily_budget_usd` | `1.00` | 0–100 | Daily Claude spend cap (§4.3) |
| `claude.premarket_max_candidates` | `50` | 0–500 | Pre-market classification cap (§4.3) |
| `premarket.gap_min_pct` | `0.03` | > 0, ≤ 1 | Pre-market gap that makes a candidate |
| `premarket.news_filter` | `news_date_today` | one or more `\|`-separated filter lists | FinViz news screen(s) (§4.2) |
| `premarket.earnings_filter` | `earningsdate_yesterdayafter\|earningsdate_todaybefore` | one or more `\|`-separated filter lists | FinViz earnings screens, unioned (§4.2) |

Every stored value is validated; `load()` fails closed on an invalid row, and every change is written to `audit_log`.

## 14. Security

- **Network:** Trader joins the external `proxy` Docker network. **Nginx Proxy Manager** (NPM) terminates TLS for `trader.sunspinner.ca` and forwards to `trader:8000`. **No host port is published**, unlike FinanceTracker's `8001:8000`, so NPM is the only way in.
- **Home network only.** The NPM proxy host has an **Access List** that allows `192.168.68.0/22` (the home LAN, `192.168.68.0`–`192.168.71.255`) and denies everything else. The app login is a second layer.
- **Local name resolution:** a Pi-hole v6 Local DNS record (Settings → Local DNS → DNS Records) points `trader.sunspinner.ca` to `192.168.68.73`, so the name works at home with no public DNS record needed.
- **TLS certificate:** because the host isn't reachable from the internet, Let's Encrypt's HTTP challenge may fail. If it does, use NPM's **DNS challenge** with your DNS provider's API token.
- **The only internet-facing part is the Telegram bot.** It makes outbound calls only (long polling), so no port is opened. Remote use goes through the Telegram commands in §4.4.
- **Future remote access,** if ever needed: a VPN (WireGuard or Tailscale), with no app changes.
- **Cookies:** HttpOnly, Secure, SameSite=Strict; CSRF token on changes; login rate limiting and lockout.
- **Secrets:** tokens and secrets are encrypted at rest with Fernet, and nothing sensitive is logged.
- **Telegram:** chat-ID allow-list; the callback data contains a signed proposal ID plus a nonce.
- **Container:** runs as a non-root user; read-only root filesystem except `/tmp`; minimal base image; pinned dependencies.
- **Audit:** every approval, setting change, kill-switch reset and credential change is written to `audit_log`.

## 15. Deployment

Trader follows the same pattern as FinanceTracker's `scripts/deploy.sh` and `docker-compose.prod.yml`.

| Item | Value |
|---|---|
| Docker host | `192.168.68.73`, Docker context `shared-docker-server` (same host as FinanceTracker) |
| Build | On the Mac, `desktop-linux` context, `DOCKER_DEFAULT_PLATFORM=linux/amd64` |
| Ship | `docker save trader:latest \| ssh stephen@192.168.68.73 docker load` |
| Recreate | `docker --context shared-docker-server compose -f docker/docker-compose.prod.yml up -d --no-build --no-deps --force-recreate trader` |
| Health wait | Poll `https://trader.sunspinner.ca/api/health` from the Mac on the home LAN until it returns 200 (up to 40 s) |
| Networks | `trader_internal` (bridge) + `proxy` (external, shared with NPM) |
| Ports | **None published.** Access is through NPM only |
| Volumes | Named volume `trader_logs:/app/logs`, not a bind mount (the compose file runs from the Mac against a remote daemon; see the FinanceTracker compose comments) |
| Env file | `docker/.env.prod`, git-ignored and kept on the Mac next to FinanceTracker's |
| Container `TZ` | `UTC`. Scheduling uses `CRON_TZ=America/New_York` and the UI shows `America/Edmonton`. Unlike FinanceTracker, no logic depends on the container's local date, because all session dates come from the exchange calendar |

- **Image:** multi-stage Dockerfile. `node:22-alpine` builds `web/` → `python:3.12-slim` runtime with the app, the static build, supercronic and supervisord.
- **PostgreSQL:** the **same instance as FinanceTracker** (`192.168.68.86:5432`), in a new database `trader`. Following the `ledger_prod` pattern, `trader_owner` owns the schema and runs the migrations, and the app connects as the non-owner `trader_app` role with explicit grants (plus `ALTER DEFAULT PRIVILEGES`, so new tables are covered too).
- **Start-up:** `alembic upgrade head` (as the owner) → create the admin user if missing → supervisord.
- **Health check:** `GET /api/health`.
- **NPM:** add a proxy host `trader.sunspinner.ca` → `http://trader:8000`, with websockets/SSE allowed, a Let's Encrypt certificate, and the home-LAN-only **Access List**.
- **Backups:** covered by the existing Postgres backup process. ⚠ VERIFY that it includes the new `trader` databases. `pg_dump` output contains encrypted tokens, so keep it outside the repo, as FinanceTracker's `.gitignore` notes.

### 15.1 Environments: dev first, then prod

Both environments run on the **same Docker host** (`192.168.68.73`) and the **same PostgreSQL server** (`192.168.68.86:5432`), fully separated. The names in §15 above are the prod values.

| Item | **dev** (build and test here first) | **prod** (after promotion) |
|---|---|---|
| Compose project / container | `trader-dev` / `trader-dev` | `trader` / `trader` |
| Env file (git-ignored, on the Mac) | `docker/.env.dev` | `docker/.env.prod` |
| Database | `trader_dev` | `trader` |
| DB roles | `trader_dev_owner`, `trader_dev_app` | `trader_owner`, `trader_app` |
| Hostname (NPM + Pi-hole, LAN-only access list) | `trader-dev.sunspinner.ca` | `trader.sunspinner.ca` |
| Log volume | `trader_dev_logs` | `trader_logs` |
| Image tag | `trader:dev` | `trader:<version>` (promoted from a tested dev tag) |
| **Telegram bot** | **Its own bot** (e.g. `@StephenTraderDevBot`) | Its own bot |
| **Questrade API personal app** | **Its own app/token chain** ("Trader-dev") | Its own app ("Trader") |
| `APP_ENCRYPTION_KEY`, `SESSION_SECRET` | Unique | Unique |
| **Anthropic key** | **Its own key** ("trader-dev"), not FinanceTracker's | Its own key ("trader") |
| Deploy | `bash docker/deploy.sh dev` | `bash docker/deploy.sh prod` |

**Why the bots and Questrade apps must be separate:**
- **Telegram:** only one process can long-poll a given bot token at a time. A second poller gets HTTP 409 Conflict, so dev and prod would fight over one bot.
- **Questrade:** a token refresh invalidates the previous token (§4.1), so two environments sharing one chain would break each other, exactly as Trader and FinanceTracker would.

**Running both at once:** allowed, since the resources are fully separate. Keep the same approval mode in both, or pause dev (`/pause`) once prod is live, so you don't get duplicate approval requests on your phone.

**Promotion criteria (dev → prod):**
1. The Phase 0 spikes (§17) have passed in dev.
2. There have been **10 consecutive trading days** in dev with no failed scheduled jobs and no missed 9:35 events.
3. The full test suite and a replay-determinism test pass on the image tag being promoted.
4. A manual check of one complete simulated day in the web app: a signal, the approval, the fill, the stop, the flatten, and the journal entry.

**Promotion steps:**
1. Tag the image.
2. Create the `trader` database and roles.
3. Create `.env.prod`.
4. Run `deploy.sh prod`.
5. Add the NPM proxy host and the Pi-hole record.
6. Start with a **fresh** `live` run. Dev trades aren't carried over; dev results stay in `trader_dev` for comparison.

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

Results, findings and scripts: [`../spikes/README.md`](../spikes/README.md).

| # | Spike | Pass criteria |
|---|---|---|
| S1 | Questrade personal-app token refresh and rotation | Refresh works 3 times in a row, and the stored token stays valid. **Pass** 2026-09-26 |
| S2 | Quote freshness | The quote timestamp during market hours is within 2 s of the current time for US names (records whether quotes are delayed). **Partial:** `delay: 0`; rerun in market hours |
| S3 | Candle limits and history depth | The maximum candles per request, and the earliest 1-min/5-min history available. **Pass:** 20,000; ~3 months |
| S4 | Universe scan timing | Fetching 9:30–9:35 bars for the universe at 9:35 finishes in under 60 s within the rate limits. **Pass:** 694 symbols in 35 s (recheck live) |
| S5 | FinViz scrape | Universe and news pages parse correctly; note any blocking. **Pass:** 695 tickers; browser User-Agent required |
| S6 | Telegram inline approval round-trip | Button → callback → DB update in under 2 s. **Pass:** ≈ 1.3 s |

## 18. Open items for Stephen

Settled from the FinanceTracker repo: Docker host `192.168.68.73` (context `shared-docker-server`), Nginx Proxy Manager on the `proxy` network, the `sunspinner.ca` domain, and the build-and-ship deploy method. **Still open:**

1. ~~PostgreSQL host/port and version~~: `192.168.68.86:5432`, PostgreSQL 14.24 (Ubuntu 22.04) ✅. The design needs PostgreSQL 13 or later.
2. ~~Home LAN subnet~~: `192.168.68.0/22` (gateway `192.168.68.1`), read from the routing table on 2026-09-26 ✅. The earlier `/24` assumption would have blocked any device given a `192.168.69–71.x` address.
3. ~~Hostnames~~: `trader-dev.sunspinner.ca` (dev) and `trader.sunspinner.ca` (prod), resolved locally through Pi-hole ✅.
4. ~~The Claude daily budget cap~~: US$1/day for dev ✅ (estimated normal use US$0.20–0.60 per trading day on Sonnet 5). Trader gets **its own Anthropic key**, separate from FinanceTracker's, so costs are tracked separately ✅.
5. You'll need to register a **second Questrade API personal app** for Trader (see §4.1). One login can have several apps, each with its own consumer key ✅. S1 still verifies that their token chains are independent.
6. ~~Pre-market candidate cap~~: classify only the top 50 by gap %, adjustable in Settings (§4.3) ✅. Check the default against real candidate counts during Phase 0.
