"""P4-T9 acceptance test 4: `SubprocessJobLauncher` spawns, reaps and reports manual job runs."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.deps import JobLauncher
from trader.api.launcher import CLI_ARGS, SubprocessJobLauncher
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # Tuesday 17:00 ET
CAL = SessionCalendar()


class FakeProcess:
    """A child process whose exit the test controls with `finish(code)`."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._done = asyncio.Event()

    def finish(self, code: int) -> None:
        self.returncode = code
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode


class FakeSpawn:
    """Records every argv and keyword set; returns a new `FakeProcess` each time."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProcess:
        self.calls.append((argv, kwargs))
        proc = FakeProcess(1000 + len(self.processes))
        self.processes.append(proc)
        return proc


def _launcher(factory: sessionmaker[Session], spawn: FakeSpawn) -> SubprocessJobLauncher:
    return SubprocessJobLauncher(factory, FixedClock(NOW), CAL, spawn=spawn)


def _events(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(s.scalars(select(m.EventLog).where(m.EventLog.source == "jobs.manual")))


async def _settle() -> None:
    """Let the reaper task (and its thread-pool DB write) finish."""
    for _ in range(200):
        await asyncio.sleep(0.01)


def test_cli_args() -> None:
    assert dict(CLI_ARGS) == {
        "nightly": ("nightly",),
        "premarket": ("premarket",),
        "preopen": ("preopen",),
        "postclose": ("postclose",),
        "token-refresh": ("token-refresh",),
    }


@pytest.mark.db
def test_satisfies_the_protocol(db_factory: sessionmaker[Session]) -> None:
    launcher: JobLauncher = _launcher(db_factory, FakeSpawn())
    assert launcher.running("nightly") is False


@pytest.mark.db
async def test_spawns_argv_and_reaps_a_finished_child(db_factory: sessionmaker[Session]) -> None:
    spawn = FakeSpawn()
    launcher = _launcher(db_factory, spawn)
    out = await launcher.launch("postclose", date(2026, 10, 5), True, "web:stephen")
    assert out.launched is True and out.job == "postclose" and out.session_date == date(2026, 10, 5)
    assert [argv for argv, _ in spawn.calls] == [("trader", "postclose", "--date", "2026-10-05", "--force")]
    assert "stdout" not in spawn.calls[0][1] and "stderr" not in spawn.calls[0][1]  # output inherited
    assert launcher.running("postclose") is True and launcher.running("nightly") is False

    spawn.processes[0].finish(0)
    await _settle()
    assert launcher.running("postclose") is False
    assert launcher.children() == {}  # no zombie left in the table
    assert _events(db_factory) == []  # exit 0 writes no event


@pytest.mark.db
async def test_a_non_zero_exit_writes_one_warning(db_factory: sessionmaker[Session]) -> None:
    spawn = FakeSpawn()
    launcher = _launcher(db_factory, spawn)
    await launcher.launch("premarket", None, False, "web:stephen")
    assert spawn.calls[0][0] == ("trader", "premarket")
    spawn.processes[0].finish(1)
    await _settle()
    assert launcher.running("premarket") is False
    events = _events(db_factory)
    assert len(events) == 1
    ev = events[0]
    assert ev.level == "warning" and ev.source == "jobs.manual"
    assert ev.data == {"job": "premarket", "date": None, "exit_code": 1}
    assert "premarket" in ev.message and "1" in ev.message


@pytest.mark.db
async def test_default_date_is_shown_but_not_passed(db_factory: sessionmaker[Session]) -> None:
    spawn = FakeSpawn()
    launcher = _launcher(db_factory, spawn)
    nightly = await launcher.launch("nightly", None, False, "web:stephen")
    refresh = await launcher.launch("token-refresh", None, False, "web:stephen")
    assert [argv for argv, _ in spawn.calls] == [("trader", "nightly"), ("trader", "token-refresh")]
    assert nightly.session_date == date(2026, 10, 7)  # the next session, as `trader nightly` defaults
    assert refresh.session_date is None
    for p in spawn.processes:
        p.finish(0)
    await _settle()


@pytest.mark.db
async def test_refuses_a_second_launch_and_token_refresh_options(db_factory: sessionmaker[Session]) -> None:
    from trader.api.errors import ApiError

    spawn = FakeSpawn()
    launcher = _launcher(db_factory, spawn)
    await launcher.launch("nightly", None, False, "web:stephen")
    with pytest.raises(ApiError) as conflict:
        await launcher.launch("nightly", None, True, "web:stephen")
    assert conflict.value.status == 409
    with pytest.raises(ApiError) as bad:
        await launcher.launch("token-refresh", None, True, "web:stephen")
    assert bad.value.status == 422
    assert len(spawn.calls) == 1
    spawn.processes[0].finish(0)
    await _settle()


@pytest.mark.db
async def test_a_spawn_failure_is_reported_not_raised(db_factory: sessionmaker[Session]) -> None:
    async def broken(*argv: str, **kwargs: Any) -> FakeProcess:
        raise FileNotFoundError("trader")

    launcher = SubprocessJobLauncher(db_factory, FixedClock(NOW), CAL, spawn=broken)
    out = await launcher.launch("postclose", None, False, "web:stephen")
    assert out.launched is False and "FileNotFoundError" in out.message
    assert launcher.running("postclose") is False
