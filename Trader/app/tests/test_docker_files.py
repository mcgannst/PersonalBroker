"""P4-T17: the image, supervisord, the entrypoint, the compose files and the deploy and smoke scripts.

File-level checks: no Docker is needed. The shell scripts run with stub executables first on PATH (the stubs
append their arguments to a log), so nothing is built, shipped or migrated.
"""

import configparser
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from annotated_types import Le

from trader.settings_store import RuntimeSettings
from trader.worker import BOT_STOP_GRACE_SECONDS, RELAY_STOP_SECONDS

TRADER = Path(__file__).resolve().parents[2]  # the Trader/ folder: the image's build context
DOCKER = TRADER / "docker"
DOCKERFILE = DOCKER / "Dockerfile"
SUPERVISORD = DOCKER / "supervisord.conf"
ENTRYPOINT = DOCKER / "entrypoint.sh"
DEPLOY = DOCKER / "deploy.sh"
SMOKE = DOCKER / "smoke.sh"
PROGRAMS = ("api", "worker", "options-worker", "cron")  # OPTSIM-T16 added the options worker

# Throwaway values the stubbed runs see. Each must never appear in a script's output.
OWNER_URL = "postgresql+psycopg://owner:Owner-Pw-7f3a@db:5432/x"
APP_URL = "postgresql+psycopg://app:App-Pw-91cd@db:5432/x"
ADMIN_PASSWORD = "Admin-Pw-2b6e44"
SESSION_SECRET = "Session-Secret-c0ffee"
ENCRYPTION_KEY = "Enc-Key-5eed5eed"
SECRET_VALUES = (
    OWNER_URL,
    "Owner-Pw-7f3a",
    APP_URL,
    "App-Pw-91cd",
    ADMIN_PASSWORD,
    SESSION_SECRET,
    ENCRYPTION_KEY,
)


# --- helpers ------------------------------------------------------------------------------------------------


def _stub(folder: Path, name: str, body: str) -> None:
    """An executable `name` in `folder` that appends `name args...` to $STUB_LOG, then runs `body`."""
    path = folder / name
    path.write_text(f'#!/bin/bash\necho "{name} $*" >> "$STUB_LOG"\n{body}\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _log(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _no_secret_in(text: str) -> None:
    for value in SECRET_VALUES:
        assert value not in text


def _instructions(dockerfile: Path) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, continuation lines joined and comments dropped."""
    joined, pending = [], ""
    for raw in dockerfile.read_text().splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined.append(pending + line)
        pending = ""
    out = []
    for line in joined:
        word, _, rest = line.partition(" ")
        out.append((word.upper(), rest.strip()))
    return out


def _stages(dockerfile: Path) -> dict[str, list[tuple[str, str]]]:
    """Instructions per build stage, keyed by the stage name (`FROM image AS name`)."""
    stages: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for word, rest in _instructions(dockerfile):
        if word == "FROM":
            match = re.fullmatch(r"(?:--platform=\S+\s+)?(\S+)\s+AS\s+(\S+)", rest, re.IGNORECASE)
            assert match, rest
            current = [("FROM", match.group(1))]
            stages[match.group(2)] = current
        elif current is not None:
            current.append((word, rest))
    return stages


def _env(stage: list[tuple[str, str]]) -> dict[str, str]:
    """The stage's `ENV KEY=value ...` settings (values without quotes)."""
    env: dict[str, str] = {}
    for word, rest in stage:
        if word == "ENV":
            for item in shlex.split(rest):
                key, sep, value = item.partition("=")
                assert sep, f"ENV {rest!r}: use KEY=value"
                env[key] = value
    return env


def _supervisord() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(SUPERVISORD)
    return parser


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|m)?", str(value))
    assert match, value
    return int(match.group(1)) * (60 if match.group(2) == "m" else 1)


def _compose(name: str) -> dict:
    return yaml.safe_load((DOCKER / name).read_text())


def _bash_n(script: Path) -> None:
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- 1. .dockerignore ---------------------------------------------------------------------------------------


def test_dockerignore_keeps_secrets_and_bulk_out_of_the_build_context() -> None:
    lines = {
        line.strip()
        for line in (TRADER / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    for pattern in (
        "docker/.env*",
        "**/.env*",
        ".git",
        "app/.venv",
        "app/tests",
        "web/node_modules",
        "web/dist",
        "spikes",
        "docs",
        "**/__pycache__",
    ):
        assert pattern in lines, pattern
    assert not any(line.startswith("!") for line in lines), "nothing is re-included"


# --- 2. Dockerfile ------------------------------------------------------------------------------------------


def test_dockerfile_has_three_stages_from_the_named_base_images() -> None:
    stages = _stages(DOCKERFILE)
    assert list(stages) == ["web", "build", "runtime"]
    assert stages["web"][0][1] == "node:22-alpine"
    assert stages["build"][0][1] == "python:3.12-slim-bookworm"
    assert stages["runtime"][0][1] == "python:3.12-slim-bookworm"
    web = " ".join(rest for word, rest in stages["web"] if word == "RUN")
    assert "npm ci" in web and "npm run build" in web
    build = " ".join(rest for word, rest in stages["build"] if word == "RUN")
    assert "uv sync --frozen --no-dev --group deploy --compile-bytecode" in build


def test_dockerfile_runtime_runs_as_trader_10001_with_the_expected_environment() -> None:
    runtime = _stages(DOCKERFILE)["runtime"]
    users = [rest for word, rest in runtime if word == "USER"]
    assert users == ["trader"]
    runs = " ".join(rest for word, rest in runtime if word == "RUN")
    assert re.search(r"groupadd\b.*--gid 10001 trader", runs)
    assert re.search(r"useradd\b.*--uid 10001\b.*trader", runs)
    env = _env(runtime)
    assert "PYTHONTZPATH" in env and env["PYTHONTZPATH"] == ""
    assert env["PATH"].startswith("/app/.venv/bin:")
    assert env["HOME"].startswith("/tmp/")
    assert env["TRADER_FINVIZ_CACHE_DIR"].startswith("/tmp/")
    assert env["WEB_DIST_DIR"] == "/app/web/dist"
    assert env["TZ"] == "UTC"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "tzdata" in runs and "ca-certificates" in runs  # Debian tzdata for supercronic's CRON_TZ
    entry = [rest for word, rest in runtime if word == "ENTRYPOINT"]
    assert entry == ['["/app/docker/entrypoint.sh"]']
    assert [rest for word, rest in runtime if word == "CMD"] == ['["all"]']
    health = [rest for word, rest in runtime if word == "HEALTHCHECK"]
    assert len(health) == 1 and "http://127.0.0.1:8000/api/health" in health[0] and "urllib" in health[0]


def test_dockerfile_copies_no_env_file_and_verifies_supercronic() -> None:
    instructions = _instructions(DOCKERFILE)
    for word, rest in instructions:
        if word in ("COPY", "ADD"):
            assert ".env" not in rest, rest
            assert word == "COPY", "ADD fetches unverified content"
    text = " ".join(rest for word, rest in instructions if word in ("RUN", "ARG"))
    assert re.search(r"SUPERCRONIC_VERSION=v\d+\.\d+\.\d+", text)
    assert re.search(r"SUPERCRONIC_SHA1SUM_AMD64=[0-9a-f]{40}\b", text)
    assert re.search(r"SUPERCRONIC_SHA1SUM_ARM64=[0-9a-f]{40}\b", text)
    assert "sha1sum -c" in text


# --- 3. supervisord.conf ------------------------------------------------------------------------------------


def _max_poll_timeout() -> int:
    field = RuntimeSettings.model_fields["telegram_poll_timeout_seconds"]
    [le] = [m.le for m in field.metadata if isinstance(m, Le)]
    return int(le)


def test_supervisord_runs_three_programs_and_gives_the_worker_time_to_stop() -> None:
    conf = _supervisord()
    programs = [s.removeprefix("program:") for s in conf.sections() if s.startswith("program:")]
    assert sorted(programs) == sorted(PROGRAMS)
    worker = conf["program:worker"]
    assert worker["stopsignal"] == "TERM"
    stopwait = int(worker["stopwaitsecs"])
    assert stopwait >= RELAY_STOP_SECONDS
    assert stopwait >= _max_poll_timeout() + BOT_STOP_GRACE_SECONDS + 5
    assert worker["command"] == "/app/docker/run-worker.sh"
    assert worker["autorestart"] == "true"
    assert "supercronic" in conf["program:cron"]["command"]
    assert conf["program:cron"]["command"].endswith("/app/docker/crontab")
    assert conf["program:api"]["command"].endswith("-m trader.api")


def test_supervisord_logs_to_stdout_and_keeps_its_files_under_tmp() -> None:
    conf = _supervisord()
    for name in PROGRAMS:
        program = conf[f"program:{name}"]
        assert program["stdout_logfile"] == "/dev/stdout", name
        assert program["stdout_logfile_maxbytes"] == "0", name
        assert program["redirect_stderr"] == "true", name
    assert conf["supervisord"]["nodaemon"] == "true"
    assert conf["supervisord"]["pidfile"].startswith("/tmp/")
    assert conf["supervisord"]["logfile"].startswith("/app/logs/")
    assert conf["unix_http_server"]["file"].startswith("/tmp/")
    assert conf["supervisorctl"]["serverurl"] == "unix://" + conf["unix_http_server"]["file"]


def test_supervisord_stops_cron_then_worker_then_api() -> None:
    """supervisord stops the highest priority number first: no new cron job starts while the worker is
    stopping, and the web stays up until the worker has written its `stopped` heartbeat. The options worker
    (OPTSIM-T16) stops after cron and before the stock worker."""
    conf = _supervisord()
    priority = {name: int(conf[f"program:{name}"]["priority"]) for name in PROGRAMS}
    assert priority["cron"] > priority["options-worker"] > priority["worker"] > priority["api"]


def test_supervisord_runs_the_options_worker_directly_and_restarts_it() -> None:
    """OPTSIM-T16: `python -m trader.options.worker` with no wrapper script (a second copy sleeps 30 s
    itself before exit 2, so supervisord never spins), restarted on every exit (2 lock held, 3 lock lost,
    4 the options run changed), with at least the worker's own 30 s to finish its step and stop."""
    from trader.options.worker import LOCK_HELD_SLEEP_SECONDS

    program = _supervisord()["program:options-worker"]
    assert program["command"] == "/app/.venv/bin/python -m trader.options.worker"
    assert program["priority"] == "250"
    assert program["autostart"] == "true" and program["autorestart"] == "true"
    assert program["stopsignal"] == "TERM"
    assert int(program["stopwaitsecs"]) >= 30
    assert int(program["startretries"]) >= 100  # a failing start never exhausts supervisord's retries
    assert LOCK_HELD_SLEEP_SECONDS > int(program["startsecs"])  # exit 2 comes after a real wait
    # the module supervisord names exists and is runnable as a module
    assert (TRADER / "app" / "trader" / "options" / "worker.py").exists()
    assert (TRADER / "app" / "trader" / "options" / "__main__.py").exists()


# --- 4. compose files ---------------------------------------------------------------------------------------


def _stopwait_sum() -> int:
    conf = _supervisord()
    return sum(int(conf[f"program:{name}"]["stopwaitsecs"]) for name in PROGRAMS)


@pytest.mark.parametrize(
    ("name", "project", "container", "env_file", "volume", "image", "app_env"),
    [
        (
            "docker-compose.dev.yml",
            "trader-dev",
            "trader-dev",
            "${TRADER_ENV_FILE:-.env.dev}",
            "trader_dev_logs",
            "trader:dev",
            "dev",
        ),
        (
            "docker-compose.prod.yml",
            "trader",
            "trader",
            "${TRADER_ENV_FILE:-.env.prod}",
            "trader_logs",
            "trader:${TRADER_TAG}",
            "prod",
        ),
    ],
)
def test_compose_runs_read_only_on_the_proxy_network_without_ports(
    name: str, project: str, container: str, env_file: str, volume: str, image: str, app_env: str
) -> None:
    compose = _compose(name)
    assert compose["name"] == project
    assert list(compose["services"]) == ["trader"]
    svc = compose["services"]["trader"]
    assert svc["container_name"] == container
    assert svc["image"] == image
    assert svc["env_file"] == [env_file] or svc["env_file"] == env_file
    assert svc["environment"]["APP_ENV"] == app_env
    assert svc["read_only"] is True
    assert any(t.startswith("/tmp:") or t == "/tmp" for t in svc["tmpfs"])
    assert svc["volumes"] == [f"{volume}:/app/logs"]
    assert sorted(svc["networks"]) == ["proxy", "trader_internal"]
    assert "ports" not in svc
    assert svc["init"] is True
    assert svc["restart"] == "unless-stopped"
    assert _seconds(svc["stop_grace_period"]) >= _stopwait_sum()
    assert compose["volumes"] == {volume: {}} or list(compose["volumes"]) == [volume]
    assert compose["networks"]["proxy"] == {"external": True}
    assert compose["networks"]["trader_internal"]["driver"] == "bridge"


def test_smoke_compose_publishes_only_on_localhost() -> None:
    compose = _compose("docker-compose.smoke.yml")
    assert compose["name"] == "trader-smoke"
    db, app = compose["services"]["db"], compose["services"]["trader"]
    assert db["image"] == "postgres:14-alpine"
    assert db["ports"] == ["127.0.0.1:15432:5432"]
    assert any(v.endswith("/docker-entrypoint-initdb.d/init.sql:ro") for v in db["volumes"])
    assert app["image"] == "trader:smoke"
    assert app["command"] == ["api"]
    assert app["ports"] == ["127.0.0.1:18000:8000"]
    assert app["read_only"] is True and app["init"] is True
    assert "SMOKE_ENV_FILE" in str(app["env_file"])
    assert "proxy" not in compose.get("networks", {})


def test_smoke_init_sql_sets_up_owner_and_app_roles_like_trader_dev() -> None:
    sql = (DOCKER / "smoke" / "init.sql").read_text()
    assert re.search(r"CREATE ROLE trader_smoke_owner LOGIN", sql)
    assert re.search(r"CREATE ROLE trader_smoke_app LOGIN", sql)
    assert "CREATE SCHEMA trader AUTHORIZATION trader_smoke_owner" in sql
    assert "GRANT USAGE ON SCHEMA trader TO trader_smoke_app" in sql
    defaults = r"ALTER DEFAULT PRIVILEGES FOR ROLE trader_smoke_owner IN SCHEMA trader\s+GRANT .* ON "
    assert re.search(defaults + "TABLES", sql)
    assert re.search(defaults + "SEQUENCES", sql)


# --- 5. entrypoint.sh ---------------------------------------------------------------------------------------


@pytest.fixture
def entry_env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    fail = 'if [ "$3" = "upgrade" ] && [ -n "${STUB_ALEMBIC_FAIL:-}" ]; then exit 1; fi\n'
    _stub(stubs, "alembic", fail + 'if [ "$3" = "current" ]; then echo "0005 (head)"; fi\nexit 0')
    _stub(stubs, "trader", 'exit "${STUB_ADMIN_EXIT:-0}"')
    env_dump = (
        'echo "env MIGRATION_DATABASE_URL=${MIGRATION_DATABASE_URL-unset}" >> "$STUB_LOG"\n'
        'echo "env ADMIN_PASSWORD_INITIAL=${ADMIN_PASSWORD_INITIAL-unset}" >> "$STUB_LOG"\n'
        'echo "env DATABASE_URL=${DATABASE_URL-unset}" >> "$STUB_LOG"\nexit 0'
    )
    _stub(stubs, "supervisord", env_dump)
    _stub(stubs, "sleep", "exit 0")
    return {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "HOME": str(tmp_path / "home"),
        "TRADER_FINVIZ_CACHE_DIR": str(tmp_path / "cache" / "trader" / "finviz"),
        "MIGRATION_DATABASE_URL": OWNER_URL,
        "DATABASE_URL": APP_URL,
        "ADMIN_PASSWORD_INITIAL": ADMIN_PASSWORD,
        "SESSION_SECRET": SESSION_SECRET,
        "APP_ENCRYPTION_KEY": ENCRYPTION_KEY,
    }


def _entry(env: dict[str, str], mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ENTRYPOINT), mode], env=env, capture_output=True, text=True, timeout=30
    )


def test_entrypoint_is_valid_bash_and_never_traces() -> None:
    _bash_n(ENTRYPOINT)
    text = ENTRYPOINT.read_text()
    assert "set -eu" in text
    assert "set -x" not in text and "set -o xtrace" not in text


def test_entrypoint_migrate_runs_alembic_then_create_admin_and_exits_0(entry_env: dict[str, str]) -> None:
    result = _entry(entry_env, "migrate")
    assert result.returncode == 0, result.stderr
    calls = _log(Path(entry_env["STUB_LOG"]))
    upgrade = calls.index("alembic -c /app/alembic.ini upgrade head")
    admin = calls.index("trader create-admin")
    assert upgrade < admin
    assert not any(c.startswith("supervisord") for c in calls)
    assert "migrate" in result.stdout and "0005 (head)" in result.stdout  # mode and revision are printed
    assert Path(entry_env["HOME"]).is_dir()
    assert Path(entry_env["TRADER_FINVIZ_CACHE_DIR"]).parent.is_dir()
    assert not Path(entry_env["TRADER_FINVIZ_CACHE_DIR"]).exists()  # the scraper creates it 0o700 itself
    _no_secret_in(result.stdout + result.stderr)


def test_entrypoint_all_execs_supervisord_without_the_owner_url_or_admin_password(
    entry_env: dict[str, str],
) -> None:
    result = _entry(entry_env, "all")
    assert result.returncode == 0, result.stderr
    calls = _log(Path(entry_env["STUB_LOG"]))
    assert calls.index("trader create-admin") < calls.index("supervisord -c /app/docker/supervisord.conf")
    assert "env MIGRATION_DATABASE_URL=unset" in calls
    assert "env ADMIN_PASSWORD_INITIAL=unset" in calls
    assert f"env DATABASE_URL={APP_URL}" in calls  # the app role's URL stays
    _no_secret_in(result.stdout + result.stderr)


def test_entrypoint_starts_supervisord_even_when_create_admin_fails(entry_env: dict[str, str]) -> None:
    result = _entry({**entry_env, "STUB_ADMIN_EXIT": "1"}, "all")
    assert result.returncode == 0, result.stderr
    calls = _log(Path(entry_env["STUB_LOG"]))
    assert "supervisord -c /app/docker/supervisord.conf" in calls
    assert "create-admin" in result.stdout and "1" in result.stdout  # the exit code is reported
    _no_secret_in(result.stdout + result.stderr)


def test_entrypoint_gives_up_after_five_failed_migrations(entry_env: dict[str, str]) -> None:
    result = _entry({**entry_env, "STUB_ALEMBIC_FAIL": "1"}, "all")
    assert result.returncode == 1
    calls = _log(Path(entry_env["STUB_LOG"]))
    assert calls.count("alembic -c /app/alembic.ini upgrade head") == 5
    assert calls.count("sleep 5") == 4  # 5 s between tries
    assert not any(c.startswith("trader") or c.startswith("supervisord") for c in calls)
    _no_secret_in(result.stdout + result.stderr)


@pytest.mark.parametrize("mode", ["worker", "cron"])
def test_entrypoint_worker_and_cron_skip_migrations(
    entry_env: dict[str, str], tmp_path: Path, mode: str
) -> None:
    stubs = Path(entry_env["PATH"].split(":")[0])
    _stub(stubs, "supercronic", "exit 0")
    # run-worker.sh and the crontab live in /app/docker in the image; here the stub folder stands in for it.
    _stub(stubs, "run-worker.sh", "exit 0")
    result = _entry({**entry_env, "TRADER_DOCKER_DIR": str(stubs)}, mode)
    assert result.returncode == 0, result.stderr
    calls = _log(Path(entry_env["STUB_LOG"]))
    assert not any(c.startswith("alembic") or c.startswith("trader") for c in calls)
    expected = "run-worker.sh " if mode == "worker" else f"supercronic -passthrough-logs {stubs}/crontab"
    assert expected in calls


def test_entrypoint_rejects_an_unknown_mode(entry_env: dict[str, str]) -> None:
    result = _entry(entry_env, "bogus")
    assert result.returncode != 0
    assert _log(Path(entry_env["STUB_LOG"])) == []


# --- 7. deploy.sh -------------------------------------------------------------------------------------------


def _env_file(path: Path, *, skip: tuple[str, ...] = ()) -> Path:
    values = {
        "DATABASE_URL": APP_URL,
        "MIGRATION_DATABASE_URL": OWNER_URL,
        "APP_ENCRYPTION_KEY": ENCRYPTION_KEY,
        "SESSION_SECRET": SESSION_SECRET,
        "ADMIN_USERNAME": "stephen",
        "ADMIN_PASSWORD_INITIAL": ADMIN_PASSWORD,
    }
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if k not in skip))
    return path


# P6-T6: a stub git for the prod guards. STUB_GIT_STATUS is `status --porcelain`'s output (empty: clean);
# STUB_GIT_TAG is what `describe --exact-match` finds at HEAD (unset: no tag); STUB_GIT_HEAD is the plain
# `describe` of HEAD (the version string).
GIT_STUB = """case "$*" in
  *"status --porcelain"*) printf "%s" "${STUB_GIT_STATUS-}" ;;
  *--exact-match*)
    if [ -z "${STUB_GIT_TAG-}" ]; then
      echo "fatal: no tag exactly matches" >&2
      exit 128
    fi
    echo "$STUB_GIT_TAG" ;;
  *describe*) echo "${STUB_GIT_HEAD:-stub-version}" ;;
esac
exit 0"""
STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


@pytest.fixture
def deploy_env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    _stub(
        stubs,
        "docker",
        'echo "docker-env DOCKER_DEFAULT_PLATFORM=${DOCKER_DEFAULT_PLATFORM-} '
        'TRADER_ENV_FILE=${TRADER_ENV_FILE-}" >> "$STUB_LOG"\nexit 0',
    )
    _stub(stubs, "ssh", "cat > /dev/null\nexit 0")
    _stub(stubs, "curl", 'printf "%s" "${STUB_HTTP_CODE:-200}"')
    _stub(stubs, "sleep", "exit 0")
    _stub(stubs, "git", GIT_STUB)
    return {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "TRADER_ENV_FILE": str(_env_file(tmp_path / "env.test")),
    }


def _deploy(env: dict[str, str], *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEPLOY), *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=60
    )


def test_deploy_is_valid_bash() -> None:
    _bash_n(DEPLOY)


def test_deploy_dev_builds_ships_recreates_and_waits_for_health(
    deploy_env: dict[str, str], tmp_path: Path
) -> None:
    result = _deploy(deploy_env, "dev", cwd=tmp_path)  # from an unrelated directory
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [c for c in _log(Path(deploy_env["STUB_LOG"])) if not c.startswith("docker-env")]
    kinds = []
    for call in calls:
        if call.startswith("docker --context desktop-linux build"):
            kinds.append("build")
            assert "--platform linux/amd64" in call
            assert f"-f {DOCKER}/Dockerfile" in call
            assert "--build-arg APP_VERSION=" in call
            assert "-t trader:dev" in call
            assert call.endswith(f" {TRADER}")
        elif call == "docker --context desktop-linux save trader:dev":
            kinds.append("save")
        elif call == "ssh stephen@192.168.68.73 docker load":
            kinds.append("load")
        elif call.startswith("docker --context shared-docker-server compose"):
            kinds.append("compose")
            assert f"-f {DOCKER}/docker-compose.dev.yml" in call
            assert call.endswith("up -d --no-build --no-deps --force-recreate trader")
        elif call.startswith("curl"):
            kinds.append("health")
            assert "https://trader-dev.sunspinner.ca/api/health" in call
    assert kinds == ["build", "save", "load", "compose", "health"]
    envs = [c for c in _log(Path(deploy_env["STUB_LOG"])) if c.startswith("docker-env")]
    assert "DOCKER_DEFAULT_PLATFORM=linux/amd64" in envs[0]
    assert all(f"TRADER_ENV_FILE={deploy_env['TRADER_ENV_FILE']}" in e for e in envs)
    assert "200" in result.stdout
    _no_secret_in(result.stdout + result.stderr)


def test_deploy_refuses_an_env_file_missing_a_required_key(
    deploy_env: dict[str, str], tmp_path: Path
) -> None:
    env_file = _env_file(tmp_path / "env.nosecret", skip=("SESSION_SECRET",))
    result = _deploy({**deploy_env, "TRADER_ENV_FILE": str(env_file)}, "dev")
    assert result.returncode == 1
    assert "SESSION_SECRET" in result.stdout + result.stderr
    assert _log(Path(deploy_env["STUB_LOG"])) == []  # nothing built
    _no_secret_in(result.stdout + result.stderr)


@pytest.mark.parametrize(
    ("line", "accepted"),
    [
        ("SESSION_SECRET=", False),
        ("SESSION_SECRET= ", False),
        ('SESSION_SECRET=""', False),
        ("SESSION_SECRET=''", False),
        ("# SESSION_SECRET=x", False),
        (f"SESSION_SECRET= {SESSION_SECRET}", True),  # fix round 1: a space after `=` is still a value
        (f'SESSION_SECRET="{SESSION_SECRET}"', True),
        (f"export SESSION_SECRET={SESSION_SECRET}", True),
    ],
)
def test_deploy_needs_a_non_empty_value_for_each_required_key(
    deploy_env: dict[str, str], tmp_path: Path, line: str, accepted: bool
) -> None:
    env_file = _env_file(tmp_path / "env.value", skip=("SESSION_SECRET",))
    env_file.write_text(env_file.read_text() + line + "\n")
    result = _deploy({**deploy_env, "TRADER_ENV_FILE": str(env_file)}, "dev")
    assert (result.returncode == 0) is accepted, (line, result.stdout + result.stderr)
    if not accepted:
        assert "SESSION_SECRET" in result.stderr
        assert _log(Path(deploy_env["STUB_LOG"])) == []
    _no_secret_in(result.stdout + result.stderr)


def test_deploy_refuses_a_missing_env_file(deploy_env: dict[str, str], tmp_path: Path) -> None:
    result = _deploy({**deploy_env, "TRADER_ENV_FILE": str(tmp_path / "absent")}, "dev")
    assert result.returncode == 1
    assert _log(Path(deploy_env["STUB_LOG"])) == []


def test_deploy_only_warns_about_missing_admin_values(deploy_env: dict[str, str], tmp_path: Path) -> None:
    env_file = _env_file(tmp_path / "env.noadmin", skip=("ADMIN_PASSWORD_INITIAL",))
    result = _deploy({**deploy_env, "TRADER_ENV_FILE": str(env_file)}, "dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ADMIN_PASSWORD_INITIAL" in result.stdout + result.stderr
    assert any(c.startswith("curl") for c in _log(Path(deploy_env["STUB_LOG"])))


def test_deploy_fails_when_health_never_returns_200(deploy_env: dict[str, str]) -> None:
    env = {**deploy_env, "STUB_HTTP_CODE": "502", "TRADER_HEALTH_TIMEOUT": "9", "TRADER_HEALTH_INTERVAL": "3"}
    result = _deploy(env, "dev")
    assert result.returncode == 1
    assert "502" in result.stdout + result.stderr
    assert sum(c.startswith("curl") for c in _log(Path(deploy_env["STUB_LOG"]))) >= 3


def test_deploy_prod_needs_a_tag(deploy_env: dict[str, str]) -> None:
    env = {k: v for k, v in deploy_env.items() if k != "TRADER_TAG"}
    result = _deploy(env, "prod")
    assert result.returncode == 1
    assert "TRADER_TAG" in result.stdout + result.stderr
    assert _log(Path(deploy_env["STUB_LOG"])) == []


def test_deploy_prod_uses_the_tag_and_the_prod_host(deploy_env: dict[str, str]) -> None:
    result = _deploy({**deploy_env, "TRADER_TAG": "v1", "STUB_GIT_TAG": "v1"}, "prod")  # clean, at the tag
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _log(Path(deploy_env["STUB_LOG"]))
    assert any(c.startswith("docker --context desktop-linux build") and "-t trader:v1" in c for c in calls)
    assert "docker --context desktop-linux save trader:v1" in calls
    assert any("docker-compose.prod.yml" in c for c in calls)
    assert any("https://trader.sunspinner.ca/api/health" in c for c in calls if c.startswith("curl"))
    assert f"git -C {TRADER} status --porcelain" in calls
    assert f"git -C {TRADER} describe --exact-match --tags --match v1 HEAD" in calls
    assert "--container trader\n" in result.stdout + "\n"


def _docker_calls(env: dict[str, str]) -> list[str]:
    return [c for c in _log(Path(env["STUB_LOG"])) if c.startswith("docker ")]


def test_deploy_prod_refuses_a_dirty_tree(deploy_env: dict[str, str]) -> None:
    env = {**deploy_env, "TRADER_TAG": "v1", "STUB_GIT_TAG": "v1", "STUB_GIT_STATUS": " M Trader/app/x.py\n"}
    result = _deploy(env, "prod")
    assert result.returncode == 1
    assert "working tree is dirty" in result.stderr
    assert _docker_calls(deploy_env) == []  # no build


def test_deploy_prod_refuses_a_head_that_is_not_the_tag(deploy_env: dict[str, str]) -> None:
    env = {**deploy_env, "TRADER_TAG": "v1.0.0", "STUB_GIT_HEAD": "phase-5-complete-12-gabc1234"}
    result = _deploy(env, "prod")
    assert result.returncode == 1
    assert "phase-5-complete-12-gabc1234" in result.stderr and "v1.0.0" in result.stderr  # names both
    assert _docker_calls(deploy_env) == []


@pytest.mark.parametrize("tag", ["dev", "latest"])
def test_deploy_prod_refuses_the_dev_and_latest_tags(deploy_env: dict[str, str], tag: str) -> None:
    result = _deploy({**deploy_env, "TRADER_TAG": tag, "STUB_GIT_TAG": tag}, "prod")
    assert result.returncode == 1
    assert f"TRADER_TAG={tag}" in result.stderr
    assert _docker_calls(deploy_env) == []


def test_deploy_dev_skips_the_prod_guards(deploy_env: dict[str, str]) -> None:
    result = _deploy({**deploy_env, "STUB_GIT_STATUS": " M dirty\n"}, "dev")  # dev may deploy a dirty tree
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("status --porcelain" in c for c in _log(Path(deploy_env["STUB_LOG"])))


def test_deploy_dev_prints_the_downtime_window_and_the_catch_up_hint(deploy_env: dict[str, str]) -> None:
    result = _deploy(deploy_env, "dev")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    down = next(i for i, line in enumerate(lines) if line.startswith("deploy: down from "))
    health = next(i for i, line in enumerate(lines) if line.startswith("deploy: health 200"))
    up = next(i for i, line in enumerate(lines) if line.startswith("deploy: up at "))
    assert lines[down - 1].startswith("deploy: recreating")  # just before the recreate
    assert down < health < up
    down_at = re.fullmatch(f"deploy: down from ({STAMP})", lines[down])
    up_at = re.fullmatch(f"deploy: up at ({STAMP})", lines[up])
    assert down_at and up_at
    hint = (
        "deploy: run uv --directory Trader/app run python ../build/cron_gap.py "
        f"--from {down_at.group(1)} --to {up_at.group(1)} --container trader-dev"
    )
    assert lines[up + 1] == hint
    # The recreate itself runs after the stamp: the compose call is logged after the build and ship.
    calls = _docker_calls(deploy_env)
    assert calls[-1].startswith("docker --context shared-docker-server compose")


def test_deploy_still_prints_the_down_stamp_and_hint_when_health_fails(deploy_env: dict[str, str]) -> None:
    env = {**deploy_env, "STUB_HTTP_CODE": "502", "TRADER_HEALTH_TIMEOUT": "3", "TRADER_HEALTH_INTERVAL": "3"}
    result = _deploy(env, "dev")
    assert result.returncode == 1
    assert re.search(f"deploy: down from ({STAMP})", result.stdout)
    assert "deploy: up at" not in result.stdout
    assert re.search(f"cron_gap.py --from {STAMP} --to {STAMP} --container trader-dev", result.stdout), (
        result.stdout
    )


def test_cron_gap_accepts_the_stamps_deploy_prints(deploy_env: dict[str, str]) -> None:
    """P6-T6 test 14: the printed window goes straight into cron_gap.py."""
    from tests.build import load_build_script

    result = _deploy(deploy_env, "dev")
    assert result.returncode == 0, result.stdout + result.stderr
    down = re.search(f"deploy: down from ({STAMP})", result.stdout)
    up = re.search(f"deploy: up at ({STAMP})", result.stdout)
    assert down and up
    cron_gap = load_build_script("cron_gap")
    assert cron_gap.main(["--from", down.group(1), "--to", up.group(1), "--container", "trader-dev"]) == 0


def test_deploy_rejects_an_unknown_environment(deploy_env: dict[str, str]) -> None:
    result = _deploy(deploy_env, "staging")
    assert result.returncode != 0
    assert _log(Path(deploy_env["STUB_LOG"])) == []


# --- 8. smoke.sh --------------------------------------------------------------------------------------------


def test_smoke_is_valid_bash_and_always_tears_down() -> None:
    _bash_n(SMOKE)
    lines = SMOKE.read_text().splitlines()
    trap = next(i for i, line in enumerate(lines) if line.strip().startswith("trap "))
    up = next(i for i, line in enumerate(lines) if re.search(r"compose .* up -d", line))
    assert trap < up, "the trap must be set before the stack starts"
    cleanup = SMOKE.read_text()
    assert "down -v" in cleanup
    assert "rm -f" in cleanup  # the temporary env file
    assert "trader:smoke" in cleanup
    assert "linux/amd64" not in cleanup  # native platform: it never replaces trader:dev
