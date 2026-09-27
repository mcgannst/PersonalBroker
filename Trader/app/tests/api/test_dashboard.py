"""P4-T5 acceptance tests 1-6: `GET /api/dashboard` on a real test database with seeded rows, fake quotes and
the real day plan of the default strategies (orb_sip, spy_overlay); `DAY_JOBS` against `docker/crontab`."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.api.test_trading import at_et, live_run, seed_closed_trade, seed_open_position
from tests.factories import add_symbol
from tests.fakes_api import fake_quotes, make_services, test_core
from tests.fakes_telegram import FakeIssuer, FakeMessenger, FakeRenderer
from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.adapters.telegram.commands import CommandDeps, pnl_view
from trader.api.routers import dashboard, trading
from trader.api.routers.dashboard import DAY_JOBS
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.scheduler import DayPlan, day_plan, event_job
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.notify import views
from trader.settings_store import RuntimeSettings
from trader.strategies.base import Strategy
from trader.strategies.orb_sip import OrbSip, OrbSipParams
from trader.strategies.spy_overlay import SpyOverlay, SpyOverlayParams

pytestmark = pytest.mark.db

CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tuesday
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # 10:00 ET
CRONTAB = Path(__file__).resolve().parents[3] / "docker" / "crontab"
STRATEGIES: list[Strategy] = [OrbSip(OrbSipParams()), SpyOverlay(SpyOverlayParams())]


def real_plan(d: date) -> DayPlan:
    return day_plan(STRATEGIES, CAL, d, RuntimeSettings())


def _client(factory: sessionmaker[Session], clock: FixedClock, **overrides: Any) -> TestClient:
    overrides.setdefault("plan", real_plan)
    services = make_services(test_core(factory, clock), **overrides)
    return make_client(services, dashboard.router, trading.router)


def _job(
    s: Session, job: str, status: str, *, day: date = DAY, error: str | None = None, at: datetime = NOW
) -> None:
    s.add(
        m.JobRun(
            job=job,
            session_date=day,
            started_at=at,
            finished_at=at if status != "running" else None,
            status=status,
            error=error,
            detail=None,
        )
    )


def _pending_proposal(s: Session, run_id: int) -> m.Proposal:
    sym = add_symbol(s, "PEND")
    cfg = s.query(m.StrategyConfig).filter_by(strategy_key="orb_sip").one()
    sig = m.Signal(
        run_id=run_id, strategy_config_id=cfg.id, symbol_id=sym, session_date=DAY, event_key="orb_open",
        ts=NOW, intent={"reason": "breakout"}, evidence={},
    )  # fmt: skip
    s.add(sig)
    s.flush()
    p = m.Proposal(
        run_id=run_id,
        signal_id=sig.id,
        kind="entry",
        order_spec={
            "symbol_id": sym,
            "side": "buy",
            "order_type": "stop",
            "stop": "21.55",
            "stop_loss": "21.41",
        },
        qty=33,
        status="pending",
        created_at=NOW - timedelta(seconds=20),
        expires_at=NOW + timedelta(seconds=70),
        sizing={"per_share_risk": "0.14"},
        escalations=0,
    )
    s.add(p)
    s.flush()
    return p


def _by_key(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in body["timeline"]}


# --- 1. a session day at 10:00 ET ---------------------------------------------------------------------------


def test_dashboard_on_a_session_morning(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        open_pos = seed_open_position(s, run_id, ticker="BBB")
        pending = _pending_proposal(s, run_id)
        _job(s, "nightly", "succeeded", at=at_et(date(2026, 10, 5), 20, 0))
        _job(s, "premarket", "succeeded", at=at_et(DAY, 8, 0))
        _job(s, "preopen", "succeeded", at=at_et(DAY, 9, 20))
        _job(s, event_job("orb_open"), "succeeded", at=at_et(DAY, 9, 35))
        s.commit()
        expected_risk = views.proposal_view(s, s.get(m.Proposal, pending.id)).risk_usd  # type: ignore[arg-type]
    client = _client(
        db_factory,
        clock,
        fired=lambda d: {"orb_open"},
        quotes=fake_quotes({open_pos.symbol_id: Decimal("20.30")}),
    )
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id and body["server_time"] == "2026-10-06T14:00:00Z"
    assert body["session"] == {
        "date": "2026-10-06",
        "phase": "open",
        "is_session": True,
        "open_at": "2026-10-06T13:30:00Z",
        "close_at": "2026-10-06T20:00:00Z",
    }
    assert body["approval_mode"] == "manual" and body["telegram_configured"] is True
    assert [p["id"] for p in body["pending"]] == [pending.id]
    assert body["pending"][0]["risk_usd"] == str(expected_risk) == "4.62"
    assert body["pending"][0]["ticker"] == "PEND" and body["pending"][0]["strategy_key"] == "orb_sip"
    [pos] = body["positions"]
    assert pos["id"] == open_pos.position_id and pos["stop_working"] is True and pos["stop"] == "19.6000"
    assert pos["last"] == "20.30" and pos["strategy_key"] == "orb_sip" and pos["status"] == "open"
    timeline = _by_key(body)
    assert timeline["premarket"]["status"] == "done" and timeline["preopen"]["status"] == "done"
    assert timeline["nightly"]["status"] == "done"
    assert timeline["orb_open"]["status"] == "done" and timeline["orb_open"]["kind"] == "event"
    assert timeline["entry_cancel"]["status"] == "next"
    assert [i["key"] for i in body["timeline"] if i["status"] == "next"] == ["entry_cancel"]
    assert (
        timeline["checkin_1130"]["status"] == "upcoming" and timeline["checkin_1330"]["status"] == "upcoming"
    )
    assert timeline["flatten"]["status"] == "upcoming" and timeline["postclose"]["status"] == "upcoming"
    assert timeline["premarket"]["at"] == "2026-10-06T12:00:00Z" and timeline["premarket"]["kind"] == "job"
    ats = [i["at"] for i in body["timeline"]]
    assert ats == sorted(ats)
    assert all(i["label"] for i in body["timeline"])
    assert [k["switch"] for k in body["killswitches"]] == [
        "daily_loss_pct",
        "max_drawdown_pct",
        "expectancy",
        "manual_pause",
    ]
    assert body["token"]["seeded"] is True and body["worker"]["ok"] is False


# --- 2. the web and Telegram cannot disagree ----------------------------------------------------------------


async def test_numbers_match_telegram_positions_and_pnl(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(datetime(2026, 10, 7, 15, 0, tzinfo=UTC))  # Wednesday 11:00 ET
    wed = date(2026, 10, 7)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        seed_closed_trade(s, run_id, day=date(2026, 10, 5), ticker="MON", pnl="-4.2500")  # this week
        seed_closed_trade(s, run_id, day=date(2026, 10, 2), ticker="FRI", pnl="100.0000")  # last week
        seed_closed_trade(s, run_id, day=wed, ticker="WED", pnl="7.1000")
        priced = seed_open_position(s, run_id, day=wed, ticker="PRC", opened_at=at_et(wed, 9, 40))
        unpriced = seed_open_position(s, run_id, day=wed, ticker="NOQ", working_stop=False)
        s.add(
            m.EquitySnapshot(
                run_id=run_id, ts=at_et(wed, 10, 0), equity=Decimal("10050.0000"),
                cash=Decimal("9000"), settled_cash=Decimal("9000"), peak_equity=Decimal("10100.0000"),
                drawdown_pct=Decimal("0.0050"),
            )
        )  # fmt: skip
        s.execute(
            update(m.Position)
            .where(m.Position.id == unpriced.position_id)
            .values(unprotected_since=at_et(wed, 10, 30))
        )
        s.commit()
    quotes = fake_quotes({priced.symbol_id: Decimal("20.75")})
    body = _client(db_factory, clock, quotes=quotes).get("/api/dashboard").json()

    lines = await views.position_lines(db_factory, clock, run_id, quotes)
    assert [p["id"] for p in body["positions"]] == [ln.position_id for ln in lines]
    for got, line in zip(body["positions"], lines, strict=True):
        assert got["ticker"] == line.ticker and got["qty"] == line.qty
        assert Decimal(got["entry"]) == line.entry
        assert (Decimal(got["last"]) if got["last"] is not None else None) == line.last
        assert (Decimal(got["stop"]) if got["stop"] is not None else None) == line.stop
        assert got["stop_working"] == line.stop_working
        assert got["unprotected_seconds"] == line.unprotected_seconds
        want = line.unrealized_pnl
        assert (Decimal(got["unrealized_pnl"]) if got["unrealized_pnl"] is not None else None) == want
    noq = next(p for p in body["positions"] if p["id"] == unpriced.position_id)
    assert noq["unprotected_seconds"] == 4 + 30 * 60  # 10:30 -> 11:00 plus the stored 4 s
    assert noq["stop_working"] is False and noq["stop"] == "19.5000"

    deps = CommandDeps(
        factory=db_factory,
        clock=clock,
        calendar=CAL,
        settings=RuntimeSettings,
        killswitches=KillSwitches(db_factory, clock),
        run_id=run_id,
        chat_id=1,
        plan=real_plan,
        fired=lambda d: set(),
        token_health=lambda: TokenHealth(True, None, None, None),
        quotes=quotes,
        messenger=FakeMessenger(),
        issuer=FakeIssuer(),
        render=FakeRenderer(),
    )
    tg = await pnl_view(deps)
    pnl = body["pnl"]
    assert pnl["session_date"] == tg.session_date.isoformat() == "2026-10-07"
    assert Decimal(pnl["realized_today"]) == tg.realized_today == Decimal("7.1")
    assert Decimal(pnl["week_to_date"]) == tg.week_to_date == Decimal("2.85")
    assert Decimal(pnl["unrealized"]) == tg.unrealized == Decimal("7.5")
    assert pnl["unrealized_partial"] is True  # NOQ has no quote
    assert Decimal(pnl["equity"]) == tg.equity and Decimal(pnl["peak_equity"]) == tg.peak_equity
    assert Decimal(pnl["drawdown_pct"]) == tg.drawdown_pct == Decimal("0.005")


def test_pnl_without_a_snapshot_uses_the_starting_cash(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        cash = s.query(m.SimAccount).filter_by(run_id=run_id).one().starting_cash
    pnl = _client(db_factory, clock).get("/api/dashboard").json()["pnl"]
    assert Decimal(pnl["equity"]) == cash == Decimal(pnl["peak_equity"])
    assert Decimal(pnl["drawdown_pct"]) == 0 and Decimal(pnl["unrealized"]) == 0
    assert pnl["unrealized_partial"] is False and Decimal(pnl["realized_today"]) == 0


# --- 3. quotes failing --------------------------------------------------------------------------------------


def test_a_failing_quote_source_still_gives_a_dashboard(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        seed_open_position(s, run_id)
        s.commit()

    async def broken(ids: Sequence[int]) -> Mapping[int, QtQuote]:
        raise ConnectionError("questrade down")

    r = _client(db_factory, clock, quotes=broken).get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"][0]["last"] is None and body["positions"][0]["unrealized_pnl"] is None
    assert body["pnl"]["unrealized_partial"] is True


def test_no_quote_source_at_all(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        seed_open_position(s, run_id)
        s.commit()
    body = _client(db_factory, clock, quotes=None).get("/api/dashboard").json()
    assert body["positions"][0]["last"] is None and body["pnl"]["unrealized_partial"] is True


# --- 4. weekend and early close -----------------------------------------------------------------------------


def test_saturday_has_no_timeline(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(datetime(2026, 10, 10, 16, 0, tzinfo=UTC))
    r = _client(db_factory, clock).get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["is_session"] is False and body["session"]["phase"] == "closed_day"
    assert body["session"]["date"] == "2026-10-12"  # the next session
    assert body["session"]["open_at"] is None and body["session"]["close_at"] is None
    assert body["timeline"] == []


def test_early_close_day_drops_the_1330_checkin(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(datetime(2026, 11, 27, 15, 0, tzinfo=UTC))  # 10:00 ET, 13:00 close
    body = _client(db_factory, clock).get("/api/dashboard").json()
    timeline = _by_key(body)
    assert "checkin_1330" not in timeline and "checkin_1130" in timeline
    assert timeline["flatten"]["at"] == "2026-11-27T17:50:00Z"
    assert body["session"]["close_at"] == "2026-11-27T18:00:00Z"
    assert timeline["postclose"]["at"] == "2026-11-27T21:15:00Z"


# --- 5. missed and failed events ----------------------------------------------------------------------------


def test_missed_and_failed_events(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(datetime(2026, 10, 6, 16, 0, tzinfo=UTC))  # 12:00 ET
    live_run(db_factory, clock)
    with db_factory() as s:
        _job(s, event_job("orb_open"), "failed", error="missed: 295s late")
        _job(s, event_job("entry_cancel"), "failed", error="RuntimeError: boom")
        _job(s, "premarket", "failed", error="FinvizBlocked: blocked")
        _job(s, "preopen", "running")
        s.commit()
    body = _client(db_factory, clock, fired=lambda d: {"orb_open"}).get("/api/dashboard").json()
    timeline = _by_key(body)
    assert timeline["orb_open"]["status"] == "missed" and "295s" in (timeline["orb_open"]["detail"] or "")
    assert timeline["entry_cancel"]["status"] == "failed"
    assert timeline["premarket"]["status"] == "failed" and timeline["preopen"]["status"] == "running"
    assert timeline["nightly"]["status"] == "upcoming"
    assert [i["key"] for i in body["timeline"] if i["status"] == "next"] == ["checkin_1330"]


# --- the rest of the dashboard ------------------------------------------------------------------------------


def test_events_candidates_and_flags(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run_id = live_run(db_factory, clock)
    with db_factory() as s:
        for i in range(25):
            s.add(
                m.EventLog(
                    ts=NOW - timedelta(minutes=30 - i), level="info", source="worker", run_id=None,
                    message=f"event {i}", data=None,
                )
            )  # fmt: skip
        s.add(m.EventLog(ts=NOW, level="debug", source="worker", run_id=None, message="noise", data=None))
        syms = [add_symbol(s, f"C{i}") for i in range(7)]
        for i, sym in enumerate(syms):
            s.add(
                m.Candidate(
                    run_id=run_id, session_date=DAY, strategy_key="orb_sip", symbol_id=sym, rvol=None,
                    rank=(7 - i) if i < 6 else None, candle=None, passed=i < 6, reject_reason=None, data=None,
                    created_at=NOW,
                )
            )  # fmt: skip
        s.commit()
    body = _client(db_factory, clock, telegram_configured=False).get("/api/dashboard").json()
    events = body["events"]
    assert len(events) == 20 and events[0]["message"] == "event 24" and events[-1]["message"] == "event 5"
    assert all(e["level"] != "debug" for e in events)
    assert [c["rank"] for c in body["candidates_top"]] == [2, 3, 4, 5, 6]  # ranked only, best first
    assert body["candidates_top"][0]["ticker"] == "C5"
    assert body["candidates_count"] == 7
    assert body["telegram_configured"] is False


def test_worker_heartbeat_and_auto_mode(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    services = make_services(test_core(db_factory, clock), plan=real_plan)
    services.core.settings.set("approval_mode", "auto", "test")
    with db_factory() as s:
        s.add(
            m.WorkerHeartbeat(
                process="worker", pid=1, host="h", started_at=NOW - timedelta(hours=1),
                beat_at=NOW - timedelta(seconds=5), session_date=DAY, phase="session",
            )
        )  # fmt: skip
        s.commit()
    body = make_client(services, dashboard.router).get("/api/dashboard").json()
    assert body["approval_mode"] == "auto"
    assert body["worker"]["ok"] is True and body["worker"]["phase"] == "session"


def test_the_dashboard_needs_a_session(db_factory: sessionmaker[Session]) -> None:
    services = make_services(test_core(db_factory, FixedClock(NOW)))
    assert make_client(services, dashboard.router, user=None).get("/api/dashboard").status_code == 401


# --- 6. DAY_JOBS agree with docker/crontab ------------------------------------------------------------------


def _crontab_day_jobs() -> set[tuple[time, str]]:
    """(ET time, command) of every weekday line that runs premarket, preopen, checkin or postclose."""
    out: set[tuple[time, str]] = set()
    for raw in CRONTAB.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        minute, hour, _dom, _mon, dow, *command = line.split()
        words = command[1:]  # drop "trader"
        if dow != "1-5" or not words or words[0] not in ("premarket", "preopen", "checkin", "postclose"):
            continue
        out.add((time(int(hour), int(minute)), " ".join(words)))
    return out


def test_day_jobs_agree_with_the_crontab() -> None:
    cron = _crontab_day_jobs()
    assert cron, "no day-level lines found in docker/crontab"
    ours = {(j.et_time, j.command) for j in DAY_JOBS if j.key != "nightly"}
    assert ours == cron
    assert any(j.key == "nightly" and j.job_name == "nightly" for j in DAY_JOBS)


def test_timeline_times_are_utc_from_et_wall_clock(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(datetime(2026, 12, 1, 15, 0, tzinfo=UTC))  # EST
    timeline = _by_key(_client(db_factory, clock).get("/api/dashboard").json())
    assert timeline["premarket"]["at"] == "2026-12-01T13:00:00Z"
    assert timeline["nightly"]["at"] == datetime.combine(
        date(2026, 11, 30), time(20, 0), tzinfo=ET
    ).astimezone(UTC).isoformat().replace("+00:00", "Z")
