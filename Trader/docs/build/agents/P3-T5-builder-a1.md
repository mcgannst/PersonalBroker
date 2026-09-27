# P3-T5 builder attempt 1
- 10:52 MT started, read plans, logged BUILD_STATE | next: read contracts and stubs
- 10:56:24 MT read contracts, fakes, PTB internals (Bot.initialize leaks token in InvalidToken, BadRequest strips 'Bad Request:' prefix) | next: write api acceptance tests 1-4
- 10:58:34 MT api tests 1-4 written (red), PtbTelegramApi implemented, 20 api tests green, mypy clean | next: notifier tests 5-10
- 11:00:42 MT notifier tests 5-10 + impl green (17), ruff/mypy clean, plan ticked | next: run check.sh
- 11:04:42 MT check.sh green (1032 passed), committed and pushed c12a3e6 to trunk | next: log finished, report
- 11:04:50 MT logged finished in BUILD_STATE | next: done
