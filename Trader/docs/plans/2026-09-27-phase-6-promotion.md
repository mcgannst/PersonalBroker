# Phase 6: Soak and Promotion (detailed plan, specification)

> **For agentic workers:** this is a specification plan (master plan §6.6): it says WHAT to build and HOW TO KNOW it works; builders write the code. Most of Phase 6 is watching `trader-dev` run and then creating prod, so only two tasks write code (P6-T2, P6-T6). Every other task is a LIVE procedure with a checklist. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Prove `trader-dev` runs every trading day without failed jobs or missed 9:35 events for 10 consecutive trading days, with a daily clean-day verdict that Stephen gets on Telegram; re-check the Phase 0 spikes live; get Stephen's manual end-to-end check; then create prod (`trader` database, `.env.prod`, NPM host, certificate, Pi-hole record, `deploy.sh prod`, a fresh live run) on the soaked code, in simulation (paper) exactly like dev. No real orders anywhere.

**Requirements:** SPEC §15, §15.1 (promotion criteria 1–4 and promotion steps 1–6), §9 (schedule), §14 (LAN only), §16, §17 (S2, S4 live rechecks); BRD reliability NFRs ("jobs can be re-run safely… a failed job is retried and triggers an alert… the engine recovers after a container restart during market hours") and objective O1 (≥ 95% of scheduled jobs succeed); BRD §11 delivery step 6 (promote dev → prod).

**Task IDs (for the board):** P6-T1, P6-T2, P6-T3, P6-T4, P6-T5, P6-T6, P6-T7, P6-T8.

---

## Task list, dependencies and parallel lanes

| ID | Title | Kind | Depends on | Lane |
|---|---|---|---|---|
| P6-T1 | Live rechecks: S2 quote freshness (pre-market and market hours), S4 live 9:35 scan timing, S1 follow-up | LIVE (orchestrator), no code | P6-T0 (needs a session day: Mon 2026-09-28 at the earliest) | L |
| P6-T2 | Soak report: `trader soak-report` and `trader soak-mark`, the Telegram soak line, two crontab lines, SPEC §9 row | Code + LIVE | P5-T17 **accepted** (its whole gauntlet, fix rounds included: it owns `cli.py`, `runtime.py`, `docker/crontab`, `tests/test_crontab.py`, SPEC §9 and master plan §7.1 until then) and every P5 gauntlet group that edits `trader/notify/*` or `tests/fakes_telegram.py` (P5-GN) accepted | C |
| P6-T3 | 10 clean trading days: daily verdicts in `BUILD_STATE.md`, deploy discipline, fixes, gate on the candidate | LIVE (orchestrator), no code | P6-T2 (the count is derived from the DB, so days before T2 lands are counted retroactively) | S |
| P6-T4 | Stephen's manual end-to-end check of one complete simulated day in the web app | **Stephen** | P6-T0 (any soak day with a trade) | M |
| P6-T5 | Promote to prod: tag, gate, `deploy.sh prod`, seed, isolation and LIVE checks, fresh live run, dev after promotion, first prod day | LIVE (orchestrator) + **Stephen** (one step) | P6-T1, P6-T3, P6-T4, P6-T6, P6-T7, P6-T8 | S |
| P6-T6 | Promotion tooling: `build/prod_env.py`, `build/cron_gap.py`, `deploy.sh` prod guards and downtime stamps | Code | P5-T17 accepted (T6's `cron_gap.py` tests read the real crontab with its weekly line; `deploy.sh` must not change while P5-T18 deploys with it) | D |
| P6-T7 | Stephen's prod credentials: prod Telegram bot, prod Anthropic key, Questrade app "Trader" | **Stephen** | P6-T8 step 2 (`.env.prod` exists) for saving the secrets; creating them can start now | K |
| P6-T8 | Prod infrastructure prep: `trader` DB and roles, `.env.prod`, backup, Pi-hole record, NPM host and certificate, host capacity | LIVE (orchestrator) | P6-T6 | I |

**Changes from the master-plan outline (§7.6), with reasons:**
- Outline IDs T1–T5 keep their meaning. Three tasks are added: **T6** (the tooling that makes `.env.prod`, the database and post-deploy catch-up safe and repeatable, instead of hand-typed commands with secrets), **T7** (Stephen's credential steps, tracked on their own so they run during the soak) and **T8** (prod infrastructure that doesn't need the soaked code, done during the soak so promotion day is short).
- **T4 (manual check) no longer waits for T3.** Any soak day with a real trade serves, and waiting until after day 10 could add days. If a trading-change deploy (decision D2) lands after the check, the check is repeated.
- **No contracts task (`P6-T1`-style) is needed:** the two code tasks (T2, T6) share no interface and own disjoint files. The only cross-task contract is the `soak-report --json` shape (T2 → T3, T5), fixed in this plan.

**Critical path:** P6-T0 → P6-T2 (build + gauntlet, ~1.5 h) → P6-T3 (wall clock: 10 trading days, ending Fri 2026-10-09 at the earliest) → P6-T5 (~2 h on promotion day). Three tasks after the plan. T6 → T8 and T7 run inside the soak window and are not on the critical path unless they slip past Fri 2026-10-09.

**Maximum parallel width:** 5 tasks at once after P6-T0 (T1, T2, T4, T6, T7), of which only **2 are builders** (T2, T6); T3 and T8 join as those finish. Well under the cap of 8 builders.

**Phase estimate (for the header):** build part 2–3 h (T2 ∥ T6 at 45–60 min each plus gauntlets ~30%, then T8 ~1 h); soak wall clock to Fri 2026-10-09 (final Sat 2026-10-10 08:30 MT once the weekly report is in); promotion ~2 h on Sat 2026-10-10 or Sun 2026-10-11; phase end after the first prod session's checks, Mon 2026-10-12 (~15:00 MT after the prod post-close).

---

## Key decisions (resolved here; Stephen can overturn any of them, see Open questions)

**D1. Clean trading day.** A session day D is **clean** when, at evaluation time, every job expected for D (the table below) **finally succeeded before its deadline**, the 9:35 event `event:orb_open` fired within its late grace, and D has no `outage` mark. Precisely:
- A job's status for D is `succeeded` if **any** `job_runs` row for `(job, D)` has `status = 'succeeded'` and `finished_at ≤ deadline` (for `event:*` rows: `started_at ≤ deadline`, because the scheduler judges lateness by when an event fires, and the 9:35 scan itself may take up to a minute; a slow scan that started inside its grace is on time). So a failed attempt followed by a successful in-process retry (P5-T15), a successful worker/cron race loser, or a **manual catch-up after a deploy that succeeds before the deadline** are all clean. Reason: the BRD reliability NFR says jobs are retried and re-run safely; what matters to trading is that the work was done in time and nothing was missed. This also matches the P5 planner's note (a job counts as failed only when its latest row is failed) while not letting a later forced re-run that fails spoil an on-time success.
- `late` (a success only after the deadline), `failed` (the latest row failed and the deadline has passed, or a `running` row is still there at the deadline), `missing` (no row at all by the deadline, e.g. the container was down), and for `event:orb_open` `missed` (an error starting `missed:`) each make D **not clean**.
- Before a job's deadline, a failed or missing job is `pending` (it may still be retried or caught up); D's verdict is `pending` while any check is pending and none has failed for good.
- `token-refresh` writes no `job_runs` row: D gets a `token-refresh` check that fails when an `error` event with source `questrade.token` (runtime `TOKEN_SOURCE`) was written after the previous session's close and by D 18:00 ET, **unless** a `succeeded` row of a job that needs a working token (`premarket`, `event:orb_open` or `postclose` for D) **started after the last such event**. It succeeds when there is no such event. (Verifier fix: `api_credentials.last_refresh_at` holds only the latest refresh, so it can't say whether the chain recovered before a *past* day's close, and a day's verdict must never change on re-evaluation just because time moved on.)
- **Retries (P5-T15/T17):** the in-process retries of `nightly`, `premarket`, `preopen`, `postclose` and `weekly` (at most 3 attempts, waits of `jobs.retry_delay_seconds` × 1, × 2) never start a wait that would end after their `RetryPolicy` deadline: 09:18 ET for `premarket` and 09:28 ET for `preopen` (P5-T17 wiring). The soak deadlines below are later than those retry deadlines on purpose, so an attempt started just before its retry deadline still counts when it succeeds.
- Replay runs never write `job_runs` (P5 decision 3), so they never affect the soak.
- An **outage** (the worker heartbeat stale or `/api/health` not 200 for more than 5 minutes between 09:30 ET and D's close, known to the orchestrator from its checks or a deploy) makes D not clean even if every job succeeded; the orchestrator records it with `trader soak-mark outage`. A deliberate restart shorter than 2 minutes (P5-T18 LIVE 6, the market-hours restart test) is **not** an outage: it is the recovery behaviour the soak should prove, and the day stays clean if no job fails or is missed.

**Expected jobs for session D and their deadlines** (ET wall clock on D unless stated; "close" = D's real close from the calendar, 13:00 on early-close days):

| `job_runs.job` | Expected when | Deadline |
|---|---|---|
| `nightly` (runs the evening before, keyed to its target session D) | every session | D 08:00 (the pre-market scan needs it) |
| `premarket` (cron 08:00; retries stop at 09:18) | every session | D 09:30 (the open: the brief and catalysts must exist before the 9:35 scan) |
| `preopen` (cron 09:20; retries stop at 09:28) | every session | D 09:30 |
| `event:<key>` for every key of D's plan (built read-only, see T2 "Plan") plus every key that has a row for D | every session | by `started_at`: `orb_open` (and any key not in `scheduler.always_fire_late`): the event time + `scheduler.late_grace_seconds` (default 120 s, so the 09:36 cron backup is inside it); safety keys (`flatten`, `entry_cancel`, `overlay_decision`): close |
| `event:orb_open` | required whenever `orb_sip` is enabled (its latest live `strategy_configs` row), even if the plan lacks it (a missing ORB event is a missed 9:35); not expected when `orb_sip` is disabled (dev after promotion, T5) | 09:35:05 + late grace (09:37:05 ET by default), by `started_at` |
| `checkin@11:30`, `checkin@13:30` (`runtime.checkin_job_name`) | every session (after the close a check-in succeeds with "after close") | 12:30 and 14:30 |
| `session_end` (`runtime.SESSION_END_JOB`, the worker) | every session | D 18:00 |
| `postclose` | every session | D 18:00 |
| `weekly` (keyed by the week's last session, P5) | only when D is the last session of its week | the following Saturday 12:00 |
| `token-refresh` (event-based, above) | every session | D 18:00 |

The T2 builder verifies this set against real `trader_dev` rows (T2 LIVE 1). If a listed job never writes a row on trunk (for example `session_end` on some path), the builder removes it from the expected set and records why in this plan's T2 notes; it does not invent other rules.

**D2. Redeploys during the soak.** A deploy **does not reset** the clean-day count unless it is a **trading change**. A deploy is a trading change when its diff (the previously deployed dev commit → the new one) does any of:
1. changes a default or allowed range of a trading setting (`RuntimeSettings` fields other than the `reports.*`, `web.*`, `telegram.*`, `jobs.*`, `preopen.*`, `postclose.*`, `replay.*`, `logging.*` groups; adding a new key to those groups is not a change either) or a strategy's default params;
2. changes the time or command of an existing `docker/crontab` line of a SPEC §9 trading job (token-refresh, nightly, premarket, preopen, event, checkin, postclose), or the events a strategy schedules. Adding a reporting line (P5-T17's `weekly`, T2's two `soak-report` lines) is not a trading change;
3. **modifies** (not only adds) code on the live decision path (`trader/strategies/`, `trader/engine/`, `trader/broker/`, `trader/market/`, `trader/jobs/nightly.py`, `trader/jobs/premarket.py`, `trader/adapters/claude/catalyst.py`, `trader/settings_store.py`) such that an existing assertion in `tests/engine/`, `tests/strategies/`, `tests/broker/`, `tests/market/`, `tests/jobs/` or `tests/integration/` had to change (new tests alone don't count).

The **P5-T18 deploy** (the first one of the soak, expected on a weekday evening or the weekend) is classified by the same rules: P5 open question 9 expects it not to reset (its engine and broker changes are replay additions); if a P5 fix round modified a decision-path file such that an existing assertion changed (for example in `trader/engine/killswitch.py`), it is a trading change and resets.

Replay-only additions (P5's `on_candles` hooks), job retries, logging, reports, web, API reads and message text are not trading changes. The orchestrator classifies every dev deploy in the soak ledger with the rule number and a one-line reason; a trading change is recorded with `trader soak-mark reset --date <first session running the new code>`, so the count restarts at that session (it counts as day 1 if clean). The day of an outage is not clean (D1). **Deploys stay out of 09:15–16:30 ET on session days** unless a fix is urgent (a job would otherwise fail or a position be left unprotected); an urgent in-hours deploy is marked `outage` for that day if it breaks D1.

**D3. Deploy downtime catch-up (Stephen, 2026-09-27).** Deploys (dev and prod) happen whenever ready; nobody waits for a gap between cron lines. After every deploy: take the downtime window that `deploy.sh` now prints (T6), run `uv --directory Trader/app run python ../build/cron_gap.py --from <down> --to <up> --container <trader-dev|trader>` to list the crontab lines that fell in it, run each listed command by hand exactly as printed (`docker --context shared-docker-server exec <container> trader <args> --date <session>`; the jobs are single-flight and idempotent), and confirm its `job_runs` row `succeeded` (for `token-refresh`, its `ok; chain last extended …` line). The printed `--date` matters: without it `trader nightly` targets the session after *today* and the session jobs use *today's* ET date, so a catch-up after midnight would run for the wrong session. A line whose window has already passed (`premarket` after 09:30 ET needs `--force` and would only spend Claude budget; `event orb_open` after its grace only records `missed`) is not run: the day is already not clean under D1, and the ledger says so. Record everything in the soak ledger.

**D4. Daily check and Stephen's Telegram line.** The count is **derived from the database every time** (`trader soak-report`), so nothing is lost if no orchestrator session runs on a day. Two new crontab lines in the container (T2) send one Telegram line per session through the container's own bot (the dev bot `@StephenTraderDevBot` in `trader-dev`): `5 18 * * 1-5 trader soak-report --notify` (18:05 ET = 16:05 MT, after the post-close and its retries) and `30 10 * * 6 trader soak-report --notify --final` (Saturday 10:30 ET = 08:30 MT, after the 09:00 weekly report, finalising the week's last session). The orchestrator additionally records the verdict in `BUILD_STATE.md` each session day (P6-T3). In prod the same lines give a daily "Ops" line (decision D6).

**D5. The promoted build.** SPEC §15.1 says prod runs `trader:<version>` "promoted from a tested dev tag", while `deploy.sh prod` builds the image. Resolved: the **git tag** `v1.0.0` is created on the exact commit `trader-dev` ran for the last clean day (its `/api/meta` version, `git describe --always --dirty` of the deploying checkout, e.g. `phase-1-complete-120-g078a236`; so **every dev deploy during the soak is made from a clean worktree** at a pushed trunk commit, and a version ending in `-dirty` can't be a candidate), `deploy.sh prod` builds `trader:v1.0.0` from a clean checkout of that tag (T6 guards refuse a dirty tree or a HEAD that isn't the tag), and "the image tag being promoted" for criterion 3 means that git tag: the full gate (`bash Trader/build/gate.sh`, including `tests/replay/test_golden.py`) passes on a clean worktree at the tag. The dependencies are pinned by `uv.lock` and `package-lock.json`, so the rebuild is the same code; the Playwright live smoke then runs against prod itself. The candidate must have run in dev for at least one full clean session.

**D6. Prod = dev, in simulation.** Prod runs the same image, the same `SimBroker`, the same paper account model; Questrade is read-only in both; the QuestTrade MCP order tools are never called. Prod is LAN-only exactly like dev (SPEC §14: NPM access list "Home LAN only" `192.168.68.0/22`, Pi-hole local DNS, no public DNS record, no published port); the SPEC does not say otherwise.

---

## File map (one owner per file)

| File | Owner | Change |
|---|---|---|
| `Trader/app/trader/jobs/soak.py` | T2 | new: soak evaluation, report, marks, notify body |
| `Trader/app/trader/cli.py` | T2 | add `soak-report`, `soak-mark` |
| `Trader/app/trader/notify/types.py` | T2 | `MessageKind` gains `"soak"`; `SoakLineView`; `Renderer.soak_line` |
| `Trader/app/trader/notify/messages.py` | T2 | `MessageRenderer.soak_line` |
| `Trader/docker/crontab` | T2 | two lines (D4) and the header comment |
| `Trader/app/tests/jobs/test_soak.py`, `Trader/app/tests/integration/test_soak_report.py`, `Trader/app/tests/test_soak_cli.py` | T2 | new tests |
| `Trader/app/tests/test_crontab.py`, any test pinning `MessageKind` values | T2 | pin the new lines / kind |
| `Trader/app/tests/fakes_telegram.py` | T2 | `FakeRenderer.soak_line` (the P3 contract test `test_implementation_has_every_protocol_member` requires every `Renderer` member on the fake) |
| `Trader/docs/SPEC.md` | T2 (§9 row, after P5-T17's §9 amendment is accepted); later T5 (§15.1 promotion record, §17 statuses), strictly after T2 is accepted | docs |
| `Trader/docs/plans/2026-09-26-build-master-plan.md` §7.1 | T2 (new "Soak report" row, Notifier kind; after P5-T17's §7.1 rows are accepted, and pulling right before the edit since the orchestrator also edits this file); later T5 (Fill model row: live recheck result), strictly after T2 | docs |
| `Trader/build/prod_env.py`, `Trader/build/cron_gap.py` | T6 | new tooling |
| `Trader/docker/deploy.sh` | T6 | prod guards, downtime stamps |
| `Trader/app/tests/test_docker_files.py` (deploy section only), `Trader/app/tests/build/__init__.py`, `Trader/app/tests/build/test_prod_env.py`, `Trader/app/tests/build/test_cron_gap.py`, `Trader/app/tests/integration/test_prod_db_setup.py` | T6 | tests |
| `Trader/spikes/README.md`, `Trader/todo.md` | T1 | live recheck results |
| `Trader/docs/build/BUILD_STATE.md` ("Soak ledger" section, board, header) | orchestrator (T3, T4, T5, T7 records) | state |
| `Trader/docker/.env.prod` (git-ignored, never committed) | T8 creates (via T6 tool); T7 adds Stephen's secrets; T5 blanks the used Questrade token | secrets |

---

## Global Constraints (Phase 6; the master plan's apply in full)

- **Trunk only;** sync with `git pull --rebase --autostash origin trunk`, publish with `git push origin HEAD:trunk`. Stage files by explicit path. Commit messages start with the task ID and end with the `Co-Authored-By` trailer.
- **Re-check trunk names first (P5-T17 lands after this plan was written).** P5-T17 changes `trader/cli.py` (a shared `Core` helper that installs the log mirror, `trader weekly`, `trader replay`), `trader/runtime.py` (`run_cli_job(..., retry=)`, `weekly_job`, `install_log_mirror`), `docker/crontab` (the Saturday `0 9 * * 6 trader weekly` line), SPEC §9 and master plan §7.1. Before writing code, T2 and T6 builders read the accepted P5-T17 commit(s) and use trunk's actual names (the CLI's `Core` helper, the crontab lines, the `weekly` job key and its `session_date`, the retry deadlines); where trunk differs from a name in this plan, trunk wins and the builder notes it in its task section.
- **Deploys from an agent:** use the P4-T19 workaround for the sandbox (a scratch `DOCKER_CONFIG` with only the plugins dir and a copy of `~/.docker/contexts`, never credentials; see the P4 plan's T19 orchestrator notes), and a **clean worktree at a pushed trunk commit** (D5). The shell hook refuses `sh -c`: run in-container checks with `python -c`, `supercronic`, or a scratch script piped to `docker … exec -i <container> python -`.
- **Shared test lane:** while working run only targeted tests; the full gate is always `bash Trader/build/gate.sh` from your worktree root (never `check.sh` directly), once before committing.
- **Shell rules:** one command per Bash call; no `&&`, `;` or `||` chaining, no `cd`, no `git -C`, no semicolons in heredoc bodies. Scratch files only in `<scratchpad>/<TASK_ID>-<role>-a<n>/`.
- **Secrets:** never print, log, echo or commit a secret. Secrets live only in the git-ignored `Trader/docker/.env.dev` and `Trader/docker/.env.prod` (main checkout; a worktree has neither, so pass absolute paths or the relative `--env-file ../../../../../Trader/docker/.env.<env>`). Env backups go to `/Users/stephen/.config/trader-backup/` (folder 700, files 600, size checked, source readable), **never** under `~/Documents` (files there can become dataless iCloud placeholders). Helpers print key names, statuses and counts only; comparisons of secrets print "same"/"different" from hashes, never values. **No secret on a command line** (it would sit in the transcript and the process list): infrastructure passwords (the PostgreSQL superuser, the Proxmox root SSH password, the NPM admin login) are read by a helper from where they are stored and passed through a mode-600 file in the agent's scratch folder (`sshpass -f <file>`, `--admin-dsn-file <file>`) or the helper's own environment, and the file is deleted right after use; never `sshpass -p <password>`.
- **Questrade token chains:** dev's chain is owned by `trader_dev.trader.api_credentials`; **never run `spikes/qt.py` or `spikes/s1_tokens.py`**, and nothing uses `QUESTRADE_REFRESH_TOKEN` in `.env.dev`. The prod chain is a separate Questrade app ("Trader") with its own token that Stephen creates; after `trader questrade-seed` it is owned by `trader.trader.api_credentials`, and the used token is blanked in `.env.prod`.
- **Simulation only:** Questrade is read only; every order is a `SimBroker` row; agents never call the QuestTrade MCP order tools (`create_order_instruction` and friends); no fake trading rows are written into the live run of `trader_dev` or `trader`.
- **Soak protection:** no deploy, migration, replay or infrastructure change that touches the Docker host's proxy between 09:15 and 16:30 ET on a session day unless urgent (D2); after every deploy do the catch-up (D3). Only the dev bot is used for build progress; the prod bot only ever talks from the prod container (one poller per bot).
- **LAN only:** prod gets the same NPM access list, Pi-hole local record and no published port as dev.
- **Times:** everything shown to Stephen is Mountain Time (America/Edmonton), labelled MT; schedules are ET.

## Review Focus (the five failure modes most likely in this phase)

1. **A wrong clean-day verdict:** a failed-then-retried job counted as a failure; a job caught up after its deadline counted as clean; a day with the container down (no rows at all) counted as clean; a missed 9:35 later fired with `--force` counted as fired; a slow but on-time 9:35 scan counted as late; a final day whose verdict flips on re-evaluation (the token check); a day still in progress reported as final. Expected: exactly D1. [T2 tests 1–7, T2 LIVE 1]
2. **Calendar and time-zone errors in deadlines:** the nightly keyed to Monday but run on Sunday, early-close deadlines (13:00), a holiday counted as a session, the DST change on 2026-11-01, Friday's weekly attribution, an MT/ET mix-up in the Telegram line. Expected: deadlines from the exchange calendar in ET, stored in UTC, shown in MT. [T2 tests 8–10]
3. **The soak tooling disturbing what it measures:** `soak-report` writing a `job_runs` row (it would then expect itself), building its plans through `runtime.plan_builder` (which creates a live run, ensures strategy defaults and writes relayed plan-problem `error` events for past days), sending the Telegram line twice, alerting through the relay, or leaking an error text with a secret. Expected: read-only apart from one deduplicated message and `soak-mark` event rows at level `info` (never relayed); error texts masked and capped. [T2 tests 11–14]
4. **Prod mixing with dev:** `PUBLIC_BASE_URL` left at the dev default (Telegram links to dev), the dev bot token, Questrade chain, encryption key, session secret or database reused, `.env.prod` world-readable or committed, a dirty or untagged tree deployed as prod. Expected: `prod_env.py check` refuses each, `deploy.sh prod` refuses a dirty or mismatched tree, T5 LIVE isolation checks pass. [T6 tests 1–9, 12–13; T5 LIVE 5–6]
5. **Deploy downtime silently skipping work:** a cron line that fell inside a deploy's recreate window never run, or a deploy in market hours. Expected: `deploy.sh` prints the window, `cron_gap.py` lists exactly the lines due in it, every listed job is re-run and confirmed. [T6 tests 10–11, 14; T3 procedure step 4; T5 LIVE 4]

---

### Task P6-T1: Live rechecks (S2, S4 live, S1 follow-up)

**Goal:** Close the Phase 0 spikes that needed market hours (promotion criterion 1): S2 quote freshness, S4 at a live 9:35, and the S1 follow-up (token chains independent). SPEC §17 S1, S2, S4; §7.1 Fill model note ("re-checked live in Phase 6"); spikes/README.

**Files:** `Trader/spikes/README.md` (results table and a new "Phase 6 live rechecks" section), `Trader/todo.md` (S1 follow-up, S2 checkboxes). No code. Scratch helpers stay in the scratch folder and are never committed.

**Interfaces (consumed):** `trader questrade-check [--symbol SPY]` (prints server-time skew, `last`, `bid`, `ask`, `delay`, `lastTradeTime`, `age_s`, rate-limit remaining); `job_runs` rows `event:orb_open` (their `started_at`/`finished_at` and `detail`); after T2, `trader soak-report --json` field `orb_open_seconds`.

**Behaviour and decisions:**
- Every Questrade call runs **inside the container** (`docker --context shared-docker-server exec trader-dev trader …`, or a scratch Python script piped to `docker --context shared-docker-server exec -i trader-dev python -`), so the DB-owned token chain is used by the processes that already share it. Never `spikes/qt.py`.
- S2 uses at most ~10 requests in all; nothing runs between 09:34 and 09:38 ET (the 9:35 scan and its 09:36 backup) or at 11:30.
- S4 live needs no extra load: the worker's own `event:orb_open` run already fetches the 9:30–9:35 bars for the whole universe, so its duration is the live measurement.
- A failing S2 (quotes delayed, or `lastTradeTime` routinely older than `stale_quote_seconds`) is an **escalation** (§5.4): the fill model depends on it; promotion waits.

**Acceptance (checklist):**
- [ ] 1. **S2 pre-market** (a session day, 08:30–09:00 ET = 06:30–07:00 MT): `questrade-check --symbol SPY` once, plus a scratch script that quotes SPY and the day's top pre-market gapper (from today's `premarket` detail) and prints `lastTradePrice`, `lastTradePriceTrHrs`, `lastTradeTime`, `delay` only. Record whether `lastTradePrice` carries the pre-market price (it differs from `lastTradePriceTrHrs`).
- [ ] 2. **S2 market hours** (a normal session day, 09:40–10:00 ET = 07:40–08:00 MT): `questrade-check --symbol SPY` five times about 3 minutes apart. Pass: `delay` 0 every time, |clock skew| ≤ 1 s, and `age_s` ≤ 2.0 in at least 4 of 5 samples.
- [ ] 3. **Universe freshness** (same window, one request): a scratch script quotes 20 symbols of today's universe (the 20 highest `rvol` names in today's `candidates`, else the first 20 by ticker) and prints only the median, 90th percentile and maximum `age_s` and how many are older than `stale_quote_seconds` (10). Recorded as information; if the 90th percentile exceeds 10 s, raise it to Stephen (a fill-model finding, not a blocker).
- [ ] 4. **S4 live:** for every soak day so far, the `event:orb_open` run's duration (`finished_at − started_at` of its succeeded row) and the universe size of that session. Pass: under 60 s on every day, with the universe ≥ 500 symbols, and no `timeout` missing-symbols count in its detail. Recorded again on the last soak day (from `soak-report --json`).
- [ ] 5. **S1 follow-up:** Telegram progress message to Stephen: "Is FinanceTracker's Questrade connection still working?" Default if no answer by the end of the soak: pass, on the evidence that the dev chain was refreshed daily by `token-refresh` and the worker for two weeks while FinanceTracker refreshed its own at 03:30 with no breakage reported. Dev ↔ prod independence is checked in T5 LIVE 14.
- [ ] 6. Update `spikes/README.md` (table: S2 → Pass/Fail with the numbers; S4 → "Pass (live: max N s over M days)"; a dated "Phase 6 live rechecks" section with every number above, times in MT) and `todo.md`; commit `P6-T1: live rechecks S2, S4, S1 follow-up` and push. SPEC §17's status cells are updated by T5.

---

### Task P6-T2: Soak report, soak marks and the daily Telegram line

**Goal:** One read-only command that computes, for each recent session, the clean-day verdict of decision D1, the running count of consecutive clean days and the earliest possible finish; a mark command for resets and outages; and a daily Telegram line through the container's bot. SPEC §15.1 criterion 2, §9 (new rows), §4.4 (Telegram), BRD O1 and reliability NFRs.

**Files:** see the file map (T2 rows).

**Interfaces (produce):**
- `trader/jobs/soak.py`:
  - `SoakVerdict = Literal["clean", "not_clean", "pending"]`
  - `CheckStatus = Literal["succeeded", "late", "failed", "missing", "missed", "pending"]`
  - `@dataclass(frozen=True) class ExpectedJob: job: str; deadline: datetime` (UTC)
  - `@dataclass(frozen=True) class JobRunRow: job: str; status: str; started_at: datetime; finished_at: datetime | None; error: str | None` (a plain copy of the `job_runs` columns it needs)
  - `@dataclass(frozen=True) class JobCheck: job: str; status: CheckStatus; deadline: datetime; attempts: int; finished_at: datetime | None; error: str | None` (`error` masked with `redact_text`, at most 200 characters)
  - `@dataclass(frozen=True) class SoakMark: session_date: date; kind: Literal["reset", "outage", "clear"]; reason: str; at: datetime`
  - `@dataclass(frozen=True) class SoakDay: session_date: date; verdict: SoakVerdict; checks: tuple[JobCheck, ...]; failed: tuple[str, ...]; orb_open: CheckStatus; orb_open_seconds: float | None; reset: bool; outage: str | None`
  - `@dataclass(frozen=True) class SoakReport: through: date; target: int; days: tuple[SoakDay, ...]; consecutive_clean: int; total_clean: int; last_final: date | None; earliest_finish: date | None; generated_at: datetime; env: Literal["dev", "prod"]`
  - `def expected_jobs(cal: SessionCalendar, session_date: date, plan: DayPlan, settings: RuntimeSettings, row_jobs: Collection[str], *, orb_required: bool = True) -> tuple[ExpectedJob, ...]` (pure; the D1 table)
  - `def evaluate_day(session_date: date, expected: Sequence[ExpectedJob], rows: Sequence[JobRunRow], now: datetime, *, token_failures: Sequence[datetime] = (), outage: str | None = None) -> SoakDay` (pure; `token_failures` are the `questrade.token` error event times in D's token window; the token check uses them and `rows` only, per D1)
  - `def build_report(days: Sequence[SoakDay], *, cal: SessionCalendar, through: date, target: int, now: datetime, env: str) -> SoakReport` (pure: count, finish date)
  - `def effective_marks(marks: Sequence[SoakMark]) -> dict[date, SoakMark]` (pure: the latest mark per date wins; `clear` removes it)
  - `@dataclass(frozen=True) class SoakDeps: factory: sessionmaker[Session]; clock: Clock; calendar: SessionCalendar; settings: Callable[[], RuntimeSettings]; plan: Callable[[date], DayPlan]; orb_enabled: Callable[[], bool]; notifier: Notifier; render: Renderer; env: Literal["dev", "prod"]`
  - `def load_report(deps: SoakDeps, *, through: date | None = None, sessions: int = 20, target: int = 10) -> SoakReport` (read-only DB access)
  - `async def notify_report(deps: SoakDeps, report: SoakReport, *, final: bool) -> bool` (sends at most one message; returns whether one was sent)
  - `def record_mark(factory, clock, run_id: int, kind: str, session_date: date, reason: str) -> None` (one `event_log` row)
  - Constants: `SOAK_SOURCE = "soak"`, `DEFAULT_TARGET = 10`, `DEFAULT_SESSIONS = 20`, `OUTAGE_MINUTES = 5`, `DEADLINES` (the D1 table as data).
- `trader/notify/types.py`: `MessageKind` gains `"soak"`; `@dataclass(frozen=True, slots=True) class SoakLineView: session_date: date; verdict: str; failed: tuple[str, ...]; orb_open_seconds: float | None; consecutive_clean: int; target: int; earliest_finish: date | None; changed: tuple[tuple[date, str], ...]; final: bool; env: str`; `Renderer.soak_line(view: SoakLineView) -> OutboundMessage`.
- `trader/notify/messages.py`: `MessageRenderer.soak_line` (pure; Telegram HTML, every dynamic string escaped; dedupe key `soak:<YYYY-MM-DD>` or `soak:<YYYY-MM-DD>:final`; `silent=True` when the verdict is clean or pending, sound when not clean).
- CLI (`trader/cli.py`):
  - `trader soak-report [--through YYYY-MM-DD] [--sessions N] [--target N] [--json] [--notify] [--final]` → a table (one line per session: date, verdict, failed checks, 9:35 seconds, marks) and a summary line, or with `--json` the object below. Exit 0 whether or not days are clean (a report is not a failure); exit 1 only when it cannot read the database; on a non-session day with `--notify` it prints "not a trading session, nothing to send" and exits 0.
  - `trader soak-mark {reset,outage,clear} --date YYYY-MM-DD --reason TEXT` → one `event_log` row (level `info`, source `soak`, `run_id` = the live run, `data = {"mark": kind, "session_date": "..."}`), prints `marked <kind> <date>`. The date must be a session in the calendar (exit 1 otherwise); the reason is 1–200 characters, masked.
- `--json` shape (contract for T3 and T5):
  `{"env": "dev", "generated_at": "...Z", "through": "2026-10-09", "target": 10, "consecutive_clean": 10, "total_clean": 10, "last_final": "2026-10-09", "earliest_finish": "2026-10-09", "days": [{"session_date": "2026-09-28", "verdict": "clean", "failed": [], "orb_open": "succeeded", "orb_open_seconds": 31.2, "reset": false, "outage": null, "checks": [{"job": "preopen", "status": "succeeded", "attempts": 1, "deadline": "...Z", "finished_at": "...Z", "error": null}]}]}`
- `docker/crontab`: `5 18 * * 1-5   trader soak-report --notify` and `30 10 * * 6    trader soak-report --notify --final`, with a header comment line.

**Interfaces (consume):** `trader.db.models.JobRun`, `EventLog`, `Notification`; `scheduler.live_day_plan(factory, registry, run_id, cal, session_date, settings)` and `scheduler.day_plan`, `runtime.active_live_run_id(factory)`, `StrategyRegistry(factory, clock).current("orb_sip").enabled` (read-only; a `KeyError` means no config yet → treat as enabled); `runtime.open_telegram`, `runtime.build_notifier`, `runtime.build_renderer`, `runtime.checkin_job_name`, `runtime.SESSION_END_JOB`, `runtime.TOKEN_SOURCE`; `engine.scheduler.event_job`, `MISSED_PREFIX`, `DayPlan`; `SessionCalendar` (`is_session`, `session_open`, `session_close`, previous/next session); `RuntimeSettings.scheduler_late_grace_seconds`, `scheduler_always_fire_late`; `engine.runs.get_live_run`; `logging_setup.redact_text`; `EnvSettings.app_env`.

**Behaviour and decisions:**
- **Read-only:** `soak-report` never goes through `run_job` and never writes `job_runs`, `settings`, `runs`, `strategy_configs` or anything else; its only writes are the `notifications` row of the one message the notifier sends (and, like every CLI command after P5-T17, a log-mirror `event_log` row if it logs an error line itself). It is not in `ManualJob`, the Dashboard timeline or the System page's job list (none of those change).
- **Plan (verifier fix):** `runtime.plan_builder` is **not** usable here: it calls `get_live_run` (which creates a live run when none is active), `StrategyRegistry.ensure_defaults()` and `report_plan_problems` (which writes relayed `error` events, so evaluating 20 past sessions could send Telegram alerts about old days). `SoakDeps.plan` is built from `scheduler.live_day_plan` with `runtime.active_live_run_id(factory)` (no active live run → `day_plan` of the enabled strategies with no exits-only ones) and the current `RuntimeSettings`; it never reports plan problems. The plan uses today's strategy configs for past days too (an approximation that can only drop events a since-disabled strategy scheduled; `orb_open` is covered by `orb_required`).
- **Window:** the last `--sessions` sessions up to `--through` (default: the latest session whose ET date ≤ today). The consecutive count is taken over the final days in order: `clean` +1, `not_clean` → 0, a `reset` mark sets it to 0 before its day is evaluated. It stops at the first `pending` day (shown as provisional, never counted). `last_final` is the last final day; `earliest_finish` is the session on which the count would reach the target if every later day is clean (`None` once reached).
- Days before the soak started (no container, e.g. Fri 2026-09-25) come out `not_clean` (missing jobs) and simply cap the count; no start date is needed.
- **Now vs deadlines:** a check whose deadline is in the future is `pending` unless it already succeeded. The weekly job makes the week's last session `pending` until Saturday 12:00 ET; the Friday 18:05 line says "weekly report due Sat" and the Saturday `--final` line settles it.
- **Telegram line** (`--notify`): for the evaluated session (`through`) it sends one line, e.g. `Soak Mon 28 Sep: clean ✅ · 9:35 scan 31 s · 1 clean day in a row (target 10) · earliest finish Fri 9 Oct`, or `Soak Tue 29 Sep: NOT clean ❌ preopen failed (TokenExpired…) · count 0 · earliest finish Tue 13 Oct`. It also names any earlier day in the window that has become final since the previous line with a verdict other than the one that line showed (`changed`, e.g. "Mon 28 Sep is now not clean: postclose failed"). `changed` is re-derived, not stored: `load_report` evaluates the window a second time with `now` = the `sent_at` of the latest `notifications` row whose `dedupe_key` starts with `soak:` and compares the two (no previous line → nothing changed). Dates are written as weekday, day and month; times are MT. With `env == "prod"` the prefix is `Ops` and the target and finish date are left out. When `orb_sip` is disabled the 9:35 part reads "9:35 scan off". Nothing is sent when Telegram is not configured (prints "Telegram not configured").
- **Dedupe:** `soak:<date>` for the weekday line and `soak:<date>:final` for Saturday's; a second cron run or a manual `--notify` for the same date sends nothing (the notifier's contract). `--final` on a weekday is allowed (manual use) and uses the `:final` key.
- **Marks:** `soak-mark` rows are `info` events, so the relay never sends them (it relays `error`/`critical` only) and the log mirror ignores them. `load_report` reads them from `event_log` (source `soak`), applies `effective_marks`, and an `outage` mark makes that day `not_clean` with the reason shown.
- **Masking:** every error text in the report, the JSON and the message goes through `redact_text` and is cut to 200 characters; `--json` never includes job `detail` payloads other than the orb-open seconds.
- **Expected set verification:** see D1; the builder adjusts `DEADLINES` only as D1 allows and notes it here.

**Acceptance tests** (pure unless marked; the builder writes them first):
- [ ] 1. A day where every expected job has one succeeded row before its deadline is `clean`; `failed` is empty; `orb_open_seconds` equals the succeeded `event:orb_open` row's duration. An `event:orb_open` row started 09:35:06 ET and finished 09:37:40 ET (a slow scan) is `succeeded` (events are judged by `started_at`); one started 09:37:10 ET is `late`.
- [ ] 2. `preopen` with a `failed` row then a `succeeded` row finished before 09:30 ET (a P5 retry) → `succeeded`, `attempts` 2, day `clean`. The same with the success at 09:31 ET → `late`, day `not_clean`.
- [ ] 3. A `succeeded` row before the deadline followed by a forced re-run that `failed` → still `succeeded` (any on-time success counts).
- [ ] 4. No rows at all for a session (container down) evaluated after its deadlines → every check `missing`, day `not_clean`; the same evaluated at 09:00 ET that day → `nightly` missing (deadline 08:00 passed), the rest `pending`, day `not_clean`.
- [ ] 5. `event:orb_open` failed with an error starting `missed:` and later a forced `succeeded` row at 09:50 → `missed`, day `not_clean`; no `event:orb_open` row and the plan has no `orb_open` while `orb_sip` is enabled → still expected, `missing`; with `orb_required=False` (orb_sip disabled) and no row → not in the expected set, the day can be `clean`, and the rendered line says "9:35 scan off".
- [ ] 6. A `running` row at the deadline → `failed` ("still running at the deadline"); a `failed` row before the deadline → `pending`, and the day verdict is `pending` while no other check has failed.
- [ ] 7. Token: an `error` event source `questrade.token` at 02:00 ET followed by a `premarket` row started 08:00 ET that `succeeded` → `token-refresh` `succeeded`; the same event with `premarket`, `event:orb_open` and `postclose` all failed (or started before the event) → `failed`; an event at 17:00 ET after a succeeded `postclose` → `failed`; no event → `succeeded`. Re-evaluating the same rows a week later gives the same verdict (no dependence on `now` once final).
- [ ] 8. Early close (Fri 2026-11-27, close 13:00 ET): safety events' deadline is 18:00Z, `checkin@13:30` with an "after close" success is `succeeded`, and Thanksgiving (2026-11-26) never appears in the window.
- [ ] 9. Deadlines across DST: `preopen` on 2026-10-30 is due by 13:30Z and on 2026-11-02 by 14:30Z; the Monday `nightly` (run Sunday 20:00 ET, keyed to Monday) is matched to Monday, not Sunday.
- [ ] 10. Weekly: on the week's last session (a Friday; a Thursday when Friday is a holiday) `weekly` is expected with the Saturday 12:00 ET deadline; the day is `pending` on Friday evening and `clean` on Saturday after a succeeded `weekly` row.
- [ ] 11. **(integration, testcontainers)** `load_report` over seeded `job_runs` and `event_log` rows gives the expected report; counting rows before and after `trader soak-report` (CliRunner) on a database with **no active live run** and a plan problem (a strategy key over 39 characters in a test plug-in, or a patched plan with `problems`): `job_runs`, `event_log`, `settings`, `runs`, `strategy_configs` unchanged (no live run created, no plan-problem events, no defaults ensured).
- [ ] 12. Count and finish: verdicts clean, clean, not_clean, clean, clean (and a pending last day) → `consecutive_clean` 2, `last_final` the 5th day, `earliest_finish` 8 sessions after it by the calendar (skipping weekends and holidays); a `reset` mark on the 4th day → 2 as well, starting at the 4th; an `outage` mark makes its day `not_clean` with the reason; `clear` undoes an earlier mark.
- [ ] 13. `--notify` twice for the same date (fake notifier with the real dedupe table in the integration test) → one message; the text is escaped HTML, times in MT, prefix `Soak` for dev and `Ops` for prod; a clean day's message is silent, a not-clean day's is not.
- [ ] 14. Masking: a `job_runs.error` containing a bearer token and a URL with `access_token=` appears redacted in the table, `--json` and the message; an error of 5,000 characters is cut to 200.
- [ ] 15. CLI: `soak-mark outage --date 2026-09-26` (a Saturday) → exit 1; `--reason ""` or a reason over 200 characters → exit 1 with "reason must be 1-200 characters", nothing written; a valid mark writes one `info` row with source `soak`, and one relay pump sends nothing for it.
- [ ] 16. Crontab: `tests/test_crontab.py` pins the two new lines (times in ET under `CRON_TZ=America/New_York`); `supercronic -test` still passes (existing test).
- [ ] 17. Gate and commit `P6-T2: soak report, soak marks and the daily Telegram line`; the SPEC §9 table gains the two rows (18:05 Mon–Fri / 16:05 MT "Soak/ops line", Sat 10:30 / 08:30 MT "final line for the week's last session") and master plan §7.1 gains a "Soak report" row (the `--json` shape, the `soak` kind, marks as `info` events).

**LIVE steps** (dev; outside 09:15–16:30 ET on a session day; D3 catch-up after the deploy):
1. **Before deploying**, against `trader_dev` read-only (`uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.dev trader soak-report --through <last session>` from the worktree): every job in the D1 table has rows for the soak days so far (09-28 onward); record any job whose rows never appear and apply D1's rule. The report for Mon 2026-09-28 matches what the orchestrator saw that day.
2. **Deploy** from a clean worktree at the pushed T2 commit (D5; sandbox workaround in the Global Constraints) with `TRADER_ENV_FILE="/Users/stephen/Documents/Code/Claude Code/Trader/Trader/docker/.env.dev" bash Trader/docker/deploy.sh dev` (if T6 has landed, its downtime stamps are printed; otherwise note the UTC times yourself just before and after) → health 200; run the D3 catch-up (before T6 lands: compare the window with `docker/crontab` by hand and run any skipped line with its `--date`); classify the deploy under D2 (expected: not a trading change, rule 2 exempts reporting lines) in the activity log.
3. `docker --context shared-docker-server exec trader-dev trader soak-report` → the table; `… --json` parses; `docker --context shared-docker-server exec trader-dev supercronic -test /app/docker/crontab` → valid.
4. `docker --context shared-docker-server exec trader-dev trader soak-report --notify` → one line arrives in Stephen's dev chat; running it again sends nothing. That day's 18:05 ET cron run then also sends nothing new for the same date (dedupe), and the next session's 18:05 ET line arrives on its own (the orchestrator records it, non-blocking).

---

### Task P6-T3: 10 clean trading days

**Goal:** Reach 10 consecutive clean trading days on `trader-dev` (SPEC §15.1 criterion 2), keep a daily ledger, fix what breaks without spoiling the count needlessly, and pass the full gate and the determinism test on the candidate commit (criterion 3).

**Files:** `Trader/docs/build/BUILD_STATE.md` only (orchestrator-owned; a new `## Soak ledger` section after the task board). No code; fixes are separate gauntlet tasks `P6-FIX<n>` (a Builder with the usual gauntlet, files as the fix needs).

**Interfaces (consumed):** `trader soak-report --json` and `trader soak-mark` (T2); `cron_gap.py` and `deploy.sh`'s downtime stamps (T6); `/api/health`; `worker_heartbeats`.

**Behaviour and decisions (the daily procedure, each session day):**
1. **Morning (optional, first day at least):** at 07:22 MT (09:22 ET) check `job_runs` `preopen` succeeded and the pre-open message arrived (P4-T19 LIVE 9 on Mon 2026-09-28). Record open items P4-T19 LIVE 9/10 there.
2. **After 16:05 MT** (18:05 ET): `docker --context shared-docker-server exec trader-dev trader soak-report --json` (or, before T2 is deployed, a read-only scratch query of `job_runs` applying D1 by hand). Append one ledger row: `| session | verdict | failed / missed | 9:35 s | count | deploys and class (D2) | notes |`, update the header ("Soak: N/10 clean, earliest finish <date>"), commit and push `BUILD_STATE.md`. When a day in the window changed verdict since the last row, correct it with a new row (the ledger is append-only; never edit old rows).
3. **Stephen's line** comes from the container's cron (D4). The orchestrator sends an extra dev-bot message only for a not-clean day (the cause and the planned fix) or at milestones (5/10, 10/10).
4. **Deploys during the soak:** only outside 09:15–16:30 ET on session days (urgent fixes excepted), always from a clean worktree at a pushed trunk commit (D5), recording the deployed version (`/api/meta`). After each: D3 catch-up with `cron_gap.py`, then the D2 classification in the ledger; a trading change → `trader soak-mark reset --date <first session on the new code> --reason "<commit>: <rule n>"`. A known outage → `soak-mark outage`.
5. **Not-clean day:** diagnose from `job_runs.error` and `event_log` (masked), open `P6-FIX<n>` with the finding, deploy the fix under rule 4; never re-run a job with `--force` just to turn a day clean after its deadline (D1 makes that `late` anyway).
6. **If no orchestrator session ran on a day,** the next session backfills a row for every missing day from the same report.
7. **P5-T18 carry-overs:** its LIVE 6 market-hours restart (on the first normal session day after its deploy, 10:30–11:15 ET or 12:00–13:15 ET) and LIVE 7 (first Saturday weekly) are recorded here; the restart does not make the day not clean (D1).
8. **Token keep-alive** is the container's 02:00 ET cron plus the worker. The master plan's start/resume `token-refresh` stays harmless (the chain is safe with concurrent refreshers, P1-T6) but is no longer needed; when a manual refresh is wanted, prefer `docker --context shared-docker-server exec trader-dev trader token-refresh`, so the refresh runs where the chain's other users are.

**Acceptance (checklist):**
- [ ] 1. `soak-report --json` on the last day shows `consecutive_clean ≥ 10` with `last_final` = that day (for a Friday, after Saturday's weekly report), and the ledger has a row for every session of the streak.
- [ ] 2. Every dev deploy during the streak is in the ledger with its downtime window, the catch-up commands run and their `job_runs` results, and its D2 class.
- [ ] 3. **Candidate:** the commit `trader-dev` ran on the last clean day (its `/api/meta` version) has run at least one full clean session; on a clean worktree at that commit `bash Trader/build/gate.sh` passes, including `tests/replay/test_golden.py` (criterion 3). Record the commit and the gate result.
- [ ] 4. The orchestrator sends Stephen "Soak complete: 10/10 clean (dates), candidate <commit>" on the dev bot.

---

### Task P6-T4: Stephen's manual end-to-end check (Stephen)

**Goal:** Stephen confirms one complete simulated day in the web app (SPEC §15.1 criterion 4): a signal, the approval, the fill, the protective stop, the exit (the stop being hit or the flatten at close − 10 min), and the journal entry.

**Files:** `BUILD_STATE.md` (orchestrator records the result).

**Behaviour and decisions:**
- Any soak day on which `orb_sip` makes an entry proposal qualifies. Stephen approves it on Telegram or the web within its TTL (5 minutes, around 07:35 MT) and answers the journal question after the close. If he misses a proposal, it expires; the next one serves.
- The check is on `https://trader-dev.sunspinner.ca`: Dashboard (the day's timeline and the position card), Trades → the trade's detail chain (signal evidence → proposal → decision → order → fill with its quote snapshot, the stop order, the exit), and Journal (the day's answer). Stephen replies "P6-T4 ok" (or lists what looked wrong) in chat or on Telegram.
- A trading-change deploy (D2) after the check means the check is repeated on the new code. If there is no entry signal for 5 soak days, the orchestrator tells Stephen (a strategy outcome, not a failure) and promotion waits for the first complete trade day (open question 8).
- This also closes P4-T19 LIVE 10 (a real button round trip through the deployed bot).

**Acceptance (checklist):**
- [ ] 1. The orchestrator sends Stephen, on the first soak day with a proposal, a dev-bot message with the trade date and the three pages to look at.
- [ ] 2. Stephen's confirmation is recorded in `BUILD_STATE.md` (date, trade id, anything he flagged; each flagged item becomes a `P6-FIX<n>` or an open question).

---

### Task P6-T6: Promotion tooling (`prod_env.py`, `cron_gap.py`, `deploy.sh` guards)

**Goal:** Make creating `.env.prod`, saving Stephen's secrets, creating the prod database, deploying a tagged build and catching up after downtime one-command, repeatable and secret-safe. SPEC §13, §15, §15.1 promotion steps 1–4; Global Constraints (secrets, backups).

**Files:** see the file map (T6 rows).

**Interfaces (produce):**
- `Trader/build/prod_env.py` (stdlib only except `create-db`, which needs psycopg and runs through `uv --directory Trader/app run python ../build/prod_env.py …`). Every subcommand takes `--env-file PATH` (default: `$TRADER_ENV_FILE`, else `Trader/docker/.env.prod` next to the script's `../docker/`) and `--dev-env-file PATH` (default the sibling `.env.dev`):
  - `init` → creates `.env.prod` (mode 600, atomic write) and refuses (exit 1, "already exists") if it exists. Keys: `DATABASE_URL=postgresql+psycopg://trader_app:<pw>@192.168.68.86:5432/trader`, `MIGRATION_DATABASE_URL=postgresql+psycopg://trader_owner:<pw>@192.168.68.86:5432/trader` (two different 32-character random letter-and-digit passwords), `APP_ENCRYPTION_KEY` (Fernet: URL-safe base64 of 32 random bytes), `SESSION_SECRET` (`secrets.token_urlsafe(48)`), `ADMIN_USERNAME=stephen`, `ADMIN_PASSWORD_INITIAL` (24 random letters and digits), `PUBLIC_BASE_URL=https://trader.sunspinner.ca`, `TZ_DISPLAY=America/Edmonton`, `TELEGRAM_CHAT_ID` copied from `.env.dev` (Stephen's user id is the same for every bot), and empty `TELEGRAM_BOT_TOKEN=`, `ANTHROPIC_API_KEY=`, `QUESTRADE_REFRESH_TOKEN=` with a comment "set with prod_env.py secret <KEY>". Then backs up (below). Prints only the key names written and the backup path.
  - `secret KEY` → `KEY` ∈ {`TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `QUESTRADE_REFRESH_TOKEN`}; reads the value with `getpass` (hidden, entered once), strips whitespace, checks the format (bot token `^\d{6,}:[A-Za-z0-9_-]{30,}$`; Anthropic `^sk-ant-[A-Za-z0-9_-]{20,}$`; Questrade `^[A-Za-z0-9_-]{20,}$`), refuses a value equal to the same key in `.env.dev` ("this is the dev value"), writes atomically (600), backs up, prints `saved KEY`. A bad format prints `KEY: not in the expected format; nothing saved` (never the value) and exits 1.
  - `unset KEY` → blanks one of those three keys (`KEY=`), backs up, prints `cleared KEY`.
  - `check` → exit 0 only if: every required key is set (`DATABASE_URL`, `MIGRATION_DATABASE_URL`, `APP_ENCRYPTION_KEY`, `SESSION_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_INITIAL`, `PUBLIC_BASE_URL`, `TELEGRAM_CHAT_ID`, `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`); `PUBLIC_BASE_URL` is exactly `https://trader.sunspinner.ca`; the database URLs name roles `trader_app`/`trader_owner`, host `192.168.68.86:5432` and database `trader`; each secret (`APP_ENCRYPTION_KEY`, `SESSION_SECRET`, `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, both URL passwords) differs from `.env.dev`'s (compared by SHA-256); the file mode is 600. Prints one line per key: `KEY ok` / `KEY empty (Stephen)` / `KEY SAME AS DEV` / `KEY wrong: <which rule>`, never a value. `--allow-empty KEY` (repeatable) lets T8 check before Stephen's secrets exist.
  - `copy-admin-password` → puts `ADMIN_PASSWORD_INITIAL` on the macOS clipboard (`pbcopy` via stdin), prints `copied` (Stephen's login step; nothing is shown).
  - `create-db --admin-dsn-file PATH [--dev-compare]` → connects as a PostgreSQL superuser whose DSN is read from a 600 file (never from the command line or the environment of a printed command) and, idempotently: creates or updates the roles `trader_owner` and `trader_app` (LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, passwords set from `.env.prod` on every run), creates database `trader` if missing (with the same owner and encoding as `trader_dev`), `REVOKE ALL ON DATABASE trader FROM PUBLIC`, `GRANT CONNECT, CREATE ON DATABASE trader TO trader_owner`, `GRANT CONNECT … TO trader_app`, then in `trader`: `CREATE SCHEMA IF NOT EXISTS trader AUTHORIZATION trader_owner`, `GRANT USAGE ON SCHEMA trader TO trader_app`, and `ALTER DEFAULT PRIVILEGES FOR ROLE trader_owner IN SCHEMA trader` granting `SELECT, INSERT, UPDATE, DELETE` on tables and `USAGE, SELECT, UPDATE` on sequences to `trader_app` (the same shape as `docker/smoke/init.sql`, which mirrors `trader_dev`), plus any role setting `trader_dev_owner`/`trader_dev_app` have (`rolconfig`, read with `--dev-compare`). Passwords are sent as **SCRAM-SHA-256 verifiers computed locally** (`hashlib.pbkdf2_hmac`, 4096 iterations, random 16-byte salt, the `SCRAM-SHA-256$<iter>:<salt>$<StoredKey>:<ServerKey>` format PostgreSQL accepts in `PASSWORD '…'`), quoted with `psycopg.sql.Literal`, so no plaintext password reaches the server's statement log either; no statement text with a password or verifier is ever logged or printed. The DSN file is refused (exit 1, nothing executed) unless it is mode 600 and outside the repository and outside `~/Documents`. Prints the step names (`role trader_owner: created|updated`, `database trader: created|exists`, …).
  - **Backup** (after every change): `/Users/stephen/.config/trader-backup/.env.prod.<YYYY-MM-DD>` (folder created with 700 and re-set to 700, file 600, byte copy, size equal to the source, source read first so a dataless placeholder fails with "source unreadable: nothing backed up", exit 1). Never a path under `~/Documents`. `TRADER_BACKUP_DIR` overrides the folder (tests only).
- `Trader/build/cron_gap.py --from ISO --to ISO [--crontab PATH] [--container NAME]`, run as `uv --directory Trader/app run python ../build/cron_gap.py …` (stdlib plus `trader.market.calendar.SessionCalendar` for the session dates; no database, no network): parses `Trader/docker/crontab` (the `CRON_TZ` line and five-field lines with numbers, `*`, ranges and lists in the day-of-week field; anything else → exit 2 naming the line), and prints one line per crontab entry whose fire time falls in `[from − 60 s, to + 60 s]` (both aware ISO datetimes, any offset): `<ET time> <MT time> docker --context shared-docker-server exec <container> trader <args> <date args>`, in time order; `nothing skipped` when none. **Date args (verifier fix):** the catch-up runs later than the fire, so each printed command pins the session the fire was for: `nightly` → `--date <calendar.next_session(fire's ET date)>`; `premarket`, `preopen`, `checkin`, `event`, `postclose` → `--date <fire's ET date>`, and when that date is not a session the line reads `<ET time> <MT time> not a session (<date>): nothing to run`; `weekly` → `--date <latest session ≤ fire's ET date>` (the week's last session, which selects the week just ended); `soak-report` → `--through <latest session ≤ fire's ET date>` (flags kept); `token-refresh` → no date. It adds `warning: the window overlaps 09:15-16:30 ET on a weekday` when it does. Commands the CLI doesn't know are printed unchanged with `(no date pinned)`.
- `Trader/docker/deploy.sh` changes:
  - Prints `deploy: down from <UTC ISO>` just before the compose recreate and `deploy: up at <UTC ISO>` when health returns 200, then `deploy: run uv --directory Trader/app run python ../build/cron_gap.py --from <down> --to <up> --container <trader-dev|trader>`. When the health wait fails it still prints the `down from` stamp and the hint (the catch-up is needed most then).
  - `prod` also refuses (exit 1, before any build) when the working tree is dirty (`git status --porcelain` not empty), when `git describe --exact-match --tags HEAD` is not `$TRADER_TAG`, or when `TRADER_TAG` is `dev`/`latest`; the messages name the reason. `dev` is unchanged apart from the stamps.

**Behaviour and decisions:**
- `prod_env.py` never prints, logs or passes a secret on a command line; errors name keys and rules only. It never touches `.env.dev` except to read it.
- `create-db` refuses to run against a database other than `trader`, and refuses role names other than the two above, so it can't touch `trader_dev` or FinanceTracker's databases.
- The PostgreSQL superuser for LIVE is the `stephen` login on `192.168.68.86` (memory: home-infra-layout; its password is in FinanceTracker's `.claude/settings.local.json`). T8 writes the DSN file (mode 600, in the agent's scratch folder under `/private/tmp`, never in the repo) with a small Python helper that reads the password from that file and prints nothing, uses it, and deletes it.

**Acceptance tests** (in `tests/build/` load the scripts by path with `importlib`; tmp paths and `TRADER_BACKUP_DIR` for files; no network):
- [ ] 1. `init` on an empty folder writes every key above, mode 600, two different DB passwords, a Fernet key that `cryptography.fernet.Fernet` accepts, `PUBLIC_BASE_URL=https://trader.sunspinner.ca`, `TELEGRAM_CHAT_ID` equal to the dev file's, and prints no value (captured stdout contains none of the generated values); a second `init` exits 1 and leaves the file byte-identical.
- [ ] 2. `secret TELEGRAM_BOT_TOKEN` with a patched `getpass` saves the value and prints `saved TELEGRAM_BOT_TOKEN`; a malformed value or the dev file's value is refused with exit 1, the file unchanged, and the value not in the output.
- [ ] 3. `unset QUESTRADE_REFRESH_TOKEN` blanks it; other keys unchanged; order and comments kept.
- [ ] 4. `check` passes on a complete file; fails naming `PUBLIC_BASE_URL wrong` for the dev URL, `SESSION_SECRET SAME AS DEV` when copied from dev, `TELEGRAM_BOT_TOKEN empty (Stephen)` when blank (exit 0 with `--allow-empty TELEGRAM_BOT_TOKEN`), `DATABASE_URL wrong` for database `trader_dev`, and a mode-644 file; no value appears in any output.
- [ ] 5. Backup: after `init`/`secret`/`unset` the backup exists with mode 600 in a 700 folder and equals the source byte for byte; an unreadable source → exit 1 and no backup; a backup folder under a path containing `/Documents/` is refused.
- [ ] 6. `copy-admin-password` pipes the password to a patched `pbcopy` and prints only `copied`.
- [ ] 7. **(integration, testcontainers)** `create-db` twice against a throwaway PostgreSQL 14 superuser: both runs succeed (the second reports `updated`/`exists`); as `trader_owner` a table can be created in schema `trader`; as `trader_app` rows can be inserted and read but `CREATE TABLE` in `trader` fails and database `trader` rejects `PUBLIC` connections by other roles; the statement log captured by the test contains no password.
- [ ] 8. `create-db` refuses a `.env.prod` whose URL names database `trader_dev` or role `trader_dev_app` (exit 1, nothing executed).
- [ ] 9. The DSN file must be mode 600 (a 644 file is refused) and outside the repository and `~/Documents` (a 600 file under the repo root is refused); its content never appears in output or exceptions. In test 7 the roles log in with the `.env.prod` passwords although only SCRAM verifiers were sent (the captured statements contain `SCRAM-SHA-256$` and never the plaintext).
- [ ] 10. `cron_gap.py` with the real `docker/crontab` (assert the listed lines are present, not the exact full output, since T2 adds lines in parallel): a window 2026-10-05 19:58–20:03 EDT lists `trader nightly --date 2026-10-06`; a Wednesday 2026-11-25 19:58–20:03 EST window lists `trader nightly --date 2026-11-27` (Thanksgiving skipped); 2026-10-05 09:30–09:40 EDT lists `trader event orb_open --date 2026-10-05` and prints the market-hours warning; a Saturday 2026-10-03 08:55–09:05 EDT window lists `trader weekly --date 2026-10-02` (the P5-T17 line); 2026-11-02 (after DST) 09:19–09:21 EST lists `trader preopen --date 2026-11-02` at 09:20 ET = 14:20Z; a 2026-11-26 07:55–08:05 EST window prints `not a session (2026-11-26)` for `premarket`; a window from 2026-10-05 20:30 EDT to 2026-10-06 02:30 EDT lists `token-refresh` without a date; an empty window prints `nothing skipped`.
- [ ] 11. `cron_gap.py` refuses an unsupported crontab line (e.g. `*/5 * * * *` or a named month) with exit 2 naming it.
- [ ] 12. `deploy.sh prod` (stubbed `docker`, `ssh`, `curl` and `git` on PATH, as the existing deploy tests do): a dirty tree → exit 1 "working tree is dirty", no build; HEAD not at the tag → exit 1 naming both; `TRADER_TAG=dev` → exit 1; a clean tree at the tag → builds `trader:<tag>` and succeeds. The existing prod tests are updated to stub `git` accordingly.
- [ ] 13. `deploy.sh dev` still behaves as before (existing tests) and now prints `deploy: down from …` before the recreate and `deploy: up at …` after the 200, in UTC ISO format, plus the `cron_gap.py` hint naming `trader-dev`.
- [ ] 14. The timestamps printed by `deploy.sh` are accepted by `cron_gap.py --from/--to` (a test feeds one to the other).
- [ ] 15. Gate and commit `P6-T6: promotion tooling (prod_env, cron_gap, deploy guards)`.

**LIVE step:** none in this task (T8 and T5 use the tools). A dry check: `uv --directory Trader/app run python ../build/cron_gap.py --from 2026-09-28T19:55:00-04:00 --to 2026-09-28T20:05:00-04:00 --container trader-dev` → lists `trader nightly --date 2026-09-29`.

---

### Task P6-T8: Prod infrastructure prep

**Goal:** Everything prod needs that doesn't depend on the soaked code, done during the soak so promotion day is short: database and roles, `.env.prod` with random secrets and its backup, the Pi-hole record, the NPM proxy host and certificate, and a capacity check of the Docker host. SPEC §14, §15, §15.1 promotion steps 2, 3 and 5.

**Files:** `Trader/docker/.env.prod` (git-ignored; created by `prod_env.py init`), `/Users/stephen/.config/trader-backup/.env.prod.<date>`; records in the activity log. No code.

**Behaviour and decisions:**
- All steps outside 09:15–16:30 ET on session days (NPM and Pi-hole are shared with `trader-dev`).
- Access follows the memory note home-infra-layout: PostgreSQL superuser `stephen` on `192.168.68.86` (password from FinanceTracker's `.claude/settings.local.json`); Pi-hole v6 in LXC 102 via the Proxmox host (`sshpass -f <600 scratch file> ssh root@192.168.68.89 pct exec 102 -- pihole-FTL --config dns.hosts '[…]'`, setting the whole array and keeping every existing entry; read the current array first and print only the host names; never `sshpass -p`, see the Global Constraints); NPM API on `http://192.168.68.73:81/api` with the admin login used for `trader-dev` on 2026-09-26, driven by a scratch Python helper that reads the login from where it is stored and keeps the API token in memory (it prints ids, domains and statuses only). If any of these can't be reached, escalate that one item (Stephen: a 3-minute UI step) and carry on with the rest.
- The NPM proxy host **clones proxy host 4** (`trader-dev.sunspinner.ca`): domain `trader.sunspinner.ca`, forward `http://trader:8000`, websockets on, block exploits on, access list 1 "Home LAN only" (`192.168.68.0/22`), the same advanced config (SSE must not be buffered; P4-T19 measured < 1 s through NPM), Force SSL and HTTP/2 on. Certificate: a new Let's Encrypt certificate for `trader.sunspinner.ca` by Cloudflare DNS challenge with the same zone-scoped token certificate 7 uses (NPM stores it in certificate 7's `meta.dns_provider_credentials`: the helper copies that field into the new certificate request in memory and **never prints or logs** certificate 7's `meta`; if the API does not expose it, Stephen adds the certificate in the NPM UI choosing the saved Cloudflare credentials — escalation with the default "carry on, promotion needs it only on the day").
- Pi-hole: add `192.168.68.73 trader.sunspinner.ca`; keep `trader-dev.sunspinner.ca` and all other records.

**Acceptance (checklist):**
- [ ] 1. **Read dev's database setup** (read only, dev app role): schema owner, role attributes and `rolconfig` of `trader_dev_owner`/`trader_dev_app`, database owner and encoding of `trader_dev`; record them (no secrets).
- [ ] 2. `python3 Trader/build/prod_env.py init --env-file "/Users/stephen/Documents/Code/Claude Code/Trader/Trader/docker/.env.prod"` → key names and the backup path; `git status` in the main checkout does not list `.env.prod` (git-ignored: `git check-ignore` confirms).
- [ ] 3. `uv --directory Trader/app run python ../build/prod_env.py create-db --admin-dsn-file <scratch 600 file> --dev-compare --env-file <abs .env.prod>` → step names; the DSN file deleted afterwards. As `trader_app` (URL from `.env.prod`, via a helper that prints only results): connect OK, `CREATE TABLE trader.x` refused; as `trader_owner`: `alembic upgrade head` is **not** run here (the container's entrypoint does it on first start). If `pg_hba.conf` refuses the new database, escalate (it lists `192.168.68.0/24`, which covers .73 and the Mac).
- [ ] 4. `prod_env.py check --allow-empty TELEGRAM_BOT_TOKEN --allow-empty ANTHROPIC_API_KEY` → every other key ok and different from dev.
- [ ] 5. Pi-hole: `dig +short trader.sunspinner.ca @192.168.68.84` → `192.168.68.73`; `dig +short trader-dev.sunspinner.ca @192.168.68.84` still → `192.168.68.73`.
- [ ] 6. NPM: the new proxy host exists with the access list, websockets, SSL forced and a valid certificate (`curl -s -o /dev/null -w "%{http_code}" https://trader.sunspinner.ca/api/health` → `502` with no TLS error, since the container doesn't exist yet); `https://trader-dev.sunspinner.ca/api/health` still `200`.
- [ ] 7. Docker host capacity: `docker --context shared-docker-server info` total memory and `docker --context shared-docker-server stats --no-stream` show at least 1.5 GiB free for a second 1 GiB container (else escalate before promotion); `docker --context shared-docker-server network inspect proxy` subnet still `172.19.0.0/16` (else set `TRADER_FORWARDED_ALLOW_IPS` in `.env.prod`).
- [ ] 8. Postgres backups: the `trader` database is on the same VM 105, which is backed up whole (SPEC §15 "VERIFY" item closed; record it).
- [ ] 9. Tell Stephen (dev bot) that `.env.prod` is ready for his two secrets (P6-T7 step 3).

---

### Task P6-T7: Stephen's prod credentials (Stephen)

**Goal:** The three things only Stephen can create (SPEC §15.1: separate bot, separate Questrade app, separate Anthropic key), saved without any secret passing through chat.

**Stephen's steps** (sent to him on Telegram and in the orchestrator's report as soon as P6-T0 is accepted; steps 1, 2 and 4a any time during the soak, step 3 after T8 step 2, step 4b on promotion day):
1. **Prod Telegram bot:** in Telegram open @BotFather → `/newbot` → name `Trader`, username e.g. `StephenTraderBot` → copy the token. Then open the new bot and press **Start** (so it may message you).
2. **Prod Anthropic key:** Anthropic Console → the "Trader" workspace → API keys → create a key named `trader` → copy it. (Optional: a monthly spend limit on the workspace.)
3. **Save both** (after the orchestrator says `.env.prod` is ready): in a Terminal window (the hidden prompt needs a real terminal), `cd "$HOME/Documents/Code/Claude Code/Trader"`, then `python3 Trader/build/prod_env.py secret TELEGRAM_BOT_TOKEN` and paste the bot token at the hidden prompt; then `python3 Trader/build/prod_env.py secret ANTHROPIC_API_KEY` and paste the key. Each prints only `saved …`. Never paste them into the chat.
4. **Questrade app "Trader":** (a) now: Questrade API Centre → register a new personal app named `Trader` (not `Trader-dev`, not FinanceTracker's). (b) On promotion day, when the orchestrator asks: generate that app's manual refresh token and, in the same Terminal folder, run `python3 Trader/build/prod_env.py secret QUESTRADE_REFRESH_TOKEN` and paste it at the hidden prompt. The orchestrator deploys and seeds it within minutes (a manual token is single use).
5. **After prod is up:** open `https://trader.sunspinner.ca` at home, run `python3 Trader/build/prod_env.py copy-admin-password` in that Terminal and paste the password into the login (user `stephen`), then set your own password under Settings → Security (at least 8 characters). Optional: turn on two-step codes.

**Acceptance (checklist):**
- [ ] 1. `prod_env.py check` shows `TELEGRAM_BOT_TOKEN ok` and `ANTHROPIC_API_KEY ok` (set, well formed, different from dev).
- [ ] 2. Bot works: `uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.prod trader telegram-test` (from a worktree; it sends one test message and never polls) → `sent message <id>`, and Stephen sees it from the new bot.
- [ ] 3. Key works: a scratch helper run with the same `--env-file` calls Anthropic's free `GET /v1/models` with the key from the environment and prints only the HTTP status → `200`.
- [ ] 4. Stephen confirms the Questrade app `Trader` exists (step 4a).

---

### Task P6-T5: Promote to prod

**Goal:** Create prod on the soaked code and start a fresh live run, in simulation, LAN-only, fully separate from dev (SPEC §15.1 promotion steps 1–6), then confirm the first prod trading day.

**Files:** `Trader/docker/.env.prod` (the used Questrade token blanked), `Trader/docs/SPEC.md` (§15.1: a dated "Promoted" line with the tag; §17: S2/S4 status cells from T1), master plan §7.1 Fill model row (the live recheck result), `BUILD_STATE.md` (orchestrator). No code.

**Behaviour and decisions:**
- **When:** the first weekend after T3 finishes (Sat 2026-10-10 at the earliest, after the 08:30 MT final soak line), or a weekday after 16:30 ET; never 09:15–16:30 ET on a session day. The first prod job is then Sunday's 20:00 ET nightly.
- **Promotion is blocked** only by criteria 1–4 (T1, T3, T4) and the credentials (T7); everything else was prepared in T8.
- **Settings carry-over (open question 5):** dev's settings rows and strategy configs that differ from the defaults are copied to prod before the first session, through the prod API (`PUT /api/settings/{key}`, `PUT /api/strategies/{key}`; validated and audited) by a scratch helper that logs in with the prod credentials from the environment and prints key names only.
- **Dev after promotion (open question 4):** keep `trader-dev` running (token chain, cron, tests of future changes) but make it quiet before prod's first session: disable `orb_sip` and `spy_overlay` in dev (no 9:35 scan competing with prod for Questrade's rate limit, whose per-app or per-login scope is still unknown; no approval prompts on Stephen's phone) and set dev's `claude.daily_budget_usd` to 0 (no duplicate Claude spend). Done through the dev API, audited. With `orb_sip` disabled dev's daily soak line no longer expects the 9:35 event (D1) and reads "9:35 scan off", so it stays a quiet health line for dev's remaining jobs.

**LIVE steps:**
1. **Pre-checks:** T1, T3, T4, T7 (steps 1–4a), T8 done; `trader-dev` healthy; no open position or working order in dev's live run; the current time is outside 09:15–16:30 ET on a session day.
2. **Tag:** `git tag -a v1.0.0 <candidate commit from T3> -m "Trader 1.0.0: promoted after the Phase 6 soak"` and `git push origin v1.0.0`. Create a clean worktree at the tag (`git worktree add <scratch>/v1.0.0 v1.0.0`); if T3's gate ran on the same commit, reuse its result, else run `bash Trader/build/gate.sh` there → pass (criterion 3).
3. **Questrade token:** ask Stephen (dev bot) for T7 step 4b; wait for his "saved"; `prod_env.py check` → all ok.
4. **Deploy:** from the tag worktree, `TRADER_TAG=v1.0.0 TRADER_ENV_FILE="/Users/stephen/Documents/Code/Claude Code/Trader/Trader/docker/.env.prod" bash Trader/docker/deploy.sh prod` → the prod guards pass, image `trader:v1.0.0` built and shipped, `trader` recreated, `200` from `https://trader.sunspinner.ca/api/health` within 120 s (`degraded` until the worker's first beat, `ok` within 60 s more). Run `uv --directory Trader/app run python ../build/cron_gap.py` for the printed window with `--container trader` (a first deploy has nothing to catch up unless it crossed a cron line: run what it lists, after step 5's seed if it needs Questrade).
5. **Seed and isolation:** `docker --context shared-docker-server exec trader trader questrade-seed` → `seeded`; `docker --context shared-docker-server exec trader trader questrade-check` → server time, a SPY quote, rate limits; `python3 Trader/build/prod_env.py unset QUESTRADE_REFRESH_TOKEN --env-file <abs .env.prod>` (the used token leaves the file; the container keeps a dead copy until its next recreate, harmless). Then a scratch helper piped to `docker … exec -i trader python -` prints only: `APP_ENV` = `prod`, the `PUBLIC_BASE_URL` host = `trader.sunspinner.ca`, the database name = `trader`, and "different" for the SHA-256 of the bot token, encryption key and session secret compared with the same values read from `trader-dev` (each container hashes its own and prints the first 12 hex characters only).
6. **Processes and schema:** `docker --context shared-docker-server exec trader supervisorctl -c /app/docker/supervisord.conf status` → `api`, `worker`, `cron` RUNNING; `id -u` → 10001; writing `/app/x` fails (read-only; check it with `docker … exec trader python -c "open('/app/x','w')"` → `OSError`, since the hook refuses `sh -c`); `/api/meta` version `v1.0.0`; `GET /api/system` (via the login helper) shows the Alembic revision at head and a fresh heartbeat; the cron check of P4-T19 LIVE 5 on `trader` → next runs in EDT/EST as expected.
7. **Fresh live run:** exactly one `runs` row, `mode = live`, `status = active`, started at this deploy, with its sim account at the carried-over starting cash; no signals, proposals, orders, fills or trades yet. (A new database guarantees it; dev results stay in `trader_dev` for comparison.)
8. **Settings carry-over and dev quieting** (Behaviour): apply, then list the carried keys in the activity log (names and values of non-secret settings only).
9. **Web through NPM:** deep links `/dashboard?proposal=1`, `/trades?position=1`, `/journal?date=<last Friday>`, `/system`, `/reports?week=<this Friday>` → 200; `/api/dashboard` without a cookie → 401; SSE latency helper (P4-T19 LIVE 6, prod credentials from the environment) → under 2000 ms; `SMOKE_MODE=live SMOKE_BASE_URL=https://trader.sunspinner.ca uv --directory Trader/app run --env-file ../../../../../Trader/docker/.env.prod npm --prefix ../web run e2e -- tests/smoke.spec.ts` → passes (read only).
10. **Telegram:** `docker --context shared-docker-server exec trader trader telegram-test --buttons` → a message from the prod bot (its heading says "Trader test message (dev)": `runtime.telegram_test_message` hard-codes the label; cosmetic, not a failure, recorded as a follow-up); ask Stephen to tap a test button (the prod bot answers `Invalid button`; non-blocking). Only the prod container polls the prod bot; the dev container keeps polling the dev bot.
11. **Admin login:** tell Stephen (dev bot, and in the report) that prod is live, where the initial password is (`ADMIN_PASSWORD_INITIAL` in `.env.prod`, copy with `prod_env.py copy-admin-password`), and to change it (T7 step 5). Never send the password.
12. **Backup:** `/Users/stephen/.config/trader-backup/.env.prod.<date>` is current (folder 700, file 600).
13. **Records:** SPEC §15.1 "Promoted <date> as v1.0.0", SPEC §17 S2/S4 statuses from T1, master plan §7.1 Fill model row ("quote staleness re-checked live in Phase 6: …"); commit `P6-T5: promoted to prod as v1.0.0 (records)` and push.
14. **First prod trading day** (Mon 2026-10-12 if promoted that weekend; non-blocking, the orchestrator records it): Sunday 20:00 ET `nightly` and 08:00 ET `premarket` succeeded in `trader`; the 07:20 MT pre-open message arrives from the **prod** bot with every check OK; `event:orb_open` fired in under 60 s; after 16:05 MT `docker … exec trader trader soak-report` shows the day `clean` and the prod "Ops" line arrived. **Chain independence:** that night's 02:00 ET `token-refresh` succeeded in **both** `trader` and `trader-dev` (dev's chain is untouched by the prod app), and FinanceTracker's connection still works (Stephen, as in T1 step 5). Phase 6 ends when this is recorded; then the orchestrator sends Stephen "Phase 6 complete: prod live as v1.0.0".

**Acceptance (checklist):** LIVE steps 1–13 ticked with their results in the activity log; step 14 recorded (it may complete after the task is accepted).

---

## Calendar

- **Soak start:** Mon 2026-09-28 (the first session after the P4-T19 deploy of `trader-dev` on Sun 2026-09-27 16:08 MT; Sunday's 18:00 MT nightly was the container's first job).
- **US market holidays in range:** none between 2026-09-28 and 2026-11-25 (Columbus Day Mon 2026-10-12 and Veterans Day Wed 2026-11-11 are normal NYSE sessions). Thanksgiving Thu 2026-11-26 is closed and Fri 2026-11-27 closes early at 13:00 ET; DST ends Sun 2026-11-01.
- **Earliest finish (no failures):** the 10 sessions are Sep 28, 29, 30, Oct 1, 2, 5, 6, 7, 8, 9. Day 10 is **Fri 2026-10-09**; its verdict is final after Saturday's weekly report (the `--final` line at **Sat 2026-10-10 08:30 MT**).
- **Earliest promotion:** Sat 2026-10-10 (or Sun 2026-10-11), given T1, T4, T7 and T8 are done by then. **First prod session:** Mon 2026-10-12.
- **If a day is not clean,** the count restarts the next session and the finish moves to the 10th session after the failed one: a failure on Tue Sep 29 → finish Tue Oct 13; on Fri Oct 2 → Fri Oct 16; on Fri Oct 9 → Fri Oct 23. Promotion then moves to the following weekend (or a weekday evening, at Stephen's choice).
- **Stephen's time-bound moments:** approving a real proposal around 07:35 MT on some soak day (T4); T7 step 4b (about 5 minutes) on promotion day.

## Contract refinements (§7.1)

- **New row "Soak report" (P6-T2):** `trader soak-report [--through] [--sessions] [--target] [--json] [--notify] [--final]` (read only; never a `job_runs` row), `trader soak-mark {reset,outage,clear} --date --reason` (an `info` `event_log` row, source `soak`, never relayed); the `--json` shape above; the clean-day rule D1 and deadlines table; cron `5 18 * * 1-5` and `30 10 * * 6`.
- **Notifier:** `MessageKind` gains `"soak"`; `Renderer.soak_line(SoakLineView)`; dedupe keys `soak:<date>` and `soak:<date>:final`.
- **Fill model** row: the "re-checked live in Phase 6" note is replaced by T1's result (T5 step 13).
- No existing contract changes shape.

## Open questions for Stephen (defaults chosen; nothing waits on them)

1. **Clean day with retries or catch-ups:** a job that failed and then succeeded (automatic retry or a manual run after a deploy) before its deadline counts as clean; after the deadline it doesn't; a missed 9:35 is never clean. Default: D1 as written.
2. **Redeploys during the soak** reset the count only for a trading change (D2's three objective rules); the day of an outage is not clean. Default: D2.
3. **Daily line in prod:** keep the 16:05 MT line in prod too, labelled "Ops", silent when clean. Default: yes (one line per trading day; say if you'd rather have it only on bad days).
4. **Dev after promotion:** keep `trader-dev` running but quiet (strategies off, Claude budget 0) so it doesn't compete with prod for Questrade's rate limit or ask you for approvals; turn them on when testing a change. Default: yes.
5. **Settings in prod:** start prod with dev's current settings and strategy configs (the ones soaked), not the factory defaults. Default: carry them over; the list goes in the promotion report.
6. **Version name:** `v1.0.0` for the first prod tag. Default: yes.
7. **Promotion day:** the first weekend after the soak (Sat 2026-10-10 at the earliest) so a full quiet day follows before the first prod session. Default: yes; a weekday evening after 16:30 ET also works if you prefer.
8. **No live trade by the end of the soak** (no ORB candidate passes for many days): promotion waits for the first complete trade day in dev for your manual check (criterion 4 can't be met by a replay, which has no approvals). Default: wait; the soak itself doesn't stop.
9. **Shell access to the container = database-owner access** (one env file for migrations and the app, P4-T19 note): only you can reach the Docker host, so keep it that way in prod. Default: keep; splitting into a migrate-only container is a later change.
10. **Prod stays LAN-only** (NPM home-LAN access list, Pi-hole name, no public DNS), like dev; remote use is through Telegram. Default: yes (SPEC §14).

## Self-review (done by the plan author)

- **Coverage:** SPEC §15.1 criteria 1 (T1), 2 (T2, T3), 3 (T3 step 3, T5 step 2), 4 (T4); promotion steps 1 tag (T5 LIVE 2), 2 database and roles (T6 `create-db`, T8), 3 `.env.prod` (T6 `init`/`secret`, T7, T8), 4 `deploy.sh prod` (T6 guards, T5 LIVE 4), 5 NPM and Pi-hole (T8), 6 fresh live run (T5 LIVE 7); §15 health wait (T5 LIVE 4), backups VERIFY item (T8 step 8); §14 LAN only (T8, D6); §9 (T2 rows); §17 S2/S4 (T1); §16 determinism on the promoted tag (T3, T5); BRD reliability NFRs and O1 (D1, T2, T3 procedure 7); Phase 5 notes for the P6 planner (latest-row rule reconciled in D1, replay runs ignored, golden test on the tag, weekly report in the soak); P4-T19 open LIVE 9 and 10 (T3 procedure 1, T4); P5-T18 LIVE 6 and 7 (T3 procedure 7).
- **Concurrency:** two builders (T2, T6) with disjoint files; `SPEC.md` and the master plan are edited by T2 and later by T5 only after T2 is accepted, never at the same time; `BUILD_STATE.md` stays orchestrator-owned.
- **No implementation code:** interfaces are signatures and data shapes; behaviour and tests are in words; LIVE steps give the exact commands.

## Verifier + spec review (P6-T0 attempt 1, 2026-09-27 MT): changes made

Checked against SPEC §9, §14, §15, §15.1, §16, §17, BRD reliability NFRs, spikes/README, the P4-T19 and P5-T15/T17/T18 LIVE steps, and trunk (e798889): `docker/crontab`, `trader/cli.py`, `trader/runtime.py`, `trader/worker.py`, `trader/engine/scheduler.py`, `trader/notify/types.py`, `trader/api/routers/{system,meta}.py`, `docker/deploy.sh`, `docker/entrypoint.sh`, the compose files. Calendar confirmed (no NYSE holiday 2026-09-28 to 2026-11-25; Columbus Day and Veterans Day are sessions; Thanksgiving closed, 11-27 early close; the 10-session dates and the slip dates are right). Fixed in place:
- **Read-only soak report:** `runtime.plan_builder` creates a live run, ensures strategy defaults and writes relayed plan-problem events, so T2 now builds plans from `scheduler.live_day_plan` with `active_live_run_id` (T2 "Plan", test 11, Review Focus 3).
- **Token check:** `api_credentials.last_refresh_at` is only the latest value, so the old rule would flip a past clean day to not clean on re-evaluation; the check now uses a later successful Questrade-dependent job (D1, test 7).
- **Event deadlines by `started_at`:** a slow but on-time 9:35 scan (up to 60 s) finished after the 09:37:05 grace would have counted as late (D1, test 1).
- **`premarket` deadline** 09:30 ET instead of 09:20 (its retries stop starting at 09:18, and an attempt started then can finish after 09:20); retry deadlines (preopen 09:28, premarket 09:18) stated in D1.
- **`orb_open` required only while `orb_sip` is enabled**, so dev after promotion (strategies off, T5) doesn't send a loud "NOT clean" line every day (D1, test 5, T5).
- **Catch-up dates:** `cron_gap.py` now prints `--date` (or `--through`) for every session-keyed command, using the exchange calendar; without it `trader nightly` after midnight targets the wrong session and the day jobs use today's date (D3, T6 test 10).
- **D2:** rule 2 no longer makes T2's own crontab lines (or P5-T17's weekly line) a trading change; `logging.*` and `postclose.*` are non-trading groups; nightly, premarket, the catalyst classifier and `trader/market/` are decision-path code; the P5-T18 deploy's classification is spelled out.
- **Candidate identity:** soak deploys come from a clean worktree at a pushed commit, since `/api/meta` is `git describe --dirty` (D5, T3 step 4, T2 LIVE 2).
- **Security:** no secret on a command line (`sshpass -f` with a 600 scratch file, never `-p`; NPM certificate credentials copied in memory, never printed); `create-db` sends SCRAM verifiers, not plaintext, and refuses a DSN file in the repo or `~/Documents` (Global Constraints, T6, T8).
- **Ownership:** T2 and T6 wait for P5-T17 **accepted** (not only verified) and T2 also for the P5 notify group; T2 owns `tests/fakes_telegram.py` (the protocol-member contract test needs `FakeRenderer.soak_line`); a "re-check trunk names after P5-T17" constraint.
- **Deploy mechanics:** the P4-T19 sandbox `DOCKER_CONFIG` workaround and the `sh -c` hook rule are in the Global Constraints; `docker network inspect` gets its context; the prod telegram-test's "(dev)" label is noted as cosmetic.
