# DB-GWEB builder, attempt 2 (combined verify+fix, master plan §6.3)

Scope: fix the web gauntlet findings for DB-T7 (0621771), DB-T8 (0736e1a), DB-T9 (49df10d).
Breaker file: Trader/web/src/gauntlet/db_web_breaker.test.tsx (aeb88be, not edited).
Worktree: .claude/worktrees/agent-aa02957b165685bda.

- 2026-09-28 20:14 MT started. Trunk at cb50e99 + aeb88be. npm ci running.
- 20:14 MT VERIFY: breaker file 10 PASS / 6 FAIL (B3, B6, B9, B11, B13, B15), same as the gauntlet.
- 20:15 MT fixture: liveEmptyDay.session.date 2026-10-10 -> 2026-10-12 (next session). No existing test depended on the old value (liveFixtures.test checks session_day only).
- 20:16 MT B9: new pages/live/safeLink.tsx (safeLink: same-site path only, rejects //, /\, whitespace/control chars; SafeLink renders an unsafe link as an <a> without href, i.e. text). ActivityFeed, RejectionsPanel (tickers + Day view), PositionChart use SafeLink; StrategiesCard imports safeLink instead of its private copy. First try rendered unsafe links as <span>: B9 then failed its "more than 3 anchors" check, so the placeholder <a> (no href; HTML's placeholder link) is used.
- 20:16 MT B3: PositionsTable uses expanded.slice(0, EXPAND_MAX).
- 20:17 MT B15: RiskPanel r2 via fmtR, pct1 BigInt decimal rounding (half away from zero); PositionRow and ActivityFeed exit amounts use T7 Money (rounds-to-zero is flat). Nit: RiskPanel dot aria-label "<label>: tripped|not tripped".
- 20:18 MT B11 (approved contract change): TopBar onRetry? (fallback: invalidate ["dashboard"] via QueryClientContext when mounted; the breaker renders TopBar without onRetry); CostBar error?/onRetry? with InlinePartError for claude_today; periods error inline with Retry; BooksCheck gets onRetry.
- 20:18 MT B13/B6: KillSwitchPanel showTradingControls? (default true) and statusTones? (default false); KillSwitchCard passes false/true. control.css maps .tone-*/.status-*/.error-box inside .control-page to --status-* tokens.
- 20:19 MT nits: RejectionsPanel ticker keys `${i}:${t}`; JobsCard Re-run only for jobs in manual_jobs.
- 20:20 MT breaker 16/16 PASS. New tests: pages/live/fixRound1.test.tsx (10), pages/control/fixRound1.test.tsx (3), all pass.
- 20:22 MT plan: "Fix round 1" notes under DB-T7/T8/T9, DB-T11 inputs (onRetry, URL clamp, tokens.css), §7.1 refinement 4 (safe links).
- 20:23 MT `npm run -s check`: tsc clean, vitest 72 files / 659 tests passed. No stray root node_modules/.vite. Committing.
