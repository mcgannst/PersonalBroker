# DB-T10 gauntlet (Verifier + Breaker + Reviewer), attempt 1

Target: 4d4b3ef `DB-T10: wire the mark publisher; D2 proofs`. Worktree agent-a16bc3fcbeb528c93.

## Log (MT)

- 21:29 started. Read the plan (DB-T10 + builder notes, S1/S1a, D2 section), runtime.py diff, tap.py, publisher.py, worker.py run/_shutdown/_beat, DB-T2 gauntlet log (N3: stuck executor thread vs interpreter exit).
- 21:35 running the builder's DB-T10 tests (runtime_marks, marks_live_unchanged, d2_static, d2_deploy_diff): 19 passed (64 s).
- 21:38 wrote tests/gauntlet/test_db_t10_breaker.py. Non-day tests: 15 passed, 1 xfailed (strict: F2 stuck thread vs process exit).
- 21:41 day tests: the real composition equals "off" on the proof AND on two new probes (event-loop iterations of every worker step, identical list across ~190 steps; trader-schema locks held by other backends after each publisher pass: always 0). Mutants: yield caught by the loop probe only, delay (3 ms) by the loop probe only, reorder candles_many by the proof (call log), publisher-leaves-a-lock by the lock probe only. So the stock test_marks_live_unchanged is timing- and lock-blind (F1).
- 21:47 checker lane: 817 passed, 35 skipped (all tests/live/test_contracts.py stubs, "implemented"), 1 xfailed (F2). tests/live + replay (golden) + decisions re-run with -rs: 516 passed, the same 35 contract-stub skips only, the D2 deploy diff not skipped.
- 21:48 committing the breaker and this log.
- 21:43 ruff/format/mypy clean on the breaker; ran the checker lane (breaker, runtime_marks, marks_live_unchanged, tests/live, tests/marks, DB-T2 breaker, P6-T11/P5-RC/FIX-OPENBARS breakers, decisions_live_unchanged, tests/decisions, tests/replay incl. golden, worker/runtime/P4 wiring tests, worker day).

## Findings

- F1 should-fix (proof sensitivity, test only): tests/integration/test_marks_live_unchanged.py compares rows, chat and the Questrade call log only. It catches a reordered or extra request (mutant "reorder": caught). It does NOT catch an extra scheduling point in the tap, a quote delayed by 3 ms, or a publisher that leaves a lock on orders behind: all three give identical rows, chat and calls. The breaker adds two probes on the same simulated day (event-loop iterations of every worker step, and trader-schema locks held by other backends after each pass). The real composition matches "off" on both, and each timing or lock mutant is caught by one of them. Suggest folding the two probes into test_marks_live_unchanged (or naming the breaker file as D2 evidence). The plan's risk 1 line cites DB-T10 tests 1-3 for "an extra event-loop yield". Today only DB-T2 test 14 and this breaker actually prove it.
- F2 should-fix (shutdown, not trading): publisher.py close() is `shutdown(wait=False, cancel_futures=True)`, but the worker thread is non-daemon and interpreter exit joins it (concurrent.futures `_python_exit`). A pass stuck past its server-side statement_timeout (dead TCP, no client keepalive or tcp_user_timeout on the engine, db/session.py:16) keeps the process alive until supervisord's SIGKILL at stopwaitsecs=60. The worker's own shutdown (beat stopped, lock release) has already finished, so trading is not affected, only the restart latency. A pass bounded by its timeout exits promptly (tested). Fix: psycopg keepalives or tcp_user_timeout on the connect args, or a daemon-thread executor. Test xfail strict. The same holds for the pre-existing to_thread users (decisions loop).
- N1 runtime.py:758: `--once` still builds the tap and the publisher (never started, no thread, executor shut down with the stack). This matches the plan ("never started by --once"). With --once, the step runs through the transparent tap.
- N2 runtime.py:839-855: HeartbeatParts now also guards `rate_limit`. Before, a raising `rate_limit_remaining` emptied the whole extra (worker._heartbeat_extra). Now only that key is dropped. This touches the heartbeat only, and is an improvement.
- N3 runtime.py:758 `lambda: run.id` versus the decisions loop's `lambda: run_id`: equivalent (run is never rebound, and a live-run change restarts the worker).
- Loosened P4 tests: both are still meaningful. The P4-T18 breaker still pins the exact rate_limit sequence [{} before open, the values after], and the one-client count. test_runtime_phase4 still pins "no rate_limit before open". The exact full extra before open is pinned by test_runtime_marks. Accept.
- Busy ruling: an additive Literal. No production code reads the publisher's PublishStep.skipped (cli/postclose read the decisions results). Accept.
- Construction order: LazyQuestrade (inner stack), then the tap, then stack.callback(close), then the engines' aclose. LIFO order: engines close, then publisher.close, then the client. close runs after Worker.run returned (tested, including SIGTERM mid-pass).

## Verdict

The deploy 459e172 to HEAD is NOT a trading change under D2. Rule 1: settings_store.py is untouched. Rule 2: docker/crontab is untouched. Rule 3: no decision-path file changed, the protected test folders only gained files, and golden is unchanged (test_d2_deploy_diff with TRADER_D2_BASE=459e172 passes. Garbage, a non-commit object and HEAD fail, never skip). runtime.py and worker.py are composition-only. Behaviourally, the worker day is identical on and off in rows, chat, call arguments, per-step loop iterations and leftover locks. F1 and F2 are should-fix and do not block the deploy.
