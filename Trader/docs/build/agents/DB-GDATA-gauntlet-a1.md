# DB-GDATA · Verifier+Breaker+Reviewer · attempt 1

Scope: DB-T3 (1acf987), DB-T4 (e84838d), DB-T5 (cbf7426), DB-T6 (9498b2d).

## Log

- 2026-09-28 20:48 MT started; worktree agent-a158eefc2c648b62b, trunk pulled (cbf7426).
- 20:50 MT verify: four commits on trunk, plan boxes T3 1-17, T4 1-11, T5 1-9, T6 1-8 ticked. Targeted lane
  (tests/live, test_live_route, test_control_route, decisions live-unchanged, decisions static, golden
  replay): 276 passed, 35 skipped (all test_contracts "is implemented" skips). VERIFY PASS.
- 20:52 MT read code: live.py:167 calls get_live_run (INSERT runs + sim_accounts per request);
  services.plan = runtime.plan_builder is used by live._timeline (live.py:117) and control.schedule
  (control.py:175); plan_builder builds a fresh StrategyRegistry per call whose enabled() reports a broken
  plug-in as an `error` event (relayed), called twice per plan (live_day_plan + exits_only_strategies).
- 20:55 MT wrote tests/gauntlet/test_db_data_breaker.py (13 functions, 35 cases) on the production
  composition (build_services on a test Core; questrade_client and QuestradeClient.quotes/candles/
  candles_many fail and record).
- 21:02 MT first run: 7 failures. B5's control diff was only health.db_latency_ms (test artefact, excluded).
  B11 under 4-worker load: median 730 ms; serial median 99 ms, so it asserts the fastest request and prints
  the medians. Added B13 (pending proposals, N+1).
- 21:05 MT checker lane (breaker + tests/live + both route tests + P6 D2 tests + P6-T11 breaker), -n 4:
  338 passed, 35 skipped, 6 xfailed (strict). No production file changed, so no full gate.

## Breaker results (35 cases)

| Test | Result | Why it matters |
|---|---|---|
| B1-B2 read-only, prod wiring (live, live?range=run, control) | FAIL x3 (xfail strict) | Every request INSERTs into runs and sim_accounts (ON CONFLICT DO NOTHING: burns both id sequences, WAL, RowExclusiveLock) twice on /api/live (route + plan builder) and once on /api/control (plan builder); the first request also takes pg_advisory_xact_lock (ensure_defaults). D1 says read-only |
| B3 broken plug-in (control, live) | FAIL x2 (xfail strict) | 2 `error` events `strategies.registry` "strategy orb_sip skipped (enabled)" per page load, relayable to Telegram, never deduped. /api/live refetches on the marks topic up to every 2 s in the session |
| B4 no Questrade, live/stale/missing marks + stale heartbeat | PASS | zero client builds, zero quotes/candles/candles_many |
| B5 replay rows on the SAME symbol and day | PASS | quote mark, mark bars, open position, trades, pending proposal, manual pause, decision log, candidates, events of a replay change nothing in 3 live variants + control |
| B6 DST week boundary, position carried over the weekend | PASS | today/week/run realized, unrealized, fees, Claude, net to 4 dp; 04:59:59Z fill in the week of 10-26, 05:00:00Z in 11-02's; S4 invariant and books exact |
| B7 Thanksgiving + Saturday | PASS | session_day 11-25 then 11-27 |
| B8 books to the cent | PASS | exact books ✓ (25 trips, 7 dp SEC fees); one -0.01 ledger row ✗ with difference -0.0100 |
| B9 unrealised from marks, partial, invariant | PASS | |
| B10 statements 1 vs 20 positions, 10 vs 100 items | PASS | 79 and 79 (ceiling 80) |
| B11 300 ms budget, normal day | PASS | serial: today median 99-103 ms (min 96), run+expand 82-95 ms; equity 20-64 ms, positions 17-68 ms, timeline 20-24 ms; 80 statements (= ceiling). Under -n 4 load: median 730 ms |
| B12 part isolation x21 (14 live, 7 control) | PASS | secrets (URL password, refresh_token=, Bearer) never in the response |
| B13 2 pending proposals on the normal day | FAIL (xfail strict) | 80 -> 86 statements: proposal_view per row (3 each); over LIVE_MAX_STATEMENTS |

## Findings

- F1 should-fix, routers/live.py:167: `get_live_run` on every request (INSERT runs, INSERT sim_accounts).
  Use a read-first lookup like DB-T6's `routers/control.py:44-51` (shared helper), creating only when none.
- F2 must-fix, livedata/control.py:175 and routers/live.py:117: `services.plan` is `runtime.plan_builder`
  (api/services.py:203): per call settings load, get_live_run INSERTs, a new StrategyRegistry (entry-point
  scan), ensure_defaults once per process (advisory locks; can INSERT a strategy_configs revision + audit row
  after a plug-in version bump), report_plan_problems (advisory lock, error event once per session), and
  `registry.enabled()` twice whose `_report` writes an undeduped relayable `error` event per broken plug-in.
  Fix: build the API's plan with `soak.readonly_plan(core.factory, core.clock, core.calendar, settings)`
  (quiet registry, no run creation, no problem reporting), in build_services or in the two callers.
- F3 should-fix, routers/control.py:117: a failing part logs at `error`, which the API's log mirror copies to
  event_log as `log.api` on every request (a write from a GET; it also floods the Control error log itself).
  Use `warning` as live.py:81 does.
- F4 should-fix, routers/live.py:135-142: pending proposals are N+1 (`proposal_view` does 3 Session.get per
  row); the normal day already sits exactly at LIVE_MAX_STATEMENTS = 80 (79 + the scan's top tickers), so
  DB-T12 test 1 has no headroom. Batch the view; F1/F2 fixes also remove ~6-8 statements.

## Review (spec + code)

Plan match: T3-T6 match the plan's interfaces, part names, Server-Timing and S3-S8, S10, S15.

- T5 choices: positions failing nulls risk: accepted (risk_panel needs LivePositions; documented). Header
  fallbacks: trading "blocked" accepted; nit, live.py:173 falls back to `session.date` (current_session: the
  NEXT session on a weekend) where session_day is the LAST session, so activity/rejections/closed_today would
  read the wrong day in that failure. Failed proposal as proposal_rejected "Failed ...": accepted (the kind
  list is contract-final). MT scan line: accepted; nit, activity.py:44 hard-codes America/Edmonton instead of
  env.tz_display.
- T4 choices: `Ledger(SessionCalendar())` cache (positions.py:55): accepted, `Ledger.balances` does not use
  the calendar (ledger.py:72-83). Private `_session_day` (risk.py:39): nit, import periods.session_day now
  that DB-T3 landed. 5-day bar lookback (positions.py:46): accepted (daily flatten). Extended-hours candles:
  accepted (bars start at the entry minute). Nit: a stale (hours old) mark counts in unrealized without
  `unrealized_partial`.
- T6 choices: live-run fallback that creates the run on a fresh DB (control.py:44-51): acceptable (every
  process does it, only on a fresh DB), but moot while F2 creates it anyway. Soak cache (health.py:241-315):
  OK; nit, keyed by id(factory). KillSwitchEventOut inline (control.py:68-90): nit, duplicates
  routers/killswitch.py's mapping. Nit: strategy_cards does 2 queries per strategy (control.py:93-107).
  Nit: on a weekend the engine chip uses current_session (next session: a Friday daily-loss trip is already
  cleared, RUNNING) while the lights use the last session (tripped).
- T3 choices: per-minute equity (equity.py:125-168) correct per S6; the largest part (20-64 ms, O(minutes x
  positions) in Python Decimal), fine for the budget. Nit: a position carried overnight is valued at cost
  before its first bar of the day (equity.py:163-165), a jump from the prior-close start line. Nit:
  `opened_at < end` vs `<= end` (equity.py:159 vs 162). Downsample: deterministic, extremes kept, OK.
  Rounding: q4 half up everywhere, net after AI exact (B6).

- 2026-09-28 21:08 MT finished.
