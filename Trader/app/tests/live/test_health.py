"""DB-T6 acceptance tests 2 and 5: the Control page's health parsing (the worker heartbeat's `questrade`,
`candle_batches` and `marks` keys, plan S10) and the soak summary (read-only, cached 60 s)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

import trader.strategies.registry as registry_mod
from tests.fakes_api import make_services, test_core
from tests.jobs.test_soak import clean_rows
from trader.api.livedata import health
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs import soak
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.strategies.orb_sip import OrbSip
from trader.strategies.registry import StrategyRegistry
from trader.strategies.spy_overlay import SpyOverlay

CAL = SessionCalendar()
TUE = date(2026, 10, 6)  # a session


def et(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


def z(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


NOW = et(TUE, 10, 15)


def counts(**over: Any) -> dict[str, Any]:
    return {"requests": 120, "http_429": 2, "pause_s": 1.5, "http_5xx": 0, "transport_errors": 1, **over}


def questrade(**over: Any) -> dict[str, Any]:
    return {"day": "2026-10-06", "since": z(et(TUE, 4, 0)), "market": counts(), "account": counts(), **over}


def batch(started: datetime, **over: Any) -> dict[str, Any]:
    return {
        "started_at": z(started),
        "symbols": 550,
        "completed": 548,
        "errors": 0,
        "outstanding": 2,
        "elapsed_s": 31.2,
        "deadline_s": 60.0,
        "http_429": 3,
        "pause_s": 2.0,
        "raised": None,
        **over,
    }


@pytest.fixture(autouse=True)
def _fresh_soak_cache() -> Iterator[None]:
    health.clear_soak_cache()
    yield
    health.clear_soak_cache()


# --- 2. questrade_stats ---


def test_questrade_stats_from_a_well_formed_detail() -> None:
    detail = {"questrade": questrade(), "rate_limit": {"market_remaining": 17, "account_remaining": 29}}
    out = health.questrade_stats(detail, NOW)
    assert out is not None
    assert out.day == TUE and out.since == et(TUE, 4, 0)
    assert out.market.requests == 120 and out.market.http_429 == 2 and out.market.pause_s == 1.5
    assert out.account.transport_errors == 1
    assert out.rate_limit == {"market_remaining": 17, "account_remaining": 29}


def test_questrade_stats_of_yesterday_or_absent_is_none() -> None:
    assert health.questrade_stats({"questrade": questrade(day="2026-10-05")}, NOW) is None
    assert health.questrade_stats({"rate_limit": {"market_remaining": 1}}, NOW) is None
    assert health.questrade_stats(None, NOW) is None
    assert health.questrade_stats({}, NOW) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"questrade": "nope"},
        {"questrade": [1, 2]},
        {"questrade": questrade(day=20261006)},
        {"questrade": questrade(day="not a date")},
        {"questrade": questrade(since="yesterday")},
        {"questrade": questrade(since="2026-10-06T04:00:00")},  # no zone
        {"questrade": questrade(market=counts(requests="120"))},
        {"questrade": questrade(market=counts(pause_s="NaN"))},
        {"questrade": questrade(market=counts(pause_s=float("nan")))},
        {"questrade": questrade(market=counts(http_429=True))},
        {"questrade": questrade(account=[1, 2, 3])},
        {"questrade": questrade(account=None)},
    ],
)
def test_questrade_stats_ignores_malformed_values(bad: dict[str, Any]) -> None:
    assert health.questrade_stats(bad, NOW) is None


# --- 2. opening_bars ---


def test_the_first_batch_of_today_in_the_opening_window_is_the_opening_fetch() -> None:
    detail = {
        "candle_batches": [
            batch(et(TUE, 9, 34, 59), symbols=1),  # one second early: not the opening fetch
            batch(et(TUE, 9, 35, 0)),
            batch(et(TUE, 9, 36, 0), symbols=2),
        ]
    }
    out = health.opening_bars(detail, CAL, NOW)
    assert out is not None
    assert out.session_date == TUE and out.started_at == et(TUE, 9, 35)
    assert (out.symbols, out.completed, out.errors, out.outstanding) == (550, 548, 0, 2)
    assert out.elapsed_s == 31.2 and out.deadline_s == 60.0 and out.http_429 == 3 and out.pause_s == 2.0
    assert out.complete is False  # 2 outstanding
    assert out.raised is None


def test_a_batch_at_0934_59_is_not_the_opening_fetch() -> None:
    assert health.opening_bars({"candle_batches": [batch(et(TUE, 9, 34, 59))]}, CAL, NOW) is None
    assert health.opening_bars({"candle_batches": [batch(et(TUE, 9, 40, 0))]}, CAL, NOW) is None


def test_complete_and_raised() -> None:
    ok = health.opening_bars({"candle_batches": [batch(et(TUE, 9, 35), outstanding=0)]}, CAL, NOW)
    assert ok is not None and ok.complete is True
    cancelled = batch(et(TUE, 9, 35), outstanding=0, raised="CancelledError", deadline_s=None)
    out = health.opening_bars({"candle_batches": [cancelled]}, CAL, NOW)
    assert out is not None and out.complete is False and out.raised == "CancelledError"
    assert out.deadline_s is None
    errs = health.opening_bars({"candle_batches": [batch(et(TUE, 9, 35), outstanding=0, errors=1)]}, CAL, NOW)
    assert errs is not None and errs.complete is False


def test_yesterdays_batch_is_not_today() -> None:
    yesterday = date(2026, 10, 5)
    assert health.opening_bars({"candle_batches": [batch(et(yesterday, 9, 35))]}, CAL, NOW) is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"candle_batches": "x"},
        {"candle_batches": {"a": 1}},
        {"candle_batches": [1, "two", None]},
        {"candle_batches": [batch(et(TUE, 9, 35), symbols="550")]},
        {"candle_batches": [batch(et(TUE, 9, 35), elapsed_s="NaN")]},
        {"candle_batches": [batch(et(TUE, 9, 35), elapsed_s=float("inf"))]},
        {"candle_batches": [batch(et(TUE, 9, 35), started_at="09:35")]},
        {"candle_batches": [batch(et(TUE, 9, 35), raised=["x"])]},
        {"candle_batches": [batch(et(TUE, 9, 35), completed=[1])]},
    ],
)
def test_opening_bars_ignores_malformed_values(bad: dict[str, Any] | None) -> None:
    assert health.opening_bars(bad, CAL, NOW) is None


def test_a_malformed_entry_is_skipped_and_the_next_one_used() -> None:
    detail = {"candle_batches": [batch(et(TUE, 9, 35), symbols="x"), batch(et(TUE, 9, 36))]}
    out = health.opening_bars(detail, CAL, NOW)
    assert out is not None and out.started_at == et(TUE, 9, 36)


# --- 2. marks ---


def test_marks_health_from_the_detail() -> None:
    out = health.marks_health({"marks": {"written_at": z(NOW), "symbols": 3, "failing": False}})
    assert out is not None and out.written_at == NOW and out.symbols == 3 and out.failing is False
    none_yet = health.marks_health({"marks": {"written_at": None, "symbols": 0, "failing": True}})
    assert none_yet is not None and none_yet.written_at is None and none_yet.failing is True


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"marks": "x"},
        {"marks": [1]},
        {"marks": {"written_at": "soon", "symbols": 3, "failing": False}},
        {"marks": {"written_at": None, "symbols": "3", "failing": False}},
        {"marks": {"written_at": None, "symbols": 3, "failing": "no"}},
        {"marks": {"written_at": None, "symbols": True, "failing": False}},
    ],
)
def test_marks_health_ignores_malformed_values(bad: dict[str, Any] | None) -> None:
    assert health.marks_health(bad) is None


# --- health_panel (db) ---


@pytest.mark.db
def test_health_panel_reads_the_heartbeat_only(db_factory: sessionmaker[Session]) -> None:
    with session_scope(db_factory) as s:
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=7,
                host="trader-dev",
                started_at=NOW - timedelta(hours=3),
                beat_at=NOW - timedelta(seconds=95),
                session_date=TUE,
                phase="idle",
                detail={
                    "rate_limit": {"market_remaining": 17},
                    "questrade": questrade(),
                    "candle_batches": [batch(et(TUE, 9, 35))],
                    "marks": {"written_at": z(NOW), "symbols": 2, "failing": False},
                },
            )
        )
        s.add(
            m.Notification(
                kind="fill",
                text="never shown",
                created_at=NOW,
                status="failed",
                attempts=2,
                error="password=hunter2 refused",
            )
        )
    services = make_services(test_core(db_factory, FixedClock(NOW)), telegram_configured=False)
    out = health.health_panel(services, NOW)
    assert out.worker.phase == "idle" and out.worker_stale is True  # 95 s > 60 s
    assert out.token.ok is True
    assert out.db_ok is True and out.db_latency_ms is not None
    assert out.telegram_configured is False
    assert out.questrade is not None and out.questrade.rate_limit == {"market_remaining": 17}
    assert out.opening_bars is not None and out.opening_bars.symbols == 550
    assert out.marks is not None and out.marks.symbols == 2
    assert len(out.notifications_failed) == 1
    assert "hunter2" not in (out.notifications_failed[0].error or "")


@pytest.mark.db
def test_health_panel_without_a_heartbeat_and_with_a_failing_database_check(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(factory: object) -> int:
        raise RuntimeError("database down")

    monkeypatch.setattr(health, "db_check", broken)
    services = make_services(test_core(db_factory, FixedClock(NOW)))
    out = health.health_panel(services, NOW)
    assert out.worker.ok is False and out.worker_stale is True
    assert out.db_ok is False and out.db_latency_ms is None
    assert out.questrade is None and out.opening_bars is None and out.marks is None


# --- 5. the soak summary (db) ---

MON, TUE2, WED = date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)


@pytest.fixture
def soak_services(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Any:
    plugins = {"orb_sip": OrbSip, "spy_overlay": SpyOverlay}
    clock = FixedClock(et(WED, 20, 0))
    monkeypatch.setattr(registry_mod, "load_all", lambda: plugins)
    StrategyRegistry(db_factory, clock, plugins=plugins).ensure_defaults()
    return make_services(test_core(db_factory, clock))


def _seed_clean(factory: sessionmaker[Session], d: date) -> None:
    with session_scope(factory) as s:
        for r in clean_rows(d):
            s.add(
                m.JobRun(
                    job=r.job,
                    session_date=d,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    status=r.status,
                    error=r.error,
                )
            )


@pytest.mark.db
def test_soak_summary_counts_the_clean_run_after_a_reset(
    db_factory: sessionmaker[Session], soak_services: Any
) -> None:
    for d in (MON, TUE2, WED):
        _seed_clean(db_factory, d)
    soak.record_mark(db_factory, FixedClock(et(MON, 17, 0)), None, "reset", MON, "new code deployed")
    now = et(WED, 20, 0)
    out = health.soak_summary(soak_services, now)
    assert out.target == 10
    assert out.consecutive_clean == 3
    assert out.day_one == MON
    assert out.last_final == WED
    assert out.earliest_finish is not None and out.earliest_finish > WED
    assert out.today is not None and out.today.session_date == WED and out.today.verdict == "clean"
    assert out.today.failed == [] and out.today.provisional is False
    assert out.generated_at == now


@pytest.mark.db
def test_soak_summary_is_cached_and_read_only(db_factory: sessionmaker[Session], soak_services: Any) -> None:
    _seed_clean(db_factory, WED)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, sql: str, *args: Any) -> None:
        statements.append(sql)

    engine = db_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        now = et(WED, 20, 0)
        first = health.soak_summary(soak_services, now)
        assert statements, "the first call reads the database"
        assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements), statements
        statements.clear()
        second = health.soak_summary(soak_services, now + timedelta(seconds=59))
        assert statements == []  # within 60 s: the cache
        assert second == first
        health.soak_summary(soak_services, now + timedelta(seconds=61))
        assert statements  # expired: read again
    finally:
        event.remove(engine, "before_cursor_execute", record)
    with db_factory() as s:  # never a run, a job_runs row or an event
        assert s.query(m.Run).count() == 0
        assert s.query(m.EventLog).count() == 0


@pytest.mark.db
def test_soak_summary_without_clean_days_and_on_a_weekend(
    db_factory: sessionmaker[Session], soak_services: Any
) -> None:
    saturday = et(date(2026, 10, 3), 12, 0)
    out = health.soak_summary(soak_services, saturday)
    assert out.consecutive_clean == 0 and out.day_one is None
    assert out.today is None  # Saturday is not a session
