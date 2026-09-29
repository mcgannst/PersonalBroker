"""DB-T1 acceptance test 6: the change feed's `marks` and `activity` topics (live dashboard plan S9).

`marks` follows the live run's `quote_marks.written_at`, `activity` the live run's `decision_log.id`; a replay
run's rows change neither, and the watermark query is still one statement.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from trader.api.feed import WATERMARK_TOPICS, watermarks
from trader.api.schemas import Topic
from trader.db import models as m

pytestmark = pytest.mark.db

T0 = datetime(2026, 9, 29, 13, 40, tzinfo=UTC)
DAY = date(2026, 9, 29)


def _marks(factory: sessionmaker[Session]) -> dict[Topic, tuple[Any, ...]]:
    with factory() as s:
        return watermarks(s)


def _runs(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with factory() as s:
        live = add_run(s, mode="live")
        replay = add_run(s, mode="replay", status="completed", label="replay")
        sym = add_symbol(s, "AAPL", questrade_id=8049)
        s.commit()
    return live, replay, sym


def _quote_mark(s: Session, run_id: int, symbol_id: int, written_at: datetime) -> None:
    s.merge(m.QuoteMark(run_id=run_id, symbol_id=symbol_id, observed_at=written_at, written_at=written_at))


def _decision(s: Session, run_id: int, seq: int) -> None:
    s.add(
        m.DecisionLog(
            run_id=run_id, session_date=DAY, seq=seq, stage="scan", outcome="passed", ts=T0, recorded_at=T0
        )
    )


def test_the_topics_are_watermarked_last() -> None:
    assert WATERMARK_TOPICS[-2:] == ("marks", "activity")


def test_watermarks_return_the_new_topics_in_one_statement(db_factory: sessionmaker[Session]) -> None:
    statements: list[str] = []
    engine = db_factory.kw["bind"]

    def record(*args: Any) -> None:
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", record)
    try:
        marks = _marks(db_factory)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1
    assert marks["marks"] == (None,) and marks["activity"] == (None,)


def test_a_live_quote_mark_changes_marks_and_a_replay_one_does_not(db_factory: sessionmaker[Session]) -> None:
    live, replay, sym = _runs(db_factory)
    with db_factory() as s:
        _quote_mark(s, replay, sym, T0 + timedelta(minutes=5))
        s.commit()
    assert _marks(db_factory)["marks"] == (None,)  # a replay's row is invisible
    with db_factory() as s:
        _quote_mark(s, live, sym, T0)
        s.commit()
    first = _marks(db_factory)
    assert first["marks"] == (T0,)
    with db_factory() as s:
        _quote_mark(s, replay, sym, T0 + timedelta(minutes=9))
        s.commit()
    assert _marks(db_factory) == first  # nothing changed for the live pages
    with db_factory() as s:
        _quote_mark(s, live, sym, T0 + timedelta(seconds=2))  # the upsert of the next pass
        s.commit()
    after = _marks(db_factory)
    assert after["marks"] == (T0 + timedelta(seconds=2),)
    assert [t for t in WATERMARK_TOPICS if after[t] != first[t]] == ["marks"]


def test_a_live_decision_row_changes_activity_and_a_replay_one_does_not(
    db_factory: sessionmaker[Session],
) -> None:
    live, replay, _ = _runs(db_factory)
    with db_factory() as s:
        _decision(s, replay, 1)
        s.commit()
    before = _marks(db_factory)
    assert before["activity"] == (None,)
    with db_factory() as s:
        _decision(s, replay, 2)
        s.commit()
    assert _marks(db_factory) == before
    with db_factory() as s:
        _decision(s, live, 1)
        s.commit()
    after = _marks(db_factory)
    assert after["activity"] != before["activity"] and after["activity"][0] is not None
    assert [t for t in WATERMARK_TOPICS if after[t] != before[t]] == ["activity"]
