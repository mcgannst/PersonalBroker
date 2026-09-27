# Phase 5: Replay, Reports and Hardening (specification plan)

> **For agentic workers:** Run under the gauntlet process in [`2026-09-26-build-master-plan.md`](2026-09-26-build-master-plan.md) (§4–§6). Read its **Global Constraints** and **Review Focus** first, then this plan's Global Constraints and Review Focus below. This is a **specification** plan (master plan §6.6): it says WHAT to build and HOW TO KNOW it works. Builders design and write the code, matching the style of the existing code on trunk, and write each numbered acceptance test first (TDD). Each task has one checkbox per acceptance test plus one for the gate and commit: tick them in this file as you go and commit the file with your code.

**Goal:** Replay the real strategy code over past sessions from 1-minute candles (deterministic, isolated from the live run, started from the CLI or a new Replay page and compared with live); one metrics module behind the existing Performance and Reports pages; a Saturday weekly report with a Claude commentary that may only quote numbers it was given; a Telegram confirmation when a kill switch is reset; and the reliability work the BRD asks for (job retries with one alert, error logs mirrored to `event_log`, container limits, a restart test).

**Architecture:** A new package `trader/replay/` (clock, candle fill model, data source, catalysts, setup, runner) drives the **unchanged** Phase 2 engine: a `ReplayClock` steps through each session, a read-only `ReplayData` serves the strategies (candle archive first, Questrade second), `SimBroker.on_candles` fills with a `CandleFillModel`, and every row the replay writes carries its own `runs.id` (mode `replay`). The replay runs as its own process (`trader replay`), started by the CLI or by the API through a launcher, one at a time. `trader/reports/metrics.py` computes every metric in Python from the rows, and `/api/metrics`, the replay comparison and the weekly report all use it. `trader/reports/weekly.py` and `trader/jobs/weekly.py` build the weekly facts, ask Claude (`trader/adapters/claude/reports.py`) for commentary, check its numbers, store it in a new `weekly_reports` table and send it once on Telegram. Hardening adds in-process retries to the day-level jobs (`trader/jobs/runner.py`), an `EventLogMirror` that copies error and critical log lines into `event_log`, and compose resource limits.

**Tech stack:** as Phase 4 (Python 3.12 via uv; SQLAlchemy 2, Alembic, psycopg 3; FastAPI; structlog; anthropic; pytest, pytest-asyncio, respx, testcontainers; React 18, TypeScript, Vite, TanStack Query, Recharts, Vitest, Playwright; Docker, supercronic, supervisord). No new dependencies.

**Spec:** [`../BRD.md`](../BRD.md) BR-41, BR-52, BR-54, BR-60, BR-61, BR-62, O4, O5 and the reliability and maintainability NFRs (§7); [`../SPEC.md`](../SPEC.md) §2 (logging), §3 (layout), §4.3 (weekly commentary), §6.3 (kill switches), §7.4 (candle fill model), §8 (replay and the candle archive), §9 (schedule), §10 (`runs`, views), §11 (`/replays`, `/metrics`, `/export/trades.csv`), §12 (Replay page), §16 (replay determinism). Master plan §7.5 (outline), §7.1 (contracts), the Phase 4 plan's "Notes for the P5 planner", "Resolved decisions" and open question 3.

**Task IDs for the board:** P5-T1, P5-T2, P5-T3, P5-T4, P5-T5, P5-T6, P5-T7, P5-T8, P5-T9, P5-T10, P5-T11, P5-T12, P5-T13, P5-T14, P5-T15, P5-T16, P5-T17, P5-T18.

## Task list, dependencies and parallel lanes

| ID | Task | Depends on | Lane |
|---|---|---|---|
| P5-T1 | Contracts: migration 0006, settings keys, replay types, API schemas and TS mirror, stubs, web client additions, fakes, gate | Every P4 task except P4-T19 accepted, i.e. its gauntlet passed and its fix rounds are on trunk (P4-T9, P4-T18 and the P4 router fix rounds included) | A |
| P5-T2 | Metrics module behind `/api/metrics` | T1 | B |
| P5-T3 | Replay clock and candle fill model | T1 | C |
| P5-T4 | Replay hooks in the engine: `SimBroker.on_candles`, `Engine.on_candles` (same-bar worst case), audit-free auto approvals, replay-scoped strategy configs | T1 | D |
| P5-T5 | Replay data source and stored catalysts | T1 | E |
| P5-T6 | Replay runner: create, pin, snapshot, step, progress, cancel, CLI composition | T1 | F |
| P5-T7 | Replay API, launcher, change-feed topics, live views without replay rows | T1 | G |
| P5-T8 | Web Replay page | T1 | H |
| P5-T9 | Weekly report: facts, Claude commentary, number check, budget, job body | T1 | I |
| P5-T10 | Telegram messages and relay: weekly report, run-to-date line, kill-switch reset confirmation, `log.*` never relayed | T1 | J |
| P5-T11 | Kill-switch trips end to end (realistic data) | T1 | K |
| P5-T12 | Reports API and CSV export columns | T1 | L |
| P5-T13 | Web Reports commentary | T1 | M |
| P5-T14 | Error log mirror to `event_log` | T1 | N |
| P5-T15 | Job retries and restart recovery | T1 | O |
| P5-T16 | Container resource and log limits | T1 | P |
| P5-T17 | Wiring: CLI, runtime, API services, launcher, crontab, SPEC §9 amendment, §7.1 rows | T2–T16 | A |
| P5-T18 | End to end: determinism golden test, isolation test, weekly day, LIVE deploy and checks | T17, P4-T19 accepted | A |

**Critical path:** P5-T1 → P5-T6 (the largest build task) → P5-T17 → P5-T18: **4 tasks**.
**Maximum parallel width:** **15** (T2–T16 all start once T1 has passed its Verifier; T16 also pulls any P4-T19 compose change first, see T16). The orchestrator's cap is 8 builders: start T6, T4, T3, T5, T7, T9, T2 and T10 first (the replay core and the report path, which the integration depends on most), then T8, T11, T12, T13, T14, T15 and T16 as slots free up.

Every build task depends only on the T1 contracts: backend tasks use each other's work through the T1 signatures (stubs raise `NotImplementedError("P5-Tn")` until their owner lands) and the fakes in `tests/fakes_replay.py` and `tests/fakes_api.py`; web tasks use the T1 web contracts and `FakeApiClient`. Only T17 and T18 use real implementations together. Where a build task's test would otherwise call another task's stub, the task below says what it injects or monkeypatches instead.

**Changes from the master-plan outline (§7.5), with reasons:**
- **One contract task (T1) for backend and web.** The P4-T18 TypeScript-mirror test now runs in every gate, so the Python schemas and `web/src/api/types.ts` must change in the same commit; a separate web-contracts task would either fail the gate or lengthen the critical path to 5.
- Outline T1 (metrics) is **T2**; outline T2 (clock and candle fill model) is **T3**, plus the engine-side hooks in **T4** (they change Phase 2 files, so they get their own owner); outline T3 (data source) is **T5**; outline T4 (runner, CLI, API) is split into **T6** (runner), **T7** (API) and **T17** (CLI wiring); outline T5 (Replay page) is **T8**; outline T6 (reports and export) is split into **T9** (weekly report), **T10** (Telegram rendering and relay), **T12** (reports API and export) and **T13** (web commentary); outline T7 (kill-switch reset flow) is **T11** plus the reset confirmation in **T10**, because the reset panel, the reset route and the audit already exist (P4-T6, P4-T15); outline T8 (hardening) is **T14**, **T15** and **T16**, and the determinism golden test moves to the end-to-end task **T18**.
- **No `trader/reports/daily.py`.** The daily summary already exists (P3-T7, `trader/jobs/postclose.py` `daily_summary_view` and `MessageRenderer.daily_summary`); T10 adds a run-to-date metrics line to it instead of a second module. **No `trader/logging.py`**: the JSON logging and redaction live in `trader/logging_setup.py` (P3); the new mirror is `trader/logging_mirror.py`.
- Code comments on trunk that say "P5-T1" (metrics), "P5-T2" (candle fill model) or "P5-T6" (export, weekly commentary) refer to the outline's numbering; this plan's owners are T2, T3, T12 and T9/T13.

**Contracts refined (master plan §7.1; names and meaning kept; T17 updates §7.1):**
1. **Broker:** `SimBroker.on_candles(candles: Mapping[int, Candle], now: datetime, *, orders: Collection[int] | None = None) -> list[FillEvent]`. The outline wrote `on_candles(candles, now)`; `Candle` carries no symbol, so the candles are keyed by `trader.symbols.id`. `orders` restricts the pass to those order ids and skips the "submitted before the bar ended" rule (used only for the same-bar worst case).
2. **Engine:** gains `Engine.on_candles(candles: Mapping[int, Candle], now: datetime) -> list[FillEvent]` (additive), the candle twin of `on_quotes`, including the same-bar worst-case pass (decision "Candle fill model" below).
3. **Proposals:** `ProposalService(..., audit_auto: bool = True)` (additive keyword). `False` (replay only) skips the `proposal.auto_approve` audit row, so a replay never writes `audit_log` rows stamped with simulated past times.
4. **Strategy configs:** `strategy_configs.scope` (`live` | `replay`); `StrategyConfigView.scope: str = "live"` (last field, defaulted); `StrategyRegistry._latest` (so `current`, `enabled`, `instance`, `update`, `ensure_defaults`) only ever sees `live` rows; `StrategyRegistry.create_replay_config(...)` writes a `replay` row for a replay's parameter override. The live settings of a plug-in can never be changed by a replay.
5. **Runs:** `runs` gains `finished_at`, `updated_at`, `progress`, `error`, `cancel_requested`; a replay run's `status` is `queued` → `running` → `completed` | `failed` | `cancelled` (the live run keeps `active`); its `params` has the shape fixed in T1.
6. **Notifier and Renderer:** `Renderer.weekly_report(v: WeeklyReportView) -> OutboundMessage`; `DailySummaryView.run_to_date: RunToDateView | None = None` (last field, defaulted). The relay never relays an `event_log` source starting with `log.`, and relays a kill-switch **reset** event (`source = "killswitch"`, message `kill switch <switch> reset`) as a `kill_switch` message with reset wording.
7. **Job runner:** `run_job(..., retry: RetryPolicy | None = None)` and `run_job_async(..., retry=...)` (additive). Without `retry` the behaviour is exactly P3's; the day-level CLI jobs pass `RetryPolicy.from_settings(...)` (T17).
8. **Web API:** `Topic` gains `replays` and `reports`; `ManualJob` gains `weekly`; `MetricsOut` gains four defaulted fields; `ApiServices.replays: ReplayLauncher | None = None` (last field); routers `replays` and `reports` join `ROUTERS` (17 routers). Dashboard events, the SSE `events` stream and the System page's events and errors exclude rows of replay runs.
9. **New contract rows** (T17 adds them to §7.1): "Replay" (runs columns and params, `ReplayRequest`, `create_replay` / `run_replay`, `trader replay`, the `/api/replays` routes, isolation rules), "Metrics" (`trader.reports.metrics` definitions), "Weekly report" (`weekly_reports`, `trader weekly`, dedupe `weekly:<week_ending>`, the number check), "Log mirror" (`log.<process>` sources, never relayed).

### Key decisions

- **Replay clock and time.** `ReplayClock` is a monotonic `Clock` (it refuses to go backwards). Every simulated record (signals, proposals, orders, fills, trades, ledger, snapshots, events of the run) takes its time from it. Wall time (the run's `started_at`, `updated_at`, `finished_at`, the data-mode decision, the Questrade history window, the Questrade client's own token cache) comes from an injected `RealClock`. The Questrade client is never given the `ReplayClock` (its docstring forbids it).
- **The replay loop.** For each session in the range: prepare the day's data; build the day plan with the pure `trader.engine.scheduler.day_plan` over the pinned strategies; then visit, in time order, every planned event time and every minute boundary while any order is working. At a minute boundary `t` the runner hands the engine the 1-minute bar that ended at `t` for each symbol with a working order (`Engine.on_candles`); at an event time it calls `Engine.run_event`; at every visited time it calls `Engine.tick`. At equal times, bars come before events (a bar ending at `t` is complete at `t`). Before any call for a visited time `t` the runner sets the `ReplayClock` to `t` (`set(t)`), so every row written by that call (order `submitted_at`, fill `ts`, the protective stop submitted by the follow-up, signals, events) carries `t`; the clock never moves inside a call. After the close: a replay-only forced close of anything still open (decision below), then `Engine.end_of_session`. Replay **never** uses `fire_event`, `run_job` or `job_runs` (so a replay can never count as a failed or missed job in the Phase 6 soak).
- **Candle fill model (SPEC §7.4, 1-minute bars).** `slip(p) = max(slippage_min, slippage_bps × p)` and `hs(p) = replay.half_spread_bps × p` (default 5 bps), each rounded to 4 dp half-up. A bar is used for an order only when `bar.end > order.submitted_at` and `bar.start` is inside regular hours; the entry cutoff is checked against `bar.start`. Rules: buy market `open + slip + hs`; sell market `open − slip − hs`; buy stop triggers when `high ≥ stop` and fills at `ref + slip + hs` with `ref = max(stop, open)`; sell stop triggers when `low ≤ stop` and fills at `ref − slip − hs` with `ref = min(stop, open)` (a gap through the stop fills at the open, worse than the stop); buy stop-limit as the buy stop, filled only if the price is at most the limit (else it keeps working); buy limit triggers when `low + hs ≤ limit` and sell limit when `high − hs ≥ limit`, and both fill at **the limit price** exactly, as the quote model does (SPEC §7.2), never at a better open (for limits `hs` is taken of the limit price; limit fills carry no slippage, as in the quote model). For market orders `slip` and `hs` are taken of the open, for stops of `ref`. `FillDecision.slippage` is `slip` (the cost against the synthetic ask or bid, comparable with live fills, which are measured against the real ask or bid); `hs` is recorded in the snapshot. Fees are the quote model's (`FillParams`). A bar with a non-positive price, `low > high` or zero volume never fills (`NoFill("bad_bar")` / `NoFill("no_volume")`).
- **Worst case when one bar touches the entry and the stop.** After an entry fills in bar `b`, the engine's normal follow-up submits the protective stop at `now = b.end`. `Engine.on_candles` then runs a **same-bar pass**: it re-applies `b` to that new stop order with the bar's open replaced by the entry's fill price (`orders=[stop id]`, so the "submitted before the bar ended" rule is skipped). If `b.low ≤ stop` the position is stopped out in the same bar at `stop − slip − hs` (the reopened open is above the stop, so `min(stop, open) = stop`): entered, then stopped, never the favourable order. Reason: it is exactly SPEC §7.4's "assume the worst case", it reuses the one fill model and the one broker loop, and it needs no knowledge of the sequence inside the bar.
- **Replay data (SPEC §8).** Strategy-facing reads never return a bar whose `end` is after the replay clock (no lookahead). Opening 5-minute bars: `candle_archive` (5m) → `intraday_candles` (5m) → Questrade `FiveMinutes` (per symbol, the whole range plus the look-back, split into windows the client accepts: `QuestradeClient.candles` refuses a window longer than `MAX_CANDLES_PER_REQUEST` = 20,000 intervals, about 69 calendar days of 5-minute bars, so a 130-session range takes 3–4 requests per symbol). **Memory:** from every `FiveMinutes` response only each session's opening bar is kept (the rest is dropped at once), and 1-minute bars are held for the current session only and released when the runner moves to the next day, so a 130-session full-mode replay stays small inside the container's 1 GiB limit (T16), which it shares with the live worker. **No wall-clock fetch deadline** (unlike the live 9:35 scan's `fetch_deadline_s`): a replay waits for every response, and an error is a counted "missing", so the result never depends on timing. 1-minute bars for fills: `candle_archive` (1m) → `intraday_candles` (1m) → Questrade `OneMinute` (one request per symbol per session). Daily bars (ATR, prior close): `daily_candles` → Questrade `OneDay`. Questrade is used only when the session is within `replay.questrade_window_days` (default 85) of the wall-clock date, and never in offline mode. **Universe:** that session's `universe_snapshots`; with none, the newest stored universe on or before the wall-clock date, every member marked `source = "biased"` and the day added to `biased_days` (the run is labelled "biased universe"). **Opening-bar stats:** that session's `open_bar_stats`; for symbols without a row, computed in memory with the nightly job's formulas (`trader.market.indicators.average_volume` over the prior `open_bar.lookback_sessions` opening bars, `atr` over 14 daily bars). **Quotes** (the overlay's SPY price, account marks, a market order's reference price) are synthetic, from the last complete 1-minute bar: `last = close`, `bid = close − hs`, `ask = close + hs`, `last_trade_time = bar end`, `delay = 0`; a symbol whose 1-minute bars for the day are not loaded yet is loaded on first request (as `load_minute_bars`), so a market entry's reference price exists whenever the day has bars. **Catalysts:** `replay.catalyst_mode = stored` reads the stored `catalysts` rows (read only) and reports a missing name as `unknown`; `unknown` reports every name as `unknown` (with `orb_sip`'s `require_catalyst` on, that mode trades nothing, and the Replay page says so). No Claude call ever.
- **Replay isolation.** A replay writes only: its `runs` row, its `sim_accounts` row, rows that carry its `run_id` (candidates, signals, proposals, orders, fills, positions, trades, cash_ledger, equity_snapshots, kill_switch_events, event_log), `replay`-scoped `strategy_configs` rows, and the two human audit rows `replay.start` / `replay.cancel`. It never writes `job_runs`, `notifications`, `notify_cursors`, `catalysts`, `universe_snapshots`, `open_bar_stats`, `candle_archive`, `intraday_candles`, `daily_candles`, `symbols`, `settings`, `telegram_callbacks` or `worker_heartbeats` (fetched Questrade data is held in memory for the run only). The one shared exception: in `full` mode its Questrade client may refresh the shared token chain (`api_credentials`), exactly as every other process does (P1-T6's single-exchange rule applies); an `offline` replay never touches it. Its approvals are automatic through a settings snapshot (`approval_mode = "auto"`), never by touching the global setting. The relay already ignores events of other runs; every replay event carries the replay's `run_id`, including a strategy plug-in failure (the pinned registry reports with the run id) and the log-mirror rows of the replay process (T14's `run_id` argument, installed by T17 with the replay's id), so nothing a replay causes appears on the live Dashboard or System page or reaches Telegram.
- **Determinism rules (Review Focus 1).** Simulated time only from the `ReplayClock` (set by the runner, never read from the wall); no `float` anywhere in prices, quantities, fees or metrics (Decimals, serialised as strings); no randomness in `trader/replay/` (no `random`, `uuid`, `secrets` or `time`-based values); every collection a result depends on is sorted (symbols and orders by id, strategies by key, candidates by the plug-in's own `(-rvol, ticker)`); data fetched concurrently is assembled by symbol id, never in completion order; the data mode is decided once and stored; the golden files are written with sorted keys. Database ids differ between runs and databases, so tests compare normalized values only.
- **Data mode.** A replay started between 09:15 and 16:30 ET on a session day runs **offline** (DB data only; missing data is counted, not fetched), so it never competes with the live worker for Questrade's rate limit; otherwise it is `full` with its own client limited to `replay.questrade_rps` (default 4 req/s). The mode is decided once when the run is created and stored in its params, so the run is reproducible. `--offline` (CLI) or `offline: true` (API) forces it.
- **Forced close (replay only).** If a position is still open at `close − 1 minute` (for example no 1-minute bar after the flatten), the runner cancels that position's working orders and submits a market exit (reason `replay_forced_close`) through the broker; at the close it is filled by the real bar ending at the close or, when there is none, by a synthetic bar (start `close − 1 minute`, end `close`, every price the last known close, or the entry price when there is none, volume 1), and it is counted in `progress.forced_closes`. The live rule (BR-42) is unchanged; this only keeps a replay from carrying a position into the next day.
- **Strategy settings snapshot.** A replay pins, per plug-in, the live `strategy_configs` row current at creation (its id is stored in the params); an override (`strategies: {key: {params, enabled}}`) is merged into those params, validated by the plug-in's model and written as a `replay`-scoped row (`revision` = the base live revision, `created_by = "replay:<run id>"`). Signals, orders and positions therefore always reference a real config row.
- **One replay at a time.** A session-level advisory lock (`trader.replay`) is held by the running process; a second start is refused (`ReplayBusy`, API 409, CLI exit 2). A `queued` or `running` row whose lock is free (the process died) is settled `failed` ("abandoned") by `reconcile_abandoned`, called before every start and by the list route.
- **Metrics (BR-52, O4).** Computed in Python from the rows by `trader.reports.metrics` (below), 4 dp half-up. For the whole run they equal `v_trade_metrics` where both define the value (a test pins it); the view stays for SQL users. `/api/metrics` keeps its route and `MetricsOut`, gaining four defaulted fields.
- **Weekly report (BR-61, SPEC §4.3).** `trader weekly` runs Saturday 09:00 ET (not a session; the job is keyed by the week's last session date). It sends the report even when the commentary is unavailable. The commentary costs at most `reports.weekly_max_cost_usd` (default US$0.05) in all, its retry included, and is counted in the daily Claude spend of the ET date it runs (the weekly job's own budget check counts the catalyst spend of that date too; the pre-market classifier's check is not changed in Phase 5, which matters only for a forced weekday run and then by at most the weekly cap); every number in it must appear in the facts Claude was given (one retry naming the offending numbers, then the report goes out without commentary). The Telegram message is self-contained (headline numbers, the commentary, the link) and sent once per week (dedupe `weekly:<week_ending>`).
- **SPEC §9 changes (T17 applies them, Open question 5):** (1) the `Sat 09:00` weekly row gets the note "runs although Saturday is not a session; reports on the Monday–Friday week just ended and is keyed by that week's last session date; it does nothing when that week had no session"; (2) after the table: "The day-level jobs (nightly, premarket, preopen, postclose, weekly) retry a failed run in-process up to `jobs.retry_attempts` times in all, waiting `jobs.retry_delay_seconds` and then twice that; an intermediate failure is a `warning` event, and only the final failure is an `error` event (one Telegram alert per job and session). Events keep their own retries (30/60/120 s)." No new crontab lines are added for retries.
- **Kill switches: gaps only.** The web reset panel with a typed reason (P4-T15), the reset route with its audit row (P4-T6) and the trip alerts (P3 relay) exist. Phase 5 adds: a Telegram confirmation when an automatic switch is reset (T10), realistic end-to-end trip tests for all three automatic switches through the real engine and relay (T11), and kill switches that work inside a replay with the replay's own run id (T6, T18).
- **Hardening.** (a) Logging: structlog JSON to stdout with redaction already exists (`trader/logging_setup.py`); **"mirrored to event_log" adds** that `error` and `critical` log lines which no code wrote to `event_log` (for example an API 500, a relay or FinViz failure, an invalid settings row) appear in the System page's error list. The mirror is asynchronous and lossy (never blocks, never raises, never recurses), rate-limited, masked with the same redaction, and never relayed to Telegram (the events that should alert are already written with `log_event`). (b) Retries as in "SPEC §9 changes" above. (c) Restart recovery already has P3 mechanisms (the worker's advisory lock and exit 2/3/4, settled event keys, `OUTCOME_UNKNOWN` for an interrupted entry event, `notifications` dedupe with `sending`/`unknown`, relay cursors); T15 pins them together in one hard-kill scenario and T18 checks a real restart in market hours. (d) Compose limits: memory 1 GiB, 2 CPUs, 256 processes, `json-file` logs 10 MB × 5.

Every command below runs from the repository root of your worktree unless it says otherwise. "Run from `Trader/app`" means `uv --directory Trader/app run ...`; web commands use `npm --prefix Trader/web ...`. LIVE steps use `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev ...` (master plan §6 preamble) or the scripts named in the step.

## File map

One owner per file. Paths are under `Trader/app/` unless they start with `web/`, `docker/`, `docs/` or `Trader/`. T1 creates the stub modules and stub methods marked (stub); the owning task replaces them with the implementation.

| Path | Responsibility | Task |
|---|---|---|
| `trader/db/models.py`, `trader/db/migrations/versions/0006_replay_reports.py` | `runs` columns, `strategy_configs.scope`, `WeeklyReport` (number re-checked at build time) | T1 |
| `trader/settings_store.py` | Phase 5 runtime settings (`replay.*`, `reports.*`, `jobs.*`, `logging.*`) | T1 |
| `trader/api/schemas.py`, `trader/api/deps.py`, `trader/api/views.py` (`metrics_out`), `trader/api/forms.py` (`SETTING_GROUPS` only), `trader/api/routers/__init__.py` | API contracts | T1 |
| `trader/replay/__init__.py`, `trader/replay/types.py` | Replay contracts (real) | T1 |
| `trader/notify/types.py` | `RunToDateView`, `WeeklyReportView`, `DailySummaryView.run_to_date`, `Renderer.weekly_report` | T1 |
| `trader/strategies/registry.py` | `StrategyConfigView.scope` (T1); `_latest` scope filter and `create_replay_config` | T1 (field, stub) then T4 |
| `trader/reports/metrics.py` (stub functions, real dataclasses) | Metrics | T2 |
| `trader/api/routers/performance.py` | `/api/metrics` through the metrics module | T2 |
| `trader/replay/clock.py`, `trader/replay/candle_fill_model.py` (stubs) | Replay clock, candle fill model | T3 |
| `trader/broker/sim_broker.py`, `trader/engine/orchestrator.py`, `trader/engine/proposals.py` (T1 adds the stub methods and the keyword) | Candle fills, same-bar pass, `audit_auto` | T4 |
| `trader/replay/data.py`, `trader/replay/catalysts.py` (stubs) | Replay data and catalysts | T5 |
| `trader/replay/setup.py`, `trader/replay/runner.py` (stubs) | Snapshot settings, pinned registry, engine build, runner, CLI composition | T6 |
| `trader/api/routers/replays.py` (stub router), `trader/api/replay_launcher.py` (stub), `trader/api/feed.py`, `trader/api/routers/dashboard.py` (events filter only), `trader/api/routers/system.py` (events filter only) | Replay API, launcher, topics, live views | T7 |
| `web/src/pages/Replay.tsx` (stub), `web/src/pages/replay/*` | Web Replay page | T8 |
| `trader/reports/weekly.py`, `trader/adapters/claude/reports.py`, `trader/jobs/weekly.py` (stubs) | Weekly report | T9 |
| `trader/notify/messages.py` (T1 adds the stub method), `trader/notify/relay.py`, `trader/jobs/postclose.py` | Telegram rendering and relay | T10 |
| `trader/engine/killswitch.py` (only to fix a defect its tests reveal) | Kill-switch trips end to end | T11 |
| `trader/api/routers/reports.py` (stub router), `trader/reports/export.py` | Reports API, CSV columns | T12 |
| `web/src/pages/Reports.tsx`, `web/src/pages/reports/*`, `web/src/pages/performance/Reports.test.tsx` | Web Reports commentary | T13 |
| `trader/logging_mirror.py` (stub) | Error log mirror | T14 |
| `trader/jobs/runner.py` (T1 adds `RetryPolicy` and the keywords) | Retries | T15 |
| `docker/docker-compose.dev.yml`, `docker/docker-compose.prod.yml` | Container limits | T16 |
| `trader/cli.py`, `trader/runtime.py`, `trader/worker.py`, `trader/api/services.py`, `trader/api/launcher.py` (`CLI_ARGS` only), `docker/crontab`, `tests/test_crontab.py`, `tests/api/test_launcher.py` (pinned `CLI_ARGS`), `Trader/docs/SPEC.md` (§9 amendment), `docs/plans/2026-09-26-build-master-plan.md` (§7.1) | Wiring | T17 |
| `web/src/api/types.ts`, `web/src/api/client.ts`, `web/src/api/http.ts` (the six new methods), `web/src/api/queryKeys.ts`, `web/src/test/fakeApi.ts`, `web/src/test/fixtures.ts`, `web/src/App.tsx` (route), `web/src/layout/Layout.tsx` (`NAV_ITEMS`) | Web contracts | T1 |
| `tests/fakes_replay.py`, `tests/db/test_migration_0006.py`, `tests/test_runtime_settings_phase5.py`, `tests/test_phase5_contracts.py`, `tests/replay/__init__.py`, `tests/api/test_phase5_schemas.py`; edits to `tests/test_phase4_contracts.py` (the router count and `ROUTER_ORDER`, and the pinned `Topic` and `ManualJob` values), `tests/fakes_api.py` (`FakeReplayLauncher`), `tests/fakes_telegram.py` (any `Renderer` fake gains `weekly_report`) | T1 tests and helpers | T1 |
| `tests/reports/test_metrics.py`, `tests/api/test_performance_phase5.py` | T2 tests | T2 |
| `tests/replay/test_clock.py`, `tests/replay/test_candle_fill_model.py` | T3 tests | T3 |
| `tests/broker/test_sim_broker_candles.py`, `tests/engine/test_engine_candles.py`, `tests/engine/test_proposals_audit_auto.py`, `tests/strategies/test_registry_scope.py` | T4 tests | T4 |
| `tests/replay/test_data.py`, `tests/replay/test_catalysts.py` | T5 tests | T5 |
| `tests/replay/test_setup.py`, `tests/replay/test_runner.py` | T6 tests | T6 |
| `tests/api/test_replays.py`, `tests/api/test_replay_launcher.py`, `tests/api/test_feed_phase5.py`, `tests/api/test_live_views_exclude_replays.py` | T7 tests | T7 |
| `web/src/pages/replay/*.test.tsx` | T8 tests | T8 |
| `tests/reports/test_weekly.py`, `tests/adapters/test_claude_reports.py`, `tests/jobs/test_weekly.py` | T9 tests | T9 |
| `tests/notify/test_messages_phase5.py`, `tests/notify/test_relay_phase5.py`, `tests/jobs/test_postclose_run_to_date.py` | T10 tests | T10 |
| `tests/integration/test_killswitch_trips.py` | T11 tests | T11 |
| `tests/api/test_reports.py`, `tests/reports/test_export_phase5.py` | T12 tests | T12 |
| `tests/test_logging_mirror.py` | T14 tests | T14 |
| `tests/jobs/test_runner_retry.py`, `tests/integration/test_restart_recovery.py` | T15 tests | T15 |
| `tests/test_docker_limits.py` | T16 tests | T16 |
| `tests/test_cli_phase5.py`, `tests/test_runtime_phase5.py` | T17 tests | T17 |
| `tests/replay/test_golden.py`, `tests/replay/golden/*`, `tests/replay/test_isolation_static.py`, `tests/integration/test_replay_isolation.py`, `tests/integration/test_weekly_report_day.py`, `web/tests/smoke.spec.ts` (adds `/replay`) | End to end | T18 |

## Global Constraints

The master plan's **Global Constraints** apply word for word (trunk only, uv, SPEC §3 layout, DDL only in migrations, `timestamptz` UTC, `Decimal` money, time only from a `Clock`, no secrets printed or committed, no network in unit or adapter tests, testcontainers for DB tests, the full gate through the shared test lane (`bash Trader/build/gate.sh`, never `check.sh` directly) once before every commit, `P5-Tn: ` commit prefix and the `Co-Authored-By` trailer, explicit staging), and so do the Phase 4 plan's phase-specific rules (every `/api` route needs a session except login, health and meta; CSRF and an audit row on every change; money as JSON strings; no secret in a response or a log; sync DB work off the event loop; web times through `format.ts` in MT; 390 px phone width, 44 px buttons; runtime settings declare `alias=`; no `model_validator`). Phase-specific additions:

- **Replay isolation (Review Focus 2):** replay code (`trader/replay/*`) never imports `anthropic`, `trader.adapters.telegram`, `trader.notify.notifier` or `trader.notify.relay`, never constructs `CatalystClassifier` or `CatalystService`, never calls `fire_event`, `run_job` or `run_job_async`, never calls `SettingsStore.set` or `StrategyRegistry.update` / `ensure_defaults`, and writes only the rows listed in the decision "Replay isolation". Every `log_event` it causes carries the replay's `run_id`.
- **Replay time:** simulated time only from the `ReplayClock`; wall time only from an injected `RealClock` (the existing `test_no_wall_clock.py` rule still holds: no `datetime.now()` in `trader/` outside `trader/market/clock.py`). No strategy-facing replay read returns data from after the replay clock.
- **Replay determinism:** the same inputs (the run's params and the same market data) give the same trades, fills, prices and metrics. No iteration over unordered sets or dicts whose order depends on hashing or insertion from concurrent tasks; ties are broken by symbol id or order id. Tests compare normalized results (tickers, dates, prices, quantities, reasons), never database ids.
- **Claude:** the only new Claude call is the weekly commentary; tests mock the client; `claude.daily_budget_usd` and `reports.weekly_max_cost_usd` are both enforced before any call; the model comes from `claude.model`.
- **Migrations:** Phase 5's migration is `0006` (`0005` is P4's); it names the schema explicitly and keeps `test_models_match_migrated_schema` and the Alembic autogenerate check passing. Before writing it, pull trunk and run `uv --directory Trader/app run alembic heads`: there must be exactly one head; if it is not `0005`, chain onto the single head with the next free number, rename the migration and its test, and say so in the report.
- **Soak protection (Phase 6 counts clean days from the P4-T19 deploy):** no Phase 5 LIVE step may make a scheduled job fail or an event be missed on `trader_dev`; deploys, migrations and every LIVE replay (they share the container's CPU and memory with the live worker) run outside 09:15–16:30 ET on session days and never within 10 minutes of a cron line (02:00, 08:00, 20:00 ET and the session lines of `docker/crontab`); no fake trading rows in the live run of `trader_dev`; the dev bot only; Questrade read only; agents never call the QuestTrade MCP order tools.
- **Shared test lane (master plan Global Constraints):** while working run only targeted tests (`uv --directory Trader/app run pytest <your test files> -q`, `npm --prefix Trader/web exec vitest run <files>`, `ruff`/`mypy` on your files). Every "Gate" step in this plan means **`bash Trader/build/gate.sh`** from the worktree root (it queues on the machine-wide lock and runs `check.sh`), once just before committing, never `check.sh` directly. Every builder, Verifier and fix prompt for this phase says so.
- **Reconcile with P4-T18 and the P4 fix rounds (they had not landed when this plan was written, 2026-09-27 14:40 MT).** Before writing code, pull trunk and re-read the P4 names you consume; where trunk differs from this plan, use trunk's name and shape, keep this plan's behaviour, and say so in your report (the Phase 4 plan did the same for the P3-T12 fix round). Expected differences: `trader/api/services.py` `build_services` as P4-T18 wrote it (T17 adds to it, never rewrites it); the new CLI commands `create-admin` and `user-password` and whatever P4-T18 changed in `trader/cli.py`, `trader/runtime.py` and `trader/worker.py` (T17 adds its commands and keywords beside them); P4-T18's contract tests `tests/api/test_ts_contract.py` (the TypeScript mirror, T1 test 7) and `tests/api/test_routes_sweep.py` (T17 test 5), whose file names may differ; `HistogramBinOut` with `allow_inf_nan` and `routers/performance.py` without T7's `model_construct` workaround (T2 keeps any name P4-T18 left in use); `JobLaunchOut.session_date` nullable in `types.ts`; `TRADER_FORWARDED_ALLOW_IPS` (P4-T4 fix) possibly added to the compose files (T16 keeps it); and the P4 router fix rounds in `dashboard.py`, `system.py`, `trading.py`, `journal.py` and `stream.py` (T7 edits only the event queries of the first two). If a P4 name this plan relies on is gone or means something else, stop and report instead of improvising (master plan §6.1).
- **Phase 4 names used, read from trunk (and the P4-T9, P4-T18 and P4-T19 builds when they land):** `ApiServices`, `Services`, `CurrentUser`, `CsrfUser`, `actor`, `live_run_id`, `resolve_run`, `ApiError`, `ApiModel`, `Items`, `MetricsOut`, `HistogramBinOut` (with `allow_inf_nan` after P4-T18), `EventOut`, `views.event_out`, `PollingChangeFeed`, `WATERMARK_TOPICS`, `SubprocessJobLauncher`, `CLI_ARGS`, `build_services`, `create_app`; web `ApiClient`, `createHttpClient`, `FakeApiClient`, `renderWithProviders`, `qk`, `TOPIC_KEYS`, `NAV_ITEMS`, `FieldInput`, `EquityChart`, `MetricTiles`. Phase 1–3 names: `Clock`, `RealClock`, `FixedClock`, `et_date`, `ET`, `SessionCalendar.is_session/session_open/session_close/next_session/previous_session/sessions_before`, `Candle`, `QtQuote`, `UniverseMember`, `UniverseStatus`, `OpenBarStats`, `OpeningBars`, `MarketDataView`, `CatalystSource`, `CatalystInfo`, `StoredCatalyst`, `CatalystStore.get`, `indicators.atr/average_volume/opening_bar/regular_hours`, `FillModel`, `FillParams`, `FillDecision`, `NoFill`, `Fees`, `OrderSpec`, `FillEvent`, `SimBroker`, `Ledger`, `Engine`, `build_engine`, `ProposalService`, `RiskManager`, `KillSwitches`, `StrategyRegistry`, `StrategyConfigView`, `day_plan`, `PlannedEvent`, `DayPlan`, `get_live_run`, `ensure_sim_account`, `starting_balance`, `log_event`, `session_scope`, `run_job`, `run_job_async`, `JobFailure`, `JobOutcome`, `redact_text`, `is_secret_key`, `configure_logging`, `MessageRenderer`, `NotificationRelay`, `alert_kind`, `OutboundMessage`, `DailySummaryView`, `daily_summary_view`, `runtime.GuardedSettings/questrade_auth/LazyQuestrade/build_notifier/build_renderer/open_telegram/run_cli_job`. If a name on trunk differs from this plan, use the trunk name and say so in your report.

## Review Focus

The five Phase 5 failure modes most likely to hurt Stephen, most likely first. Each is pinned by acceptance tests in the task named.

1. **Replay results that are not reproducible or that peek at the future** (a bar ending after the replay clock used by a strategy or a synthetic quote; a set iterated in hash order; the Questrade window measured from the replay clock; two runs of the same inputs giving different trades). Expected: strategy-facing reads never return data after the clock; the same inputs give identical normalized trades and metrics, matching a committed golden file. [T3 tests 1–2; T5 tests 3–5, 10; T6 tests 4–5; T18 tests 1–2, 4]
2. **A replay leaking into the live system** (a Telegram message, a Claude call, a `job_runs` or `catalysts` row, the global approval mode or a live strategy revision changed, an event without the replay's run id relayed, replay rows on the Dashboard or in `/api/metrics?run=live`). Expected: only the rows listed under "Replay isolation" change, the live metrics are identical before and after, nothing is relayed. [T4 tests 7–9; T5 tests 8–9; T6 tests 1–3, 8; T7 tests 8–9; T10 test 5; T14 test 9; T17 test 6; T18 tests 3–4, LIVE 3]
3. **Candle fills that flatter the strategy** (entry and stop in one bar counted as a win, the half-spread left out, a gap through a stop filled at the stop instead of the open, a fill from a bar that ended before the order existed, a fill outside regular hours or after the entry cutoff). Expected: every SPEC §7.4 rule and the decisions above hold, and the same-bar case always stops out. [T3 tests 3–10; T4 tests 1–6]
4. **Numbers Stephen can't trust** (metrics that differ from a hand calculation, from `v_trade_metrics` or between the web, Telegram and the weekly report; a commentary that invents or computes a number; a commentary that overspends the budget or is sent twice). Expected: hand-calculated values match exactly; the number check rejects any figure not in the facts; the budget stops the call; the message is sent once. [T2 tests 1–7; T9 tests 1–9; T10 tests 1–3; T18 test 5]
5. **Operations that fail silently or loudly** (a failed nightly job that is never retried, three phone alerts for one failure, a mirror that blocks the worker's event loop, recurses on a database error or floods Telegram, a restart during market hours that duplicates an order or a message). Expected: a transient failure is retried and alerts once only if every attempt fails; the mirror never blocks or raises and is never relayed; a hard kill and restart duplicates nothing. [T14 tests 1–8; T15 tests 1–8; T10 test 6; T18 LIVE 6]

---

### Task P5-T1: Contracts: migration 0006, settings keys, replay types, API schemas and TS mirror, stubs, web client additions, fakes, gate

**Goal:** Create every shared contract of Phase 5 so T2–T16 can be built in parallel: one migration, the settings keys, the replay types, the API schemas with their TypeScript mirror, the web client additions, the stub modules and stub methods, the fakes and the gate. No behaviour beyond the small real pieces named. BRD BR-52, BR-54, BR-61; SPEC §8, §10, §11, §13.

**Files:** as the file map rows marked T1 (including the stub modules and stub methods of T2–T16), plus the test files listed for T1. Expect about 45 minutes: it is larger than usual because the backend and web contracts share one commit (see "Changes from the outline").

**Interfaces (produce):**
- **Migration `0006_replay_reports.py`** (`revision = "0006"`, `down_revision = "0005"`; schema `trader`):
  - `runs`: add `finished_at timestamptz NULL`, `updated_at timestamptz NULL`, `progress jsonb NULL`, `error text NULL`, `cancel_requested boolean NOT NULL DEFAULT false`; index `ix_runs_mode_status (mode, status)`.
  - `strategy_configs`: add `scope varchar(10) NOT NULL DEFAULT 'live'` with check `ck_strategy_configs_scope` (`scope IN ('live', 'replay')`); drop the unique constraint `uq_strategy_configs_key_revision`; create the unique index `uq_strategy_configs_key_revision_live` on `(strategy_key, revision)` `WHERE scope = 'live'`.
  - `weekly_reports`: `week_ending date` PK, `week_start date NOT NULL`, `run_id bigint NOT NULL` FK `runs.id`, `facts jsonb NOT NULL`, `commentary text NULL`, `commentary_status varchar(20) NOT NULL`, `commentary_error text NULL`, `model varchar(60) NULL`, `input_tokens int NULL`, `output_tokens int NULL`, `cost_usd numeric(10,6) NOT NULL DEFAULT 0`, `created_at timestamptz NOT NULL`, `updated_at timestamptz NOT NULL`.
  - Downgrade: refuses with a clear error when any `strategy_configs` row has scope `replay` ("delete the replay runs first"); otherwise drops `weekly_reports`, restores the unique constraint and drops the new columns and indexes.
  - ORM: `Run` gains the five columns; `StrategyConfig.scope: Mapped[str]` and its table args become the check plus the partial unique index (no `UniqueConstraint`); new `WeeklyReport` model.
- **Runtime settings** (field → DB key, default, bounds): `replay_half_spread_bps: Decimal` → `replay.half_spread_bps`, 5, 0–100; `replay_catalyst_mode: Literal["stored", "unknown"]` → `replay.catalyst_mode`, `stored` (SPEC §8's `replay_catalyst_mode`); `replay_questrade_rps: float` → `replay.questrade_rps`, 4.0, 1–10; `replay_questrade_window_days: int` → `replay.questrade_window_days`, 85, 1–120; `replay_max_sessions: int` → `replay.max_sessions`, 130, 1–500; `reports_weekly_commentary: bool` → `reports.weekly_commentary`, True; `reports_weekly_max_cost_usd: Decimal` → `reports.weekly_max_cost_usd`, 0.05, 0–1; `jobs_retry_attempts: int` → `jobs.retry_attempts`, 3, 1–5; `jobs_retry_delay_seconds: int` → `jobs.retry_delay_seconds`, 120, 10–1800; `logging_mirror_level: Literal["error", "critical", "off"]` → `logging.mirror_level`, `error`; `logging_mirror_max_per_minute: int` → `logging.mirror_max_per_minute`, 30, 1–600. `forms.SETTING_GROUPS` gains `replay.` → "Replay", `reports.` → "Reports", `jobs.` and `logging.` → "Operations".
- **`trader/replay/types.py`** (real): `ReplayStatus = Literal["queued", "running", "completed", "failed", "cancelled"]`; `DataMode = Literal["full", "offline"]`; `CatalystMode = Literal["stored", "unknown"]`; `REPLAY_OVERRIDE_KEYS: frozenset[str]` = {`starting_cash`, `risk_pct`, `cash_account_mode`, `no_entry_before_close_minutes`, `slippage_min`, `slippage_bps`, `fees.commission`, `fees.sec_rate`, `replay.half_spread_bps`, `replay.catalyst_mode`, `killswitch.daily_loss_pct`, `killswitch.max_drawdown_pct`, `killswitch.expectancy_min_trades`, `killswitch.expectancy_threshold_r`}; frozen dataclasses `StrategyOverride(enabled: bool | None = None, params: Mapping[str, Any] | None = None)`, `ReplayRequest(date_from: date, date_to: date, label: str | None = None, overrides: Mapping[str, Any] = {}, strategies: Mapping[str, StrategyOverride] = {}, offline: bool = False)` (mapping defaults via `field(default_factory=...)`), `PinnedStrategy(key, config_id: int, revision: int, version: str, scope: Literal["live", "replay"], enabled: bool, params: Mapping[str, Any])`, `ReplayProgress(sessions_total: int = 0, sessions_done: int = 0, current_date: date | None = None, trades: int = 0, forced_closes: int = 0, biased_days: tuple[date, ...] = (), missing_opening_bars: int = 0, missing_minute_bars: int = 0, questrade_requests: int = 0)` with `to_json()` / `from_json()`, `ReplayRun(id, label: str | None, status: ReplayStatus, date_from, date_to, data_mode: DataMode, catalyst_mode: CatalystMode, half_spread_bps: Decimal, settings: RuntimeSettings, strategies: tuple[PinnedStrategy, ...], overrides: Mapping[str, Any], progress: ReplayProgress, created_at: datetime, finished_at: datetime | None, error: str | None, cancel_requested: bool)`; `load_replay_run(factory, run_id: int) -> ReplayRun` (real reader of the row; `ReplayNotFound` for an unknown id or a live run); exceptions `ReplayInvalid(errors: list[tuple[str, str]])` (field path, message), `ReplayBusy`, `ReplayNotFound`; protocols `ReplayBroker` (`working_symbol_ids() -> list[int]`, `working_orders() -> list[OrderView]`, `open_positions() -> list[PositionView]`, `submit(spec: OrderSpec, session: Session | None = None) -> int`, `cancel(order_id: int, reason: str, session: Session | None = None) -> bool`; `SimBroker` satisfies it), `ReplayEngine` (`broker: ReplayBroker`; `async run_event(event_key, session_date) -> EventResult`; `async on_candles(candles: Mapping[int, Candle], now) -> list[FillEvent]`; `async tick(now) -> None`; `async end_of_session(session_date) -> list[PositionView]`), `ReplayMarket` (`MarketDataView` plus `async prepare_day(session_date) -> None`, `async load_minute_bars(symbol_ids: Sequence[int], session_date) -> None`, `bar_ending_at(symbol_id, at: datetime) -> Candle | None`, `last_close(symbol_id, at: datetime) -> Decimal | None`, `progress_counts() -> Mapping[str, int]`, `biased_days: frozenset[date]`). **`runs.params` of a replay** (written by T6, read by `load_replay_run`): `{"kind": "replay", "date_from", "date_to", "label", "data_mode", "catalyst_mode", "half_spread_bps", "settings": <RuntimeSettings.model_dump(mode="json", by_alias=True)>, "overrides": {...}, "strategies": [<PinnedStrategy as JSON>], "code_version": <EnvSettings.app_version>}`.
- **`trader/api/schemas.py`** additions (and exactly mirrored in `web/src/api/types.ts`): literals `ReplayStatus`, `DataMode`, `CatalystMode`, `CommentaryStatus = Literal["ok", "disabled", "budget", "rejected", "error"]`; `Topic` gains `replays`, `reports`; `ManualJob` gains `weekly`; `MetricsOut` gains `losses: int = 0`, `total_fees: Decimal = Decimal(0)`, `avg_slippage_per_share: Decimal | None = None`, `trades_without_r: int = 0` (after the existing fields). Models: `ReplayStrategyIn(enabled: bool?, params: dict?)`; `ReplayIn(date_from: date, date_to: date, label: str? ≤ 200, overrides: dict[str, Any] = {}, strategies: dict[str, ReplayStrategyIn] = {}, offline: bool = False)`; `ReplayStrategyOut(key, config_id: int, revision: int, version, scope: Literal["live", "replay"], enabled: bool, params: dict)`; `ReplayProgressOut(sessions_total, sessions_done, current_date: date?, trades, forced_closes, biased_days: list[date], missing_opening_bars, missing_minute_bars, questrade_requests)`; `ReplaySummaryOut(id, label?, status: ReplayStatus, date_from, date_to, created_at, finished_at?, data_mode: DataMode, biased: bool, trades: int, expectancy_r: Decimal?, total_pnl: Decimal?)`; `ReplayOut(id, label?, status, date_from, date_to, created_at, finished_at?, data_mode, catalyst_mode: CatalystMode, half_spread_bps: Decimal, overrides: dict, strategies: list[ReplayStrategyOut], progress: ReplayProgressOut, biased: bool, cancel_requested: bool, error: str?, metrics: MetricsOut?, live_metrics: MetricsOut?, events: list[EventOut])`; `ReplayOptionsOut(override_keys: list[str], max_sessions: int, latest_allowed: date, questrade_from: date, archive_from: date?, snapshots_from: date?, busy: bool, offline_now: bool)`; `WeeklyReportOut(week_start, week_ending, run_id, created_at, updated_at, commentary: str?, commentary_status: CommentaryStatus, commentary_error: str?, model: str?, cost_usd: Decimal, facts: dict[str, Any], telegram_status: str?)`.
- **`trader/api/deps.py`:** protocol `ReplayLauncher` (`async launch(run_id: int) -> None`, `running() -> bool`); `ApiServices.replays: ReplayLauncher | None = None` (last field).
- **`trader/api/views.py`:** `metrics_out(m: Metrics) -> MetricsOut` (real, a field-by-field mapping; the histogram bins map to `HistogramBinOut`, open ends as P4-T7 sends them).
- **`trader/api/routers/__init__.py`:** imports and registers `replays` and `reports` (after `watchlist`, before `stream`), 17 routers; `trader/api/routers/replays.py` and `reports.py` are stubs with `router = APIRouter(tags=[...])` and no routes.
- **`trader/notify/types.py`:** `RunToDateView(trades: int, win_rate: Decimal | None, expectancy_r: Decimal | None, total_pnl: Decimal, expectancy_trades: int, expectancy_min_trades: int)`; `WeeklyReportView(week_start: date, week_ending: date, trades: int, wins: int, win_rate: Decimal | None, expectancy_r: Decimal | None, total_pnl: Decimal, max_drawdown_pct: Decimal | None, adherence_pct: Decimal | None, commentary: str | None, commentary_note: str | None)`; `DailySummaryView.run_to_date: RunToDateView | None = None`; `Renderer.weekly_report(self, v: WeeklyReportView) -> OutboundMessage`. `MessageRenderer.weekly_report` is a stub raising `NotImplementedError("P5-T10")`, so the `_is_renderer` mypy check keeps passing.
- **Stub methods and keywords in existing files:** `SimBroker.on_candles(self, candles: Mapping[int, Candle], now: datetime, *, orders: Collection[int] | None = None) -> list[FillEvent]` and `async Engine.on_candles(self, candles: Mapping[int, Candle], now: datetime) -> list[FillEvent]` (raise `NotImplementedError("P5-T4")`); `ProposalService.__init__(..., audit_auto: bool = True)` (stored; T4 makes it take effect); `StrategyConfigView.scope: str = "live"` (last field; `_view` fills it); `StrategyRegistry.create_replay_config(self, key: str, *, base: StrategyConfigView, params: Mapping[str, Any], enabled: bool, created_by: str) -> StrategyConfigView` (stub); `trader.jobs.runner.RetryPolicy(attempts: int = 1, first_delay_s: float = 120.0, backoff: float = 2.0)` (frozen, with `classmethod from_settings(s: RuntimeSettings) -> RetryPolicy`) and the keywords `run_job(..., retry: RetryPolicy | None = None, sleep: Callable[[float], None] = time.sleep)` and `run_job_async(..., retry: RetryPolicy | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep)` (until T15, a policy with `attempts > 1` raises `NotImplementedError("P5-T15")`; `None` or one attempt behaves exactly as today).
- **Stub modules** (functions raise `NotImplementedError("P5-Tn")`; classes have their constructor and method signatures), with the exact signatures in each owning task's Interfaces: `trader/reports/metrics.py` (dataclasses real), `trader/replay/clock.py`, `trader/replay/candle_fill_model.py`, `trader/replay/data.py`, `trader/replay/catalysts.py`, `trader/replay/setup.py`, `trader/replay/runner.py`, `trader/api/replay_launcher.py`, `trader/reports/weekly.py`, `trader/adapters/claude/reports.py`, `trader/jobs/weekly.py`, `trader/logging_mirror.py`.
- **Web contracts:** `types.ts` mirrors every new and changed schema (same names, snake_case fields, `Money`/`IsoTime`/`IsoDate`, `X | null`, string unions); `client.ts` `ApiClient` gains `replayOptions(): Promise<ReplayOptionsOut>`, `replays(q: { limit?: number }): Promise<Items<ReplaySummaryOut>>`, `replay(id: number): Promise<ReplayOut>`, `startReplay(body: ReplayIn): Promise<ReplayOut>`, `cancelReplay(id: number): Promise<ReplayOut>`, `weeklyReport(week: IsoDate): Promise<WeeklyReportOut | null>` (404 → null); `http.ts` implements those six (thin fetch wrappers: `GET /api/replays/options`, `GET /api/replays?limit=`, `GET /api/replays/{id}`, `POST /api/replays`, `POST /api/replays/{id}/cancel`, `GET /api/reports/weekly?week=`), so page tasks never touch `http.ts`; `queryKeys.ts` gains `qk.replayOptions()`, `qk.replays(q)`, `qk.replay(id)`, `qk.weeklyReport(week)`, and `TOPIC_KEYS.replays = ["replays", "replay", "replayOptions"]`, `TOPIC_KEYS.reports = ["weeklyReport"]`; `FakeApiClient` implements the six (recording calls); `fixtures.ts` gains `replayOptions`, `replayQueued`, `replayRunning`, `replayCompleted` (with `metrics`, `live_metrics`, a biased day and two events), `replaySummaries`, `weeklyReportOk` (commentary with numbers from its facts) and `weeklyReportBudget` (`commentary_status: "budget"`); `web/src/pages/Replay.tsx` stub (default export rendering the title "Replay"); `App.tsx` route `/replay` behind `RequireAuth`; `NAV_ITEMS` gains `{ to: "/replay", label: "Replay" }` after Reports (it lands in the phone's More menu). Existing web tests that pin the navigation list are updated here.
- **`tests/fakes_replay.py`:** `FakeReplayMarket` (in-memory universe, stats, opening bars and 1-minute bars per symbol and day, synthetic quotes, lookahead-filtered like the real one), `FakeReplayEngine` (records every call in order with the clock time), `candle(start, o, h, l, c, v)` and `minute_series(...)` builders, `seed_replay_world(factory, ...)` (symbols, a live run, live strategy configs, universe snapshots, archive rows) used by T5, T6 and T18. `tests/fakes_api.py`: `FakeReplayLauncher` (records `launch(run_id)`, `running()` configurable) and `make_services(..., replays=FakeReplayLauncher())`.

**Behaviour and decisions:**
- The weekly report table has a `run_id` (the live run it describes) but is operational data, not a trading row.
- The TypeScript mirror is written in the same commit as the Python schemas, so the P4-T18 mirror test passes at every commit.

**Acceptance tests:**
- [x] 1. After `alembic upgrade head`: the new `runs` columns, `strategy_configs.scope` with its check, the partial unique index and `weekly_reports` exist with the keys above; two `live` rows with the same `(strategy_key, revision)` violate the index, while a `replay` row with the same pair is accepted; the ORM matches the migrated schema; downgrade to `0005` works on a database without replay rows and refuses with the documented message when one exists.
- [x] 2. Every new runtime setting has its default, rejects a value outside its bounds, and has a `SETTING_GROUPS` group (the P4 settings-page tests still pass).
- [x] 3. `ReplayProgress.to_json()` → `from_json()` round-trips (dates as ISO strings); `load_replay_run` reads a hand-written replay row with the params shape above, and raises `ReplayNotFound` for the live run and an unknown id.
- [x] 4. `metrics_out` maps every `Metrics` field; a `MetricsOut` built without the four new fields has their defaults; `model_dump(mode="json")` of a `ReplayOut` fixture has money as strings and dates as `YYYY-MM-DD`.
- [x] 5. The contract test imports every stub module; `ROUTERS` has 17 routers; `FakeReplayLauncher` satisfies `ReplayLauncher`, `FakeReplayMarket` satisfies `ReplayMarket`, `FakeReplayEngine` satisfies `ReplayEngine` (typed assignments, checked by mypy); `MessageRenderer` still satisfies `Renderer`; every stub raises `NotImplementedError` naming its owner.
- [x] 6. `run_job` with `retry=None` and with `RetryPolicy(attempts=1)` behaves exactly as before (the existing runner tests pass unchanged); `RetryPolicy.from_settings` reads `jobs.retry_attempts` and `jobs.retry_delay_seconds`.
- [x] 7. The P4-T18 TypeScript mirror test passes with the new models and literal values (`Topic`, `ManualJob`, `ReplayStatus`, `DataMode`, `CatalystMode`, `CommentaryStatus`).
- [x] 8. Web: `FakeApiClient` satisfies `ApiClient` (`tsc`); `createHttpClient` sends `POST /api/replays` with the CSRF header and the JSON body, and `weeklyReport` resolves `null` on a 404 (mocked `fetch`); `/replay` renders the stub title when logged in; the navigation shows "Replay".
- [x] 9. Gate: `bash Trader/build/gate.sh` passes (the shared test lane; never `check.sh` directly); commit `P5-T1: ...` and push.

**Build notes (P5-T1 builder, 2026-09-27; trunk 45cb891 re-read after P4-T18):**
- `alembic heads` was `0005 (head)`, so the migration is `0006` as planned (no renumbering).
- P4 names consumed are as the plan says: `HistogramBinOut` already allows the `-Infinity`/`Infinity` sentinels;
  the mirror test is `tests/api/test_ts_contract.py` and the route sweep `tests/api/test_routes_sweep.py`.
- Literals: `ReplayStatus`, `DataMode`, `CatalystMode` are defined once in `trader.replay.types` and re-exported
  by `trader.api.schemas`; `CommentaryStatus` is defined in `trader.api.schemas` (and imported by
  `trader.reports.weekly`).
- Protocol shapes: `ReplayEngine.broker` and `ReplayMarket.biased_days` are read-only properties, so `Engine`
  (whose `broker` is a `SimBroker` attribute) and `ReplayData` satisfy them (checked by mypy in
  `tests/test_phase5_contracts.py`).
- Extra real helpers: `replay_run_from_row(row)`, `REPLAY_STATUSES`, `ACTIVE_STATUSES`, `ConfigScope`, the base
  `ReplayError`; `PinnedStrategy.to_json/from_json`; `trader.reports.metrics.OPEN_LOW/OPEN_HIGH` (the histogram's
  open-end sentinels, which `metrics_out` passes through). `runs.params["half_spread_bps"]` is a string.
- `StrategyRegistry._latest` is not scope-filtered yet (T4); no `replay` row can exist before T4/T6 land.
- Pinned tests updated besides those named: `tests/test_phase4_contracts.py::test_api_services_fields` (the new
  last field `replays`), and the web navigation pins in `web/src/shell.test.tsx` and
  `web/src/gauntlet/web_pages_breaker.test.tsx` (the More menu now lists Replay after Reports), and three
  earlier pins the contracts change by design: `tests/api/test_system.py` (alembic revision `0006`),
  `tests/db/test_migration_0002.py::test_keys` (the partial unique index replaces the constraint) and
  `tests/test_phase3_contracts.py` (`DailySummaryView` gains `run_to_date`).
- `make_services` gives `replays=FakeReplayLauncher()` by default (`replays=None` for the 503 case).
- Fakes: `tests/fakes_replay.py` also has `FakeReplayBroker`, `EngineCall`, `ReplayWorld` and `shifted`;
  `FakeReplayEngine(clock, broker=None, *, hook=None)` records each call with the clock time and runs the hook.
- `tests/test_phase5_contracts.py` accepts a stub that no longer raises, and only requires that one still
  raising `NotImplementedError` names its owner, so T2-T16 never edit it (one owner per file). Every stub
  raised its owner's name when T1 was committed.

**LIVE step:** apply migration 0006 to `trader_dev` outside 09:15–16:30 ET on a session day and not within 10 minutes of a cron line (soak protection): `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev alembic upgrade head` → `Running upgrade 0005 -> 0006` (or the numbers the build-time `alembic heads` check chose); `... alembic current` → `0006 (head)`; `... alembic check` → no new operations. The running `trader-dev` container keeps working (the new columns are nullable or defaulted). Record it in the activity log.

---

### Task P5-T2: Metrics module behind `/api/metrics`

**Goal:** One definition of every performance number (expectancy in R, win rate, profit factor, max drawdown, average slippage, adherence), computed per run and date range from the rows and matching hand calculations, used by `/api/metrics`, the replay comparison and the weekly report. BR-52, O4; SPEC §10 (`v_trade_metrics`), §11 (`/metrics`), §12 (Performance); Phase 4 open question 3.

**Files:** `trader/reports/metrics.py`, `trader/api/routers/performance.py`, `tests/reports/test_metrics.py`, `tests/api/test_performance_phase5.py`.

**Interfaces:**
- Consumes: `Trade`, `EquitySnapshot`, `Journal` models; `metrics_out`, `MetricsOut`, `resolve_run`, `check_range` (T1, P4-T7).
- Produces in `trader.reports.metrics` (dataclasses from T1): `TradeRow(pnl: Decimal, pnl_r: Decimal | None, qty: int, slippage_total: Decimal, fees_total: Decimal)`; `HistogramBin(lo: Decimal, hi: Decimal, count: int)`; `Metrics(run_id, date_from, date_to, trades, wins, losses, win_rate, avg_win_r, avg_loss_r, expectancy_r, profit_factor, avg_slippage, avg_slippage_per_share, max_drawdown_pct, adherence_pct, total_pnl, total_fees, trades_without_r, r_histogram: tuple[HistogramBin, ...])`; `R_LOW = Decimal("-3.0")`, `R_HIGH = Decimal("5.0")`, `R_BINS = 16`; `metrics_from_rows(run_id: int, date_from: date | None, date_to: date | None, trades: Sequence[TradeRow], equity: Sequence[Decimal], answers: Sequence[bool | None]) -> Metrics` (pure); `r_histogram(values: Iterable[Decimal]) -> tuple[HistogramBin, ...]` (pure); `max_drawdown(equity: Sequence[Decimal]) -> Decimal | None` (pure); `compute_metrics(factory, run_id: int, date_from: date | None = None, date_to: date | None = None) -> Metrics`.
- Changes `trader.api.routers.performance`: `GET /api/metrics` returns `metrics_out(compute_metrics(...))` (same route, parameters, errors and wire format); the SQL constants `_VIEW_SQL`, `_RANGED_SQL`, `_HISTOGRAM_SQL` and the old `compute_metrics` / `histogram_bins` are removed (the route keeps any name P4-T18 left in use, re-exported from the module).

**Behaviour and decisions (all ratios rounded to 4 dp half-up; money summed exactly):**
- Rows: the run's `trades` with `session_date` in the inclusive range, ordered by `closed_at`, then id; the run's `equity_snapshots` whose America/New_York date is in the range, ordered by `ts`; the run's `journal` rows in the range.
- `trades` = count; `wins` = `pnl > 0`; `losses` = `pnl ≤ 0`; `win_rate` = wins / trades (None with no trades).
- `expectancy_r` = mean `pnl_r` over trades that have one; `avg_win_r` / `avg_loss_r` = the same over wins / losses; `trades_without_r` = trades with a null `pnl_r` (so a missing planned risk is visible, never silently dropped).
- `profit_factor` = sum of positive `pnl` / −(sum of negative `pnl`); None when there is no losing P&L.
- `avg_slippage` = mean `slippage_total` (dollars per trade, as the view); `avg_slippage_per_share` = sum `slippage_total` / sum `qty`.
- `max_drawdown_pct` = the largest `(peak − equity) / peak` over the snapshots in range, where `peak` is the running maximum of equity from the first snapshot in range; None without snapshots. For the whole run this equals the stored `drawdown_pct` maximum (the broker's snapshot uses the same running peak).
- `adherence_pct` = followed / answered over journal rows whose `rules_followed` is not null; None when nothing was answered.
- `total_pnl` = sum `pnl` (0 with no trades); `total_fees` = sum `fees_total`.
- `r_histogram`: the P4-T7 wire format exactly (18 bins: open-ended below −3 R, sixteen `[lo, hi)` bins of 0.5 R, open-ended 5 R and above), counting trades with a `pnl_r`.
- A run with no rows gives zeros and Nones, never a division error.

**Acceptance tests:**
- [x] 1. Hand calculation: trades (+2R, P&L 20), (−1R, −10), (+0.5R, 5), (−1R, −10), (null R, −3) → trades 5, wins 2, losses 3, win rate 0.4000, expectancy 0.1250 (over four), avg win 1.2500, avg loss −1.0000, profit factor 25/23 = 1.0870, total P&L 2, `trades_without_r` 1.
- [x] 2. Equity 100, 110, 99, 105, 88 → max drawdown (110 − 88) / 110 = 0.2000; a single snapshot → 0; none → None.
- [x] 3. Journal answers Yes, No, Yes, not answered → adherence 0.6667; slippage totals 0.30, 0.10 with quantities 10 and 30 → average 0.2000 per trade and 0.0100 per share.
- [x] 4. `compute_metrics` over the whole run equals `v_trade_metrics` for trades, wins, win rate, expectancy, avg win and loss R, profit factor, average slippage, max drawdown and adherence on a seeded run (the view is read in the test).
- [x] 5. A date range that excludes the first trade gives the other trades' values; the range's drawdown uses only the snapshots in range; an empty range gives zero trades and None ratios.
- [x] 6. `r_histogram`: +2R in `[2.0, 2.5)`, −1R in `[−1.0, −0.5)`, −7R in the open first bin, exactly 5R in the open last bin; 18 bins always.
- [x] 7. The P4-T7 `/api/metrics` tests pass unchanged; `GET /api/metrics?run=<replay id>&from=&to=` returns the replay's numbers with the four new fields; the kill switch's expectancy (`KillSwitches.inputs(...).expectancy_r`) equals `expectancy_r` for the same trades.
- [x] 8. Gate and commit `P5-T2: ...`.

**Build notes (P5-T2 builder, 2026-09-27; trunk 8ddff8e):**
- Names P4-T18 left in use are kept in `trader/api/routers/performance.py`: `compute_metrics(factory, run_id,
  date_from, date_to) -> MetricsOut` (now `metrics_out(trader.reports.metrics.compute_metrics(...))`, used by
  `tests/api/test_routes_sweep.py`) and `histogram_bins(counts)` (now built from the new
  `trader.reports.metrics.histogram_from_counts`, used by `tests/api/test_performance.py`); `check_range`,
  `thin` and `close_when_done` are unchanged. `_VIEW_SQL`, `_RANGED_SQL`, `_HISTOGRAM_SQL` and `_METRIC_FIELDS`
  are gone. Extra public names in the metrics module: `R_WIDTH`, `histogram_from_counts`.
- Plan conflict (fixture only): P4-T7's `test_metrics_range_and_empty_range` seeded snapshots with a flat
  equity of 10000 and hand-set `drawdown_pct` values, and asserted the ranged drawdown was the stored maximum
  (0.03). The plan's definition computes the drawdown from the equity in range, so that fixture gave 0. The
  fixture now carries consistent equity (12000, 10000, 9700) that gives the same asserted 0.03 in range (and
  would give 0.1917 if the range leaked); the assertion is unchanged.
- The whole-run equality with `v_trade_metrics` holds because `SimBroker.snapshot_equity` uses the same
  running peak and rounds each point to 4 dp (rounding is monotonic, so the max of the rounded values is the
  rounded max).

---

### Task P5-T3: Replay clock and candle fill model

**Goal:** A monotonic replay `Clock` and the SPEC §7.4 candle fill model, including the half-spread estimate and gap-through handling, implementing the P2 `FillModel` protocol. SPEC §3a (Clock), §7.4; master plan §7.1 (Clock, Fill model).

**Files:** `trader/replay/clock.py`, `trader/replay/candle_fill_model.py`, `tests/replay/test_clock.py`, `tests/replay/test_candle_fill_model.py`.

**Interfaces:**
- Consumes: `Clock`, `Candle`, `QtQuote`, `OrderSpec`, `FillDecision`, `NoFill`, `Fees`, `FillParams`, `Q4`.
- Produces in `trader.replay.clock`: `ReplayClock(start: datetime)` with `now() -> datetime`, `set(at: datetime) -> None` (UTC-aware only; `ValueError` when `at` is before the current time), `advance(delta: timedelta) -> None` (`ValueError` for a negative delta).
- Produces in `trader.replay.candle_fill_model`: `CandleFillModel(params: FillParams, half_spread_bps: Decimal)` with `slip(price) -> Decimal`, `half_spread(price) -> Decimal`, `fees(side, qty, price) -> Fees` (identical to `QuoteFillModel.fees`), `evaluate(order, market, now) -> FillDecision | None`, `assess(order, market, now) -> FillDecision | NoFill`; `CANDLE_SNAPSHOT_SOURCE = "candle_1m"`.

**Behaviour and decisions:** the rules of the decision "Candle fill model" above, in full. Also:
- `assess` raises `TypeError` for a `QtQuote` (the mirror of `QuoteFillModel`), and does not check the bar's time against the order (the broker does, T4).
- Trigger names in `FillDecision.trigger`: `market`, `stop`, `stop_gap` (the open was already through the stop), `stop_limit`, `limit`.
- `quote_snapshot`: `{"source": "candle_1m", "start", "end", "open", "high", "low", "close", "volume", "half_spread", "evaluated_at"}` (Decimals as strings, times ISO).
- A computed price that is not positive → `NoFill("no_bid")` (as the quote model).

**Acceptance tests (pure):**
- [x] 1. `ReplayClock` moves forward with `set` and `advance`, refuses a naive datetime, an earlier time and a negative delta; it satisfies `Clock` (mypy).
- [x] 2. Two models built with the same params give identical decisions for the same inputs, and a decision never depends on `now` except for `evaluated_at`.
- [x] 3. Buy stop 10.20, bar O 10.00 H 10.35 L 9.95 → fills at 10.20 + slip(10.20) + hs(10.20) = 10.20 + 0.01 + 0.0051 = 10.2151 (defaults), trigger `stop`, slippage 0.01, half spread 0.0051 in the snapshot.
- [x] 4. Buy stop 10.20, bar opening at 10.40 (gap up) → reference 10.40, fills at 10.40 + 0.01 + 0.0052 = 10.4152, trigger `stop_gap`.
- [x] 5. Sell stop 9.80, bar O 9.90 L 9.70 → 9.80 − 0.01 − 0.0049 = 9.7851; bar opening at 9.60 (gap through) → 9.60 − 0.01 − 0.0048 = 9.5852 (worse than the stop).
- [x] 6. A bar whose high stays below the buy stop, and one whose low stays above the sell stop → `NoFill("not_triggered")`.
- [x] 7. Market buy and sell use the open plus/minus slip and half spread; slippage uses `max(slippage_min, bps × price)` (a 250.00 price gives 0.125 → 0.1250).
- [x] 8. Buy stop-limit: triggered with a price above the limit → `NoFill("above_limit")`; within the limit → fills. Limit buy 10.00 with low 9.99 and `hs` 0.005 → fills at exactly 10.00 with zero slippage, also when the bar opens at 9.90 (never better than the limit, as the quote model); with low 9.999 → `not_triggered`. The sell limit mirrors it.
- [x] 9. Zero volume → `no_volume`; `low > high` or a non-positive price → `bad_bar`; a `QtQuote` → `TypeError`.
- [x] 10. Fees: the SEC fee on sells only, rounded to 4 dp, identical to `QuoteFillModel.fees` for the same params.
- [x] 11. Gate and commit `P5-T3: ...`.

**Build notes (P5-T3 builder, 2026-09-27; trunk 8ddff8e):**
- `ReplayClock` accepts any timezone-aware datetime and stores it in UTC (as `FixedClock`); setting the same time
  again is allowed (bars then events at `t`).
- `CandleFillModel` delegates `slip` and `fees` to a `QuoteFillModel` built from the same `FillParams`, so both
  are identical to the quote model by construction.
- The bar's open is **not** checked against its high/low range (only non-positive prices, `low > high` and a
  negative volume are `bad_bar`): T4's same-bar pass reopens the bar at the entry fill price, which can lie
  above the high after slippage and half spread; a test pins that this still stops out at `stop − slip − hs`.
- `stop_gap` means the open was strictly through the stop (an open equal to the stop is `stop`, same price).
  A stop-limit keeps the trigger `stop_limit` also on a gap. The sell stop-limit mirrors the buy one
  (`NoFill("below_limit")` when `ref − slip − hs` is under the limit).

---

### Task P5-T4: Replay hooks in the engine: `SimBroker.on_candles`, `Engine.on_candles` (same-bar worst case), audit-free auto approvals, replay-scoped strategy configs

**Goal:** The small, additive changes to Phase 2 code that replay needs: the candle twin of the broker's quote loop, the engine's candle entry point with the same-bar worst-case pass, a proposal service that can skip the auto-approval audit row, and strategy config rows scoped to a replay. SPEC §7.4, §8; master plan §7.1 (Broker, Engine, Proposals); contract refinements 1–4.

**Files:** `trader/broker/sim_broker.py`, `trader/engine/orchestrator.py`, `trader/engine/proposals.py`, `trader/strategies/registry.py`, `tests/broker/test_sim_broker_candles.py`, `tests/engine/test_engine_candles.py`, `tests/engine/test_proposals_audit_auto.py`, `tests/strategies/test_registry_scope.py`.

**Interfaces:**
- Consumes: the `FillModel` protocol (tests use a fake candle model that fills at the bar's open for triggered orders, and one run with the real `CandleFillModel` once T3 is on trunk, marked to skip while it is a stub); `StrategyConfigView.scope`, the T1 stubs.
- Produces: `SimBroker.on_candles(candles: Mapping[int, Candle], now: datetime, *, orders: Collection[int] | None = None) -> list[FillEvent]`; `Engine.on_candles(candles: Mapping[int, Candle], now: datetime) -> list[FillEvent]`; `ProposalService(..., audit_auto: bool = True)`; `StrategyRegistry.create_replay_config(key, *, base, params, enabled, created_by) -> StrategyConfigView`; `_latest` filtered to `scope == "live"`.

**Behaviour and decisions:**
- **`SimBroker.on_candles`** runs the same per-order loop as `on_quotes` (the same locks, lock order, savepoint per order, orphan-sell and buying-power checks; the quote-staleness bookkeeping does not apply to candles), with these differences: an order is considered only when its symbol has a candle and `candle.end > order.submitted_at` (unless its id is in `orders`, which also limits the pass to those ids); regular hours and the entry cutoff are checked against `candle.start` (`session_open ≤ start < session_close`; an entry whose `candle.start ≥ cutoff` is cancelled with reason `entry cutoff`); every timestamp written (fill `ts`, `closed_at`, ledger `ts`, trade `closed_at`) is `now`. `NoFill` outcomes other than `not_triggered` / `above_limit` / `below_limit` are logged once per order at `info` (a bad bar is data, not an outage).
- **`Engine.on_candles`**: `broker.on_candles(candles, now)`, then `_after_fill` for each fill exactly as `on_quotes` does (the same error isolation and alerts), then the **same-bar pass**: for each entry fill of this call whose position now has a working stop order, call `broker.on_candles({symbol: bar with open replaced by the entry fill's price}, now, orders=[stop order id])` and run `_after_fill` for any fill it returns. Returns every fill in order. `on_quotes` is unchanged.
- **`audit_auto=False`** skips only the automatic audit rows: `proposal.auto_approve` in `create` and `proposal.auto_execute_on_expiry:<kind>` in `_expire` (unreachable in auto mode, skipped for safety); the proposal's `decided_via = "auto"`, `decided_by` and its event are unchanged. Every human audit row (`proposal.approve` / `reject`) is written as today.
- **Scoped configs:** `_latest` (and therefore `current`, `enabled`, `instance`, `update`, `ensure_defaults`) only sees `scope = 'live'` rows. `create_replay_config` validates `{**base.params, **params}` with the plug-in's model, writes a row with `scope = 'replay'`, `revision = base.revision`, `version = base.version`, `created_by` as given, and no audit row (the replay's `replay.start` audit row describes it); it refuses a `base` that is not a `live` row of that key. `config_ids(key)` and `config_key(id)` include replay rows (so `on_fill` and `_event_strategies` resolve a replay's configs).

**Acceptance tests (real DB; fake candle model unless stated):**
- [x] 1. A buy stop submitted at 09:35:05 gets the 09:35–09:36 bar (end after submission) and fills with `ts = now`; a stop submitted at exactly 15:50:00 ignores the 15:49 bar (end equal to submission) and fills on the 15:50 bar.
- [x] 2. A bar starting at 15:59 fills a working exit (inside regular hours) with `ts` 16:00:00; a bar starting at 16:00 fills nothing; an entry whose bar starts at or after `close − no_entry_before_close_minutes` is cancelled (`entry cutoff`), never filled.
- [x] 3. **Same-bar worst case:** one bar that triggers the buy stop entry (high ≥ entry) and has `low ≤ stop_loss`: `Engine.on_candles` returns the entry fill and then the protective stop's fill in the same call; the trade's `exit_reason` is `protective_stop`, its P&L is negative, and the stop fill price is `stop − slip − hs` with the real `CandleFillModel` (skipped while T3 is a stub).
- [x] 4. The same bar with `low > stop_loss` → only the entry fills, and the stop keeps working into the next bar.
- [x] 5. Two symbols in one call: each order gets its own symbol's bar; an order whose symbol has no bar is untouched; a failing order (fake model raising) is rolled back alone with one `error` event, and the other fills.
- [x] 6. `on_quotes` behaviour is unchanged (the P2 broker and engine tests pass), and a `QuoteFillModel` broker given candles raises `TypeError` from the model inside the per-order savepoint (logged, not crashing the call).
- [x] 7. `ProposalService(audit_auto=False)` in auto mode: the proposal is `auto_approved` / submitted with `decided_via = "auto"` and no `audit_log` row; with the default `True` the P2 audit row is still written.
- [x] 8. `create_replay_config` writes a `replay` row (revision equal to the base's) that `current`, `enabled`, `instance` and `update` never return; `update` of the live config afterwards creates live revision n+1 without conflict; invalid params raise `ValidationError`; a `replay` base is refused.
- [x] 9. `ensure_defaults` after a replay row exists creates or skips live rows exactly as before; `config_key(<replay row id>)` returns the key.
- [x] 10. Gate and commit `P5-T4: ...`.

**Build notes (P5-T4 builder, 2026-09-27; trunk 8ddff8e):**
- `on_quotes` and `on_candles` share one per-order loop body (`SimBroker._guarded`, the savepoint and error
  event) and one `_apply`, which gains the keywords `at` (the time the entry cutoff is checked against: the
  bar's start for candles) and `in_hours`; for quotes both default to exactly the P2 values.
- A bar's `NoFill` other than `QUIET_CANDLE_NO_FILL` (`not_triggered`, `above_limit`, `below_limit`) is logged
  once per order per broker instance (an in-memory set, so one replay process logs it once); no staleness
  bookkeeping (`stale_since` stays null).
- `Engine.on_candles` finds each entry's stop through `open_positions()` (`stop_order_id`) and checks it is
  still working; the reopened bar only changes `open` (`dataclasses.replace`). `on_quotes` now calls the same
  `_follow_up` helper, with unchanged behaviour.
- `create_replay_config` raises `ValueError` ("not a live ... config") for a base that is missing, of another
  key or `replay`-scoped, `KeyError` for an unknown plug-in, `ValidationError` for bad params; its params base
  is the stored live row's (not the caller's copy). Constants `LIVE_SCOPE` / `REPLAY_SCOPE` in the registry.
- The real-`CandleFillModel` variant of test 3 is skipped while P5-T3's model is a stub; it runs as soon as
  T3 lands (`tests/engine/test_engine_candles.py`).
- For T7: `api/feed.py`'s `strategies` watermark is `max(strategy_configs.id)`, so a new `replay` row moves it
  until T7 filters it (T7 test 9).

---

### Task P5-T5: Replay data source and stored catalysts

**Goal:** Everything a strategy reads during a replay, from the archive and caches first and Questrade second, with no lookahead and no writes; and the read-only catalyst source. SPEC §8 (data, candle archive, biased universe, `replay_catalyst_mode`), §4.1 (history depth); Review Focus 1–2.

**Files:** `trader/replay/data.py`, `trader/replay/catalysts.py`, `tests/replay/test_data.py`, `tests/replay/test_catalysts.py`.

**Interfaces:**
- Consumes: `MarketDataView`, `CatalystSource`, `CatalystInfo`, `ReplayMarket` (T1), models `UniverseSnapshot`, `Symbol`, `OpenBarStat`, `CandleArchive`, `IntradayCandle`, `DailyCandle`, `JobRun`, `Catalyst`; `QuoteClient` (a `QuestradeClient` built by T6 with a real clock and `replay.questrade_rps`, or `None` offline) and `trader.adapters.questrade.client.MAX_CANDLES_PER_REQUEST`; `indicators.atr/average_volume/opening_bar/regular_hours`; `CatalystStore.get`, `StoredCatalyst`.
- Produces in `trader.replay.data`: `ReplayData(factory, clock: Clock, wall: Clock, calendar: SessionCalendar, client: QuoteClient | None, *, run_id: int, date_from: date, date_to: date, half_spread_bps: Decimal, questrade_window_days: int, lookback_sessions: int)` implementing `ReplayMarket` (all of `MarketDataView`: `universe`, `universe_status`, `open_bar_stats`, `opening_bars`, `quotes`, `candles`, `prior_close`, `prior_closes`, `symbol_ids`; plus `prepare_day`, `load_minute_bars`, `bar_ending_at`, `last_close`, `progress_counts`, `biased_days`); `BIASED_SOURCE = "biased"`.
- Produces in `trader.replay.catalysts`: `ReplayCatalysts(factory, mode: CatalystMode)` implementing `CatalystSource`; `UNKNOWN_REASON = "replay: no stored catalyst"`.

**Behaviour and decisions:** the decision "Replay data" above, plus:
- `prepare_day(d)`: loads the universe, its opening bars (every member; from Questrade, per symbol, `FiveMinutes` over `[first session − lookback, date_to]` in windows of at most `MAX_CANDLES_PER_REQUEST` intervals, fetched once per symbol, keeping only each session's opening bar for the rest of the run), the stats and SPY's 1-minute bars for `d`; the previous day's 1-minute bars are released. Missing opening bars are reported per symbol in `opening_bars().missing` (`no_archived_bar` offline or outside the window, `questrade_error: HTTP <status>`, `no_bar_at_open`) and counted in `missing_opening_bars`.
- `load_minute_bars(ids, d)`: loads each symbol's regular-hours 1-minute bars for `d` once (archive → cache → one Questrade `OneMinute` request); a symbol with none adds one to `missing_minute_bars`.
- **No lookahead:** `opening_bars`, `candles`, `quotes`, `prior_close(s)` and `open_bar_stats` only use bars whose `end ≤ clock.now()` (and daily bars before the session). `bar_ending_at` and `last_close` are runner-only helpers and also never return a bar ending after `at`.
- **Synthetic quotes:** for each requested symbol with a complete 1-minute bar today, a `QtQuote` with `symbol_id` = the DB id, `last` = close, `bid` = close − hs, `ask` = close + hs, `last_trade_time` = the bar's end, `delay = 0`, `is_halted = False`; symbols without one are absent.
- **Universe status:** a stored universe → as `MarketDataService.universe_status`; a biased day → `UniverseStatus(source="biased", fallback_from=None, stale=False, age_sessions=None)`.
- **Questrade window:** a session older than `wall` date − `questrade_window_days` is never fetched; `progress_counts()` includes `questrade_requests`.
- **No writes** to any table; errors from Questrade become "missing" counts and a `warning` event with the replay's `run_id` (at most one per day and kind), never an exception to the strategy.
- `ReplayCatalysts.get(ids, d)`: `stored` → the stored rows (`CatalystStore.get`), and a `StoredCatalyst(symbol_id, "unknown", "neutral", None, None, UNKNOWN_REASON, None, Decimal(0), False)` for a missing id; `unknown` → that for every id. It never writes.

**Acceptance tests (real DB seeded with `seed_replay_world`; Questrade through the in-memory `FakeQuestrade` in `tests/fakes_questrade.py`, which records its calls):**
- [x] 1. A session with a stored universe and stats returns them exactly; a session without a snapshot returns the newest stored universe with every member `source = "biased"`, `universe_status().source == "biased"`, and the day in `biased_days`.
- [x] 2. Opening bars come from `candle_archive` (5m) when present, else `intraday_candles`, else Questrade (one request per symbol for the whole range); offline or outside the window they are `missing` with `no_archived_bar`.
- [x] 3. **No lookahead:** at 09:35:04 the 09:30–09:35 bar is returned; at 09:34:59 it is not; `candles(sid, open, close, "OneMinute")` at 10:00 returns only bars ending by 10:00; `quotes` at 10:00:30 uses the 09:59–10:00 bar.
- [x] 4. Stats for a biased day are computed from the prior `open_bar.lookback_sessions` opening bars and 14 daily bars and equal the nightly job's values for the same inputs (the nightly helper is called in the test).
- [x] 5. Two `ReplayData` over the same seeded data and the same fake Questrade responses return identical values for every method (determinism), with ids sorted.
- [x] 6. A synthetic quote has `bid < last < ask` by `hs`, `delay = 0` and `last_trade_time` = the bar end; `spy_overlay`'s stale check accepts it at 15:30:05.
- [x] 7. A Questrade 500 for one symbol leaves that symbol missing (`questrade_error: HTTP 500`), counts it, writes one `warning` event with the replay's `run_id`, and the other symbols load.
- [x] 8. **No writes:** row counts of every table are unchanged after a full day of reads, fetches included.
- [x] 9. `ReplayCatalysts`: `stored` returns a stored row as is and `unknown` (unclassified, reason `UNKNOWN_REASON`) for a missing one; `unknown` mode returns `unknown` for a stored one; no `catalysts` row is written.
- [x] 10. **Request size and memory:** a 130-session full-mode range asks Questrade for `FiveMinutes` bars in windows of at most `MAX_CANDLES_PER_REQUEST` intervals (the recorded calls), each symbol's range once; a fake response of 78 bars per session leaves one kept bar per session; after `prepare_day` of day 2, day 1's 1-minute bars are no longer held; `quotes` for a symbol whose minute bars were not loaded loads them once and answers from the last complete bar.
- [x] 11. Gate and commit `P5-T5: ...`.

**Build notes (P5-T5 builder, 2026-09-27; trunk 8ddff8e):**
- The Questrade `FiveMinutes` range per symbol spans the sessions still missing from the archive and cache
  (inside the Questrade window), not always the whole range: a symbol whose range days are archived asks only
  for its look-back. Still one range per symbol, split into windows of at most `MAX_CANDLES_PER_REQUEST`.
- Fetches go through `client.candles_many` (one symbol's windows at a time, reduced at once); any failure is
  that request's `questrade_error: HTTP <status>` (0 for a transport or parse failure). Warning events:
  source `replay.data`, kinds `opening_bars`, `minute_bars`, `daily_bars`, at most one per day and kind.
- Daily bars: `daily_candles` then one `OneDay` request per symbol over `[date_from - 40 days, date_to]`
  (the in-window part); older daily and opening bars are pruned in `prepare_day`. `held_counts()` (extra
  helper) reports the bars held in memory.
- A biased day's members keep only the names of the newest stored universe (`price`, `avg_volume`,
  `atr14` are `None`, since the snapshot's numbers are from a later date); the stats are computed with the
  nightly formulas (`jobs.nightly.MIN_OPENING_BARS`, `DAILY_LOOKBACK`, `atr`, `average_volume`).
- Universe, universe status, stored stats and symbol ids are read through a `MarketDataService` whose client
  refuses every fetch; `prior_close(s)` returns nothing until the previous session has closed on the replay
  clock; `candles()` never fetches (current-day 1-minute bars, archive, cache, kept opening bars).
- `ReplayCatalysts` uses `CatalystStore.get` with a clock that refuses to be read; `unknown_catalyst(sid)` is
  exported for T6/T18.

---

### Task P5-T6: Replay runner: create, pin, snapshot, step, progress, cancel, CLI composition

**Goal:** Create a replay run from a request (validated, settings snapshot with automatic approvals, strategies pinned), run it deterministically through the real engine session by session, record progress, honour cancellation, and settle abandoned runs. BR-54, O5; SPEC §8, §16 (determinism); Review Focus 1–2.

**Files:** `trader/replay/setup.py`, `trader/replay/runner.py`, `tests/replay/test_setup.py`, `tests/replay/test_runner.py`.

**Interfaces:**
- Consumes: T1 types; `ReplayClock`, `CandleFillModel` (T3), `ReplayData`, `ReplayCatalysts` (T5), `Engine`, `SimBroker`, `Ledger`, `ProposalService(audit_auto=False)`, `RiskManager`, `KillSwitches`, `StrategyRegistry.create_replay_config` (T4), `day_plan`, `ensure_sim_account`, `SettingsStore`, `RuntimeSettings`, `FillParams`, `QuestradeClient`, `runtime.questrade_auth`, `RealClock`, `EnvSettings.app_version`. Tests use `FakeReplayEngine` / `FakeReplayMarket` for the loop and inject `config_writer` for overrides, so T3–T5 stubs are never called.
- Produces in `trader.replay.setup`: `SnapshotSettings(settings: RuntimeSettings)` (a `SettingsStore` subclass: `load()` returns the frozen snapshot, `set()` raises `RuntimeError("replay settings are read-only")`); `PinnedRegistry(factory, clock, pinned: Mapping[str, StrategyConfigView], run_id: int, plugins: Mapping[str, type[Any]] | None = None)` (a `StrategyRegistry` subclass: `keys()` = the pinned keys, `current(key)` = the pinned view, so `enabled()` and `instance(key)` (which the engine calls) build from the pinned views and never read a live row; `update` / `ensure_defaults` raise `RuntimeError`; `_report` writes its event with the replay's `run_id`); `build_replay_engine(factory, clock: ReplayClock, calendar, run: ReplayRun, market: ReplayMarket, catalysts: CatalystSource, registry: PinnedRegistry) -> Engine`.
- Produces in `trader.replay.runner`: `REPLAY_LOCK = "trader.replay"`; `create_replay(factory, wall: Clock, calendar, settings: SettingsStore, registry: StrategyRegistry, request: ReplayRequest, actor: str, *, config_writer: Callable[..., StrategyConfigView] | None = None, app_version: str = "dev") -> int` (raises `ReplayInvalid`, `ReplayBusy`); `@dataclass(frozen=True) ReplayDeps(factory, wall: Clock, calendar, market_factory: Callable[[ReplayRun, ReplayClock], ReplayMarket], catalysts_factory: Callable[[ReplayRun], CatalystSource], engine_factory: Callable[[ReplayRun, ReplayClock, ReplayMarket, CatalystSource], ReplayEngine])`; `async run_replay(deps: ReplayDeps, run_id: int) -> ReplayRun` (the final state); `request_cancel(factory, wall, run_id: int, actor: str) -> bool`; `reconcile_abandoned(factory, wall) -> list[int]`; `@asynccontextmanager open_replay_deps(core: Core, *, data_mode: DataMode) -> AsyncIterator[ReplayDeps]` (the real composition: `ReplayData` with a `QuestradeClient(questrade_auth(core), RealClock(), market_rps=settings.replay_questrade_rps)` in `full` mode and `None` offline, `ReplayCatalysts`, `build_replay_engine` with a `PinnedRegistry`).

**Behaviour and decisions:**
- **Validation** (`ReplayInvalid` with every problem, field paths like `date_to`, `overrides.risk_pct`, `strategies.orb_sip.params.top_n`): `date_from ≤ date_to`; `date_to` before the wall-clock ET date, or equal to it only after that session's close + 15 min; the range holds at least one session and at most `replay.max_sessions`; `label` ≤ 200 characters; override keys in `REPLAY_OVERRIDE_KEYS` and each value valid for `RuntimeSettings`; strategy keys known to the registry, params valid for the plug-in's model (merged over the live params), and at least one `entry` strategy enabled.
- **Busy:** `reconcile_abandoned` first; then any replay row `queued` or `running` → `ReplayBusy`.
- **Create** (one transaction): the settings snapshot = the live `SettingsStore.load()` with the overrides applied and `approval_mode = "auto"`; the pinned strategies (live current rows, or `config_writer(...)` rows for overrides, `created_by = "replay:<run id>"`; the run row is inserted first to get its id); `data_mode` = `offline` when `request.offline` or the wall clock is in 09:15–16:30 ET of a session day, else `full`; the `runs` row (`mode = "replay"`, `status = "queued"`, `started_at = updated_at = wall now`, `label` = the request's or `replay <from>..<to>`, params as T1 fixes); the audit row `replay.start` (actor as given, after: from, to, overrides, strategies, data mode).
- **Run:** take the `REPLAY_LOCK` session advisory lock on a dedicated connection for the whole run (not acquired → `ReplayBusy`); load the run (must be `queued`); set `running`; create the sim account with the snapshot's starting cash at the first session's open − 1 minute (replay clock); then for each session in the range, in order: stop with `cancelled` if `cancel_requested`; `market.prepare_day`; `day_plan(enabled pinned strategies, calendar, day, snapshot)`; the time-ordered loop of the decision "The replay loop" (minute boundaries only while `engine.broker.working_symbol_ids()` is non-empty; `market.load_minute_bars` for every working symbol before its first bar is needed; bars before events at the same instant; `tick` at every visited time); the forced close of the decision "Forced close" (at `close − 1 minute` and at the close, through `engine.broker` and `engine.on_candles`); `end_of_session` at the close; then update `progress` (sessions done, current date, trades so far, forced closes, the market's counts and biased days) and `updated_at`. At the end: `completed` (or `cancelled`), `finished_at`, the label gains " (biased universe)" when any day was biased. Any exception: `failed` with a redacted one-line `error`, one `error` event with the replay's `run_id` (never relayed), the lock released.
- **Cancel:** `request_cancel` sets `cancel_requested` on a `queued` or `running` replay (audit `replay.cancel`), returns False otherwise; a `queued` run is set `cancelled` at once.
- **Abandoned:** `reconcile_abandoned` tries the `REPLAY_LOCK`; when it is free, every `running` replay, and every `queued` one older than 2 minutes, becomes `failed` with error `abandoned` (and `finished_at`).
- The engine is built fresh per run (not per day); kill switches, the ledger and equity snapshots carry across days as in live.

**Acceptance tests (real DB for create/cancel/abandon/snapshot; the loop with `FakeReplayEngine` and `FakeReplayMarket`):**
- [x] 1. `create_replay` writes a `queued` replay with the params shape, `approval_mode = "auto"` in its settings, the overrides applied, the live strategy rows pinned, one `replay.start` audit row; the global `approval_mode` setting and the live strategy revisions are unchanged.
- [x] 2. An override of `orb_sip.top_n` pins a `replay`-scoped row through `config_writer` with `created_by = "replay:<id>"`; the live `current("orb_sip")` still returns the old revision.
- [x] 3. Validation: from after to, a future `to`, today before 16:15 ET, an empty range (a holiday weekend), more than `replay.max_sessions`, an unknown override key, an invalid override value, an unknown strategy, invalid plug-in params and no enabled entry strategy each give `ReplayInvalid` naming the field; nothing is written.
- [x] 4. **Loop order** (fake engine): for a normal day with events at 09:35:05, 11:30:00, 15:30:00, 15:50:00 and an entry order working from 09:35:05 to 10:02, the recorded calls are: bars at each minute boundary 09:36–10:02 only, `run_event` at each event time after the bars of the same instant, `tick` at every visited time, `end_of_session` at the close; on 2026-11-27 (13:00 close) the events follow the early close.
- [x] 5. **Determinism** (fakes): two runs of the same request produce identical recorded call sequences (each call recorded with the `ReplayClock` time, which equals the visited time) and identical progress JSON.
- [x] 6. Progress is written after every session (`sessions_done` 1..n, `current_date`); a cancel requested during day 2 (fake engine flips it) ends with `cancelled` after day 2, `finished_at` set.
- [x] 7. A fake engine raising on day 2 → `failed`, `error` is one masked line, one `error` event with the replay's `run_id`, the lock is released (a new run can start).
- [x] 8. `SnapshotSettings.set` raises; `PinnedRegistry.update` and `ensure_defaults` raise; a plug-in whose params fail to build is reported with the replay's `run_id` (no `run_id`-less event).
- [x] 9. `ReplayBusy`: while one run holds the lock (a second connection), `create_replay` and `run_replay` of another refuse; `reconcile_abandoned` settles a `running` row whose lock is free and a `queued` row older than 2 minutes as `failed` (`abandoned`), and leaves a locked one alone.
- [x] 10. Data mode: created at 10:00 ET on a session day → `offline`; at 17:00 ET, or on a Saturday → `full`; `offline=True` → `offline`.
- [x] 11. Forced close: a position still open at `close − 1 minute` (fake market with no bars after 15:40) has its working orders cancelled and a market exit with reason `replay_forced_close` submitted, which the synthetic bar at the last close fills at the close; it is counted in `forced_closes`, and no position is open at the start of the next day.
- [x] 12. Gate and commit `P5-T6: ...`.

**Build notes (P5-T6 builder, 2026-09-27; trunk 8ddff8e):**
- `open_replay_deps` builds the Questrade auth as `QuestradeAuth(core.factory, core.crypto, core.clock)`, the
  same object `runtime.questrade_auth` returns, instead of importing `trader.runtime`, which imports the Telegram
  adapters (replay isolation). The client gets `RealClock()` and `market_rps = replay.questrade_rps`; the wall
  clock of the deps is `core.clock`.
- `ReplayDeps` keeps the six T1 fields; the day plans are built from the installed plug-ins (`load_all()`) with
  the pinned params, because the `ReplayEngine` protocol exposes no registry. Extra real names: `REPLAY_LOCK_KEY`
  (the signed 64-bit key of `REPLAY_LOCK`, blake2b like the worker's) and
  `trader.replay.setup.pinned_views(factory, run)` (the pinned `StrategyConfigView`s; the pinned params and
  enabled flag win over the row's).
- Busy: `create_replay` refuses when `REPLAY_LOCK` is held (after `reconcile_abandoned`) or any replay row is
  `queued`/`running` (checked under a transaction-level `trader.replay.create` lock). A strategy override writes its
  `replay` row through `config_writer` on its own connection before the run row commits (the T4 signature has no
  session), so "one transaction" holds for the run row, its params and the audit row; an orphan `replay` row
  after a failure is invisible to the live run. An `enabled`-only override that differs from the live flag also
  gets its own `replay` row, so the pinned row always matches what runs.
- `run_replay` of a run that is no longer `queued` (for example cancelled before it started) returns it unchanged.
  Cancel is checked before each session, so a cancel during the last session lets it complete.
- Forced close: the check runs at the first visited time at or after `close - 1 minute` (visited whenever a position
  is open); the synthetic bar's price is `market.last_close(sid, close)`, else the position's `avg_price`.
- The failure event (source `replay`, level `error`) is stamped by the replay clock once it exists.
- Tests use a stand-in monotonic clock only while P5-T3's `ReplayClock` is still a stub (autouse fixture), and
  check `registry.current` after an override only once P5-T4's `create_replay_config` is on trunk (the live row
  is also checked by query, so the test is meaningful before T4).

---

### Task P5-T7: Replay API, launcher, change-feed topics, live views without replay rows

**Goal:** Start, list, watch, compare and cancel replays from the web; run them in their own process; push progress over SSE; and keep replay rows out of the live Dashboard, SSE events and System lists. BR-54; SPEC §11 (`POST /replays`, `GET /replays`, `GET /replays/{id}`), §12 (Replay); contract refinement 8.

**Files:** `trader/api/routers/replays.py`, `trader/api/replay_launcher.py`, `trader/api/feed.py`, `trader/api/routers/dashboard.py` (events query only), `trader/api/routers/system.py` (events and errors queries only), `tests/api/test_replays.py`, `tests/api/test_replay_launcher.py`, `tests/api/test_feed_phase5.py`, `tests/api/test_live_views_exclude_replays.py`.

**Interfaces:**
- Consumes: `create_replay`, `request_cancel`, `reconcile_abandoned`, `load_replay_run`, `ReplayInvalid`, `ReplayBusy`, `ReplayNotFound`, `REPLAY_OVERRIDE_KEYS` (T1/T6; tests monkeypatch `trader.api.routers.replays.create_replay` and friends until T6 lands, and `compute_metrics` until T2 lands); `metrics_out`, `event_out`, schemas, `ApiServices.replays`, `CsrfUser`, `actor` (T1, P4).
- Produces in `trader.api.routers.replays`: `GET /api/replays/options` → `ReplayOptionsOut`; `GET /api/replays?limit=50` → `Items[ReplaySummaryOut]`; `GET /api/replays/{id}` → `ReplayOut`; `POST /api/replays` (body `ReplayIn`) → `ReplayOut` with status 202; `POST /api/replays/{id}/cancel` → `ReplayOut`.
- Produces in `trader.api.replay_launcher`: `SubprocessReplayLauncher(factory, clock, *, executable: str = "trader", spawn=asyncio.create_subprocess_exec)` implementing `ReplayLauncher` (`launch(run_id)` spawns `trader replay --run <id>`; `running()`).
- Changes `trader.api.feed`: `WATERMARK_TOPICS` gains `replays` (max replay `runs.id`, max replay `runs.updated_at`) and `reports` (max `weekly_reports.updated_at`); the `events` messages only carry rows whose `run_id` is null or the live run; the watermarks of the trading topics (`proposals`, `orders`, `fills`, `positions`, `trades`, `candidates`, `killswitch`, `events`) count only rows that are not of a replay run (`run_id` null or of a `live` run), so a running replay does not make every open live page refetch each poll; `strategies` ignores `replay`-scoped config rows.
- Changes `dashboard.py` and `system.py`: their `event_log` queries (Dashboard events, `/api/events`, the System errors list) add `run_id IS NULL OR run_id = <live run id>`.

**Behaviour and decisions:**
- **Options:** `override_keys` sorted; `max_sessions` from settings; `latest_allowed` = the last session whose close + 15 min has passed; `questrade_from` = wall ET date − `replay.questrade_window_days`; `archive_from` = the earliest `candle_archive` 1m `start_ts` date (null when empty); `snapshots_from` = the earliest `universe_snapshots.session_date`; `busy` = a replay `queued` or `running` after `reconcile_abandoned`; `offline_now` = a start now would be offline.
- **Start:** `ReplayIn` → `ReplayRequest`; `create_replay(..., actor(user), app_version=env.app_version)` in the thread pool; `ReplayInvalid` → 422 with `fields` (`loc` from the field path, never the input value); `ReplayBusy` → 409 "A replay is already running."; then `services.replays.launch(run_id)`; a spawn failure marks the run `failed` ("could not start: <type>") and returns 500 `internal`; `services.replays` None → 503 "Replays are not available". Response 202 with the new run.
- **Field mapping:** `created_at` is `runs.started_at` (the row's creation, wall clock); `finished_at`, `progress`, `error`, `cancel_requested` are the T1 columns; everything else comes from `load_replay_run`.
- **List:** replay runs only, newest first, `limit` 1–200; `trades`, `expectancy_r`, `total_pnl` from one grouped query over `trades`.
- **Get:** 404 for an unknown id or a live run; `metrics` = `compute_metrics(run)` and `live_metrics` = `compute_metrics(live run, date_from, date_to)` for a `completed` or `cancelled` run (null while `queued`/`running`); `events` = the run's last 20 `event_log` rows, newest first, via `event_out` (masked).
- **Cancel:** `request_cancel(...)`; not cancellable → 409 "The replay is not running."
- **Launcher:** the child gets the API's environment and inherited output (it reaches `docker logs`), is not awaited (reaped by a done-callback task, as P4-T9's launcher), and a non-zero exit writes one `warning` event (source `replay.launcher`, data: run id, exit code; never output) with the replay's `run_id`.
- Every change route needs the CSRF header and writes its audit row through `create_replay` / `request_cancel`.

**Acceptance tests (real DB; `make_client`; `FakeReplayLauncher`):**
- [x] 1. `POST /api/replays` with a valid body → 202, a `queued` run, `launch(run_id)` called once, the audit row with actor `web:stephen`; without the CSRF header → 403; without a session → 401.
- [x] 2. Invalid bodies (from after to, unknown override key, bad plug-in params) → 422 whose `fields` name the location and never echo the value; a busy system → 409.
- [x] 3. `GET /api/replays` lists replay runs only, newest first, with trade counts and totals; `limit` 201 → 422.
- [x] 4. `GET /api/replays/{id}` for a completed seeded replay returns its strategies, progress, `biased` true with its days, `metrics` of the replay and `live_metrics` over the same dates, and the last events; a running one has null metrics; the live run's id → 404.
- [x] 5. Cancel of a running replay → `cancel_requested` true; of a completed one → 409.
- [x] 6. `GET /api/replays/options` returns the keys, dates and flags above from seeded data and a fixed clock (10:00 ET on a session day → `offline_now` true).
- [x] 7. `SubprocessReplayLauncher` spawns exactly `trader replay --run 7` (fake spawn records argv), reaps the child, and a child exiting 1 writes one `warning` event with the run id.
- [x] 8. **Live views:** with events of the live run, of a replay run and without a run seeded, the Dashboard events, `/api/events` and the System errors list contain no replay row; the SSE `events` message after inserting a replay event carries nothing, while a live one is carried.
- [x] 9. The feed emits `invalidate` with `replays` when a replay's `updated_at` changes and with `reports` when a `weekly_reports` row is written; inserting a replay run's order, fill, trade, event and a `replay`-scoped strategy config changes no trading-topic or `strategies` watermark, while the same rows of the live run still do (the P4 feed tests pass).
- [x] 10. Gate and commit `P5-T7: ...`.

**Build notes (P5-T7 builder, 2026-09-27; trunk 97416bc):**
- Replay rows are excluded with one shared filter, `trader.api.feed.live_or_unscoped(run_id)` = `run_id IS NULL
  OR run_id IN (SELECT id FROM runs WHERE mode = 'live')`, used by the feed watermarks, the SSE `events`
  messages, the Dashboard events and `/api/events` + the System errors. This matches the plan's "null or of a
  live run" and excludes every replay row; it differs from "`= <live run id>`" only for rows of an older,
  non-active live run (still shown), and needs no live-run lookup (so `/api/system` creates no run).
- `offline_now` and `latest_allowed` are computed in the router (09:15 inclusive to 16:30 exclusive ET on a
  session day; close + 15 min inclusive). T6's `create_replay` decides the stored data mode itself; T17 may
  share one helper.
- `SubprocessReplayLauncher.launch` first checks that the run is a `queued` replay (`ReplayNotFound` /
  `ValueError`, nothing spawned), and a lost child (`wait()` failed) writes the same `replay.launcher`
  warning with `exit_code: null` and the error type.
- List rows are mapped from `runs.params` directly (the settings snapshot is not parsed), so one unreadable
  snapshot cannot break the list; `expectancy_r` is the mean of the trades with an R, 4 dp half-up;
  `trades` 0 gives null `expectancy_r` and `total_pnl`.
- `GET /api/replays/{id}` masks `error` with `redact_text`; ids outside 1..2^63-1 are 422.
- `tests/api/test_web_client_contract.py` (P4 review): the five replay client methods were removed from
  `PENDING_ROUTES` (coordinator's instruction), so the contract test covers them.

---

### Task P5-T8: Web Replay page

**Goal:** Start a replay over a date range with optional setting and strategy overrides, watch its progress, see its results and compare them with the live run over the same dates. BR-54; SPEC §12 (Replay).

**Files:** `web/src/pages/Replay.tsx`, `web/src/pages/replay/*` (components and their `*.test.tsx`).

**Interfaces:**
- Consumes: T1 web contracts (`replayOptions`, `replays`, `replay`, `startReplay`, `cancelReplay`, `settings`, `strategies`, `trades`, `equity`, `qk`, fixtures, `FakeApiClient`); P4 components `FieldInput` (`pages/settings/FieldInput.tsx`), `EquityChart` (`pages/performance/EquityChart.tsx`), `MetricTiles` where their props fit (imported, never modified); `format.ts`; `ui.tsx`.
- Produces: `src/pages/Replay.tsx` (default export) and `src/pages/replay/` components `ReplayList.tsx`, `ReplayForm.tsx`, `ReplayDetail.tsx`, `CompareTable.tsx`, `ReplayProgress.tsx` with tests.

**Behaviour and decisions:**
- **Without `?id=`:** the list (`replays({limit: 50})`): label, range, status badge (queued info, running info with progress, completed ok, failed bad, cancelled muted), a "biased universe" badge, trades, expectancy R, total P&L, created time in MT; tapping a row opens `?id=`. Above it, a "New replay" button opening the form; disabled with the reason when `options.busy`.
- **Form:** from and to dates (defaults: the 20 sessions before `latest_allowed`, bounded by `latest_allowed`), label, the data notes from options ("Questrade data only from <questrade_from>; archive from <archive_from>; universe snapshots from <snapshots_from>; earlier days use today's universe (biased)"), an "Offline (database only)" checkbox checked and locked when `offline_now` with the note "Market hours: the replay uses stored data only"; an "Adjust settings" section rendering a `FieldInput` for each `override_keys` setting (descriptors from `settings()`, current value as the default, only changed values sent); per strategy (from `strategies()`): an Enabled checkbox and its params form (only changed params sent). When the catalyst mode override is `unknown` and `orb_sip.require_catalyst` is on, a warning says no entries will be taken. Submit → `startReplay`, then navigate to `/replay?id=<new id>`; a 422 shows each `fields` message under its input; a 409 shows the server message.
- **Detail (`?id=`):** header (label, range, status, data mode, catalyst mode, half spread), a progress bar (`sessions_done`/`sessions_total`, current date) while queued or running, with a Cancel button (confirm "Stop this replay after the current day?"); warnings: biased days (count and list), forced closes, missing opening bars and minute bars, the error of a failed run; the pinned strategies (key, revision, `replay` scope marked "override"); when finished: `CompareTable` (rows: trades, win rate, expectancy R, profit factor, total P&L, max drawdown, average slippage; columns Replay, Live (same dates), Difference) and the equity curve (`equity({run: String(id)})`); the replay's trades (`trades({run: String(id), limit: 100})`) as a table (date, ticker, entry, exit, R, P&L, exit reason); the last events.
- **Live updates:** the `replays` SSE topic invalidates the list and the detail (T1 `TOPIC_KEYS`); while a run is `queued` or `running` and SSE is disconnected, the detail refetches every 5 s.
- Phone layout: single column at 390 px; tables scroll horizontally inside their card.

**Acceptance tests (Vitest with `FakeApiClient` and fixtures):**
- [x] 1. The list renders the fixtures in order with status badges, the biased badge and MT times; tapping a row navigates to `?id=`.
- [x] 2. The form's defaults come from `replayOptions`; submitting with one changed setting (`risk_pct` "0.01") and one changed strategy param (`top_n` 10) calls `startReplay` with only those in `overrides` and `strategies`, then navigates to the new id.
- [x] 3. `offline_now: true` checks and locks the Offline box and shows the market-hours note; `busy: true` disables "New replay" with the reason.
- [x] 4. A 422 with `fields` for `overrides.risk_pct` shows the message under that input; a 409 shows the server message.
- [x] 5. The running fixture shows the progress bar (3 of 10) and a Cancel button that, after confirmation, calls `cancelReplay(id)` once.
- [x] 6. The completed fixture shows the comparison with replay, live and difference for each metric (win rate as a percentage, expectancy as R), the biased-days warning, the trades table and the events.
- [x] 7. The catalyst-mode `unknown` override with `require_catalyst` on shows the "no entries will be taken" warning.
- [x] 8. At 390 px no element is wider than the viewport (the page container test used in P4) and every button is at least 44 px tall.
- [x] 9. Gate and commit `P5-T8: ...`.

**Build notes (P5-T8 builder, 2026-09-27; trunk 6ea87b5):**
- Default range: the web has no session calendar, so "the 20 sessions before `latest_allowed`" is the 20
  weekdays ending at `latest_allowed` (holidays not skipped; the server validates the range).
- 422 messages are matched by joining each `loc` after `body` with dots (`overrides.fees.commission`,
  `strategies.orb_sip.params.top_n`, `date_to`), as P5-T7 builds `loc`; unplaced ones (for example `strategies`)
  show with the server message above the Start button.
- The list's summary has no progress, so the (single) running row fetches its detail for "3/10".
- Replay trades and equity never move the trading-topic SSE watermarks (T7), so the detail invalidates its
  trades and equity queries whenever the replay's status or `sessions_done` changes.
- Comparison differences are exact BigInt decimal subtractions (`replay/shared.ts` `decimalDiff`); win rate and
  drawdown differences are percentage points. Cancel uses an inline confirm ("Stop replay" / "Keep running")
  rather than `settings/Confirm`, whose fixed "Cancel" label would read ambiguously next to "Cancel replay".
- The catalyst warning shows when the effective `replay.catalyst_mode` is `unknown` and any enabled strategy's
  effective params have `require_catalyst: true` (only when `settings()` has that descriptor).
- Reused P4 pieces unmodified: `FieldInput`, `EquityChart`, `strategyChanges` (StrategyForms), `validate.ts`;
  `MetricTiles` was not used (the comparison table replaces it on this page).

---

### Task P5-T9: Weekly report: facts, Claude commentary, number check, budget, job body

**Goal:** Every Saturday, a report of the Monday–Friday week just ended: the facts from the metrics module, a 150–300-word Claude commentary that may only quote numbers in those facts, stored and sent once on Telegram, within the Claude budget. BR-61; SPEC §4.3 ("Weekly report"), §9 (Sat 09:00); Review Focus 4.

**Files:** `trader/reports/weekly.py`, `trader/adapters/claude/reports.py`, `trader/jobs/weekly.py`, `tests/reports/test_weekly.py`, `tests/adapters/test_claude_reports.py`, `tests/jobs/test_weekly.py`.

**Interfaces:**
- Consumes: `compute_metrics`, `Metrics` (T2; tests monkeypatch `trader.reports.weekly.compute_metrics` with a table-driven fake until T2 lands); `WeeklyReport`, `Trade`, `Journal`, `KillSwitchEvent`, `Catalyst`, `Symbol` models; `WeeklyReportView`, `Notifier`, `Renderer` (T1; tests use `RecordingNotifier` and a fake renderer); `cost_usd`, `PRICES_PER_MTOK` from `trader.adapters.claude.catalyst`; `RuntimeSettings`; `SessionCalendar`.
- Produces in `trader.reports.weekly`: `WeekWindow(start: date, end: date, sessions: tuple[date, ...], week_ending: date | None)` (frozen; `week_ending` = the last session, None when the week had none); `week_window(cal, any_day: date) -> WeekWindow` (the Monday–Friday week containing the day; a Saturday or Sunday belongs to the week just ended, as the web's `tradingWeek` in `web/src/pages/performance/dates.ts` already does); `last_completed_week(cal, today: date) -> WeekWindow` (Saturday or Sunday → this week; Monday–Friday → the previous week); `build_facts(factory, cal, run_id: int, week: WeekWindow) -> dict[str, Any]`; `check_numbers(text: str, facts: Mapping[str, Any]) -> list[str]` (the offending tokens; empty means OK); `claude_spent(factory, day: date) -> Decimal`; `upsert_report(factory, clock, week: WeekWindow, run_id: int, facts, outcome: CommentaryOutcome) -> None`; `CommentaryOutcome(status: CommentaryStatus, text: str | None, error: str | None, model: str | None, input_tokens: int, output_tokens: int, cost_usd: Decimal)`.
- Produces in `trader.adapters.claude.reports`: `COMMENTARY_SYSTEM_PROMPT: str`; `MAX_COMMENTARY_TOKENS = 900`; `Commentary(status: Literal["ok", "error"], text: str | None, model: str, input_tokens: int, output_tokens: int, cost_usd: Decimal, error: str | None)`; `CommentaryWriter(client: Any, settings: Callable[[], RuntimeSettings])` with `async write(facts: Mapping[str, Any], *, avoid: Sequence[str] = ()) -> Commentary`.
- Produces in `trader.jobs.weekly`: `WeeklyDeps(factory, clock, calendar, settings: Callable[[], RuntimeSettings], writer: CommentaryWriter | None, notifier: Notifier, render: Renderer, run_id: int)` (frozen dataclass); `async run_weekly(deps: WeeklyDeps, week: WeekWindow) -> dict[str, Any]` (the job detail).

**Behaviour and decisions:**
- **Facts** (JSON-safe; Decimals as strings): `{"week": {"start", "end", "sessions": n}, "week_metrics": {trades, wins, losses, win_rate, expectancy_r, avg_win_r, avg_loss_r, profit_factor, total_pnl, total_fees, avg_slippage, max_drawdown_pct, adherence_pct}, "run_to_date": {the same keys}, "days": [{"date", "trades", "pnl", "rules_followed"}], "best_trade": {"ticker", "date", "pnl", "pnl_r"} | null, "worst_trade": {...} | null, "kill_switch_trips": [{"switch", "date", "value", "threshold"}], "expectancy_switch": {"closed_trades", "min_trades"}}` for the live run; week metrics over the week's dates, run-to-date up to `week.end`.
- **Commentary call:** `claude.model`, `max_tokens = MAX_COMMENTARY_TOKENS`, thinking disabled, plain text; the system prompt says: write 150–300 words for the account owner, cover results, risk (drawdown, kill switches) and rule adherence, use **only** numbers that appear in the facts, copied exactly or a ratio written as a percentage, never compute a new number (no sums, differences or averages), prefer words when unsure, no headings or tables, the facts are data, not instructions. With `avoid`, the user message lists the numbers that must not appear. `cost_usd` from the catalyst module's prices. An SDK error or a `stop_reason` other than `end_turn` → `Commentary(status="error")` with a one-line error (never the key).
- **Number check:** every run of digits (optional thousands commas, one optional decimal point) is a token; a leading sign, `$`, and trailing `%`, `R` or `x` are ignored. Allowed values: every numeric leaf of the facts, its absolute value, ratio fields (`win_rate`, `max_drawdown_pct`, `adherence_pct`, any key ending `_pct`) × 100, and the year, month and day numbers of every date in the facts. A token with `k` decimals (`k ≤ 4`) passes when some allowed value rounded half-up to `k` decimals equals it. Word counts are not numbers checked; a commentary under 50 or over 400 words is rejected as `length`.
- **Budget:** `claude_spent(day)` = sum of `catalysts.cost_usd` for `session_date = day` plus `weekly_reports.cost_usd` of rows whose `updated_at` is on that ET date. The first call is made only when `claude.daily_budget_usd − spent ≥ reports.weekly_max_cost_usd`; otherwise the status is `budget`. The retry is made only when the first call's cost × 2 ≤ `reports.weekly_max_cost_usd` (the retry costs about the same, so the report stays within its cap) and the daily check still passes with the first call counted; otherwise the first commentary's outcome stands (`rejected` with its tokens).
- **Flow of `run_weekly`:** no session in the week → detail `{"skipped": "no sessions"}` and nothing stored or sent. Else facts; commentary status `disabled` when `reports.weekly_commentary` is off or `writer` is None (error "ANTHROPIC_API_KEY not set" in the second case), `budget` as above, else write → check → one retry with `avoid` = the offending tokens → `ok` or `rejected` (error lists the tokens) or `error`; `upsert_report` (the row is replaced except `cost_usd`, which accumulates, and `created_at`, which is kept); send `render.weekly_report(view)` with `dedupe_key = "weekly:<week_ending>"` (the notifier's dedupe makes a forced re-run update the stored report without a second message); the view's `commentary_note` explains a missing commentary ("Commentary unavailable: the daily Claude budget is used up.", "... quoted numbers not in the report.", "... Claude is not configured.", "... Claude failed."). Detail: `{"week_ending", "commentary_status", "cost_usd", "trades", "sent": <dedupe status>}`.
- The commentary is untrusted text: it is stored as is and escaped by the renderer and by React.

**Acceptance tests (fake Anthropic client as in the P2 catalyst tests; real DB):**
- [x] 1. `week_window` of Wednesday 2026-11-25, and of Saturday 2026-11-28, → 2026-11-23..2026-11-27 with 4 sessions (Thanksgiving) and `week_ending` 2026-11-27; `last_completed_week` on Saturday 2026-11-28 → that week; on Monday 2026-11-30 → the same week; a week with no sessions (a test calendar whose `is_session` is False for that week) → `week_ending` None.
- [x] 2. `build_facts` on a seeded week: the week and run-to-date metrics equal `compute_metrics` over those ranges, the days list has every session with its trades and answer, best and worst trades by R, the week's kill-switch trips.
- [x] 3. `check_numbers` passes a text quoting "4 trades", "a 50% win rate" (0.5000), "+0.13R" (0.1250 → 0.13 at 2 dp), "$5.00", "November 27"; flags "7 trades", "a 51% win rate" and "$12.34" (not in the facts).
- [x] 4. A commentary with a made-up number, then a clean one on the retry → status `ok`, two calls, the second listing the offending number; two bad ones → `rejected`, commentary null, the error lists the tokens, and the report is still stored and sent with the "quoted numbers" note.
- [x] 5. **Budget:** spent 0.97 with a 1.00 budget and a 0.05 weekly cap → no call, status `budget`, the report sent with its note; spent 0.90 → one call, its cost stored and counted by `claude_spent` the same day; a first call costing 0.03 that quotes a made-up number gets no retry (0.06 > 0.05) and ends `rejected`.
- [x] 6. `reports.weekly_commentary = false` → no call, status `disabled`; no Anthropic client → `disabled` with "ANTHROPIC_API_KEY not set".
- [x] 7. The SDK raising, and `stop_reason = "max_tokens"` → status `error`, a one-line error without the API key.
- [x] 8. **Once:** `run_weekly` twice for the same week sends one Telegram message (dedupe `weekly:2026-11-27`), keeps one row, and accumulates the second call's cost.
- [x] 9. The prompt sent to Claude contains the facts JSON and the "only numbers in the facts" rule, and the call uses `claude.model` and `max_tokens` 900.
- [x] 10. Gate and commit `P5-T9: ...`.

**Build notes (P5-T9 builder, 2026-09-27):**
- `build_facts` gains a keyword `settings: RuntimeSettings | None = None` (for `expectancy_switch.min_trades`; read from the database when omitted); `run_weekly` passes `deps.settings()`.
- Extra public helpers in `trader.reports.weekly`: `check_length(text)` (the 50–400 word rule, kept out of `check_numbers` so test 3's short texts are only number-checked), `allowed_values(facts)`, `METRIC_KEYS`; in `trader.adapters.claude.reports`: `build_prompt(facts, avoid)` (facts as sorted JSON inside `<facts>`).
- A length failure is `rejected` and also gets the one retry (budget permitting); its note is "Commentary unavailable: it was not the expected length." `reports.weekly_commentary = false` has its own note ("... it is turned off in Settings."). A failed retry is `error` (its cost counted). A rejected report's error lists every offending number of both attempts.
- `expectancy_switch.closed_trades` counts trades with an R up to the week's end since the switch's last reset, as `KillSwitches` does. Kill-switch trip `value`/`threshold` are not treated as ratios by the number check (the rule names `win_rate` and `*_pct` keys only).
- Detail `sent` follows the postclose pattern: `sent`, `handed_off` (no `notifications` row, e.g. a fake), `duplicate` (the key existed before this run), `failed`, `error`.
- Tests monkeypatch `trader.reports.weekly.compute_metrics` (T2 was a stub); T18's weekly-day test exercises the real one.

---

### Task P5-T10: Telegram messages and relay: weekly report, run-to-date line, kill-switch reset confirmation, `log.*` never relayed

**Goal:** The Telegram side of Phase 5: the self-contained weekly report message, a run-to-date metrics line on the daily summary, a confirmation when an automatic kill switch is reset in the web app, and the rule that mirrored log rows never reach the phone. BR-32, BR-41, BR-60, BR-61; SPEC §4.4, §6.3; contract refinement 6.

**Files:** `trader/notify/messages.py`, `trader/notify/relay.py`, `trader/jobs/postclose.py`, `tests/notify/test_messages_phase5.py`, `tests/notify/test_relay_phase5.py`, `tests/jobs/test_postclose_run_to_date.py`.

**Interfaces:**
- Consumes: `WeeklyReportView`, `RunToDateView`, `DailySummaryView.run_to_date` (T1); `compute_metrics` (T2; `postclose` catches any exception from it, including the stub's `NotImplementedError`); `KillSwitches.reset` event shape (message `kill switch <switch> reset`, data `switch`, `reason`, `equity_at_reset`); `MIRROR_SOURCE_PREFIX = "log."` (T1 constant in `trader.logging_mirror`).
- Produces: `MessageRenderer.weekly_report(v) -> OutboundMessage`; the daily summary's run-to-date line; the kill-switch reset wording in `MessageRenderer.alert`; relay rules below; `daily_summary_view` filling `run_to_date`.

**Behaviour and decisions:**
- **Weekly report message** (kind `weekly_report`): "<b>Weekly report</b> <week_start> to <week_ending>", then "Trades: N (W wins, win rate X%)", "Expectancy: +0.13R", "P&L: +$5.00", "Max drawdown: X%", "Rules followed: X% of answered days" (lines with a None value are left out), a blank line, the commentary (escaped) or the `commentary_note` in italics, then the link `/reports?week=<week_ending>`. When the text would exceed `TELEGRAM_LIMIT`, the commentary is cut at a word boundary with "… (full text on the web)". `weekly_link` is unchanged.
- **Daily summary line** (when `run_to_date` is set, after the drawdown line): "Run to date: 12 trades, win rate 41.7%, expectancy +0.18R, P&L +$23.40" and, while `expectancy_trades < expectancy_min_trades`, "Expectancy switch: 12 of 50 trades". `daily_summary_view` builds it from `compute_metrics(factory, run_id, None, session_date)` and the settings (trunk's `daily_summary_view(factory, run_id, session_date, now, archive)` gains an additive keyword for `killswitch.expectancy_min_trades`; its only caller is `run_postclose` in the same file); any exception → `run_to_date = None` (logged at warning), and the summary goes out as before (BR-60).
- **Reset confirmation:** the relay's `events` stream also takes rows with `source = "killswitch"` whose message matches `kill switch <switch> reset` (level `warning`), for the live run only (the run filter already applies); they render through `alert` with kind `kill_switch` as "<b>KILL SWITCH RESET: <switch></b> at <time>", "Reason: <reason>" and "Entries allowed again unless another switch is tripped." The manual pause's own events keep their P3 handling.
- **Never relayed:** any event whose `source` starts with `log.` (the mirror's rows), in addition to the P3 `NEVER_RELAYED` sources.
- All other P3 relay behaviour (cursors, dedupe keys, retries, backlog cap) is unchanged.

**Acceptance tests:**
- [x] 1. `weekly_report` of a full view: every headline number appears once in the text, the commentary is escaped (a `<b>` in it shows literally), the link is `/reports?week=2026-11-27`, kind `weekly_report`.
- [x] 2. A view with `commentary None` and a note shows the note; a 5,000-character commentary is cut to fit 4,096 with the web pointer, never splitting an HTML entity or tag.
- [x] 3. Win rate 0.4167, expectancy 0.1800, P&L 23.40 render as "41.7%", "+0.18R", "+$23.40" (the same numbers `compute_metrics` gives; the daily line and the weekly message use one formatter).
- [x] 4. `daily_summary_view` with a monkeypatched `compute_metrics` fills `run_to_date` and the "12 of 50" line; with `compute_metrics` raising, `run_to_date` is None and the P3 postclose tests pass unchanged.
- [x] 5. **Reset relay:** after `KillSwitches.reset(run, "max_drawdown_pct", "reviewed", "web:stephen")` on the live run, one pump sends one `kill_switch` message with "KILL SWITCH RESET: max_drawdown_pct" and "Reason: reviewed" (dedupe `event:<id>`); a second pump sends nothing; a reset in a replay run is not sent.
- [x] 6. **Never relayed:** an `error` event with source `log.worker` (run_id null) is skipped and the cursor still advances past it; an `error` event with source `job.nightly` is still sent.
- [x] 7. The P3 relay and message tests pass unchanged.
- [x] 8. Gate and commit `P5-T10: ...`.

**Build notes (P5-T10 builder, 2026-09-27; trunk 8ddff8e):**
- Shared formatters in `trader.notify.messages`: `fmt_rate` (one decimal, `41.7%`, win rate and adherence),
  `fmt_signed_r`, `fmt_signed_money` (zero shows `+$0.00`, as the P3 realized P&L line) and
  `run_to_date_lines(v)`; the weekly max drawdown uses the daily summary's two-decimal percentage.
  `WEB_POINTER` and `RESET_MESSAGE` (the regex of `kill switch <switch> reset`) are module constants.
- The weekly message's link label is "Weekly report <week_ending>"; with neither commentary nor note the body
  is left out. The reset confirmation's reason is masked and capped like other alert text.
- `daily_summary_view(..., *, expectancy_min_trades: int | None = None)`: None (the P3 callers) computes no
  metrics; `run_postclose` passes `killswitch.expectancy_min_trades`. `expectancy_trades` counts as the switch
  does: `trades - trades_without_r` of the run-to-date metrics, or, after an expectancy reset, the trades with
  an R multiple closed since the last reset (queried directly).
- Relay: `log.*` sources are excluded in SQL (`startswith` with autoescape), so the cursor still passes them;
  reset rows are taken with `source = 'killswitch'` and `message LIKE 'kill switch % reset'` at any level.

---

### Task P5-T11: Kill-switch trips end to end (realistic data)

**Goal:** Prove, through the real engine, broker, ledger, risk manager, kill switches and relay (with a fake Telegram), that each automatic switch trips from a realistic sequence of fills, alerts once, blocks only entries, and clears as SPEC §6.3 says. BR-41, O4; SPEC §6.3; master plan outline "each switch trips from realistic data".

**Files:** `tests/integration/test_killswitch_trips.py`; `trader/engine/killswitch.py` only if a test reveals a defect (then a regression test in the same file and a note in the report).

**Interfaces:**
- Consumes: `Engine` built as `build_engine` builds it but with a fake `MarketDataView` (scripted quotes) and a scripted test strategy plug-in (registered through the `plugins=` argument of `StrategyRegistry`, emitting `EnterLong` stop entries and relying on the real protective-stop path), `NotificationRelay` with `RecordingNotifier`/fake Telegram from `tests/fakes_telegram.py`, the P4 API reset route through `make_client`.

**Behaviour and decisions:**
- Settings for the tests: starting cash 720 USD, `risk_pct` 0.02, auto approval, `killswitch.expectancy_min_trades` 5 (lowered so a test day is short), other thresholds default.
- Each scenario drives quotes through `Engine.on_quotes` and events through `run_event`, then pumps the relay once.

**Acceptance tests (real DB, fake clock, fake quotes):**
- [x] 1. **Daily loss:** three stop-outs in one session (a test plug-in with `max_positions` 3) take the day's P&L past −5% including fees and slippage; the trip happens on the fill that crosses it (`kill_switch_events` row with value and threshold 0.05), the relay sends one `kill_switch` message with "Value x% vs threshold 5%"; a fourth `EnterLong` the same session is rejected with check `kill_switch`; the next session's first entry is accepted and no reset alert is sent.
- [x] 2. **Drawdown:** losing trades over three sessions reach 15% below the peak equity: one trip, one alert, entries blocked on the following sessions; after `POST /api/killswitch/max_drawdown_pct/reset` with a reason, entries are allowed and a further 15% fall from the equity at the reset trips again.
- [x] 3. **Expectancy:** four closed trades with mean R ≤ 0 do not trip; the fifth close with the mean still ≤ 0 trips (value = the mean R, threshold 0); with a mean > 0 after five trades nothing trips; after a reset it re-arms only after five more trades.
- [x] 4. **Exits never blocked:** with all three automatic switches and `manual_pause` tripped, the protective stop proposal of an open position is submitted and fills, and the flatten event exits the position.
- [x] 5. Each trip writes exactly one `error` event and one alert even when two fills evaluate the switches at the same moment (two concurrent `on_quotes` calls in threads).
- [x] 6. Gate and commit `P5-T11: ...`.

**Build notes (P5-T11 builder, 2026-09-27; trunk 9407204):**
- Tests only; no defect found, so `trader/engine/killswitch.py` is unchanged and nothing is xfailed.
- Test 2: a 15% drawdown in three sessions cannot stay under the 5% daily limit every day
  (1 − 0.95³ ≈ 14.3%), so the third session's gapped stop-out trips `daily_loss_pct` on the same fill; the test
  asserts one `max_drawdown_pct` trip and one drawdown alert (plus the daily one), then the reset (blank reason
  422, audit row, Telegram "KILL SWITCH RESET" confirmation) and the re-trip from the equity at the reset.
- Test 5 forces the race with a barrier inside `KillSwitches.inputs`, so both fills compute crossing inputs
  before either evaluates.
- Added (orchestrator): the manual pause through the real `TelegramBot` + `Commands` (confirmation on the fake
  Telegram, entries blocked, the stop still fills, `/resume`), and the daily switch inside a replay through the
  real `run_replay` and `build_replay_engine` (trip row and event carry the replay's run id and replay time,
  the fourth entry is rejected, the next replayed session trades, the live relay sends nothing). A web-app
  pause is not relayed to Telegram (a `warning` event; P3 design, pinned by P5-T10's relay test).

---

### Task P5-T12: Reports API and CSV export columns

**Goal:** Serve the stored weekly report to the web, and extend the trades CSV with the columns that make each row traceable for records. BR-61 (web side), BR-62; SPEC §11 (`/export/trades.csv`).

**Files:** `trader/api/routers/reports.py`, `trader/reports/export.py`, `tests/api/test_reports.py`, `tests/reports/test_export_phase5.py`.

**Interfaces:**
- Consumes: `WeeklyReport`, `Notification`, `SimAccount`, `Run`, `StrategyConfig`, `Position` models; `WeeklyReportOut`, `CurrentUser` (T1, P4); `week_window` (T9; tests monkeypatch it with a plain Monday–Friday function, weekends mapped to the week just ended, until T9 lands).
- Produces in `trader.api.routers.reports`: `GET /api/reports/weekly?week=YYYY-MM-DD` → `WeeklyReportOut` (404 when none).
- Changes `trader.reports.export`: `TRADE_CSV_COLUMNS` gains, after the existing fifteen, `currency`, `strategy_version`, `config_revision`, `config_scope`, `stop_loss`, `unprotected_seconds`, `run_mode`; `trades_csv` fills them (the sim account's currency, the position's config version, revision and scope, the position's stop loss and unprotected seconds, the run's mode).

**Behaviour and decisions:**
- **Weekly:** the report whose `week_start ≤ week ≤ week_start + 6 days` (the Monday–Friday week containing the date, a Saturday or Sunday counting to the week just ended, as T9's `week_window` and the web's `tradingWeek`); `telegram_status` = the `notifications` status of `weekly:<week_ending>` or null; `facts` as stored; commentary as stored (never re-generated on read). A malformed date → 422.
- **Export:** existing columns, order and CSV-injection guard unchanged; the new text cells go through `safe_cell`; numbers as stored.

**Acceptance tests:**
- [x] 1. `GET /api/reports/weekly?week=2026-11-25` and `?week=2026-11-28` (the Saturday) return the report stored for week ending 2026-11-27 with its facts, commentary and status; `?week=2026-11-30` (no report) → 404; no session → 401.
- [x] 2. `telegram_status` is `sent` when a `sent` notification with key `weekly:2026-11-27` exists, else null.
- [x] 3. The CSV header is the 22 columns in order; a trade of a replay run with a replay-scoped config shows `config_scope = replay` and `run_mode = replay`; the P4-T7 export tests pass (they use the constant).
- [x] 4. Gate and commit `P5-T12: ...`.

---

### Task P5-T13: Web Reports commentary

**Goal:** The Reports page (the Telegram weekly link's target) shows the week's Claude commentary, or why there is none, instead of the Phase 4 note line. BR-61; SPEC §12; Phase 4 note "P5-T6 adds the Claude commentary to it".

**Files:** `web/src/pages/Reports.tsx`, `web/src/pages/reports/*` (a `Commentary.tsx` component and its test), `web/src/pages/performance/Reports.test.tsx` (its note-line test is replaced).

**Interfaces:**
- Consumes: T1 web contracts (`weeklyReport`, `qk.weeklyReport`, `WeeklyReportOut`, fixtures `weeklyReportOk`, `weeklyReportBudget`), `format.ts`, `ui.tsx`.

**Behaviour and decisions:**
- The page requests `weeklyReport(<Monday of the shown week>)`. With a report: a "Commentary" card with the text as paragraphs (plain text, split on blank lines; React escapes it), and a footer "Generated <created_at in MT> by <model>, US$<cost>"; status `budget`, `disabled`, `rejected` or `error` shows a muted one-line reason ("The daily Claude budget was used up", "Claude is not configured", "The commentary quoted numbers that are not in the report, so it was withheld", "Claude failed"); `null` (404) shows "No weekly report for this week yet (it is written on Saturday morning)." The rest of the page (metrics, trades, journal answers, navigation) is unchanged.
- The `reports` SSE topic refreshes it (T1 `TOPIC_KEYS`).
- The P4 gauntlet test `web/src/gauntlet/web_pages_breaker.test.tsx` is not edited; if it pinned the note line, report it to the orchestrator instead.

**Acceptance tests (Vitest):**
- [x] 1. With `weeklyReportOk` the commentary paragraphs, the MT time, the model and the cost render; a `<script>` inside the commentary text renders as text.
- [x] 2. With `weeklyReportBudget` the budget reason shows and no commentary card body.
- [x] 3. With `null` the "No weekly report for this week yet" line shows; the metrics still render.
- [x] 4. `?week=2026-11-25` calls `weeklyReport("2026-11-23")`; changing to the previous week requests that Monday.
- [x] 5. The P4 Reports tests still pass except the note-line test, which now asserts the new states.
- [x] 6. Gate and commit `P5-T13: ...`.

---

### Task P5-T14: Error log mirror to `event_log`

**Goal:** `error` and `critical` log lines that no code wrote to `event_log` appear in the System page's error list, without ever blocking, recursing, leaking a secret or reaching Telegram. SPEC §2 ("mirrored to the event_log table for the UI"), BRD NFR reliability and security; Review Focus 5.

**Files:** `trader/logging_mirror.py`, `tests/test_logging_mirror.py`.

**Interfaces:**
- Consumes: `configure_logging`, `redact_text`, `is_secret_key`, `REDACTED` (`trader.logging_setup`); `EventLog`; `Clock`; `RuntimeSettings.logging_mirror_level/logging_mirror_max_per_minute`.
- Produces: `MIRROR_SOURCE_PREFIX = "log."`; `EventLogMirror(factory, clock, *, process: str, level: Literal["error", "critical"], max_per_minute: int, run_id: int | None = None, queue_size: int = 1000, flush_seconds: float = 1.0)` (`run_id` is stamped on every row; the replay process passes its replay's id, every other process none) with `install() -> None` (adds its handler to the root logger), `start() -> None` (a daemon thread writing batches every `flush_seconds`), `flush(timeout: float = 2.0) -> int` (rows written), `close() -> None` (flush, stop, remove the handler), `dropped: int`; `install_event_mirror(factory, clock, process: str, settings: RuntimeSettings, *, run_id: int | None = None) -> EventLogMirror | None` (None when `logging.mirror_level` is `off`; else installed and started).

**Behaviour and decisions:**
- **What is mirrored:** stdlib records at or above the level reaching the root logger, including structlog events (whose record message is the event dict). Skipped: records of the loggers `trader.logging_mirror`, `sqlalchemy*`, `alembic*`; records whose event dict has `event_logged=True` (a caller that also wrote `log_event` may pass it); anything emitted by the mirror's own thread.
- **Row:** `level` = `error` or `critical`; `source` = `log.<process>` (cut to 50 characters); `run_id` = the mirror's `run_id` (null except in a replay process); `message` = `<logger>: <event>` (the structlog event name, or the stdlib message) masked and cut to 500 characters; `data` = the other fields, masked like the log line (`redact_text` on strings, secret-named keys `[REDACTED]`), JSON-safe, at most 4 KB (cut with `"truncated": true`), plus `exc_type` and a masked one-line `exc_message` when the record has an exception (never the traceback).
- **Never blocks:** `emit` only builds the masked row and `put_nowait`s it; a full queue drops the record and increments `dropped`. **Never raises:** every error inside `emit` or the thread is swallowed; a DB failure drops that batch and counts it (it is not logged through the root logger, so no recursion).
- **Rate limits:** at most one row per (logger, event) per 60 s of the clock (later repeats in that window are counted into the next row's `data.repeated`); at most `max_per_minute` rows per minute overall; when rows were dropped for the limit, one summary row "log mirror dropped N lines" at `error` the next minute.
- Mirror rows are never relayed (T10), so they reach the System page only.

**Acceptance tests (real DB; `configure_logging` in a subprocess-free test with a fresh root logger per test):**
- [x] 1. After `install_event_mirror(... level "error")`, `structlog.get_logger("trader.x").error("thing.failed", symbol="AAA")` and a stdlib `logging.getLogger("finviz").error("boom")` each produce one `event_log` row (`source` `log.test`, message `trader.x: thing.failed`, data with `symbol`) after `flush()`; an `info` and a `warning` produce none.
- [x] 2. **Masked:** a line with `token="abc123secret"` and a message containing `https://api.telegram.org/bot123456:AAAA.../getUpdates` stores `[REDACTED]` for both; nothing unmasked reaches the row.
- [x] 3. **Non-blocking:** with the DB paused (a factory whose connect blocks), 5,000 error lines return from `emit` in under 1 s in total, the queue keeps at most `queue_size`, and `dropped` counts the rest.
- [x] 4. **No recursion:** a factory raising on every write → no exception escapes, no new log lines are produced by the mirror, and the process keeps logging to stdout.
- [x] 5. The same (logger, event) ten times in 60 s → one row, and the next row after 60 s carries `repeated: 9`; 100 distinct events in one minute with `max_per_minute` 30 → 30 rows and one "dropped" summary row.
- [x] 6. `event_logged=True`, `sqlalchemy.engine` errors and records from the mirror's own logger are skipped.
- [x] 7. `logging.mirror_level = "off"` → `install_event_mirror` returns None and installs no handler; `critical` level mirrors only critical lines.
- [x] 8. `close()` flushes pending rows within 2 s and removes the handler (a later error line writes nothing).
- [x] 9. A mirror built with `run_id=7` (process `replay`) stamps `run_id` 7 on every row, including the "dropped" summary row.
- [x] 10. Gate and commit `P5-T14: ...`.

**Build notes (P5-T14 builder, 2026-09-27):**
- Rate limits are applied by the writer (not in `emit`), so `emit` stays a mask plus `put_nowait` and a burst fills
  the queue as test 3 expects. Repeats are keyed on (logger, event) and measured on the row's clock time (taken in
  `emit`). A line cut by the per-minute limit is not remembered for the repeat window.
- `dropped` counts every line that never became a row: a full queue, the per-minute limit and a failed batch. The
  "log mirror dropped N lines" row (`data.dropped`) reports all three, once, when the next minute starts (or at
  `close()`). If that write fails, its count is reported again later.
- Extras: `EventLogMirror.queued()` (the queue length, used by test 3) and a `source` attribute. `close()` is
  idempotent. The mirror never logs anything itself, and log lines emitted by its own thread (or by the thread
  running an inline flush, during the write) are skipped.
- Structlog exceptions reach the handler already rendered by `format_exc_info`, so `exc_type` and `exc_message` come
  from the rendered traceback's last line. The traceback itself is never stored.
- The P5-T1 stub test (`test_every_stub_names_its_owner`) now really calls `install()`, `start()` and
  `install_event_mirror(None, ...)`: each leaves a harmless handler or daemon thread with a `None` factory in the
  test process (every write fails and is counted). Left as is (T1 owns that file).

---

### Task P5-T15: Job retries and restart recovery

**Goal:** A transient failure of a day-level job is retried in-process and alerts once only when every attempt failed; and one hard-kill scenario proves a restart during market hours duplicates nothing. BRD NFR reliability ("A failed job is retried and triggers an alert. The engine recovers after a container restart during market hours."), SPEC §9; Review Focus 5.

**Files:** `trader/jobs/runner.py`, `tests/jobs/test_runner_retry.py`, `tests/integration/test_restart_recovery.py`.

**Interfaces:**
- Consumes: `RetryPolicy` and the keywords (T1); P3 worker and relay pieces (`Worker`/`run_worker` pieces as trunk names them, `fire_event`, `NotificationRelay`, `SimBroker`), `tests/integration/test_worker_day.py` fixtures.
- Produces: the retry behaviour of `run_job` / `run_job_async` with `retry`.

**Behaviour and decisions:**
- With `retry = RetryPolicy(attempts=n, first_delay_s=d, backoff=b)` and `n > 1`: the single-flight lock is held across all attempts; each attempt runs `_start` (so "already succeeded" still skips) and inserts its own `job_runs` row; a failed attempt `k < n` is recorded `failed` with its error and a **`warning`** event (source `job.<job>`, message "<job> failed for <date> (attempt k of n), retrying in S s", data: `error`, `attempt`, `attempts`); then it sleeps `d × b^(k−1)` seconds through the injected `sleep`; the last failed attempt writes the **`error`** event (the one the relay alerts) with message "<job> failed for <date> after n attempts" and data `attempt = n`. A success after a failed attempt adds `"attempts": k` to the job detail.
- Not retried: `JobFailure` (a deliberate failure, for example a degenerate nightly result or a missed event), `KeyboardInterrupt`/`CancelledError` (recorded and re-raised as today), and a success that could not be recorded.
- `retry=None` or `attempts = 1` behaves exactly as P3 (the existing tests pin it).
- An intermediate failed attempt stays a `failed` `job_runs` row (honest history), but the job's result for the session is its **last** row: the System page, the Dashboard timeline and the Phase 6 soak report count a (job, session) as failed only when its latest row is `failed` (noted for the P6 planner below).
- The last attempt's event keeps the caller's `failure_level` (P3-REVIEW): `retry` never raises an alert level a caller lowered.
- **Restart scenario** (one integration test in the new file, building on P3's `test_worker_restart_mid_session` by importing the helpers of `tests/integration/test_worker_day.py`, which is not edited): worker A is stopped without its shutdown path (its lock connection closed, heartbeat left `session`, the `event:orb_open` job row left `running`, one `notifications` row left `sending`, the relay cursor behind the last fill); worker B starts at a later fake time.

**Acceptance tests:**
- [ ] 1. A body failing twice then succeeding with `attempts=3`: three `job_runs` rows (failed, failed, succeeded), two `warning` events and no `error` event, the sleeps were 120 s and 240 s (fake sleep), the outcome `succeeded` with `attempts: 3` in the detail.
- [ ] 2. A body failing three times: three failed rows, two `warning` events and exactly one `error` event (after the third), outcome `failed`; the relay (P3) would alert once.
- [ ] 3. A `JobFailure` is not retried (one row, one `error` event); a `CancelledError` during the sleep is recorded and re-raised, and the lock is released.
- [ ] 4. A second process starting the same job while the first sleeps between attempts gets `skipped` (`already running`).
- [ ] 5. `run_job_async` has the same semantics with an async fake sleep.
- [ ] 6. **Restart:** worker B acquires the lock (A's is gone), does not re-run `orb_open` (it is settled `failed` with `OUTCOME_UNKNOWN` and one `critical` event), keeps polling the working entry order, which fills once; the `sending` notification becomes `unknown` and is never re-sent; the relay sends each later fill once; B's heartbeat goes to `session`.
- [ ] 7. **No duplicates:** after the restart there is one signal and one order per intent, one fill per order, and one notification per dedupe key.
- [ ] 8. A pending entry proposal created by A expires on time under B, and B's flatten still closes the position before the close.
- [ ] 9. Gate and commit `P5-T15: ...`.

---

### Task P5-T16: Container resource and log limits

**Goal:** The container cannot exhaust the shared Docker host's memory, CPU or disk: memory, CPU and process limits, and capped Docker logs. BRD NFR reliability; SPEC §15, §15.1.

**Files:** `docker/docker-compose.dev.yml`, `docker/docker-compose.prod.yml`, `tests/test_docker_limits.py`.

**Behaviour and decisions (both compose files, service `trader`):**
- `mem_limit: 1g`, `memswap_limit: 1g` (no swap beyond the limit), `cpus: 2.0`, `pids_limit: 256`, `ulimits: {nofile: {soft: 4096, hard: 8192}}`.
- `logging: {driver: json-file, options: {max-size: "10m", max-file: "5"}}` (Docker keeps at most 50 MB of stdout logs; supervisord's own log in the `trader_*_logs` volume is already 10 MB × 3).
- A comment explains each limit and that the replay subprocess shares them (it runs inside the container).
- Every existing P4 compose setting is kept (read-only root, tmpfs, volume, networks, no ports, init, `stop_grace_period`, restart policy). P4-T19 or a P4-T4 fix round may add `TRADER_FORWARDED_ALLOW_IPS` (or other environment keys) to these files: pull trunk right before editing and again before committing, keep every such change, and never overwrite the files wholesale.

**Acceptance tests (file level):**
- [x] 1. Both compose files parse (YAML) and have the limits and the logging options above for `trader`.
- [x] 2. The P4 file tests (`tests/test_docker_files.py`) pass unchanged: read-only, tmpfs, volume, networks, no ports, env file, init, stop grace period.
- [x] 3. The memory limit is at least 4 × the tmpfs size (256 MB), so `/tmp` can never take the container's whole budget.
- [x] 4. Gate and commit `P5-T16: ...`.

**LIVE step (after T17's deploy in T18):** covered by T18 LIVE 2.

---

### Task P5-T17: Wiring: CLI, runtime, API services, launcher, crontab, SPEC §9 amendment, §7.1 rows

**Goal:** Connect the Phase 5 pieces: the `trader replay` and `trader weekly` commands, retries for the day-level jobs, the log mirror in every process, the replay launcher in the API, the weekly cron line, the SPEC §9 amendment and the master plan's contract rows. SPEC §8, §9, §11; contract refinements 1–9.

**Files:** `trader/cli.py`, `trader/runtime.py`, `trader/worker.py`, `trader/api/services.py`, `trader/api/launcher.py` (`CLI_ARGS` only), `docker/crontab`, `tests/test_crontab.py`, `tests/api/test_launcher.py` (its pinned `CLI_ARGS` gains `weekly`), `Trader/docs/SPEC.md` (§9 only), `docs/plans/2026-09-26-build-master-plan.md` (§7.1), `tests/test_cli_phase5.py`, `tests/test_runtime_phase5.py`.

**Interfaces:**
- Consumes: every T1–T16 production interface; `open_replay_deps`, `create_replay`, `run_replay`, `reconcile_abandoned` (T6); `run_weekly`, `WeeklyDeps`, `last_completed_week`, `week_window` (T9); `CommentaryWriter` (T9); `install_event_mirror` (T14); `RetryPolicy` (T1/T15); `SubprocessReplayLauncher` (T7).
- Changes `trader.cli`:
  - `replay` command: `trader replay --from YYYY-MM-DD --to YYYY-MM-DD [--label TEXT] [--offline] [--set KEY=VALUE ...]` (overrides, values parsed as JSON when they parse, else strings) creates the run (`actor = "cli"`) and runs it in the foreground; `trader replay --run ID` runs an existing queued run (the launcher's form). It lowers its own priority (`os.nice(10)`), installs the log mirror (process `replay`, with `run_id` = the replay's id, installed as soon as the id is known), prints one line per finished session ("2026-11-24 done: 1 trade, P&L -7.20") and a final summary (run id, status, trades, expectancy R, total P&L, biased days). Exit codes: 0 completed or cancelled, 1 failed or invalid (the `ReplayInvalid` messages printed one per line), 2 busy.
  - `weekly` command: `trader weekly [--date YYYY-MM-DD] [--force]` (`--date`: any day of the week; default `last_completed_week`) through `runtime.weekly_job`; `job_runs` key `weekly` with `session_date = week_ending`; a week without sessions prints "no sessions" and exits 0.
  - `nightly` and `premarket` pass `retry=RetryPolicy.from_settings(settings)` to `run_job`.
- Changes `trader.runtime`: `weekly_job(core, week: WeekWindow, *, force: bool) -> JobOutcome` (notifier and renderer as the other jobs; a `CommentaryWriter` over `anthropic.AsyncAnthropic(api_key=..., timeout=60, max_retries=1)` only when `ANTHROPIC_API_KEY` is set; the live run; `run_cli_job(..., retry=...)`); `run_cli_job` gains `retry: RetryPolicy | None = None`, and `preopen_job`, `postclose_job` and `weekly_job` pass the settings' policy (`checkin_job` and `event_backup` do not: events have their own retries); `install_log_mirror(core, process: str) -> EventLogMirror | None` (reads settings through `GuardedSettings`, never raises); `run_worker` installs it (process `worker`) and closes it on shutdown after the relay's last pump.
- Changes `trader.api.services.build_services`: `replays = SubprocessReplayLauncher(core.factory, core.clock)`; installs the mirror (process `api`) and closes it with the stack.
- Changes `trader.cli`: every command that builds a `Core` installs the mirror (process `cron`) and closes it at exit, through one shared helper. On trunk `_core(name)` is used by some commands while `nightly`, `premarket` and others call `build_core()` directly, so the helper must cover both (the nightly and pre-market jobs are the ones whose errors matter most).
- `CLI_ARGS["weekly"] = ("weekly",)` (the launcher's existing rule that a `date` must be a trading session stays; any session of the week selects that week).
- `docker/crontab`: `0 9 * * 6    trader weekly`, and the header comment says so (replacing "The weekly report line is added by P5-T6").
- `tests/test_crontab.py`: `EXPECTED` gains the weekly line; its command must be a registered CLI command (the existing test checks it).
- **SPEC §9:** the two amendments of the decision "SPEC §9 changes" (weekly row note; in-process retries paragraph). No other SPEC text changes.
- **Master plan §7.1:** rows "Broker", "Engine", "Proposals", "Notifier", "Event firing" (job retries note) and "Fill model" updated per contract refinements 1–7; new rows "Replay", "Metrics", "Weekly report", "Log mirror", "Strategy config scope"; the "Web API" row gains `/api/replays*`, `/api/reports/weekly`, topics `replays`/`reports`, `ManualJob` `weekly`.

**Behaviour and decisions:**
- A replay CLI run and the API launcher use the same `run_replay`, so a web-started and a CLI-started replay behave identically.
- The mirror is optional wiring: a failure to install it is logged at warning and the process carries on.

**Acceptance tests:**
- [ ] 1. `trader replay --from ... --to ...` (CliRunner, test DB, `open_replay_deps` monkeypatched to fakes) creates and runs a replay, prints a line per session and the summary, exits 0; an invalid range exits 1 with the messages; a held `REPLAY_LOCK` exits 2; `--set risk_pct=0.01` reaches the overrides.
- [ ] 2. `trader replay --run ID` runs a queued run and refuses a completed one (exit 1).
- [ ] 3. `trader weekly --date 2026-11-25` runs the week ending 2026-11-27 once (a second run is `skipped`), `--force` re-runs it; without `--date` on Saturday 2026-11-28 it chooses that week.
- [ ] 4. `nightly`, `premarket`, `preopen`, `postclose` and `weekly` pass a `RetryPolicy` built from the settings; `checkin` and `event` do not (recorded by a monkeypatched `run_job` / `run_job_async`).
- [ ] 5. `build_services` returns `ApiServices` with a `SubprocessReplayLauncher`; the P4-T18 route sweep and CSRF sweep cover the new routes (401 without a session, 403 without the CSRF header) without changes to those tests.
- [ ] 6. The worker and the API install the log mirror with their process names and close it on shutdown (an error line during the run appears in `event_log` with `log.worker`); `trader nightly` (which builds its `Core` directly on trunk) mirrors an error line as `log.cron`; `trader replay` mirrors one with the replay's `run_id`; with `logging.mirror_level = "off"` none is installed.
- [ ] 7. `docker/crontab` has the weekly line, `supercronic -test` semantics hold (the P3 crontab tests pass with the new `EXPECTED`), and the Saturday line is not taken for a Dashboard day job (the P4-T5 `DAY_JOBS` test passes).
- [ ] 8. SPEC §9 and master plan §7.1 contain the new text (a test greps for the retry paragraph, the weekly note and the new §7.1 row names).
- [ ] 9. Gate and commit `P5-T17: ...`.

---

### Task P5-T18: End to end: determinism golden test, isolation test, weekly day, LIVE deploy and checks

**Goal:** Prove the whole of Phase 5 works together: a replay over committed fixture data gives exactly the golden trades twice; a replay changes nothing it must not; a weekly report goes from data to Telegram; and, on `trader-dev`, a deploy, two real replays, a real weekly report and a restart in market hours behave as specified. SPEC §15.1 promotion criterion 3 (replay determinism), §16; BR-54, BR-61; Review Focus 1–5.

**Files:** `tests/replay/test_golden.py`, `tests/replay/golden/<scenario>_data.json`, `tests/replay/golden/<scenario>_expected.json`, `tests/replay/test_isolation_static.py`, `tests/integration/test_replay_isolation.py`, `tests/integration/test_weekly_report_day.py`, `web/tests/smoke.spec.ts` (the live mode also visits `/replay`, read only).

**Interfaces:**
- Consumes: everything; `seed_replay_world` (T1); `open_replay_deps` with a fake Questrade client (offline for the golden test).
- Produces: the golden fixture format: `*_data.json` = symbols, universe snapshots (one day deliberately missing, so it is biased), open-bar stats, `candle_archive` 5m and 1m rows, daily candles and catalysts; `*_expected.json` = the normalized trades (`session_date`, `ticker`, `qty`, `entry_price`, `exit_price`, `pnl`, `pnl_r`, `exit_reason`, `opened_at`, `closed_at`), the per-day candidate rankings (ticker, rank, reject reason) and the whole-run metrics. `UPDATE_GOLDEN=1` rewrites the expected file (never set in the gate).

**Behaviour and decisions:**
- **Golden scenario** (`orb_week`): sessions 2026-11-23 to 2026-11-27, real `orb_sip` and `spy_overlay` with default params, catalysts stored for the breakouts; it covers a clean breakout flattened at `close − 10m`, a same-bar entry-and-stop day, a gap-through stop, an overlay exit on a negative SPY day, the Thanksgiving holiday (skipped) and the 13:00 early close on Friday (flatten at 12:50), plus one biased day (no snapshot).
- The golden test runs the replay twice in the same database (two runs) and once more in a fresh database, and compares every normalized output with the expected file and with each other.

**Acceptance tests:**
- [ ] 1. **Golden:** the `orb_week` replay's normalized trades, rankings and metrics equal `orb_week_expected.json`, including the same-bar day's stop-out at `stop − slip − hs`, the gap-through fill at the open less costs, the overlay exit and the early-close flatten.
- [ ] 2. **Determinism:** the three runs (same DB twice, fresh DB once) give identical normalized outputs, and their progress JSON is identical too (it holds no wall-clock fields).
- [ ] 3. **Isolation (DB):** counting every table before and after an offline replay with an override: only the tables and rows of the decision "Replay isolation" changed (every new row with a run id carries the replay's, and no `event_log` row without a run id was added), `audit_log` gained exactly `replay.start`, `strategy_configs` gained only a `replay`-scoped row, `job_runs`, `notifications`, `catalysts`, `universe_snapshots`, `open_bar_stats`, `candle_archive`, `intraday_candles`, `daily_candles`, `settings` and `notify_cursors` are unchanged; `compute_metrics(live)` and `GET /api/dashboard` are identical before and after; one relay pump sends nothing.
- [ ] 4. **Isolation (static):** no module under `trader/replay/` imports `anthropic`, `trader.adapters.telegram`, `trader.notify.notifier` or `trader.notify.relay`, or references `CatalystClassifier`, `CatalystService`, `fire_event`, `run_job`, `run_job_async`, `.set(` on a settings store or `ensure_defaults(` (an AST scan); for determinism it also imports none of `random`, `uuid`, `secrets` and calls no `float(` on a price or quantity.
- [ ] 5. **Weekly day:** a seeded live week with trades, journal answers and one kill-switch trip; `trader weekly` (CliRunner, fake Anthropic returning a commentary that quotes only facts, fake Telegram) stores the report, sends one message whose numbers equal `compute_metrics` for that week, passes `check_numbers`, and `GET /api/reports/weekly` returns it; a second run sends nothing.
- [ ] 6. Gate and commit `P5-T18: ...` (the LIVE results go in the activity log and this file's LIVE notes).

**LIVE steps** (dev only; Questrade read only; no fake rows in the live run; never print a secret; the soak protection rule applies):
1. **Before deploying:** P4-T19 is accepted and `trader-dev` is healthy; migration 0006 is applied (T1 LIVE); the current time is outside 09:15–16:30 ET on a session day and not within 10 minutes of a cron line (a restart at 19:58 would make the 20:00 nightly miss its run).
2. **Deploy:** from your worktree, `TRADER_ENV_FILE="/Users/stephen/Documents/Code/Claude Code/Trader/Trader/docker/.env.dev" bash Trader/docker/deploy.sh dev` → `200` from `https://trader-dev.sunspinner.ca/api/health` within 120 s (`ok` within 60 s more). `docker --context shared-docker-server inspect trader-dev --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}} {{.HostConfig.LogConfig.Config}}'` → `1073741824 2000000000 256 map[max-file:5 max-size:10m]`. `docker --context shared-docker-server exec trader-dev supercronic -test /app/docker/crontab` → valid.
3. **Replay (real data):** outside 09:15–16:30 ET on a session day and not within 10 minutes of a cron line (the replay shares the container with the live worker, and fetches from Questrade in `full` mode; if the summary says `offline`, the window was wrong: stop and report), `docker --context shared-docker-server exec trader-dev trader replay --from <the session 10 sessions before the last one> --to <the last complete session> --label "P5 LIVE check"` → exit 0 with a summary; run the same command again → exit 0; compare the two runs' normalized trades with a small Python helper in your scratch folder run through `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev python <helper> <id1> <id2>` (prints only "identical" or the first difference) → `identical`. During both runs: the live run's `compute_metrics` before and after is equal, no `notifications` or `job_runs` rows were added by the replays, and no `event_log` row without a run id or with the live run's id was added by them (the helper prints the counts only). Also record the container's peak memory during the run (`docker --context shared-docker-server stats --no-stream trader-dev` once a minute; it must stay below 80% of the 1 GiB limit, else stop the replay with `POST /api/replays/{id}/cancel` or by killing its process, and report).
4. **Web:** `https://trader-dev.sunspinner.ca/replay?id=<id1>` renders the comparison (checked through the Playwright live smoke, which now visits `/replay` read only: `SMOKE_MODE=live SMOKE_BASE_URL=https://trader-dev.sunspinner.ca uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev npm --prefix ../web run e2e -- tests/smoke.spec.ts` → passes).
5. **Weekly report:** on a weekday (so the next Saturday's cron reports a different week and LIVE 7 stays a real check), `docker --context shared-docker-server exec trader-dev trader weekly --date <a day of the last completed week>` → exit 0; the dev chat receives one weekly report message with the commentary (or a clear note); `weekly_reports` has the row with `cost_usd` ≤ 0.05; `/reports?week=<that date>` shows the commentary. Record the status, cost and word count (never the key).
6. **Restart in market hours:** on a normal (not early-close) session day, between 10:30 and 11:15 ET or between 12:00 and 13:15 ET (clear of 09:35, the 11:30 entry cancel and check-in, the 12:32 and 13:30 cron lines, 15:30 and 15:50), `docker --context shared-docker-server restart trader-dev`; within 60 s the worker heartbeat is `session` and fresh; afterwards the day's `job_runs` has no failed row caused by the restart, there is at most one notification per dedupe key (the unique index guarantees it; check no `unknown` row was created for an already-delivered message), and any working order of the live run is still working or filled normally. If the phase ends outside market hours, the orchestrator records this step for the next trading day instead of waiting (non-blocking).
7. **First Saturday:** on the first Saturday after the deploy whose week LIVE 5 did not already report, the 09:00 ET cron run sends the weekly report on its own and `job_runs` has `weekly` succeeded for that week's last session; the orchestrator records it (non-blocking).

---

## Resolved decisions (planner, 2026-09-27, each with its reason)

1. **Worst case in one bar** = entry then stop (the same-bar pass with the bar reopened at the entry fill), because SPEC §7.4 asks for it and it keeps one fill model and one broker loop.
2. **Half spread** = 5 bps of the reference price added to buys and taken from sells (`replay.half_spread_bps`), recorded in each fill's snapshot and kept out of `slippage`, so replay slippage compares with live slippage (which is measured against the real ask or bid).
3. **Replay never uses `job_runs`, `fire_event` or the relay,** so a replay can never count against the Phase 6 soak or send a message.
4. **Replay holds fetched Questrade data in memory only** (no cache writes, and only the bars it needs: opening bars for the run, 1-minute bars for the current day), so the rule "a replay writes only rows of its own run" has one exception only (the shared Questrade token refresh of a `full` replay) and can be tested by counting rows.
5. **Metrics move to Python** behind the same route and schema, because hand-calculated definitions (running-peak drawdown per range, trades without R counted) are clearer and testable as pure functions; the SQL view stays for ad-hoc queries.
6. **Retries are in-process, not extra cron lines,** because they need no crontab, SPEC table, Dashboard timeline or test changes and they alert once; a crash of the whole process is already caught by the pre-open check and can be re-run from the System page.
7. **The log mirror covers only `error` and `critical` lines and never alerts,** because warnings would flood `event_log` and the lines that must alert are already written with `log_event`.

## Open questions for Stephen (defaults chosen; the build does not wait on them)

1. **Replay setting overrides** create `replay`-scoped strategy config rows (a new `scope` column), so "what if" replays never touch the live settings. Default: yes.
2. **Market-hours replays run offline** (database data only) so they never compete with the live worker for Questrade's rate limit, and a Questrade-backed replay is limited to 4 requests per second. Default: yes.
3. **A position still open after the close in a replay** is force-closed at the last price (`replay_forced_close`) and counted, instead of being carried overnight. Default: yes.
4. **Weekly commentary budget:** uses `claude.model` (Sonnet 5), at most US$0.05 per report, counted in Saturday's daily Claude budget; the Telegram message includes the commentary text (self-contained, SPEC §4.4). Default: yes.
5. **SPEC §9 amendment** (weekly keyed by the week's last session; in-process retries, 3 attempts with 2 and 4 minute waits, one alert only after the last failure). Default: T17 applies it.
6. **Container limits** 1 GiB memory, 2 CPUs, 256 processes, Docker logs 10 MB × 5, on the shared Docker host (`192.168.68.73`). Default: these values; they are one line each to change.
7. **Kill-switch reset confirmation on Telegram** (an automatic switch reset in the web app sends "KILL SWITCH RESET ... Reason: ..."). Default: yes.
8. **`event_log` retention:** no pruning in version 1 (a few hundred rows per trading day, plus replay rows). Default: no pruning; revisit if the table passes 1 million rows.
9. **Phase 5 deploy during the soak:** deploying `trader-dev` on a weekday evening (after 16:30 ET) or a weekend does not break the "clean days" count, because no job or event fails. Default: deploy then, and do the market-hours restart check (T18 LIVE 6) on a normal trading day.
10. **Replay length:** at most 130 sessions (about six months) per run (`replay.max_sessions`). Default: 130.

## Notes for the P6 planner

- Promotion criterion 3 (SPEC §15.1) is `tests/replay/test_golden.py` (T18); run it on the image tag being promoted.
- The soak report (P6-T2) can ignore replay runs entirely: they never write `job_runs`; their `runs.status` shows failures separately.
- With T15's retries a (job, session) can have a `failed` row followed by a `succeeded` one: it is a clean day for that job. Count a job as failed only when its latest `job_runs` row for the session is `failed` (the intermediate attempts wrote `warning` events, not alerts).
- The weekly report of each soak week is stored in `weekly_reports` and is a ready-made progress summary for Stephen.

## Self-review (done by the plan author)

- **Coverage:** BR-41 (T11 trips, T10 reset confirmation, T6/T18 kill switches in replay), BR-52 (T2 metrics, T8 comparison), BR-54 (T6, T7, T8, T17 CLI), BR-60 (T10 run-to-date line), BR-61 (T9, T10, T12, T13, T17 cron), BR-62 (T12 columns), O4 and O5, the reliability NFRs (T14 mirror, T15 retries and restart, T16 limits, T18 LIVE 6), maintainability ("replay results that come out the same every time": T18 tests 1–2). SPEC §2 logging (T14), §7.4 (T3, T4), §8 (T5, T6, T18), §9 (T17), §10 runs and views (T1, T2), §11 replays and metrics (T2, T7, T12), §12 Replay and Reports (T8, T13), §16 determinism (T18). Master plan §7.5 outline tasks mapped in "Changes from the master-plan outline". Phase 4 notes for the P5 planner: export extended not recreated (T12), metrics behind the same route and `MetricsOut` (T2), reset panel not rebuilt (T10/T11), Reports commentary (T13), `resolve_run` reused for replays (T7, T8).
- **Concurrency:** T2–T16 depend only on T1 and own disjoint files; the shared registration points (`cli.py`, `runtime.py`, `worker.py`, `api/services.py`, `api/launcher.py`, `docker/crontab`, SPEC §9, master plan §7.1) belong to T17; `ROUTERS`, `types.ts`, `client.ts`, `http.ts`, `queryKeys.ts`, `App.tsx` and `NAV_ITEMS` are fixed in T1, so no build task edits them.
- **No implementation code** in this plan: interfaces are signatures and data shapes; behaviour and tests are in words.

## Verifier + spec review (P5-T0 attempt 1, 2026-09-27 14:43 MT): changes made

Checked against SPEC §7.4, §8, §9, §16, BRD BR-41/52/54/60–62 and the reliability NFRs, and every consumed name against trunk (f378a6a). Fixed in place:
- **Gate:** every gate is `bash Trader/build/gate.sh` (shared test lane), never `check.sh` directly (Global Constraints, T1 test 9).
- **P4 reconciliation:** T1 now waits for every P4 task except P4-T19 to be accepted (fix rounds on trunk), and a new Global Constraint lists what P4-T18 and the P4 fix rounds may still change and how builders adapt.
- **Questrade request size:** the client refuses windows over 20,000 intervals, so a whole-range `FiveMinutes` request per symbol would fail; T5 splits it (decision "Replay data", T5 behaviour and test 10).
- **Memory (soak protection):** the replay runs inside the live container; it keeps only opening bars and one day of 1-minute bars (decision "Replay data", T5 test 10, T18 LIVE 3 memory watch).
- **Isolation:** log-mirror rows of the replay process carry the replay's run id (T14 `run_id`, T17); the trading-topic SSE watermarks ignore replay rows (T7 test 9); the `api_credentials` token refresh of a `full` replay is named as the one shared write.
- **Determinism:** the runner sets the `ReplayClock` before each call; an explicit determinism rule list (no floats, no randomness, sorted inputs, no wall-clock fetch deadline); T18's static scan also checks `random`/`uuid`/`secrets`.
- **Fill model:** candle limit orders fill at the limit price, as the quote model (SPEC §7.2), never at a better open (T3 test 8).
- **Weekly report:** Saturday and Sunday map to the week just ended, matching the web's `tradingWeek` (T9, T12); the retry may not push a report past `reports.weekly_max_cost_usd` (T9 test 5).
- **Wiring gaps:** T17 also owns `tests/api/test_launcher.py` (pinned `CLI_ARGS`), and the log mirror covers `nightly`/`premarket`, which build their `Core` directly on trunk; T1 also updates the pinned `Topic`/`ManualJob` values in `tests/test_phase4_contracts.py`; `daily_summary_view` gains a keyword (T10); `PinnedRegistry` covers `enabled()`/`instance()` (T6).
- **LIVE steps:** no deploy, migration or replay within 10 minutes of a cron line; the market-hours restart avoids 11:30 and 13:30; the manual weekly run is on a weekday so the first Saturday cron is still a real check.
- **Retries and the soak:** a job counts as failed only when its latest row is failed (T15, notes for the P6 planner).
