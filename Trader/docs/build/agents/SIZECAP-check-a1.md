# SIZECAP check (Verifier + Breaker + Code reviewer), attempt 1

Target: e12c363 (per-stock cap `max_position_pct`, `trader live-run new --confirm`). Worktree agent-a349189d995cd0554.

- 00:05 MT: started; read the diff (risk.py, runs.py, cli.py, settings_store.py, replay/types.py, recorder.py, orchestrator.py), the builder log, SPEC 6.1, fill_model.py, sim_broker.account_state, LiveRunWatch, old vs new golden JSON.
- 00:06 MT: wrote tests/gauntlet/test_sizecap_breaker.py (10 test functions, 32 cases): sizing property (3000 random cases), real fills (limit, stop-limit, market >= $2 with commission+ECN), total equity vs settled cash, 10 x 10% vs settled cash, position_cap reason form, DST/early-close reset block, confirm twice, --set max_position_pct validation, LiveRunWatch, golden changed only in size. 3 strict xfails pin findings.
- 00:07 MT: checker lane (xdist 4): my file + tests/engine + test_position_cap_day + test_golden + P2 T8T9/T10T11 breakers: 248 passed, 3 xfailed. --runxfail confirms each xfail fails for the stated reason: gap stop fill 103.10 vs cap 100.50, $0.50 market 72.93 vs cap 72, CLI crash or late refusal leaves starting_cash 1000 with the old run still active.
- Findings, should-fix: (1) cli.py live_run_new writes --set/--strategy-param before start_new_live_run in separate transactions (not atomic). (2) The cap is on the estimated cost: a stop entry gapping above its stop, or a market entry under $2, fills above the cap (orb_sip price_min 5 rules out the latter). Nits: _key_values parses numbers as JSON floats, a negative-equity reason prints "$-x", the dashboard risk cap (api/livedata/risk.py:173) is unaware of max_position_pct.
- 00:08 MT: verdict GO for tonight's deploy. Committed and pushed.
