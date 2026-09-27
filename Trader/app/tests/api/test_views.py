"""P4-T1 acceptance test 8: the small shared API view builders (`trader.api.views`), used by T5, T6 and T9."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.telegram.commands import _reset_hint
from trader.api import views
from trader.db import models as m
from trader.engine.killswitch import SWITCHES, KillSwitches
from trader.engine.runs import get_live_run
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

DAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)  # 11:30 ET
BOT_TOKEN = "123456789:AAHfakeTokenValueForTestsOnlyXYZ12345"


# --- token_out ---------------------------------------------------------------------------------------------


def test_token_out_ok() -> None:
    health = TokenHealth(True, NOW + timedelta(minutes=20), NOW - timedelta(hours=2), None)
    out = views.token_out(lambda: health, NOW)
    assert (out.ok, out.seeded, out.error) == (True, True, None)
    assert out.age_hours == pytest.approx(2.0)
    assert out.expires_at == NOW + timedelta(minutes=20) and out.last_refresh_at == NOW - timedelta(hours=2)


def test_token_out_not_seeded_and_failing_health() -> None:
    out = views.token_out(lambda: TokenHealth(False, None, None, None), NOW)
    assert (out.ok, out.seeded, out.error) == (False, False, "not seeded")

    def broken() -> TokenHealth:
        raise RuntimeError("refresh_token=secret-value")

    failed = views.token_out(broken, NOW)
    assert failed.ok is False and failed.seeded is False
    assert failed.error is not None and "secret-value" not in failed.error


# --- worker_out --------------------------------------------------------------------------------------------


def _beat(s: Session, phase: str, beat_at: datetime) -> None:
    s.add(
        m.WorkerHeartbeat(
            process="worker",
            pid=42,
            host="trader-dev",
            started_at=beat_at - timedelta(hours=1),
            beat_at=beat_at,
            session_date=DAY,
            phase=phase,
            detail={"rate_limit": {"market": 19}},
        )
    )
    s.commit()


@pytest.mark.db
def test_worker_out_without_a_row(db_factory: sessionmaker[Session]) -> None:
    out = views.worker_out(db_factory, NOW, stale_seconds=120)
    assert out.ok is False and out.phase is None and out.beat_at is None and out.age_seconds is None


@pytest.mark.db
def test_worker_out_fresh_session_beat_is_ok(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        _beat(s, "session", NOW - timedelta(seconds=10))
    out = views.worker_out(db_factory, NOW, stale_seconds=120)
    assert out.ok is True and out.phase == "session" and out.age_seconds == pytest.approx(10.0)
    assert (out.pid, out.host, out.session_date) == (42, "trader-dev", DAY)
    assert out.detail == {"rate_limit": {"market": 19}}


@pytest.mark.db
def test_worker_out_stale_beat_is_not_ok(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        _beat(s, "idle", NOW - timedelta(seconds=121))
    assert views.worker_out(db_factory, NOW, stale_seconds=120).ok is False


@pytest.mark.db
@pytest.mark.parametrize("phase", ["stopped", "stopping"])
def test_worker_out_stopped_is_not_ok_however_fresh(db_factory: sessionmaker[Session], phase: str) -> None:
    with db_factory() as s:
        _beat(s, phase, NOW)
    out = views.worker_out(db_factory, NOW, stale_seconds=120)
    assert out.ok is False and out.phase == phase


# --- event_out ---------------------------------------------------------------------------------------------


def test_event_out_masks_a_bot_token() -> None:
    row = m.EventLog(
        id=7,
        ts=NOW,
        level="error",
        source="telegram",
        run_id=None,
        message=f"send failed: https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "url": f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            "n": 3,
            "nested": {"password": "x"},
        },
    )
    out = views.event_out(row)
    assert (out.id, out.ts, out.level, out.source) == (7, NOW, "error", "telegram")
    assert BOT_TOKEN not in out.message and "sendMessage" in out.message
    assert out.data is not None and BOT_TOKEN not in str(out.data) and out.data["n"] == 3
    assert out.data["nested"] == {"password": "[REDACTED]"}


def test_event_out_without_data() -> None:
    row = m.EventLog(id=1, ts=NOW, level="info", source="worker", run_id=None, message="started", data=None)
    assert views.event_out(row).data is None


# --- killswitch_states -------------------------------------------------------------------------------------


def _trip(s: Session, run_id: int, switch: str, value: str | None, threshold: str | None) -> None:
    s.add(
        m.KillSwitchEvent(
            run_id=run_id,
            switch=switch,
            session_date=DAY,
            tripped_at=NOW - timedelta(minutes=5),
            value=Decimal(value) if value is not None else None,
            threshold=Decimal(threshold) if threshold is not None else None,
        )
    )
    s.commit()


@pytest.mark.db
def test_killswitch_states_with_max_drawdown_tripped(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        _trip(s, run.id, "max_drawdown_pct", "0.160000", "0.150000")
    states = views.killswitch_states(KillSwitches(db_factory, clock), db_factory, run.id, DAY)
    assert [k.switch for k in states] == list(SWITCHES)
    by = {k.switch: k for k in states}
    dd = by["max_drawdown_pct"]
    assert dd.tripped and dd.needs_web_reset and dd.automatic
    assert (dd.value, dd.threshold) == (Decimal("0.16"), Decimal("0.15"))
    assert dd.tripped_at == NOW - timedelta(minutes=5)
    assert dd.clears == _reset_hint("max_drawdown_pct")
    for name in ("daily_loss_pct", "expectancy", "manual_pause"):
        k = by[name]
        assert not k.tripped and not k.needs_web_reset and k.clears == ""
        assert k.value is None and k.threshold is None and k.tripped_at is None
    assert all(k.label for k in states)


@pytest.mark.db
def test_killswitch_states_manual_pause_and_daily_loss(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    ks = KillSwitches(db_factory, clock)
    assert ks.pause(run.id, DAY, "web:stephen")
    with db_factory() as s:
        _trip(s, run.id, "daily_loss_pct", "-0.060000", "0.050000")
    by = {k.switch: k for k in views.killswitch_states(ks, db_factory, run.id, DAY)}
    pause = by["manual_pause"]
    assert pause.tripped and pause.automatic is False and pause.needs_web_reset is False
    assert "Resume" in pause.clears and "/resume" in pause.clears
    daily = by["daily_loss_pct"]
    assert daily.tripped and daily.automatic and daily.needs_web_reset is False
    assert daily.clears == _reset_hint("daily_loss_pct")
    # daily_loss_pct is tripped for its own session only (KillSwitches.active)
    other = {k.switch: k for k in views.killswitch_states(ks, db_factory, run.id, date(2026, 10, 7))}
    assert other["daily_loss_pct"].tripped is False and other["manual_pause"].tripped is True
