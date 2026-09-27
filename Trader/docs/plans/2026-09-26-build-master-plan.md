# Trader Build: Master Plan

> **For agentic workers:** This is the top-level plan. The orchestrator follows §2–§5 to run the build. Detailed step-by-step plans live next to this file, one per phase (`2026-09-26-phase-1-data-layer.md` first). Before starting any phase whose detailed plan doesn't exist yet, the orchestrator writes it with the `superpowers:writing-plans` skill, following §7 (it goes through the gauntlet as task `Pn-T0`). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Trader simulation platform described in the approved BRD and SPEC, in the dev environment, to the point where it meets the dev → prod promotion criteria (SPEC §15.1).

**Architecture:** A single Python 3.12 package (`Trader/app/trader`) with adapters (Questrade, FinViz, Claude, Telegram), a market-data layer, a strategy plug-in engine, a simulated broker and ledger, scheduled jobs and a long-running worker. A FastAPI API serves a React web app. Everything runs in one Docker container on the shared Docker host, with PostgreSQL on the shared database server.

**Tech stack:** Python 3.12 (uv), SQLAlchemy 2 + Alembic + psycopg 3, httpx, selectolax, pydantic v2, exchange_calendars, pandas, anthropic, python-telegram-bot v21, FastAPI, Typer (CLI), structlog; React 18 + TypeScript + Vite; pytest, respx, testcontainers, Vitest, Playwright; Docker, supercronic, supervisord.

**Spec:** [`../BRD.md`](../BRD.md), [`../SPEC.md`](../SPEC.md) (v1.0 plus the Phase 0 amendments of 2026-09-26), and the Phase 0 findings in [`../../spikes/README.md`](../../spikes/README.md). Executors read all three.

**Shared state file:** [`../build/BUILD_STATE.md`](../build/BUILD_STATE.md). Every agent reads it first and writes to it as described in §3.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **Branching:** work directly on `trunk` (repo `mcgannst/PersonalBroker`). No feature branches, no worktrees (repo `CLAUDE.md`).
- **Python 3.12** (SPEC §2), managed with `uv`; the project lives in `Trader/app/` with `pyproject.toml` there.
- **Package layout** exactly as SPEC §3 (`Trader/app/trader/...`, `Trader/web/`, `Trader/docker/`).
- **Database:** PostgreSQL 14 at `192.168.68.86:5432`. Dev database `trader_dev`, schema `trader`. Migrations run as `trader_dev_owner` (`MIGRATION_DATABASE_URL`); the app runs as `trader_dev_app` (`DATABASE_URL`), which **cannot create or drop tables**, so any DDL (including partitions) belongs in migrations.
- **Timestamps** are `timestamptz` in UTC. **Money and prices** are `numeric(14,4)` in the DB and `Decimal` in Python. Never use `float` for money.
- **Time:** all logic gets the time from a `Clock`; never call `datetime.now()` or `date.today()` in `trader/` outside `trader/market/clock.py`. Schedules are America/New_York; UI shows America/Edmonton.
- **Secrets:** read from `Trader/docker/.env.dev` (git-ignored) via environment variables. Never print, log or commit a secret value. Never put a secret in a test fixture.
- **Questrade token chain has exactly one owner.** Before Phase 1 task P1-T6, the owner is `Trader/docker/.env.dev` (`QUESTRADE_REFRESH_TOKEN`, used only by `spikes/qt.py`). P1-T6 moves it into `trader_dev.trader.api_credentials`. After that, **never run `spikes/qt.py` again**, and nothing else may use the token in `.env.dev`.
- **Keep the token alive:** once seeded, run `bash Trader/app/scripts/trader-dev.sh token-refresh` at least once every 24 hours of calendar time until cron runs it (Phase 3). The orchestrator does this at every start/resume (§4). If it fails with "already used or expired", stop and escalate (§5.4): Stephen must generate a new manual token.
- **Unit and adapter tests never touch the network.** Questrade, FinViz, Claude and Telegram are mocked (respx / fakes). Only steps explicitly marked **LIVE** may call real services, and they use the dev credentials.
- **Integration tests** use a throwaway PostgreSQL 14 container (testcontainers, Docker Desktop on the Mac), never `trader_dev`.
- **FinViz politeness:** at most 1 request per 2 s, a browser User-Agent, 12-hour cache (SPEC §4.2).
- **Questrade limits:** market data 20 req/s, account 30 req/s (SPEC §4.1). Intraday candles exist only ~3 months back; candles include 04:00–20:00 ET, so filter to regular hours explicitly.
- **FinViz universe filters default:** `ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa`. SPY is always added separately.
- **Quality gate** (run by every builder before committing and by the Verifier): `bash Trader/app/scripts/check.sh` = `ruff check`, `ruff format --check`, `mypy trader`, `pytest -q`. All must pass.
- **Commits:** message starts with the task ID (`P1-T4: ...`), ends with the trailer `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Stage files by explicit path, never `git add -A` or `git add .`. `git pull --rebase` then `git push` after every commit.

## Review Focus

The five conditions the spec implies but that are easy to miss, most likely first. Each has a test in the task that owns it (task IDs in brackets).

1. **Questrade token rotated by one process while another holds the old one** (api, worker and cron share it). Expected: exactly one exchange; the second process reuses the new token; no dead chain. [P1-T6: `test_concurrent_refresh_exchanges_once`]
2. **Early-close and holiday sessions** (e.g. 13:00 ET close, Thanksgiving). Expected: no jobs on holidays; session events and the flatten follow the real close; T+1 skips weekends and holidays. [P1-T4: `test_early_close_day`, `test_holiday_is_not_a_session`; Phase 2 ledger tests]
3. **FinViz returns something unexpected** (block page, empty body, layout change, or an ignored filter that returns the whole market). Expected: an error, an alert, and the previous night's universe is used; never a silently wrong universe. [P1-T5: `test_filter_ignored_raises`, `test_empty_body_is_blocked`; P1-T9: `test_nightly_falls_back_to_previous_universe`]
4. **A stale or missing quote/candle at a decision point** (halted stock, no 9:30 bar, quote older than 10 s). Expected: no fill and no crash; the symbol is skipped with a recorded reason. [P1-T7: `test_candles_many_reports_missing`; Phase 2 fill-model tests]
5. **A job run twice for the same session, or re-run after a crash halfway.** Expected: no duplicate rows; the second run is a no-op or completes the remainder. [P1-T9: `test_nightly_is_idempotent`]

---

## 1. Phases

Phases follow the BRD delivery plan (§11). Each phase ends with a working, tested increment committed to `trunk`.

| Phase | Deliverable | Detailed plan |
|---|---|---|
| 1 | Data layer: toolchain, DB schema and migrations, calendar and clock, settings, FinViz scraper, Questrade auth and client, indicators, nightly job, CLI | [`2026-09-26-phase-1-data-layer.md`](2026-09-26-phase-1-data-layer.md) (written) |
| 2 | Engine: strategy framework, `orb_sip` and `spy_overlay`, risk manager and kill-switch checks, proposal service, simulated broker, fill model, ledger, Claude catalysts, pre-market job | written at phase start (P2-T0) |
| 3 | Worker, Telegram bot and commands, event scheduler, cron schedule, remaining jobs (preopen, checkin, postclose incl. candle archive, token keep-alive) | written at phase start (P3-T0) |
| 4 | REST API, auth, React web app, Docker image, deploy script, deploy to `trader-dev` | written at phase start (P4-T0) |
| 5 | Replay mode, metrics and reports (daily, weekly, CSV), kill-switch reset flow, hardening | written at phase start (P5-T0) |
| 6 | Dev soak (10 trading days) and promotion to prod | written at phase start (P6-T0); needs Stephen |

## 2. Roles

The **orchestrator** is the main Claude session. It never writes product code itself. It spawns subagents with the Agent tool, one role per agent, each with a fresh context. All roles use the prompt templates in §6.

| Role | Does | May change |
|---|---|---|
| **Orchestrator** | Picks the next task, spawns agents, runs the gauntlet, updates the task board, commits the state file, escalates | `docs/build/BUILD_STATE.md` (board, header, escalations) |
| **Planner** | Writes a phase's detailed plan (task `Pn-T0`) | `docs/plans/*.md` |
| **Builder** | Implements one task by following its plan steps with TDD; fixes gauntlet findings | Only the files listed in its task, plus fixtures |
| **Verifier** | Runs the quality gate on a clean checkout of `trunk` and reports | Nothing (read-only) |
| **Breaker** | Tries to break the task: writes new tests for the Review Focus items and edge cases the task implies | Only `Trader/app/tests/gauntlet/test_<task_id>_*.py` |
| **Spec reviewer** | Checks the task against its plan, the SPEC sections it implements and the Global Constraints | Nothing (read-only) |
| **Code reviewer** | Checks readability, naming, duplication, error handling, typing, security (secrets, SQL injection, logging) | Nothing (read-only) |

## 3. The shared state file

Path: `Trader/docs/build/BUILD_STATE.md`. It is the single source of truth for where the build is. Anyone (a new orchestrator session, Stephen) can read it and know exactly what to do next.

### 3.1 Sections

1. **Header:** current phase, current task, current gauntlet stage, last updated (UTC), last pushed commit, token last refreshed.
2. **Task board:** one row per task: `ID | Title | Status | Attempt | Stage results | Last commit`. Status is one of `todo`, `building`, `gauntlet`, `fixing`, `accepted`, `blocked`. Stage results is a short string such as `V✅ B❌(2) S✅ C✅`.
3. **Escalations:** open questions or blockers for Stephen, with the date raised.
4. **Activity log:** append-only; newest at the bottom.

### 3.2 Who writes what

- **Only the orchestrator** edits the header, the task board and the escalations, and only the orchestrator commits the state file.
- **Every agent** (orchestrator included) appends to the activity log: once when it starts, and once when it finishes. It appends with a **single** shell append so concurrent agents can't overwrite each other:

  ```bash
  cat >> Trader/docs/build/BUILD_STATE.md <<'EOF'

  ### 2026-09-27T14:05:12Z · P1-T4 · Builder · attempt 1 · finished
  - Result: done; all plan steps complete
  - Commits: 3f2a9c1, 8b7e0d4
  - Gate: check.sh passed (41 tests)
  - Notes: exchange_calendars early-close API is `session_close(date)`; used it
  EOF
  ```

- Entry heading format: `### <UTC ISO time> · <task ID> · <role> · attempt <n> · <started|finished|failed>`. Body: `Result`, `Commits`, `Gate`, `Findings` (gauntlet roles), `Notes`. Keep entries short.
- **Never** edit or delete earlier log entries. **Never** write secrets into the file.

### 3.3 Commit and push policy

- **Builders** commit after each green step in their plan (at least every 30 minutes of work) and push immediately (`git pull --rebase && git push`).
- **The Breaker** commits its new gauntlet tests even when they fail. They are the evidence, and the Builder makes them pass.
- **The orchestrator** commits `BUILD_STATE.md` after every stage transition and pushes. So the pushed state is never more than one stage behind.

## 4. Orchestrator loop

- [ ] **Start / resume.** Read, in order: this plan, `BUILD_STATE.md`, the current phase plan, and `git log --oneline -20`. Then run `git pull --rebase`.
- [ ] **Token keep-alive.** If `api_credentials` has been seeded (task P1-T6 accepted), run `bash Trader/app/scripts/trader-dev.sh token-refresh`. Before that, if the header's "token last refreshed" is more than 24 hours old, run `python3 Trader/spikes/s1_tokens.py` (it rotates the token in `.env.dev` safely). Record the time in the header. On failure, escalate (§5.4) and stop.
- [ ] **Recover an interrupted stage.** If the header shows a task in `building`, `gauntlet` or `fixing`, check the activity log for that stage's `finished` entry.
  - No `finished` entry: the agent died. Run `git status`. Commit any uncommitted work that belongs to the task's file list (message `<ID>: WIP recovered after interruption`), then restart that stage with a fresh agent and tell it about the recovered commit.
  - There is a `finished` entry: continue from the next stage.
- [ ] **Pick the next task.** It's the first `todo` task whose dependencies (listed in the phase plan) are all `accepted`. Tasks whose file lists don't overlap may run in parallel (the phase plan marks them as parallel lanes). Run at most **3 Builders at once**.
- [ ] **Build.** Set the task to `building`, commit and push the state file, and spawn a Builder (§6.1).
- [ ] **Gauntlet.** When the Builder finishes, set `gauntlet` and run the stages in §5.
- [ ] **Accept.** When every stage passes, set `accepted`, record the last commit, commit and push the state file, and append an orchestrator log entry.
- [ ] **Phase end.** When every task in a phase is `accepted`, spawn a **phase reviewer** (a Code reviewer with the whole phase diff: `git diff <phase-start-commit>..HEAD`). Treat its findings like gauntlet findings on a synthetic task `Pn-REVIEW`. Then write the next phase's plan (task `P<n+1>-T0`).
- [ ] **Notify.** At each phase end, and on every escalation, send Stephen a Telegram message through the dev bot: `bash Trader/app/scripts/trader-dev.sh notify "<text>"` once P1-T9 is accepted, otherwise `python3 Trader/build/notify.py "<text>"` (created in P1-T1).

## 5. The gauntlet

Every task, including each phase's planning task `Pn-T0`, must pass every stage in order. A failure at any stage sends the task back to the Builder with the findings (status `fixing`). After the Builder's fix, the gauntlet restarts **from the Verifier**.

| # | Stage | Pass condition |
|---|---|---|
| 1 | **Verifier** | `check.sh` passes on a fresh `git pull` of trunk; the task's plan steps are all ticked; there are no uncommitted changes |
| 2 | **Breaker** | Writes 3–8 new tests aimed at the task's weak points (Review Focus items it owns, bad input, boundaries, time zones, concurrency, idempotency). **Passes** if all its new tests pass against the Builder's code. Failing tests are committed and go back to the Builder |
| 3 | **Spec reviewer** and **Code reviewer** (run in parallel; both read-only) | No findings rated **must-fix**. Findings rated **should-fix** go back to the Builder once; **nit** findings are logged only |

- **Planning tasks (`Pn-T0`)** skip the Breaker. Their Verifier checks the plan has no placeholders ("TBD", "similar to Task N", steps without code) and that every task has Files, Interfaces, tests and a commit step. Their Spec reviewer checks coverage of the BRD/SPEC items assigned to the phase.
- **Attempts:** a task may go round the loop 3 times. On the 4th failure, set it to `blocked` and escalate.

### 5.4 Escalation

Escalate (write in Escalations, set the task to `blocked`, notify on Telegram, move on to other unblocked tasks) when:
- a task fails the gauntlet 3 times;
- the spec is ambiguous or contradicts itself, or a finding requires changing the SPEC;
- a live service fails in a way code can't fix (Questrade token dead, FinViz blocking, Docker host down);
- anything would need a secret, a permission, or a decision that only Stephen can provide.

Never work around an escalation by guessing. Other unblocked tasks carry on.

## 6. Agent prompt templates

The orchestrator fills in the `<...>` parts. Every prompt starts with the common preamble.

**Common preamble**
```
You are the <ROLE> for task <TASK_ID> ("<TITLE>") of the Trader build, attempt <N>.
Repo: /Users/stephen/Documents/Code/Claude Code/Trader (git, branch trunk; never create branches).
Read first: Trader/docs/plans/2026-09-26-build-master-plan.md (Global Constraints, §3 state-file rules),
then the task section in <PHASE_PLAN_PATH>, then the SPEC sections it cites.
Log to the shared state file exactly as §3.2 says: append a "started" entry now and a
"finished" or "failed" entry at the end, each with ONE `cat >> ... <<'EOF'` command.
Never print or commit secrets. Stage files by explicit path.
```

### 6.1 Builder
```
Implement the task by following its steps in order, ticking each checkbox in the plan file as you go
(commit the plan file together with your code). Use TDD exactly as written: run each failing test and
see it fail before implementing. Run `bash Trader/app/scripts/check.sh` before every commit.
Commit and push after every green step (`git pull --rebase && git push`).
If a step is impossible as written (wrong API, spec conflict), do not improvise a different design:
stop, log "failed" with the reason, and report back.
<On a fix attempt:> Fix these gauntlet findings, adding a regression test for each: <FINDINGS>.
Report: commits, test count, anything surprising.
```

### 6.2 Verifier
```
Read-only. Run `git pull --rebase`, `git status` (must be clean), then `bash Trader/app/scripts/check.sh`.
Confirm every checkbox of the task in the plan is ticked. Report PASS or FAIL with the exact failing output.
```

### 6.3 Breaker
```
Your job is to break this task's code. Read its plan section, the SPEC sections it cites, and the
Review Focus list in the master plan. Write 3–8 NEW pytest tests in
Trader/app/tests/gauntlet/test_<task_id_lowercase>_breaker.py targeting: the Review Focus items this task
owns, malformed or missing input, boundaries, time zones and DST, concurrency and re-runs.
Do not modify any other file. Run them. Commit the test file even if tests fail, and push.
Report PASS (all new tests pass) or FAIL with each failing test name and why it matters.
```

### 6.4 Spec reviewer
```
Read-only. Compare the task's committed code (git log --grep "<TASK_ID>:" and the diffs) with its plan
section, the SPEC sections it cites, and the Global Constraints. Report findings as a list, each rated
must-fix / should-fix / nit, with file:line and the spec text it violates. Report PASS if there are no
must-fix findings.
```

### 6.5 Code reviewer
```
Read-only. Review the task's diffs for correctness, readability, naming consistent with the codebase,
duplication, typing, error handling, resource cleanup, and security (secrets in logs or code, SQL
built from strings, unvalidated input). Rate each finding must-fix / should-fix / nit, with file:line.
Report PASS if there are no must-fix findings.
```

### 6.6 Planner (task `Pn-T0`)
```
Write the detailed plan for Phase <n> using the superpowers:writing-plans skill, saved as
Trader/docs/plans/<date>-phase-<n>-<name>.md. It must follow the structure of the Phase 1 plan:
tasks with Files, Interfaces (exact names and types, reusing what earlier phases produced; read the real
code, not just the earlier plans), bite-sized TDD steps with full code, commit steps, dependencies and
parallel lanes. Cover every requirement assigned to Phase <n> in §7 of the master plan. Add the task rows
to the task board is the orchestrator's job; list them at the top of your plan instead.
```

## 7. Phase scopes for the plans still to be written

Each later phase's Planner must cover all of these. The interfaces listed are the contracts other phases rely on. Keep the names; types may be refined if the real Phase 1 code requires it, and the Planner must say so.

### Phase 2: Engine (BRD BR-10–13, BR-20–23, BR-40–42; SPEC §3a, §4.3, §5, §6, §7.1–7.3, §10 trading tables)

- Migration: `runs`, `sim_accounts`, `strategy_configs`, `catalysts`, `candidates`, `signals`, `proposals`, `orders`, `fills`, `positions`, `trades`, `cash_ledger`, `equity_snapshots`, `journal`, `kill_switch_events`, views `v_daily_pnl`, `v_trade_metrics`.
- `trader/strategies/base.py`: `Strategy` protocol, `StrategyContext`, `ScheduledEvent(key, at: SessionOffset)`, intents `EnterLong`, `Exit`, `Cancel` exactly as SPEC §5.1; `SessionOffset.parse("open+5m")`.
- `trader/strategies/registry.py`: entry-point discovery (`trader.strategies`), `load_enabled(session) -> list[tuple[Strategy, StrategyConfig]]`.
- `trader/strategies/orb_sip.py` and `spy_overlay.py` per SPEC §5.2–5.3, tested against hand-built candle scenarios (breakout, no fill, stop hit, doji, bearish, early close).
- `trader/engine/risk.py`: `RiskManager.evaluate(intent, account, clock) -> SizedIntent | Rejection` with the sizing formula and check order of SPEC §6.1; exits and cancels never blocked.
- `trader/engine/killswitch.py`: `KillSwitches.check(run_id) -> list[TrippedSwitch]`, `trip`, `reset(switch, reason, actor)`, `pause`/`resume` (manual_pause) per SPEC §6.3.
- `trader/engine/proposals.py`: `ProposalService.create(...)`, `decide(proposal_id, decision, via, actor) -> DecisionResult` (first decision wins), `expire_due(now)`, state machine and TTLs per SPEC §6.2, audit log.
- `trader/broker/`: `Broker` protocol, `SimBroker.submit/cancel/on_quote`, `QuoteFillModel` (SPEC §7.2 table, slippage, stale quotes, fees, quote snapshot), `Ledger` (T+1 via `SessionCalendar.next_session`, settled cash, buying power, FX conversion).
- `trader/adapters/claude/catalyst.py`: `classify(inputs) -> Catalyst` with JSON-schema structured output, daily budget, cost tracking; model from settings (`claude-sonnet-5` default).
- `trader/engine/orchestrator.py`: wires intent → risk → proposal → broker → fill → `on_fill`.
- `trader/jobs/premarket.py`: `trader premarket` (FinViz news/earnings screens, gap check from quotes, headlines, top-50 cap, catalysts, Telegram brief text returned for Phase 3 to send).
- Integration test: a full simulated day with fake clock and fake data (SPEC §16).

### Phase 3: Worker, Telegram and schedule (BR-30–34, BR-60; SPEC §1 processes, §4.4, §9)

- `trader/worker.py`: async loop; quote polling for working orders every `quote_poll_seconds`; proposal expiry; strategy events from `Strategy.schedule()` fired at session-relative times; `LISTEN/NOTIFY` wake-ups; heartbeat row; restart recovery during market hours.
- `trader/adapters/telegram/bot.py`: long polling, chat-ID allow-list, signed callback data with nonce, Approve/Reject → `ProposalService.decide`, all message types in SPEC §4.4, commands `/status /positions /pnl /pending /pause /resume /help`, `/pause` confirmation. Acknowledge the button first, then edit the message (spike S6). Log Telegram error bodies; a failed acknowledgement never undoes a decision.
- Jobs: `token-refresh`, `preopen`, `event <name>` (cron backups), `checkin`, `postclose` (end-of-day cancels, journal row, metrics, equity snapshot, **candle archive** of the 9:30–9:35 bar for every universe symbol plus 1-min RTH candles for the top 20 and SPY, daily summary).
- `Trader/docker/crontab` exactly per SPEC §9 with `CRON_TZ=America/New_York`.
- `trader notify` CLI command (replaces `Trader/build/notify.py`).

### Phase 4: API, web app and deployment (BR-50–56; SPEC §11, §12, §14, §15)

- FastAPI app with every endpoint in SPEC §11, session auth (Argon2, signed cookie, CSRF, rate-limited login, optional TOTP), SSE stream, JSON Schema forms for strategy settings.
- React app with every page in SPEC §12, phone layout, America/Edmonton display, Vitest tests, a Playwright smoke test (login → dashboard → approve).
- `Trader/docker/Dockerfile` (multi-stage), `supervisord.conf`, `docker-compose.dev.yml` (container `trader-dev`, networks `trader_internal` + external `proxy`, no published ports, volume `trader_dev_logs`), `deploy.sh dev|prod` following FinanceTracker's `scripts/deploy.sh`.
- LIVE: deploy to `trader-dev`; `https://trader-dev.sunspinner.ca/api/health` returns 200 (NPM host, certificate and access list already exist).

### Phase 5: Replay, reports, hardening (BR-41, BR-52, BR-54, BR-60–62; SPEC §7.4, §8, §16)

- `trader/replay/`: `ReplayClock`, candle fill model (SPEC §7.4, worst case when one bar hits both), runner using archived candles then Questrade, "biased universe" label, `replay_catalyst_mode`.
- Replay determinism golden-file test.
- `trader/reports/`: metrics (expectancy R, win rate, profit factor, drawdown, slippage, adherence), daily summary, weekly report with Claude commentary (no invented numbers), CSV export.
- Kill-switch reset flow in the web app (reason required, audit-logged); alerts.
- Hardening: job retries and alerts, structlog JSON mirrored to `event_log`, container runs non-root with a read-only root filesystem.

### Phase 6: Soak and promotion (SPEC §15.1)

- Rerun S2 and live S4 in market hours and record the results in `spikes/README.md`.
- 10 consecutive trading days in dev with no failed jobs and no missed 9:35 events (tracked in `BUILD_STATE.md`).
- Promotion steps, each needing Stephen: create the prod Questrade app, bot and Anthropic key; create `trader` DB and roles; `.env.prod`; `deploy.sh prod`; NPM host and Pi-hole record for `trader.sunspinner.ca`.
