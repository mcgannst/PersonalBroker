# P3-OUTAGE builder attempt 3
- 12:35:46 MT started, synced trunk | next: log BUILD_STATE, read relay/bot/notifier
- 12:41:49 MT read relay/bot/notifier/tests. Design: relay ordered backlog walk (failed notifications + pending proposals by created_at), 30s backoff, streams held while Telegram presumed down. Bot: surely-not-sent discards nonce, retries next pump | next: implement bot.py
- 12:46:44 MT bot + relay implemented, xfail removed, both outage integration tests pass | next: unit tests for rulings 1-3, update existing bot tests
- 12:52:34 MT unit tests added (relay 8, bot 6 incl params), plan T6/T8 notes written | next: full check.sh
- 13:00:02 MT check.sh green on trunk 1befdf1 (1580 passed), committed and pushed 707ef23 | next: log finished, report
