"""Reads of the decision log for the API and the CLI (stub from P6-T9; P6-T12 implements it).

`resolve_run(None)` is the active live run (else the latest live run); an explicit id of a live or replay run
is that run; an unknown id is None. Without an explicit run id only a live run is ever served (replay
isolation). `load_day` orders rows by `seq`, filters by stage, outcome and ticker, and takes the summary from
the day's `day` row.
"""

from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from trader.decisions.types import DayItem, DayView, DecisionOutcome, DecisionStage


def resolve_run(factory: sessionmaker[Session], run_id: int | None) -> tuple[int, str] | None:
    raise NotImplementedError("P6-T12: resolve_run")


def list_days(factory: sessionmaker[Session], *, run_id: int | None, limit: int = 30) -> list[DayItem]:
    raise NotImplementedError("P6-T12: list_days")


def load_day(
    factory: sessionmaker[Session],
    run_id: int | None,
    session_date: date,
    *,
    stage: DecisionStage | None = None,
    outcome: DecisionOutcome | None = None,
    ticker: str | None = None,
    limit: int = 2000,
    offset: int = 0,
) -> DayView | None:
    raise NotImplementedError("P6-T12: load_day")
