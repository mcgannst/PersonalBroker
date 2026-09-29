# DB-T11 builder, attempt 2 (fix round 1 for the web wiring gauntlet)

Base: trunk f8be0ca. Worktree: agent-a2cfde12b409f73ff.

## Log

- 2026-09-28 21:10 MT: started; pulled trunk (f8be0ca). Read gauntlet log, breaker file. BUILD_STATE started entry written.
- 21:11 MT: `npm ci`; dropped `.fails` from B8/B12/B13 and confirmed they fail for the reported reasons
  (control not refetched; `.btn-danger` Reject and `.timeline-item.tone-bad .timeline-label`; header `light tone-ok`).
- 21:13 MT: fixes: KillSwitchPanel invalidates `system` too; TOPIC_KEYS.killswitch += `system`; `.live-page/.control-page
  .btn-danger` and timeline-label remaps to `--status-bad`; global `.status-*` classes; LiveDot uses `status-ok/muted`;
  `--chart-exit` slate; `public/theme-init.js` pre-paint (CSP forbids inline); DayDecisions URL re-read on back/forward.
- 21:14 MT: dead code: deleted JobRuns.tsx + test; StatusCards/RateLimits (+ private cards) and their tests; unused
  dashboard.css rules. Added layout/dbT11FixRound1.test.tsx (10 tests).
- 21:15 MT: targeted vitest (breaker, fix-round, system, reports, settings, shell): 19 files / 208 passed.
- 21:16 MT: full check: 2 failures pinning the old killswitch topic map (queryKeys.test.ts, web_pages_breaker topic
  case); updated both to include `system`.
- 21:17 MT: `npm run -s check`: tsc clean, vitest 73 files / 708 passed (breaker 14/14). `npm run build` ok
  (dist has theme-init.js). No stray root node_modules/.vite.
- 21:18 MT: plan DB-T11 "Fix round 1" notes written; committing.

## Result

B8 fixed, B12 fixed, B13 fixed; nits done (pre-paint theme, DayDecisions back/forward, dead code, --chart-exit).
