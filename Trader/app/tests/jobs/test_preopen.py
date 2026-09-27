"""P3-T10: the 09:20 ET pre-open check (token, universe, opening-bar stats, pre-market, kill switches,
worker heartbeat), sent straight from the cron process."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from tests.fakes_telegram import FakeRenderer, RecordingNotifier
from trader.adapters.questrade.auth import QuestradeAuthError
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.jobs.preopen import PreopenDeps, run_preopen
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.types import UniverseStatus
from trader.notify.types import Check, PreopenView
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 13, 20, tzinfo=UTC)  # 09:20 ET
THANKSGIVING = date(2026, 11, 26)
CHECK_NAMES = ["token", "universe", "open_bar_stats", "premarket", "kill_switches", "worker"]
FINVIZ = UniverseStatus("finviz", None, False, None)


class Harness:
    def __init__(self, factory: sessionmaker[Session], run_id: int, clock: FixedClock) -> None:
        self.factory = factory
        self.run_id = run_id
        self.clock = clock
        self.notifier = RecordingNotifier()
        self.render = FakeRenderer()
        self.settings = RuntimeSettings()
        self.token_error: Exception | None = None
        self.universe = FINVIZ
        self.token_calls = 0

    async def token_check(self) -> None:
        self.token_calls += 1
        if self.token_error is not None:
            raise self.token_error

    async def universe_status(self, d: date) -> UniverseStatus:
        return self.universe

    def deps(self) -> PreopenDeps:
        return PreopenDeps(
            factory=self.factory,
            clock=self.clock,
            calendar=CAL,
            settings=lambda: self.settings,
            token_check=self.token_check,
            universe_status=self.universe_status,
            killswitches=KillSwitches(self.factory, self.clock),
            run_id=self.run_id,
            notifier=self.notifier,
            render=self.render,
        )

    def view(self) -> PreopenView:
        views = [args[0] for name, args in self.render.calls if name == "preopen"]
        assert len(views) == 1
        view = views[0]
        assert isinstance(view, PreopenView)
        return view


def _checks(detail: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in detail["checks"]}


def _seed_healthy(s: Session, session_date: date = DAY, beat_at: datetime | None = NOW) -> None:
    sid = add_symbol(s, "AAA")
    s.add(
        m.OpenBarStat(symbol_id=sid, session_date=session_date, avg_open_vol_14d=Decimal("1000"), atr14=None)
    )
    s.add(
        m.JobRun(
            job="premarket",
            session_date=session_date,
            started_at=NOW - timedelta(hours=1, minutes=20),
            finished_at=NOW - timedelta(hours=1),
            status="succeeded",
            detail={},
        )
    )
    if beat_at is not None:
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=1,
                host="h",
                started_at=beat_at - timedelta(hours=1),
                beat_at=beat_at - timedelta(seconds=10),
                session_date=session_date,
                phase="idle",
            )
        )


@pytest.fixture
def harness(db_factory: sessionmaker[Session]) -> Harness:
    with db_factory() as s:
        run_id = add_run(s)
        s.commit()
    return Harness(db_factory, run_id, FixedClock(NOW))


async def test_holiday_is_skipped_and_sends_nothing(harness: Harness) -> None:
    harness.clock.set(datetime(2026, 11, 26, 14, 20, tzinfo=UTC))
    detail = await run_preopen(harness.deps(), THANKSGIVING)
    assert detail == {"skipped": "not a session"}
    assert harness.notifier.sent == [] and harness.token_calls == 0


async def test_healthy_sends_six_ok_checks(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s)
        s.commit()
    detail = await run_preopen(harness.deps(), DAY)
    assert detail["ok"] is True
    assert [c["name"] for c in detail["checks"]] == CHECK_NAMES
    assert all(c["ok"] and c["level"] == "info" for c in detail["checks"])
    assert len(harness.notifier.sent) == 1
    msg = harness.notifier.sent[0]
    assert msg.kind == "preopen" and msg.dedupe_key == "preopen:2026-10-06"
    view = harness.view()
    assert view.session_date == DAY and view.approval_mode == "manual"
    assert [c.name for c in view.checks] == CHECK_NAMES
    assert all(isinstance(c, Check) and c.ok for c in view.checks)


async def test_healthy_with_notify_when_ok_off_sends_nothing(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s)
        s.commit()
    harness.settings = RuntimeSettings.model_validate({"preopen.notify_when_ok": False})
    detail = await run_preopen(harness.deps(), DAY)
    assert detail["ok"] is True
    assert harness.notifier.sent == []


async def test_token_heartbeat_and_drawdown_errors(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s, beat_at=NOW - timedelta(minutes=10))
        s.add(
            m.KillSwitchEvent(
                run_id=harness.run_id,
                switch="max_drawdown_pct",
                session_date=date(2026, 10, 5),
                tripped_at=NOW - timedelta(days=1),
                value=Decimal("0.2"),
                threshold=Decimal("0.15"),
            )
        )
        s.commit()
    harness.token_error = QuestradeAuthError("The refresh token was already used or has expired.")
    harness.settings = RuntimeSettings.model_validate({"preopen.notify_when_ok": False})
    detail = await run_preopen(harness.deps(), DAY)
    checks = _checks(detail)
    assert detail["ok"] is False
    errors = {n for n, c in checks.items() if c["level"] == "error"}
    assert errors == {"token", "kill_switches", "worker"}
    assert "already used or has expired" in checks["token"]["detail"]
    assert "max_drawdown_pct" in checks["kill_switches"]["detail"]
    assert "worker not running" in checks["worker"]["detail"]
    assert all(checks[n]["ok"] for n in ("universe", "open_bar_stats", "premarket"))
    assert len(harness.notifier.sent) == 1  # sent although notify_when_ok is off


async def test_missing_heartbeat_is_an_error(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s, beat_at=None)
        s.commit()
    detail = await run_preopen(harness.deps(), DAY)
    worker = _checks(detail)["worker"]
    assert not worker["ok"] and worker["level"] == "error"
    assert "worker not running" in worker["detail"]


@pytest.mark.parametrize("phase", ["stopping", "stopped"])
async def test_a_fresh_heartbeat_of_a_stopping_or_stopped_worker_is_an_error(
    harness: Harness, phase: str
) -> None:
    """Fix round 1: a worker shutting down (phase `stopping`) is treated like a stopped one."""
    with harness.factory() as s:
        _seed_healthy(s)
        s.query(m.WorkerHeartbeat).update({m.WorkerHeartbeat.phase: phase})
        s.commit()
    worker = _checks(await run_preopen(harness.deps(), DAY))["worker"]
    assert not worker["ok"] and worker["level"] == "error"
    assert "worker not running" in worker["detail"] and phase in worker["detail"]


async def test_manual_pause_is_a_warning(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s)
        s.commit()
    KillSwitches(harness.factory, harness.clock).pause(harness.run_id, DAY, actor="test")
    ks = _checks(await run_preopen(harness.deps(), DAY))["kill_switches"]
    assert not ks["ok"] and ks["level"] == "warning" and "manual_pause" in ks["detail"]


async def test_stale_fallback_is_an_error(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s)
        s.commit()
    harness.universe = UniverseStatus("fallback", date(2026, 9, 29), True, 5)
    uni = _checks(await run_preopen(harness.deps(), DAY))["universe"]
    assert not uni["ok"] and uni["level"] == "error"
    assert "2026-09-29" in uni["detail"] and "stale" in uni["detail"]


async def test_fresh_fallback_is_a_warning_and_missing_premarket_a_warning(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s)
        s.query(m.JobRun).delete()
        s.commit()
    harness.universe = UniverseStatus("fallback", date(2026, 10, 5), False, 1)
    detail = await run_preopen(harness.deps(), DAY)
    checks = _checks(detail)
    assert not checks["universe"]["ok"] and checks["universe"]["level"] == "warning"
    assert "1 session" in checks["universe"]["detail"]
    assert not checks["premarket"]["ok"] and checks["premarket"]["level"] == "warning"
    assert detail["ok"] is False


async def test_no_universe_and_no_open_bar_stats_are_errors(harness: Harness) -> None:
    harness.universe = UniverseStatus(None, None, False, None)
    checks = _checks(await run_preopen(harness.deps(), DAY))
    assert checks["universe"]["level"] == "error"
    assert checks["open_bar_stats"]["level"] == "error"


async def test_a_failed_premarket_run_is_a_warning(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s)
        s.query(m.JobRun).update({m.JobRun.status: "failed"})
        s.commit()
    pm = _checks(await run_preopen(harness.deps(), DAY))["premarket"]
    assert not pm["ok"] and pm["level"] == "warning"


async def test_a_check_that_raises_becomes_an_error_check(harness: Harness) -> None:
    with harness.factory() as s:
        _seed_healthy(s)
        s.commit()

    async def broken(d: date) -> UniverseStatus:
        raise RuntimeError("db down")

    deps = harness.deps()
    object.__setattr__(deps, "universe_status", broken)
    detail = await run_preopen(deps, DAY)
    uni = _checks(detail)["universe"]
    assert uni["level"] == "error" and "RuntimeError" in uni["detail"]
    assert len(harness.notifier.sent) == 1


async def test_a_token_url_in_a_check_error_is_masked_in_the_detail(harness: Harness) -> None:
    """P3-REVIEW: the job detail (job_runs.detail, shown in the web app) never keeps a secret."""
    with harness.factory() as s:
        _seed_healthy(s)
        s.commit()
    secret = "0123456789abcdefREFRESHtoken"
    harness.token_error = RuntimeError(
        f"GET https://login.example/oauth2/token?refresh_token={secret} failed"
    )
    detail = await run_preopen(harness.deps(), DAY)
    token = _checks(detail)["token"]
    assert token["ok"] is False and secret not in token["detail"] and "[REDACTED]" in token["detail"]
