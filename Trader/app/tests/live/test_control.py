"""DB-T6 acceptance tests 1, 3, 4 and 6: `job_summary`, the schedule, the engine and strategy cards and the
error log of the Control page (`trader.api.livedata.control`)."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_api import make_services, test_core
from trader.api.deps import ApiServices
from trader.api.livedata import control, risk
from trader.api.schemas import OpeningBarsOut
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.scheduler import DayPlan, PlannedEvent
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.strategies.orb_sip import OrbSip
from trader.strategies.registry import StrategyRegistry
from trader.strategies.spy_overlay import SpyOverlay

CAL = SessionCalendar()
TUE = date(2026, 10, 6)  # a session
SAT, MON = date(2026, 10, 10), date(2026, 10, 12)


def et(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


def batch(**over: Any) -> OpeningBarsOut:
    values: dict[str, Any] = {
        "session_date": TUE,
        "started_at": et(TUE, 9, 35),
        "symbols": 550,
        "completed": 548,
        "errors": 0,
        "outstanding": 2,
        "elapsed_s": 31.2,
        "deadline_s": 60.0,
        "http_429": 0,
        "pause_s": 0.0,
        "complete": False,
        "raised": None,
        **over,
    }
    return OpeningBarsOut(**values)


# --- 1. job_summary ---


def test_the_opening_bar_batch_summary() -> None:
    assert control.job_summary("event:orb_open", {"x": 1}, batch()) == "548 of 550 bars in 31.2 s"
    assert (
        control.job_summary("event:orb_open", None, batch(http_429=4)) == "548 of 550 bars in 31.2 s, 4 x 429"
    )
    assert control.job_summary("event:orb_open", None, batch(elapsed_s=31.0)) == "548 of 550 bars in 31 s"


def test_the_batch_is_only_for_orb_open() -> None:
    assert control.job_summary("premarket", {"candidates": 12}, batch()) == "candidates 12"


def test_skipped_and_error() -> None:
    assert (
        control.job_summary("premarket", {"skipped": "not a session", "n": 1}, None)
        == "skipped: not a session"
    )
    long = "boom password=hunter2 " + "x" * 200
    text = control.job_summary("postclose", {"error": long}, None)
    assert text is not None
    assert "hunter2" not in text and text.startswith("boom password=")
    assert len(text) == 80


def test_generic_scalars_in_key_order() -> None:
    detail = {
        "universe": 543,
        "notes": "y" * 41,  # longer than 40: skipped
        "ratio": 0.5,
        "list": [1, 2],
        "ok": True,
        "tail": "shown? no, a fourth",
    }
    assert control.job_summary("nightly", detail, None) == "universe 543 · ratio 0.5 · ok true"
    assert control.job_summary("nightly", {"mode": "auto"}, None) == "mode auto"


def test_masked_generic_strings() -> None:
    text = control.job_summary("nightly", {"url": "token=abc123secret"}, None)
    assert text is not None and "abc123secret" not in text


@pytest.mark.parametrize("detail", [None, {}, [], "text", 5, {"list": [1], "nested": {"a": 1}}])
def test_none_cases(detail: Any) -> None:
    assert control.job_summary("nightly", detail, None) is None


# --- 3. the schedule (db) ---


def plan_for(d: date) -> DayPlan:
    """orb_open at 09:35 and flatten at 15:50 on every session."""
    if not CAL.is_session(d):
        return DayPlan(d, False, None, None, ())
    events = (
        PlannedEvent("orb_open", et(d, 9, 35), ("orb_sip",), False),
        PlannedEvent("flatten", et(d, 15, 50), ("orb_sip",), True),
    )
    return DayPlan(d, True, CAL.session_open(d), CAL.session_close(d), events)


def _job(
    s: Session,
    job: str,
    d: date,
    started: datetime,
    seconds: float | None,
    status: str,
    *,
    error: str | None = None,
    detail: Any = None,
) -> None:
    s.add(
        m.JobRun(
            job=job,
            session_date=d,
            started_at=started,
            finished_at=started + timedelta(seconds=seconds) if seconds is not None else None,
            status=status,
            error=error,
            detail=detail,
        )
    )


def _services(factory: sessionmaker[Session], now: datetime, **over: Any) -> ApiServices:
    return make_services(test_core(factory, FixedClock(now)), **{"plan": plan_for, **over})


def heartbeat_detail() -> dict[str, Any]:
    return {
        "candle_batches": [
            {
                "started_at": "2026-10-06T13:35:00Z",  # 09:35 ET
                "symbols": 550,
                "completed": 550,
                "errors": 0,
                "outstanding": 0,
                "elapsed_s": 28.4,
                "deadline_s": 60.0,
                "http_429": 1,
                "pause_s": 1.0,
                "raised": None,
            }
        ]
    }


@pytest.mark.db
def test_the_schedule_of_a_session_day(db_factory: sessionmaker[Session]) -> None:
    now = et(TUE, 12, 0)
    with session_scope(db_factory) as s:
        _job(s, "nightly", TUE, et(date(2026, 10, 5), 20, 0), 240, "succeeded", detail={"universe": 543})
        _job(s, "premarket", TUE, et(TUE, 8, 0), 30, "failed", error="timeout")
        _job(s, "premarket", TUE, et(TUE, 8, 5), 180, "succeeded", detail={"candidates": 12})
        _job(s, "preopen", TUE, et(TUE, 9, 20), 40, "succeeded", detail={"skipped": "no candidates"})
        _job(s, "event:orb_open", TUE, et(TUE, 9, 35, 5), 3, "failed", error="db error")
        _job(s, "event:orb_open", TUE, et(TUE, 9, 36), 31, "succeeded", detail={"orders": 1})
        _job(s, "checkin@11:30", TUE, et(TUE, 11, 30), 20, "succeeded")
        _job(s, "premarket", date(2026, 10, 5), et(date(2026, 10, 5), 8, 0), 10, "failed")  # another day
    items = control.schedule(_services(db_factory, now), now, heartbeat_detail())
    by_key = {i.key: i for i in items}
    assert [i.key for i in items] == [
        "nightly",
        "premarket",
        "preopen",
        "orb_open",
        "checkin_1130",
        "checkin_1330",
        "flatten",
        "postclose",
    ]
    pre = by_key["premarket"]
    assert pre.status == "done" and pre.attempts == 2
    assert pre.started_at == et(TUE, 8, 5) and pre.finished_at == et(TUE, 8, 8)
    assert pre.duration_seconds == 180.0
    assert pre.summary == "candidates 12"
    assert pre.rerun == "premarket"
    assert by_key["nightly"].rerun == "nightly" and by_key["nightly"].summary == "universe 543"
    assert by_key["preopen"].summary == "skipped: no candidates" and by_key["preopen"].rerun == "preopen"
    orb = by_key["orb_open"]
    assert orb.kind == "event" and orb.status == "done" and orb.attempts == 2
    assert orb.summary == "550 of 550 bars in 28.4 s, 1 x 429"
    assert orb.rerun is None
    assert by_key["checkin_1130"].status == "done" and by_key["checkin_1130"].rerun is None
    assert by_key["checkin_1330"].status == "next" and by_key["checkin_1330"].attempts == 0
    assert by_key["checkin_1330"].started_at is None and by_key["checkin_1330"].duration_seconds is None
    post = by_key["postclose"]
    assert post.status == "upcoming" and post.rerun == "postclose" and post.attempts == 0
    assert by_key["flatten"].status == "upcoming"


@pytest.mark.db
def test_a_running_job_has_no_duration(db_factory: sessionmaker[Session]) -> None:
    now = et(TUE, 8, 1)
    with session_scope(db_factory) as s:
        _job(s, "premarket", TUE, et(TUE, 8, 0), None, "running")
    items = {i.key: i for i in control.schedule(_services(db_factory, now), now, None)}
    assert items["premarket"].status == "running"
    assert items["premarket"].finished_at is None and items["premarket"].duration_seconds is None


@pytest.mark.db
def test_on_a_saturday_the_schedule_is_mondays(db_factory: sessionmaker[Session]) -> None:
    now = et(SAT, 12, 0)
    with session_scope(db_factory) as s:  # Friday's rows are not Monday's
        _job(s, "premarket", date(2026, 10, 9), et(date(2026, 10, 9), 8, 0), 10, "succeeded")
    items = control.schedule(_services(db_factory, now), now, heartbeat_detail())
    assert items and all(i.at.astimezone(ET).date() in (date(2026, 10, 11), MON) for i in items)
    assert items[0].key == "nightly" and items[0].at == et(date(2026, 10, 11), 20, 0)
    assert [i.status for i in items] == ["next"] + ["upcoming"] * (len(items) - 1)
    assert all(i.attempts == 0 and i.summary is None for i in items)  # today's batch is not Monday's


@pytest.mark.db
def test_the_plan_failing_leaves_the_jobs(db_factory: sessionmaker[Session]) -> None:
    def broken(d: date) -> DayPlan:
        raise RuntimeError("plan down")

    now = et(TUE, 12, 0)
    items = control.schedule(_services(db_factory, now, plan=broken), now, None)
    assert {i.kind for i in items} == {"job"} and len(items) == 6


# --- 4. engine and strategies (db) ---

PLUGINS = {"orb_sip": OrbSip, "spy_overlay": SpyOverlay}


@pytest.mark.db
def test_the_engine_card_with_a_manual_pause(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = et(TUE, 10, 0)
    with session_scope(db_factory) as s:
        run_id = add_run(s, started_at=et(date(2026, 9, 28), 18, 0))  # Monday evening ET
    services = _services(db_factory, now)
    assert services.killswitches.pause(run_id, TUE, "web:stephen")
    monkeypatch.setattr(risk, "trading_state", lambda ks, rid, day: "paused")
    card = control.engine_card(services, run_id, now)
    assert card.trading == "paused"
    assert card.paused_at == now
    assert card.approval_mode == "manual"
    assert card.run_id == run_id
    assert card.run_started_at == et(date(2026, 9, 28), 18, 0)
    assert card.run_start_date == date(2026, 9, 28)
    assert card.version == "dev" and card.app_env == "dev"
    assert card.alembic_revision is not None


@pytest.mark.db
def test_the_engine_card_running(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    now = et(TUE, 10, 0)
    with session_scope(db_factory) as s:
        run_id = add_run(s, started_at=et(date(2026, 9, 28), 23, 30))  # 03:30Z on the 29th
    calls: list[tuple[int, date]] = []

    def state(ks: object, rid: int, day: date) -> str:
        calls.append((rid, day))
        return "running"

    monkeypatch.setattr(risk, "trading_state", state)
    card = control.engine_card(_services(db_factory, now), run_id, now)
    assert card.trading == "running" and card.paused_at is None
    assert card.run_start_date == date(2026, 9, 28)  # the ET date, not the UTC one
    assert calls == [(run_id, TUE)]


@pytest.mark.db
def test_the_strategy_cards(db_factory: sessionmaker[Session]) -> None:
    now = et(TUE, 10, 0)
    with session_scope(db_factory) as s:
        run_id = add_run(s)
        orb = add_strategy_config(s, "orb_sip", params={"max_positions": 2})
        add_strategy_config(s, "orb_sip", revision=2, params={"max_positions": 3}, created_at=now)
        add_strategy_config(s, "spy_overlay", enabled=False)
        sym = add_symbol(s, "AAPL")
        s.add(
            m.Position(
                run_id=run_id,
                symbol_id=sym,
                strategy_config_id=orb,  # an older revision still counts
                qty=10,
                avg_price=100,
                session_date=TUE,
                opened_at=now,
                entry_order_id=1,
                unprotected_seconds=0,
            )
        )
    services = _services(
        db_factory, now, registry=StrategyRegistry(db_factory, FixedClock(now), plugins=PLUGINS)
    )
    cards = {c.key: c for c in control.strategy_cards(services, run_id)}
    assert set(cards) == {"orb_sip", "spy_overlay"}
    orb_card = cards["orb_sip"]
    assert orb_card.kind == "entry" and orb_card.enabled is True and orb_card.revision == 2
    assert orb_card.version == "1.0.0" and orb_card.updated_at == now and orb_card.updated_by == "test"
    assert orb_card.owns_open_positions is True
    assert orb_card.max_positions == 3
    assert orb_card.settings_link == "/settings#strategies"
    spy = cards["spy_overlay"]
    assert spy.kind == "overlay" and spy.enabled is False
    assert spy.owns_open_positions is False and spy.max_positions is None


@pytest.mark.db
def test_a_strategy_without_settings_is_left_out(db_factory: sessionmaker[Session]) -> None:
    now = et(TUE, 10, 0)
    with session_scope(db_factory) as s:
        run_id = add_run(s)
        add_strategy_config(s, "orb_sip")
    services = _services(
        db_factory, now, registry=StrategyRegistry(db_factory, FixedClock(now), plugins=PLUGINS)
    )
    assert [c.key for c in control.strategy_cards(services, run_id)] == ["orb_sip"]


# --- 6. the error log (db) ---


@pytest.mark.db
def test_the_error_log(db_factory: sessionmaker[Session]) -> None:
    t0 = et(TUE, 9, 0)
    with session_scope(db_factory) as s:
        live = add_run(s)
        replay = add_run(s, mode="replay", status="done", label="r")
        s.add(m.EventLog(ts=t0, level="warning", source="worker", message="w1", run_id=live))
        s.add(m.EventLog(ts=t0, level="info", source="worker", message="info row", run_id=live))
        s.add(m.EventLog(ts=t0, level="error", source="log.api", message="password=hunter2 bad", run_id=None))
        s.add(m.EventLog(ts=t0, level="critical", source="replay", message="replay row", run_id=replay))
        s.add(m.EventLog(ts=t0, level="debug", source="worker", message="debug row", run_id=None))
        s.add(m.EventLog(ts=t0, level="critical", source="engine", message="c1", run_id=None))
    rows = control.error_log(db_factory)
    assert [r.message for r in rows] == ["c1", "password=[REDACTED] bad", "w1"]
    assert [r.level for r in rows] == ["critical", "error", "warning"]
    assert rows[1].source == "log.api"


@pytest.mark.db
def test_the_error_log_limit(db_factory: sessionmaker[Session]) -> None:
    t0 = et(TUE, 9, 0)
    with session_scope(db_factory) as s:
        for i in range(205):
            s.add(m.EventLog(ts=t0 + timedelta(seconds=i), level="warning", source="w", message=f"m{i}"))
    rows = control.error_log(db_factory)
    assert len(rows) == 200 and rows[0].message == "m204" and rows[-1].message == "m5"
    assert [r.id for r in rows] == sorted((r.id for r in rows), reverse=True)
    assert len(control.error_log(db_factory, limit=3)) == 3
