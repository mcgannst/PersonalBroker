"""Starts a queued replay in its own process (SPEC §8; P5-T7): `trader replay --run <id>` as a child of the
API process, with the API's environment and inherited output (it reaches `docker logs`), not awaited (a
reaper task waits for it, as `SubprocessJobLauncher` does). Implements `trader.api.deps.ReplayLauncher`.

- `launch(run_id)` first checks that the run is a `queued` replay (`ReplayNotFound` / `ValueError`
  otherwise, and nothing is spawned); a spawn failure is raised to the caller, which fails the run.
- A non-zero exit writes one `warning` event (source `replay.launcher`, data: run id and exit code, never
  the child's output) with the replay's `run_id`, so it never reaches the live views or Telegram. When
  waiting for the child fails, the event says its outcome is unknown instead.
- `running()` is True while a child it started (or is starting) is alive. The replay's own advisory lock
  (`trader.replay.runner.REPLAY_LOCK`) still guards one-at-a-time inside the child.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import anyio.to_thread
import structlog
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock
from trader.replay.types import ReplayNotFound

log = structlog.get_logger("api.replay_launcher")

EXIT_EVENT_SOURCE = "replay.launcher"


def command(executable: str, run_id: int) -> tuple[str, ...]:
    """The argv of a replay child."""
    return (executable, "replay", "--run", str(run_id))


class SubprocessReplayLauncher:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        *,
        executable: str = "trader",
        spawn: Callable[..., Any] = asyncio.create_subprocess_exec,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._executable = executable
        self._spawn = spawn
        self._children: dict[int, Any] = {}  # run id -> its running child process
        self._starting: set[int] = set()  # run ids between the check and the spawn
        self._reapers: set[asyncio.Task[None]] = set()  # strong references until each finishes

    async def launch(self, run_id: int) -> None:
        await anyio.to_thread.run_sync(self._check_queued, run_id)
        self._starting.add(run_id)
        try:
            proc = await self._spawn(*command(self._executable, run_id), stdin=asyncio.subprocess.DEVNULL)
            self._children[run_id] = proc
        finally:
            self._starting.discard(run_id)
        log.info("api.replay_launched", run_id=run_id, pid=proc.pid)
        task = asyncio.create_task(self._reap(run_id, proc), name=f"reap-replay-{run_id}")
        self._reapers.add(task)
        task.add_done_callback(self._reapers.discard)

    def running(self) -> bool:
        return bool(self._children) or bool(self._starting)

    def children(self) -> dict[int, int]:
        """The running children: run id -> pid (for tests and diagnostics)."""
        return {run_id: proc.pid for run_id, proc in self._children.items()}

    # --- internals ------------------------------------------------------------------------------------------

    def _check_queued(self, run_id: int) -> None:
        with self._factory() as s:
            row = s.get(m.Run, run_id)
            if row is None or row.mode != "replay":
                raise ReplayNotFound(f"replay {run_id} not found")
            if row.status != "queued":
                raise ValueError(f"replay {run_id} is {row.status}, not queued")

    async def _reap(self, run_id: int, proc: Any) -> None:
        code: int | None = None
        lost: str | None = None  # the error type when waiting for the child failed
        try:
            code = await proc.wait()
        except Exception as exc:
            lost = type(exc).__name__
            log.error("api.replay_wait_failed", run_id=run_id, error_type=lost)
        finally:
            if self._children.get(run_id) is proc:
                del self._children[run_id]
        log.info("api.replay_exited", run_id=run_id, exit_code=code)
        if not code and lost is None:
            return
        try:
            await anyio.to_thread.run_sync(self._record_exit, run_id, code, lost)
        except Exception as exc:
            log.error("api.replay_exit_event_failed", run_id=run_id, error_type=type(exc).__name__)

    def _record_exit(self, run_id: int, code: int | None, lost: str | None) -> None:
        if lost is not None:
            message = f"Lost track of replay {run_id} ({lost}); its outcome is unknown"
            data: dict[str, Any] = {"run_id": run_id, "exit_code": None, "error_type": lost}
        else:
            message = f"Replay {run_id} exited with code {code}"
            data = {"run_id": run_id, "exit_code": code}
        with session_scope(self._factory) as s:
            log_event(s, self._clock, "warning", EXIT_EVENT_SOURCE, message, data, run_id=run_id)
