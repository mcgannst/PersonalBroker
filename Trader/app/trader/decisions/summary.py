"""The day summary of a decision journal (stub from P6-T9; P6-T10 implements it).

`summarize` counts a day's rows into a `DaySummary`; `summary_text` renders it in at most 3 lines and 400
characters (times MT when shown), e.g. `812 scanned · 14 ranked · 1 passed (NVDA) · ...`.
"""

from collections.abc import Sequence
from datetime import date

from trader.decisions.types import DaySummary, DecisionRowView


def summarize(rows: Sequence[DecisionRowView], *, run_id: int, session_date: date, final: bool) -> DaySummary:
    raise NotImplementedError("P6-T10: summarize")


def summary_text(s: DaySummary, *, link: str | None) -> str:
    raise NotImplementedError("P6-T10: summary_text")
