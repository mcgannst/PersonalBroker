"""P6-T6 Breaker (gauntlet attempt 1): the promotion tooling.

Targets: secrets (no password, DSN or verifier in any output, exception, log record or subprocess argv on
every failure path of `prod_env.py create-db`; only SCRAM verifiers reach the server; the DSN and passwords
files refused in the repository, under ~/Documents, through a symlink or with a mode other than 600;
identifier injection refused before connecting); `cron_gap.py` over midnight ET, the DST change, weekends,
holidays, early closes and windows covering many jobs, with the session each catch-up must pin;
`deploy.sh prod` guards against real git (dirty, untracked outside Trader/, staged, a tag on another
commit, two tags on one commit, a glob tag, no repository) and its stamps on failure; `env_check.py`
compares without printing a value or a hash. Fakes, a real temporary git repository and a throwaway
PostgreSQL container only: no real database, SSH, Docker host or GitHub."""

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tests.build import load_build_script

TRADER = Path(__file__).resolve().parents[2].parent  # the Trader/ folder
DEPLOY = TRADER / "docker" / "deploy.sh"
EXEC = "docker --context shared-docker-server exec trader-dev trader "
HEX12 = re.compile(r"\b[0-9a-f]{12}\b")


@pytest.fixture(scope="module")
def prod_env() -> ModuleType:
    return load_build_script("prod_env")


@pytest.fixture(scope="module")
def cron_gap() -> ModuleType:
    return load_build_script("cron_gap")


@pytest.fixture(scope="module")
def env_check() -> ModuleType:
    return load_build_script("env_check")


def _no_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in prod_env may start a process (a secret on argv would be `ps`-visible)."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"prod_env started a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)


def _secret_file(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


# --- prod_env: create_db against a throwaway PostgreSQL (statement log on) ---------------------------------

ADMIN_PW = "AdminPwBreaker0123456789Zq"
OWNER_PW = "OwnerPwBreaker0123456789abcdefgh"
APP_PW = "AppPwBreaker0123456789abcdefghijk"
OLD_OWNER_PW = "OldOwnerPwBreaker01"
OLD_APP_PW = "OldAppPwBreaker0123"
STRANGER_PW = "StrangerPwBreaker01"


@pytest.fixture
def pg() -> Iterator[Any]:  # one container per test: each starts from an empty cluster
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer("postgres:14-alpine", password=ADMIN_PW, driver="psycopg").with_command(
        "postgres -c log_statement=all"
    )
    with container as started:
        yield started


@pytest.fixture
def admin(pg: Any) -> dict[str, Any]:
    return {
        "host": pg.get_container_host_ip(),
        "port": int(pg.get_exposed_port(5432)),
        "user": pg.username,
        "password": ADMIN_PW,
        "dbname": "postgres",
    }


def _server_log(pg: Any) -> str:
    stdout, stderr = pg.get_logs()
    return str((stdout + stderr).decode(errors="replace"))


@pytest.mark.db
def test_create_db_cli_failure_paths_never_show_a_secret(
    pg: Any,
    admin: dict[str, Any],
    prod_env: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong admin password, connection refused, a malformed DSN, a bad port, the admin DSN naming another
    database, a database that doesn't exist yet, equal passwords: exit 1 each, and no password, DSN or
    verifier in stdout, stderr or any log record; no subprocess."""
    _no_popen(monkeypatch)
    caplog.set_level(logging.DEBUG)
    wrong_pw = "WrongAdminPwBreaker77"
    refused_pw = "RefusedPwBreaker88"
    malformed_pw = "MalformedPwBreaker99"
    port_pw = "PortPwBreaker66"
    base = {**admin}
    cases = {
        "wrong admin password": make_conninfo(**{**base, "password": wrong_pw}),
        "connection refused": make_conninfo(host="127.0.0.1", port=1, user="x", password=refused_pw),
        "malformed conninfo": f"host=127.0.0.1 password={malformed_pw} dbname=postgres nonsense",
        "bad port": f"postgresql://postgres:{port_pw}@127.0.0.1:notaport/postgres",
        "admin DSN names trader_dev": make_conninfo(**{**base, "dbname": "trader_dev"}),
        "database trader does not exist yet": make_conninfo(**{**base, "dbname": "trader"}),
    }
    pws = _secret_file(tmp_path / "pws", f"owner={OWNER_PW}\napp={APP_PW}\n")
    secrets = [ADMIN_PW, wrong_pw, refused_pw, malformed_pw, port_pw, OWNER_PW, APP_PW]
    for index, (name, dsn) in enumerate(cases.items()):
        dsn_file = _secret_file(tmp_path / f"dsn-{index}", dsn)
        code = prod_env.main(["create-db", "--admin-dsn-file", str(dsn_file), "--passwords-file", str(pws)])
        out = capsys.readouterr()
        text = out.out + out.err
        assert code == 1, (name, text)
        assert "prod_env:" in out.err, name
        for value in [*secrets, dsn]:
            assert value not in text, (name, "value in output")
        assert "SCRAM-SHA-256$" not in text, name
    same = _secret_file(tmp_path / "same", f"owner={OWNER_PW}\napp={OWNER_PW}\n")
    good = _secret_file(tmp_path / "good-dsn", make_conninfo(**admin))
    assert prod_env.main(["create-db", "--admin-dsn-file", str(good), "--passwords-file", str(same)]) == 1
    out = capsys.readouterr()
    assert OWNER_PW not in out.out + out.err
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for value in secrets:
        assert value not in logged
    # Nothing was created by any refused or failed run.
    with psycopg.connect(make_conninfo(**admin), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'trader_%'").fetchone() == (0,)


@pytest.mark.db
def test_create_db_takes_over_existing_roles_and_sends_only_verifiers(
    pg: Any,
    admin: dict[str, Any],
    prod_env: ModuleType,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roles that already exist with too much power and old passwords, and a `trader` database that already
    exists (owned by the admin, PUBLIC may connect): create_db demotes the roles, replaces the passwords,
    revokes PUBLIC; the server's statement log has verifiers only; nothing client-side logs a verifier."""
    _no_popen(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with psycopg.connect(make_conninfo(**admin), autocommit=True) as conn:
        conn.execute(
            f"CREATE ROLE trader_owner LOGIN SUPERUSER CREATEDB CREATEROLE PASSWORD '{OLD_OWNER_PW}'"
        )
        conn.execute(f"CREATE ROLE trader_app LOGIN CREATEDB PASSWORD '{OLD_APP_PW}'")
        conn.execute("CREATE DATABASE trader")
        conn.execute(f"CREATE ROLE breaker_stranger LOGIN PASSWORD '{STRANGER_PW}'")
    lines: list[str] = []
    prod_env.create_db(make_conninfo(**admin), owner_password=OWNER_PW, app_password=APP_PW, out=lines.append)
    assert "role trader_owner: updated" in lines and "role trader_app: updated" in lines
    assert "database trader: exists" in lines
    with psycopg.connect(make_conninfo(**admin), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolpassword FROM pg_authid "
            "WHERE rolname IN ('trader_owner', 'trader_app') ORDER BY rolname"
        ).fetchall()
    for name, is_super, createdb, createrole, verifier in rows:
        assert (is_super, createdb, createrole) == (False, False, False), name
        assert verifier.startswith("SCRAM-SHA-256$4096:"), name
    assert rows[0][4] != rows[1][4]
    for role, old, new in (("trader_owner", OLD_OWNER_PW, OWNER_PW), ("trader_app", OLD_APP_PW, APP_PW)):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(make_conninfo(**{**admin, "user": role, "password": old, "dbname": "trader"}))
        psycopg.connect(make_conninfo(**{**admin, "user": role, "password": new, "dbname": "trader"})).close()
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        psycopg.connect(
            make_conninfo(
                **{**admin, "user": "breaker_stranger", "password": STRANGER_PW, "dbname": "trader"}
            )
        )
    log = _server_log(pg)
    assert "ALTER ROLE" in log and "SCRAM-SHA-256$" in log
    for value in (OWNER_PW, APP_PW):
        assert value not in log
    out = capsys.readouterr()
    client_side = out.out + out.err + "\n".join(lines) + "\n".join(r.getMessage() for r in caplog.records)
    for value in (OWNER_PW, APP_PW, ADMIN_PW, "SCRAM-SHA-256$"):
        assert value not in client_side


# --- prod_env: refusals before connecting -------------------------------------------------------------------


def test_secret_files_refused_by_mode_place_symlink_and_kind(
    prod_env: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every refused DSN or passwords file exits 1 before any connection, and its content never shows."""
    # The real defaults first: the repository root and the real ~/Documents.
    assert prod_env.DOCUMENTS == Path.home() / "Documents"
    assert (Path(prod_env.REPO_ROOT) / "Trader" / "build" / "prod_env.py").is_file()
    repo = tmp_path / "repo"
    documents = tmp_path / "home" / "Documents"
    repo.mkdir()
    documents.mkdir(parents=True)
    monkeypatch.setattr(prod_env, "REPO_ROOT", repo)
    monkeypatch.setattr(prod_env, "DOCUMENTS", documents)

    def no_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("connected although the file was refused")

    monkeypatch.setattr(prod_env, "_connect", no_connect)
    _no_popen(monkeypatch)
    dsn_text = "postgresql://stephen:DsnSecretBreaker42@192.0.2.1:5432/postgres"
    pws_text = f"owner={OWNER_PW}\napp={APP_PW}\n"
    outside = tmp_path / "outside"
    good_pws = _secret_file(outside / "pws", pws_text)
    good_dsn = _secret_file(outside / "dsn", dsn_text)
    bad_dsns = [
        _secret_file(outside / f"dsn-{mode:o}", dsn_text, mode)
        for mode in (0o644, 0o640, 0o604, 0o400, 0o700)
    ]
    in_repo = _secret_file(repo / "Trader" / "dsn", dsn_text)
    in_docs = _secret_file(documents / "dsn", dsn_text)
    link_docs = outside / "link-to-docs"
    link_docs.symlink_to(in_docs)
    link_repo = outside / "link-to-repo"
    link_repo.symlink_to(in_repo)
    empty = _secret_file(outside / "empty", "   \n")
    folder = outside / "folder"
    folder.mkdir()
    cases: list[tuple[Path, Path]] = [(p, good_pws) for p in bad_dsns]
    cases += [
        (p, good_pws) for p in (in_repo, in_docs, link_docs, link_repo, empty, folder, outside / "absent")
    ]
    cases += [(good_dsn, _secret_file(outside / "pws-644", pws_text, 0o644))]
    cases += [(good_dsn, _secret_file(documents / "pws", pws_text))]
    cases += [(good_dsn, _secret_file(outside / "pws-half", f"owner={OWNER_PW}\n"))]
    for dsn_path, pws_path in cases:
        code = prod_env.main(
            ["create-db", "--admin-dsn-file", str(dsn_path), "--passwords-file", str(pws_path)]
        )
        out = capsys.readouterr()
        assert code == 1, (dsn_path.name, pws_path.name, out.err)
        for value in ("DsnSecretBreaker42", dsn_text, OWNER_PW, APP_PW):
            assert value not in out.out + out.err, (dsn_path.name, pws_path.name)


@pytest.mark.parametrize(
    ("kwargs", "admin_db"),
    [
        ({"database": 'trader"; DROP DATABASE trader_dev; --'}, "postgres"),
        ({"database": "Trader"}, "postgres"),
        ({"database": "trader "}, "postgres"),
        ({"database": "trader_dev"}, "postgres"),
        ({"owner_role": 'trader_owner" SUPERUSER --'}, "postgres"),
        ({"owner_role": "trader_dev_owner"}, "postgres"),
        ({"app_role": "trader_dev_app"}, "postgres"),
        ({"app_role": "postgres"}, "postgres"),
        ({}, "trader_dev"),
        ({}, "financetracker"),
        ({}, 'postgres" OR 1=1'),
    ],
)
def test_create_db_refuses_foreign_or_injected_names_before_connecting(
    prod_env: ModuleType, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, str], admin_db: str
) -> None:
    def no_connect(*args: Any, **kw: Any) -> Any:
        raise AssertionError("connected although the names were refused")

    monkeypatch.setattr(prod_env, "_connect", no_connect)
    dsn = make_conninfo(host="192.0.2.1", user="stephen", password="InjectDsnPw31", dbname=admin_db)
    with pytest.raises(prod_env.ProdEnvError) as err:
        prod_env.create_db(dsn, owner_password=OWNER_PW, app_password=APP_PW, **kwargs)
    for value in ("InjectDsnPw31", OWNER_PW, APP_PW, dsn):
        assert value not in str(err.value)
    for url in (
        f"postgresql+psycopg://trader_owner:{OWNER_PW}@h:5432/trader_dev",
        f"postgresql+psycopg://trader_dev_app:{APP_PW}@h:5432/trader",
    ):
        with pytest.raises(prod_env.ProdEnvError) as err:
            prod_env.verify_roles(url, url)
        assert OWNER_PW not in str(err.value) and APP_PW not in str(err.value)


# --- cron_gap: calendar edges -------------------------------------------------------------------------------


def _gap(cron_gap: ModuleType, capsys: pytest.CaptureFixture[str], start: str, end: str) -> list[str]:
    assert cron_gap.main(["--from", start, "--to", end]) == 0
    return capsys.readouterr().out.splitlines()


def _entries(lines: list[str]) -> list[tuple[str, str]]:
    """(ET date-time, command or 'not a session' note) per listed fire, in printed order."""
    result = []
    for line in lines:
        match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) E[SD]T  \S+ \S+ \S+  (.*)$", line)
        if match:
            rest = match.group(2)
            result.append((match.group(1), rest[len(EXEC) :] if rest.startswith(EXEC) else rest))
    return result


def _in_order(expected: list[tuple[str, str]], got: list[tuple[str, str]]) -> None:
    """Every expected entry is present, in this order (other entries may sit between them)."""
    position = 0
    for item in expected:
        assert item in got[position:], (item, got)
        position = got.index(item, position) + 1


def test_a_window_across_midnight_lists_every_job_with_its_own_session(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Thursday 15:50 EDT to Friday 09:25 EDT: Thursday's jobs pin Thursday, the nightly pins Friday, Friday's
    morning jobs pin Friday, the token refresh pins nothing."""
    lines = _gap(cron_gap, capsys, "2026-10-08T19:50:00Z", "2026-10-09T13:25:00Z")
    got = _entries(lines)
    _in_order(
        [
            ("2026-10-08 15:55", "event flatten --date 2026-10-08"),
            ("2026-10-08 15:58", "event flatten --date 2026-10-08"),
            ("2026-10-08 16:15", "postclose --date 2026-10-08"),
            ("2026-10-08 18:05", "soak-report --notify --through 2026-10-08"),
            ("2026-10-08 20:00", "nightly --date 2026-10-09"),
            ("2026-10-09 02:00", "token-refresh"),
            ("2026-10-09 08:00", "premarket --date 2026-10-09"),
            ("2026-10-09 09:20", "preopen --date 2026-10-09"),
        ],
        got,
    )
    assert [t for t, _ in got] == sorted(t for t, _ in got)
    assert not any("orb_open" in c or "checkin" in c for _, c in got)  # outside the window
    assert lines[-1] == "warning: the window overlaps 09:15-16:30 ET on a weekday"


def test_a_weekend_window_before_columbus_day(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Friday 19:00 to Monday 07:00 (Columbus Day is a session): no Friday or Saturday nightly, one Sunday
    nightly for Monday, Saturday's weekly and final soak line keyed to Friday, three token refreshes, no
    session-day job and no market-hours warning."""
    lines = _gap(cron_gap, capsys, "2026-10-09T19:00:00-04:00", "2026-10-12T07:00:00-04:00")
    got = _entries(lines)
    assert got == [
        ("2026-10-10 02:00", "token-refresh"),
        ("2026-10-10 09:00", "weekly --date 2026-10-09"),
        ("2026-10-10 10:30", "soak-report --notify --final --through 2026-10-09"),
        ("2026-10-11 02:00", "token-refresh"),
        ("2026-10-11 20:00", "nightly --date 2026-10-12"),
        ("2026-10-12 02:00", "token-refresh"),
    ]
    assert not any(line.startswith("warning") for line in lines)


def test_holidays_and_early_closes_are_marked_and_pinned(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Thanksgiving: every session-day job reads `not a session`, the soak line settles Wednesday, both
    nightlies prepare the early-close Friday, whose midday flatten backups are real. Christmas (Friday): the
    Thursday nightly prepares Monday, Saturday's weekly is keyed to the early-close Thursday."""
    lines = _gap(cron_gap, capsys, "2026-11-25T19:00:00-05:00", "2026-11-27T13:05:00-05:00")
    got = _entries(lines)
    holiday = [c for t, c in got if t.startswith("2026-11-26") and "not a session" in c]
    assert len(holiday) == 12, got  # premarket, preopen, orb_open, 2 checkins, 2 --due, 4 flattens, postclose
    assert all(c.startswith("not a session (2026-11-26): nothing to run [trader ") for c in holiday)
    assert any("[trader postclose]" in c for c in holiday)
    assert not any("--date 2026-11-26" in c for _, c in got)
    _in_order(
        [
            ("2026-11-25 20:00", "nightly --date 2026-11-27"),
            ("2026-11-26 02:00", "token-refresh"),
            ("2026-11-26 18:05", "soak-report --notify --through 2026-11-25"),
            ("2026-11-26 20:00", "nightly --date 2026-11-27"),
            ("2026-11-27 08:00", "premarket --date 2026-11-27"),
            ("2026-11-27 09:36", "event orb_open --date 2026-11-27"),
            ("2026-11-27 12:32", "event --due --date 2026-11-27"),
            ("2026-11-27 12:55", "event flatten --date 2026-11-27"),
            ("2026-11-27 12:58", "event flatten --date 2026-11-27"),
        ],
        got,
    )
    lines = _gap(cron_gap, capsys, "2026-12-24T19:00:00-05:00", "2026-12-26T11:00:00-05:00")
    got = _entries(lines)
    _in_order(
        [
            ("2026-12-24 20:00", "nightly --date 2026-12-28"),
            ("2026-12-25 08:00", "not a session (2026-12-25): nothing to run [trader premarket]"),
            ("2026-12-25 18:05", "soak-report --notify --through 2026-12-24"),
            ("2026-12-26 09:00", "weekly --date 2026-12-24"),
            ("2026-12-26 10:30", "soak-report --notify --final --through 2026-12-24"),
        ],
        got,
    )
    assert not any(t.startswith("2026-12-25 20:00") for t, _ in got)  # no Friday nightly


def test_dst_offsets_margins_and_the_market_hours_warning(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fall-back Sunday: the 02:00 token refresh fires once and the Sunday nightly (01:00Z Monday) pins
    Monday, whatever offset the window is written in. The ±60 s margin is exact. The warning covers
    09:15-16:30 ET on weekdays only, with open ends."""
    utc = _gap(cron_gap, capsys, "2026-11-01T04:30:00Z", "2026-11-02T01:30:00Z")
    tokyo = _gap(cron_gap, capsys, "2026-11-01T13:30:00+09:00", "2026-11-02T10:30:00+09:00")
    assert utc == tokyo
    assert _entries(utc) == [
        ("2026-11-01 02:00", "token-refresh"),
        ("2026-11-01 20:00", "nightly --date 2026-11-02"),
    ]
    assert "2026-11-01 02:00 EST" in utc[0]
    # The margin: the 20:00 EDT nightly is listed from 20:01:00 and from a window ending 19:59:00, not beyond.
    assert "nightly" in _gap(cron_gap, capsys, "2026-10-05T20:01:00-04:00", "2026-10-05T20:10:00-04:00")[0]
    assert _gap(cron_gap, capsys, "2026-10-05T20:01:01-04:00", "2026-10-05T20:10:00-04:00") == [
        "nothing skipped"
    ]
    assert "nightly" in _gap(cron_gap, capsys, "2026-10-05T19:50:00-04:00", "2026-10-05T19:59:00-04:00")[0]
    assert _gap(cron_gap, capsys, "2026-10-05T19:50:00-04:00", "2026-10-05T19:58:59-04:00") == [
        "nothing skipped"
    ]
    warning = "warning: the window overlaps 09:15-16:30 ET on a weekday"
    windows = {
        ("2026-10-06T16:29:59-04:00", "2026-10-06T16:40:00-04:00"): True,
        ("2026-10-06T16:30:00-04:00", "2026-10-06T16:40:00-04:00"): False,
        ("2026-10-06T09:00:00-04:00", "2026-10-06T09:15:00-04:00"): False,
        ("2026-10-06T09:00:00-04:00", "2026-10-06T09:15:01-04:00"): True,
        ("2026-10-10T10:00:00-04:00", "2026-10-10T15:00:00-04:00"): False,  # Saturday
        ("2026-10-04T23:00:00-04:00", "2026-10-05T13:20:00Z"): True,  # Sunday night into Monday 09:20 ET
    }
    for (start, end), warned in windows.items():
        assert (warning in _gap(cron_gap, capsys, start, end)) is warned, (start, end)


# --- deploy.sh prod guards against a real git repository ----------------------------------------------------

ENV_VALUES = {
    "DATABASE_URL": "postgresql+psycopg://trader_app:DeployAppPwBrk1@192.168.68.86:5432/trader",
    "MIGRATION_DATABASE_URL": "postgresql+psycopg://trader_owner:DeployOwnerPwBrk2@192.168.68.86:5432/trader",
    "APP_ENCRYPTION_KEY": "DeployEncKeyBrk3",
    "SESSION_SECRET": "DeploySessionBrk4",
    "ADMIN_USERNAME": "stephen",
    "ADMIN_PASSWORD_INITIAL": "DeployAdminPwBrk5",
}
DEPLOY_SECRETS = [v for k, v in ENV_VALUES.items() if k != "ADMIN_USERNAME"] + [
    "DeployAppPwBrk1",
    "DeployOwnerPwBrk2",
]
DOCKER_STUB = """case " $* " in
  *" ${STUB_DOCKER_FAIL:-none} "*) exit 1 ;;
esac
exit 0"""


def _git(repo: Path, *args: str, date: str = "2026-09-01T00:00:00") -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "b",
        "GIT_AUTHOR_EMAIL": "b@example.invalid",
        "GIT_COMMITTER_NAME": "b",
        "GIT_COMMITTER_EMAIL": "b@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True)


def _stub(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text(f'#!/bin/bash\necho "{name} $*" >> "$STUB_LOG"\n{body}\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    """A repository holding a copy of Trader/docker/deploy.sh (one commit, tagged later per case), stubs for
    docker, ssh, curl and sleep, and a logging wrapper around the real git."""
    repo = tmp_path / "repo"
    (repo / "Trader" / "docker").mkdir(parents=True)
    shutil.copy2(DEPLOY, repo / "Trader" / "docker" / "deploy.sh")
    (repo / ".gitignore").write_text(".env.prod\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    real_git = shutil.which("git")
    assert real_git
    _stub(stubs, "git", f'exec "{real_git}" "$@"')
    _stub(stubs, "docker", DOCKER_STUB)
    _stub(stubs, "ssh", "cat > /dev/null\nexit 0")
    _stub(stubs, "curl", 'printf "%s" "${STUB_HTTP_CODE:-200}"')
    _stub(stubs, "sleep", "exit 0")
    env_file = tmp_path / "env.prod"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in ENV_VALUES.items()))
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "TRADER_ENV_FILE": str(env_file),
        "TRADER_HEALTH_TIMEOUT": "3",
        "TRADER_HEALTH_INTERVAL": "3",
    }
    return {
        "repo": repo,
        "env": env,
        "log": tmp_path / "calls.log",
        "script": repo / "Trader/docker/deploy.sh",
    }


def _run_deploy(world: dict[str, Any], env_name: str, **extra: str) -> subprocess.CompletedProcess[str]:
    script = extra.pop("script", str(world["script"]))
    return subprocess.run(
        ["bash", script, env_name],
        env={**world["env"], **extra},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(world: dict[str, Any]) -> list[str]:
    path: Path = world["log"]
    return path.read_text().splitlines() if path.exists() else []


def _prepare(world: dict[str, Any], case: str) -> dict[str, str]:
    repo: Path = world["repo"]
    tag = {"TRADER_TAG": "v1.0.0"}
    if case == "tag on the previous commit":
        _git(repo, "tag", "-a", "v1.0.0", "-m", "rel")
        (repo / "later.txt").write_text("x")
        _git(repo, "add", "later.txt")
        _git(repo, "commit", "-q", "-m", "two")
        return tag
    _git(repo, "tag", "-a", "v1.0.0", "-m", "rel")
    if case == "untracked file outside Trader/":
        (repo / "notes.txt").write_text("x")
    elif case == "staged change":
        (repo / "staged.txt").write_text("x")
        _git(repo, "add", "staged.txt")
    elif case == "modified tracked file":
        (repo / ".gitignore").write_text(".env.prod\n*.bak\n")
    elif case == "glob tag":
        return {"TRADER_TAG": "v1.*"}
    elif case == "empty TRADER_TAG":
        return {"TRADER_TAG": ""}
    elif case == "a tag that is also a branch name only":
        _git(repo, "branch", "v2.0.0")
        return {"TRADER_TAG": "v2.0.0"}
    return tag


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("untracked file outside Trader/", "working tree is dirty"),
        ("staged change", "working tree is dirty"),
        ("modified tracked file", "working tree is dirty"),
        ("tag on the previous commit", "not the tag v1.0.0"),
        ("glob tag", "not the tag v1.*"),
        ("empty TRADER_TAG", "needs TRADER_TAG"),
        ("a tag that is also a branch name only", "not the tag v2.0.0"),
    ],
)
def test_deploy_prod_guards_refuse_against_real_git(world: dict[str, Any], case: str, reason: str) -> None:
    extra = _prepare(world, case)
    result = _run_deploy(world, "prod", **extra)
    assert result.returncode == 1, (case, result.stdout, result.stderr)
    assert reason in result.stderr, (case, result.stderr)
    assert not any(c.startswith("docker ") for c in _calls(world)), case  # nothing built or shipped
    assert "down from" not in result.stdout  # nothing went down, so no stamp or hint
    for value in DEPLOY_SECRETS:
        assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("phase_tag_is_newer", [True, False])
def test_deploy_prod_with_two_tags_on_one_commit_builds_and_labels_the_release_tag(
    world: dict[str, Any], phase_tag_is_newer: bool
) -> None:
    """A phase tag on the same commit (the builder's --match case): the guard passes, the image is
    `trader:v1.0.0`, and the version baked in (`/api/meta`, which T5 LIVE 6 and 9 expect to read `v1.0.0`)
    is the release tag, not the other tag. An ignored .env.prod doesn't make the tree dirty."""
    repo: Path = world["repo"]
    first, second = ("v1.0.0", "phase-6-complete") if phase_tag_is_newer else ("phase-6-complete", "v1.0.0")
    _git(repo, "tag", "-a", first, "-m", first, date="2026-10-01T00:00:00")
    _git(repo, "tag", "-a", second, "-m", second, date="2026-10-02T00:00:00")
    (repo / ".env.prod").write_text("ignored\n")
    result = _run_deploy(world, "prod", TRADER_TAG="v1.0.0")
    assert result.returncode == 0, result.stdout + result.stderr
    [build] = [c for c in _calls(world) if c.startswith("docker --context desktop-linux build")]
    assert "-t trader:v1.0.0" in build
    assert "--build-arg APP_VERSION=v1.0.0 " in build, build


def test_deploy_stamps_on_failure_dev_without_git_and_no_secret_on_argv(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """A failed recreate still prints the `down from` stamp and the hint; a failed build (nothing went down)
    prints neither; dev deploys from a folder that isn't a git repository (prod refuses it); no secret from
    the env file reaches any command's argv (docker, ssh, curl, git) or the output."""
    stamp = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    failed = _run_deploy(world, "dev", STUB_DOCKER_FAIL="compose")
    assert failed.returncode != 0
    assert re.search(f"deploy: down from {stamp}", failed.stdout), failed.stdout
    assert re.search(f"cron_gap.py --from {stamp} --to {stamp} --container trader-dev", failed.stdout)
    assert "deploy: up at" not in failed.stdout
    build_failed = _run_deploy(world, "dev", STUB_DOCKER_FAIL="build")
    assert build_failed.returncode != 0
    assert "down from" not in build_failed.stdout and "cron_gap.py" not in build_failed.stdout
    loose = tmp_path / "loose" / "Trader" / "docker"
    loose.mkdir(parents=True)
    shutil.copy2(DEPLOY, loose / "deploy.sh")
    dev = _run_deploy(world, "dev", script=str(loose / "deploy.sh"))
    assert dev.returncode == 0, dev.stdout + dev.stderr
    assert "deploy: up at" in dev.stdout
    prod = _run_deploy(world, "prod", script=str(loose / "deploy.sh"), TRADER_TAG="v1.0.0")
    assert prod.returncode == 1 and "git status failed" in prod.stderr
    _git(world["repo"], "tag", "-a", "v1.0.0", "-m", "rel")
    ok = _run_deploy(world, "prod", TRADER_TAG="v1.0.0")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    everything = "\n".join(_calls(world)) + "".join(
        r.stdout + r.stderr for r in (failed, build_failed, dev, prod, ok)
    )
    for value in DEPLOY_SECRETS:
        assert value not in everything


# --- env_check ----------------------------------------------------------------------------------------------

DEV = {
    "DATABASE_URL": "postgresql+psycopg://trader_dev_app:DevAppPwBrk11@192.168.68.86:5432/trader_dev",
    "MIGRATION_DATABASE_URL": "postgresql+psycopg://trader_dev_owner:DevOwnerPwBrk12@192.168.68.86:5432/trader_dev",
    "APP_ENCRYPTION_KEY": "DevEncKeyBrk13",
    "SESSION_SECRET": "DevSessionBrk14",
    "ADMIN_USERNAME": "stephen",
    "ADMIN_PASSWORD_INITIAL": "DevAdminPwBrk15",
    "TELEGRAM_BOT_TOKEN": "111:DevBotTokenBrk16",
    "TELEGRAM_CHAT_ID": "424242",
    "ANTHROPIC_API_KEY": "sk-ant-DevKeyBrk17",
    "QUESTRADE_REFRESH_TOKEN": "DevQtBrk18",
    "PUBLIC_BASE_URL": "https://trader-dev.sunspinner.ca",
    "TZ_DISPLAY": "America/Edmonton",
    "TRADER_FORWARDED_ALLOW_IPS": "172.18.0.0/16",
}
PROD = {
    **DEV,
    "DATABASE_URL": "postgresql+psycopg://trader_app:ProdAppPwBrk21@192.168.68.86:5432/trader",
    "MIGRATION_DATABASE_URL": "postgresql+psycopg://trader_owner:ProdOwnerPwBrk22@192.168.68.86:5432/trader",
    "APP_ENCRYPTION_KEY": "ProdEncKeyBrk23",
    "SESSION_SECRET": "ProdSessionBrk24",
    "ADMIN_PASSWORD_INITIAL": "ProdAdminPwBrk25",
    "TELEGRAM_BOT_TOKEN": "222:ProdBotTokenBrk26",
    "ANTHROPIC_API_KEY": "sk-ant-ProdKeyBrk27",
    "QUESTRADE_REFRESH_TOKEN": "",
    "PUBLIC_BASE_URL": "https://trader.sunspinner.ca",
}
_NOT_SECRET = {"", "stephen", "America/Edmonton", "172.18.0.0/16", "424242"}
ENV_SECRETS = sorted(
    {v for d in (DEV, PROD) for v in d.values() if v not in _NOT_SECRET and not v.startswith("https://")}
    | {"DevAppPwBrk11", "DevOwnerPwBrk12", "ProdAppPwBrk21", "ProdOwnerPwBrk22"}
)


def _dump_file(env_check: ModuleType, path: Path, values: dict[str, Any], app_env: str) -> Path:
    path.write_text(json.dumps(env_check.fingerprints(values, app_env)))
    return path


def _compare(env_check: ModuleType, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = env_check.main(["compare", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def _clean(text: str, *dumps: Path) -> None:
    for value in ENV_SECRETS:
        assert value not in text
    for dump in dumps:
        for fingerprint in json.loads(dump.read_text())["keys"].values():
            if HEX12.fullmatch(str(fingerprint)):
                assert fingerprint not in text


def test_compare_expect_names_missing_extra_and_changed_keys_without_values_or_hashes(
    env_check: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = _dump_file(env_check, tmp_path / "a.json", DEV, "dev")
    b_data = env_check.fingerprints({**DEV, "SESSION_SECRET": "OtherSessionBrk31"}, "dev")
    b_data["keys"]["EXTRA_KEY"] = hashlib.sha256(b"x").hexdigest()[:12]
    del b_data["keys"]["TELEGRAM_CHAT_ID"]
    b = tmp_path / "b.json"
    b.write_text(json.dumps(b_data))
    code, out, err = _compare(env_check, capsys, str(a), str(b), "--expect", "same")
    lines = out.splitlines()
    assert code == 1
    assert {"SESSION_SECRET different", "TELEGRAM_CHAT_ID different", "EXTRA_KEY different"} <= set(lines)
    assert "APP_ENCRYPTION_KEY same" in lines and "DATABASE_URL.password same" in lines
    _clean(out + err, a, b)
    code, out, _ = _compare(env_check, capsys, str(a), str(a), "--expect", "different")
    assert code == 1 and all(line.endswith(" same") for line in out.splitlines())
    ignore = ["--ignore", "SESSION_SECRET", "--ignore", "TELEGRAM_CHAT_ID", "--ignore", "EXTRA_KEY"]
    code, out, _ = _compare(env_check, capsys, str(a), str(b), "--expect", "same", *ignore)
    assert code == 0, out
    broken = tmp_path / "broken.json"
    broken.write_text('{"keys": {"SESSION_SECRET": "DevSessionBrk14"')  # a truncated file holding a value
    for args in (
        (str(a), str(broken), "--expect", "same"),
        (str(a), str(b)),
        (str(a), str(b), "--expect", "same", "--rules", "prod"),
    ):
        code, out, err = _compare(env_check, capsys, *args)
        assert code == 2, args
        assert "DevSessionBrk14" not in out + err


@pytest.mark.parametrize(
    ("prod_change", "dev_change", "line"),
    [
        ({}, {}, None),
        ({"PUBLIC_BASE_URL": "https://trader-dev.sunspinner.ca"}, {}, "PUBLIC_BASE_URL wrong"),
        ({"PUBLIC_BASE_URL": "https://trader.sunspinner.ca/"}, {}, "PUBLIC_BASE_URL wrong"),
        ({"PUBLIC_BASE_URL": "http://trader.sunspinner.ca"}, {}, "PUBLIC_BASE_URL wrong"),
        ({"PUBLIC_BASE_URL": ""}, {}, "PUBLIC_BASE_URL wrong: required, empty"),
        (
            {"DATABASE_URL": "postgresql+psycopg://trader_app:ProdAppPwBrk21@192.168.68.86:5433/trader"},
            {},
            "DATABASE_URL wrong: port must be 5432",
        ),
        (
            {"DATABASE_URL": "postgresql+psycopg://trader_app:ProdAppPwBrk21@192.168.68.87:5432/trader"},
            {},
            "DATABASE_URL wrong: host must be 192.168.68.86",
        ),
        (
            {
                "MIGRATION_DATABASE_URL": "postgresql+psycopg://trader_app:ProdOwnerPwBrk22@192.168.68.86:5432/trader"
            },
            {},
            "MIGRATION_DATABASE_URL wrong: role must be trader_owner",
        ),
        (
            {"DATABASE_URL": "postgresql+psycopg://trader_app:DevAppPwBrk11@192.168.68.86:5432/trader"},
            {},
            "DATABASE_URL.password SAME AS DEV",
        ),
        ({"ANTHROPIC_API_KEY": DEV["ANTHROPIC_API_KEY"]}, {}, "ANTHROPIC_API_KEY SAME AS DEV"),
        ({"APP_ENCRYPTION_KEY": DEV["APP_ENCRYPTION_KEY"]}, {}, "APP_ENCRYPTION_KEY SAME AS DEV"),
        ({"APP_ENCRYPTION_KEY": ""}, {}, "APP_ENCRYPTION_KEY wrong: required, empty"),
        ({"TELEGRAM_BOT_TOKEN": ""}, {}, "TELEGRAM_BOT_TOKEN empty (Stephen)"),
        ({"MIGRATION_DATABASE_URL": None}, {}, "MIGRATION_DATABASE_URL unavailable"),
        ({}, {"MIGRATION_DATABASE_URL": None}, "MIGRATION_DATABASE_URL.password unavailable (dev)"),
        ({"DATABASE_URL": "not a url"}, {}, "DATABASE_URL wrong: not a database URL"),
    ],
)
def test_compare_rules_prod(
    env_check: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    prod_change: dict[str, Any],
    dev_change: dict[str, Any],
    line: str | None,
) -> None:
    prod = _dump_file(env_check, tmp_path / "prod.json", {**PROD, **prod_change}, "prod")
    dev = _dump_file(env_check, tmp_path / "dev.json", {**DEV, **dev_change}, "dev")
    code, out, err = _compare(env_check, capsys, str(prod), str(dev), "--rules", "prod")
    lines = out.splitlines()
    if line is None:
        assert code == 0, out
        assert "PUBLIC_BASE_URL ok" in lines and "SESSION_SECRET different" in lines
        assert "MIGRATION_DATABASE_URL.password different" in lines
    else:
        assert code == 1, out
        assert any(entry.startswith(line) for entry in lines), (line, lines)
    _clean(out + err, prod, dev)


def test_dump_prints_no_part_of_a_value_and_reads_the_init_environment(
    env_check: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """URL-encoded passwords are hashed decoded and never printed in either form; a key the process lacks is
    taken from /proc/1/environ (never printed), and reported `unavailable` when that can't be read."""
    encoded = "Enc%2FPw%40Brk41"
    values = {**PROD, "DATABASE_URL": f"postgresql+psycopg://trader_app:{encoded}@192.168.68.86:5432/trader"}
    environ = {k: v for k, v in values.items() if k != "MIGRATION_DATABASE_URL"}
    proc = tmp_path / "environ"
    proc.write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in values.items()) + b"\0")
    for key, value in {**environ, "APP_ENV": "prod"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setattr(env_check, "PROC_ENVIRON", proc)
    assert env_check.main(["dump"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    for value in [*ENV_SECRETS, encoded, "Enc/Pw@Brk41", values["MIGRATION_DATABASE_URL"]]:
        assert value not in out
    assert data["keys"]["DATABASE_URL.password"] == hashlib.sha256(b"Enc/Pw@Brk41").hexdigest()[:12]
    assert (
        data["keys"]["MIGRATION_DATABASE_URL.password"]
        == hashlib.sha256(b"ProdOwnerPwBrk22").hexdigest()[:12]
    )
    assert data["db"]["MIGRATION_DATABASE_URL"]["role"] == "trader_owner"
    assert data["app_env"] == "prod"
    monkeypatch.setattr(env_check, "PROC_ENVIRON", tmp_path / "absent")
    assert env_check.main(["dump"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["keys"]["MIGRATION_DATABASE_URL"] == "unavailable"
    assert data["db"]["MIGRATION_DATABASE_URL"] == "unavailable"
