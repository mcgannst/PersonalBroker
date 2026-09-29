# DB-T11 gauntlet, attempt 1 (Verifier + Breaker + Spec/Code reviewer)

Commit under test: 02dcccf (DB-T11: live dashboard page, Control route and nav, live-update throttle).
Worktree: agent-aaf77a7408b47da54.

## Log

- 2026-09-28 20:59 MT: started; pulled trunk (50985fb, contains 02dcccf). BUILD_STATE started entry written.
- 2026-09-28 21:00 MT: read the plan DB-T11 section, builder notes, open questions 1-9, S9/S11-S16, design D9.
- 2026-09-28 21:01 MT: `npm ci`; `npm run -s check` -> tsc clean, vitest 72 files / 699 passed.
- 2026-09-28 21:05 MT: code read: Dashboard.tsx, App.tsx, Layout.tsx, main.tsx, useLiveUpdates.ts, Settings.tsx,
  DayDecisions.tsx, styles.css, layout.css, smoke.spec.ts; the deleted DashboardPage/SystemPage tests against
  LivePage.test.tsx and SystemCarryOver.test.tsx.
- 2026-09-28 21:07 MT: wrote src/gauntlet/db_t11_breaker.test.tsx (14 tests); 11 pass, 3 fail (B8, B12, B13),
  committed as `it.fails` (strict: they fail the gate once fixed) so the shared gate stays green.
- 2026-09-28 21:08 MT: `npm run -s check` -> tsc clean, 73 files / 710 passed + 3 expected fail; `npm run build` ok.

## Verify

PASS. 02dcccf is on trunk; plan DB-T11 boxes 1-10 ticked; web check (tsc + vitest 72 files / 699) and build green
before the breaker file; no stray root node_modules/.vite.

## Break (14 tests)

Pass: B1 Control still shows "Token error" + text (the weakened SystemCarryOver case has an equivalent),
B2 Settings has no engine controls, Telegram test on both pages, Control exactly one Pause/Resume,
B3 hostile ?expand= (5000 ids, dupes, junk, unsafe ints) -> <= 3 valid distinct ids, B4 invalid ?range -> today,
?proposal highlight kept, auto mode + failed pending part still shows the panel, B5 /system?encoded#hash ->
/control unchanged, B6 mixed SSE burst -> 1 leading + 1 trailing per 2 s for live and control, B7 Approve not
delayed by an open throttle window, B9 queries mounted while already disconnected poll every 15 s (others do not),
hello stops it, B10 main.tsx applies the stored theme before render (dark with blocked storage), B11 every CSS
variable used is defined in all 4 token blocks, styles.css has no :root palette, B14 smoke helpers (Server-Timing
parse, median, 300 ms strict, prints only numbers).

Fail:
- B8: a kill-switch reset on Control never refetches /api/control (KillSwitchPanel invalidates only
  `dashboard`; the SSE `killswitch` topic maps to dashboard+killswitches), so the Control lights stay "Tripped"
  until the next heartbeat/events invalidation.
- B12: on the Dashboard the Reject button (.btn-danger -> var(--bad), styles.css:202) and a failed/missed timeline
  label (.timeline-item.tone-bad .timeline-label -> var(--bad), pages/dashboard/dashboard.css:142) are red:
  the .live-page mapping only remaps --tone.
- B13: the header live dot is `light tone-ok` (Layout.tsx:113) outside .live-page -> green --ok on every page.

## Review

Must-fix: none.
Should-fix: S1 (B12), S2 (B13), S3 (B8, owner DB-T9 KillSwitchPanel/Control wiring).
Nits: theme flash before JS for Light users (index.html has no pre-paint script; the no-attribute default is
dark); DayDecisions.tsx:333 filters read from the URL only on mount (back/forward leaves them stale); dead code:
pages/system/JobRuns.tsx and StatusCards'/RateLimits exports used only by their tests, KillSwitchPanel's
showTradingControls=true/statusTones=false paths now used only by tests, most of dashboard.css; Control's Pause
button is also .btn-danger red (DB-T9); --chart-exit is money green on charts (DB-T1 tokens).
Deviations: SystemCarryOver file OK; page-level notices OK (needed, PositionRow has no stop flag); .live-page
mapping incomplete (S1); TDD order process-only.
