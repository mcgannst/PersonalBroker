# DB-GDATA · Builder · attempt 2 (fix round 1)

Scope: findings F1-F4 and nits of Trader/docs/build/agents/DB-GDATA-gauntlet-a1.md on DB-T3..DB-T6.

## Log

- 2026-09-28 21:10 MT started; worktree agent-ac8ffa30489e9187b, trunk 44fd758. Read the gauntlet log, the
  breaker, routers/live.py, routers/control.py, livedata/control.py, soak.readonly_plan, runtime.plan_builder.
- 21:13 MT new livedata/readonly.py: `live_run` (SELECT active live run, get_live_run only when none) and
  `day_plan` (soak.readonly_plan, quiet registry over the API registry's plug-in classes: load_all costs
  ~10 ms per call and livedata may import only StrategyRegistry from trader.strategies).
- 21:14 MT F1 live.py uses readonly.live_run; F2 live._timeline and control.schedule use readonly.day_plan;
  F3 control router part failure -> warning; F4 _pending: one outer-joined SELECT (proposal, signal,
  config, cancelled order) + one symbols SELECT (always run, empty IN for 0) into the identity map, then
  proposal_view unchanged.
- 21:15 MT nits: fallback session_day = last session (no call to periods.session_day); header trading and
  Control engine chip on the session day (same as the lights); activity_feed(tz=) with the route passing
  ZoneInfo(env.tz_display); risk imports periods.session_day; strategy_cards 2 batched statements total.
- 21:17 MT equity `<` vs `<=`: changing to `<=` broke 3 DB-T3 tests (an extra point for a position opened
  exactly on a minute end). The two conditions answer different questions (open DURING the minute vs held
  AT its end with its cash already counted), so reverted and commented instead.
- 21:18 MT tests: contract signature of activity_feed (+tz), live route 500 target renamed to live_run,
  test_control autouse fixture routes readonly.day_plan to the fake services.plan, new tests: schedule
  uses the read-only plan and writes nothing; _pending statements equal for 0/2/10 pending and views
  equal to proposal_view's. Removed the 3 strict-xfail decorators (6 cases) from the breaker.
- 21:20 MT targeted lane (breaker, tests/live, both route tests), -n 4: 304 passed, 35 skipped.
  Serial B10/B11/B13: 73 statements (1 vs 20 positions); /api/live 74 statements (ceiling 80, was 80),
  median 90.4 ms today, 82.3 ms run+expand; 74 without and with 2 pending.
- 21:22 MT live-unchanged, golden replay, P6-GD + P6-T11 breakers, tests/decisions, test_dashboard:
  210 passed. Plan T3-T6 "Fix round 1" notes added. Full gate next.
- 21:29 MT full gate (bash Trader/build/gate.sh, lane 2, no wait): ruff, format, mypy clean; pytest 4079
  passed, 35 skipped (6:08); web 72 files / 699 tests passed. Commit and push.
- 2026-09-28 21:29 MT finished.
