"""The worker's quote marks (live dashboard plan S1, S1a, S2; D8, D10).

- `trader.marks.tap.QuoteTap` wraps the worker's shared Questrade client at the composition root: a
  transparent pass-through that remembers each quote the worker already fetched (synchronous bookkeeping
  only: no task, lock, timeout, sleep, thread or I/O of its own).
- `trader.marks.publisher.MarkPublisher` is a supervised worker task that drains the tap every 2 s and, in its
  own single-thread executor, writes `quote_marks` and `mark_bars` for the live run's held and working
  symbols. It never calls Questrade, and nothing on the decision path reads its tables.

Import direction: no decision-path module imports this package; it imports from the decision path only
read-only types, protocols and constants (tests/live/test_contracts.py).
"""
