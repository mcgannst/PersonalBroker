# Phase 3: Worker, Telegram and Schedule (specification plan)

> **For agentic workers:** Run under the gauntlet process in [`2026-09-26-build-master-plan.md`](2026-09-26-build-master-plan.md) (§4–§6). Read its **Global Constraints** and **Review Focus** first, then this plan's Global Constraints reminder and Review Focus below. This is a **specification** plan (master plan §6.6): it says WHAT to build and HOW TO KNOW it works. Builders design and write the code, matching the style of the existing code on trunk, and write each numbered acceptance test first (TDD). Each task has one checkbox per acceptance test plus one for the gate and commit: tick them in this file as you go and commit the file with your code.

**Goal:** A long-running worker that fires every strategy's session events on time, polls quotes for working orders, expires proposals, relays trading events to Telegram and runs the Telegram bot (inline approvals, signed callbacks, remote commands); the day-level cron jobs (pre-open check, check-ins, event backups, post-close with the candle archive and the daily summary); and the crontab that runs them. Proven end to end by a worker day driven through a fake Telegram.

**Architecture:** New packages `trader/notify/` (message types, message formats, notifier, relay) and `trader/adapters/telegram/` (API client, signed callbacks, bot, commands), plus `trader/engine/scheduler.py`, `trader/worker.py`, `trader/jobs/{preopen,checkin,events,postclose}.py` and a composition root `trader/runtime.py`. Processes talk only through PostgreSQL (SPEC §1): the engine (Phase 2) never calls Telegram; the worker's **relay** reads new rows (pending proposals, fills, error events, overlay decisions) and sends them. Cron jobs send their own messages (brief, pre-open, check-in, daily summary) through the same `Notifier`. **SPEC §1 mentions `LISTEN/NOTIFY`; this phase uses polling instead (decision):** the worker reads the tables every `quote_poll_seconds` (2 s) during the session and every `worker.idle_poll_seconds` otherwise, which meets every latency in SPEC §4.4 without a second connection type; `LISTEN/NOTIFY` can be added later for the P4 SSE stream without changing these contracts.

**Tech stack:** Python 3.12 via uv; SQLAlchemy 2, Alembic, psycopg 3; pydantic v2; `python-telegram-bot` v21 (new, used as a low-level API client only); structlog; pytest, pytest-asyncio, respx, testcontainers.

**Spec:** [`../BRD.md`](../BRD.md) BR-30, BR-31, BR-32, BR-33, BR-34, BR-42 (safety net), BR-60; [`../SPEC.md`](../SPEC.md) §1 (processes), §4.4 (Telegram), §5.1 (scheduled events), §6.2 (proposal expiry and escalations), §6.3 (`manual_pause`), §8 (candle archive), §9 (schedule), §14 (Telegram security); [`../../spikes/README.md`](../../spikes/README.md) S6 findings (acknowledge first, then edit; log Telegram's error body; no emoji in callback answers).

**Task IDs for the board:** P3-T1, P3-T2, P3-T3, P3-T4, P3-T5, P3-T6, P3-T7, P3-T8, P3-T9, P3-T10, P3-T11, P3-T12, P3-T13.

## Task list, dependencies and parallel lanes

| ID | Task | Depends on | Lane |
|---|---|---|---|
| P3-T1 | Contracts: migration 0004, settings keys, notify and Telegram types, stubs, fakes, dependency | P2 (every task past its Verifier, and the P2-B1 fix's migration 0003 on trunk) | A |
| P3-T2 | Logging unification (P1-REVIEW should-fix 3) | T1 | B |
| P3-T3 | Session event scheduler and async job runner (P1-REVIEW should-fix 4) | T1 | C |
| P3-T4 | Message formats (`Renderer`) | T1 | D |
| P3-T5 | Telegram API client and Notifier | T1 | E |
| P3-T6 | Telegram bot: update loop, signed callbacks, approvals, journal answers | T1 | F |
| P3-T7 | Telegram commands | T1 | G |
| P3-T8 | Notification relay | T1 | H |
| P3-T9 | Worker process | T1 | I |
| P3-T10 | Day-level jobs: pre-open, check-in, event backup | T1 | J |
| P3-T11 | Post-close job and candle archive | T1 | K |
| P3-T12 | Wiring: runtime composition, CLI commands, crontab, LIVE checks | T2–T11 | A |
| P3-T13 | Integration: a worker day with a fake Telegram | T12 | A |

**Critical path:** P3-T1 → P3-T9 (the largest build task) → P3-T12 → P3-T13: **4 tasks**.
**Maximum parallel width:** **10** (T2–T11 all start once T1 has passed its Verifier). The orchestrator's cap is 8 builders: start T9, T6, T3, T11, T8, T5, T7 and T10 first, then T4 and T2 (the two smallest) as slots free up.

Every build task (T2–T11) depends only on T1: it uses other tasks' code through the T1 contracts (protocols, dataclasses, ORM columns) and the fakes in `tests/fakes_telegram.py`, never through another build task's implementation. Only T12 and T13 use real implementations together.

**Changes from the master-plan outline (§7.3), with reasons:**
- **Contract-first T1** (new) holds every shared type, the migration and the stubs, so ten tasks can run at once.
- The outline's T3 (notifier and messages) is split into **T4** (pure message formats) and **T5** (API client and notifier): they share no code.
- **T8 relay** is new. The Phase 2 engine has no notifier (its alerts are `event_log` rows, SPEC §1 "only through PostgreSQL"), so the worker relays new rows to Telegram. This also delivers alerts raised by cron processes.
- **Logging** (outline "also in P3-T2") is its own small task **T2**; the job-runner fix (P1-REVIEW should-fix 4) goes to **T3**, which owns `trader/jobs/runner.py`.
- The outline's T8 (crontab) is folded into the **T12 wiring** task, which owns every shared registration point (`cli.py`, `docker/crontab`, `trader/runtime.py`).
- Outline T9 is **T13**.

**Contracts refined (master plan §7.1; names and meaning kept, update §7.1 in T12):**
1. **Notifier:** `Notifier.send(msg: OutboundMessage) -> None` is `async` (the worker and the jobs are async). `OutboundMessage` carries an optional `dedupe_key`; a message with a key already sent is not sent again. `send` never raises. Message builders are pure functions of view dataclasses, exposed as a `Renderer` (`trader/notify/messages.py`).
2. **Event firing:** `async fire_event(deps: FireDeps, key: str, session_date: date, *, force: bool = False) -> FireResult`. Idempotent through a `job_runs` row `event:<key>` using the same advisory-lock and skip rules as `run_job` (new `run_job_async`). Added: a late-firing policy (see T3), because a restarted worker or a cron backup can arrive after the event's time.
3. **Telegram library:** SPEC §2's `python-telegram-bot` v21 is kept, but only its low-level `telegram.Bot` is used, behind a `TelegramApi` protocol; the worker runs its own `getUpdates` loop instead of PTB's `Application`, so the bot shares the worker's event loop and tests use a simple fake.
4. **Web links** in messages use these paths under `PUBLIC_BASE_URL`, which the Phase 4 web app must serve: `/dashboard?proposal=<id>`, `/trades?position=<id>`, `/journal?date=<YYYY-MM-DD>`, `/system`.
5. **Crontab:** SPEC §9's lines, plus one extra `12:55 Mon–Fri trader event flatten` backup for early-close days (the 15:55 backup would come after a 13:00 close). The `weekly` line is added by P5-T6, when that command exists.

Every command below runs from the repository root of your worktree unless it says otherwise. "Run from `Trader/app`" means `uv --directory Trader/app run ...`. LIVE steps use `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev ...` (master plan §6 preamble).

## File map

One owner per file. Paths are under `Trader/app/` unless they start with `docker/` or `docs/`. T1 creates the stub modules marked (stub); the owning task replaces the stubs with the implementation.

| Path | Responsibility | Task |
|---|---|---|
| `trader/db/models.py` | Append `WorkerHeartbeat`, `TelegramCallback`, `Notification`, `NotifyCursor` | T1 |
| `trader/db/migrations/versions/0004_worker.py` | The four Phase 3 tables (number re-checked at build time, see T1) | T1 |
| `trader/settings_store.py` | Phase 3 runtime settings | T1 |
| `trader/market/sessions.py` | `session_phase`, `current_session` (two tiny pure helpers, real in T1) | T1 |
| `trader/notify/__init__.py`, `trader/notify/types.py` | `OutboundMessage`, `Button`, `MessageKind`, view dataclasses, `Notifier` and `Renderer` protocols | T1 |
| `trader/adapters/telegram/__init__.py`, `trader/adapters/telegram/types.py` | `Update`, `CallbackQuery`, `TelegramApi`, `TelegramApiError`, `CallbackIssuer`, `ProposalMessenger`, `CommandHandler` | T1 |
| `pyproject.toml`, `uv.lock` | Add `python-telegram-bot` | T1 |
| `tests/fakes_telegram.py` | `FakeTelegramApi`, `RecordingNotifier`, `FakeRenderer`, `FakeIssuer`, `FakeMessenger` | T1 |
| `tests/db/test_migration_0004.py`, `tests/test_runtime_settings_phase3.py`, `tests/test_phase3_contracts.py`, `tests/market/test_sessions.py`, `tests/notify/__init__.py` (empty) | T1 tests and the new test package | T1 |
| `trader/logging_setup.py`, `tests/test_logging_setup.py` | structlog configuration; stdlib routed through it | T2 |
| `trader/engine/scheduler.py` (stub) | Day plan, due events, `fire_event` | T3 |
| `trader/jobs/runner.py` | `run_job_async`; clean exit when success can't be recorded (T1 adds only the `run_job_async` stub, before T3 starts) | T3 |
| `tests/engine/test_scheduler.py`, `tests/jobs/test_runner_async.py` | T3 tests | T3 |
| `trader/notify/messages.py` (stub), `tests/notify/test_messages.py` | Every message format | T4 |
| `trader/adapters/telegram/api.py` (stub), `trader/notify/notifier.py` (stub) | PTB-backed `TelegramApi`; `TelegramNotifier`, `NullNotifier` | T5 |
| `tests/adapters/test_telegram_api.py`, `tests/notify/test_notifier.py` | T5 tests (`tests/notify/__init__.py` comes from T1) | T5 |
| `trader/adapters/telegram/callbacks.py` (stub), `trader/adapters/telegram/bot.py` (stub) | Signed callbacks; update loop, approvals, pause confirmation routing, journal answers, proposal messages | T6 |
| `tests/adapters/test_telegram_callbacks.py`, `tests/adapters/test_telegram_bot.py` | T6 tests | T6 |
| `trader/adapters/telegram/commands.py` (stub), `tests/adapters/test_telegram_commands.py` | `/status`, `/positions`, `/pnl`, `/pending`, `/pause`, `/resume`, `/help` | T7 |
| `trader/notify/relay.py` (stub), `tests/notify/test_relay.py` | DB rows → Telegram | T8 |
| `trader/worker.py` (stub), `tests/test_worker.py` | Worker loop, heartbeat, single-instance lock | T9 |
| `trader/jobs/preopen.py`, `trader/jobs/checkin.py`, `trader/jobs/events.py` (stubs), `tests/jobs/test_preopen.py`, `tests/jobs/test_checkin.py`, `tests/jobs/test_events.py` | Day-level jobs | T10 |
| `trader/jobs/postclose.py` (stub), `trader/market/repository.py` (add `upsert_candle_archive`), `tests/jobs/test_postclose.py` | Post-close and archive | T11 |
| `trader/runtime.py` (stub), `trader/cli.py`, `docker/crontab`, `tests/test_runtime.py`, `tests/test_crontab.py`, `tests/test_cli.py` (add smoke tests), `docs/plans/2026-09-26-build-master-plan.md` (§7.1 rows) | Wiring | T12 |
| `tests/integration/test_worker_day.py` | End-to-end worker day | T13 |

## Global Constraints (reminder)

The master plan's **Global Constraints** apply word for word (trunk only, uv, SPEC §3 layout, DDL only in migrations, `timestamptz` UTC, `Decimal` money, time only from a `Clock`, no secrets printed or committed, no network in unit or adapter tests, testcontainers for DB tests, `check.sh` before every commit, `P3-Tn: ` commit prefix and the `Co-Authored-By` trailer, explicit staging). Phase-specific additions:

- **No wall clock, no real sleeping.** Loops take an injectable `sleep: Callable[[float], Awaitable[None]]` (default `asyncio.sleep`) and read time only from the `Clock`. Tests drive `FixedClock` and a fake sleep; no test waits in real time.
- **Telegram secrets.** The bot token appears only inside the API client. Never log a URL, a request, or an exception's `repr` from the Telegram client (PTB and httpx put the token in the URL); log the exception type and Telegram's error description. T2 sets the `telegram` and `httpx` loggers to WARNING.
- **Telegram never blocks trading.** Nothing in the engine, the worker loop or a job waits on a Telegram call that can raise: every send goes through `Notifier.send`, which catches everything.
- **Times shown to Stephen** are Mountain Time (`TZ_DISPLAY`, `America/Edmonton`) labelled `MT`; schedule logic is ET from the calendar; storage is UTC.
- **Telegram HTML:** messages use `parse_mode="HTML"`, and every dynamic string (tickers, reasons, Claude text, errors) is escaped with `html.escape`. Callback answers (toasts) are plain text, at most 200 characters, **no emoji** (S6).
- **IDs:** `symbol_id` is always `trader.symbols.id`; the run is the live run from `get_live_run`.
- **Migrations:** Phase 3's migration is `0004` (`0003` belongs to the Phase 2 B1 fix round, the `trades.exit_reason` length fix); it names the schema explicitly and keeps `test_models_match_migrated_schema` and the Alembic autogenerate check passing.
- **Runtime settings:** every new field declares `alias="<db key>"` (P2 rule); no `model_validator`.
- Phase 1–2 interfaces used, read from trunk: `run_job`, `log_event`, `session_scope`, `SessionCalendar`, `FixedClock`, `et_date`, `QuestradeAuth.health()/access()`, `get_live_run(factory, clock, settings) -> RunInfo`, `StrategyRegistry.enabled() -> list[tuple[Strategy, StrategyConfigView]]`, `SessionOffset.resolve(cal, session)`, `Engine.run_event(event_key, session_date) -> EventResult` / `poll_quotes() -> list[FillEvent]` / `tick(now)` / `end_of_session(session_date) -> list[PositionView]`, `build_engine(core, client, catalysts) -> Engine`, `ProposalService.decide(proposal_id, decision, via, actor) -> DecisionResult` (raises `KeyError` for an unknown proposal of the run), `KillSwitches.active(run_id, session_date) -> list[ActiveSwitch]`, `.blocking(run_id, session_date)`, `.pause(run_id, session_date, actor) -> bool`, `.resume(run_id, actor) -> bool`, `QuestradeAuth.access()/health() -> TokenHealth`, `MarketDataService.quotes/candles/universe/universe_status/opening_bars`, `run_premarket(deps, session_date)` (its detail carries `"brief"`), `EnvSettings.telegram_bot_token/telegram_chat_id/session_secret/public_base_url/tz_display`. At plan time P2-T13 (`trader/engine/orchestrator.py`) and P2-T15 were not yet on trunk; their names above come from the Phase 2 plan's code. If a name on trunk differs from this plan, use the trunk name and say so in your report.

## Review Focus

The five Phase 3 failure modes most likely to hurt Stephen, most likely first. Each is pinned by acceptance tests in the task named.

1. **A session event fired twice, or not at all** (the worker and the cron backup both fire; the worker restarts mid-session; an early close; the worker is down at 9:35). Expected: each `(key, session)` runs exactly once; a late event within `scheduler.late_grace_seconds` still fires; a late entry event beyond the grace is recorded as missed and alerted, while safety events (`flatten`, `entry_cancel`, `overlay_decision`) fire whenever they are late, until the close; a missed event is alerted once, not on every step. [T3 tests 3–8 and 12; T10 tests 7–9; T9 test 7]
2. **A forged, replayed or foreign Telegram callback, or a double decision** (a tap and a web approval together; a tap after expiry; an old button). Expected: only a correctly signed, unused callback from the configured chat reaches `ProposalService.decide`; the first decision wins; a failed acknowledgement never undoes a decision. [T6 tests 1–10]
3. **Telegram failing** (HTTP 400/429/5xx, a timeout, 409 Conflict from a second poller). Expected: trading carries on; the error is logged with Telegram's description and never the token; no message is sent twice after a retry or a restart. [T5 tests 4–9; T8 tests 6–8; T9 test 9]
4. **A worker crash or a second worker** (restart at 10:00 with a working order and a pending proposal; two workers started). Expected: the restarted worker resumes polling and expiring without duplicate fills, proposal messages or events; a second worker exits at once. [T9 tests 6–8; T13 test 2]
5. **Holidays, early closes and DST** (Thanksgiving, the day after at 13:00 ET, the March and November clock changes). Expected: every job does nothing on a holiday; the day plan follows the real close; cron lines are ET (`CRON_TZ=America/New_York`); the worker's times come from the calendar in UTC. [T3 tests 1–2; T10 tests 1, 5; T11 test 1; T12 tests 5–6]

---

### Task P3-T1: Contracts: migration 0004, settings keys, notify and Telegram types, stubs, fakes, dependency

**Goal:** Create every shared contract of Phase 3 so T2–T11 can be built in parallel against it. Small: declarations, one migration, two tiny helpers, fakes. No behaviour beyond that. SPEC §4.4, §9, §10 (new operational tables), §13.

**Files:** see the file map (T1 rows and every (stub) module). Also update any test that pins the Alembic head revision (none did at plan time).

**Migration number (re-check at build time):** revision `0003` is taken by the Phase 2 B1 fix round (the `trades.exit_reason` length fix, `0003_*.py`), which may still be landing when this plan is written. Phase 3's migration is therefore **`0004_worker.py`, `revision = "0004"`, `down_revision = "0003"`**. Before writing it, the builder pulls trunk and runs `uv --directory Trader/app run alembic heads` (and lists `trader/db/migrations/versions/`): there must be exactly one head. If the head is not `0003` (another Phase 2 fix round added a migration), use the next free number instead, set `down_revision` to that head, rename the migration and its test file to match, and say so in the report. If `0003` is not on trunk yet, stop and report (T1 must chain onto it, never branch beside it).

**Interfaces (produce):**
- **Dependency:** `python-telegram-bot>=21,<22` via `uv add` (updates `pyproject.toml` and `uv.lock`). Add `telegram.*` to the mypy `ignore_missing_imports` overrides only if its bundled types fail `mypy --strict`.
- **Migration 0004 and ORM models** (schema `trader`, all timestamps `timestamptz`):
  - `WorkerHeartbeat` (`worker_heartbeats`): `process` varchar(30) PK (`"worker"`), `pid` int, `host` varchar(100), `started_at`, `beat_at`, `session_date` date NULL, `phase` varchar(20) (`starting`/`idle`/`session`/`stopping`/`stopped`), `detail` jsonb NULL.
  - `TelegramCallback` (`telegram_callbacks`): `nonce` varchar(16) PK, `kind` varchar(20) (`proposal`/`pause`/`journal`), `ref` varchar(50), `chat_id` bigint, `message_id` bigint NULL, `created_at`, `expires_at` NULL, `used_at` NULL, `used_action` varchar(20) NULL; index `ix_telegram_callbacks_kind_ref` on (`kind`, `ref`).
  - `Notification` (`notifications`): `id` bigint identity PK, `kind` varchar(30), `dedupe_key` varchar(200) NULL with a unique index, `text` text, `created_at`, `sent_at` NULL, `status` varchar(10) (`sending`/`sent`/`failed`), `message_ids` jsonb NULL, `attempts` int default 0, `error` text NULL.
  - `NotifyCursor` (`notify_cursors`): `stream` varchar(30) PK, `last_id` bigint, `updated_at`.
- **Settings** (`RuntimeSettings`, field → DB key, default, bounds): `worker_heartbeat_seconds` → `worker.heartbeat_seconds`, 15, 5–300; `worker_heartbeat_stale_seconds` → `worker.heartbeat_stale_seconds`, 120, 30–3600; `worker_idle_poll_seconds: float` → `worker.idle_poll_seconds`, 30, 5–300; `scheduler_late_grace_seconds` → `scheduler.late_grace_seconds`, 120, 0–3600; `scheduler_always_fire_late: list[str]` → `scheduler.always_fire_late`, `["flatten", "entry_cancel", "overlay_decision"]`, each matching `^[a-z][a-z0-9_]{0,38}$` (at most 39 characters, so the job name `event:<key>` fits `job_runs.job` and the failure-event source `job.event:<key>` fits `event_log.source`, both varchar(50)), no duplicates; `telegram_poll_timeout_seconds` → `telegram.poll_timeout_seconds`, 30, 1–50; `telegram_confirm_ttl_seconds` → `telegram.confirm_ttl_seconds`, 60, 10–600; `telegram_relay_catchup_max` → `telegram.relay_catchup_max`, 20, 0–200; `preopen_notify_when_ok: bool` → `preopen.notify_when_ok`, true; `postclose_archive_top_n` → `postclose.archive_top_n`, 20, 0–100.
- **`trader.market.sessions`** (real, pure): `SessionPhase = Literal["closed_day", "pre_market", "open", "after_close"]`; `session_phase(cal, now) -> SessionPhase` (`closed_day` on a weekend or holiday; otherwise by `session_open`/`session_close`, open is `[open, close)`); `current_session(cal, now) -> date` (today's ET date if it is a session, else the next session).
- **`trader.notify.types`** (frozen, slots dataclasses; prices `Decimal`; times UTC `datetime`):
  - `MessageKind = Literal["proposal", "fill", "stop_hit", "flatten", "overlay", "kill_switch", "job_failure", "token_failure", "escalation", "alert", "daily_summary", "weekly_report", "premarket_brief", "preopen", "checkin", "reply"]`.
  - `Button(text: str, callback_data: str)`; `OutboundMessage(kind: MessageKind, text: str, buttons: tuple[tuple[Button, ...], ...] = (), dedupe_key: str | None = None, silent: bool = False)`.
  - Views: `ProposalView(proposal_id, kind, status, ticker, side, order_type, qty, stop, limit, stop_loss, risk_usd, reason, strategy_key, created_at, expires_at, decided_via: str | None, error: str | None)`; `FillView(fill_id, ticker, side, purpose, qty, price, ts, reason, position_id, stop_loss, pnl, pnl_r)` (`pnl`/`pnl_r` set when the fill closed a position); `OverlayView(decision, spy_return: Decimal | None, prior_close: Decimal | None, price: Decimal | None, position_ids, ts, note: str)` (the three prices are None when `spy_overlay` held for lack of data; `note` is the event message); `AlertView(kind: MessageKind, level, source, message, ts, data: Mapping[str, Any])`; `PositionLine(position_id, ticker, qty, entry, last, stop, unrealized_pnl, unprotected_seconds)`; `StatusView(now, phase: SessionPhase, session_date, next_event_key, next_event_at, approval_mode, blocking_switches: tuple[str, ...], token_ok, token_age_hours, token_error, heartbeat_age_seconds, positions: tuple[PositionLine, ...], pending_count)`; `PnlView(session_date, realized_today, unrealized, week_to_date, equity, peak_equity, drawdown_pct)`; `TradeLine(ticker, qty, entry, exit, pnl, pnl_r, exit_reason)`; `DailySummaryView(session_date, trades: tuple[TradeLine, ...], realized_pnl, fees, equity, drawdown_pct, open_positions: tuple[PositionLine, ...], decisions, avg_decision_seconds, unprotected_seconds, blocking_switches, archive: Mapping[str, int])`; `Check(name, ok: bool, level: Literal["info", "warning", "error"], detail: str)`; `PreopenView(session_date, approval_mode, checks: tuple[Check, ...])`.
  - `Notifier` protocol: `async send(msg: OutboundMessage) -> None`.
  - `Renderer` protocol (T4 implements; everyone else takes one): `proposal(v, buttons) -> OutboundMessage`, `proposal_closed(v: ProposalView, final_status: str, via: str | None) -> str`, `fill(v: FillView) -> OutboundMessage`, `overlay(v) -> OutboundMessage`, `alert(v: AlertView) -> OutboundMessage`, `daily_summary(v, buttons) -> OutboundMessage`, `journal_answered(session_date, rules_followed: bool) -> OutboundMessage`, `premarket_brief(session_date, brief: str) -> OutboundMessage`, `preopen(v) -> OutboundMessage`, `checkin(v: StatusView, at_label: str) -> OutboundMessage`, `status(v) -> OutboundMessage`, `positions(lines, now) -> OutboundMessage`, `pnl(v) -> OutboundMessage`, `pause_confirm(buttons) -> OutboundMessage`, `help() -> OutboundMessage`, `reply(text: str) -> OutboundMessage`, `weekly_link(week_ending: date) -> OutboundMessage`.
- **`trader.adapters.telegram.types`:** `CallbackQuery(id: str, data: str, chat_id: int, from_id: int, message_id: int | None)`; `Update(update_id: int, chat_id: int | None, from_id: int | None, text: str | None, callback: CallbackQuery | None)`; `TelegramApiError(Exception)` with `status: int | None`, `description: str`, `retry_after: float | None`; `TelegramApi` protocol: `async get_updates(offset: int | None, timeout: int) -> list[Update]`, `async send_message(chat_id: int, text: str, buttons: tuple[tuple[Button, ...], ...] = (), silent: bool = False) -> int` (message id), `async edit_message(chat_id: int, message_id: int, text: str, buttons: tuple[tuple[Button, ...], ...] = ()) -> None`, `async edit_buttons(chat_id: int, message_id: int, buttons: tuple[tuple[Button, ...], ...] = ()) -> None` (changes only the inline keyboard), `async answer_callback(callback_id: str, text: str | None = None) -> None`, `async aclose() -> None`. `CallbackKind = Literal["proposal", "pause", "journal"]`; `CallbackIssuer` protocol: `issue(kind, ref: str, actions: Sequence[str], chat_id: int, ttl_seconds: int | None) -> tuple[str, dict[str, str]]` (nonce, action → callback data), `bind(nonce: str, message_id: int) -> None`; `ProposalMessenger` protocol: `async send_proposal(proposal_id: int, *, resend: bool = False) -> bool`, `async sync_closed() -> int`; `CommandHandler` protocol: `async handle(text: str) -> list[OutboundMessage]`, `async confirm_pause(action: str) -> str`.
- **Stubs** (functions raise `NotImplementedError`, classes have their constructor signature and method signatures) for: `trader/engine/scheduler.py`, `run_job_async` in `trader/jobs/runner.py` (T1 appends only this stub to the existing file; T3 then owns the file), `trader/notify/messages.py`, `trader/notify/notifier.py`, `trader/notify/relay.py`, `trader/adapters/telegram/{api,callbacks,bot,commands}.py`, `trader/worker.py`, `trader/jobs/{preopen,checkin,events,postclose}.py`, `trader/runtime.py`, with the exact signatures in each owning task's Interfaces below.
- **`tests/fakes_telegram.py`:** `FakeTelegramApi` (records every call in order as `calls: list[tuple[str, dict[str, Any]]]`, numbers messages from 1, `queue_updates(*updates: Update)`, `fail(method, exc, times=1)`, and helpers `callback_update(data, chat_id, message_id)` and `text_update(text, chat_id)`); `RecordingNotifier` (`sent: list[OutboundMessage]`, honours `dedupe_key` like the real one); `FakeRenderer` (every method returns an `OutboundMessage` whose text is the view's `repr`, and records `(method, args)`); `FakeIssuer` (deterministic nonces `n1`, `n2`, … and data `"<kind>:<ref>:<action>:<nonce>"`); `FakeMessenger` (records sent proposal ids).

**Behaviour and decisions:**
- The four tables are operational, not trading data, so they have no `run_id` (like `job_runs`).
- `notifications.dedupe_key` is unique where not NULL; many rows may have NULL.
- The stubs import cleanly, so `mypy trader` and the contract test pass with the stubs in place.

**Acceptance tests:**
- [ ] 1. After `alembic upgrade head` the four tables exist with the columns, keys and index above; downgrade to the previous head (`0003`) drops them and leaves the Phase 2 tables intact; the ORM models match the migrated schema (the P1 comparison test stays green).
- [ ] 2. Two `notifications` rows with the same `dedupe_key` violate the unique index; two with NULL do not.
- [ ] 3. Every new setting has its default, rejects a value outside its bounds, and `scheduler.always_fire_late` rejects a duplicate or a malformed key.
- [ ] 4. `session_phase`: Saturday → `closed_day`; Thanksgiving 2026-11-26 → `closed_day`; 09:29:59 ET on a session → `pre_market`; 09:30:00 → `open`; 13:00:00 ET on 2026-11-27 (early close) → `after_close`; 15:59 ET on a normal day → `open`. `current_session` on Saturday 2026-10-03 → Monday 2026-10-05.
- [ ] 5. The contract test imports every stub module, checks every view is a frozen dataclass, `FakeTelegramApi` satisfies `TelegramApi`, `RecordingNotifier` satisfies `Notifier`, `FakeRenderer` satisfies `Renderer`, and P2's `Engine` satisfies both `WorkerEngine` (T9 stub) and `EventRunner` (T3 stub) (structural checks via typed assignments, which mypy verifies; the protocols are part of the stubs).
- [ ] 6. Gate: `bash Trader/app/scripts/check.sh` passes; commit `P3-T1: ...` and push.

**LIVE step:** apply migration 0004 to `trader_dev` (`env.py` reads `MIGRATION_DATABASE_URL`, the owner role):
`uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev alembic upgrade head` → `Running upgrade 0003 -> 0004` (preceded by `0002 -> 0003` if the B1 fix hasn't been applied to `trader_dev` yet); then `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev alembic current` → `0004 (head)`. Record it in the activity log. (If the number changed at build time, the expected output changes with it.)

---

### Task P3-T2: Logging unification (P1-REVIEW should-fix 3)

**Goal:** One structured logging setup for every process: structlog configured once, stdlib loggers (the FinViz scraper, PTB, httpx, SQLAlchemy) routed through it, and token-bearing loggers kept quiet. SPEC §2 (structlog, JSON to stdout). Mirroring to `event_log` and redaction stay in P5-T8.

**Interfaces:**
- Produces in `trader.logging_setup` (keep `HTTP_LOGGERS` and `quiet_http_loggers()`; add `"telegram"` to the quiet list): `configure_logging(process: str, *, json: bool = True, level: str = "INFO") -> None`. Idempotent (a second call changes nothing and adds no second handler).
- Consumes: nothing new. T12 calls it at the start of every CLI command and of the worker.

**Behaviour and decisions:**
- structlog renders JSON lines to stdout with `timestamp` (ISO UTC), `level`, `logger`, `event`, `process` (the argument), plus the bound fields. With `json=False` (a TTY during development) it uses the console renderer.
- The root stdlib logger gets one handler using structlog's `ProcessorFormatter`, so a `logging.getLogger("trader.adapters.finviz")` record comes out in the same JSON shape with its logger name.
- `quiet_http_loggers()` still runs inside `configure_logging` (httpx, httpcore and now telegram at WARNING), because the Questrade and Telegram tokens travel in URLs.
- Exceptions are rendered with `format_exc_info` into an `exception` field.
- `bootstrap.build_core` keeps calling `quiet_http_loggers()` only (it must not configure logging for library users and tests).

**Acceptance tests:**
- [ ] 1. After `configure_logging("cron")`, a structlog `log.info("x", a=1)` produces one JSON line with `event == "x"`, `a == 1`, `process == "cron"`, `level == "info"` and a UTC timestamp.
- [ ] 2. A stdlib `logging.getLogger("trader.adapters.finviz.scraper").warning("blocked")` produces one JSON line with that logger name and `level == "warning"`.
- [ ] 3. Calling `configure_logging` twice leaves exactly one root handler, and a message is printed once.
- [ ] 4. `httpx`, `httpcore` and `telegram` loggers are at WARNING after configuration: an INFO record from `httpx` containing `bot123:SECRET` is not emitted.
- [ ] 5. An exception logged with `log.exception` has an `exception` field containing the traceback text.
- [ ] 6. The existing `tests/test_logging_setup.py` tests still pass.
- [ ] 7. Gate and commit `P3-T2: ...`.

---

### Task P3-T3: Session event scheduler and async job runner (P1-REVIEW should-fix 4)

**Goal:** Turn every enabled strategy's `schedule()` into today's real event times, decide which events are due, and fire each `(event, session)` exactly once across the worker and the cron backups. Add the async variant of `run_job` and make a failure to record success end cleanly. SPEC §5.1, §9; master plan §7.1 "Event firing".

**Interfaces:**
- Consumes: `Strategy.schedule(cal)`, `ScheduledEvent`, `SessionOffset.resolve(cal, session)` (P2-T6); `StrategyRegistry.enabled()` (P2-T6); `Engine.run_event(event_key, session_date) -> EventResult` (P2-T13; `EventResult.strategies: list[str]`, `.outcomes: list[IntentOutcome]`) through the `EventRunner` protocol; `JobRun`, `log_event`, `session_scope`; `RuntimeSettings.scheduler_*` (T1).
- Produces in `trader.jobs.runner`: `async run_job_async(factory, clock, job: str, session_date: date, fn: Callable[[], Awaitable[dict[str, Any]]], force: bool = False) -> JobOutcome` with exactly `run_job`'s semantics (advisory lock on a dedicated connection, `skipped` "already succeeded" / "already running", abandoned `running` rows marked failed, failures recorded with an error event).
- Produces in `trader.engine.scheduler`:
  - `EVENT_JOB_PREFIX = "event:"`; `event_job(key) -> str`.
  - `PlannedEvent(key: str, at: datetime, strategies: tuple[str, ...], always_fire_late: bool)` (frozen).
  - `DayPlan(session_date: date, is_session: bool, open: datetime | None, close: datetime | None, events: tuple[PlannedEvent, ...])` (events sorted by time, then key).
  - `day_plan(strategies: Sequence[Strategy], cal: SessionCalendar, session_date: date, settings: RuntimeSettings) -> DayPlan`.
  - `fired_keys(factory, session_date) -> set[str]`: the **settled** keys, i.e. those with a succeeded `event:<key>` job run, a failed run whose error starts with `missed:`, or `MAX_EVENT_ATTEMPTS` failed runs. Settled keys are never due again, so a missed or persistently failing event is recorded and alerted once (or three times), not on every 2 s worker step and every check-in. (`fire_event` itself does not consult it: an explicit `trader event KEY` still runs.)
  - `due_events(plan: DayPlan, now: datetime, fired: set[str]) -> list[PlannedEvent]` (unfired events with `at <= now`, in time order).
  - `EventRunner` protocol: `async run_event(event_key: str, session_date: date) -> Any` (parameter names as P2's `Engine.run_event`, so the engine satisfies it structurally).
  - `MAX_EVENT_ATTEMPTS = 3`.
  - `FireDeps(factory, clock, calendar, settings: Callable[[], RuntimeSettings], plan: Callable[[date], DayPlan], runner: Callable[[], Awaitable[EventRunner]])`.
  - `FireStatus = Literal["fired", "skipped", "missed", "too_early", "not_scheduled", "not_session", "failed"]`; `FireResult(key, session_date, status: FireStatus, detail: dict[str, Any])`.
  - `async fire_event(deps: FireDeps, key: str, session_date: date, *, force: bool = False) -> FireResult`.

**Behaviour and decisions:**
- `day_plan` resolves each `ScheduledEvent.at` with the calendar, so early closes follow automatically. Two enabled strategies may share a key (the engine runs every strategy with that key); if they resolve it to different times, the earliest wins and an `error` event (source `scheduler`) names both. `always_fire_late` is `key in settings.scheduler_always_fire_late`. A key longer than 39 characters (see the settings regex) is left out of the plan with one `error` event, because its job name would not fit `job_runs.job`.
- `fire_event` order: not a session → `not_session` (nothing written); key not in the plan → `not_scheduled`; `now < at` and not `force` → `too_early`; `now > at + late_grace` and not `always_fire_late` and not `force` → `missed`: write a failed `job_runs` row `event:<key>` with error `missed: <n>s late` (through `run_job_async` with a body that raises a `MissedEvent`, so it is recorded like any failure, including its `error` event, which the relay turns into an alert) — unless the key already succeeded, which gives `skipped`; a safety event after the session close is `missed` too (nothing left to do). Otherwise `run_job_async(event_job(key), session_date, body)`, where the body builds the runner lazily (`deps.runner()`, so a skipped backup never opens a Questrade client) and returns `{"strategies": <strategy keys run>, "outcomes": <number of intents handled>}` from the `EventResult`. The `JobOutcome` maps to `fired` / `skipped` / `failed`.
- A second concurrent `fire_event` for the same key gets `skipped` ("already running") from the advisory lock; a later one gets `skipped` ("already succeeded").
- **Late policy (SPEC ambiguity resolved):** SPEC §9 has the cron backup at 09:36 (55 s after 09:35:05) and at 15:55 (5 min after 15:50) but says nothing about how late an event may fire. An entry event far too late (a 10:00 ORB entry) is worse than none, while an exit or cancel must never be skipped. Hence the grace (default 120 s) and the `always_fire_late` list.
- **Clean exit (P1-REVIEW should-fix 4):** in `run_job` and `run_job_async`, if marking the run `succeeded` fails (a DB error or `JobRunMissing`), log `job.record_success_failed` at critical level and return `JobOutcome("failed", detail, error="succeeded but could not be recorded: <ExceptionType>")` instead of raising; the CLI then prints one line and exits 1 (no traceback).

**Acceptance tests:**
- [ ] 1. With `orb_sip` and `spy_overlay` (default params), the plan for Tue 2026-10-06 has `orb_open` 13:35:05Z, `entry_cancel` 15:30Z, `overlay_decision` 19:30Z, `flatten` 19:50Z; for Fri 2026-11-27 (13:00 ET close) `overlay_decision` 17:30Z and `flatten` 17:50Z.
- [ ] 2. The plan for Thanksgiving is `is_session=False` with no events, and `fire_event` returns `not_session` and writes nothing.
- [ ] 3. `fire_event("orb_open")` at 09:35:05 ET runs the runner once and records `event:orb_open` succeeded; a second call returns `skipped` and the runner count stays 1.
- [ ] 4. Two concurrent `fire_event` calls (asyncio.gather, real DB) run the runner once; the other result is `skipped`.
- [ ] 5. At 09:35:00 → `too_early`, nothing written; with `force=True` it fires.
- [ ] 6. At 09:36:00 (55 s late) `orb_open` fires; at 09:40:00 (295 s late, grace 120) it is `missed`, a failed job run and an `error` event exist, and the runner was never built.
- [ ] 7. `flatten` 20 minutes late (before the close) still fires; after the close it is `missed`; a `flatten` that already succeeded returns `skipped` at 15:55 even though it is late (the cron backup on a normal day).
- [ ] 8. The runner raising → `failed`, error recorded; a later call runs it again (a failed run is retried).
- [ ] 9. Two strategies scheduling one key at different times → the earliest time and one `error` event.
- [ ] 10. `run_job_async` has `run_job`'s skip, lock and failure semantics (three tests mirroring the P1 runner tests).
- [ ] 11. `run_job` and `run_job_async` whose success update fails (the row deleted underneath, or the session factory made to fail on the second commit) return `failed` with "could not be recorded" and do not raise.
- [ ] 12. Settled keys: after `orb_open` is `missed`, `fired_keys` contains it and `due_events` no longer returns it (exactly one failed run and one `error` event after ten further `due_events`/`fire_event` rounds driven the way the worker drives them); a runner that fails three times makes the key settled, and a fourth round does not call it.
- [ ] 13. Gate and commit `P3-T3: ...`.

---

### Task P3-T4: Message formats (`Renderer`)

**Goal:** Every Telegram message of SPEC §4.4, self-contained (ticker, quantity, prices, stop, P&L, reason) with a home-network link, as pure functions of the T1 views. BR-32, BR-33, BR-60.

**Interfaces:**
- Consumes: the views, `OutboundMessage`, `Button`, `Renderer` (T1).
- Produces in `trader.notify.messages`: `TELEGRAM_LIMIT = 4096`; `MessageRenderer(base_url: str, tz: ZoneInfo)` implementing `Renderer`; `link(base_url, path) -> str`; `fmt_money(Decimal) -> str` (`$1,234.56`, negative `-$12.30`); `fmt_price(Decimal) -> str` (4 dp trimmed to at least 2); `fmt_pct(Decimal) -> str` (`+1.23%`); `fmt_time(dt, tz) -> str` (`09:35 MT`); `fmt_duration(seconds) -> str` (`3m 20s`).

**Behaviour and decisions:**
- **Proposal** (kind `proposal`): headline by kind (`ENTRY`, `PROTECTIVE STOP`, `EXIT`, `CANCEL`), ticker, side, qty, order type with stop/limit, stop loss, risk in dollars, the strategy and reason, expiry time in MT with the minutes left, and `link(/dashboard?proposal=<id>)`. Buttons: one row `✅ Approve` / `❌ Reject` from the given buttons. `proposal_closed` gives the edited text: the same body with a final line `Approved via telegram at 09:36 MT` / `Rejected` / `Expired` / `Failed: <error>` (the broker refused the approved order) / `Already decided (<status>)`. A view whose status is `auto_approved` or `submitted` with `decided_via = auto` (auto mode, BR-30) renders with an `AUTO` prefix on the headline (`AUTO ENTRY ...`), no buttons and no expiry line.
- **Fill** by purpose and reason: entry → kind `fill` (`BOUGHT 33 AAA @ 21.5608, stop 21.41`); a stop order → `stop_hit` (`STOPPED OUT`); an exit with reason `flatten_close` → `flatten`; other exits → `fill` with the reason. A closing fill adds P&L in dollars and R. Link `/trades?position=<id>`.
- **Overlay:** decision (hold or exit), SPY return from the prior close, prior close and price, the positions affected.
- **Alert** by `AlertView.kind`: `kill_switch` (switch, value vs threshold, "entries blocked; reset in the web app" for automatic switches), `job_failure` (job, session, error first 300 characters), `token_failure` ("Questrade token refresh failed: <error>. Paste a new token in Settings."), `escalation` (unprotected position, expired flatten; the message from the event plus its data), `alert` (anything else). Link `/system`.
- **Daily summary:** date, each trade line, realized P&L, fees, equity, drawdown, open positions (a loud `STILL OPEN` line if any, BR-42), decisions and average decision time, total unprotected time (BR-33), kill switches, candle-archive counts, `Rules followed?` with the `Yes` / `No` buttons, link `/journal?date=<d>`. `journal_answered` gives a short reply (`Journal 2026-10-06: rules followed = Yes`).
- **Pre-market brief:** a heading plus the P2-T14 brief text as is (escaped).
- **Pre-open:** approval mode and one line per check with `OK` / `WARNING` / `ERROR`; kind `preopen`.
- **Status / check-in / positions / pnl / help / pause_confirm / reply / weekly_link** as their names say; `/help` lists the seven commands with one-line descriptions (SPEC §4.4 table).
- Every dynamic string is HTML-escaped. Text longer than `TELEGRAM_LIMIT` is cut at the last line break before the limit with a final `… (truncated, see the web app)` line; buttons are kept. No message is empty.

**Acceptance tests:**
- [ ] 1. An entry proposal view renders ticker, qty, buy stop, stop loss, risk dollars, reason, expiry in MT, the link with the proposal id, and two buttons in one row carrying the given callback data.
- [ ] 2. Each proposal kind renders its headline; `proposal_closed` renders each final status (including `failed`); an auto-approved entry renders `AUTO ENTRY` with no buttons.
- [ ] 3. Entry fill, stop fill, flatten fill and overlay exit fill render kinds `fill`, `stop_hit`, `flatten`, `fill`; closing fills include P&L and R (for example `+$10.82 (+2.17R)`).
- [ ] 4. Each alert kind renders its specific text; a kill-switch alert for `max_drawdown_pct` says it needs a web-app reset, `manual_pause` does not.
- [ ] 5. The daily summary renders trades, P&L, equity, drawdown, decision time, unprotected time, archive counts and the Yes/No buttons; with an open position it contains `STILL OPEN`.
- [ ] 6. A reason containing `<b>&` is escaped; a 10,000-character brief is cut below 4096 characters on a line break with the truncation line.
- [ ] 7. Times render in MT: 13:35:05Z on 2026-10-06 (MDT) → `07:35 MT`; 14:35:05Z on Mon 2026-11-02, the first session after the clocks change on Sun 2026-11-01 (MST) → `07:35 MT`.
- [ ] 8. `MessageRenderer` satisfies the `Renderer` protocol (typed assignment) and every method returns a non-empty message.
- [ ] 9. Gate and commit `P3-T4: ...`.

---

### Task P3-T5: Telegram API client and Notifier

**Goal:** A `TelegramApi` implementation on `python-telegram-bot`'s `telegram.Bot`, and the `Notifier` that sends `OutboundMessage`s through it: never raising, deduplicated, logging Telegram's error body, never the token. SPEC §4.4, §14; S6 findings.

**Interfaces:**
- Consumes: `TelegramApi`, `TelegramApiError`, `Update`, `CallbackQuery`, `Button`, `OutboundMessage`, `Notification` model (T1); `log_event`.
- Produces in `trader.adapters.telegram.api`: `PtbTelegramApi(token: SecretStr, *, bot: telegram.Bot | None = None, timeout: float = 20.0)` implementing `TelegramApi` (and `async __aenter__/__aexit__`).
- Produces in `trader.notify.notifier`: `TelegramNotifier(api: TelegramApi, chat_id: int, factory, clock, *, sleep=asyncio.sleep)` implementing `Notifier`; `NullNotifier()` (logs `telegram.not_configured` once per process, records nothing); `split_text(text: str, limit: int = 4096) -> list[str]`.

**Behaviour and decisions:**
- `PtbTelegramApi` translates PTB objects into the T1 dataclasses (only `message` text updates and `callback_query` updates; others are returned with both `text` and `callback` None). Buttons become an `InlineKeyboardMarkup`; `parse_mode` is HTML; `silent` sets `disable_notification`. PTB errors become `TelegramApiError`: `RetryAfter` → status 429 with `retry_after`; `BadRequest` → 400; `Forbidden` → 403; `Conflict` → 409; `TimedOut`/`NetworkError` → status None; the description is PTB's message text (Telegram's `description`). The token never appears in an error or log. `get_updates` asks only for `message` and `callback_query` updates.
- `TelegramNotifier.send(msg)`: if `dedupe_key` is set, insert a `notifications` row with status `sending` (`ON CONFLICT DO NOTHING`); when the key already exists, do nothing (a crash between insert and send means the message may be lost, never doubled: at most once, by design). Without a key the row is inserted plainly. Send each part of `split_text(msg.text)` (buttons on the last part); on 429 wait `retry_after` (capped at 30 s) and retry once; on a network error (status None) wait 2 s and retry once; any other error, or a second failure, marks the row `failed` with `error = "<status> <description>"`, logs a structlog warning and a `warning` event (source `telegram`), and returns. On success: status `sent`, `sent_at`, `message_ids`. It never raises, whatever happens (including DB errors, which are logged).
- Sends are spaced at least 1 s apart inside one notifier (Telegram's per-chat limit), using the injected sleep.

**Acceptance tests (respx for the API client, `FakeTelegramApi` for the notifier):**
- [ ] 1. `send_message` posts to the Telegram `sendMessage` endpoint with chat id, HTML parse mode and the inline keyboard, and returns the message id from the response.
- [ ] 2. `get_updates` turns a callback-query update and a text update into `Update` values with the right chat ids, data and message id.
- [ ] 3. A 400 response `{"ok": false, "description": "Bad Request: message is not modified"}` raises `TelegramApiError(400, ...)` with that description; a 429 with `retry_after: 3` gives `retry_after == 3`; a 409 gives 409.
- [ ] 4. A transport error is raised as `TelegramApiError(None, ...)`, and neither its message nor any captured log line contains the token string.
- [ ] 5. The notifier sends one message and records a `sent` row with the message id.
- [ ] 6. The same `dedupe_key` sent twice (and from two notifier instances) reaches the API once.
- [ ] 7. A 400 from the API → no exception, a `failed` row with `400 Bad Request: ...`, one `warning` event; a 429 then success → sent once after the (fake) wait.
- [ ] 8. The API raising on every call, and the DB factory raising, both leave `send` returning normally.
- [ ] 9. A 9,000-character message is sent as three parts, with the buttons only on the last.
- [ ] 10. `NullNotifier.send` returns without error and logs once.
- [ ] 11. Gate and commit `P3-T5: ...`.

---

### Task P3-T6: Telegram bot: update loop, signed callbacks, approvals, journal answers

**Goal:** The worker's Telegram side: long-poll updates, accept only the configured chat, verify signed single-use callbacks, turn Approve/Reject taps into `ProposalService.decide(..., via="telegram")`, route commands and the pause confirmation, record the daily "Rules followed?" answer, and send (and later close) proposal messages. BR-31, BR-34, BR-60; SPEC §4.4, §14.

**Interfaces:**
- Consumes: `TelegramApi`, `Update`, `CallbackQuery`, `CommandHandler`, `CallbackIssuer`, `ProposalMessenger`, `Renderer`, views (T1); `TelegramCallback`, `Journal`, `Proposal`, `Symbol`, `Signal`, `StrategyConfig` models; `ProposalService.decide` (P2-T11); `log_event`.
- Produces in `trader.adapters.telegram.callbacks`: `CallbackSigner(secret: bytes)` with `derive(session_secret: str) -> CallbackSigner` (key = HMAC-SHA256(session secret, `"trader.telegram.callback.v1"`)), `data(kind_code, ref, action, nonce) -> str`, `parse(data) -> ParsedCallback | None` (None when malformed or the MAC is wrong, compared with `hmac.compare_digest`); `ParsedCallback(kind: CallbackKind, ref: str, action: str, nonce: str)`; `DbCallbackIssuer(factory, clock, signer)` implementing `CallbackIssuer`, plus `claim(parsed, chat_id, message_id) -> ClaimResult` and `release(nonce) -> None`; `ClaimResult = Literal["ok", "unknown", "used", "expired", "wrong_message"]`.
- Produces in `trader.adapters.telegram.bot`: `TelegramBot(api, chat_id: int, factory, clock, issuer: DbCallbackIssuer, signer: CallbackSigner, decide: Callable[[int, Decision, Via, str], DecisionResult], commands: CommandHandler, render: Renderer, run_id: int, *, settings: Callable[[], RuntimeSettings], sleep=asyncio.sleep)` implementing `ProposalMessenger`, with `async run(stop: asyncio.Event) -> None` and `async handle_update(update: Update) -> None`.

**Behaviour and decisions:**
- **Callback data** (≤ 64 bytes, Telegram's limit): `<k>:<ref>:<a>:<nonce>:<mac>`; `k` is `p` (proposal, ref = proposal id, actions `a`/`r`), `s` (pause, ref = run id, actions `y`/`n`), `j` (journal, ref = `YYYYMMDD`, actions `y`/`n`); nonce 8 URL-safe random characters; mac = the first 16 characters of the base64url HMAC-SHA256 of `v1|<k>:<ref>:<a>:<nonce>`. One nonce per message, shared by its buttons, so the first tap on either button uses it up.
- **Update filter:** an update whose `chat_id` (or callback `chat_id`) is not the configured chat is ignored and logged (a `warning` event, source `telegram`, with the chat id and update kind, never the text); no reply is sent to it.
- **Callback handling order** (S6): parse and verify the MAC (bad → answer `Invalid button`, warning event); `claim` the nonce in its own transaction (`UPDATE … SET used_at WHERE used_at IS NULL AND expires_at …`); a used nonce → answer `Already answered`; expired → `Button expired`; a message id that doesn't match the bound one → `Old message`. Then act: for `p`, call `decide(proposal_id, "approve" | "reject", "telegram", f"telegram:{from_id}")` (exactly `ProposalService.decide`'s signature; `Decision` and `Via` come from `trader.engine.proposals`; a DB transaction, P2). A `KeyError` (no such proposal in the live run) keeps the nonce used and answers `Unknown proposal`; any other exception `release`s the nonce and answers `Error, try again`. Then **answer the callback first** (`Approved` / `Rejected` / `Approved, but the order failed` for status `failed` / `Already <status>` when `already_decided`), and only then edit the message to `render.proposal_closed(...)` without buttons. A failed answer or edit is logged with Telegram's description and never changes the decision.
- **Proposal messages:** `send_proposal(id, resend=False)` loads the proposal, its symbol and sizing into a `ProposalView`; skips (returns False) unless it is `pending`, or when a `proposal` callback row already exists for it and `resend` is False; otherwise issues a nonce **with no TTL** (`ttl_seconds=None`: the proposal's own expiry is enforced by `ProposalService.decide`, which expires it and returns `already_decided` with status `expired`, so a late tap gets `Already expired` and the message is closed, not `Button expired`), sends `render.proposal(...)`, and binds the message id. `sync_closed()` edits every bound, unused proposal message whose proposal is no longer pending to its final text (buttons removed) and marks the nonce `used_action = "closed"`; it returns the number edited.
- **Commands:** a text update starting with `/` goes to `commands.handle(text)` and each returned message is sent. A pause confirmation callback (`s`) calls `commands.confirm_pause("y"|"n")` and answers with its text.
- **Journal:** a `j` callback upserts `journal` for (live run, date): `rules_followed`, `answered_via = "telegram"`, `updated_at`; answers `Saved`; removes the summary's buttons with `edit_buttons(chat, message_id, ())` (the summary text stays); sends `render.journal_answered(...)`.
- **Message-id check:** `claim` compares the callback's message id with the nonce's bound `message_id` only when one is bound (the journal nonce is unbound, T11).
- **Polling loop:** `get_updates(offset, timeout=telegram.poll_timeout_seconds)`, offset = last update id + 1; each update is handled in its own try block (one bad update never stops the loop). A 409 Conflict (another poller for this bot) logs one `critical` event and backs off 30 s; other errors back off 1, 2, 4 … 60 s. `stop` ends the loop after the current call.

**Acceptance tests (FakeTelegramApi, a real DB, a fake `decide` that records calls unless stated):**
- [ ] 1. A valid Approve callback from the configured chat calls `decide(id, "approve", "telegram", "telegram:<from_id>")` once; the recorded call order is `answer_callback` then `edit_message`.
- [ ] 2. The same callback delivered twice (a replay) calls `decide` once; the second is answered `Already answered`.
- [ ] 3. A callback with one character of the MAC changed, or a proposal id changed (MAC no longer matches), is refused without calling `decide`.
- [ ] 4. A valid callback from another chat id is ignored: no `decide`, no answer, one warning event without the text.
- [ ] 5. Tapping Reject after Approve on the same message is refused (nonce used); with the real `ProposalService`, a web `decide` first and then a tap gives `Already submitted` and one order (Review Focus 2).
- [ ] 6. A tap after the proposal expired (real `ProposalService`, fake clock past `expires_at`) answers `Already expired` and edits the message to `Expired` (the proposal nonce has `expires_at` NULL, so the claim itself succeeds).
- [ ] 7. `answer_callback` failing with a 400 still leaves the decision made and the message edited; `edit_message` failing is logged and the loop continues.
- [ ] 8. `decide` raising a `RuntimeError` releases the nonce, so a second tap works; `decide` raising `KeyError` answers `Unknown proposal` and a second tap gets `Already answered`.
- [ ] 9. An expired nonce (pause confirmation after 60 s) answers `Button expired`.
- [ ] 10. A callback whose message id differs from the bound one answers `Old message`.
- [ ] 11. `send_proposal` sends once per pending proposal and returns False on the second call; with `resend=True` it sends a new message with a new nonce; a non-pending proposal is not sent. `sync_closed` edits a message whose proposal was decided on the web.
- [ ] 12. A journal `y` callback writes `rules_followed = true`, `answered_via = "telegram"` for the live run and date.
- [ ] 13. The run loop: a 409 logs one critical event and sleeps 30 s (fake sleep); a text `/status` update goes to the command handler and its reply is sent.
- [ ] 14. Every generated callback data string is ≤ 64 bytes for a proposal id of 10 digits.
- [ ] 15. Gate and commit `P3-T6: ...`.

---

### Task P3-T7: Telegram commands

**Goal:** The remote commands of SPEC §4.4 for use away from home: `/status`, `/positions`, `/pnl`, `/pending`, `/pause` (with confirmation), `/resume`, `/help`. BR-34, BR-30 (mode shown), BR-33 (unprotected time shown).

**Interfaces:**
- Consumes: `CommandHandler`, `CallbackIssuer`, `ProposalMessenger`, `Renderer`, views, `session_phase`, `current_session` (T1); `DayPlan` and the `fired_keys` signature (T3 contract, injected as callables); `KillSwitches.active/pause/resume` (P2-T10); `TokenHealth` (P1-T6); `QtQuote`; models `Position`, `Order`, `Trade`, `Proposal`, `EquitySnapshot`, `WorkerHeartbeat`, `Symbol`.
- Produces in `trader.adapters.telegram.commands`: `CommandDeps(factory, clock, calendar, settings: Callable[[], RuntimeSettings], killswitches: KillSwitches, run_id: int, chat_id: int, plan: Callable[[date], DayPlan], fired: Callable[[date], set[str]], token_health: Callable[[], TokenHealth], quotes: Callable[[Sequence[int]], Awaitable[Mapping[int, QtQuote]]] | None, messenger: ProposalMessenger, issuer: CallbackIssuer, render: Renderer)`; `Commands(deps)` implementing `CommandHandler`; builders (tested directly): `status_view(deps) -> StatusView`, `position_lines(deps) -> tuple[PositionLine, ...]`, `pnl_view(deps) -> PnlView`.

**Behaviour and decisions:**
- Parsing: the first word, lower-cased, with any `@BotName` suffix removed; unknown commands and plain text get `render.reply("Unknown command. Try /help.")`.
- `/status`: phase and session date, the next unfired planned event (key and time) or "none today", approval mode, blocking kill switches (from `KillSwitches.active(run_id, current_session(calendar, now))`), token health (OK, or the last error; age since the last refresh in hours; an error if older than 26 h), worker heartbeat age (from `worker_heartbeats`), open positions and the pending proposal count.
- `/positions`: open positions of the live run: ticker, qty, entry (`avg_price`), last price from `quotes` (None → shown as `n/a`, and a quote failure never fails the command), stop (the working stop order's `stop_price`, else the position's `stop_loss`, marked `(no stop order)`), unrealized P&L, unprotected time (`unprotected_seconds` plus `now − unprotected_since` while unprotected).
- `/pnl`: today's realized P&L (trades with today's `session_date`), unrealized (as `/positions`), week to date (trades since the Monday of the current ET week), equity and drawdown from the latest `equity_snapshots` row (or the sim account's starting cash when there is none).
- `/pending`: `messenger.send_proposal(id, resend=True)` for each pending proposal of the live run, oldest first; replies `No pending proposals.` when there are none, else nothing extra.
- `/pause`: if `manual_pause` is already active, reply `Already paused.`; else issue a `pause` nonce (TTL `telegram.confirm_ttl_seconds`, actions `y`/`n`) and send `render.pause_confirm(buttons)` ("Pause new entries? Exits and stops keep working."). `confirm_pause("y")` calls `KillSwitches.pause(run_id, current_session(calendar, now), actor="telegram")` (audited by P2) and returns `Paused: new entries are blocked.`; `"n"` returns `Cancelled.`.
- `/resume`: `KillSwitches.resume(run_id, actor="telegram")`; True → `Manual pause lifted.` plus, when automatic switches are still tripped, `Still blocked by: <switches>. Reset them in the web app.`; False → `Not paused.`. It never resets an automatic switch (BR-34).
- `/help`: `render.help()`.
- Read-only commands open read-only DB work only; only `/pause` confirmation and `/resume` change state.

**Acceptance tests (real DB with seeded rows; FakeRenderer, FakeIssuer, FakeMessenger):**
- [ ] 1. `status_view` on a session at 10:00 ET after `orb_open` fired shows phase `open`, next event `entry_cancel` at 15:30Z, approval mode `manual`, no switches, token OK, heartbeat age 5 s, one position, one pending proposal.
- [ ] 2. On Saturday `status_view` shows `closed_day`, the next session's date and no next event today.
- [ ] 3. A token whose last refresh is 30 h old, or with `last_error`, is shown as not OK with the reason.
- [ ] 4. `position_lines` shows the stop order's price; with no working stop it shows `stop_loss` flagged `(no stop order)` and an unprotected time growing with the clock; a quote callable that raises gives `last = None` without failing.
- [ ] 5. `pnl_view` sums today's trades, the week's trades from Monday (a trade last Friday excluded), and drawdown from the latest snapshot.
- [ ] 6. `/pending` re-sends each pending proposal with `resend=True`, oldest first; none → `No pending proposals.`.
- [ ] 7. `/pause` then `confirm_pause("y")` blocks entries (`KillSwitches.blocking` returns `manual_pause`) and writes an audit row with actor `telegram`; `confirm_pause("n")` changes nothing; `/pause` when already paused replies `Already paused.`.
- [ ] 8. `/resume` lifts `manual_pause` only: with `max_drawdown_pct` also tripped it stays blocking and the reply names it; `/resume` when not paused replies `Not paused.`.
- [ ] 9. `/status@StephenTraderDevBot` works; `/nonsense` and plain text get the unknown-command reply; `/help` calls `render.help()`.
- [ ] 10. Gate and commit `P3-T7: ...`.

---

### Task P3-T8: Notification relay

**Goal:** Deliver what the engine and the cron processes record to Telegram: new pending proposals (with buttons), fills, stop-outs and flattens, the overlay decision, kill-switch trips, escalations, job and token failures. Exactly once per row, surviving restarts. BR-32, SPEC §4.4 message list, §6.2 escalations.

**Interfaces:**
- Consumes: `Notifier`, `Renderer`, `ProposalMessenger`, views (T1); `NotifyCursor`, `Fill`, `Order`, `Position`, `Trade`, `Symbol`, `Proposal`, `EventLog` models.
- Produces in `trader.notify.relay`: `STREAMS = ("proposals", "fills", "events")`; `NotificationRelay(factory, clock, notifier: Notifier, render: Renderer, messenger: ProposalMessenger, run_id: int, *, settings: Callable[[], RuntimeSettings])` with `async pump() -> RelayReport` and `RelayReport(proposals: int, fills: int, events: int, closed: int, skipped: int)`; `alert_kind(source: str, level: str, message: str) -> MessageKind` (pure).

**Behaviour and decisions:**
- `pump()` runs five steps, each in its own try block (a failure in one is logged and the others still run):
  1. **Proposals:** `messenger.send_proposal(id)` for every `pending` proposal of the run, oldest first (the messenger skips ones already sent).
  1b. **Auto-mode proposals** (BR-32 "new proposals" also in auto mode, BR-30): proposals of the run with `id > cursor("proposals")` and `decided_by = "auto"` (auto mode; not `auto_flatten_on_expiry`, whose result shows up as a fill) → `render.proposal(ProposalView, ())` (the `AUTO` form, no buttons), `silent=True`, `dedupe_key = "proposal:<id>"`; the cursor advances past every proposal it reads, pending ones included (those are the messenger's).
  2. **Fills:** fills of the run with `id > cursor("fills")`, in id order → `render.fill(FillView)` (the view joins order purpose and reason, symbol ticker, the position's stop loss, and the trade's `pnl`/`pnl_r` when the fill closed it), sent with `dedupe_key = "fill:<id>"`; the cursor advances after each send.
  3. **Events:** `event_log` rows with `id > cursor("events")` and either level `error`/`critical`, or source `strategy.spy_overlay` with `data.decision` present (the overlay decision note) → `render.alert(AlertView)` or `render.overlay(OverlayView)`, `dedupe_key = "event:<id>"`. A `strategy.spy_overlay` row with `data.decision` always renders as an overlay message, whatever its level (the `error`/`warning` "holding" notes carry no prices, so the view's prices are None). Rows from source `telegram` are never relayed (no loops).
  4. **Closed proposals:** `messenger.sync_closed()`.
- `alert_kind`: `killswitch` → `kill_switch`; `job.*` → `job_failure`; `questrade.token` → `token_failure`; `proposals` → `escalation`; `engine` with level `critical` → `escalation`; everything else → `alert`.
- **Cursors:** a missing cursor starts at the current maximum id (no flood of history on first start). After a restart the relay catches up from its cursor, but sends at most `telegram.relay_catchup_max` rows per stream in one pump; the rest are skipped, the cursor moves past them, and one `alert` summarises "N older alerts not sent; see the System page".
- The cursor update and the notification are ordered send-then-advance; together with the notifier's `dedupe_key` this gives no duplicates after a crash between them.
- The relay never waits on Telegram beyond `Notifier.send` (which never raises).

**Acceptance tests (real DB, RecordingNotifier, FakeRenderer, FakeMessenger):**
- [ ] 1. A new pending proposal is passed to the messenger; an auto-mode one (`decided_by = "auto"`, status `submitted`) is not, and instead produces one silent message without buttons; an expired flatten auto-submitted by `auto_flatten_on_expiry` produces no proposal message.
- [ ] 2. An entry fill, a stop fill and a flatten fill each produce one message with the right view (purpose, reason, P&L on the closing fill).
- [ ] 3. A kill-switch `error` event → `kill_switch`; a `job.nightly` error → `job_failure`; a `proposals` error (unprotected position) → `escalation`; an `info` event → nothing; a `telegram` warning → nothing.
- [ ] 4. The spy_overlay decision note (info level, `data.decision = "hold"`) → one overlay message; the `error`-level "benchmark unknown, holding" note → one overlay message (not an alert as well) with no prices.
- [ ] 5. With no cursor rows, existing fills and events are not sent; only rows added afterwards are.
- [ ] 6. Running `pump()` twice, and running it from two relay instances, sends each row once.
- [ ] 7. A notifier that fails for one message still lets the cursor and the other streams advance (the failure is the notifier's to log).
- [ ] 8. After 50 events accumulate with `relay_catchup_max = 20`, one pump sends 20 plus one summary, and the next pump sends nothing old.
- [ ] 9. A messenger that raises in `send_proposal` does not stop fills and events from being relayed in the same pump.
- [ ] 10. Gate and commit `P3-T8: ...`.

---

### Task P3-T9: Worker process

**Goal:** The long-running `python -m trader.worker` loop: fire session events on time, poll quotes for working orders, expire proposals and escalate, relay notifications, run the Telegram bot, end the session, write a heartbeat, survive restarts, and refuse to run twice. SPEC §1 (worker), §6 pipeline, §7.2 polling, §9; BR-31, BR-42.

**Interfaces:**
- Consumes (all through protocols, fakes in the tests): `WorkerEngine` below (satisfied by the P2-T13 `Engine`); `DayPlan`, `due_events`, `fired_keys`, `fire_event`, `FireDeps`, `FireResult` (T3 contracts, injected); `NotificationRelay.pump` (T8, as a callable); `TelegramBot.run` (T6, as a callable); `session_phase`, `current_session`, `WorkerHeartbeat` (T1); `run_job_async` (T3 contract, injected).
- Produces in `trader.worker`:
  - `WorkerEngine` protocol: `async run_event(event_key: str, session_date: date) -> Any`, `async poll_quotes() -> Sequence[Any]`, `async tick(now: datetime) -> None`, `async end_of_session(session_date: date) -> Sequence[Any]` (names and arity exactly as P2's `Engine`, which returns `EventResult`, `list[FillEvent]` and `list[PositionView]`; `Sequence` keeps the protocol satisfied without importing those types).
  - `WorkerDeps(factory, clock, calendar, settings: Callable[[], RuntimeSettings], engine_for: Callable[[date], Awaitable[WorkerEngine]], plan: Callable[[date], DayPlan], fire: Callable[[str, date], Awaitable[FireResult]], fired: Callable[[date], set[str]], relay: Callable[[], Awaitable[Any]] | None, bot: Callable[[asyncio.Event], Awaitable[None]] | None, end_session: Callable[[date, Callable[[], Awaitable[dict[str, Any]]]], Awaitable[Any]], sleep=asyncio.sleep, process: str = "worker", host: str = "")`.
  - `StepReport(now, phase: SessionPhase, fired: list[FireResult], fills: int, relayed: bool, ended: bool)`.
  - `Worker(deps)`: `async step() -> StepReport` (one iteration), `async run(stop: asyncio.Event) -> None`, `acquire_single_instance(engine: sqlalchemy.Engine) -> Connection | None` (module function).
  - `main(argv: Sequence[str] | None = None) -> int` for `python -m trader.worker [--once]`, which calls `trader.runtime.run_worker(once=...)` (T12) and returns its exit code; `if __name__ == "__main__": raise SystemExit(main())`.

**Behaviour and decisions:**
- **Single instance:** `run` first takes a PostgreSQL session advisory lock (key from `"trader.worker"`) on a dedicated connection held for the process lifetime; if it isn't granted, log critical "another worker is running" and exit with code 2 before doing anything else (two workers would fight over the bot, 409, and double-poll).
- **Each step** reads `now` and the phase:
  - `open` (or `pre_market` within 60 s of the open): make sure today's engine and plan exist (`engine_for(session)` once per session; the engine is rebuilt each session so settings changes apply, per P2-T13); fire every due event through `fire` (which handles idempotency and lateness) in time order; `engine.poll_quotes()`; `engine.tick(now)`; then `relay()`. Sleep `quote_poll_seconds`.
  - `after_close` on a session day, once: `end_session(session, lambda: engine.end_of_session(session))` (T12 passes `run_job_async` with job `session_end`, so a restart doesn't repeat it), still fire due safety events first (fire decides), then `relay()`. Then idle.
  - `closed_day`, `pre_market` (earlier), idle after close: `relay()` only; sleep `worker.idle_poll_seconds`.
- Each part of a step is in its own try block: an exception is logged (structlog plus one `error` event, source `worker`) and the step continues; the loop never dies on an exception from the engine, the relay or the database. Ten consecutive failed steps log one `critical` event.
- **Heartbeat:** upsert `worker_heartbeats` (`process`, pid, host, `started_at` at start, `beat_at`, session date, phase, detail `{"fills_today", "last_event"}`) at start (`starting`), at least every `worker.heartbeat_seconds`, and on stop (`stopped`, which a `--once` run also writes on exit).
- **Bot:** started as a separate asyncio task with the same `stop` event, so long polling never delays quote polling; if the bot task dies it is logged and restarted after 30 s.
- **Restart mid-session:** nothing is held in memory that the database doesn't have. On start at 10:00, `fired(session)` already contains `orb_open` (from the database), working orders are polled again, pending proposals keep expiring, and the relay resumes from its cursors.
- **Shutdown:** SIGTERM and SIGINT set `stop`; the current step finishes, the bot task is awaited (up to one poll timeout), the heartbeat is marked `stopped`, the lock connection closed.
- `--once` runs a single `step()` (used by the LIVE smoke check) and exits 0. It never starts the bot task, so it never calls `getUpdates` (no update is consumed and no 409 is caused for a worker that is polling).

**Acceptance tests (FixedClock, fake sleep that advances the clock, fake engine and callables, real DB for heartbeat and lock):**
- [ ] 1. Stepping from 09:35:00 to 09:35:10 ET in 2 s steps fires `orb_open` exactly once, at the first step at or after 09:35:05.
- [ ] 2. During the session each step calls `poll_quotes` and `tick` once and the relay once, then sleeps `quote_poll_seconds`; on Saturday it only relays and sleeps `idle_poll_seconds`.
- [ ] 3. The engine is built once per session and rebuilt for the next session.
- [ ] 4. After the close, `end_session` is called once for the day, even across many steps.
- [ ] 5. An exception from `poll_quotes`, from `relay` and from `fire` each leaves the step completing its other parts, with one `error` event each.
- [ ] 6. The heartbeat row is written at start, updated at least every `worker.heartbeat_seconds` of fake time, and marked `stopped` after `stop` is set.
- [ ] 7. Restart: a worker started at 10:00 with `fired` returning `{"orb_open"}` does not fire it again, and polls and ticks normally (Review Focus 4).
- [ ] 8. A second `acquire_single_instance` while the first connection holds the lock returns None, and `run` exits with code 2 without polling.
- [ ] 9. A bot callable that raises is restarted after 30 s of fake time and the quote loop keeps its 2 s cadence meanwhile.
- [ ] 10. On an early-close day (2026-11-27) the session phase ends at 13:00 ET and `end_session` runs then.
- [ ] 11. `main(["--once"])` calls `run_worker(once=True)` (monkeypatched) and returns its code; a `Worker` run in once mode performs one step and never calls the `bot` callable.
- [ ] 12. Gate and commit `P3-T9: ...`.

---

### Task P3-T10: Day-level jobs: pre-open, check-in, event backup

**Goal:** The cron jobs around the session: the 09:20 pre-open check (token, data freshness, kill switches, worker heartbeat), the 11:30 and 13:30 check-ins (a status push plus a backup for due events), and `trader event <key>` (the cron backup for the worker's events). Each does nothing on a holiday. SPEC §9; master plan §7.3 T6.

**Interfaces:**
- Consumes: `Notifier`, `Renderer`, views, `Check`, `session_phase`, `WorkerHeartbeat` (T1); `fire_event`, `FireDeps`, `FireResult`, `DayPlan`, `due_events`, `fired_keys` (T3 contracts; tests use fakes or seeded `job_runs` rows); `status_view` inputs are rebuilt here from the DB, not imported from T7; `QuestradeAuth` via a `TokenCheck` callable; `KillSwitches.active`; `MarketDataService.universe_status` (P2-T7) via a callable; `JobRun`, `OpenBarStat` models.
- Produces:
  - `trader.jobs.preopen`: `PreopenDeps(factory, clock, calendar, settings: Callable[[], RuntimeSettings], token_check: Callable[[], Awaitable[None]], universe_status: Callable[[date], Awaitable[UniverseStatus]], killswitches: KillSwitches, run_id: int, notifier: Notifier, render: Renderer)`; `async run_preopen(deps, session_date) -> dict[str, Any]` (the job detail: every `Check` as a dict, and `ok: bool`).
  - `trader.jobs.checkin`: `CheckinDeps(factory, clock, calendar, settings, run_id, notifier, render, plan: Callable[[date], DayPlan], fired: Callable[[date], set[str]], fire: Callable[[str, date], Awaitable[FireResult]], quotes: Callable[[Sequence[int]], Awaitable[Mapping[int, QtQuote]]] | None)`; `async run_checkin(deps, session_date, at_label: str) -> dict[str, Any]`.
  - `trader.jobs.events`: `async run_event_backup(fire: Callable[[str, date], Awaitable[FireResult]], calendar, clock, key: str | None, session_date: date, *, due: bool = False, plan=None, fired=None, force: bool = False) -> list[FireResult]`.

**Behaviour and decisions:**
- **Holidays:** each function returns `{"skipped": "not a session"}` at once on a non-session day (the CLI also checks, T12).
- **Pre-open checks** (each a `Check`; the job itself succeeds even when checks fail, because a failed check is an alert, not a job failure):
  1. `token`: `token_check()` (T12 passes `QuestradeAuth.access` in a thread) succeeds → OK; raises → `error` with the message.
  2. `universe`: `universe_status(session)`; no universe → `error`; a fallback → `warning` with its age; stale → `error`.
  3. `open_bar_stats`: rows for the session > 0 → OK, else `error`.
  4. `premarket`: a succeeded `premarket` job run for the session → OK, else `warning`.
  5. `kill_switches`: none active → OK; `manual_pause` → `warning`; an automatic switch → `error` (entries blocked today).
  6. `worker`: heartbeat `beat_at` within `worker.heartbeat_stale_seconds` → OK; older or missing → `error` ("worker not running: approvals and fills will not happen").
  The message (`render.preopen`, dedupe `preopen:<date>`) is sent when any check is not OK, or always when `preopen.notify_when_ok` is true. It goes out directly from the cron process (not through the relay), so it arrives even when the worker is down.
- **Check-in** (`at_label` `"11:30"` or `"13:30"`): after the session close → `{"skipped": "after close"}` (early-close days at 13:30). Otherwise send `render.checkin(status, at_label)` (dedupe `checkin:<date>:<label>`), built from the same DB reads as `/status`; then backup-fire every `due_events(plan, now, fired)` through `fire` (SPEC §9 "entry-cancel event at 11:30"). Detail lists the fire results.
- **Event backup:** `key` given → `fire(key, session)` once; `due=True` → fire every due event. With the idempotent `fire`, a backup after the worker already fired is `skipped` (not an error). Exit status (T12): 0 for `fired`, `skipped`, `too_early`, `not_session`, `not_scheduled`; 1 for `failed` and `missed`.

**Acceptance tests:**
- [ ] 1. On Thanksgiving each of the three jobs returns the not-a-session result and sends nothing.
- [ ] 2. Pre-open with everything healthy sends one `preopen` message with six OK checks; with `notify_when_ok = false` it sends nothing and the detail has `ok: true`.
- [ ] 3. Pre-open with a token check that raises, a heartbeat 10 minutes old and `max_drawdown_pct` tripped reports three `error` checks with those details and sends the message.
- [ ] 4. Pre-open with a stale fallback universe reports `error`, a fresh fallback `warning`, and a missing pre-market run `warning`.
- [ ] 5. Check-in `13:30` on 2026-11-27 (13:00 close) is skipped; `11:30` on a normal day sends one message with the given label.
- [ ] 6. Running a check-in twice sends one message (the dedupe key).
- [ ] 7. Check-in at 11:30:00 ET with `entry_cancel` due and unfired calls `fire("entry_cancel")`; with it already fired it does not call `fire`.
- [ ] 8. `run_event_backup(key="orb_open")` passes the `fire` result through; when the fake `fire` says `skipped` (the worker fired first) that is returned unchanged.
- [ ] 9. `run_event_backup(due=True)` fires every due event and none that is not yet due.
- [ ] 10. Gate and commit `P3-T10: ...`.

---

### Task P3-T11: Post-close job and candle archive

**Goal:** The 16:15 ET job: end-of-day order cancels (the BR-42 safety net), the day's journal row, the daily summary with the Rules-followed button, and the **candle archive** (the 9:30–9:35 bar for every universe symbol, plus 1-minute regular-hours candles for the day's top 20 candidates and SPY). Idempotent. BR-60, BR-42, BR-33; SPEC §8 (candle archive), §9, §10 `candle_archive`, `journal`.

**Interfaces:**
- Consumes: `Notifier`, `Renderer`, `CallbackIssuer`, views (T1); `WorkerEngine.end_of_session` (T9 protocol; the P2 `Engine`); `MarketDataService.universe`, `candles` (P2-T7) through the `ArchiveData` protocol below; `regular_hours`, `opening_bar` (P1-T8); `Interval`, `Candle`; models `CandleArchive`, `IntradayCandle`, `Candidate`, `Journal`, `Trade`, `Proposal`, `Position`, `EquitySnapshot`, `KillSwitchEvent`, `Symbol`; `OVERLAY_SYMBOL`.
- Produces:
  - `trader.market.repository.upsert_candle_archive(session, symbol_id: int, interval_code: Literal["1m", "5m"], candles: Iterable[Candle]) -> int` (`ON CONFLICT (symbol_id, interval, start_ts) DO UPDATE`, like `upsert_intraday_candles`).
  - `trader.jobs.postclose`: `ArchiveData` protocol (`async universe(session_date)`, `async candles(symbol_id, start, end, interval) -> list[Candle]`, `async opening_bars(session_date, symbol_ids=None) -> OpeningBars`); `PostcloseDeps(factory, clock, calendar, settings: Callable[[], RuntimeSettings], engine: WorkerEngine, data: ArchiveData, notifier: Notifier, render: Renderer, issuer: CallbackIssuer, chat_id: int, run_id: int)`; `async run_postclose(deps, session_date) -> dict[str, Any]`; `async archive_candles(deps, session_date) -> dict[str, Any]`; `daily_summary_view(factory, run_id, session_date, now, archive) -> DailySummaryView`.

**Behaviour and decisions:**
- Order: (1) `engine.end_of_session(session)` (cancels working orders, snapshots equity, returns positions still open, each reported loudly; P2 logs the `critical` event); (2) insert the `journal` row for (run, session) if missing (`rules_followed` NULL; never overwrite an answer); (3) `archive_candles`; (4) build `daily_summary_view` and send `render.daily_summary(view, buttons)` with a `journal` nonce from the issuer (actions `y`/`n`, no TTL, ref `YYYYMMDD`), dedupe `summary:<date>`. The journal nonce is not bound to a message id (`Notifier.send` returns none); T6's claim skips the message-id check for an unbound nonce. Returns `{"open_positions", "cancelled", "archive", "summary_sent"}`.
- **Archive (a):** the opening 5-minute bar for every universe symbol of the session. Read it first from `intraday_candles` (5m, `ts = session_open`), which the 9:35 scan cached; fetch the missing ones with `data.opening_bars(session, missing_ids)` (one batched call, rate-limited by the client); write to `candle_archive` interval `5m`.
- **Archive (b):** 1-minute regular-hours candles (`session_open` to `session_close`, filtered with `regular_hours`) for the session's top `postclose.archive_top_n` candidates of the live run (the `candidates` rows ordered by `rank`, any strategy, distinct symbols) plus SPY; written with interval `1m`.
- A symbol whose bar or candles can't be fetched is listed in `archive.missing` with its reason; the job still succeeds. More than 5% of the universe missing in (a), or SPY missing in (b), also logs an `error` event (source `job.postclose`), which the relay alerts.
- Re-running for the same session (forced) writes no duplicate rows (primary-key upsert), keeps the journal answer, and sends no second summary (dedupe).
- `daily_summary_view`: the session's trades (`trades.session_date`), realized P&L and fees (`v_daily_pnl`), the latest equity snapshot (equity, drawdown), open positions, the number of decided proposals and their average `decision_latency_ms` (BR-33), total `unprotected_seconds` of positions opened that day (BR-33), blocking kill switches, archive counts.
- Non-session day → `{"skipped": "not a session"}`.

**Acceptance tests (real DB; fake engine, fake `ArchiveData`, RecordingNotifier, FakeRenderer, FakeIssuer):**
- [ ] 1. On a holiday nothing runs and nothing is sent.
- [ ] 2. With 3 universe symbols (two opening bars cached, one not) the archive has three `5m` rows at the session open and the fake data was asked for the one missing symbol only.
- [ ] 3. With candidates ranked 1–25 and `archive_top_n = 20`, 1-minute candles are archived for ranks 1–20 plus SPY only, and bars outside 09:30–16:00 ET (04:00 and 19:59 bars in the fake data) are not stored.
- [ ] 4. On 2026-11-27 (13:00 close) the 1-minute window ends at 13:00 ET.
- [ ] 5. A symbol whose candles raise is listed in `archive.missing`; with SPY missing an `error` event is written; the job result is still returned.
- [ ] 6. A forced re-run leaves the archive row count unchanged, keeps a journal answer already given, and sends one summary in total.
- [ ] 7. `end_of_session` is called once with the session date, and an open position it returns appears in the summary view as open.
- [ ] 8. `daily_summary_view` for a seeded day (one trade +10.8157, `decision_latency_ms` 1200 and 3000, 95 unprotected seconds) gives realized 10.8157, average decision 2.1 s, unprotected 95 s.
- [ ] 9. The summary message carries the journal buttons from the issuer (kind `journal`, ref `YYYYMMDD`, actions `y`/`n`).
- [ ] 10. `upsert_candle_archive` inserts, then updates on conflict, and returns the row count.
- [ ] 11. Gate and commit `P3-T11: ...`.

---

### Task P3-T12: Wiring: runtime composition, CLI commands, crontab, LIVE checks

**Goal:** Put the real pieces together: the composition root that builds the notifier, the bot, the relay, the engine and the worker from `Core`; the CLI commands the crontab calls; the crontab itself; failure alerts from the cron side; and the §7.1 contract rows. Then check the whole thing against the dev bot and `trader_dev`. SPEC §1, §9, §13; master plan §7.1.

**Interfaces:**
- Consumes: every T1–T11 production interface, `build_core`, `build_engine` (P2-T13), `QuestradeAuth`, `QuestradeClient`, `FinvizScraper`, `CatalystService`/`CatalystStore`/`CatalystClassifier` (P2-T12), `run_premarket` (P2-T14), `run_job`, `run_job_async`, `configure_logging`.
- Produces in `trader.runtime`: `build_notifier(core) -> Notifier` (`TelegramNotifier` when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, else `NullNotifier`); `build_renderer(core) -> MessageRenderer` (`PUBLIC_BASE_URL`, `TZ_DISPLAY`); `build_signer(core) -> CallbackSigner` (from `SESSION_SECRET`); `build_decider(core, run_id: int) -> Callable[[int, Decision, Via, str], DecisionResult]` (see Behaviour); `async open_engine(core, stack: AsyncExitStack) -> Engine` (Questrade client and catalyst service entered on the stack; one per session); `fire_deps(core, runner_factory) -> FireDeps`; `async run_worker(once: bool = False) -> int` (builds everything, acquires the single-instance lock, runs `Worker`); `async run_cli_job(core, job, session_date, body, *, force) -> JobOutcome` (wraps `run_job_async`).
- Produces in `trader.cli` (new commands; existing ones keep working): `preopen [--date] [--force]`; `checkin --at HH:MM [--date] [--force]` (job name `checkin@HH:MM`); `event KEY [--date] [--force]` and `event --due`; `postclose [--date] [--force]`; `telegram-test [--buttons]` (sends a clearly labelled test message through the configured bot; with `--buttons` it adds two buttons whose unsigned data is `test`, which a running bot answers `Invalid button`, as it should). Changed: `premarket` sends the brief through the notifier after a success (dedupe `premarket:<date>`); `token-refresh` logs an `error` event with source `questrade.token` on failure (the relay alerts; the pre-open check alerts again at 09:20 if the worker is down). Every command calls `configure_logging("cron")` first; job commands exit 1 on `failed`/`missed` and print one line, never a traceback.
- Produces `docker/crontab` (runs under supercronic in P4-T10):
  ```
  CRON_TZ=America/New_York
  0 2 * * *      trader token-refresh
  0 20 * * 0-4   trader nightly
  0 8 * * 1-5    trader premarket
  20 9 * * 1-5   trader preopen
  36 9 * * 1-5   trader event orb_open
  30 11 * * 1-5  trader checkin --at 11:30
  55 12 * * 1-5  trader event flatten
  30 13 * * 1-5  trader checkin --at 13:30
  55 15 * * 1-5  trader event flatten
  15 16 * * 1-5  trader postclose
  ```
- Updates the master plan §7.1 rows "Notifier" and "Event firing" to the refined shapes (contract refinements 1 and 2 above) and adds a row "Telegram callbacks" (`CallbackSigner`/`telegram_callbacks`, used by P4's web app only to read).

**Behaviour and decisions:**
- The worker's `engine_for(session)` closes the previous session's stack (Questrade client) before opening the next.
- **The bot's `decide`** is `ProposalService(core.factory, core.clock, core.settings, broker, run.id).decide`, over its own `SimBroker` for the live run built exactly as in `build_engine` (no Questrade client: submitting and cancelling only touch the database, and `SimBroker` keeps no state in memory). So a tap works whether or not a session engine is open, and the row lock in `decide` keeps it consistent with the engine's own `ProposalService`.
- **Bot ↔ commands cycle:** `Commands` needs the bot as its `ProposalMessenger` and the bot needs `Commands`; `run_worker` builds the bot first with a small forwarding `CommandHandler` whose target is set once `Commands` exists (no change to either class's contract).
- Cron-side job commands build the engine lazily (only when a body actually runs), so a skipped backup costs no Questrade call.
- `telegram-test` is the only way agents exercise the real bot; it never long-polls (the worker may be polling), it only sends.
- The worker entry point stays `python -m trader.worker` (SPEC §1); supervisord is P4-T10.

**Acceptance tests:**
- [ ] 1. `build_notifier` returns `NullNotifier` without Telegram env vars and `TelegramNotifier` with them (fake env, no network).
- [ ] 2. `run_worker(once=True)` with a test `Core` (testcontainers DB, `FakeQuestrade`, `FakeTelegramApi` injected through monkeypatched builders) completes one step, writes a heartbeat and returns 0.
- [ ] 3. CLI smoke tests (Typer `CliRunner`, builders monkeypatched to fakes): `preopen`, `checkin --at 11:30`, `event orb_open`, `event --due`, `postclose` each run on a session date and print their result; on a holiday they print "not a trading session" and exit 0; `event` with a `missed` result exits 1 with one line.
- [ ] 4. `premarket` success sends the brief once, a second run sends nothing; `token-refresh` failure writes the `questrade.token` error event.
- [ ] 5. `tests/test_crontab.py`: the file's first non-comment line is `CRON_TZ=America/New_York`; every other line has five valid cron fields and a command; the set of (schedule, command) pairs equals the table above (which matches SPEC §9 plus the documented 12:55 line); every command's first word after `trader` is a registered Typer command (checked against `cli.app`).
- [ ] 6. The crontab's times, read in ET, are the SPEC §9 times on a date in EDT (2026-10-06) and in EST (2026-12-01): 09:36 stays 09:36 ET on both (a check that the file has no UTC conversion baked in).
- [ ] 7. The master plan §7.1 rows are updated as described.
- [ ] 8. Gate and commit `P3-T12: ...`.

**LIVE steps** (dev bot @StephenTraderDevBot and `trader_dev`; token and chat id come from `.env.dev`, never printed). Safety rules: only the dev bot's token (the one in `Trader/docker/.env.dev`) is ever used; no prod token or prod database. Nothing in Phase 3 can place a real order: Questrade is used read-only (quotes, candles, token refresh; SPEC §4.1), every order is a `SimBroker` row, and agents never call the QuestTrade MCP order tools. Never print the token, the chat id or a Telegram URL.
1. `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev trader telegram-test` → prints `sent message <id>`; the message appears in Stephen's chat with the dev bot. Record the message id.
2. `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev trader telegram-test --buttons` → `sent message <id>` with two buttons (nobody is polling, so a tap is not answered until a worker runs; that is expected).
3. Only when the session is not open and not about to open (a weekend or holiday, or a session day before 09:00 or after 16:30 ET; otherwise skip and say so, because in-session the step would fire real strategy events into `trader_dev` and send proposals to Stephen): `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev python -m trader.worker --once` → exit 0 (the relay's cursors start at the current maximum ids, so no old rows are sent); then `SELECT process, phase, beat_at FROM trader.worker_heartbeats` through the dev database shows `worker` with a fresh `beat_at` and phase `stopped` (a once-run marks the heartbeat stopped on exit, as any shutdown does). Run the query with a short Python one-liner through `uv ... run python -c` using `trader.bootstrap.build_core()`, printing only the three columns. A `permission denied` error means the app role can't write the new tables: escalate (§5.4) with the error text.
4. On a trading day before 09:20 ET only (skip otherwise and say so): `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev trader preopen` → a pre-open message arrives; the `worker` check is `error` (no worker is running on the dev host yet), which is expected until P4-T11 deploys it.
Record each result in the activity log. Do not run a long-polling worker against the dev bot for more than one `--once` step (the orchestrator does not poll, but a later deployed worker will).

---

### Task P3-T13: Integration: a worker day with a fake Telegram

**Goal:** Prove the Phase 3 pieces work together over a full manual-approval day with a fake clock: proposal message, tap Approve, fill, protective stop approval, overlay decision, flatten (auto-submitted on expiry), post-close summary and the Rules-followed answer, all in order and on time. Plus a restart in the middle. No production code (a failure is reported and fixed in the owning task). SPEC §4.4, §6.2, §9; BR-30–34, BR-42, BR-60.

**Interfaces:**
- Consumes: `trader.runtime` builders with `FakeQuestrade` (P2-T7), fake FinViz and fake Claude (as in P2-T15), `FakeTelegramApi` (T1), a real testcontainers database, `FixedClock` and a fake sleep that advances it. Reuse P2-T15's setup helpers by importing them if they are module-level functions in `tests/integration/test_simulated_day.py`; otherwise build the same data locally. Do not modify P2 files.
- Produces: `tests/integration/test_worker_day.py` with the tests below.

**Behaviour (the day: Tue 2026-10-06, `approval_mode = manual`, `auto_flatten_on_expiry = true`, same market data as P2-T15):** the test runs `run_nightly` and `run_premarket` for the session, then drives the `Worker` with `step()` from 09:30 to 16:05 ET in 2 s steps (fewer steps where nothing happens is fine, but every scheduled event time must be crossed by a step), then `run_postclose`. The fake Telegram user taps by queueing a callback update built from the buttons of the last `sendMessage` call.

**Acceptance tests:**
- [ ] 1. `test_manual_day_through_telegram`: in this order, the fake API receives (a) at 09:35:05 an entry proposal message for AAA with Approve/Reject; the test taps Approve at 09:35:30 → the callback is answered, then the message edited to "Approved via telegram"; (b) a `fill` message after the 09:36 quote; (c) a protective-stop proposal, tapped Approve → stop order working; (d) at 15:30 an overlay `hold` message; (e) at 15:50 an exit proposal that is not tapped, expires at 15:55, auto-submits (P2), and a `flatten` fill message follows; (f) after `run_postclose`, a daily summary with the Yes/No buttons, tapped Yes → the `journal` row has `rules_followed = true`, `answered_via = "telegram"`. At the end no position is open (BR-42), `proposals.decided_via` is `telegram` for (a) and (c), and every message was sent once.
- [ ] 2. `test_worker_restart_mid_session`: the same day, but the worker object is discarded at 10:00 and a new one built from the same database: no second `orb_open` job run, no second proposal message, the protective stop remains working, and the day still ends flat with one summary (Review Focus 4).
- [ ] 3. `test_cron_backup_after_worker_fired`: `run_event_backup(key="orb_open")` at 09:36 after the worker fired returns `skipped` and creates no proposal; `run_event_backup(key="flatten")` at 15:55 after the worker's flatten returns `skipped` (Review Focus 1).
- [ ] 4. `test_foreign_chat_cannot_approve`: an Approve callback with the right data from another chat id leaves the proposal pending (Review Focus 2).
- [ ] 5. Gate and commit `P3-T13: ...`.

---

## Open questions for Stephen (defaults chosen; the build does not wait on them)

1. **Late events:** an entry event (the 9:35 ORB) fires up to 120 s late (`scheduler.late_grace_seconds`) and is otherwise recorded as missed and alerted; `flatten`, `entry_cancel` and `overlay_decision` fire however late, until the close. Change either in Settings.
2. **Extra cron line:** `12:55 Mon–Fri trader event flatten` backs up the flatten on early-close days (SPEC §9 has only 15:55, which is after a 13:00 close). On normal days it does nothing.
3. **Pre-open message:** sent every trading day, even when everything is OK (`preopen.notify_when_ok`, default on), so you see the approval mode each morning. Turn it off to hear only about problems.
4. **Real-button test:** agents can only send to the dev bot, not tap. After the worker is deployed (P4-T11), one tap on a real proposal (or `telegram-test --buttons`) would confirm the round trip, as S6 did.
5. **supercronic and `CRON_TZ`:** the crontab uses `CRON_TZ=America/New_York` as SPEC §2 says; P4-T10 must confirm with `supercronic -test docker/crontab` in the image that supercronic honours it.

## Self-review (done by the plan author)

- **Coverage:** BR-30 (mode shown in `/status`, pre-open; toggle stays P2/P4), BR-31 (T6 approvals, T8 proposal relay, expiry via T9 `tick`), BR-32 (T4 formats, T8 relay, T10/T11 direct sends), BR-33 (decision time and unprotected time in T4, T7, T11), BR-34 (T7 commands, `/resume` never resets automatic switches), BR-60 (T11 summary and journal, T6 answer). SPEC §1 worker (T9), §4.4 (T4–T8), §9 (T3, T10–T12), §8 candle archive (T11), §14 Telegram security (T6), S6 findings (T5, T6).
- **Concurrency:** T2–T11 each depend on T1 only and own disjoint files; `cli.py`, `docker/crontab`, `trader/runtime.py` and the master plan belong to T12 alone.
- **No implementation code** in this plan: interfaces are signatures and data shapes; behaviour and tests are in words.

## Plan verify+fix (P3-T0 attempt 1)

Changes made by the combined Verifier and Spec reviewer, checked against the Phase 2 code on trunk:
- **Migration renumbered to `0004`** (`down_revision = "0003"`, the P2-B1 `exit_reason` fix); the T1 builder re-checks `alembic heads` at build time and chains onto the single head (T1).
- **Settled event keys:** `fired_keys` now also counts missed events and events that failed `MAX_EVENT_ATTEMPTS` times, so the worker and the check-ins stop re-firing them every step (T3 test 12). Event keys are capped at 39 characters so `job.event:<key>` fits `event_log.source`.
- **Telegram decisions:** `decide` has `ProposalService.decide`'s real four-argument signature (actor `telegram:<from_id>`). Proposal nonces carry no TTL, so a late tap reaches `decide` and gets `Already expired` (this removes a contradiction between T6's behaviour and its test 6). `KeyError` and the `failed` status are handled. T12 builds the bot's decider on its own `SimBroker`, so taps work outside a session.
- **Protocols match P2 names** (`run_event(event_key, ...)`), and T1's contract test checks that P2's `Engine` satisfies them.
- **Auto mode:** proposals are relayed as silent `AUTO` messages (BR-32 in auto mode). Overlay "holding" notes at `error` level render as overlay messages, not alerts.
- **LIVE safety:** dev bot only, no real orders possible, `--once` never long-polls, and the worker smoke run happens only outside the session.
- `tests/notify/__init__.py` moved to T1 (it was shared by T4 and T5). The DST test date is corrected (the clocks change on Sun 2026-11-01). The `LISTEN/NOTIFY` to polling choice is stated.
