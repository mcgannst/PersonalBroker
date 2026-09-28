# FIX-PACING · attempt 1 · live log

Task: pace Questrade market data below the 20 req/s limit and make the 429 pause follow
X-RateLimit-Reset (root cause of the 2026-09-28 9:35 ET opening-bar slowdown, see LIVE-0928-a1.md).

- 16:34 MT start. Worktree at trunk 1fa182f. No settings key for the live market rate exists
  (only replay.questrade_rps), so the default goes in a module constant MARKET_RPS = 17.0.
- 16:36 MT existing pins found: test_questrade_client.py::test_429_on_every_attempt_does_not_pause_after_the_last
  expects junk-Reset backoff [0.5, 1, 2, 4]; the new market cap makes it [0.5, 1, 2, 2].
  P1-T7 breaker "past-reset" (Reset one hour ago) expects a >= 0.5 s backoff: a Reset more than 1 s in
  the past is treated as stale (clock skew / bogus) and falls back to the backoff, so it stays green.
- 16:38 MT RED: tests/adapters/test_questrade_pacing.py (15 cases) written first. Collection failed on the
  missing MARKET_RPS, then with MARKET_RPS=20 and a `monotonic` constructor arg 9 failed for the right
  reasons (0.5 s pause for Reset+0.2 s, 7.5 s backoff total, 27.1 s batch at 20 rps, 21 s paused).
- 16:41 MT GREEN: MARKET_RPS = 17.0 (default market_rps, still a constructor arg; no live settings key
  exists, only replay.questrade_rps). `_pause_after_429`: usable Reset within 2 s -> clamp(delta+0.05,
  0.05, 2.0); Reset > 2 s ahead -> min(30, delta) (hourly exhaustion, MAX_429_PAUSE kept); Reset missing,
  unparseable, NaN or > 1 s in the past -> 0.5*2**attempt capped 2 s for market (account keeps the 30 s
  cap). stats unchanged. New `monotonic` arg on QuestradeClient feeds both buckets (tests only).
- 16:42 MT pin updated (only one): test_questrade_client.py::test_429_on_every_attempt_does_not_pause_after_the_last
  [0.5, 1, 2, 4] -> [0.5, 1, 2, 2], abs 0.05 -> 0.07 (a 1/17 s slot rides on the post-pause sleep).
- 16:43 MT simulation (discrete-event virtual clock, 543 candle requests, instant responses):

  | server model                              | new 17 rps          | old 20 rps + 0.5 s floor |
  |-------------------------------------------|---------------------|--------------------------|
  | 20/s window, alone                        | 31.9 s, 0 x 429     | 27.7 s, 1 x 429          |
  | 20/s window + 3 rps other load            | 31.9 s, 1 x 429     | 36.4 s, 18 x 429, 9 s    |
  | 20/s window + 4 rps other load            | 33.9 s, 32 x 429    | 39.4 s, 19 x 429, 12 s   |
  | 429 every 15 requests, Reset +0.1-0.4 s   | 44.0 s, 38 x 429    | 48.9 s, 38 x 429, 21 s   |

  Honest note: the "429 every 15 whatever the pace" worst case lands at 44 s, inside 45 s but not
  "well under", because pacing cannot help when the 429 rate does not fall with it. The window model
  (what Questrade actually does) is where the fix pays: +3 rps load goes from 36.4 s to 31.9 s.
- 16:44 MT targeted: tests/adapters tests/gauntlet tests/market 1020 passed (FIX-OPENBARS and P1 breakers
  green). Gate attempt 1 stopped at ruff E501 (2 long lines), fixed, ruff/format/mypy clean locally.
- 16:58 MT gate.sh exit 0: ruff/format/mypy clean, 3629 passed, web 464 passed. Committing and pushing.
- 16:59 MT commit 459e172 pushed to trunk (rebased over dc4cd1e, a docs-only commit).
- 16:59 MT deploy dev from this clean worktree at 459e172: down 22:59:43Z, up 23:00:35Z (health 200
  after 12 s). /api/meta version phase-5-complete-24-g459e172. Worker heartbeat started 23:00:32Z,
  beat 1.8 s old at check, phase idle.
- 17:00 MT cron_gap 22:59:43Z..23:00:35Z: "nothing skipped". It lists the 18:05 EDT (16:05 MDT)
  soak-report --notify as "may have been interrupted" (started in the 120 min before the down stamp);
  soak-report is read-only and writes no job_runs row, it ran 54 min before the downtime, so it was not
  re-run (a re-run would only resend the Telegram report). The 20:00 ET nightly is outside the window.
- 17:01 MT `trader decisions record --date 2026-09-28 --final`: run 1, 588 rows, final.
