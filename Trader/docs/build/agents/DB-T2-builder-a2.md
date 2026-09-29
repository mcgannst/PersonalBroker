# DB-T2 builder, attempt 2 (fix round 1)

Base: trunk 351b704. Worktree agent-a92460448f47a87ab. Inputs: DB-T2-gauntlet-a1.md, tests/gauntlet/test_db_t2_breaker.py, plan DB-T2 + S1a.

## Log (MT)

- 20:27 started, synced to 351b704. Read gauntlet log, breaker, tap.py, publisher.py, types.py, plan DB-T2 section.
- 20:29 BUILD_STATE started entry written.
- 20:31 F1: tap.py `_failed` and the health warning each guard their logger call. N4: `_roll_day` and the `before` snapshot in candles_many are guarded separately. Still one await per method.
- 20:32 F2: publisher.py `_lock_keys` runs the run row and then the symbols rows (ORDER BY id) `FOR KEY SHARE NOWAIT` before the inserts. On 55P03 it raises `_Busy`, rolls back, returns skipped=None with 0/0, and increments `busy_passes`. The observations carry over to the next pass. No warning or event, and the streak is untouched. types.py (the DB-T1 contract) is left unchanged, so there is no `busy` literal.
- 20:33 N5: test_errors.py uses a `SECRET-<uuid4>` sentinel. Removed the two xfail markers from the breaker.
- 20:34 breaker 19/19 passed. Added tests: tap (raising logger, failed roll keeps the batch record) and publisher_db (symbol/run FOR UPDATE -> busy <1 s then the carry is written, mark-table lock timeout is still an error, FOR NO KEY UPDATE does not block). tests/marks + worker_marks + test_errors: 83 passed.
- 20:38 targeted lane (breaker, marks, live, worker, worker_marks, test_errors, adapters, market, FIX-OPENBARS, P6-T11, P6-GD, decisions_live_unchanged, worker_day, decisions, golden, isolation_static): 949 passed, 33 skipped (contract stubs whose implementations are covered by their own tasks), 0 failed. ruff/format/mypy clean on the touched files. Plan DB-T2 notes updated (Fix round 1, builder-note correction). Running the full gate.
- 20:58 gate.sh passed (exit 0): ruff, format, mypy clean, pytest 3999 passed / 33 skipped (19 min), vitest 72 files / 659 tests. Committing and pushing.
