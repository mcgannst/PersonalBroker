"""P5-T7 acceptance test 7: `SubprocessReplayLauncher` spawns `trader replay --run <id>`, reaps the child and
reports a failed exit with one `warning` event carrying the replay's run id."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.test_replays import add_replay
from trader.api.deps import ReplayLauncher
from trader.api.replay_launcher import EXIT_EVENT_SOURCE, SubprocessReplayLauncher
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.replay.types import ReplayNotFound

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 30, 22, 0, tzinfo=UTC)  # Monday 17:00 ET


class FakeProcess:
    """A child process whose exit the test controls with `finish(code)` (or `lose()`: waiting fails)."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.error: BaseException | None = None
        self._done = asyncio.Event()

    def finish(self, code: int) -> None:
        self.returncode = code
        self._done.set()

    def lose(self) -> None:
        self.error = ProcessLookupError("gone")
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        if self.error is not None:
            raise self.error
        assert self.returncode is not None
        return self.returncode


class FakeSpawn:
    """Records every argv and keyword set; returns a new `FakeProcess` each time, or raises `error`."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.processes: list[FakeProcess] = []
        self.error: BaseException | None = None

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProcess:
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        proc = FakeProcess(2000 + len(self.processes))
        self.processes.append(proc)
        return proc


def _events(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(s.scalars(select(m.EventLog).where(m.EventLog.source == EXIT_EVENT_SOURCE)))


async def _settle() -> None:
    """Let the reaper task (and its thread-pool DB write) finish."""
    for _ in range(200):
        await asyncio.sleep(0.01)


def _queued(factory: sessionmaker[Session], run_id: int = 7, status: str = "queued") -> int:
    with factory() as s:
        out = add_replay(s, status=status, run_id=run_id)
        s.commit()
    return out


def test_satisfies_the_protocol(db_factory: sessionmaker[Session]) -> None:
    launcher: ReplayLauncher = SubprocessReplayLauncher(db_factory, FixedClock(NOW), spawn=FakeSpawn())
    assert launcher.running() is False


async def test_7_spawns_trader_replay_run_7_and_reaps_a_clean_exit(db_factory: sessionmaker[Session]) -> None:
    run_id = _queued(db_factory, 7)
    spawn = FakeSpawn()
    launcher = SubprocessReplayLauncher(db_factory, FixedClock(NOW), spawn=spawn)
    await launcher.launch(run_id)
    [(argv, kwargs)] = spawn.calls
    assert argv == ("trader", "replay", "--run", "7")
    assert kwargs == {"stdin": asyncio.subprocess.DEVNULL}  # env and output inherited, stdin closed
    assert launcher.running() is True and launcher.children() == {7: 2000}
    spawn.processes[0].finish(0)
    await _settle()
    assert launcher.running() is False and launcher.children() == {}
    assert _events(db_factory) == []


async def test_7_a_child_exiting_1_writes_one_warning_with_the_run_id(
    db_factory: sessionmaker[Session],
) -> None:
    run_id = _queued(db_factory, 7)
    spawn = FakeSpawn()
    launcher = SubprocessReplayLauncher(
        db_factory, FixedClock(NOW), executable="/app/.venv/bin/trader", spawn=spawn
    )
    await launcher.launch(run_id)
    assert spawn.calls[0][0] == ("/app/.venv/bin/trader", "replay", "--run", "7")
    spawn.processes[0].finish(1)
    await _settle()
    assert launcher.running() is False
    [event] = _events(db_factory)
    assert (event.level, event.run_id, event.ts) == ("warning", 7, NOW)
    assert event.data == {"run_id": 7, "exit_code": 1}
    assert event.message == "Replay 7 exited with code 1"


async def test_a_lost_child_is_reported_as_unknown(db_factory: sessionmaker[Session]) -> None:
    run_id = _queued(db_factory, 9)
    spawn = FakeSpawn()
    launcher = SubprocessReplayLauncher(db_factory, FixedClock(NOW), spawn=spawn)
    await launcher.launch(run_id)
    spawn.processes[0].lose()
    await _settle()
    [event] = _events(db_factory)
    assert event.run_id == 9 and event.data == {
        "run_id": 9,
        "exit_code": None,
        "error_type": "ProcessLookupError",
    }
    assert launcher.running() is False


async def test_only_a_queued_replay_is_launched(db_factory: sessionmaker[Session]) -> None:
    done = _queued(db_factory, 3, status="completed")
    with db_factory() as s:
        live = m.Run(mode="live", started_at=NOW, params={}, status="active")
        s.add(live)
        s.commit()
        live_id = live.id
    spawn = FakeSpawn()
    launcher = SubprocessReplayLauncher(db_factory, FixedClock(NOW), spawn=spawn)
    with pytest.raises(ValueError, match="not queued"):
        await launcher.launch(done)
    for bad in (live_id, 987654):
        with pytest.raises(ReplayNotFound):
            await launcher.launch(bad)
    assert spawn.calls == [] and launcher.running() is False


async def test_a_spawn_failure_is_raised_and_leaves_nothing_running(
    db_factory: sessionmaker[Session],
) -> None:
    run_id = _queued(db_factory, 7)
    spawn = FakeSpawn()
    spawn.error = FileNotFoundError("trader")
    launcher = SubprocessReplayLauncher(db_factory, FixedClock(NOW), spawn=spawn)
    with pytest.raises(FileNotFoundError):
        await launcher.launch(run_id)
    assert launcher.running() is False and _events(db_factory) == []
