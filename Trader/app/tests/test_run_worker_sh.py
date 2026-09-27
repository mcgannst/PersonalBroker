"""P4-T17: docker/run-worker.sh, supervisord's `worker` program.

It runs `python -m trader.worker` as a child, forwards a stop signal to it, exits with the worker's code,
and waits WORKER_RESTART_DELAY seconds first after exit 2 (another worker holds the lock) or 3 (the lock was
lost), so a second worker never restarts in a tight loop. A stub `python` stands in for the worker.
"""

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "run-worker.sh"

STUB_PYTHON = """#!/bin/bash
echo "python $*" >> "$STUB_LOG"
if [ -n "${STUB_WAIT:-}" ]; then
  trap 'echo TERM >> "$STUB_LOG"; exit "${STUB_TERM_EXIT:-0}"' TERM
  echo started >> "$STUB_LOG"
  i=0
  while [ "$i" -lt 300 ]; do /bin/sleep 0.1; i=$((i + 1)); done
fi
exit "${STUB_EXIT:-0}"
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    _executable(stubs / "python", STUB_PYTHON)
    _executable(stubs / "sleep", '#!/bin/bash\necho "sleep $*" >> "$STUB_LOG"\n')
    return {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "WORKER_RESTART_DELAY": "7",
    }


def _calls(env: dict[str, str]) -> list[str]:
    path = Path(env["STUB_LOG"])
    return path.read_text().splitlines() if path.exists() else []


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


def test_script_is_valid_bash() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_clean_exit_returns_0_at_once(env: dict[str, str]) -> None:
    result = _run({**env, "STUB_EXIT": "0"})
    assert result.returncode == 0
    assert _calls(env) == ["python -m trader.worker"]


@pytest.mark.parametrize("code", [2, 3])
def test_lock_exits_wait_before_returning(env: dict[str, str], code: int) -> None:
    result = _run({**env, "STUB_EXIT": str(code)})
    assert result.returncode == code
    assert _calls(env) == ["python -m trader.worker", "sleep 7"]


def test_default_delay_is_30_seconds(env: dict[str, str]) -> None:
    without = {k: v for k, v in env.items() if k != "WORKER_RESTART_DELAY"}
    assert _run({**without, "STUB_EXIT": "2"}).returncode == 2
    assert _calls(env)[-1] == "sleep 30"


@pytest.mark.parametrize("code", [1, 4])
def test_other_exits_return_at_once(env: dict[str, str], code: int) -> None:
    """1 (setup failed) and 4 (the live run changed: restart on the new run now) never wait."""
    result = _run({**env, "STUB_EXIT": str(code)})
    assert result.returncode == code
    assert _calls(env) == ["python -m trader.worker"]


def _wait_for(env: dict[str, str], line: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if line in _calls(env):
            return
        time.sleep(0.05)
    raise AssertionError(f"{line!r} never logged: {_calls(env)}")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_reaches_the_worker_and_its_code_is_returned_without_sleeping(
    env: dict[str, str], sig: signal.Signals
) -> None:
    """supervisord's TERM (or a Ctrl-C) reaches the worker, which then writes its `stopped` heartbeat; the
    script exits with the worker's code, even a lock code, without the restart delay."""
    run_env = {**env, "STUB_WAIT": "1", "STUB_TERM_EXIT": "2"}
    proc = subprocess.Popen(
        ["bash", str(SCRIPT)], env=run_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        _wait_for(env, "started")
        os.kill(proc.pid, sig)
        code = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert code == 2
    calls = _calls(env)
    assert "TERM" in calls
    assert not any(c.startswith("sleep") for c in calls)
