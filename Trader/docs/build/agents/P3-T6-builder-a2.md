# P3-T6 builder attempt 2
- 11:15:12 MT started fix round (4 should-fix + nits), synced trunk | next: read bot.py, callbacks.py, relay.py, plan
- 11:18:00 MT read bot/callbacks/relay/tests/breaker; breaker needs 5 foreign events per chat so rate limit will be a burst cap of 5 | next: add issuer methods
- 11:23:04 MT implemented all 4 items + nits; T6 tests, breaker, contracts green (128) | next: plan text, check.sh
- 11:27:30 MT committed and pushed b887987; check.sh 1317 passed, 5 failed (other groups' breakers only) | next: report
