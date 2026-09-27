# P3-T10T11 builder attempt 2
- 11:14:53 MT started fix round, synced trunk | next: read code and breaker tests
- 11:19:28 MT fixed checkin/events isolation, postclose isolation+summary status+nonce skip, batched 5m upsert, stopping phase; jobs+market+breaker tests green (185) | next: regression tests
- 11:22:09 MT regression tests added (events 3, checkin 3, postclose 6, preopen 2), jobs+breaker 147 passed | next: plan doc update, check.sh
- 11:36:02 MT committed and pushed c4ed413 (check.sh: 1352 passed, 16 failed all in other groups' breaker files) | next: finished entry, report
