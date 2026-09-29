# SIZECAP builder, attempt 1

Task: 10% per-stock cap setting (`max_position_pct`) in risk sizing, `position_cap` rejection, `trader live-run new --confirm`, docs. Approved by Stephen Mon 2026-09-28 ~23:30 MT for Tuesday premarket.

- 23:30 MT: started; worktree agent-aa01b25f56b3abaab at 1fa26b2. Read engine/risk.py, settings_store.py, engine/runs.py, replay/types.py, api/forms.py.
- 23:33 MT: RED: tests/engine/test_risk_cap.py (RiskContext has no `symbol`). GREEN: `max_position_pct` setting (Decimal, > 0, <= 1, default 0.10), cap in RiskManager._entry (cap unit = entry x (1 + slippage_buffer) + ECN/share if direct-routed; commission off the cap), `position_cap` RiskCheck with reason "1 share of X costs $P, over the 10% cap $C". Tie order for limited_by: risk, cash, cap.
- 23:35 MT: settings tests (validation, Risk group after risk_pct, replay snapshot without the key loads as 1). forms.SETTING_GROUPS + replay.types._snapshot_settings.
- 23:37 MT: orchestrator passes the ticker (RiskContext.symbol); recorder proposal sizing adds shares_cap/max_position_pct/cap_dollars only when present (old proposals unchanged).
- 23:38 MT: no existing mechanism to start a fresh live run (get_live_run only creates when none is active). Added engine.runs.start_new_live_run / check_new_live_run and `trader live-run new --confirm [--set K=V] [--strategy-param S.P=V]`; tests/engine/test_live_run_new.py 21 passed.
- 23:39 MT: tests/integration/test_position_cap_day.py (6 entries with max_positions 10, each <= 10% of $720; ZZZ at $100 rejected position_cap, in decision_log risk rows). 1 passed.
- 23:44 MT: full pytest (not the gate): 41 failures, all from sizing changing. Scenario tests pinned to the pre-cap sizing with `max_position_pct = 1` in their setup (worker_day, simulated_day, killswitch_trips, marks/decisions_live_unchanged, engine_candles, replay_isolation). test_orchestrator end-to-end and test_p2_t10t11_breaker sizing edges updated to the capped numbers (commented). test_risk.py's base ctx pins cap 1.
- 23:48 MT: golden regenerated (UPDATE_GOLDEN=1) with justification: all 4 golden trades were cash-limited at ~100% of the $720 equity; the 10% cap now binds on each: BBB 34 -> 3, CCC 46 -> 4, AAA 66 -> 6, DDD 23 -> 2. Prices, exit reasons, R multiples (to 4 dp rounding) unchanged; P&L, fees, slippage totals and drawdown scale down. Data file unchanged.
- 23:53 MT: SPEC §6.1, §13 table + list, §15.1 (live-run new); master plan §7.1 Risk row. ruff, format, mypy clean.
- 00:00 MT: gate.sh exit 0: ruff, format, mypy clean; pytest 4175 passed, 35 skipped; vitest 74 files / 729 passed. Committing and pushing. Not deployed (the orchestrator deploys).
