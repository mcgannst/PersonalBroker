# Live Dashboard and Control page: implementation plan (specification)

> **For agentic workers:** this is a specification plan (master plan §6.6): it says WHAT to build and HOW TO KNOW it works; builders write the code. Read first: the approved design [`2026-09-28-live-dashboard-design.md`](2026-09-28-live-dashboard-design.md) (decisions D1–D10 are fixed; this plan argues from them and changes none), the master plan [`2026-09-26-build-master-plan.md`](2026-09-26-build-master-plan.md) (Global Constraints, §3 state file, §6 preamble, §7.1 contracts), and the Phase 6 plan's D2 rule ([`2026-09-27-phase-6-promotion.md`](2026-09-27-phase-6-promotion.md), "D2. Redeploys during the soak"). Steps use checkbox (`- [ ]`) syntax.

**Task IDs (for the board):** DB-T1, DB-T2, DB-T3, DB-T4, DB-T5, DB-T6, DB-T7, DB-T8, DB-T9, DB-T10, DB-T11, DB-T12.

**Goal:** the web app's main page becomes a read-only live monitor (`/dashboard`, fed by `GET /api/live`), and everything that changes the engine moves to a new Control page (`/control`, fed by `GET /api/control`), which absorbs the System page. Prices come only from quotes the worker already fetched, written to the database by a small mark publisher in the worker. The deploy is not a trading change under Phase 6 D2, and the plan proves it.

**Written against:** trunk `cb50e99` (trader-dev runs `459e172`, `/api/meta` `phase-5-complete-24-g459e172`, alembic head `0007`). The next migration is **0008** (re-check `ls Trader/app/trader/db/migrations/versions/` at build time; if another 0008 landed first, take the next free number and say so in the task notes).

---

## Global Constraints

Every task's requirements implicitly include the master plan's Global Constraints (branching on `trunk`, Python 3.12 with `uv`, `numeric(14,4)` money as `Decimal`, `timestamptz` UTC, the `Clock` rule, secrets, no network in unit tests, testcontainers for integration tests, commit format, explicit `git add` paths) and these:

- **Shared test lane:** while working run only targeted tests (`uv --directory <worktree>/Trader/app run pytest <files> -q`, `npm --prefix <worktree>/Trader/web exec vitest run <files>`, `ruff`/`mypy` on your files). The full gate is **only** `bash Trader/build/gate.sh` from your worktree root, once, just before committing (again only if a rebase brought in code touching yours). Never run `check.sh` directly.
- **Soak safety (D10):** no task modifies any file on the live decision path: `Trader/app/trader/strategies/`, `trader/engine/`, `trader/broker/`, `trader/market/`, `trader/jobs/nightly.py`, `trader/jobs/premarket.py`, `trader/adapters/claude/catalyst.py`, `trader/settings_store.py`. No new settings key is added (a new key would touch `settings_store.py`); thresholds and cadences in this plan are module constants. No existing assertion in `tests/engine/`, `tests/strategies/`, `tests/broker/`, `tests/market/`, `tests/jobs/` or `tests/integration/` may change (adding new test files there is allowed). No `docker/crontab` line changes. `Trader/app/tests/replay/golden/` is never touched. A builder who finds one of these unavoidable stops and reports (master plan §6.1), never works around it.
- **Import direction:** no decision-path module imports `trader.marks` or `trader.api.livedata`. `trader.marks` imports from decision-path packages only read-only types, protocols and constants (`QtQuote`, `CandleRequest`, `QuoteClient`, `Candle`, `Clock`, `et_date`, `ET`, `SessionCalendar`); `trader.api.livedata` may call read-only functions of them (`KillSwitches.active`/`inputs`/`blocking`, `Ledger.balances`, `SessionCalendar` methods, `StrategyRegistry.keys`/`current`/`plugin_class`) and never a write (`evaluate`, `pause`, `reset`, `submit`, `snapshot_equity`, `record`).
- **The dashboard never calls Questrade (D8):** `GET /api/live` and `GET /api/control` and everything under `trader/api/livedata/` never use `ApiServices.quotes`, `ApiServices.candles`, `trader.api.routers.trading.open_positions`, `trader.notify.views.last_prices`/`position_lines`, `OffLoopMarketData` or any `MarketDataService`. Prices come from `quote_marks`/`mark_bars` (and stored candles) only.
- **The mark publisher never feeds a decision:** nothing in the engine, broker, strategies, risk or proposals reads `quote_marks` or `mark_bars`; the tap returns exactly the objects the wrapped client returned. **Bars built from quotes are never candles:** `mark_bars`/`MarkBar`/`quote_marks`/`QuoteMark` are referenced only by `trader/db/models.py`, the 0008 migration, `trader/marks/publisher.py`, `trader/api/feed.py` and `trader/api/livedata/{positions,equity}.py` (an allow-list test, DB-T10 test 5); no module converts a mark bar into a `trader.market.types.Candle` or writes it to `intraday_candles`/`candle_archive`, so strategies, metrics, reports, replay, the decision log and the post-close archive can never read one.
- **The quote tap is synchronous bookkeeping only (soak safety, S1a):** no `await` of its own, no task, lock, timeout, sleep, thread or I/O (database, file, network, log line above `warning`), bounded memory, identical return objects, exceptions and cancellation, and `candles_many`'s `deadline_s` passed through untouched.
- **Off the event loop:** every database step of the worker's mark publisher runs in the publisher's **own single-thread executor** (`concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="marks")` via `loop.run_in_executor`), never the default executor that the Questrade client's token fetch (`asyncio.to_thread(self._tokens.access)`) and the decisions loop use, so a stuck publisher can never hold a thread the trading path needs; the API routes do their database work in `anyio.to_thread.run_sync` (the existing router pattern). No blocking call on an event loop.
- **Clock discipline:** no `datetime.now()`, `date.today()`, `time.time()` or `time.monotonic()` call in `trader/` outside `trader/market/clock.py` (AST test `tests/gauntlet/test_p1_t4_breaker.py`). Elapsed times use `asyncio.get_running_loop().time()` (as `MarketDataService._fetch_opening_bars` does) or `time.perf_counter()` (as `meta.db_check` does).
- **Replay isolation:** every live aggregate filters run-scoped rows to the active live run id (`deps.live_run_id`), and `event_log` rows with `trader.api.feed.live_or_unscoped`; no replay row reaches `/api/live`, `/api/control` or the `marks`/`activity` SSE watermarks.
- **Money** in API responses is a decimal string (pydantic `Decimal`); times are UTC ISO `...Z`; the web shows times in MT (`lib/format.ts` `fmtTime`), never UTC.
- **Free text** (reasons, error messages, event messages, tickers, strategy names) is rendered as plain text in React (no `dangerouslySetInnerHTML`), and every message or error from the database passes `trader.logging_setup.redact_text` / `api.views.redacted_json` / `api.views.event_out` on the way out.
- **Web:** no new npm dependency (Recharts 2.15 is available; sparklines are inline SVG). Touch targets at least 44 px (`MIN_TOUCH_PX`). No page scrolls sideways at 390 px.
- **Deploy window:** nothing is deployed between 09:15 and 16:30 ET on a session day. Builders never deploy; DB-T12's LIVE steps do, after the close.
- **Never run** `spikes/qt.py` or `spikes/s1_tokens.py`. Never print, log or commit a secret.

## Review Focus

The five failure modes most likely to hurt, each owned by named acceptance tests.

1. **The mark tap or publisher changes trading** (an extra Questrade request, a changed or copied quote object, a swallowed exception or cancellation, an extra event-loop yield or a changed 9:35 opening-bar deadline, a blocked event loop or a starved default thread pool, an exception raised into the worker step, unbounded memory, a decision or report reading the new tables). Expected: identical trading rows, identical Telegram chat and an identical Questrade call log with the publisher on and off; the tap is transparent (same objects, same exceptions, no yield of its own, the opening-bar guard fires at the same moment); a slow database never delays the step loop. [DB-T2 tests 1–6, 11–15; DB-T10 tests 1–3 and 5]
2. **Wrong period P&L at a boundary:** week start (Monday, ET), the DST changes (2026-11-01 and 2026-03-08), a holiday or weekend "today", replay rows or another run's rows counted, costs double-counted (fees already inside `trades.pnl`). Expected: the definitions of S4/S5 below, pinned case by case. [DB-T3 tests 1–9; DB-T12 test 3]
3. **The books check lies:** a false ✗ from rounding drift after many trades, or a false ✓ when a ledger row is missing, duplicated or a trade row is missing. Expected: the S3 identity holds exactly (to 0.0001) on correct books, and each corruption flips it to ✗ with the difference shown. [DB-T3 tests 10–14]
4. **Stale data shown as fresh:** a mark older than 30 s without the badge, a stale heartbeat without the badge, SSE down without the 15 s polling fallback, the 2 s throttle dropping the last update. Expected: badges and the degraded live indicator; the throttle always delivers a trailing refetch. [DB-T4 tests 1–3; DB-T7 test 2; DB-T8 test 3; DB-T9 test 6; DB-T11 tests 3–6]
5. **The dashboard costs too much or fails as a whole:** a Questrade call from the dashboard or control routes, `/api/live` over 300 ms on a normal day, a per-row (N+1) query, or one failing part blanking the page. Expected: zero Questrade calls (fake asserts), a measured budget with a statement-count ceiling that does not grow with rows and a `Server-Timing` header checked live, and per-part isolation (`part_errors`) with per-panel error states. [DB-T5 tests 3–6; DB-T6 test 7; DB-T11 test 2; DB-T12 tests 1–2 and LIVE 5]

---

## Settled design points

These answer the planning questions; the tasks below refer to them as S1–S16.

**S1. Where marks come from, cadence and ownership.** Today the worker's engine polls quotes only for symbols with working orders (`Engine.poll_quotes` → `broker.working_symbol_ids()`, every `quote_poll_seconds` = 2 s in the session; a held position always has a working stop order, so held symbols are included) and for account valuation after a fill (`Engine._account`); the bot's `/positions` also quotes. None of these quotes is stored. All of them go through **one** object: the worker's shared `LazyQuestrade` client built in `runtime._run_worker`. Changing the engine or `MarketDataService` would be a decision-path change, so the marks are taken at the composition root instead: a **`QuoteTap`** (new, `trader/marks/tap.py`) wraps that client, passes every call through unchanged and returns the very object the client returned, and on the side remembers each returned quote with the time it was observed. A **`MarkPublisher`** (new, `trader/marks/publisher.py`) is one more supervised worker task beside the relay, heartbeat, bot and decisions loop (built like `DecisionsLoop`, P6-T11): every `PUBLISH_INTERVAL_S` = 2.0 s it drains the tap's new observations on the event loop (a dict swap, no I/O) and, only when there are any, writes them in its own single-thread executor to `quote_marks` (latest per symbol) and `mark_bars` (1-minute OHLC of observed prices), limited to symbols the live run holds or has working orders in. It never calls Questrade, never triggers a quote fetch, and nothing on the decision path reads its tables. Ownership: `trader/worker.py` gains `WorkerDeps.marks` (DB-T2 owns the file); `trader/runtime.py` builds the tap and the publisher and merges their health into the heartbeat (DB-T10 owns the file). When the worker is down (cron backups run the 9:35 scan) there are simply no new marks and the page shows "prices stale".

**S1a. Why the tap, and the rules that make it safe (verifier, DB-T0).** No safer source exists on trunk: neither `Engine`, `SimBroker` nor `MarketDataService` keeps the quotes it fetched (`MarketDataService` caches only `symbols.id → questrade_id`; `Engine.poll_quotes` hands the quotes straight to `broker.on_quotes`), so reading "the engine's in-memory quote cache" would need a decision-path change (D10). The tap is kept, and it sits in front of every worker market-data call (the 2 s quote poll, `_account` after a fill, `spy_overlay`, the 9:35 `opening_bars` batch through `MarketDataService._fetch_opening_bars`, and `candles`), so it is specified as pure synchronous bookkeeping:
- **Pass-through shape.** Each proxied method is a plain `async def` whose only `await` is a direct `await self._inner.<same method>(<same arguments>)`: no `create_task`, `ensure_future`, `gather`, `wait`, `wait_for`, `shield`, `asyncio.timeout`, `sleep`, `to_thread`/`run_in_executor`, lock (asyncio or threading), context manager or retry. So the tap adds no event-loop iteration, and a cancellation (the `asyncio.timeout` guard in `_fetch_opening_bars`, a worker stop) reaches the inner call exactly as before.
- **Same objects, same errors.** It returns the inner call's return value itself (never a copy, filter or rebuilt list/dict). An exception or `BaseException` (including `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`) from the inner call propagates as the same instance with a bare `raise`; nothing in `finally` returns or suppresses. Its own bookkeeping runs in `try: ... except Exception:` blocks that can only log (at `warning`, once per failure streak: below the log mirror's `error` level, so no database write) and never replace or chain the caller's result or exception.
- **`candles_many` untouched.** `deadline_s` is forwarded as the same keyword with the same value (None when the caller omitted it, which is the inner default); `reqs` is forwarded as the same object and is not iterated before the inner call returns. Around the call the tap reads only `loop.time()` (the loop's clock, as `_fetch_opening_bars` does) and a snapshot of `stats["market"]` via `dataclasses.asdict` (None counts as zeros: a `LazyQuestrade` opens its client inside this very call at 9:35). After it returns, it counts the result in one O(n) pass (n ≤ the request count, ~550) and keeps only the counts (`CandleBatch`), never a reference to `reqs`, the result dict or any candle.
- **`stats`** is a read-only property that returns `getattr(self._inner, "stats", None)` on every access (never cached: `LazyQuestrade.stats` builds a fresh dict each time and is None until the client opens), so `MarketDataService._market_stats` and its `client_stats` log fields see what they saw before. The tap forwards no other attribute (the heartbeat keeps reading `rate_limit_remaining()` from the `LazyQuestrade` itself).
- **Bounded memory.** Undrained observations keep a reference to each frozen, slotted `QtQuote` the client already built (no copy): at most `MAX_OBSERVATIONS_PER_SYMBOL` = 30 per Questrade id (15 publisher intervals at the 2 s poll: longer than a 30 s supervised restart), across at most `MAX_TAP_SYMBOLS` = 1000 ids (least recently observed dropped), so at most 30,000 references even if the publisher never drains; `candle_batches` keeps at most 5 count records. Recording is O(len(quotes)) with O(1) dict/deque operations.
- **No I/O and no clock but the Clock.** Observation times come from the injected `Clock.now()`; health numbers are computed on demand from counters. The tap never touches the database, a file or the network.

**S2. Tables (migration 0008).** `quote_marks` (one row per run and symbol, upserted) and `mark_bars` (1-minute bars built from observed quotes). Why a second table: 1-minute candles are stored only after the close (`jobs/postclose.py` archives `1m` bars to `candle_archive`), and equity snapshots are written only on fills and at the session end, so without it the sparklines and the intraday equity line would be empty during the session. Bars from stored candles win where both exist (`BarOut.source`).

**S3. Books check (from the real ledger model).** `cash_ledger` rows are the only cash record: one `deposit` (= `sim_accounts.starting_cash`), `buy` = −round4(price × qty), `sell` = +round4(price × qty), `fee` = −fees.total stored at `numeric(14,4)`. `trades.pnl` = round4((exit − entry) × qty − (entry fees + exit fees)), net of both fees; `positions.avg_price` is the entry fill price. The check is the exact identity:

```
actual   = cash + positions_at_cost
         = Σ cash_ledger.amount (run)  +  Σ round4(avg_price × qty) over open positions
expected = starting_cash + realized_gross − fees_paid
         = sim_accounts.starting_cash
           + Σ over the run's trades of (exit_price − entry_price) × qty          (exact at 4 dp)
           − Σ over the run's fills of round4_half_up(fees.total)                 (as the ledger stores it)
ok       = |actual − expected| < 0.005   (to the cent)
```

Computing realized gross from `trades` prices and fees from `fills` (rounded the way the ledger column rounds them) makes the identity exact on correct books after any number of trades, so there is no rounding drift. It catches a fill without its ledger rows, a duplicated ledger row, a position whose cost disagrees with its entry fill, and a closed position without its `trades` row. `realized_recorded` = Σ `trades.pnl` is returned for information (it differs from `realized_gross − closed fees` by at most 0.00005 per trade from `pnl` rounding) and is not part of ✓/✗. The top bar's wording: "cash + positions at cost = starting cash + realised − fees, to the cent ✓/✗".

**S4. Period P&L (reusing `trader.reports.metrics`).** Per period (today, week, run), from the active live run only:
- `realized` = Σ `trades.pnl` with `trades.session_date` in the period (already after both fees).
- `unrealized` = Σ over currently open positions with a mark of (mark − avg_price) × qty − that position's entry fill fees (open P&L after its entry fee); `None` when no open position has a mark, `unrealized_partial` true when some lack one. The same open-position value is used for all three periods (the strategies flatten daily, BR-42; an overnight holding would count fully in "today": accepted, open question 5).
- `pnl_after_fees` = realized + (unrealized or 0). This is the headline "trading P&L after fees".
- `fees` = Σ round4(`fills.fees.total`) of the run's fills whose `ts` falls in the period's ET bounds (fees paid in the period; informational, already inside `pnl_after_fees`).
- `claude_usd` = Claude spend in the period: `catalysts.cost_usd` with `session_date` in the period plus `weekly_reports.cost_usd` with `updated_at` in the period's ET bounds (the same sources as `trader.reports.weekly.claude_spent`, over a range).
- `net_after_ai` = pnl_after_fees − claude_usd.
- `trades`, `wins`, `losses`, `win_rate`, `expectancy_r`, `trades_without_r` from `trader.reports.metrics.metrics_from_rows(run_id, date_from, date_to, trade_rows, [], [])` over one query of the run's trades (bucketed by `session_date` in Python), so the definitions equal `/api/metrics`.
- Invariant (tested): for the run period with every open position marked, `pnl_after_fees == equity_at_marks − starting_cash` exactly, where `equity_at_marks` = Σ cash_ledger + Σ mark × qty.
- `claude_today` uses `trader.reports.weekly.claude_spent(factory, et_date(now))` against `RuntimeSettings.claude_daily_budget_usd` (the budget the catalyst classifier and the weekly report check).

**S5. Periods, week boundaries, DST and holidays.** All period logic is in America/New_York dates.
- `session_day` (the "today" the dashboard reports) = today's ET date when it is a session, else the latest session before it (Saturday, Sunday or a holiday show the last session, labelled with its date). Note this differs on purpose from `notify.views.pnl_view`, which uses the next session.
- `today` = [session_day, session_day]; `week` = [Monday of the ET week containing today's ET date, today's ET date] (on Saturday and Sunday the week is the one just ended, as in `pnl_view`); `run` = [ET date of `runs.started_at`, today's ET date].
- Date-keyed rows (`trades.session_date`, `catalysts.session_date`) compare dates. Timestamp-keyed rows (`fills.ts`, `weekly_reports.updated_at`, equity points) use the ET day bounds `[00:00 ET of date_from, 00:00 ET of date_to + 1 day)` converted with `zoneinfo` (a DST-change day is 23 or 25 hours long).
- Pinned cases: Sunday 2026-11-01 (DST ends) belongs to the week of Monday 2026-10-26; a fill at 2026-11-02T04:59:59Z (Sunday 23:59:59 EST) is in that week and one at 2026-11-02T05:00:00Z is in the week of 2026-11-02; a fill at 2026-03-09T03:59:59Z (Sunday 2026-03-08 23:59:59 EDT) is in the week of 2026-03-02. Thanksgiving 2026-11-26: `session_day` is 2026-11-25.

**S6. Equity series and downsampling.** Snapshots exist only at fills and at the session end, so the series is built from three sources, merged by time: stored `equity_snapshots` (`source="snapshot"`); for `range=today` only, one derived point per minute of `session_day` in which a position of the run was open, `equity(m) = Σ cash_ledger rows with ts ≤ end of m + Σ over positions open at the end of m of qty × close of that symbol's bar for m` (the latest earlier bar's close when m has none, else the avg price), from `mark_bars` and stored 1-minute candles (`source="marks"`); and a final `now` point = equity at the latest marks (`source="now"`, the `equity_now` value DB-T4 computes; added whenever it is given). `start_equity` is the day's start line: for `today` the latest snapshot before the session open of `session_day` (the value the daily-loss kill switch uses), else `sim_accounts.starting_cash`; for `run`, `starting_cash`. **Downsampling to ≤ 500 points:** when there are more than 500 points, split the time range into 250 equal buckets and keep, per non-empty bucket, its minimum-equity and maximum-equity points in time order (one point when they coincide); the series' first and last points are always kept (replacing their bucket's points when needed to stay ≤ 500). This keeps every drawdown extreme, is deterministic and O(n). `downsampled` says whether it happened. Fill markers are listed separately (≤ 200, newest kept) and never downsampled.

**S7. Activity feed items and sources.** Newest first, at most 100, items whose time falls in the ET day of `session_day` (so a weekend shows the last session's activity). Every item has an id `"<kind>:<row id>"`, one plain-text line, a chip, a tone and an optional link.

| kind | chip | source (live run only) | text (example) | link |
|---|---|---|---|---|
| `order_placed` | trades | `orders.submitted_at` | `Buy stop 25 AAPL @ 182.40 (entry)` | `/trades?position=<id>` when set |
| `order_cancelled` | trades | `orders.closed_at` with `status='cancelled'` | `Cancelled entry AAPL: entry cutoff` | idem |
| `fill` | trades | `fills.ts` joined to its order | `Filled buy 25 AAPL @ 182.46, slippage 0.06/share` (slippage = `fills.slippage`, per share vs the planned price) | idem |
| `exit` | trades | `trades.closed_at` | `Exit AAPL (stop): -12.50, -0.50 R` (the stored `exit_reason`, e.g. the stop, flatten, overlay, kill-switch or expiry reasons, shown as stored) | `/trades?position=<id>` |
| `proposal_created` | proposals | `proposals.created_at` | `Proposal entry AAPL 25 sh (manual)` or `(auto)` (`proposals` has no approval-mode column: `(auto)` when `decided_via = 'auto'`, else `(manual)`) | `/dashboard?proposal=<id>` |
| `proposal_approved` / `proposal_rejected` | proposals | `proposals.decided_at` with `decided_via` | `Approved entry AAPL via telegram at 09:36 MT` / `Rejected ... : entry blocked: kill switch ...` (the stored `error` when set) | idem |
| `proposal_expired` | proposals | `proposals.expired_at` | `Expired exit AAPL (auto-flatten)` when `auto_flatten_on_expiry` executed it | idem |
| `kill_switch_tripped` / `kill_switch_reset` | alerts | `kill_switch_events.tripped_at` / `reset_at` | `Kill switch daily loss tripped: 5.2% vs 5.0%` / `... reset by web:stephen: <reason>` | `/control` |
| `job_failed` | alerts | `job_runs` with `status='failed'`, `finished_at` in the day (job_runs has no run id) | `premarket failed (attempt 2): <masked error, 120 chars>` | `/control` |
| `alert` | alerts | `event_log` level `error`/`critical`, `live_or_unscoped`, source not starting with `log.` (the log mirror lives on Control) | `<source>: <masked message, 160 chars>` | `/control` |
| `scan` | scan | `candidates` of the run and `session_day` (written at scan time, so it is live; the decision log lags by up to its refresh) | `09:35 scan: 543 → 12 passed → AAPL, MSFT, NVDA` (count of rows, count with `passed`, top 3 passed tickers by `rank`, time = min `created_at`) | `/reports?day=<D>` |

Tones: `up`/`down` only for money outcomes (an exit's P&L sign), `warn` for kill-switch trips, job failures, alerts and rejected/expired proposals, else `neutral`.

**S8. Rejections panel.** Counts by rule for `session_day` from `decision_log` (the design's source): rows of the live run with `outcome='rejected'` and a `symbol_id`, grouped by `(stage, rule)` (scan and risk stages; a row without a rule counts as `unknown`); when the day's scan was recorded as a summary only (`reports.decisions_scan_detail` not `all`: the scan row without a symbol carries `data.counts.rejects_by_rule`), those counts are used for the scan stage (no tickers). Tickers per rule: up to 50, `truncated` when more. When the decision log has no scan rows for the day yet (the loop refreshes every 60 s and pauses 09:34–09:38 ET) but `candidates` has rows, the scan-stage counts come from `candidates.reject_reason` (`source="candidates"`), so the panel is never empty right after 9:35. Each rule links to `/reports?day=<D>&stage=<stage>&outcome=rejected`; each ticker to the same plus `&ticker=<T>` (DB-T11 teaches the Day view to read these parameters).

**S9. Live updates.** Two new `Topic` values: `marks` (watermark `max(quote_marks.written_at)` of live runs: changes at most every 2 s in the session) and `activity` (watermark `max(decision_log.id)` of live runs: a rebuilt day gets new ids; a skipped pass changes nothing). The web maps both to the `dashboard` query prefix. The new page queries use keys under the existing prefixes, `qk.live(...) = ["dashboard", "live", ...]` and `qk.control() = ["system", "control"]`, so every existing mutation that invalidates `["dashboard"]` or `["system"]` (approve, reject, kill switches, approval mode, run job, watchlist) refreshes the new pages with no change to those components. `LiveUpdatesProvider` throttles invalidations of the `dashboard` and `system` prefixes to at most one per `LIVE_THROTTLE_MS` = 2000 ms (leading call immediately, one trailing call at the end of the window, never dropped). While the stream is down, queries under both prefixes poll every `DISCONNECTED_REFETCH_MS` = 15 s (the existing mechanism, extended to `system`) and the top bar shows the live indicator as degraded.

**S10. Health data via the heartbeat.** The worker's heartbeat `detail` (P4-T18 `WorkerDeps.heartbeat_extra`) gains three keys, built by the tap and the publisher and merged in `runtime` (the existing `rate_limit` key stays):
- `questrade`: `{"day": "YYYY-MM-DD" (ET), "since": ISO UTC, "market": {"requests", "http_429", "pause_s", "http_5xx", "transport_errors"}, "account": {same}}`: today's deltas of the client's `stats` (`CallStats`). The baseline is **zero** while the tap has never seen the client's stats (the `LazyQuestrade` opens its client in this process on first use, often inside the 9:35 batch itself, so every count it has is this process's; `since` = when the tap was built), and on the first proxied call or health read whose ET date differs from the baseline's day it becomes a snapshot of the counters taken **before** that call is forwarded (`since` = that time). A baseline taken at a later heartbeat would silently drop the 429s of a batch that opened the client.
- `candle_batches`: today's `candles_many` calls, newest last, at most 5: `{"started_at", "symbols", "completed", "errors", "outstanding", "elapsed_s", "deadline_s", "http_429", "pause_s", "raised"}` (`symbols` = distinct requests, as the client dedupes them; `completed` = results that are candle lists, `errors` = `QuestradeApiError` results, `outstanding` = distinct requests missing from the result, `raised` = exception type name or null, e.g. `CancelledError` when the `_fetch_opening_bars` guard fired; 429 and pause deltas of the market stats across the call). The Control page reads the opening-bar fetch as the first batch of today that started in [09:35, 09:40) ET.
- `marks`: `{"written_at": ISO or null, "symbols": int, "failing": bool}`.
All values are JSON-safe (no NaN or Infinity: `Worker._heartbeat_extra` refuses those and would drop the whole extra).

**S11. Visual style tokens (D9).** One file, `web/src/theme/tokens.css`, defines every colour, spacing and type token for the whole app: a light palette and a dark slate palette (surfaces slate 900/800, 1 px borders slate 700, one accent colour sky 400), selected by `data-theme="light" | "dark"` on `<html>` or, with `data-theme="auto"`, by `prefers-color-scheme`. Separate token families keep D9's colour rule checkable: `--money-up`/`--money-down`/`--money-flat` (green and red, used only through the `.money` classes for money values) and `--status-ok`/`--status-warn`/`--status-bad`/`--status-muted` (accent, amber, orange, slate: never green or red) for lights and badges. Numbers use `--font-num` with `font-variant-numeric: tabular-nums`. Panels are flat (`--panel-radius: 6px`, `--panel-border: 1px solid var(--border)`, no shadows, gradients or glow). `--touch: 44px`. `web/src/theme/tokens.ts` exports the token names, `moneyTone(value)` and `readToken(name)` (charts read CSS variables so both themes work). Existing pages keep their variable names (`--bg`, `--surface`, `--text`, `--border`, `--accent`, `--ok`, `--bad`...), which `tokens.css` now defines, so they re-theme without edits. Default theme: dark (open question 1).

**S12. Navigation and routes (D6).** Primary navigation: Dashboard, Control, Reports, Replay, Settings. The other pages keep their routes and deep links (Telegram's `/trades?position=`, `/journal?date=`, `/candidates`, `/performance`) and sit under "More" (side list: a "More" group; phone: tabs Dashboard, Control, Reports, More → Replay, Settings, Trades, Candidates, Performance, Journal). `/system` (a Telegram link in the Web links contract) redirects to `/control` keeping its query string; `/dashboard?proposal=<id>` keeps working (highlight the pending proposal, or show `ProposalPanel` when it is decided). Settings loses its "Approval mode" and "Kill switches" cards (moved to Control) and shows a one-line card linking to `/control`; everything else on Settings stays.

**S13. How the existing tests migrate.** The backend's `/api/dashboard` and `/api/system` routes stay as they are (no page calls them after this build; removing them is open question 3), so their tests, `test_routes_sweep.py` and `test_web_client_contract.py` stay green. Web: `pages/dashboard/DashboardPage.test.tsx` and `pages/system/SystemPage.test.tsx` are deleted with the pages they test and replaced by `pages/live/LivePage.test.tsx` (DB-T11) and `pages/control/ControlPage.test.tsx` (DB-T9), which carry over every behaviour those tests pinned that still applies (listed in those tasks). `PendingProposal.test.tsx`, `CandidatesPage.test.tsx`, the System component tests (`StatusCards`, `JobRuns`, `RunJob`, `EventLog`, `WatchlistUpload`) and the Settings component tests stay, because Control reuses those components unchanged. `shell.test.tsx`, `test/render.test.tsx`, `pages/touchTargets.test.tsx`, `gauntlet/web_pages_breaker.test.tsx` and `web/tests/smoke.spec.ts` are updated in DB-T11: page lists and nav names change; each assertion about the old Dashboard/System page moves to the equivalent new page (XSS tags, empty states, error boxes, `?proposal=` handling, 390 px layout), none is dropped without an equivalent.

**S14. Performance budget.** `GET /api/live` answers in under 300 ms on a normal day. How: one thread hop; each part uses indexed reads bounded to the live run and one day (or the run's small tables); positions ≤ 20, activity ≤ 100, sparkline ≤ 60 points, equity ≤ 500 points; no Questrade. Tested three ways: a statement count per request (SQLAlchemy `before_cursor_execute` counter; deterministic) that is **the same for 1 and 20 open positions and for 10 and 100 activity items** (no per-row query) and at most `LIVE_MAX_STATEMENTS` = 80 (the timeline alone reuses `services.plan`, which runs several queries; the measured count is recorded in the task notes, and the ceiling may only be lowered), the median of 10 warm requests on the seeded "normal day" under 300 ms in the testcontainer (DB-T12 test 1), and live on trader-dev through the route's `Server-Timing` header (`app;dur=<ms>` first, then one `<part>;dur=<ms>` per part so a slow part is visible), read by the Playwright live smoke as the median of 5 requests after one warm-up (the smoke spec DB-T11 writes; run live at DB-T12 LIVE 5). `GET /api/control` has no hard budget (the soak summary is cached 60 s in-process; target under 1 s).

**S15. Part isolation (§7 of the design).** Each aggregate computes its parts independently: a part that raises becomes `null` in the response and adds `PartErrorOut(part, message)` (message = exception type and a masked, 120-character text; logged once per request with `error_type`), and the route still answers 200. A panel whose part is `null` shows its own error state with Retry (a refetch of the aggregate); the rest of the page renders. A whole-request failure (network, 401, 500) keeps the page frame, shows the error box with Retry, and keeps the last good data visible when there is some.

**S16. Stale and empty states.** A mark is `live` when observed at most `MARK_STALE_SECONDS` = 30 s ago, `stale` when older, `missing` when there is none; a stale or missing mark shows a "prices stale" badge on the positions panel and the row, and never hides the row. The heartbeat badge shows when its age exceeds `HEARTBEAT_BADGE_SECONDS` = 60 s (independent of `worker.heartbeat_stale_seconds`, which still decides `WorkerOut.ok`). Empty states: "No open positions" plus "n trades closed today"; "No activity yet today"; "No rejections recorded today"; on a non-session day the timeline says "Market closed today; next session <date>".

---

## Task table

| ID | Title | Depends on | Lane | Est. |
|---|---|---|---|---|
| DB-T1 | Contracts: migration 0008 and ORM, API schemas and topics, stubs, web types/client/fake/fixtures, theme tokens | none | C (contracts) | 45 min |
| DB-T2 | Worker: quote tap and mark publisher (`trader/marks`, `WorkerDeps.marks`) | DB-T1 | W (worker) | 90 min |
| DB-T3 | Live data A: period P&L and costs, books check, equity series | DB-T1 | A (api data) | 75 min |
| DB-T4 | Live data B: positions with marks, sparklines and bars, risk panel and kill-switch lights | DB-T1 | B (api data) | 75 min |
| DB-T5 | Live data C and route: activity feed, rejections, `GET /api/live` | DB-T1 | L (api route) | 75 min |
| DB-T6 | Control API: engine, strategies, schedule, health, soak, errors, `GET /api/control` | DB-T1 | K (api route) | 70 min |
| DB-T7 | Web: top bar, period P&L, costs, books check, equity chart, risk panel | DB-T1 | WA (web) | 75 min |
| DB-T8 | Web: positions table, rows, sparklines, position chart, activity feed, rejections, today | DB-T1 | WB (web) | 90 min |
| DB-T9 | Web: Control page and its cards | DB-T1 | WC (web) | 80 min |
| DB-T10 | Backend wiring: runtime tap/publisher/heartbeat, D2 behavioural and static proofs | DB-T1, DB-T2 | X (integration) | 60 min |
| DB-T11 | Web wiring: Dashboard page, routes and nav, live-update throttle, Settings move, test migration, smoke | DB-T1, DB-T7, DB-T8, DB-T9 | Y (integration) | 90 min |
| DB-T12 | End to end: seeded day, performance budget, D2 deploy diff, docs, LIVE deploy | DB-T3, DB-T4, DB-T5, DB-T6, DB-T10, DB-T11 | Z (end to end) | 60 min + LIVE |

**Critical path (4 tasks):** DB-T1 → DB-T8 (or DB-T7/DB-T9) → DB-T11 → DB-T12. The backend chain DB-T1 → DB-T2 → DB-T10 → DB-T12 is also 4.

**Maximum parallel width: 8** (DB-T2 to DB-T9 all start when DB-T1 is accepted; the cap of 8 builders is met exactly). Then DB-T10 and DB-T11 run in parallel (width 2; DB-T10 may start as soon as DB-T2 is accepted), then DB-T12 alone.

## File map (one owner per file)

| File | Owner | Change |
|---|---|---|
| `Trader/app/trader/db/migrations/versions/0008_live_marks.py` | DB-T1 | new |
| `Trader/app/trader/db/models.py` | DB-T1 | `QuoteMark`, `MarkBar` added |
| `Trader/app/trader/api/schemas.py` | DB-T1 | new literals and models; `Topic` gains `marks`, `activity` |
| `Trader/app/trader/api/feed.py` | DB-T1 | `WATERMARK_TOPICS` and watermark columns for the two topics |
| `Trader/app/trader/api/routers/__init__.py` | DB-T1 | registers `live` and `control` |
| `Trader/app/trader/marks/__init__.py`, `types.py` | DB-T1 | new (contracts, final) |
| `Trader/app/trader/marks/tap.py`, `publisher.py` | DB-T1 stub → DB-T2 | implemented |
| `Trader/app/tests/live/__init__.py`, `tests/live/test_contracts.py` | DB-T1 | new (DB-T1 test 5 needs the package first) |
| `Trader/app/trader/worker.py` | DB-T2 | `WorkerDeps.marks`, task start and shutdown |
| `Trader/app/trader/api/livedata/__init__.py`, `types.py` | DB-T1 | new (contracts, final) |
| `Trader/app/trader/api/livedata/periods.py`, `books.py`, `equity.py` | DB-T1 stub → DB-T3 | implemented |
| `Trader/app/trader/api/livedata/positions.py`, `risk.py` | DB-T1 stub → DB-T4 | implemented |
| `Trader/app/trader/api/livedata/activity.py` | DB-T1 stub → DB-T5 | implemented |
| `Trader/app/trader/api/routers/live.py` | DB-T1 stub → DB-T5 | implemented |
| `Trader/app/trader/api/livedata/control.py`, `health.py` | DB-T1 stub → DB-T6 | implemented |
| `Trader/app/trader/api/routers/control.py` | DB-T1 stub → DB-T6 | implemented |
| `Trader/app/trader/runtime.py` | DB-T10 | `live_marks`, heartbeat merge, `WorkerDeps(marks=...)` |
| `Trader/web/src/api/types.ts`, `client.ts`, `http.ts`, `queryKeys.ts` | DB-T1 | mirror types, `live`/`control` methods, keys, topics |
| `Trader/web/src/test/fakeApi.ts`, `test/liveFixtures.ts` (new) | DB-T1 | defaults and fixtures |
| `Trader/web/src/theme/tokens.css`, `theme/tokens.ts` (new) | DB-T1 | tokens (final values) |
| `Trader/web/src/pages/live/Panel.tsx` (new) | DB-T1 | shared panel frame (final) |
| `Trader/web/src/pages/live/TopBar.tsx`, `PeriodPnl.tsx`, `CostBar.tsx`, `BooksCheck.tsx`, `EquityChart.tsx`, `RiskPanel.tsx`, `liveA.css` and their tests | DB-T7 | new |
| `Trader/web/src/pages/live/PositionsTable.tsx`, `PositionRow.tsx`, `Sparkline.tsx`, `PositionChart.tsx`, `ActivityFeed.tsx`, `RejectionsPanel.tsx`, `TodayTimeline.tsx`, `liveB.css` and their tests | DB-T8 | new |
| `Trader/web/src/pages/Control.tsx`, `pages/control/*` (new) | DB-T9 | new |
| `Trader/web/src/pages/Dashboard.tsx`, `pages/live/LivePage.test.tsx`, `App.tsx`, `main.tsx`, `styles.css`, `layout/Layout.tsx`, `layout/layout.css`, `live/useLiveUpdates.ts` and test, `pages/Settings.tsx`, `pages/settings/SettingsPage.test.tsx`, `pages/reports/DayDecisions.tsx` and test, `shell.test.tsx`, `test/render.test.tsx`, `pages/touchTargets.test.tsx`, `gauntlet/web_pages_breaker.test.tsx`, `web/tests/smoke.spec.ts`; deletions: `pages/System.tsx`, `pages/system/SystemPage.test.tsx`, `pages/dashboard/DashboardPage.test.tsx`, `pages/dashboard/EventList.tsx`, `KillSwitchLights.tsx`, `PnlTiles.tsx`, `PositionCard.tsx` | DB-T11 | as described |
| `Trader/app/tests/...` new test files | the task that names them | new |
| Existing tests pinning `Topic`, the router count, the alembic head or `API_METHODS` (`tests/test_phase4_contracts.py` and `tests/test_phase5_contracts.py` (both assert `len(ROUTERS) == 18` and the tag order: now 20, `live` and `control` before `stream`), `tests/api/test_feed.py`, `tests/api/test_feed_phase5.py`, `tests/api/test_phase5_schemas.py`, `tests/api/test_stream.py`, `tests/api/test_ts_contract.py`, `tests/api/test_system.py`, `tests/db/test_migration_0007.py`, `web/src/api/queryKeys.test.ts`, `web/src/test/fakeApi.test.ts`, `web/src/api/http.test.ts`) | DB-T1 | the two topics, head `0008`, two methods added |
| `Trader/docs/SPEC.md`, `Trader/docs/plans/2026-09-26-build-master-plan.md` (§7.1 rows) | DB-T12 | docs |

---

## DB-T1: Contracts

**Goal:** Create every shared contract so DB-T2 to DB-T9 can build in parallel against it: the migration and ORM for the two new tables, the API schemas and their TypeScript mirror, the two new SSE topics end to end, stub modules and stub routes with their final signatures, the web client methods, fake responses and fixtures, the theme tokens and the shared panel frame. Design §5.1–§5.4, §6, D7–D9.

**Files:** see the file map (DB-T1 rows). Keep it contracts only: stubs raise `NotImplementedError`; the only behaviour written here is the migration, the ORM, the two feed watermarks, the web client plumbing, the tokens and `Panel`.

**Interfaces (provide):**

Migration `0008_live_marks.py` (`revision = "0008"`, `down_revision = "0007"`; the app role gets DML through the schema's default privileges, as 0007 says):
- `trader.quote_marks`: `run_id bigint NOT NULL REFERENCES trader.runs(id)`, `symbol_id bigint NOT NULL REFERENCES trader.symbols(id)`, `bid numeric(14,4)`, `ask numeric(14,4)`, `last numeric(14,4)`, `quote_time timestamptz` (Questrade's `lastTradeTime`), `observed_at timestamptz NOT NULL` (when the worker received it), `written_at timestamptz NOT NULL`, `is_halted boolean NOT NULL DEFAULT false`; primary key `(run_id, symbol_id)`.
- `trader.mark_bars`: `run_id bigint NOT NULL REFERENCES trader.runs(id)`, `symbol_id bigint NOT NULL REFERENCES trader.symbols(id)`, `minute_start timestamptz NOT NULL`, `open`, `high`, `low`, `close numeric(14,4) NOT NULL`, `samples integer NOT NULL`, `updated_at timestamptz NOT NULL`; primary key `(run_id, symbol_id, minute_start)`; `CHECK (low <= open AND low <= close AND high >= open AND high >= close AND low > 0)`, `CHECK (samples > 0)`.
- Downgrade drops both tables.

ORM (`trader/db/models.py`, Phase 6 section comment "Live marks (migration 0008)"): `class QuoteMark(Base)` (`__tablename__ = "quote_marks"`) and `class MarkBar(Base)` (`__tablename__ = "mark_bars"`) with exactly those columns (`Money` type for prices).

`trader/marks/types.py` (final, no stubs):
```python
PUBLISH_INTERVAL_S: float = 2.0
MAX_OBSERVATIONS_PER_SYMBOL: int = 30       # the tap keeps at most this many undrained per symbol (S1a)
MAX_TAP_SYMBOLS: int = 1000                 # the tap forgets the least recently observed symbols beyond this
MAX_CANDLE_BATCHES: int = 5
MARK_BARS_KEEP_DAYS: int = 10
PUBLISH_STATEMENT_TIMEOUT_MS: int = 5000    # SET LOCAL statement_timeout and lock_timeout of a publisher pass
MARKS_SOURCE: str = "marks"                 # event_log source of the publisher's warning/info events

@dataclass(frozen=True, slots=True)
class ObservedQuote:
    qt_id: int                # Questrade symbol id, as the client returned it
    quote: QtQuote            # the very object the client returned
    observed_at: datetime     # the Clock time the call returned

@dataclass(frozen=True, slots=True)
class CandleBatch:
    started_at: datetime
    symbols: int
    completed: int
    errors: int
    outstanding: int
    elapsed_s: float
    deadline_s: float | None
    http_429: int
    pause_s: float
    raised: str | None

@dataclass(frozen=True, slots=True)
class PublishStep:
    at: datetime
    skipped: Literal["no_run", "no_new_quotes", "error"] | None
    marks_written: int
    bars_written: int

class LatestQuotes(Protocol):
    def drain(self) -> list[ObservedQuote]: ...

EventWriter = Callable[[str, str, dict[str, Any], int | None], None]   # (level, message, data, run_id), never raises
```

`trader/marks/tap.py` (stub; DB-T2 implements): `class QuoteTap` with `__init__(self, inner: QuoteClient, clock: Clock) -> None`; `async quotes(self, ids: Sequence[int]) -> list[QtQuote]`; `async candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]`; `async candles_many(self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None) -> dict[CandleRequest, list[Candle] | QuestradeApiError]`; `stats` property (the inner client's `stats` attribute or None); `drain(self) -> list[ObservedQuote]`; `health_detail(self) -> dict[str, Any]` (the `questrade` and `candle_batches` keys of S10).

`trader/marks/publisher.py` (stub; DB-T2 implements): `@dataclass(frozen=True) class MarkPublisherDeps: factory: sessionmaker[Session]; clock: Clock; tap: LatestQuotes; run_id: Callable[[], int | None]; event: EventWriter | None = None`; `class MarkPublisher` with `__init__(self, deps: MarkPublisherDeps, *, interval_s: float = PUBLISH_INTERVAL_S, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None`, `async run(self, stop: asyncio.Event) -> None`, `async run_once(self) -> PublishStep`, `health_detail(self) -> dict[str, Any]` (the `marks` key of S10), `close(self) -> None` (shuts its executor down without waiting; idempotent).

`trader/api/livedata/types.py` (final):
```python
MARK_STALE_SECONDS = 30
HEARTBEAT_BADGE_SECONDS = 60
SPARK_POINTS = 60
EQUITY_MAX_POINTS = 500
FILL_MARKERS_MAX = 200
ACTIVITY_LIMIT = 100
REJECTION_TICKERS_MAX = 50
EXPAND_MAX = 3
NEAR_STOP_R = Decimal("0.25")
ERRORS_SHOWN = 200
SOAK_CACHE_SECONDS = 60.0
PART_MESSAGE_CHARS = 120
LIVE_MAX_STATEMENTS = 80    # S14 ceiling per GET /api/live (may only be lowered)

@dataclass(frozen=True, slots=True)
class PeriodWindow:
    key: PeriodKey
    date_from: date
    date_to: date
    start_at: datetime    # 00:00 ET of date_from, as UTC
    end_at: datetime      # 00:00 ET of date_to + 1 day, as UTC (exclusive)

@dataclass(frozen=True, slots=True)
class OpenValue:          # one open position's contribution to P&L and equity
    position_id: int
    symbol_id: int
    qty: int
    avg_price: Decimal
    mark: Decimal | None
    entry_fees: Decimal   # round4 of its entry fill's fees.total

@dataclass(frozen=True, slots=True)
class LivePositions:
    positions: list[LivePositionOut]
    open_values: list[OpenValue]
    equity_at_marks: Decimal    # Σ cash_ledger + Σ (mark or avg_price) × qty
    all_marked: bool
```

Stub functions (final signatures; DB-T3/T4/T5/T6 implement; all synchronous, run in a worker thread):
- `periods.py`: `et_day_bounds(d: date) -> tuple[datetime, datetime]`; `session_day(calendar: SessionCalendar, now: datetime) -> date`; `period_windows(calendar: SessionCalendar, now: datetime, run_started_at: datetime) -> tuple[PeriodWindow, PeriodWindow, PeriodWindow]`; `claude_spent_between(factory: sessionmaker[Session], window: PeriodWindow) -> Decimal`; `period_blocks(factory: sessionmaker[Session], run_id: int, windows: Sequence[PeriodWindow], open_values: Sequence[OpenValue]) -> list[PeriodPnlOut]`; `claude_today(factory: sessionmaker[Session], now: datetime, settings: RuntimeSettings) -> ClaudeTodayOut`.
- `books.py`: `books_check(factory: sessionmaker[Session], run_id: int) -> BooksCheckOut`.
- `equity.py`: `downsample(points: Sequence[EquityPointLiveOut], max_points: int = EQUITY_MAX_POINTS) -> tuple[list[EquityPointLiveOut], bool]`; `equity_series(factory: sessionmaker[Session], calendar: SessionCalendar, run_id: int, now: datetime, range_: LiveRange, equity_now: Decimal | None) -> EquitySeriesOut`.
- `positions.py`: `mark_state(observed_at: datetime | None, now: datetime) -> MarkState`; `live_positions(factory: sessionmaker[Session], run_id: int, now: datetime, expand: Collection[int]) -> LivePositions`.
- `risk.py`: `killswitch_lights(killswitches: KillSwitches, factory: sessionmaker[Session], calendar: SessionCalendar, settings: RuntimeSettings, run_id: int, now: datetime, equity: Decimal) -> list[KillSwitchLightOut]`; `trading_state(killswitches: KillSwitches, run_id: int, day: date) -> TradingState`; `risk_panel(killswitches: KillSwitches, registry: StrategyRegistry, factory: sessionmaker[Session], calendar: SessionCalendar, settings: RuntimeSettings, run_id: int, now: datetime, positions: LivePositions) -> RiskOut`.
- `activity.py`: `activity_feed(factory: sessionmaker[Session], run_id: int, day: date, *, limit: int = ACTIVITY_LIMIT) -> list[ActivityItemOut]`; `rejections(factory: sessionmaker[Session], run_id: int, day: date) -> RejectionsOut`.
- `control.py`: `engine_card(services: ApiServices, run_id: int, now: datetime) -> EngineOut`; `strategy_cards(services: ApiServices, run_id: int) -> list[StrategyCardOut]`; `schedule(services: ApiServices, now: datetime, heartbeat_detail: Mapping[str, Any] | None) -> list[ScheduleItemOut]`; `job_summary(job: str, detail: Any, batch: OpeningBarsOut | None) -> str | None`; `error_log(factory: sessionmaker[Session], limit: int = ERRORS_SHOWN) -> list[EventOut]`.
- `health.py`: `health_panel(services: ApiServices, now: datetime) -> HealthPanelOut`; `questrade_stats(detail: Mapping[str, Any] | None, now: datetime) -> QuestradeStatsOut | None`; `opening_bars(detail: Mapping[str, Any] | None, calendar: SessionCalendar, now: datetime) -> OpeningBarsOut | None`; `soak_summary(services: ApiServices, now: datetime) -> SoakSummaryOut`.

Routers (stubs with final signatures, registered in `ROUTERS` after `decisions.router`, before `stream.router`):
- `trader/api/routers/live.py`: `router = APIRouter(tags=["live"], dependencies=[Depends(current_user)])`; `@router.get("/live", response_model=LiveOut) async def get_live(services: Services, response: Response, range_: Annotated[LiveRange, Query(alias="range")] = "today", expand: Annotated[str | None, Query(max_length=64, pattern=r"^[1-9][0-9]{0,18}(,[1-9][0-9]{0,18}){0,2}$")] = None) -> LiveOut`.
- `trader/api/routers/control.py`: same dependency; `@router.get("/control", response_model=ControlOut) async def get_control(services: Services) -> ControlOut`.

`trader/api/schemas.py` additions (new section `# --- live dashboard and control (DB-T1) ---`); `Topic` gains `"marks"` and `"activity"` (appended at the end):
```python
PeriodKey = Literal["today", "week", "run"]
LiveRange = Literal["today", "run"]
MarkState = Literal["live", "stale", "missing"]
TradingState = Literal["running", "paused", "blocked"]   # paused: manual pause; blocked: an automatic switch
ActivityKind = Literal["order_placed", "order_cancelled", "fill", "exit", "proposal_created",
    "proposal_approved", "proposal_rejected", "proposal_expired", "kill_switch_tripped",
    "kill_switch_reset", "job_failed", "alert", "scan"]
ActivityChip = Literal["trades", "proposals", "alerts", "scan"]
ActivityTone = Literal["neutral", "up", "down", "warn"]
EquitySource = Literal["snapshot", "marks", "now"]
BarSource = Literal["candle", "marks"]
KillSwitchUnit = Literal["pct", "r", "none"]
RejectionSource = Literal["decision_log", "candidates", "none"]

class PartErrorOut(ApiModel): part: str; message: str
class PeriodPnlOut(ApiModel): period: PeriodKey; date_from: date; date_to: date; realized: Decimal;
    unrealized: Decimal | None = None; unrealized_partial: bool; pnl_after_fees: Decimal; fees: Decimal;
    claude_usd: Decimal; net_after_ai: Decimal; trades: int; wins: int; losses: int;
    win_rate: Decimal | None = None; expectancy_r: Decimal | None = None; trades_without_r: int
class ClaudeTodayOut(ApiModel): date: dt.date; spent_usd: Decimal; cap_usd: Decimal; used_fraction: Decimal | None = None
class BooksCheckOut(ApiModel): ok: bool; cash: Decimal; positions_at_cost: Decimal; actual: Decimal;
    starting_cash: Decimal; realized_gross: Decimal; fees_paid: Decimal; expected: Decimal;
    difference: Decimal; realized_recorded: Decimal; open_positions: int
class EquityPointLiveOut(ApiModel): ts: UtcDateTime; equity: Decimal; source: EquitySource
class FillMarkerOut(ApiModel): fill_id: int; ts: UtcDateTime; ticker: str; side: str; purpose: str;
    qty: int; price: Decimal; position_id: int | None = None
class EquitySeriesOut(ApiModel): range: LiveRange; start_equity: Decimal | None = None;
    points: list[EquityPointLiveOut]; fills: list[FillMarkerOut]; downsampled: bool
class KillSwitchLightOut(ApiModel): switch: str; label: str; tripped: bool; tripped_at: UtcDateTime | None = None;
    value: Decimal | None = None; threshold: Decimal | None = None; unit: KillSwitchUnit;
    count: int | None = None; count_min: int | None = None; trip_value: Decimal | None = None;
    trip_threshold: Decimal | None = None; automatic: bool; needs_web_reset: bool; clears: str
class RiskOut(ApiModel): killswitches: list[KillSwitchLightOut]; equity: Decimal; open_risk: Decimal;
    open_risk_cap: Decimal | None = None; slots_used: int; slots_max: int; open_positions: int
class SparkPointOut(ApiModel): ts: UtcDateTime; price: Decimal
class BarOut(ApiModel): start: UtcDateTime; open: Decimal; high: Decimal; low: Decimal; close: Decimal; source: BarSource
class LivePositionOut(ApiModel): id: int; symbol_id: int; ticker: str; strategy_key: str;
    side: Literal["long"]; qty: int; entry: Decimal; mark: Decimal | None = None; bid: Decimal | None = None;
    ask: Decimal | None = None; mark_at: UtcDateTime | None = None; mark_state: MarkState;
    stop: Decimal | None = None; stop_working: bool; target: Decimal | None = None;
    planned_risk: Decimal | None = None; unrealized: Decimal | None = None; unrealized_r: Decimal | None = None;
    distance_to_stop_r: Decimal | None = None; near_stop: bool; opened_at: UtcDateTime; held_seconds: int;
    unprotected_seconds: int; spark: list[SparkPointOut]; bars: list[BarOut] | None = None;
    fills: list[FillMarkerOut]; link: str
class ActivityItemOut(ApiModel): id: str; ts: UtcDateTime; kind: ActivityKind; chip: ActivityChip;
    ticker: str | None = None; text: str; amount: Decimal | None = None; tone: ActivityTone; link: str | None = None
class RejectionRuleOut(ApiModel): stage: DecisionStage; rule: str; count: int; tickers: list[str]; truncated: bool; link: str
class RejectionsOut(ApiModel): session_date: date; source: RejectionSource; total: int;
    rules: list[RejectionRuleOut]; final: bool; recorded_at: UtcDateTime | None = None
class LiveOut(ApiModel): server_time: UtcDateTime; run_id: int; run_started_at: UtcDateTime;
    session: SessionInfoOut; session_day: date; approval_mode: Literal["manual", "auto"];
    trading: TradingState; telegram_configured: bool; worker: WorkerOut; worker_stale: bool;
    marks_stale_seconds: int; closed_today: int;
    periods: list[PeriodPnlOut] | None = None; claude_today: ClaudeTodayOut | None = None;
    books: BooksCheckOut | None = None; equity: EquitySeriesOut | None = None; risk: RiskOut | None = None;
    positions: list[LivePositionOut] | None = None; activity: list[ActivityItemOut] | None = None;
    rejections: RejectionsOut | None = None; timeline: list[TimelineItemOut] | None = None;
    pending: list[ProposalOut] | None = None; part_errors: list[PartErrorOut]

class EngineOut(ApiModel): approval_mode: Literal["manual", "auto"]; trading: TradingState;
    paused_at: UtcDateTime | None = None; run_id: int; run_started_at: UtcDateTime; run_start_date: date;
    version: str; app_env: str; alembic_revision: str | None = None
class StrategyCardOut(ApiModel): key: str; kind: Literal["entry", "overlay"]; enabled: bool; revision: int;
    version: str; updated_at: UtcDateTime; updated_by: str | None = None; owns_open_positions: bool;
    max_positions: int | None = None; settings_link: str
class ScheduleItemOut(ApiModel): key: str; label: str; kind: Literal["job", "event"]; at: UtcDateTime;
    status: TimelineStatus; detail: str | None = None; started_at: UtcDateTime | None = None;
    finished_at: UtcDateTime | None = None; duration_seconds: float | None = None; attempts: int;
    summary: str | None = None; rerun: ManualJob | None = None
class QuestradeCountsOut(ApiModel): requests: int; http_429: int; pause_s: float; http_5xx: int; transport_errors: int
class QuestradeStatsOut(ApiModel): day: date; since: UtcDateTime; market: QuestradeCountsOut;
    account: QuestradeCountsOut; rate_limit: dict[str, int] | None = None
class OpeningBarsOut(ApiModel): session_date: date; started_at: UtcDateTime; symbols: int; completed: int;
    errors: int; outstanding: int; elapsed_s: float; deadline_s: float | None = None; http_429: int;
    pause_s: float; complete: bool; raised: str | None = None
class MarksHealthOut(ApiModel): written_at: UtcDateTime | None = None; symbols: int; failing: bool
class HealthPanelOut(ApiModel): worker: WorkerOut; worker_stale: bool; token: TokenOut; db_ok: bool;
    db_latency_ms: int | None = None; telegram_configured: bool; questrade: QuestradeStatsOut | None = None;
    opening_bars: OpeningBarsOut | None = None; marks: MarksHealthOut | None = None;
    notifications_failed: list[NotificationOut]; tz_iana_version: str | None = None
class SoakTodayOut(ApiModel): session_date: date; verdict: str; failed: list[str]; provisional: bool
class SoakSummaryOut(ApiModel): target: int; consecutive_clean: int; total_clean: int;
    day_one: date | None = None; earliest_finish: date | None = None; last_final: date | None = None;
    today: SoakTodayOut | None = None; generated_at: UtcDateTime
class ControlOut(ApiModel): server_time: UtcDateTime; session: SessionInfoOut; manual_jobs: list[ManualJob];
    engine: EngineOut | None = None; killswitches: list[KillSwitchLightOut] | None = None;
    killswitch_history: list[KillSwitchEventOut] | None = None; strategies: list[StrategyCardOut] | None = None;
    schedule: list[ScheduleItemOut] | None = None; health: HealthPanelOut | None = None;
    soak: SoakSummaryOut | None = None; errors: list[EventOut] | None = None; part_errors: list[PartErrorOut]
```

`trader/api/feed.py`: `WATERMARK_TOPICS` gains `"marks"` and `"activity"` (appended); `_watermark_columns()` gains `"marks": (_max(m.QuoteMark.written_at, live_or_unscoped(m.QuoteMark.run_id)),)` and `"activity": (_max(m.DecisionLog.id, live_or_unscoped(m.DecisionLog.run_id)),)`. Still one statement.

Web (`Trader/web/src/`):
- `api/types.ts`: one type per new model and a union per new literal, same names and snake_case fields, `X | None` as `X | null`, optional `?:` only where Python has a default (the `test_ts_contract.py` rules); `Topic` gains `"marks" | "activity"`.
- `api/client.ts`: `export interface LiveQuery { range?: LiveRange; expand?: string; }`; `ApiClient` gains `live(q: LiveQuery): Promise<LiveOut>;` and `control(): Promise<ControlOut>;` (under a `// live dashboard and control (DB-T1)` comment); `API_METHODS` gains `"live"` and `"control"`.
- `api/http.ts`: `live: (q) => get<LiveOut>(\`/live${queryString(q)}\`)`, `control: () => get<ControlOut>("/control")`.
- `api/queryKeys.ts`: `qk.live: (q: LiveQuery = {}) => ["dashboard", "live", q] as const`; `qk.control: () => ["system", "control"] as const`; `TOPIC_KEYS.marks = ["dashboard"]`, `TOPIC_KEYS.activity = ["dashboard"]`.
- `test/liveFixtures.ts` (new): `liveOut` (a session day: 1 open position with a live mark, 3 periods, books ok, 30 equity points, 12 activity items across all chips, 2 rejection rules, a timeline, no pending) and builders `liveWith(overrides)`, `positionsN(n: number, opts?: { staleMarks?: boolean; nearStop?: boolean })` (0, 1 and 20 positions), `liveEmptyDay` (non-session day, no positions, no activity, no rejections), `liveAllPartsFailed` (every part null with a `part_errors` entry each), `controlOut`, `controlWith(overrides)`, `controlStaleWorker` (heartbeat 95 s old), `controlNoSoak`; every free-text field of `liveOut`/`controlOut` has a `<img src=x onerror=alert(1)>`-style variant builder `withXssText()` for the XSS tests.
- `test/fakeApi.ts`: defaults `live: liveOut`, `control: controlOut`.
- `theme/tokens.css` (final values, S11) and `theme/tokens.ts`: `export const TOKENS` (a `const` object mapping a short name to each CSS variable: `bg`, `surface`, `surface2`, `text`, `textMuted`, `border`, `accent`, `accentText`, `moneyUp`, `moneyDown`, `moneyFlat`, `statusOk`, `statusWarn`, `statusBad`, `statusMuted`, `chartGrid`, `chartLine`, `chartEntry`, `chartStop`, `chartTarget`, `chartFill`, `fontNum`, `panelRadius`, `panelBorder`, `touch`, plus the existing `ok`, `okBg`, `warn`, `warnBg`, `bad`, `badBg`, `info`, `infoBg`, `muted`, `mutedBg`, `space1`–`space6`, `radius`, `font`, `mono`), `export type MoneyTone = "up" | "down" | "flat"`, `export function moneyTone(value: string | number | null | undefined): MoneyTone` (sign of a decimal string; null/zero → flat), `export function readToken(name: string): string` (computed value on `document.documentElement`, "" in tests), `export type ThemeChoice = "auto" | "dark" | "light"`, `export const THEME_STORAGE_KEY = "trader.theme"`.
- `pages/live/Panel.tsx`: `export function Panel(props: { title: string; ariaLabel?: string; error?: string | null; onRetry?: () => void; empty?: ReactNode | null; badge?: ReactNode; children?: ReactNode }): JSX.Element` (a `<section aria-label>` with a heading; when `error` is set it shows the text and a 44 px "Retry" button instead of the children; when `empty` is set and there are no children it shows it as plain text).

**Behaviour and decisions:**
- The migration is additive (two new tables); nothing existing changes. `quote_marks`/`mark_bars` are run-scoped like every trading table (SPEC §3a) so a later live run starts clean; a replay never writes them (DB-T2 test 9).
- `tests/api/test_ts_contract.py`'s literal list gains every new literal (`PeriodKey`, `LiveRange`, `MarkState`, `TradingState`, `ActivityKind`, `ActivityChip`, `ActivityTone`, `EquitySource`, `BarSource`, `KillSwitchUnit`, `RejectionSource`).
- Existing tests that pin the topic list, the alembic head or the method list are updated in this task (file map row); none of them is in a rule-3 folder.
- `Panel` and the tokens are final here because three parallel web tasks share them; later tasks never edit them (a needed change is reported to the orchestrator).

**Acceptance tests:**
- [x] 1. `tests/db/test_migration_0008.py`: upgrade to 0008 creates both tables with the listed columns, keys and checks (a bar with `low > open` and one with `samples = 0` are refused); downgrade to 0007 drops them; `alembic heads` is `0008`; the app role can insert, update and delete rows (default privileges).
- [x] 2. The ORM matches the migration: the existing `tests/db/test_migration.py::test_models_match_migrated_schema` (alembic `compare_metadata`) and `test_alembic_check_through_env_sees_no_changes` pass unchanged with the two new tables.
- [x] 3. `tests/api/test_ts_contract.py` passes with every new model and literal mirrored; `tests/api/test_web_client_contract.py` passes with `live` (`GET /api/live`, query names `range`, `expand`) and `control` (`GET /api/control`).
- [x] 4. `tests/api/test_routes_sweep.py` passes: both new routes answer 401 without a session.
- [x] 5. `tests/live/test_contracts.py` (new): every stub in `trader/marks/tap.py`, `trader/marks/publisher.py` and `trader/api/livedata/{periods,books,equity,positions,risk,activity,control,health}.py` raises `NotImplementedError`; the signatures match this plan (inspect: parameter names, kinds, defaults); importing `trader.marks` and `trader.api.livedata` imports nothing from `trader.engine.orchestrator`, `trader.broker.sim_broker` or `trader.strategies` except the read-only names the Global Constraints list.
- [x] 6. Feed: `watermarks(s)` returns the two new topics; a new `quote_marks` row of the live run changes `marks`, one of a replay run does not; a new `decision_log` row of the live run changes `activity`, one of a replay run does not; still one SQL statement.
- [x] 7. Web: `queryKeys.test.ts` (every `Topic` has `TOPIC_KEYS`, `qk.live()` starts with `"dashboard"`, `qk.control()` with `"system"`), `fakeApi.test.ts` (defaults for `live` and `control`), `http.test.ts` (`live({range: "run", expand: "12,15"})` requests `/api/live?range=run&expand=12%2C15`; `control()` requests `/api/control`) pass.
- [x] 8. `tokens.css` defines every name in `TOKENS` in both palettes (a vitest reads the file and checks each variable appears under `[data-theme="dark"]`, `[data-theme="light"]` and the `prefers-color-scheme` blocks); `--money-*` values are green/red hues and no `--status-*` value is (hue check on the hex values: money-up hue in 90–160°, money-down hue in 340–20°, every status hue outside both ranges); `moneyTone("-0.01") === "down"`, `moneyTone("0") === "flat"`, `moneyTone(null) === "flat"`.
- [x] 9. `Panel` renders its title as a heading inside a labelled region, shows the error text and a Retry button (≥ 44 px, calls `onRetry`) instead of the children when `error` is set, and renders error text containing `<script>` as plain text.
- [x] 10. Gate (`bash Trader/build/gate.sh`) and commit `DB-T1: live dashboard contracts (0008, schemas, stubs, web types, tokens)`.

**LIVE steps:** none.

---

## DB-T2: Worker quote tap and mark publisher

**Goal:** Implement S1: the transparent `QuoteTap` and the `MarkPublisher` task, and start the publisher from the worker. D8, D10, design §5.2.

**Files:** `Trader/app/trader/marks/tap.py`, `Trader/app/trader/marks/publisher.py`, `Trader/app/trader/worker.py`; tests `Trader/app/tests/marks/__init__.py`, `tests/marks/test_tap.py`, `tests/marks/test_publisher.py`, `tests/marks/test_publisher_db.py` (testcontainer), `tests/test_worker_marks.py` (new, beside the existing `tests/test_worker.py`, which stays unchanged).

**Interfaces (consume):** `trader.market.data_service.QuoteClient` (protocol: `quotes`, `candles`, `candles_many`), `trader.adapters.questrade.models.QtQuote`/`CandleRequest`, `trader.adapters.questrade.client.QuestradeApiError`/`CallStats` (read its fields with `dataclasses.asdict`, as `MarketDataService._market_stats` does), `Clock`, `et_date`, `ET`, `m.Symbol`, `m.Position`, `m.Order`, `m.QuoteMark`, `m.MarkBar`; `Worker._supervise`, `Worker._finish` (existing).

**Interfaces (provide):** the DB-T1 signatures, plus `trader/worker.py`: `WorkerDeps.marks: MarkPublisher | None = None` (last field, after `decisions`).

**Behaviour and decisions:**
- **Tap rules: S1a, all of them** (pass-through shape, same objects and errors, `candles_many` untouched, live `stats`, bounded memory, no I/O). In short: `quotes(ids)` is `result = await self._inner.quotes(ids)`, then a guarded block records one `ObservedQuote(q.symbol_id, q, clock.now())` per element (`q.symbol_id` is the Questrade id: `MarketDataService.quotes` maps it to `symbols.id` only after the tap returns), then `return result`; an exception or cancellation from the inner call propagates before anything is recorded. `candles` is `return await self._inner.candles(symbol_id, start, end, interval)` and nothing else. `candles_many(reqs, *, deadline_s=None)` takes the day baseline if due and the `before` snapshot, then in `try: result = await self._inner.candles_many(reqs, deadline_s=deadline_s)` / `except BaseException as exc: <guarded append of a CandleBatch with raised=type(exc).__name__>; raise` / `else: <guarded append>`, then `return result`.
- **Tap memory.** Undrained observations are kept per Questrade id in a bounded deque (`MAX_OBSERVATIONS_PER_SYMBOL`, oldest dropped) inside an insertion-ordered dict of at most `MAX_TAP_SYMBOLS` ids (an id moves to the end when observed; the first is dropped beyond the cap). `drain()` swaps the dict for an empty one and returns the observations in observation order; it is synchronous, does no I/O, and is only ever called on the event loop's thread (the publisher calls it before handing work to its executor), so no lock of any kind is needed or allowed.
- **Tap health.** `health_detail()` returns S10's `questrade` block (today's deltas of `stats["market"]` and `stats["account"]` against the S10 baseline: zero until the first ET date change, then the snapshot taken before the first call of the new day) and `candle_batches` (today's, newest last, at most `MAX_CANDLE_BATCHES`); `questrade` is absent while the inner client has no `stats` (a `LazyQuestrade` that has not opened its client returns None). Floats rounded to 3 dp; everything JSON-safe (`json.dumps(..., allow_nan=False)` passes). It is synchronous, O(1), and never raises (a failure returns `{}` and logs once per streak at `warning`).
- **Publisher loop.** `run(stop)` repeats `run_once()` then sleeps `interval_s` (waking early on `stop`) until `stop` is set; it never raises. `run_once()`: drain the tap on the loop; no observations → `skipped="no_new_quotes"` with **no database access**; else, in the publisher's own single-thread executor (`loop.run_in_executor(self._executor, ...)`, never the default executor; Global Constraints), in one short session and one transaction that first runs `SET LOCAL statement_timeout` and `SET LOCAL lock_timeout` to `PUBLISH_STATEMENT_TIMEOUT_MS` and reads trading tables with plain SELECTs only (no `FOR UPDATE`, no advisory lock), so it never waits on or blocks a trading row lock and holds at most one pooled connection at a time: resolve `run_id()` (None → `no_run`, observations discarded), map Questrade ids to `trader.symbols.id` (an in-memory cache; misses read `select(Symbol.id, Symbol.questrade_id).where(questrade_id in ...)`), keep only symbols of open positions or working orders of that run (one query), then in one transaction upsert `quote_marks` (latest observation per symbol: `bid`, `ask`, `last` = `q.last` else `q.last_regular`, `quote_time` = `q.last_trade_time`, `observed_at`, `written_at` = `clock.now()`, `is_halted`; an older observation never overwrites a newer row: `ON CONFLICT DO UPDATE ... WHERE quote_marks.observed_at <= excluded.observed_at`) and `mark_bars` (per symbol and minute of `observed_at`, from observations with a price: insert open=high=low=close=price, samples=n; on conflict keep `open`, take `greatest`/`least` for high/low, `close` from the latest observation of that minute, `samples = mark_bars.samples + n`). Prices ≤ 0 or None are skipped for bars. Once per ET day (the first successful write of a new ET date) it also deletes this run's `mark_bars` older than `MARK_BARS_KEEP_DAYS` days.
- **Failure policy.** A failing pass (database down, mapping error, statement timeout) logs one masked warning and writes one `warning` event through `deps.event` (called inside the executor, never on the loop) per failure streak (source `marks`, never relayed: the relay relays only `error`/`critical`), and one `info` "marks recovered after N failures" on the next success; `health_detail()["failing"]` is true during a streak. The drained observations of a failed pass are dropped (the next poll brings fresh ones).
- **Worker.** `Worker.run` starts `self._supervise("marks_publisher", self.deps.marks.run, stop)` as `tasks["marks"]` when `deps.marks` is set (not with `--once`), **after** the decisions loop (the existing tasks start in the same order as before); `_shutdown` finishes it with `self._finish(tasks.get("marks"), 0)` beside the decisions loop (a pass is idempotent, so no grace; a pass stuck in the executor is abandoned, its thread ends at its statement timeout). Nothing else in `worker.py` changes; the step loop, relay, heartbeat and bot behave exactly as before.

**Acceptance tests:**
- [ ] 1. Tap transparency (quotes): with a fake inner client, `await tap.quotes(ids)` returns the identical list object (`is`), the inner was called once with the identical `ids` object, and each quote object in `drain()` is identical (`is`) to the returned element.
- [ ] 2. Tap transparency (errors): a `QuestradeApiError`, a `RuntimeError`, an `asyncio.CancelledError` and a `KeyboardInterrupt` from the inner call propagate as the same instance (`is`, same `__traceback__` tail frame in the inner fake) for `quotes`, `candles` and `candles_many`; nothing is recorded for `quotes`; a recording failure (the clock made to raise, the deque made to raise) is swallowed, logged once per streak at `warning`, and the caller still gets the identical inner result; a recording failure while an inner exception propagates leaves that exception (not the recording error) with the caller and no `__context__` from the tap.
- [ ] 3. Tap `candles_many`: result dict returned unchanged (`is`); `deadline_s` passed through as the same keyword value (a float, None, and omitted → None); `reqs` passed as the same object and not iterated before the inner call (a sequence that records iteration); a batch recorded with symbols/completed/errors/outstanding counted as S10 says (duplicate requests counted once) and the 429/pause deltas from a fake `stats` (None before the call and a `CallStats` after → deltas from zero); the tap keeps no reference to `reqs` or the result (a `weakref` to the result dict dies after the caller drops it); a raising inner call records `raised` and re-raises the same instance; `candles` is a pure pass-through.
- [ ] 4. Tap `stats` is live: it equals `inner.stats` on every access (a fake whose `stats` changes after the tap is built, and one that is None then a dict, as `LazyQuestrade` before and after opening). With the tap between a real `MarketDataService` and a fake client, `opening_bars` produces the same `OpeningBars` and the same `market.opening_bars_*` log fields (`client_stats` included) as without the tap.
- [ ] 5. Tap bounds and health: 50 observations of one symbol keep the newest 30; 1,200 symbols keep the 1,000 most recently observed; `health_detail()` gives today's deltas, counts a batch that opened the client from zero (stats None before the first call), resets its baseline at the ET date change to the counters before the first call of the new day (fixed clock moved across 00:00 ET, in EDT and EST), and is `json.dumps(..., allow_nan=False)`-safe.
- [ ] 6. Publisher with no observations does no database work (a factory that raises on use is never called) and returns `no_new_quotes`.
- [ ] 7. (db) Publisher writes: two observations of a held symbol and one of an unheld symbol → one `quote_marks` row for the held symbol with the newest values, none for the unheld; a working-order-only symbol is written; an older observation arriving later does not overwrite a newer row; `mark_bars` for one minute has the first price as open, max/min, the last price as close and `samples` = 2; a second pass in the same minute updates it (samples add up).
- [ ] 8. (db) Retention: bars older than 10 days of the run are deleted once on the first write of a new ET day, other runs' bars untouched.
- [ ] 9. (db) Run scoping: `run_id()` returning None writes nothing; rows always carry the live run id given; no replay run id is ever used (the publisher only takes `run_id()`).
- [ ] 10. Failure policy: a raising factory during a pass → one warning event per streak (3 failing passes → 1 event), `failing` true, `run()` keeps looping; the next success writes one `info` event and `failing` false; `run_once` never raises.
- [ ] 11. Off the event loop and off the default executor: a publisher whose database step sleeps 3 s in its thread does not delay a concurrent coroutine that ticks every 0.1 s (the tick count during the pass is ≥ 25), and while it is stuck an `asyncio.to_thread` call (what the Questrade client's token fetch uses) still completes at once, even with the loop's default executor limited to one worker (`loop.set_default_executor(ThreadPoolExecutor(1))`); the database step runs on a thread named `marks*`; (db) the pass's transaction has `statement_timeout` and `lock_timeout` set (read back with `SHOW` inside a monkeypatched step), and a trading row locked `FOR UPDATE` by another session does not block the publisher's reads.
- [ ] 12. Worker: with `WorkerDeps(marks=<fake publisher>)` the worker starts it beside the relay, restarts it after 30 s when its `run` raises (fake sleep), cancels it on stop without waiting, and `--once` never starts it; with `marks=None` nothing changes (the existing worker tests pass unchanged).
- [ ] 13. Static: `trader/marks/` imports no write API of the decision path (AST: no reference to `submit`, `snapshot_equity`, `evaluate`, `pause`, `reset`, `record`, `log_event`); it writes only `quote_marks` and `mark_bars` (AST: the only ORM tables named in insert/update/delete are those two); no decision-path module imports `trader.marks`. For `tap.py` alone (AST): each of `quotes`, `candles`, `candles_many` contains exactly one `await`, whose operand is a call of the same-named method on `self._inner`; the module references none of `create_task`, `ensure_future`, `gather`, `wait`, `wait_for`, `shield`, `timeout`, `sleep`, `to_thread`, `run_in_executor`, `Lock`, `Semaphore`, `Event`, `Condition`, `open`, `sessionmaker`, `Session`, `select`, `insert`, `httpx`; no `return` inside a `finally`; no `log.error`/`log.critical`/`log.exception`.
- [ ] 14. No yield of its own: with an inner fake whose methods complete without suspending, `tap.quotes(ids).send(None)`, `tap.candles(...).send(None)` and `tap.candles_many(reqs, deadline_s=45.0).send(None)` each raise `StopIteration` carrying the identical inner result on the first `send` (the tap never suspends where the inner call does not); with an inner fake that suspends exactly once, the tapped coroutine suspends exactly once too.
- [ ] 15. The 9:35 guard is unchanged: through a real `MarketDataService` with `fetch_deadline_s=0.2`, (a) a fake client that honours `deadline_s` and completes half the requests gives the same `OpeningBars.missing` (`"timeout"` for the rest) with and without the tap, and (b) a fake client that ignores `deadline_s` and sleeps is stopped by `_fetch_opening_bars`'s own `asyncio.timeout` guard at the same loop time (± 10 ms) with and without the tap, returning the same all-`"timeout"` result, and the tap records one batch with `raised="CancelledError"`; the inner fake saw the identical `deadline_s` in both runs.
- [ ] 16. Gate and commit `DB-T2: worker quote tap and mark publisher`.

**LIVE steps:** none (DB-T12 deploys).

---

## DB-T3: Live data A: period P&L and costs, books check, equity series

**Goal:** Implement S3, S4, S5 and S6 in `trader/api/livedata/periods.py`, `books.py` and `equity.py`. D3, D4, design §3 items 1–2, §5.1.

**Files:** those three modules; tests (the `tests/live/` package is DB-T1's) `tests/live/test_periods.py`, `tests/live/test_books.py`, `tests/live/test_equity.py`, `tests/live/test_live_data_db.py` (testcontainer).

**Interfaces (consume):** `trader.reports.metrics.metrics_from_rows`, `TradeRow`; `trader.reports.weekly.claude_spent`; `trader.market.calendar.SessionCalendar` (`is_session`, `previous_session`, `session_open`, `session_close`); `trader.market.clock.ET`, `et_date`; `trader.broker.types.Fees.from_json` (read-only, for fee totals); ORM `Trade`, `Fill`, `Order`, `CashLedger`, `SimAccount`, `Run`, `Position`, `EquitySnapshot`, `Catalyst`, `WeeklyReport`, `MarkBar`, `IntradayCandle`, `CandleArchive`.

**Interfaces (provide):** the DB-T1 signatures in `periods.py`, `books.py`, `equity.py`.

**Behaviour and decisions:**
- `et_day_bounds`, `session_day`, `period_windows` exactly as S5 (week starts Monday; `run` from the ET date of `runs.started_at`; windows use `zoneinfo`, never a fixed offset).
- `period_blocks` as S4: one query for the run's trades (`pnl`, `pnl_r`, `qty`, `slippage_total`, `fees_total`, `session_date`), bucketed per window, each block's counts from `metrics_from_rows`; fees from one query of the run's fills (`ts`, `fees`) summed per window with `round4_half_up(Fees.from_json(fees).total)`; Claude spend per window from `claude_spent_between`; `unrealized` from `open_values` (S4 wording, same value in the three blocks). All money exact `Decimal`, quantized to 4 dp half up.
- `claude_today` as S4; `used_fraction` = spent / cap rounded to 4 dp, None when the cap is 0.
- `books_check` as S3 (one read-only session; the numbers of `BooksCheckOut` are all 4 dp); `open_positions` counts the run's open positions.
- `equity_series` as S6. For `today`, the derived per-minute points cover minutes from the session open of `session_day` to `min(now, close)`; a minute's bar is the stored 1-minute candle (`intraday_candles` interval `1m` keyed by `ts`, then `candle_archive` interval `1m` keyed by `start_ts`) when present, else the `mark_bars` row (read-only, kept as a `BarOut`/equity point, never turned into a `trader.market.types.Candle`). `equity_now` (from DB-T4's `LivePositions.equity_at_marks`) becomes the final `now` point when given; for a non-session `session_day` in the past no `now` point is added to `today` (the day is over), but it is added to `run`. Points are sorted by `ts`, duplicates at the same instant keep `snapshot` over `marks` over `now`.
- Only rows of `run_id` are read (replay rows can never enter).

**Acceptance tests (pure unless marked db):**
- [ ] 1. `period_windows` on Wednesday 2026-10-07 14:00 ET: today 10-07, week 10-05..10-07, run from the run's ET start date; on Saturday 2026-10-10 12:00 ET: today = Friday 10-09 (last session), week 10-05..10-10.
- [ ] 2. DST: `et_day_bounds(2026-11-01)` is `[2026-11-01T04:00Z, 2026-11-02T05:00Z)` (25 hours) and `et_day_bounds(2026-03-08)` is `[2026-03-08T05:00Z, 2026-03-09T04:00Z)` (23 hours); the S5 pinned fill cases land in the stated weeks.
- [ ] 3. Holidays: on Thanksgiving 2026-11-26 `session_day` is 2026-11-25; on Monday 2026-10-12 (a session day) before the open `session_day` is 2026-10-12.
- [ ] 4. `period_blocks` (db): trades on Monday and Wednesday of the week, one the previous Friday, one of a replay run on Wednesday and one of an older live run → today, week and run realized equal the hand sums; the replay and old-run trades are never counted; wins/losses/win rate/expectancy equal `compute_metrics` over the same dates.
- [ ] 5. Fees (db): the week's fees equal Σ of the rounded fill fees with `ts` in the week bounds, including a fill at 2026-11-02T04:59:59Z counted in the week of 2026-10-26 (DST end); `pnl_after_fees` does not subtract fees again (a trade with pnl −12.50 and fees 0.35 gives realized −12.50, fees 0.35).
- [ ] 6. Claude (db): catalysts on three session dates and a weekly report updated on Saturday → today/week/run spend as S4; `net_after_ai` = `pnl_after_fees` − spend; `claude_today` equals `claude_spent(factory, et_date(now))` and the cap is `claude_daily_budget_usd`.
- [ ] 7. Unrealized: open values with marks give (mark − avg) × qty − entry fees; one without a mark sets `unrealized_partial`; none with a mark gives `unrealized` None and `pnl_after_fees` = realized.
- [ ] 8. Invariant (db): on a seeded run with closed trades and two marked open positions, the run block's `pnl_after_fees` equals `equity_at_marks − starting_cash` exactly.
- [ ] 9. Empty run (db): no trades, no fills → every block zero, win rate and expectancy None, no error.
- [ ] 10. Books ✓ (db): a run built through the real `SimBroker` (fills, ledger rows, trades) with 150 round trips and odd fee fractions (SEC fee to 7 dp) → `ok` true and `difference` exactly 0.0000 (no drift).
- [ ] 11. Books ✗ (db, each from the ✓ state): delete a closed position's `trades` row (the table is not append-only) → ✗ with the difference equal to that trade's gross; add a duplicate `fee` ledger row → ✗ by that fee (the ledger is append-only, so corrupt by inserting); change an open position's `avg_price` → ✗.
- [ ] 12. Books with open positions: two open positions (entry fees paid) → ✓; `positions_at_cost` = Σ round4(avg × qty); `realized_recorded` = Σ `trades.pnl`.
- [ ] 13. Books on a brand-new run (deposit only) → ✓ with every component zero except cash = starting cash.
- [ ] 14. `books_check` reads only (a statement listener sees only SELECTs).
- [ ] 15. `downsample`: 499 and 500 points unchanged (`downsampled` false); 10,000 points → at most 500, first and last kept, the global minimum and maximum kept, time order kept, deterministic.
- [ ] 16. `equity_series` (db): `today` with 2 snapshots, 30 minutes of `mark_bars` while one position was open, and `equity_now` → snapshot points, 30 derived `marks` points whose values equal cash-as-of + qty × bar close, and one `now` point; `start_equity` = the last snapshot before the open (or starting cash when none); a stored 1-minute candle overrides the mark bar of its minute; `run` range → snapshots of the whole run plus `now`, `start_equity` = starting cash; fills of the range as markers.
- [ ] 17. Gate and commit `DB-T3: live P&L periods, books check and equity series`.

**LIVE steps:** none.

---

## DB-T4: Live data B: positions with marks, sparklines and bars, risk panel

**Goal:** Implement the positions part (≤ 20 rows with marks, R, distance to stop, sparklines, expanded bars) and the risk panel with live kill-switch values, in `positions.py` and `risk.py`. D2, D8, design §3 items 2–3, §5.1–5.2.

**Files:** `Trader/app/trader/api/livedata/positions.py`, `risk.py`; tests `tests/live/test_positions.py`, `tests/live/test_risk.py`, `tests/live/test_positions_db.py` (testcontainer).

**Interfaces (consume):** `trader.notify.views.open_position_rows` (positions with tickers and working stop prices: the same stop logic as Telegram's `/positions`, read-only), `trader.api.routers.trading.strategy_keys`, ORM `QuoteMark`, `MarkBar`, `IntradayCandle`, `CandleArchive`, `Fill`, `Order`, `Signal`, `Proposal`, `CashLedger`; `trader.engine.killswitch.KillSwitches.active`/`inputs`/`blocking`, `SWITCHES`; `trader.api.views.killswitch_states` (labels, `needs_web_reset`, `clears`); `trader.broker.ledger.Ledger.balances` and `trader.broker.types.AccountState` (to build the account `inputs` needs); `StrategyRegistry.keys`/`current`/`plugin_class`.

**Interfaces (provide):** the DB-T1 signatures in `positions.py` and `risk.py`.

**Behaviour and decisions:**
- One row per open position of the run (`closed_at IS NULL`), at most 20 (the design's cap; more are still returned, the UI handles 20: say so in a comment; D2 sizes the UI). `side` is always `"long"` (positions have `qty ≥ 0`; the engine only enters long).
- Mark: the run's `quote_marks` row of the symbol; `mark` = its `last` (None when null), `bid`, `ask`, `mark_at` = `observed_at`, `mark_state` by `mark_state()` (S16).
- `stop` / `stop_working` from `open_position_rows` (the newest working stop order's price, else the position's `stop_loss` with `stop_working` false); `target` = a numeric `target` key in the entry signal's `evidence` when present, else None (`orb_sip` has none).
- `planned_risk` = `positions.planned_risk`; `unrealized` = (mark − avg) × qty (gross, the Telegram definition; the P&L periods subtract entry fees separately); `unrealized_r` = unrealized / planned_risk (4 dp) when both exist; `distance_to_stop_r` = (mark − stop) / (planned_risk / qty) when mark, stop and planned_risk exist (0 or negative when at or through the stop); `near_stop` = `distance_to_stop_r ≤ 0.25`; `held_seconds` = now − `opened_at`; `unprotected_seconds` as `notify.views.lines_from` computes it.
- Bars since entry: stored 1-minute candles (`intraday_candles` `1m` by `ts`, then `candle_archive` `1m` by `start_ts`) for minutes they cover, else `mark_bars` (as `BarOut(source="marks")` only, never a `Candle`), from the minute of `opened_at` to now, at most 390 (the latest kept). `spark` = those bars' closes reduced to at most 59 points by taking every k-th (k = ceil(n / 59), always keeping the last) plus a final point at `mark_at` with the mark (so ≤ 60). `bars` is filled only for ids in `expand` (at most `EXPAND_MAX`; others ignored). `fills` = the position's fills (entry and exit legs).
- `open_values` and `equity_at_marks` as DB-T1 types say (cash from `Ledger.balances(...).total`), `all_marked` true when every open position has a mark.
- `link` = `/trades?position=<id>`.
- Risk panel: `open_risk` = Σ max(0, (avg − stop) × qty) over open positions with a stop; `slots_max` = Σ `params["max_positions"]` (default 1) over enabled strategies whose plug-in `kind == "entry"`; `slots_used` = positions of the run opened on `session_day` by those strategies' configs (entries today, the count `max_positions` limits); `open_risk_cap` = `risk_pct × equity × slots_max` rounded to 2 dp (None when `slots_max` is 0); `equity` = `equity_at_marks`.
- `killswitch_lights`: the four `SWITCHES` in order with the labels/`needs_web_reset`/`clears` of `api.views.killswitch_states`, `tripped`/`tripped_at` and `trip_value`/`trip_threshold` from the open event, and live values from `KillSwitches.inputs(run_id, session_day, account, session_open)` with an `AccountState` built from the ledger balances and `equity_at_marks`: `daily_loss_pct` value = −`daily_pnl_pct`, threshold `killswitch_daily_loss_pct`, unit `pct`; `max_drawdown_pct` value = `drawdown_pct`, threshold `killswitch_max_drawdown_pct`, unit `pct`; `expectancy` value = `expectancy_r`, threshold `killswitch_expectancy_threshold_r`, unit `r`, `count` = `closed_trades`, `count_min` = `killswitch_expectancy_min_trades`; `manual_pause` unit `none`, no values. On a non-session `session_day` the session open used is that day's open.
- `trading_state`: `paused` when `manual_pause` is active, else `blocked` when `KillSwitches.blocking(run_id, day)` names another switch, else `running`.
- Read-only: no write of any kind (`KillSwitches.evaluate` is never called).

**Acceptance tests:**
- [x] 1. `mark_state`: observed 30 s ago → `live`, 30.001 s → `stale`, None → `missing`.
- [x] 2. (db) One position with a fresh mark: mark, bid, ask, `unrealized`, `unrealized_r`, `distance_to_stop_r` and `near_stop` as defined (a mark 0.2 R above the stop → near); a working stop order's price wins over `stop_loss`; a position whose stop order is cancelled shows `stop_working` false and the stop loss.
- [x] 3. (db) Stale and missing marks: a 45 s old mark → `stale` with its value kept; no mark → `missing`, `unrealized` None, row still present.
- [x] 4. (db) 0, 1 and 20 open positions → 0, 1 and 20 rows; each with `spark` ≤ 60 points ending at the mark; closed positions and positions of a replay run never appear.
- [x] 5. (db) Bars: 100 minutes of `mark_bars` and 20 minutes of stored 1-minute candles overlapping → `bars` for an expanded id has one bar per minute with `source` `candle` where candles exist; `expand` with 4 ids fills only the first 3; an id not open is ignored.
- [x] 6. (db) `open_values` and `equity_at_marks`: equals ledger cash + Σ (mark or avg) × qty; `all_marked` false with one missing mark.
- [x] 7. (db) Risk: two positions with stops → `open_risk` = Σ (avg − stop) × qty; a position whose stop is above its entry (trailed) contributes 0; `slots_max` from the enabled entry strategy's `max_positions` (an overlay strategy adds nothing); `slots_used` counts today's entries only.
- [x] 8. (db) Kill-switch lights: with a prior-close snapshot of 1000 and equity at marks 960 → daily loss value 0.04 vs threshold 0.05, not tripped; a tripped `max_drawdown_pct` event → `tripped`, `trip_value`/`trip_threshold` from the event, `needs_web_reset` as `killswitch_states`; `expectancy` shows count vs min; `manual_pause` active → `trading_state` `paused`; another switch tripped → `blocked`.
- [x] 9. Read-only (db): a statement listener sees only SELECTs across `live_positions`, `risk_panel` and `killswitch_lights`.
- [x] 10. No Questrade (static): DB-T4 creates `tests/live/test_no_questrade_static.py`, an AST check that none of the eight `trader/api/livedata/` modules nor `routers/live.py` nor `routers/control.py` references `quotes` or `candles` attributes of the services, `open_positions`, `last_prices`, `position_lines`, `OffLoopMarketData`, `MarketDataService`, `LazyQuestrade`, `QuestradeClient`, `questrade_client`, `trader.market.data_service` or `trader.adapters.questrade.client`, or imports `httpx` (it covers all ten files from the start, so DB-T5 and DB-T6 never edit it; the stubs pass trivially).
- [x] 11. Gate and commit `DB-T4: live positions, sparklines and risk panel`.

**LIVE steps:** none.

---

## DB-T5: Live data C and the `/api/live` route

**Goal:** Implement the activity feed (S7), the rejections panel (S8) and the `GET /api/live` aggregate with part isolation (S15), the `Server-Timing` header (S14) and the `?range=`/`?expand=` parameters. D5, D7, design §3 items 1–5, §5.1.

**Files:** `Trader/app/trader/api/livedata/activity.py`, `Trader/app/trader/api/routers/live.py`; tests `tests/live/test_activity.py`, `tests/live/test_activity_db.py`, `tests/api/test_live_route.py`.

**Interfaces (consume):** DB-T1 stubs of DB-T3/DB-T4 (real once those land; this task's route tests monkeypatch them); `trader.api.routers.dashboard.build_timeline`, `DAY_JOBS`, `RunRow`, `EVENT_LABELS` (import, unchanged), `ApiServices.plan`/`fired`, `trader.api.deps.live_run_id`, `_settings`; `trader.api.views.worker_out`, `event_out`; `trader.notify.views.proposal_view`, `ProposalOut.from_view`; `trader.api.feed.live_or_unscoped`; ORM `Order`, `Fill`, `Trade`, `Proposal`, `KillSwitchEvent`, `JobRun`, `EventLog`, `Candidate`, `DecisionLog`, `Symbol`.

**Interfaces (provide):** `activity_feed`, `rejections` (DB-T1 signatures) and the route.

**Behaviour and decisions:**
- `activity_feed` builds S7's items for the ET day of `day` (bounds from `periods.et_day_bounds`), merges them newest first (ties by id), keeps `limit`. Each source query is bounded by the day and the run (orders, fills, trades, proposals, kill-switch events by `run_id`; job runs by `finished_at`; events by `live_or_unscoped`). Text is built from stored values with `redact_text` on anything free-form and truncated as S7 says; tickers from `symbols`. `amount` is the trade P&L for exits, the fill price for fills, None otherwise.
- `rejections` as S8.
- The route (`async def get_live`): reads `now` from `services.core.clock`, then runs one synchronous function in `anyio.to_thread.run_sync` that computes, in this order and each inside its own guard (S15): the live run (`live_run_id`), `session_info` (as `dashboard._stored` builds `SessionInfoOut`), `session_day`, `worker_out` (stale badge: `worker.age_seconds > HEARTBEAT_BADGE_SECONDS`), `approval_mode` and `trading_state`, `live_positions(expand)` (part `positions`), `period_blocks` (`periods`, using the positions' `open_values`; when the positions part failed, it runs with `open_values` = [] and the route sets `unrealized_partial` true on each block with `model_copy`), `claude_today`, `books_check` (`books`), `equity_series(range_, equity_at_marks)` (`equity`), `risk_panel` (`risk`), `activity_feed(session_day)` (`activity`), `rejections(session_day)` (`rejections`), the timeline (`timeline`: `build_timeline` for `current_session` with plan/fired/job runs, exactly as `dashboard._stored`), pending proposals (`pending`), and `closed_today` (trades of `session_day`). A part that raises is None plus a `PartErrorOut(part, "<ExceptionType>: <masked text, 120 chars>")`, logged once with `error_type`; the live run id or the session info failing is a real error (500), since nothing else can be computed.
- `expand` is split on commas into at most 3 ids (the query pattern already refuses more).
- Response header `Server-Timing: app;dur=<milliseconds, 1 dp>` measured with `time.perf_counter()` around the thread call, followed by one `<part>;dur=<ms>` entry per part computed (part names as in `part_errors`), so a slow part is visible live.
- The route never touches `services.quotes`, `services.candles` or any market-data service.

**Acceptance tests:**
- [ ] 1. (db) Activity: a seeded session day with an entry order, its fill (slippage 0.06), a stop exit trade (−12.50, −0.50 R), a proposal created/approved via telegram, one expired and auto-flattened, a kill-switch trip and reset, a failed premarket job run, an `error` event, a `log.worker` error event, 543 candidates with 12 passed → items of every kind in S7 except the `log.` event, newest first, texts as S7's examples (MT times), chips and tones as S7, links as S7; another day's rows and a replay run's rows absent; `limit` respected.
- [ ] 2. (db) Rejections: decision log rows for 800 scan rejections over 3 rules and 2 risk rejections → counts by (stage, rule), ≤ 50 tickers with `truncated`, links with `stage` and `outcome=rejected`; a summary-only scan row supplies scan counts without tickers; with no decision-log scan rows but candidates present → `source` `candidates`; none of either → `source` `none`, total 0.
- [ ] 3. Route shape: with every part function monkeypatched to return fixtures, `GET /api/live` returns them in `LiveOut`; `?range=run` reaches `equity_series`; `?expand=12,15` reaches `live_positions` as {12, 15}; `?expand=1,2,3,4` and `?expand=abc` → 422; `?range=week` → 422.
- [ ] 4. Part isolation: each part in turn raising `RuntimeError("boom <secret-looking token>")` → 200, that part `null`, one `part_errors` entry naming it with the type and masked text, every other part present; the live run lookup raising → 500 error shape `ErrorOut`.
- [ ] 5. No Questrade: with `ApiServices.quotes` and `candles` replaced by functions that fail the test when called, a seeded `/api/live` (both ranges, with `expand`) returns 200; `tests/live/test_no_questrade_static.py` covers `routers/live.py` and `activity.py`.
- [ ] 6. `Server-Timing` header present with a numeric `app;dur` first and one entry per part; the route runs its database work off the event loop (a part that sleeps 1 s in its thread does not block a concurrent `/api/health` request on the same app, which answers first).
- [ ] 7. Replay exclusion (db): a replay run with trades, fills, proposals, events, decision log rows and quote marks on the same day changes nothing in the response (compare with and without the replay rows).
- [ ] 8. Auth: 401 without a session (sweep) and no CSRF needed (GET).
- [ ] 9. Gate and commit `DB-T5: activity feed, rejections and GET /api/live`.

**LIVE steps:** none.

---

## DB-T6: Control API

**Goal:** Implement `GET /api/control` (design §4, §5.3): engine card, kill switches with history, strategies, today's schedule with job runs and re-run mapping, health (heartbeat, token, database, Telegram, Questrade stats, opening-bar fetch, marks), the soak summary and the latest 200 warning-or-higher events. D1, D6, D7.

**Files:** `Trader/app/trader/api/livedata/control.py`, `health.py`, `Trader/app/trader/api/routers/control.py`; tests `tests/live/test_control.py`, `tests/live/test_health.py`, `tests/api/test_control_route.py`.

**Interfaces (consume):** `dashboard.build_timeline`/`DAY_JOBS`/`RunRow`/`EVENT_LABELS`, `ApiServices.plan`/`fired`/`registry`/`killswitches`/`credentials`/`telegram_configured`; `api.views.token_out`, `worker_out`, `event_out`; `trader.api.routers.meta.db_check`, `tz_iana_version`; `trader.api.routers.system._undelivered`, `_alembic_revision`, `_rate_limit` (import, unchanged); `trader.api.routers.jobs.job_run_out`; `trader.api.launcher.CLI_ARGS`; `trader.jobs.soak.SoakDeps`, `load_report`, `readonly_plan`, `orb_enabled_reader`; `trader.notify.notifier.NullNotifier`; `trader.runtime.build_renderer` (imported late, as `system.telegram_test` does); DB-T4's `killswitch_lights` and `trading_state` (stubs until DB-T4 lands; this task's tests monkeypatch them); `KillSwitchEventOut` built as `routers/killswitch.py` builds its history (last 20 of the live run).

**Interfaces (provide):** the DB-T1 signatures of `control.py`, `health.py` and the route.

**Behaviour and decisions:**
- **Engine:** approval mode from settings; `trading` from `trading_state`; `paused_at` = the manual pause's `tripped_at`; run id, `runs.started_at` and its ET date; `version`/`app_env` from `core.env`; alembic revision via `system._alembic_revision`.
- **Kill switches:** `killswitch_lights` with `equity` = DB-T4's `live_positions(factory, run_id, now, ()).equity_at_marks` (no bars requested); history = the live run's last 20 `kill_switch_events`, newest first, as `KillSwitchEventOut` (the fields `routers/killswitch.py` returns).
- **Strategies:** for each `registry.keys()` with a current config: `kind` = `registry.plugin_class(key).kind`, enabled, revision, version, `created_at`/`created_by` of the config row, `owns_open_positions` (the same exists-query as `routers/strategies.py`), `max_positions` from params, `settings_link` = `/settings#strategies`.
- **Schedule:** `build_timeline` for `current_session` (today on a session day, else the next session) enriched per item from `job_runs` of that session: the latest row's `started_at`, `finished_at`, duration, `attempts` = rows of that job, `summary` = `job_summary(job, detail, opening_bars)`, `rerun` = the `ManualJob` of that day job (`nightly`, `premarket`, `preopen`, `postclose`; events and check-ins have none). `manual_jobs` = `list(CLI_ARGS)` (includes `token-refresh`, `weekly`).
- **`job_summary`:** `event:orb_open` with today's opening-bar batch → `"<completed> of <symbols> bars in <elapsed> s"` (plus `", <n> x 429"` when any); a detail with a string `skipped` → `"skipped: <text>"`; with `error` → the masked error, 80 chars; otherwise up to three top-level entries whose values are int, float, bool or a string ≤ 40 characters, in the detail's key order, as `"key value"` joined by `" · "`; None for an empty or non-dict detail. Pure.
- **Health:** worker heartbeat (`worker_out` with `worker_heartbeat_stale_seconds`) and `worker_stale` (> 60 s); token (`token_out(services.credentials.health, now)`); database (`db_check`, failure → `db_ok` false); Telegram configured; `questrade_stats(detail)` (S10 `questrade` plus the existing `rate_limit` via `system._rate_limit`; None when absent, or when its `day` is not today's ET date); `opening_bars(detail)` (the first `candle_batches` entry of today started in [09:35, 09:40) ET, `complete` = outstanding 0 and errors 0 and raised null; None otherwise); `marks` from the detail; failed notifications via `system._undelivered`; `tz_iana_version`. Unknown or malformed detail values are ignored, never an error.
- **Soak:** `load_report(SoakDeps(factory=core.factory, clock=core.clock, calendar=core.calendar, settings=load, plan=soak.readonly_plan(core.factory, core.clock, core.calendar, load), orb_enabled=soak.orb_enabled_reader(core.factory, core.clock), notifier=NullNotifier(), render=build_renderer(core), env=core.env.app_env))` where `load = lambda: trader.api.deps._settings(services)` (the same deps `cli.py` builds for `soak-report`, with a null notifier), with the defaults (20 sessions, target 10), read-only; `day_one` = the earliest session of the trailing run of `clean` days that `consecutive_clean` counts (None when 0); `today` = the report day whose date is today's ET date (None otherwise); cached in-process for `SOAK_CACHE_SECONDS` keyed by today's ET date (a module-level cache with a `threading.Lock`).
- **Errors:** the newest `ERRORS_SHOWN` `event_log` rows at `warning` or above, `live_or_unscoped`, including `log.*` sources (the log mirror copy), via `event_out` (masked). The page filters by level and source client-side and uses `GET /api/events` for older rows.
- Part isolation exactly as S15 (parts: `engine`, `killswitches`, `strategies`, `schedule`, `health`, `soak`, `errors`).
- No mutation here: every action on the page uses the existing routes (`PUT /api/settings/approval_mode`, `POST /api/killswitch/pause|resume|{switch}/reset`, `PUT /api/strategies/{key}`, `POST /api/jobs/{job}/run`, `POST /api/system/telegram-test`, `POST|DELETE /api/watchlist`), with their confirmations, typed reasons, CSRF and audit unchanged.

**Acceptance tests:**
- [ ] 1. `job_summary`: the orb_open batch text; skipped; error masked and truncated; generic scalars (three, key order, long strings skipped); None cases.
- [ ] 2. `questrade_stats`/`opening_bars`/marks parsing: a well-formed detail → the models; yesterday's `questrade.day` → None; a batch at 09:34:59 ET is not the opening fetch, one at 09:35:00 is; malformed values (strings for ints, NaN-like strings, lists) → None, never an exception.
- [ ] 3. (db) Schedule: a session day with premarket succeeded (2 attempts), orb_open failed once then succeeded, postclose upcoming → statuses as `build_timeline`, attempts, durations, summaries, `rerun` for day jobs only; on a Saturday the schedule is Monday's, all upcoming.
- [ ] 4. (db) Engine and strategies: manual pause active → `paused` with `paused_at`; strategies list both plug-ins with kind, revision, `owns_open_positions` true for the one holding a position.
- [ ] 5. (db) Soak: seeded job runs for 3 clean sessions after a reset mark → `consecutive_clean` 3, `day_one` the first of them, `today` present on a session day; a second call within 60 s does not query again (statement listener); `load_report` failing → part `soak` null with a `part_errors` entry.
- [ ] 6. (db) Errors: warning, error and critical rows (including `log.api`) listed newest first up to 200; `info` rows and replay-run rows absent; messages masked (a `password=...` text is redacted).
- [ ] 7. Route: part isolation as DB-T5 test 4 for each control part; no Questrade (services' `quotes`/`candles` fail the test if called); 401 without a session; the route is read-only (statement listener: SELECTs only, the soak report included).
- [ ] 8. Gate and commit `DB-T6: GET /api/control`.

**LIVE steps:** none.

---

## DB-T7: Web components A: top bar, periods, costs, books, equity chart, risk

**Goal:** Build the dashboard's first two sections (design §3 items 1–2) as small components on the DB-T1 types and tokens. D3, D4, D9.

**Files:** `Trader/web/src/pages/live/TopBar.tsx`, `PeriodPnl.tsx`, `CostBar.tsx`, `BooksCheck.tsx`, `EquityChart.tsx`, `RiskPanel.tsx`, `liveA.css`, and tests `TopBar.test.tsx`, `PeriodPnl.test.tsx`, `CostBar.test.tsx`, `BooksCheck.test.tsx`, `EquityChart.test.tsx`, `RiskPanel.test.tsx` in the same folder.

**Interfaces (provide; props are the contract DB-T11 composes):**
```ts
export function TopBar(props: { live: LiveOut; connected: boolean; updatedAt: number; nowMs?: number }): JSX.Element
export function PeriodPnl(props: { periods: PeriodPnlOut[] | null; error?: string | null; onRetry?: () => void }): JSX.Element
export function CostBar(props: { claude: ClaudeTodayOut | null; periods: PeriodPnlOut[] | null }): JSX.Element
export function BooksCheck(props: { books: BooksCheckOut | null; error?: string | null; onRetry?: () => void }): JSX.Element
export function EquityChart(props: { equity: EquitySeriesOut | null; range: LiveRange; onRange: (r: LiveRange) => void; error?: string | null; onRetry?: () => void }): JSX.Element
export function RiskPanel(props: { risk: RiskOut | null; error?: string | null; onRetry?: () => void }): JSX.Element
```

**Behaviour and decisions:**
- **TopBar** is the region labelled "Session" (the smoke test reads it): session date and phase label (`dashboard/labels.ts` `phaseLabel`), the three periods' trading P&L after fees with realised and open in small text, fees, Claude spend with `CostBar`, net after AI, win rate, trades, expectancy R (run block), the engine chip (`AUTO`/`MANUAL` and `RUNNING`/`PAUSED`/`BLOCKED`, status tokens), the live indicator (`Live` when connected and the data is at most 10 s old; `Degraded: polling every 15 s` when not connected; `Updated <n> s ago` otherwise), the heartbeat badge when `worker_stale`, and `BooksCheck` (✓/✗ with a tap-to-expand breakdown). Money values render through a `.money` span with `moneyTone` (the only place green/red appears); everything else uses status tokens. Phone: the periods stack; nothing scrolls sideways at 390 px.
- **PeriodPnl** shows today | week | since start as three columns (realised + open, trades, wins/losses, win rate; expectancy R for the run column), `open` marked "partial" when `unrealized_partial`, "–" for None.
- **CostBar**: today's Claude spend against the cap as a bar (`used_fraction`, capped visually at 100 %, with the exact numbers as text), amber at ≥ 80 %, and the fees and net after AI per period.
- **BooksCheck**: ✓ (status ok) or ✗ (status bad) with the text "cash + positions at cost = starting cash + realised − fees"; expanded: each component and the difference to the cent.
- **EquityChart** (Recharts `LineChart`, colours from `readToken`): the series with the start line as a reference line, fill markers as dots (buy/sell shapes differ), Today ⇄ Whole run toggle (two 44 px buttons calling `onRange`), a "downsampled" note when set, empty state "No equity data for this range".
- **RiskPanel**: four kill-switch lights with value vs threshold in their units (percent with 1 dp, R with 2 dp, count vs min), tripped ones first-class with `tripped_at` in MT and "Reset on Control" link to `/control`; open risk $ vs cap as a bar; slots used n / max; open positions count.
- Every component uses `Panel` for its frame, error and empty state; all text from the API renders as plain text.

**Acceptance tests (vitest with `liveFixtures`):**
- [x] 1. TopBar shows the three P&L numbers with the right money tone class (a negative today → `money down`), fees, Claude spend, net after AI, win rate, trades and expectancy; the engine chip text for auto/running, manual/paused and blocked.
- [x] 2. Live indicator: connected and fresh → "Live"; not connected → "Degraded: polling every 15 s"; connected but `updatedAt` 25 s old → "Updated 25 s ago"; `worker_stale` → heartbeat badge.
- [x] 3. BooksCheck ✓ and ✗; expanding ✗ shows the difference; a `null` books with an error shows the error and Retry (calls `onRetry`).
- [x] 4. PeriodPnl with `unrealized` null and partial flags; CostBar at 0, 50 %, 80 % (amber) and 130 % (bar capped, text exact) and with cap 0 (no bar, text only).
- [x] 5. EquityChart renders with 500 points, shows the start line and markers, toggles range via `onRange`, shows the empty state for an empty series.
- [x] 6. RiskPanel: values vs thresholds in units, a tripped switch with its link to `/control`, open risk vs cap, slots.
- [x] 7. D9 colour rule: green/red classes (`money up`/`money down`) appear only on money values (a test renders every A component with the full fixture and checks every element with a money class contains a formatted money value); status lights use only status classes.
- [x] 8. Touch targets ≥ 44 px for every button and link in these components; no horizontal overflow at 390 px (layout test as `touchTargets.test.tsx` does).
- [x] 9. XSS: `withXssText()` fixture renders no `img` or `script` element and shows the text literally.
- [x] 10. `npm --prefix Trader/web exec tsc -b --noEmit` clean for these files; gate and commit `DB-T7: live dashboard top bar, P&L, costs, books, equity and risk components`.

**LIVE steps:** none.

---

## DB-T8: Web components B: positions, activity, rejections, today

**Goal:** Build the dashboard's positions, activity, rejections and today sections (design §3 items 3–5). D2, D5.

**Files:** `Trader/web/src/pages/live/PositionsTable.tsx`, `PositionRow.tsx`, `Sparkline.tsx`, `PositionChart.tsx`, `ActivityFeed.tsx`, `RejectionsPanel.tsx`, `TodayTimeline.tsx`, `liveB.css`, and tests `PositionsTable.test.tsx`, `Sparkline.test.tsx`, `PositionChart.test.tsx`, `ActivityFeed.test.tsx`, `RejectionsPanel.test.tsx`, `TodayTimeline.test.tsx`.

**Interfaces (provide):**
```ts
export type PositionSort = "unrealized" | "stop";
export function PositionsTable(props: { positions: LivePositionOut[] | null; closedToday: number; staleAfterSeconds: number; expanded: number[]; onExpand: (ids: number[]) => void; error?: string | null; onRetry?: () => void }): JSX.Element
export function PositionRow(props: { p: LivePositionOut; expanded: boolean; onToggle: () => void }): JSX.Element
export function Sparkline(props: { points: SparkPointOut[]; entry: string; stop: string | null; width?: number; height?: number; label: string }): JSX.Element
export function PositionChart(props: { p: LivePositionOut }): JSX.Element
export type ActivityFilter = "all" | ActivityChip;
export function ActivityFeed(props: { items: ActivityItemOut[] | null; error?: string | null; onRetry?: () => void }): JSX.Element
export function RejectionsPanel(props: { rejections: RejectionsOut | null; error?: string | null; onRetry?: () => void }): JSX.Element
export function TodayTimeline(props: { timeline: TimelineItemOut[] | null; session: SessionInfoOut; error?: string | null; onRetry?: () => void }): JSX.Element
```

**Behaviour and decisions:**
- **PositionsTable**: compact rows (ticker, long, qty; entry → mark; stop / target; unrealised $ and R; time held; sparkline); sort buttons "Unrealised $" (default, descending) and "Distance to stop" (ascending `distance_to_stop_r`, missing last); rows with `near_stop` highlighted (status warn border, plus the text "near stop" for screen readers); a "prices stale" badge on the panel when any row is `stale`/`missing` and on each such row; tap a row → expands `PositionChart` (at most 3 expanded: a fourth tap collapses the oldest) and calls `onExpand` with the ids (DB-T11 puts them in `?expand=`); empty state "No open positions" and "<n> trades closed today". Designed for 0–20 rows; phone shows ticker, unrealised and sparkline, the rest on expand.
- **Sparkline**: inline SVG polyline (no library) of the points scaled to the box, entry as a dashed horizontal line, stop as a solid line in the status-bad token (a stop line is a status, not money), `role="img"` with `aria-label` "<ticker> since entry"; with fewer than 2 points a flat "no data" placeholder.
- **PositionChart**: Recharts line of `bars` closes (1-minute) with entry, stop and target reference lines and fill markers, "bars from quotes" note when any bar has `source` `marks`; "Chart data not available yet" when `bars` is null or empty; a link "Open trade" to `p.link`.
- **ActivityFeed**: filter chips All, Trades, Proposals, Alerts, Scan (44 px, `aria-pressed`), newest first, each item time (MT) + text + amount (money tone only for exits) + link; empty state "No activity yet today" (per chip: "Nothing in <chip> today").
- **RejectionsPanel**: title "Rejected today, and why", total, rules sorted by count (label: stage and rule), tap a rule → its tickers (links to the Day view per S8), "+n more" when truncated; source note ("from candidates, decision log not recorded yet" when `source` is `candidates`); empty "No rejections recorded today".
- **TodayTimeline**: the day's jobs and events with ✓ / ⏳ / ✗ / next and MT times (reuse `pages/dashboard/Timeline.tsx` for the list), "Market closed today; next session <date>" on a non-session day.
- Every component uses `Panel`; text renders as plain text.

**Acceptance tests:**
- [x] 1. PositionsTable with 0, 1 and 20 positions (fixtures): the empty state with the closed count; 20 rows with sparklines; default sort by unrealised descending; "Distance to stop" sort ascending with missing last; `near_stop` rows highlighted and announced.
- [x] 2. Expand: tapping rows calls `onExpand` with up to 3 ids, a fourth tap drops the oldest; the expanded row shows `PositionChart` with its bars, or "Chart data not available yet".
- [x] 3. Stale marks: a `stale` row and a `missing` row show the row badge and the panel badge; all `live` → no badge.
- [x] 4. Sparkline: 60 points render a polyline with 60 coordinates, the entry and stop lines, the aria label; 1 point → placeholder.
- [x] 5. ActivityFeed: chips filter by `chip` and keep order; empty states; exit amounts carry money tone, other items none; links rendered as router links.
- [x] 6. RejectionsPanel: rules and counts, tapping a rule reveals tickers with the S8 link format (`/reports?day=D&stage=scan&outcome=rejected&ticker=T`), truncated note, the `candidates` source note, the empty state.
- [x] 7. TodayTimeline for a session day and a closed day.
- [x] 8. Panel errors: each component with `null` data and an error shows the error and Retry without throwing.
- [x] 9. Touch targets ≥ 44 px; no horizontal overflow at 390 px with 20 positions; XSS fixture renders text literally.
- [x] 10. Types clean; gate and commit `DB-T8: live dashboard positions, activity, rejections and today components`.

**LIVE steps:** none.

---

## DB-T9: Web Control page

**Goal:** Build the Control page (design §4) from `GET /api/control`, reusing the existing action components so every action keeps its rules. D1, D6.

**Files:** `Trader/web/src/pages/Control.tsx`, `Trader/web/src/pages/control/EngineCard.tsx`, `KillSwitchCard.tsx`, `StrategiesCard.tsx`, `JobsCard.tsx`, `HealthCard.tsx`, `SoakCard.tsx`, `ErrorLog.tsx`, `control.css`, and tests `pages/control/ControlPage.test.tsx`, `EngineCard.test.tsx`, `KillSwitchCard.test.tsx`, `JobsCard.test.tsx`, `HealthCard.test.tsx`, `SoakCard.test.tsx`, `ErrorLog.test.tsx`.

**Interfaces (provide):** `export default function ControlPage(): JSX.Element` (heading level 1 "Control"; query `qk.control()` → `api.control()`), and the cards (each also takes `error?: string | null; onRetry?: () => void`):
```ts
export function EngineCard(props: { engine: EngineOut | null }): JSX.Element
export function KillSwitchCard(props: { lights: KillSwitchLightOut[] | null; history: KillSwitchEventOut[] | null }): JSX.Element
export function StrategiesCard(props: { strategies: StrategyCardOut[] | null }): JSX.Element
export function JobsCard(props: { schedule: ScheduleItemOut[] | null; manualJobs: ManualJob[]; session: SessionInfoOut }): JSX.Element
export function HealthCard(props: { health: HealthPanelOut | null }): JSX.Element
export function SoakCard(props: { soak: SoakSummaryOut | null }): JSX.Element
export function ErrorLog(props: { errors: EventOut[] | null }): JSX.Element
```

**Interfaces (consume, unchanged):** `pages/settings/ApprovalMode.tsx` (`ApprovalMode`, its confirm flow), `pages/settings/KillSwitchPanel.tsx` (`KillSwitchPanel`: typed reason, reset, audit), `pages/settings/Confirm.tsx`, `pages/settings/TelegramTest.tsx`, `pages/system/RunJob.tsx`, `pages/system/StatusCards.tsx` (`FailedSends`, `RateLimits`, `utcOffsetLabel`), `pages/system/EventLog.tsx` (`EventLog` for older rows), `pages/system/WatchlistUpload.tsx`, `api.pause()`/`api.resume()`.

**Behaviour and decisions:**
- Sections in design order: **Engine** (approval mode via `ApprovalMode`; trading state; Pause / Resume buttons with the existing `Confirm` dialog calling `api.pause()`/`api.resume()` and invalidating `["dashboard"]`, `["system"]`, `["killswitches"]`; live run id and start date; deployed version and alembic revision); **Kill switches** (lights with value vs threshold and last trip, then `KillSwitchPanel` for resets and history); **Strategies** (each on/off with revision, "owns open positions" note, link to `/settings#strategies`; the on/off toggle uses `api.putStrategy(key, {enabled})` with a `Confirm` dialog, exactly the call `StrategyForms` makes); **Today's schedule and jobs** (the schedule list with MT times, status, duration, attempts, summary, and a Re-run button for items with `rerun`, which opens `RunJob` preselected; below it `RunJob` for every manual job); **Health** (worker heartbeat age with the stale badge, token, database latency, Telegram with `TelegramTest`, Questrade today: requests, 429s, pause seconds, rate limits; opening-bar fetch: "543 of 543 bars in 31.2 s" and complete/incomplete, or "not fetched by the worker today"; marks: last write and failing flag; time-zone check via `utcOffsetLabel` and `tz_iana_version`; `FailedSends`); **Soak** (n / target clean, day 1, earliest finish, today's verdict so far, last final day); **Error log** (the 200 rows with level and source filters, plain text, "Older events" opening `EventLog`); **Manual watchlist** (`WatchlistUpload`, carried over from System).
- Each card uses `Panel`: a `null` part with a `part_errors` entry shows that card's error and Retry; the page never blanks for one part.
- Mutations invalidate `["dashboard"]` and `["system"]` (the existing components already do; new ones in this task do too).

**Acceptance tests:**
- [ ] 1. ControlPage renders every section from `controlOut` with heading "Control".
- [ ] 2. Pause: tapping Pause opens the confirm dialog; confirming calls `api.pause()` once and refetches; cancelling calls nothing; Resume likewise when paused.
- [ ] 3. Approval mode and kill-switch reset go through the reused components (the fake records `putSetting("approval_mode", ...)` after confirm, `resetKillSwitch(switch, {reason})` only with a valid typed reason).
- [ ] 4. Strategy toggle calls `putStrategy(key, {enabled: false})` only after confirm.
- [ ] 5. Jobs: Re-run on the premarket item opens `RunJob` for `premarket`; items without `rerun` have no button; durations and summaries shown; MT times.
- [ ] 6. Health: stale worker badge (`controlStaleWorker`), opening-bar text complete and incomplete, "not fetched by the worker today" when null, Questrade counts, marks failing flag.
- [ ] 7. Soak card with a summary and with `soak` null plus a part error (error and Retry, the rest renders).
- [ ] 8. Error log: filters by level and source; XSS text literal; empty state "No warnings or errors".
- [ ] 9. Carried over from `SystemPage.test.tsx` (each still applies): the worker-down text `WORKER_DOWN_TEXT` when the worker is not ok, `TELEGRAM_OFF_TEXT` when Telegram is not configured, failed sends listed, the watchlist upload present.
- [ ] 10. Touch targets ≥ 44 px; no horizontal overflow at 390 px; gate and commit `DB-T9: Control page`.

**LIVE steps:** none.

---

## DB-T10: Backend wiring and the D2 proofs

**Goal:** Wire the tap and the publisher into the worker's composition root, merge their health into the heartbeat (S10), and prove the deploy is logging only: a simulated worker day identical with the publisher on and off (trading rows, Telegram chat and the Questrade call log), plus the static import rules. D8, D10, design §8.

**Files:** `Trader/app/trader/runtime.py`; tests `Trader/app/tests/integration/test_marks_live_unchanged.py` (new), `tests/test_runtime_marks.py` (new), `tests/live/test_d2_static.py` (new).

**Interfaces (provide):**
- `trader/runtime.py`: `def live_marks(core: Core, client: QuoteClient, run_id: Callable[[], int | None]) -> tuple[QuoteClient, MarkPublisher | None]` (returns `(QuoteTap(client, core.clock), MarkPublisher(MarkPublisherDeps(core.factory, core.clock, tap, run_id, marks_event_writer(core))))`); `def marks_event_writer(core: Core) -> EventWriter` (one `event_log` row via `_record_event`, source `marks`; never raises).
- `_run_worker`: `tapped, publisher = live_marks(core, client, lambda: run_id)`; `SessionEngines(core, tapped, watch=watch)`; the bot's `MarketDataService(factory, clock, core.calendar, tapped)`; `heartbeat_extra()` returns `{"rate_limit": ...}` as today plus `tapped.health_detail()` when `tapped` is a `QuoteTap` and `{"marks": publisher.health_detail()}` when there is a publisher (each merged under a guard: a failing health call leaves its keys out and the others in); `WorkerDeps(..., marks=publisher)`; `stack.callback(publisher.close)` when there is a publisher (its executor is shut down with the worker's stack). `client` (the `LazyQuestrade`) keeps serving `rate_limit_remaining()`. `heartbeat_extra` stays synchronous and does no I/O (it runs on the loop inside `Worker._beat`).
- No other process changes: `preopen_job`, `checkin_job`, `event_backup`, `postclose_job`, `trader premarket` and the API keep their own clients without a tap (they are short-lived; the worker is where the session's quotes are).

**Behaviour and decisions:**
- "Off" for the proof is `live_marks` monkeypatched to `lambda core, client, run_id: (client, None)` (no tap, no publisher): exactly the trunk composition.
- "On" drives the worker's own publisher (`worker.deps.marks.run_once()`) after every worker step, as P6-T11 test 4 drives the decisions loop. The engines and the bot really run through the `QuoteTap` in "on" (the composition under test), so the 9:35 `orb_open` opening-bar batch, the 2 s quote polls and `_account` all pass through it.

**Acceptance tests:**
- [ ] 1. **(integration) A worker day is identical with the marks on and off:** the P3-T13 manual worker day (`tests/integration/test_worker_day.py` composition, via `test_decisions_live_unchanged.build_world`-style setup in two fresh databases, `fresh_factory` fixture) run with the marks on and off gives `trading_rows(on) == trading_rows(off)` (`tests/integration/test_decisions_day.trading_rows`: candidates, signals, proposals, orders, fills, trades, cash ledger, equity snapshots), `chat_on == chat_off` (every Telegram message, unfiltered), and `w_on.fq.calls == w_off.fq.calls` (the fake Questrade's call log: no extra request of any kind).
- [ ] 2. In the "on" database `quote_marks` has a row for the traded symbol with the day's last observed quote and `mark_bars` has bars for the minutes it was held; the "off" database has neither; no `marks` warning event in either.
- [ ] 3. With the publisher's database step failing throughout the "on" day (a monkeypatched write that raises), the trading rows, chat and Questrade calls still equal the "off" day, and exactly one `marks` warning event exists.
- [ ] 4. Wiring (`tests/test_runtime_marks.py`, like the existing `run_worker` wiring tests): `run_worker` passes a `MarkPublisher` in `WorkerDeps.marks`, the engines and the bot's market data get the `QuoteTap`, the heartbeat `detail` contains `rate_limit`, `questrade`, `candle_batches` (after a `candles_many`) and `marks`, and a `health_detail` that raises leaves the other keys and still writes the beat; the publisher's `close` runs when the worker exits; the heartbeat's `rate_limit` still comes from the `LazyQuestrade`.
- [ ] 5. Static (`tests/live/test_d2_static.py`): no module under the decision path (the `DECISION_PATH` list of `tests/decisions/test_static.py`) imports `trader.marks` or `trader.api.livedata` (AST); no module outside `trader/runtime.py`, `trader/worker.py` and `trader/marks/` imports `trader.marks`; **allow-list:** across all of `trader/`, the names `QuoteMark`, `MarkBar`, `quote_marks` and `mark_bars` (AST names and attribute access, plus a text search for the table names in SQL strings) appear only in `trader/db/models.py`, `trader/db/migrations/versions/0008_*.py`, `trader/marks/publisher.py`, `trader/api/feed.py`, `trader/api/livedata/positions.py` and `trader/api/livedata/equity.py`, so `strategies/`, `engine/`, `broker/`, `market/`, `jobs/` (including `postclose.py`), `reports/`, `replay/`, `decisions/` and `notify/` can never read a quote-built bar; and `positions.py`/`equity.py` never construct `trader.market.types.Candle` (AST).
- [ ] 6. Gate and commit `DB-T10: wire the mark publisher; D2 proofs`.

**LIVE steps:** none (DB-T12).

---

## DB-T11: Web wiring

**Goal:** Compose the new Dashboard page, route and navigate to Control, throttle live updates, move the engine controls off Settings, teach the Reports Day view the rejection links, and migrate every existing web test and the smoke spec (S9, S12, S13). D1, D6, D9.

**Files:** see the file map (DB-T11 row), including the deletions.

**Interfaces (provide):**
- `pages/Dashboard.tsx` (rewritten): `export default function DashboardPage(): JSX.Element`: heading level 1 "Dashboard"; query `qk.live({range, expand})` → `api.live(...)`; `range` and `expand` kept in the URL (`?range=run`, `?expand=12,15`) next to `?proposal=`; layout (tablet: two columns for sections 2 and 4; phone: the order of design §3): `TopBar`; `EquityChart` | `RiskPanel`; `PositionsTable`; `ActivityFeed` | `RejectionsPanel`; `TodayTimeline` and, when `approval_mode` is manual or any proposal is pending, "Pending approvals" with `PendingProposal` (existing one-tap flow; `?proposal=<id>` highlights it, or shows `ProposalPanel` when it is not pending) and the decision notices of the old page.
- `App.tsx`: routes add `/control` → `ControlPage`; `/system` → `<Navigate to={"/control" + search} replace />`; others unchanged.
- `layout/Layout.tsx`: `NAV_ITEMS` = Dashboard, Control, Reports, Replay, Settings; `MORE_ITEMS` = Trades, Candidates, Performance, Journal (side list shows them under a "More" heading); phone tabs Dashboard, Control, Reports, More (menu: Replay, Settings, Trades, Candidates, Performance, Journal); a theme control in the header (Auto / Dark / Light, stored under `THEME_STORAGE_KEY`, applied as `data-theme` on `<html>`, default dark).
- `live/useLiveUpdates.ts`: `export const LIVE_THROTTLE_MS = 2000`; invalidations of the `dashboard` and `system` prefixes throttled (leading plus one trailing per window); `setDashboardPolling` generalised to both prefixes (15 s while disconnected, cleared on reconnect; losing the stream refetches both once).
- `main.tsx` imports `theme/tokens.css` before `styles.css`; `styles.css` drops its own `:root` and dark-mode variable blocks (now in `tokens.css`) and keeps everything else.
- `pages/Settings.tsx`: the "Approval mode" and "Kill switches" cards are replaced by one card "Engine controls moved" with a link to `/control`; the other cards unchanged.
- `pages/reports/DayDecisions.tsx`: initial filters from the URL's `stage`, `outcome` and `ticker` when valid (`DecisionStage`/`DecisionOutcome` values, ticker `^[A-Z.]{1,10}$`), else none; changing a filter updates the URL.
- `web/tests/smoke.spec.ts`: local mode approves the seeded proposal on the new Dashboard ("Pending approvals", the `Proposal <id>: ENTRY AAA` article), opens `/control` (heading "Control") instead of `/system`, and checks `/system` redirects to `/control`; live mode visits Dashboard (region "Session" with a phase), Control, Candidates, Trades, Performance, Journal, Settings, Reports (this week) and Replay, requests `/api/live` once to warm up and then 5 times (through the page's authenticated request context), reads each response's `Server-Timing` `app;dur`, fails when their median is 300 ms or more, and logs the five values and the median (never credentials or headers other than `Server-Timing`); logout checks with `/control`.

**Behaviour and decisions:**
- Test migration per S13: every assertion of the deleted `DashboardPage.test.tsx` that still applies moves to `pages/live/LivePage.test.tsx` (pending proposals and approve/reject flow, decision notices, `?proposal=` highlight and `ProposalPanel`, Telegram-not-configured notice, error box with Retry, empty states); `web_pages_breaker.test.tsx` cases that rendered `DashboardPage`/`SystemPage`/`PnlTiles`/`PositionCard` now render the new Dashboard/Control with the `withXssText()` fixtures and the new empty-state texts; the nav-order assertions change to the S12 order; the "dashboard polls every 15 s while disconnected" breaker case keeps its meaning with the `live` query and gains the same for `control`.
- No component of DB-T7/DB-T8/DB-T9 is edited here (a needed change is reported).

**Acceptance tests:**
- [ ] 1. `LivePage.test.tsx`: the page composes every section from `liveOut`; 0/1/20-position fixtures; the non-session empty day; `range` and `expand` round-trip through the URL and reach `api.live` (fake call args); pending approvals shown in manual mode and hidden in auto mode with none pending, shown in auto mode when one is pending; approve flow as before.
- [ ] 2. One failing part (`liveWith` a null part plus `part_errors`) shows that panel's error with Retry and every other panel; `liveAllPartsFailed` shows every panel's error and the page frame; a failing `api.live` shows the error box with Retry and keeps the last data.
- [ ] 3. Throttle: 10 `invalidate` messages for `marks` within 1 s cause one immediate refetch and one trailing refetch at 2 s (fake timers); a message after the window refetches again; messages for other prefixes (`candidates`) are not throttled.
- [ ] 4. The trailing refetch is never dropped: a burst ending at 1.9 s still produces the trailing refetch at 2 s.
- [ ] 5. Polling fallback: while disconnected both `live` and `control` queries refetch every 15 s; `hello` stops it; losing the stream refetches both once.
- [ ] 6. The top bar shows "Degraded: polling every 15 s" while disconnected (provider state reaches it).
- [ ] 7. Navigation: side list and phone tabs in the S12 order; the More menu lists the S12 items; `/system?x=1` lands on `/control?x=1`; `/dashboard?proposal=12` still survives the login redirect (`shell.test.tsx` updated); the theme control switches `data-theme` and persists it (storage mocked).
- [ ] 8. Settings no longer renders the approval-mode and kill-switch cards and links to `/control`; `SettingsPage.test.tsx` updated; the Day view opens with `stage=scan&outcome=rejected&ticker=AAPL` filters applied and ignores invalid values.
- [ ] 9. `touchTargets.test.tsx` and `render.test.tsx` cover Dashboard and Control (every page in the list renders, ≥ 44 px targets, no overflow at 390 px); the old page files are deleted and nothing imports them (`tsc -b` clean).
- [ ] 10. `npm --prefix Trader/web run build` succeeds; gate and commit `DB-T11: live dashboard page, Control route and nav, live-update throttle`.

**LIVE steps:** none (DB-T12 runs the smoke live).

---

## DB-T12: End to end, performance budget, D2 deploy diff, docs and LIVE

**Goal:** Prove the whole feature on a seeded normal day against the real routes and database, measure the performance budget, fix the D2 classification objectively, update the docs, and deploy to trader-dev after the close.

**Files:** `Trader/app/tests/integration/test_live_api_day.py` (new), `Trader/app/tests/live/test_d2_deploy_diff.py` (new), `Trader/docs/SPEC.md`, `Trader/docs/plans/2026-09-26-build-master-plan.md` (§7.1), this plan (checkboxes, notes).

**Behaviour and decisions:**
- The seeded "normal day" (one testcontainer database): a live run started 40 sessions ago with 60 closed trades, 1 prior-close equity snapshot per session plus fill snapshots (≈ 180 rows), 20 open positions (the D2 maximum) with `quote_marks` and 120 minutes of `mark_bars` each, 543 candidates with 12 passed, 1,000 `decision_log` rows for today, 2,000 `event_log` rows, 30 `job_runs` for today, 5 kill-switch events, and a replay run with its own rows on the same day.
- **The D2 base is computed, never hard-coded or edited at deploy time.** `test_d2_deploy_diff.py` takes `DEPLOYED` from the environment variable `TRADER_D2_BASE` when set (LIVE step 1 sets it to the commit trader-dev is running, parsed from `/api/meta`), else from git: the parent of the oldest commit on `HEAD`'s first-parent history whose subject starts with `DB-T1:` (`git log --first-parent --format=%H --grep '^DB-T1:' HEAD`, last line, then `^`), so the gate proves the dashboard commits alone touch nothing on the decision path. `BUILT` = `HEAD`. It asserts `DEPLOYED` is an ancestor of `BUILT` (`git merge-base --is-ancestor`); when it is not, the test fails with that reason (a deploy from an unrelated commit must be looked at by hand). It skips (with the reason) only when git is unavailable or, without `TRADER_D2_BASE`, no `DB-T1:` commit is found; with `TRADER_D2_BASE` set it never skips (a bad value fails). No commit sha appears as a literal in the test.

**Acceptance tests:**
- [ ] 1. **Performance (db):** after 2 warm-up requests, the median of 10 `GET /api/live` requests on the seeded day, measured as the route's own `Server-Timing` `app;dur` (the thread's work, not the test client's overhead), is under 300 ms; every request runs at most `LIVE_MAX_STATEMENTS` SQL statements (listener), and the count is identical on the seeded day trimmed to 1 open position and 10 activity items (no per-row query); `?range=run&expand=<3 ids>` also under 300 ms median; `GET /api/control` (soak cache warm) median under 1 s. On failure the per-part `Server-Timing` entries are printed. The measured numbers (median, statement count) are printed in the test output and copied into the task notes.
- [ ] 2. **No Questrade (db):** both routes, all parameter variants, with `ApiServices.quotes`/`candles` failing the test if called → 200.
- [ ] 3. **Numbers end to end (db):** the seeded day's `/api/live` periods equal hand-computed values (today, week, run), books ✓, the replay run's rows change nothing (response equal with and without them), `positions` has 20 rows, `activity` 100 items, rejections by rule.
- [ ] 4. **D2 deploy diff (`test_d2_deploy_diff.py`):** the base is resolved as the Behaviour bullet says (env, else the parent of the first `DB-T1:` commit; a unit case monkeypatches `TRADER_D2_BASE` to a non-ancestor and to garbage → fails, never skips); from `DEPLOYED` to `BUILT`: `git diff --name-status` over the decision-path list is empty; no line removed or modified in `tests/engine`, `tests/strategies`, `tests/broker`, `tests/market`, `tests/jobs`, `tests/integration` (only added files); `Trader/app/tests/replay/golden/` unchanged; no `docker/crontab` job line changed; `trader/settings_store.py` unchanged.
- [ ] 5. **Golden replay and the D2 behavioural tests pass in the gate:** `tests/replay/test_golden.py`, `tests/integration/test_marks_live_unchanged.py`, `tests/integration/test_decisions_live_unchanged.py`, `tests/live/test_d2_static.py`, `tests/decisions/test_static.py`.
- [ ] 6. **Docs:** SPEC §10 (`quote_marks`, `mark_bars`), §11 (`GET /api/live`, `GET /api/control`, topics `marks`/`activity`), §12 (Dashboard and Control pages, nav, `/system` redirect); master plan §7.1: new row "Live dashboard" (the tables, the tap and publisher and their guarantees, `WorkerDeps.marks`, the heartbeat keys, the two routes and their models, the topics, the theme tokens file) and the refinements listed under "§7.1 contract refinements" below, in the rows they change (Worker process, Web API, Web API types, Web links).
- [ ] 7. Gate and commit `DB-T12: live dashboard end to end, budget, D2 diff, docs`.

**LIVE steps** (dev; the orchestrator or this task's builder when told to deploy; **only outside 09:15–16:30 ET on a session day**, i.e. after 14:30 MT; never during market hours):
1. **D2 classification before deploying:** read `/api/meta` on trader-dev (`curl -s https://trader-dev.sunspinner.ca/api/meta`, no auth needed); its version is a `git describe` string (`...-g<sha>`, e.g. `phase-5-complete-24-g459e172` today): take the hex after the last `-g` (stop and report if there is none or it ends in `-dirty`). From a clean worktree at the trunk commit about to be deployed, run `TRADER_D2_BASE=<that sha> uv --directory <worktree>/Trader/app run pytest tests/live/test_d2_deploy_diff.py tests/live/test_d2_static.py tests/integration/test_marks_live_unchanged.py tests/replay/test_golden.py -q` → all pass, none skipped (nothing is edited or committed for this step, so the deployed commit is exactly the tested one). Record "not a trading change (live dashboard: logging and web only; D2 rules 1–3 checked)" in the activity log. If any check fails, it is a trading change: stop, tell Stephen why, and if deployed anyway mark `trader soak-mark reset --date <first session on the new code> --reason "<commit>: live dashboard, rule <n>"`.
2. **Deploy** from a clean worktree at the pushed trunk commit: `TRADER_ENV_FILE="/Users/stephen/Documents/Code/Claude Code/Trader/Trader/docker/.env.dev" bash Trader/docker/deploy.sh dev` → prints `deploy: down from <UTC>` and `deploy: up at <UTC>`, health 200; the entrypoint migrates to 0008.
3. **Catch-up (do not wait for a cron gap):** `uv --directory Trader/app run python ../build/cron_gap.py --from <down> --to <up> --container trader-dev 2>&1` (stdout: lines to run; stderr: "may have been interrupted"); run every listed command exactly as printed; for each interrupted candidate check its `job_runs` row (the Control page or `GET /api/jobs`) started before the down stamp and re-run only a `running`/`failed` one; confirm each re-run's row `succeeded`.
4. **Checks:** `/api/meta` shows the new commit; alembic revision `0008` (Control page Engine card, or `GET /api/system` `alembic_revision`); the worker heartbeat is fresh and its detail has `marks` (and `questrade` once the client opened); no `marks` warning events.
5. **Playwright live smoke:** `SMOKE_MODE=live SMOKE_BASE_URL=https://trader-dev.sunspinner.ca uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev npm --prefix ../web run e2e -- tests/smoke.spec.ts` (run from the main checkout's paths as in P5-T18; credentials only from the environment, nothing printed) → passes: Dashboard and Control render without an error box, no sideways scroll at 390 px, `/api/live` `Server-Timing` `app;dur` median of 5 under 300 ms (values recorded). Run it after 16:30 ET only (like every LIVE step here): it adds API load, never worker load.
6. **Next session (non-blocking, after 09:40 ET):** during the session `quote_marks.written_at` advances every ~2 s while a position or working order exists (`/api/live` positions show `mark_state` `live`), the Control page shows today's Questrade counts and the opening-bar fetch ("n of n bars in s"); after the close the soak ledger row for that day carries "deploy <commit>: not a trading change (live dashboard)".
7. Record the deploy (downtime window, catch-up commands and results, D2 class, measured budget) in `BUILD_STATE.md` and send the Telegram progress update.

---

## §7.1 contract refinements

Additive changes to the master plan's cross-phase contracts (DB-T12 writes them into §7.1):

1. **Worker process:** `WorkerDeps.marks: MarkPublisher | None = None` (last field): one more supervised task beside the decisions loop, cancelled on stop without grace, never started by `--once`. The heartbeat `detail` gains `questrade`, `candle_batches` and `marks` (S10); `rate_limit`, `fills_today`, `last_event` unchanged.
2. **Quote client at the worker's composition root:** the worker's engines and its bot's market data get `QuoteTap(LazyQuestrade)`; the tap satisfies `QuoteClient`, returns the wrapped client's results and exceptions unchanged, and is synchronous bookkeeping only (S1a: one direct `await` per method, no task, lock, timeout or I/O, `deadline_s` forwarded untouched, bounded memory; `stats` forwards the wrapped client's `stats` on every access). `MarketDataService`, `Engine` and every other process are unchanged. The publisher's database work runs in its own single-thread executor, never the default one.
3. **Web API:** `GET /api/live?range=today|run&expand=<≤3 ids>` → `LiveOut` (with `Server-Timing: app;dur=`) and `GET /api/control` → `ControlOut`; both read-only, `live_or_unscoped`/live-run filtered, never Questrade, each part isolated (`part_errors`). `Topic` gains `marks` and `activity`; `ROUTERS` gains `live.router` and `control.router` (20 routers). `/api/dashboard` and `/api/system` stay (unused by the web app).
4. **Web API types:** the new models and literals mirrored in `web/src/api/types.ts`; `ApiClient.live(q: LiveQuery)` and `ApiClient.control()`; query keys `qk.live` under the `dashboard` prefix and `qk.control` under `system`.
5. **Web links:** `/control` added; `/system` redirects to `/control` (the link Telegram sends keeps working); `/reports?day=<D>` accepts `stage`, `outcome`, `ticker` filters.
6. **Tables:** `quote_marks` and `mark_bars` (migration 0008), run-scoped, written only by the worker's mark publisher, read only by the API; a replay never writes them.

## D2 (soak) classification and its proof

**Classification: not a trading change.** Under Phase 6 D2 a deploy is a trading change only when its diff (1) changes a default or range of a trading setting or a strategy's default params, (2) changes an existing crontab line of a trading job or the events a strategy schedules, or (3) modifies code on the live decision path such that an existing assertion in `tests/engine|strategies|broker|market|jobs|integration` had to change. This build:
1. adds no setting and changes none (`settings_store.py` is untouched; every threshold is a module constant);
2. changes no crontab line and no strategy schedule;
3. modifies no file on the decision path at all, and changes no existing assertion in those test folders (it only adds test files).

What it adds is logging (a worker task that writes two new tables from quotes the worker already received), API reads, web pages and health numbers in the heartbeat, which D2 lists as not trading changes ("logging, reports, web, API reads"). The one piece that sits beside the trading path, the `QuoteTap` wrapper at the composition root, is proven transparent rather than assumed.

**Proof (all in the gate, re-run at LIVE step 1):**
- Behavioural: DB-T10 test 1: the full simulated worker day gives identical trading rows (candidates, signals, proposals, orders, fills, trades, cash ledger, equity snapshots), an identical Telegram chat and an identical Questrade call log with the publisher on and off; DB-T10 test 3: the same with the publisher failing throughout; DB-T2 tests 1–5, 13–15: tap transparency (identity of results, exceptions and cancellations; no yield of its own; `opening_bars` and its 9:35 deadline guard unchanged through the tap; bounded memory; no await, lock, task or I/O in `tap.py`); DB-T2 test 11: the publisher never blocks the event loop nor the default executor.
- Static: DB-T10 test 5 and DB-T2 test 13 (import direction, write targets, the `mark_bars`/`quote_marks` reader allow-list, the tap's no-await/no-lock/no-I/O shape); DB-T12 test 4 (`git diff` from the commit trader-dev runs, read from `/api/meta` at deploy time into `TRADER_D2_BASE`: no decision-path file, no rule-3 assertion, no golden file, no crontab job line, no settings change).
- Golden: `tests/replay/test_golden.py` passes and `Trader/app/tests/replay/golden/` is unchanged (DB-T12 tests 4–5); a replay never builds a tap or a publisher (only `runtime._run_worker` does).

If any of these fails at deploy time, the deploy is classified a trading change and the soak count resets (`soak-mark reset` for the first session on the new code), and Stephen is told why before deploying.

## Open questions for Stephen (each has a default; the build proceeds on the default)

1. **Theme default.** The design asks for dark slate with the light theme kept. Default: dark slate for the whole app, with a header control (Auto / Dark / Light) remembered per browser.
2. **Navigation.** Default: primary Dashboard, Control, Reports, Replay, Settings; Trades, Candidates, Performance and Journal under "More" (their Telegram links keep working).
3. **Old routes.** `GET /api/dashboard` and `GET /api/system` are no longer used by the web app. Default: keep them in this build (no risk, tests unchanged); remove in a later cleanup.
4. **"Today" on weekends and holidays.** Default: show the last session (e.g. Friday on Saturday), labelled with its date; the week is the one just ended.
5. **Overnight positions.** Open P&L of a position held overnight counts fully in "today" (the strategies flatten daily, so this should not happen). Default: accept; revisit if a multi-day strategy is added.
6. **Engine controls on Settings.** Default: approval mode and kill switches move to Control and leave Settings (Settings shows a link); the Telegram test appears on both pages.
7. **Sparklines and intraday equity from worker quotes.** 1-minute candles are only stored after the close, so live sparklines and the intraday equity line use 1-minute bars built from the quotes the worker already polls (about one every 2 s per held symbol), replaced by real candles where stored. Default: yes, with a small "from quotes" note on the expanded chart.
8. **Target column.** `orb_sip` has no profit target. Default: show "–" (the column appears when a strategy records a target).
9. **System page contents.** Default: everything from System moves to Control (including the manual watchlist upload and failed Telegram sends), and `/system` redirects there.

## Plan verification (DB-T0 verifier + spec reviewer, attempt 1, 2026-09-28)

Checked against trunk `e734f58` (code as of `cb50e99`). Changes made in this pass:
- **Quote tap soak safety (S1a, new):** no safer source exists on trunk (no in-memory quote cache in `Engine`, `SimBroker` or `MarketDataService`), so the tap stays, now specified as synchronous bookkeeping only: one direct `await` per method, no task, lock, timeout, sleep, thread or I/O, bare re-raise, `deadline_s` and `reqs` forwarded untouched, no reference kept to results, `stats` forwarded live, memory capped at 30 × 1000 references. New DB-T2 tests 14 (no yield of its own) and 15 (the 9:35 `_fetch_opening_bars` guard fires identically through the tap); tests 2–5, 11 and 13 tightened.
- **Heartbeat Questrade counts:** the baseline was taken at the first heartbeat, which would drop the 429s of a batch that opened the `LazyQuestrade` client (often the 9:35 batch itself); now zero until the first ET date change, then taken before the first call of the new day (S10).
- **Publisher isolation:** its database work moves from `asyncio.to_thread` to its own single-thread executor (the Questrade client's token fetch uses the default executor), with `statement_timeout`/`lock_timeout` and plain SELECTs; `MarkPublisher.close()` added and wired.
- **Quote-built bars never become candles:** an allow-list of the only modules that may reference `mark_bars`/`quote_marks` (DB-T10 test 5), and no `Candle` built from a mark bar.
- **D2 deploy diff:** the base is read at deploy time from `/api/meta` into `TRADER_D2_BASE` (the gate uses the parent of the first `DB-T1:` commit), never hard-coded or edited into the test before deploying; must be an ancestor of `HEAD`.
- **Performance budget:** the fixed 45-statement ceiling (likely too low with `services.plan` in the timeline) became `LIVE_MAX_STATEMENTS` = 80 plus a statement count that must not grow with rows (N+1 guard); timing from the route's own `Server-Timing` with per-part entries; the live smoke takes the median of 5 after a warm-up.
- **Real names:** `proposals` has no `approval_mode` column (S7 now uses `decided_via`); `candle_archive` is keyed by `start_ts`; `redact_text` lives in `trader.logging_setup`; `tests/test_phase5_contracts.py` also pins `len(ROUTERS) == 18` (added to DB-T1); `tests/live/__init__.py` moved to DB-T1 (its test 5 needs the package before DB-T3 exists). No-Questrade static test widened to `LazyQuestrade`/`QuestradeClient`/`httpx`/`trader.market.data_service`.
