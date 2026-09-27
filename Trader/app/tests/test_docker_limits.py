"""P5-T16: container resource and log limits in both compose files (SPEC §15, §15.1).

File-level checks: no Docker is needed. The P4 compose checks (read-only, tmpfs, volume, networks, no ports,
env file, init, stop grace period) stay in tests/test_docker_files.py, unchanged.
"""

import re
from pathlib import Path

import pytest
import yaml

DOCKER = Path(__file__).resolve().parents[2] / "docker"
COMPOSE_FILES = ("docker-compose.dev.yml", "docker-compose.prod.yml")
GIB = 1024**3
UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _bytes(value: str | int) -> int:
    """A Docker byte size such as `1g`, `256m` or `1024` in bytes (binary units, as Docker reads them)."""
    match = re.fullmatch(r"(\d+)\s*([bkmg]?)b?", str(value).strip().lower())
    assert match, value
    return int(match.group(1)) * UNITS[match.group(2)]


def _service(name: str) -> dict:
    compose = yaml.safe_load((DOCKER / name).read_text())
    return compose["services"]["trader"]


def _tmpfs_bytes(svc: dict) -> int:
    [tmp] = [t for t in svc["tmpfs"] if t.startswith("/tmp:")]
    options = dict(item.split("=", 1) for item in tmp.split(":", 1)[1].split(",") if "=" in item)
    return _bytes(options["size"])


# --- 1. limits and logging ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_limits_memory_cpu_processes_and_files(name: str) -> None:
    svc = _service(name)
    assert _bytes(svc["mem_limit"]) == GIB
    assert _bytes(svc["memswap_limit"]) == GIB  # equal to mem_limit: no swap beyond the limit
    assert float(svc["cpus"]) == 2.0
    assert int(svc["pids_limit"]) == 256
    assert svc["ulimits"] == {"nofile": {"soft": 4096, "hard": 8192}}


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_caps_docker_logs_at_10_mb_times_5(name: str) -> None:
    logging = _service(name)["logging"]
    assert logging["driver"] == "json-file"
    options = logging["options"]
    # Compose wants strings here (a bare 5 is an error on some Compose versions).
    assert isinstance(options["max-size"], str)
    assert isinstance(options["max-file"], str)
    assert _bytes(options["max-size"]) == 10 * 1024**2
    assert options["max-file"] == "5"


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_compose_comments_explain_the_limits_and_the_shared_replay(name: str) -> None:
    text = (DOCKER / name).read_text().lower()
    comments = " ".join(line.split("#", 1)[1] for line in text.splitlines() if "#" in line)
    assert "replay" in comments  # the replay subprocess runs inside the container and shares the limits
    for word in ("memory", "cpu", "process", "log"):
        assert word in comments, word


# --- 3. tmpfs cannot take the whole memory budget -----------------------------------------------------------


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_memory_limit_is_at_least_four_times_the_tmpfs(name: str) -> None:
    svc = _service(name)
    tmpfs = _tmpfs_bytes(svc)
    assert tmpfs == 256 * 1024**2
    assert _bytes(svc["mem_limit"]) >= 4 * tmpfs


def test_dev_and_prod_have_the_same_limits() -> None:
    keys = ("mem_limit", "memswap_limit", "cpus", "pids_limit", "ulimits", "logging")
    dev, prod = (_service(name) for name in COMPOSE_FILES)
    assert {k: dev[k] for k in keys} == {k: prod[k] for k in keys}
