# DB-T2 gauntlet (Verifier + Breaker + Reviewer), attempt 1

Target: ec1e011 `DB-T2: worker quote tap and mark publisher`. Worktree agent-a2ee08cef81a58b85 at 49df10d.

## Log (MT)

- 20:00 started. Read plan (DB-T2, S1a, S10, D2, Plan verification, builder notes), tap.py, publisher.py, worker.py diff, client.py candles_many/TokenBucket, data_service opening_bars, FIX-OPENBARS breaker.
- 20:10 builder suite re-run on 49df10d: tests/marks + tests/test_worker_marks.py 64 passed (62 s).
- 20:15 review notes so far: tap.py matches the S1a shape. The extra _roll_day in quotes() is required by S10 and guarded. _failed() (the logger call) runs unguarded inside the except blocks, so a double fault could replace the caller's result/exception. FK locks: trading locks positions/orders FOR UPDATE (the publisher only plain-SELECTs them, fine). runs key cols = {id} only (the partial unique index is not a key), so no runs conflict. BUT nightly.py -> repo.upsert_symbols ON CONFLICT DO UPDATE SET ticker, exchange (unique key cols) takes FOR UPDATE on symbols rows, which conflicts with the publisher's RI KEY SHARE. The builder note "trading path never locks a symbols row" is wrong, and a lock-order deadlock is possible (nightly can be the victim). Writing breaker tests.
- 20:45 wrote tests/gauntlet/test_db_t2_breaker.py (14 tests, 19 cases) on a virtual-time event loop (the selector jumps to the next timer and counts iterations). A scratch mutation (one extra `await asyncio.sleep(0)` in the tap's quotes/candles_many, not committed) is caught by the iteration-count checks.
- 21:05 results: 17 passed, 2 xfailed (strict) = findings F1 and F2, both reproduced (F1: quotes raised RuntimeError and the CancelledError was replaced by RuntimeError. F2: nightly got psycopg DeadlockDetected "while locking tuple in relation symbols").
- 21:10 targeted lane (no full gate, as instructed): breaker + tests/marks + tests/live + test_worker_marks + test_worker + tests/adapters + tests/market + FIX-OPENBARS breaker + P6-T11 breaker (D2) + integration/test_decisions_live_unchanged + decisions/test_static + replay/test_golden + replay/test_isolation_static: 676 passed, 15 skipped (tests/live contract stubs now implemented), 2 xfailed, 0 failed (240 s).
- Measured tap bookkeeping on the loop: 0.008 ms for 20 quotes, 0.23 ms for 550, 10 ms for 10,000.

## Findings

- F1 should-fix, tap.py:218-222 (`_failed`) and 270-273 (health): the logger is called unguarded inside the `except Exception:` blocks at tap.py:108-109, 113-114, 129-130, 215-216. If structlog raises (a double fault), quotes() loses its result and candles_many's CancelledError (the 9:35 guard) is replaced by the logging error, which `_fetch_opening_bars` does not catch (it catches only TimeoutError). Fix: wrap the log call in its own try/except Exception. Test: xfail strict.
- F2 should-fix, publisher.py:250-286 against repository.py:82-102 and nightly.py:315-319: the INSERT's FK checks take KEY SHARE on symbols rows in symbol order while nightly's one transaction upserts symbols with ON CONFLICT DO UPDATE SET ticker, exchange (unique key columns), which takes FOR UPDATE. A lock-order cycle is detected by nightly's deadlock check first (the publisher's own check ran before the cycle closed, and its lock_timeout is 5 s), so nightly is aborted. The builder note (plan line 470) "the trading path never locks a symbols row" is wrong. Needs nightly (20:00 ET cron, or a manual run from the Control page) to overlap with held positions or working orders plus quote polling, so it is unlikely on the normal schedule. Fix: before the inserts, `SELECT ... FROM trader.symbols WHERE id = ANY(:sids) ORDER BY id FOR KEY SHARE NOWAIT` (and the run row), so the publisher never waits while it holds a key lock, or at least lock_timeout well below deadlock_timeout. Test: xfail strict.
- N1 nit, tap.py:105-109: the extra `_roll_day` in quotes() is required by S10 ("first proxied call"), guarded, synchronous and O(1). OK. The DB-T2 "in short" text should say so.
- N2 nit, publisher.py:51: set_config(..., true) is exactly SET LOCAL and bindable. Verified: the timeouts fire at 400 ms (lock and statement) and the pooled connection keeps its defaults. Accept.
- N3 nit, worker.py:570: the worker cancels the publisher but never calls close(). A pass stuck on a dead TCP connection (statement_timeout is server-side) keeps a non-daemon executor thread, and interpreter exit joins it, so the stop would end in docker's SIGKILL. close() does not avoid that join either. DB-T10 wiring should close it, and consider psycopg keepalives/connect_timeout. Not a trading risk.
- N4 nit, tap.py:126-130: a `_roll_day` failure also drops that batch's record (same try as `before`).
- N5 orchestrator: tests/api/test_errors.py:146 asserts "abc" is absent from logs and tracebacks, which contain the checkout path. It fails in any path containing "abc" (e.g. agent-a547eeabc...). Path-fragile, not fixed here.

## Verdict

The tap is safe for the soak: byte-identical 9:35 batch (dispatch times, 429 pauses, 45 s cancellations, results, logs, loop iterations) with and without it. F1 needs a double fault. F2 is a publisher/nightly lock-order issue off the normal schedule and should be fixed before the DB-T12 deploy.
