"""Replay mode (SPEC §8): the unchanged Phase 2 engine stepped through past sessions from 1-minute candles.

`types` holds the shared contracts (P5-T1); `clock` and `candle_fill_model` (P5-T3), `data` and `catalysts`
(P5-T5), `setup` and `runner` (P5-T6) implement them. Replay code never imports `anthropic`, the Telegram
adapter, the notifier or the relay, and never runs a job through `run_job` / `fire_event`.
"""
