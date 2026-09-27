# P3-T5/T9 Spec+Code reviewer, attempt 1

- 11:05 MT started. Reviewing c12a3e6 (T5) and ada38cc (T9).
- 11:20 MT read plans, T5 and T9 diffs, T3 scheduler on trunk, SettingsStore | next: T5 tests, then write findings
- 11:09 MT review done: T5 PASS (0 must-fix), T9 FAIL (2 must-fix: unguarded settings() reads kill the run loop, inline relay can block the quote loop and heartbeat for minutes) | next: log finished, hand back
