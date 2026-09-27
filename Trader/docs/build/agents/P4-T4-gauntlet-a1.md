# P4-T4 gauntlet attempt 1
- 14:04:00 MT started, synced with trunk | next: read plans and auth code
- 14:08:55 MT verify: worktree clean at d1e79f3, T4 boxes 1-14 ticked; read auth.py, routers/auth.py, __main__.py (forwarded_allow_ips=*) | next: write breaker tests
- 14:15:04 MT breaker: 16 tests written, 14 pass, 2 fail (XFF spoofing via forwarded_allow_ips=*, NUL username 500) | next: commit, push, check.sh
- 14:15:43 MT committed and pushed f0d9b8e (P4-B4); running check.sh | next: report
- 14:21:57 MT check.sh: ruff, format, mypy green, pytest 2075 passed and 2 failed (both are the new breaker tests); the web check did not run because pytest failed first | next: hand back
