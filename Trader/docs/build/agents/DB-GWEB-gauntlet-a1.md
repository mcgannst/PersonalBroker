# DB-GWEB gauntlet, attempt 1 (Verifier + Breaker + Spec/Code reviewer)

Scope: DB-T7 (0621771), DB-T8 (0736e1a), DB-T9 (49df10d); contracts DB-T1 (1a6512d).
Worktree: .claude/worktrees/agent-a08901c4532412d28. Scratch: scratchpad/dbgweb.

- 2026-09-28 20:00 MT started. Trunk at 49df10d, three commits present.
- 20:02 MT VERIFY: 0621771 (T7), 0736e1a (T8), 49df10d (T9) on trunk. Plan boxes 1-10 ticked for all three. `npm run -s check` green on 49df10d (tsc clean, vitest 69 files / 630 tests).
- 20:05 MT read spec D1-D10, §3, §4, §7, plan S7-S16, DB-T7/T8/T9/T11 and every component. Writing src/gauntlet/db_web_breaker.test.tsx (16 tests).
- 20:08 MT note: `npm --prefix X exec vitest run --root X` makes npm swallow --root and run vitest from the worktree root (stray node_modules/.vite, deleted). Use `npm --prefix X exec -- vitest run --root X <files>`.
- 20:12 MT BREAK result: 10 PASS, 6 FAIL (all product findings, not test bugs): B3 expand clamp, B6 Control green/red lights, B9 javascript: links, B11 top bar part errors (no Retry, claude_today silent), B13 unconfirmed Resume (KillSwitchPanel), B15 float/negative-zero formatting. `npm run -s check`: tsc clean, 640 passed / 6 failed (only the new file).
- 20:14 MT REVIEW findings in the report (must/should/nit with file:line). Committing the test file as "DB-GWEB: web gauntlet tests".
