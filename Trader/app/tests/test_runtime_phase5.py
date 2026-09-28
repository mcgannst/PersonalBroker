"""P5-T17: the Phase 5 runtime wiring. `build_services` gains the replay launcher and the API's log mirror
(acceptance test 5, plus test 6 for the API); `run_worker` installs the worker's mirror and closes it on
shutdown (test 6); `install_log_mirror` never raises and honours `logging.mirror_level = "off"`;
`weekly_job` builds its Claude client only with a key; the replay offline window is one shared helper."""

# ruff: noqa: F811  (the imported `world` / `wired` fixtures are test parameters)

import logging
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, time
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import trader.runtime as rt
from tests.api.test_routes_sweep import Wired, wired  # noqa: F401 (the fixture)
from tests.test_runtime import World, world  # noqa: F401 (the fixture)
from trader.api import services as api_services
from trader.api.replay_launcher import SubprocessReplayLauncher
from trader.api.routers import replays as replays_router
from trader.db import models as m
from trader.logging_mirror import EventLogMirror
from trader.logging_setup import configure_logging  # the real one (conftest stubs the attribute)
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET
from trader.replay import runner as replay_runner
from trader.worker import Worker

pytestmark = pytest.mark.db

CAL = SessionCalendar()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for stray in handlers:  # a mirror another test left installed is set aside for this test
        if type(stray).__name__ == "_MirrorHandler":
            root.removeHandler(stray)
    structlog.reset_defaults()
    configure_logging("test")  # structlog routed through the stdlib root logger
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    structlog.reset_defaults()


def mirror_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if type(h).__name__ == "_MirrorHandler"]


def events(factory: sessionmaker[Session], source: str) -> list[m.EventLog]:
    with factory() as s:
        return list(
            s.execute(select(m.EventLog).where(m.EventLog.source == source).order_by(m.EventLog.id)).scalars()
        )


# --- install_log_mirror -------------------------------------------------------------------------------------


def test_install_log_mirror_installs_a_started_mirror_for_the_process(world: World) -> None:
    mirror = rt.install_log_mirror(world.core, "cron")
    assert isinstance(mirror, EventLogMirror)
    try:
        assert mirror.process == "cron" and mirror.run_id is None
        assert mirror.handler in logging.getLogger().handlers
        structlog.get_logger("trader.test").error("mirrored.line")
    finally:
        rt.close_log_mirror(mirror)
    assert [r.message for r in events(world.factory, "log.cron")] == ["trader.test: mirrored.line"]
    assert mirror_handlers() == []


def test_install_log_mirror_is_none_when_off(world: World) -> None:
    world.core.settings.set("logging.mirror_level", "off", actor="test")
    assert rt.install_log_mirror(world.core, "cron") is None
    assert mirror_handlers() == []
    rt.close_log_mirror(None)  # a no-op


def test_install_log_mirror_never_raises(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("no mirror today")

    monkeypatch.setattr(rt, "install_event_mirror", broken)
    assert rt.install_log_mirror(world.core, "cron") is None


def test_an_unreadable_settings_row_installs_the_default_mirror_without_an_event(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable() -> Any:
        raise RuntimeError("settings table gone")

    monkeypatch.setattr(world.core.settings, "load", unreadable)
    mirror = rt.install_log_mirror(world.core, "cron")
    try:
        assert mirror is not None and mirror.level == "error"
    finally:
        rt.close_log_mirror(mirror)
    assert events(world.factory, "settings") == []


# --- 6. the worker ------------------------------------------------------------------------------------------


async def test_run_worker_mirrors_an_error_line_as_log_worker_and_closes_the_mirror(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[logging.Handler]] = []

    async def run(self: Worker, stop: Any, *, once: bool = False) -> None:
        seen.append(mirror_handlers())
        structlog.get_logger("trader.test").error("worker.step_failed", step="relay")

    monkeypatch.setattr(Worker, "run", run)
    assert await rt.run_worker(once=True) == 0
    assert len(seen[0]) == 1  # installed while the worker ran
    [row] = events(world.factory, "log.worker")
    assert row.message == "trader.test: worker.step_failed" and row.data == {"step": "relay"}
    assert row.run_id is None
    assert mirror_handlers() == []  # closed on shutdown


async def test_run_worker_with_the_mirror_off_installs_none(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world.core.settings.set("logging.mirror_level", "off", actor="test")
    seen: list[int] = []

    async def run(self: Worker, stop: Any, *, once: bool = False) -> None:
        seen.append(len(mirror_handlers()))
        structlog.get_logger("trader.test").error("worker.step_failed")

    monkeypatch.setattr(Worker, "run", run)
    assert await rt.run_worker(once=True) == 0
    assert seen == [0]
    assert events(world.factory, "log.worker") == []


async def test_run_worker_closes_the_mirror_when_the_worker_exits_with_a_code(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lost(self: Worker, stop: Any, *, once: bool = False) -> None:
        raise SystemExit(3)

    monkeypatch.setattr(Worker, "run", lost)
    assert await rt.run_worker(once=True) == 3
    assert mirror_handlers() == []


# --- 5 and 6. the API ---------------------------------------------------------------------------------------


async def test_build_services_has_the_replay_launcher_and_the_api_mirror(wired: Wired) -> None:
    async with AsyncExitStack() as stack:
        services = await api_services.build_services(wired.core, stack)
        assert isinstance(services.replays, SubprocessReplayLauncher)
        assert services.replays.running() is False
        [handler] = mirror_handlers()
        structlog.get_logger("trader.test").error("api.request_failed", path="/api/x")
    [row] = events(wired.factory, "log.api")
    assert row.message == "trader.test: api.request_failed"
    assert mirror_handlers() == []  # closed with the stack


async def test_build_services_with_the_mirror_off_installs_none(wired: Wired) -> None:
    wired.core.settings.set("logging.mirror_level", "off", actor="test")
    async with AsyncExitStack() as stack:
        services = await api_services.build_services(wired.core, stack)
        assert services.replays is not None
        assert mirror_handlers() == []


# --- weekly_job's Claude client -----------------------------------------------------------------------------


async def test_weekly_job_builds_claude_only_with_a_key(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.reports.weekly import week_window

    captured: list[Any] = []

    async def fake_run_weekly(deps: Any, week: Any) -> dict[str, Any]:
        captured.append(deps.writer)
        return {"week_ending": week.week_ending.isoformat()}

    monkeypatch.setattr(rt, "run_weekly", fake_run_weekly)
    week = week_window(CAL, datetime(2026, 11, 25).date())
    world.clock.set(datetime(2026, 11, 28, 14, 0, tzinfo=UTC))
    out = await rt.weekly_job(world.core, week, force=False)
    assert out.status == "succeeded" and captured == [None]

    clients: list[dict[str, Any]] = []

    class FakeClaude:
        def __init__(self, **kw: Any) -> None:
            clients.append(kw)

        async def close(self) -> None:
            clients.append({"closed": True})

    monkeypatch.setattr(rt, "claude_client_class", lambda: FakeClaude)
    from pydantic import SecretStr

    keyed = world.core.__class__(
        **{
            **world.core.__dict__,
            "env": world.core.env.model_copy(update={"anthropic_api_key": SecretStr("k")}),
        }
    )
    out = await rt.weekly_job(keyed, week, force=True)
    assert out.status == "succeeded" and captured[-1] is not None
    assert clients[0]["timeout"] == 30 and clients[0]["max_retries"] == 1
    assert clients[-1] == {"closed": True}


# --- one offline-window helper ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "offline"),
    [
        ((2026, 11, 24, 9, 14, 59), False),
        ((2026, 11, 24, 9, 15, 0), True),
        ((2026, 11, 24, 16, 29, 59), True),
        ((2026, 11, 24, 16, 30, 0), False),
        ((2026, 11, 28, 12, 0, 0), False),  # Saturday
        ((2026, 11, 26, 12, 0, 0), False),  # Thanksgiving
    ],
)
def test_the_runner_and_the_router_share_one_offline_window(at: tuple[int, ...], offline: bool) -> None:
    y, mo, d, hh, mi, ss = at
    now = datetime.combine(datetime(y, mo, d).date(), time(hh, mi, ss), tzinfo=ET).astimezone(UTC)
    assert replay_runner.in_offline_window(CAL, now) is offline
    assert replays_router.offline_now(CAL, now) is offline
    assert not hasattr(replays_router, "OFFLINE_FROM")  # the router keeps no copy of the window
