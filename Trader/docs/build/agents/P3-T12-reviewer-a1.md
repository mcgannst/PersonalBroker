# P3-T12 reviewer, attempt 1 (spec + code)

- 12:17 MT started. Fetched origin. Reviewing 3d774d1 and c83d4c7.
- 12:24 MT read runtime.py, cli.py, views.py, crontab, diffs of bot, commands, relay, checkin, conftest, master plan. Archived c83d4c7 into the scratchpad (local trunk is behind origin).
- 12:26 MT T12 related tests 152 passed on the reviewed tree, mypy clean on runtime, cli, views.
- 12:21 MT (correction: the two lines above were stamped ahead of the real clock, they happened at about 12:19 and 12:20)
- 12:21 MT finished. Verdict PASS, no must-fix. 4 should-fix: (1) unguarded settings reads at composition (runtime.py 424, 176, 529, 565, 621) defeat T9's guarded settings, a bad settings row stops the worker and every cron job with no alert; (2) cli.py 440 prints JobOutcome.error unmasked and possibly multi-line; (3) no test for SessionEngines rollover (previous session stack closed, one shared Questrade client); (4) the run id is fixed at worker start for bot, relay and commands (runtime.py 424) while engines re-read the live run each session. Nits and Phase 4 follow-ups in the report.
