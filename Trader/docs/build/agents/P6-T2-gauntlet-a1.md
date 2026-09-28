# P6-T2 gauntlet, attempt 1 (commit 1603651)

- 2026-09-27 23:04 MT started; worktree up to date with trunk (HEAD 1603651)
- 2026-09-27 23:05 MT gate.sh started in the background on 1603651; git status clean apart from my own files; acceptance boxes 1-17 all ticked
- 2026-09-27 23:12 MT breaker file written: 14 tests, 11 pass, 3 fail (a token recovery row after the deadline flips a final day; "9:35 scan off" while orb is pending; the Saturday --final line shows a failed weekly as pending)
- 2026-09-27 23:16 MT gate.sh on 1603651 PASSED (ruff, mypy, 3238 pytest, 436 vitest). Verify: PASS
- 2026-09-27 23:17 MT Breaker FAIL (3 of 14). Review: 0 must-fix, 3 should-fix (the three failing tests), nits: sync DB in async notify_report, "sent" printed even when the send failed, --json --notify ends with the "sent" line, --sessions < --target, SPEC MT column vs tzdata
- note: tzdata 2026.4 has America/Edmonton on UTC-6 all year from Mar 2026, so 18:05 EST = 17:05 MT (the SPEC MT column says 16:05); the DST test takes the MT time from tzdata
