# Live Dashboard and Control page (design spec)

Status: approved in conversation by Stephen, 2026-09-28 (approach 1, layout "try it, adjust later"). This spec is the input to the implementation plan.

## 1. Purpose

The web app's main page becomes a **live monitor**: what is being bought and sold, open positions and their gains, the account's performance today / this week / since the start, and what trading and AI cost. Everything that changes the engine moves to a separate **Control** page. The beebots.tech screenshot Stephen shared is the model for layout and content, not colours.

Success means Stephen can answer, from one screen on an iPad during the session:
- what we hold right now, how each position is doing, and how close each is to its stop or target;
- how much we made or lost today, this week and since the start, before and after AI costs;
- what the engine did today (scan, entries, exits, alerts) and why candidates were rejected;
- whether anything is unhealthy (stale prices, kill switches, failed jobs, Questrade 429s).

## 2. Decisions (from the conversation)

| # | Decision |
|---|---|
| D1 | Separate **Control** page for everything that changes the engine; the Dashboard is read-only. |
| D2 | Design for up to **20 open positions** (future strategy cap): compact sortable rows with sparklines; a full chart opens on tap (up to 3 at once). |
| D3 | Gain periods: **today, this week, since the start** — realised + unrealised, win rate, trade count; expectancy in R for the run. Longer/custom ranges stay on Reports. |
| D4 | Costs: trading P&L after fees is the headline; beside it fees, Claude spend and **net after AI** per period, and today's Claude spend against its daily cap. |
| D5 | One **activity feed** with filter chips (trades, proposals, alerts, scan) plus a **"Rejected today, and why"** panel (counts by rule, tap for tickers). Full per-candidate detail stays on Reports → Day. |
| D6 | Control page = daily operation (approval mode, pause, kill switches, strategies on/off, jobs and re-runs, health, soak, error log). **Settings stays separate** for tunable parameters. **System merges into Control.** Navigation: Dashboard, Control, Reports, Replay, Settings. |
| D7 | Approach 1: two aggregate read routes, `GET /api/live` and `GET /api/control`, refreshed by the existing SSE change feed; pages built from small components. |
| D8 | **The dashboard never calls Questrade.** Prices shown come from the worker's own quote polling, stored in the database (§5.2). This protects the Questrade rate budget (Monday 2026-09-28 lesson). |
| D9 | Visual style: modern and restrained — dark slate surfaces (light theme kept), one accent colour, green/red only for money, tabular numerals, flat panels with 1 px borders, no glow/gradients/emoji. 44 px touch targets; phone stacks sections in order. |
| D10 | Soak safety: no decision-path change. The deploy is classified "not a trading change" under Phase 6 D2, proven by tests (§8). |

## 3. Dashboard layout

Tablet width; phone stacks top to bottom in this order.

1. **Top bar** — Trading P&L (today | week | since start; realised + open); Fees; Claude spend with today's bar vs cap; Net after AI; win rate, trades, expectancy R (run); engine state chip (AUTO/MANUAL, RUNNING/PAUSED); live indicator (SSE connected, last update age); **books check** (cash + positions at cost + realised = ledger equity, to the cent) ✓/✗.
2. **Equity chart** (left) — intraday equity from equity snapshots with the day's start line and fill markers; toggle Today ⇄ Whole run. **Risk panel** (right) — the four kill-switch lights with value vs threshold; open risk $ vs cap; slots used n / max positions.
3. **Positions** — 0–20 rows: ticker, long/short, qty; entry → current price; stop / target; unrealised $ and R; time held; sparkline since entry with entry and stop marked. Sort by unrealised $ (default) or distance to stop; rows within 0.25 R of the stop are highlighted. Tap → expanded chart (1-minute bars since entry, entry/stop/target lines, fill markers); up to 3 expanded. Empty state: "No open positions" plus today's closed trades count.
4. **Activity feed** (left) — newest first; chips: All, Trades, Proposals, Alerts, Scan. Items: orders placed, fills (with slippage vs planned), stops/targets/flatten/overlay exits (with reason), proposals created/approved/expired (manual or auto), kill-switch trips/resets, job failures, and one summary line per scan ("09:35 scan: 543 → 12 passed → AAPL, MSFT"). **Rejected today, and why** (right) — counts by rule from the decision log; tap a rule → its tickers (links to Reports → Day filtered).
5. **Today** — the day's job/event timeline (✓ / ⏳ / ✗ with times in MT) and, only in Manual mode, pending proposals with Approve/Reject (the existing one-tap flow).

## 4. Control page layout

1. **Engine** — approval mode switch (confirm, as decided in Phase 4); trading Running/Paused with Pause/Resume (confirm); live run id and start date; deployed version.
2. **Kill switches** — each switch's state, value vs threshold, last trip; Reset with typed reason (existing rules, audited).
3. **Strategies** — each strategy on/off with its config revision and a link to its Settings section.
4. **Today's schedule and jobs** — every expected job with time (MT), status, duration and key detail (e.g. "543 bars in 31 s"); Re-run for jobs the System page already allows.
5. **Health** — worker heartbeat age; Questrade token state; Questrade 429s and pause seconds today; opening-bar fetch time and completeness for today; Telegram status with Test; database OK.
6. **Soak** — clean-day count n/10, day 1 date, earliest finish, today's verdict so far.
7. **Error log** — warning and above from `event_log` (including the log copy), filterable by level and source.

No new engine actions are added: every action already exists on Dashboard/System/Settings and keeps its rules (confirmation, typed reason, CSRF, audit).

## 5. Data

### 5.1 `GET /api/live` (read-only)
One response with: server time; run id; session info; engine state; period P&L blocks (today, week, run: realised, unrealised, trades, wins, win rate, expectancy R for run, fees, Claude spend, net after AI); Claude cap and today's spend; books check result; equity series (today, or the run when `?range=run`, downsampled to ≤ 500 points); risk (kill switches, open risk, slots); positions (≤ 20) with latest mark, mark time, unrealised $/R, sparkline points (≤ 60); activity feed (latest 100, typed items); rejection counts by rule for today; today's timeline; pending proposals. Money as decimal strings. Replay rows never included (`live_or_unscoped`). Must answer in < 300 ms on a normal day.

### 5.2 Marks written by the worker
A small **mark publisher** in the worker writes the latest quote it already fetched for each held or working symbol to a new table (e.g. `quote_marks`: run_id, symbol_id, bid, ask, last, quote_time, written_at; one row per symbol, upserted). It runs off the event loop, at most every 2 s, only from quotes the worker already has — it never triggers a Questrade call and never feeds back into any decision. Failure logs one masked warning per streak and never raises. The API reads marks from this table; a mark older than 30 s shows a "prices stale" badge. Sparkline and expanded-chart bars come from stored 1-minute candles plus the latest mark; when bars are missing the chart shows what exists.

### 5.3 `GET /api/control` (read-only)
Engine state, kill switches, strategies, today's schedule with job_runs, health (heartbeat, token, Questrade stats: 429s, pause seconds, opening-bar fetch elapsed/completeness — exposed by the worker via its heartbeat detail), Telegram status, soak summary (from the soak report, read-only), and the latest 200 warning+ events. Actions keep using the existing mutation routes.

### 5.4 Real-time updates
The existing SSE change feed gains topics for `marks` and `activity`; the pages re-fetch the aggregate on a topic, throttled to at most once per 2 s. If SSE drops, pages poll every 15 s and show the live indicator as degraded.

## 6. Components (web)

Small, testable components under `web/src/pages/live/` and `web/src/pages/control/`: TopBar, PeriodPnl, CostBar, BooksCheck, EquityChart, RiskPanel, PositionsTable, PositionRow + Sparkline, PositionChart, ActivityFeed, RejectionsPanel, TodayTimeline, PendingProposals (reused), EngineCard, KillSwitchCard, StrategiesCard, JobsCard, HealthCard, SoakCard, ErrorLog. Shared theme tokens (colours, spacing, typography) in one file. Existing Dashboard and System pages are replaced; their tests are migrated.

## 7. Error handling

- API errors: each panel shows its own error state with Retry; one failing panel never blanks the page.
- Stale marks (> 30 s) or a stale heartbeat (> 60 s) show badges, not hidden rows.
- Empty states for no positions, no activity, no rejections, non-session days.
- All free text (reasons, catalyst text, error messages) rendered as plain text.

## 8. Testing and soak safety

- API: seeded testcontainers days — positions at 0, 1 and 20; period boundaries (week start, DST, holidays); books check pass/fail; replay rows excluded; no Questrade call (the fake client asserts zero calls); response time budget.
- Worker mark publisher: writes only from existing quotes; never raises into the worker; stops cleanly; a simulated trading day is identical with the publisher on and off (all trading tables and Telegram messages), as in P6-T11.
- D2 check: no decision-path file changes (strategies, engine, broker, market decision code, jobs/nightly, jobs/premarket, catalyst, settings_store); golden replay files unchanged.
- Web: each component with FakeApiClient fixtures (0 / 1 / 20 positions, stale marks, errors, empty days); touch targets; phone layout; XSS in free text.
- LIVE: deploy after the close, run the cron catch-up, and the Playwright live smoke visits Dashboard and Control.

## 9. Out of scope (for now)

Multiple strategies' leaderboard (appears when there is more than one strategy), public/visitor views, audience polls, custom date ranges on the Dashboard (Reports has them), any new engine action.
