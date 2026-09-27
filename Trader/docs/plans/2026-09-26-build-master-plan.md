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
| 2 | Engine: strategy framework, `orb_sip` and `spy_overlay`, risk manager and kill-switch checks, proposal service, simulated broker, fill model, ledger, Claude catalysts, pre-market job | outline §7.2; detail written at phase start (P2-T0) |
| 3 | Worker, Telegram bot and commands, event scheduler, cron schedule, remaining jobs (preopen, checkin, postclose incl. candle archive, token keep-alive) | outline §7.3; detail at phase start (P3-T0) |
| 4 | REST API, auth, React web app, Docker image, deploy script, deploy to `trader-dev` | outline §7.4; detail at phase start (P4-T0) |
| 5 | Replay mode, metrics and reports (daily, weekly, CSV), kill-switch reset flow, hardening | outline §7.5; detail at phase start (P5-T0) |
| 6 | Dev soak (10 trading days) and promotion to prod | outline §7.6; detail at phase start (P6-T0); needs Stephen |

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
- [ ] **Phase start estimate.** When a phase starts (its `Pn-T0` or first task), tell Stephen the estimated duration and expected finish time (UTC), with the reasoning: the critical path of dependent tasks × about 30–45 min per task (build + gauntlet), plus ~30% for fix rounds, plus ~45 min for the phase review and next plan. Record the estimate in the header. Revise it in progress reports when the actual pace differs.
- [ ] **Notify.** At each phase end, and on every escalation, send Stephen a Telegram message through the dev bot: `bash Trader/app/scripts/trader-dev.sh notify "<text>"` once P1-T9 is accepted, otherwise `python3 Trader/build/notify.py "<text>"` (created in P1-T1).

## 5. The gauntlet

Every task, including each phase's planning task `Pn-T0`, must pass every stage in order. A failure at any stage sends the task back to the Builder with the findings (status `fixing`). After the Builder's fix, the gauntlet restarts **from the Verifier**.

| # | Stage | Pass condition |
|---|---|---|
| 1 | **Verifier** | `check.sh` passes on a fresh `git pull` of trunk; the task's plan steps are all ticked; there are no uncommitted changes |
| 2 | **Breaker**, run in parallel with stage 3 | Writes 3–8 new tests aimed at the task's weak points (Review Focus items it owns, bad input, boundaries, time zones, concurrency, idempotency). **Passes** if all its new tests pass against the Builder's code. Failing tests are committed and go back to the Builder |
| 3 | **Spec reviewer** and **Code reviewer** (run in parallel with each other and with the Breaker; both read-only) | No findings rated **must-fix**. Findings rated **should-fix** go back to the Builder once; **nit** findings are logged only |

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
Shell rules (a hook enforces them): one command per Bash call. No `&&`, `;` or `||` chaining, no `cd`
at all (even on its own), and no `git -C`. Use absolute paths, and `uv --directory
"/Users/stephen/Documents/Code/Claude Code/Trader/Trader/app" run ...` wherever a plan says "run from
Trader/app". Git commands run from the repo root, the Bash tool's default working directory. Always pull with `git pull --rebase --autostash` (other agents' log entries leave
BUILD_STATE.md modified). So "git pull --rebase && git push" means two separate calls, and a plan's `cd ... && git add ...` block
becomes separate `git add` / `git commit` calls with absolute or repo-relative paths. Avoid semicolons
even inside heredoc bodies (the hook sees them). If `uv sync` ran before `trader/` existed, run
`uv sync --reinstall-package trader` once.
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
parallel lanes. Implement the task outline for Phase <n> in §7 of the master plan, keeping the §7.1 cross-phase contracts. List the phase's task IDs at the top of your plan;
the orchestrator adds them to the task board.
```

## 7. Task outlines for Phases 2–6

These outlines fix each later phase's **task boundaries, files, cross-phase interfaces and test obligations**. The Planner of each phase (`Pn-T0`) turns them into a detailed plan with code, reading the real code built so far. The Planner may split or merge tasks, and may refine types where the real code requires it, but must keep the **cross-phase contracts** below (names, arguments and meaning), or escalate if one can't work.

Conventions for all outlines: paths are under `Trader/app/` unless they start with `web/` or `docker/`; each task ends with `check.sh` passing and a commit; "Tests prove" lists what the tests must show, and the Breaker adds its own on top.

### 7.1 Cross-phase contracts

These are the seams between phases. Changing one means updating every phase that uses it.

| Contract | Defined in | Used by | Shape |
|---|---|---|---|
| Clock | P1-T4 | all | `Clock.now() -> datetime` (UTC). `ReplayClock` (P5) implements it |
| Intents | P2-T6 | P2-T8/9 strategies → P2-T10 risk → P2-T13 engine | `EnterLong(symbol_id, order_type, stop, limit, stop_loss, reason, evidence)`, `Exit(position_id, order_type, stop, reason)`, `Cancel(order_id, reason)`; frozen dataclasses, prices `Decimal` |
| Strategy | P2-T6 | P2-T8/9, P3-T1 scheduler, P5 replay | `Strategy` protocol exactly as SPEC §5.1; `ScheduledEvent(key: str, at: SessionOffset)`; `SessionOffset.parse("open+5m" \| "close-30m")`, `.resolve(cal, session) -> datetime` |
| Risk | P2-T10 | P2-T13 | `RiskManager.evaluate(intent, ctx: RiskContext) -> SizedOrder \| Rejection` |
| Proposals | P2-T11 | P2-T13, P3 bot, P4 API | `ProposalService.create(signal_id, sized: SizedOrder, kind) -> Proposal`; `decide(proposal_id, decision: Literal["approve","reject"], via: Literal["telegram","web","auto"], actor: str) -> DecisionResult(status, already_decided: bool)`; `expire_due(now) -> list[Proposal]` |
| Broker | P2-T5 | P2-T13, P3 worker, P5 replay | `Broker.submit(OrderSpec) -> int` (order id); `cancel(order_id, reason)`; `on_quotes(quotes: Sequence[QtQuote], now) -> list[FillEvent]`; `end_of_session(session_date)` |
| Fill model | P2-T4 (quotes), P5-T2 (candles) | SimBroker, replay | `FillModel.evaluate(order: OrderSpec, market: QtQuote \| Candle, now) -> FillDecision \| None` |
| Market data | P2-T7 | strategies, jobs, worker | `MarketDataService` (async): `universe(session_date)`, `open_bar_stats(session_date)`, `opening_bars(session_date)`, `quotes(symbol_ids)`, `candles(symbol_id, start, end, interval)`; reads the DB cache first, Questrade second |
| Notifier | P3-T3 | engine, jobs, worker, P5 reports | `Notifier.send(msg: OutboundMessage) -> None`; message builders are pure functions in `trader/notify/messages.py` |
| Event firing | P3-T1 | worker, cron backups | `fire_event(key, session_date)` idempotent via `job_runs` row `event:<key>` |

### 7.2 Phase 2: Engine

Requirements: BRD BR-03, BR-05, BR-10–13, BR-20–23, BR-40–42; SPEC §3a, §4.2 (pre-market), §4.3, §5, §6, §7.1–7.3, §10.

| ID | Task | Files | Tests prove | Depends on |
|---|---|---|---|---|
| P2-T0 | Write the Phase 2 detailed plan | `docs/plans/<date>-phase-2-engine.md` | (planning gauntlet, §5) | P1-REVIEW |
| P2-T1 | Migration 0002: trading tables and views | `trader/db/models.py`, `trader/db/migrations/versions/0002_trading.py`, `tests/db/test_migration_0002.py` | Every §10 trading table exists with its keys; `run_id` on every trading row; `v_trade_metrics` gives the right win rate and expectancy for a hand-made set of trades | P1 |
| P2-T2 | Runs, sim account and the full runtime settings set | `trader/engine/runs.py`, `trader/settings_store.py` | `get_live_run()` creates one live run and reuses it; the sim account applies the CAD→USD rate and 1.5% fee once; every §13 runtime key has a validated default (risk_pct 2%, slippage $0.01/5 bps, TTLs 5/3/5 min, kill-switch 5%/15%/50 trades, no_entry_before_close 30 min, quote_poll 2 s, stale_quote 10 s, auto_flatten_on_expiry true, Claude model/budget/cap) | T1 |
| P2-T3 | Ledger with T+1 settlement | `trader/broker/ledger.py` | Cash settles on the next session (Fri → Mon, Wed before Thanksgiving → Fri); settled vs total cash; buying power follows `cash_account_mode`; ledger rows are append-only | T1, T2 |
| P2-T4 | Quote-based fill model | `trader/broker/fill_model.py` | Every row of the SPEC §7.2 table, including stop-limit refusal above the limit; slippage = max(min, bps × price); stale quote (> 10 s) never fills; SEC fee on sells only; the quote snapshot is returned with each fill | T2 |
| P2-T5 | Simulated broker, orders, positions, trades | `trader/broker/base.py`, `trader/broker/sim_broker.py` | Submit → working → filled; cancel; position opens and closes; trade row with P&L and R; end of session cancels every working order; long only (a sell larger than the position is refused) | T3, T4 |
| P2-T6 | Strategy framework and registry | `trader/strategies/base.py`, `trader/strategies/registry.py`, `pyproject.toml` (entry points) | `SessionOffset` resolves on normal and early-close days; plug-ins found through the `trader.strategies` entry point; each settings change creates a new `strategy_configs` version; invalid params rejected by the plug-in's pydantic model | T1, T2 |
| P2-T7 | Market data service | `trader/market/data_service.py` | Reads universe, stats and bars from the cache; falls back to Questrade; fetches 9:30–9:35 bars for the whole universe within the rate limit; missing bars reported per symbol, not raised (Review Focus 4) | T6 |
| P2-T8 | `orb_sip` plug-in 1.0.0 | `trader/strategies/orb_sip.py`, `tests/strategies/scenarios/*.py` | Hand-built scenarios: breakout fills; no breakout, cancelled at 11:30; stop hit; doji skipped; bearish candle skipped; price/ATR out of range; catalyst missing or bearish; early close moves the flatten; every candidate saved with its reject reason; evidence recorded (BR-13) | T6, T7 |
| P2-T9 | `spy_overlay` plug-in 1.0.0 | `trader/strategies/spy_overlay.py` | Negative SPY return from the prior close exits entry positions at 15:30; positive or zero holds; the decision is logged either way; uses `close-30m` on early-close days | T6, T7 |
| P2-T10 | Risk manager and kill switches | `trader/engine/risk.py`, `trader/engine/killswitch.py` | Sizing formula and each of the six checks in SPEC §6.1 order; zero shares rejected; exits and cancels never blocked; daily loss resets next session; drawdown and expectancy need a manual reset with a reason; `manual_pause` blocks entries only; every trip writes `kill_switch_events` | T5 |
| P2-T11 | Proposal service | `trader/engine/proposals.py` | Every state transition in SPEC §6.2; first decision wins, the second gets `already_decided`; expiry by kind and TTL; auto mode approves at once; expired protective stop records unprotected time and escalates; expired flatten auto-submits when enabled; decision latency stored; approval-mode changes audited | T5, T10 |
| P2-T12 | Claude catalyst classifier | `trader/adapters/claude/catalyst.py` | JSON-schema structured output parsed and validated; daily budget stops calls and marks `unknown`; cost stored per call; the model comes from settings; the API is mocked (no network) | T2 |
| P2-T13 | Engine orchestrator | `trader/engine/orchestrator.py` | Intent → risk → proposal → broker → fill → `on_fill` → protective stop, end to end with fakes; rejections logged; signals and candidates saved with the strategy config version | T8–T12 |
| P2-T14 | Pre-market job | `trader/jobs/premarket.py`, `trader/cli.py` | FinViz news and earnings screens plus the ≥ 3% gap check from quotes; headlines per candidate; top 50 by gap classified, the rest marked "not classified (over cap)"; returns the brief text; idempotent per session | T7, T12 |
| P2-T15 | Integration: one full simulated day | `tests/integration/test_simulated_day.py` | Fake clock and fake data from nightly to flatten: signal, proposal, auto-approval, fill, stop, flatten, trade and ledger rows correct; no position left open (BR-42) | T13, T14 |

Parallel lanes after T2: {T3, T4} then T5; {T6 → T7 → T8, T9}; T12 alone.

### 7.3 Phase 3: Worker, Telegram and schedule

Requirements: BRD BR-30–34, BR-60; SPEC §1 (processes), §4.4, §9; spike S6 findings.

| ID | Task | Files | Tests prove | Depends on |
|---|---|---|---|---|
| P3-T0 | Write the Phase 3 detailed plan | `docs/plans/<date>-phase-3-worker-telegram.md` | (planning gauntlet) | P2 |
| P3-T1 | Session event scheduler | `trader/engine/scheduler.py` | Today's events come from every enabled strategy's `schedule()`; resolved to real times on normal, early-close and holiday days; `fire_event` runs once per (key, session) even when the worker and the cron backup both fire | P2 |
| P3-T2 | Worker process | `trader/worker.py`, migration `0003_worker.py` (heartbeat) | Quote polling only for symbols with working orders; fills flow to the engine; proposals expire on time; events fire at their times; heartbeat updated; after a restart mid-session it resumes working orders and pending proposals without duplicating anything | T1 |
| P3-T3 | Notifier and message formats | `trader/notify/messages.py`, `trader/notify/notifier.py` | Every message type in SPEC §4.4 is self-contained (ticker, qty, prices, stop, P&L, reason) and includes the home-network link; sending failures are logged with Telegram's error body and never raise into the engine | P2 |
| P3-T4 | Telegram bot: approvals | `trader/adapters/telegram/bot.py` | Only the configured chat ID is accepted; callback data is signed with a nonce, and a replayed or forged callback is refused; Approve/Reject call `ProposalService.decide`; the button is acknowledged before the message is edited; a failed acknowledgement doesn't undo the decision | T3 |
| P3-T5 | Telegram commands | `trader/adapters/telegram/commands.py` | `/status`, `/positions`, `/pnl`, `/pending` output; `/pause` asks for confirmation, then blocks entries only and is audited; `/resume` lifts only the manual pause, never an automatic kill switch; other chats ignored and logged | T4 |
| P3-T6 | Day-level jobs | `trader/jobs/{preopen,checkin,events}.py`, `trader/cli.py` | Each job does nothing on a holiday; `preopen` checks the token, data freshness, kill switches and worker heartbeat, and alerts on problems; cron backup `trader event <key>` is a no-op if the worker already fired it | T1, T3 |
| P3-T7 | Post-close job and candle archive | `trader/jobs/postclose.py` | End-of-day cancels; equity snapshot; journal row; daily summary with the Rules-followed button; **candle archive**: the 9:30–9:35 bar for every universe symbol plus 1-minute regular-hours candles for the top 20 and SPY; idempotent | T3, T6 |
| P3-T8 | Crontab | `docker/crontab`, `tests/test_crontab.py` | Every line matches the SPEC §9 table and parses; `CRON_TZ=America/New_York`; every command exists in the CLI | T6, T7 |
| P3-T9 | Integration: worker day with fake Telegram | `tests/integration/test_worker_day.py` | Manual approval mode through a fake Telegram: proposal message, tap Approve, fill, stop, overlay decision, flatten, daily summary, all in order and on time with a fake clock | T2–T8 |

### 7.4 Phase 4: API, web app and deployment

Requirements: BRD BR-50–56, BR-62 (export route); SPEC §4.2 (manual CSV upload), §11, §12, §14, §15.

| ID | Task | Files | Tests prove | Depends on |
|---|---|---|---|---|
| P4-T0 | Write the Phase 4 detailed plan | `docs/plans/<date>-phase-4-web-deploy.md` | (planning gauntlet) | P3 |
| P4-T1 | API skeleton and health | `trader/api/main.py`, `trader/api/deps.py` | App factory wires `Core`; `/api/health` reports DB, token age and worker heartbeat without login; errors are JSON; serves the built web app | P3 |
| P4-T2 | Authentication | `trader/api/auth.py`, migration `0004_users.py` | Argon2 hashes; HttpOnly, Secure, SameSite=Strict cookie; CSRF token required on changes; login rate limit and lockout; optional TOTP; the admin user is created from env on first start only | T1 |
| P4-T3 | Read endpoints | `trader/api/routers/{dashboard,trading,metrics,system}.py`, `trader/api/schemas.py` | Every GET in SPEC §11 returns the documented data from a seeded DB; filters by run and date work; every endpoint needs the session cookie | T2 |
| P4-T4 | Write endpoints | `trader/api/routers/{proposals,settings,strategies,journal,killswitch,jobs,credentials,watchlist}.py` | Approve/reject share `ProposalService.decide` with Telegram; settings validated and audited; strategy settings form schema is the plug-in's JSON Schema; kill-switch reset needs a reason; Questrade token paste reseeds `QuestradeAuth`; CSV watchlist upload (§4.2 manual fallback) | T3 |
| P4-T5 | Live updates (SSE) | `trader/api/routers/stream.py` | A new proposal, fill or event reaches a connected client within 2 s (driven by `LISTEN/NOTIFY`) | T3 |
| P4-T6 | Web app shell | `web/` (Vite, React 18, TypeScript, TanStack Query), `web/src/api.ts`, login page, layout | Login and logout; phone-width layout; times shown in America/Edmonton; Vitest component tests | T2 |
| P4-T7 | Dashboard and Candidates pages | `web/src/pages/{Dashboard,Candidates}.tsx` | Session timeline, approval badge, pending approvals with countdowns and buttons, position card, kill-switch lights, events; ranking table with reject reasons | T5, T6 |
| P4-T8 | Trades, Performance and Journal pages | `web/src/pages/{Trades,Performance,Journal}.tsx` | Trade detail chain (signal → proposal → decision → order → fill quote) with a 5-minute chart; equity curve, drawdown, R histogram and metric tiles; journal editing | T6 |
| P4-T9 | Settings and System pages | `web/src/pages/{Settings,System}.tsx` | Approval-mode toggle; settings forms generated from JSON Schema; Telegram test button; job runs, token status, heartbeat, errors, rate-limit usage | T6 |
| P4-T10 | Docker image and runtime | `docker/Dockerfile`, `docker/supervisord.conf`, `docker/entrypoint.sh`, `docker/docker-compose.dev.yml` | Multi-stage build (node:22-alpine → python:3.12-slim); non-root user; read-only root filesystem except `/tmp`; entrypoint runs `alembic upgrade head` as owner, creates the admin if missing, starts supervisord (api, worker, cron); no published ports; networks `trader_internal` + external `proxy`; volume `trader_dev_logs` | T1–T9 |
| P4-T11 | Deploy script and LIVE deploy to dev | `docker/deploy.sh`, `web/tests/smoke.spec.ts` (Playwright) | `deploy.sh dev` builds amd64 on the Mac, ships to `192.168.68.73`, recreates `trader-dev`, and waits for `https://trader-dev.sunspinner.ca/api/health` = 200; Playwright smoke: login → dashboard → approve a seeded proposal | T10 |

### 7.5 Phase 5: Replay, reports, hardening

Requirements: BRD BR-41, BR-52, BR-54, BR-60–62, non-functional reliability; SPEC §7.4, §8, §16.

| ID | Task | Files | Tests prove | Depends on |
|---|---|---|---|---|
| P5-T0 | Write the Phase 5 detailed plan | `docs/plans/<date>-phase-5-replay-reports.md` | (planning gauntlet) | P4 |
| P5-T1 | Metrics | `trader/reports/metrics.py` | Expectancy in R, win rate, profit factor, max drawdown, average slippage and adherence % match hand calculations; per run and date range | P4 |
| P5-T2 | Replay clock and candle fill model | `trader/replay/clock.py`, `trader/replay/candle_fill_model.py` | `ReplayClock` implements `Clock`; every SPEC §7.4 rule, including worst case when one bar touches both entry and stop, and the half-spread estimate | P4 |
| P5-T3 | Replay data source | `trader/replay/data.py` | Candle archive first, Questrade second (inside its ~3-month window); universe from snapshots, else the current universe with the "biased universe" label; `replay_catalyst_mode` uses stored catalysts or `unknown`; no Claude calls | T2 |
| P5-T4 | Replay runner, CLI and API | `trader/replay/runner.py`, `trader/cli.py`, API replay routes, `tests/replay/golden/` | `trader replay --from --to` creates a `replay` run with automatic approvals; results never mix with the live run; **determinism golden test**: the same inputs give identical trades | T1–T3 |
| P5-T5 | Replay page | `web/src/pages/Replay.tsx` | Start a replay, watch progress, view results, compare with live | T4 |
| P5-T6 | Reports and export | `trader/reports/{daily,weekly,export}.py`, `trader/jobs/weekly.py` | Daily summary content; weekly report on Saturday with Claude commentary whose numbers all appear in the metrics it was given (checked by the test); CSV export of all trades | T1 |
| P5-T7 | Kill-switch reset flow and alerts | `web/src/pages/Settings.tsx` (reset panel), API | Reset needs a typed reason and is audited; each switch trips from realistic data and alerts on Telegram; the expectancy switch trips after N trades with expectancy ≤ threshold | T1 |
| P5-T8 | Hardening | `trader/logging.py`, `trader/jobs/runner.py`, `docker/*` | structlog JSON to stdout mirrored to `event_log` with secrets redacted; a failed job retries and alerts; container restart during market hours recovers without duplicates; resource and log-volume limits set | T4–T7 |

### 7.6 Phase 6: Soak and promotion

Requirements: SPEC §15.1. Most of this phase is waiting and checking; the steps marked **Stephen** need him.

| ID | Task | Files | Tests prove / done when | Depends on |
|---|---|---|---|---|
| P6-T0 | Write the Phase 6 detailed plan | `docs/plans/<date>-phase-6-promotion.md` | (planning gauntlet) | P5 |
| P6-T1 | Live rechecks | `spikes/README.md` | S2 (quote timestamp within 2 s during market hours) and live S4 (9:35 scan under 60 s) run with `trader questrade-check` and a timed scan; results recorded | P5 |
| P6-T2 | Soak report | `trader/jobs/soak.py`, `trader/cli.py` | `trader soak-report` lists, per trading day, failed jobs and missed 9:35 events; the orchestrator records a running count of consecutive clean days in `BUILD_STATE.md` | P5 |
| P6-T3 | 10 clean trading days | `BUILD_STATE.md` | 10 consecutive trading days with no failed jobs and no missed 9:35 events; the full test suite and the determinism test pass on the tag to promote | T2 |
| P6-T4 | Manual end-to-end check (**Stephen**) | `BUILD_STATE.md` | Stephen confirms one full simulated day in the web app: signal, approval, fill, stop, flatten, journal | T3 |
| P6-T5 | Promote to prod (**Stephen** for credentials) | `docker/.env.prod` (git-ignored), NPM, Pi-hole | Prod Questrade app, bot and Anthropic key created by Stephen; `trader` database and roles; `deploy.sh prod`; NPM host, certificate and Pi-hole record for `trader.sunspinner.ca`; a fresh live run starts | T4 |
