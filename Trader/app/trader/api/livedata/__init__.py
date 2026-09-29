"""The live dashboard's and Control page's read models (live dashboard plan S3–S16).

Each module computes one part of `GET /api/live` or `GET /api/control`, synchronously, in the route's worker
thread. Every function reads only the database and read-only functions of decision-path objects
(`KillSwitches.active`/`inputs`/`blocking`, `Ledger.balances`, `SessionCalendar`, `StrategyRegistry`
lookups); none calls Questrade (D8) or writes anything. Prices come from `quote_marks`/`mark_bars` and stored
candles only. Run-scoped rows are filtered to the live run; `event_log` rows through
`trader.api.feed.live_or_unscoped`.

Modules: `types` (constants and internal types), `periods`, `books`, `equity` (DB-T3), `positions`, `risk`
(DB-T4), `activity` (DB-T5), `control`, `health` (DB-T6).
"""
