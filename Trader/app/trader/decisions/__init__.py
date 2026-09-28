"""The decision log (Phase 6 amendment): a per-day journal of every trading decision and its reasons,
built from the rows the engine already writes (SPEC §10 `decision_log`).

Modules: `types` (contracts, P6-T9), `recorder`, `orb_explain`, `summary`, `prune` (P6-T10), `read`, `export`
(P6-T12), `loop` (P6-T11). Logging only: it writes nothing but `decision_log`, and no decision-path module
(strategies, engine, broker, market, nightly, premarket, the catalyst classifier, settings store) imports it.
"""
