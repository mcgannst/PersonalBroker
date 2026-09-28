"""P6-T6 tests 2-4: `build/env_check.py` fingerprints an environment (hash prefixes and non-secret parts only)
and compares two fingerprints, plainly (`--expect same|different`) or with the prod rules (A = prod, B = dev).
Throwaway values only; each must never appear in any output."""

import ast
import hashlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.build import BUILD_DIR, load_build_script

DEV = {
    "DATABASE_URL": "postgresql+psycopg://trader_dev_app:DevAppPw111@192.168.68.86:5432/trader_dev",
    "MIGRATION_DATABASE_URL": "postgresql+psycopg://trader_dev_owner:DevOwnerPw222@192.168.68.86:5432/trader_dev",
    "APP_ENCRYPTION_KEY": "dev-fernet-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=",
    "SESSION_SECRET": "dev-session-secret-bbbbbbbbbbbbbbbb",
    "ADMIN_USERNAME": "stephen",
    "ADMIN_PASSWORD_INITIAL": "dev-admin-pw-cccccc",
    "TELEGRAM_BOT_TOKEN": "111:dev-bot-token-dddddd",
    "TELEGRAM_CHAT_ID": "424242",
    "ANTHROPIC_API_KEY": "sk-ant-dev-eeeeeeeeeeee",
    "QUESTRADE_REFRESH_TOKEN": "dev-qt-ffffffff",
    "PUBLIC_BASE_URL": "https://trader-dev.sunspinner.ca",
    "TZ_DISPLAY": "America/Edmonton",
}
PROD = {
    **DEV,
    "DATABASE_URL": "postgresql+psycopg://trader_app:ProdAppPw333@192.168.68.86:5432/trader",
    "MIGRATION_DATABASE_URL": "postgresql+psycopg://trader_owner:ProdOwnerPw444@192.168.68.86:5432/trader",
    "APP_ENCRYPTION_KEY": "prod-fernet-key-gggggggggggggggggggggggggggggg=",
    "SESSION_SECRET": "prod-session-secret-hhhhhhhhhhhhhhhh",
    "ADMIN_PASSWORD_INITIAL": "prod-admin-pw-iiiiii",
    "TELEGRAM_BOT_TOKEN": "222:prod-bot-token-jjjjjj",
    "ANTHROPIC_API_KEY": "sk-ant-prod-kkkkkkkkkkkk",
    "PUBLIC_BASE_URL": "https://trader.sunspinner.ca",
}
SECRETS = [
    v
    for env in (DEV, PROD)
    for k, v in env.items()
    if k not in ("ADMIN_USERNAME", "TZ_DISPLAY", "PUBLIC_BASE_URL", "TELEGRAM_CHAT_ID")
] + ["DevAppPw111", "DevOwnerPw222", "ProdAppPw333", "ProdOwnerPw444"]


@pytest.fixture(scope="module")
def env_check() -> ModuleType:
    return load_build_script("env_check")


def _no_value_in(text: str) -> None:
    for value in SECRETS:
        assert value not in text


def _no_hash_in(text: str, *envs: dict[str, str]) -> None:
    for env in envs:
        for value in env.values():
            assert hashlib.sha256(value.encode()).hexdigest()[:12] not in text


def _dump(
    env_check: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    tmp_path: Path,
) -> dict[str, Any]:
    for key in env_check.KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(env_check, "PROC_ENVIRON", tmp_path / "no-proc-environ")
    assert env_check.main(["dump"]) == 0
    out = capsys.readouterr().out
    _no_value_in(out)
    result: dict[str, Any] = json.loads(out)
    return result


def _write(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data))
    return path


def _compare(
    env_check: ModuleType, capsys: pytest.CaptureFixture[str], a: Path, b: Path, *flags: str
) -> tuple[int, list[str]]:
    code = env_check.main(["compare", str(a), str(b), *flags])
    out = capsys.readouterr()
    _no_value_in(out.out + out.err)
    return code, out.out.splitlines()


# --- 2. dump ------------------------------------------------------------------------------------------------


def test_env_check_runs_on_python_3_10(env_check: ModuleType) -> None:
    ast.parse((BUILD_DIR / "env_check.py").read_text(), feature_version=(3, 10))


def test_dump_prints_hash_prefixes_empties_and_db_parts(
    env_check: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environ = {**DEV, "ANTHROPIC_API_KEY": "", "APP_ENV": "dev"}
    del environ["QUESTRADE_REFRESH_TOKEN"]
    data = _dump(env_check, capsys, monkeypatch, environ, tmp_path)
    assert data["app_env"] == "dev"
    assert data["public_base_url"] == "https://trader-dev.sunspinner.ca"
    assert data["db"]["DATABASE_URL"] == {
        "role": "trader_dev_app",
        "host": "192.168.68.86",
        "port": 5432,
        "database": "trader_dev",
    }
    assert data["db"]["MIGRATION_DATABASE_URL"]["role"] == "trader_dev_owner"
    keys = data["keys"]
    assert keys["SESSION_SECRET"] == hashlib.sha256(DEV["SESSION_SECRET"].encode()).hexdigest()[:12]
    assert keys["DATABASE_URL.password"] == hashlib.sha256(b"DevAppPw111").hexdigest()[:12]
    assert keys["MIGRATION_DATABASE_URL.password"] == hashlib.sha256(b"DevOwnerPw222").hexdigest()[:12]
    assert keys["ANTHROPIC_API_KEY"] == "empty"
    assert keys["QUESTRADE_REFRESH_TOKEN"] == "empty"
    assert keys["TRADER_FORWARDED_ALLOW_IPS"] == "empty"
    assert set(keys) == set(env_check.KEYS) | {"DATABASE_URL.password", "MIGRATION_DATABASE_URL.password"}
    assert all(v == "empty" or (len(v) == 12 and int(v, 16) >= 0) for v in keys.values())


def test_dump_env_file_matches_the_environment_and_reads_quotes_like_compose(
    env_check: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from_environ = _dump(env_check, capsys, monkeypatch, DEV, tmp_path)
    lines = ["# dev secrets", ""]
    for i, (key, value) in enumerate(DEV.items()):
        quoted = [value, f'"{value}"', f"'{value}'", value][i % 4]
        prefix = "export " if i % 4 == 3 else ""
        lines.append(f"{prefix}{key}={quoted}")
    env_file = tmp_path / ".env.dev"
    env_file.write_text("\n".join(lines) + "\n")
    assert env_check.main(["dump", "--env-file", str(env_file)]) == 0
    out = capsys.readouterr().out
    _no_value_in(out)
    from_file = json.loads(out)
    assert from_file["keys"] == from_environ["keys"]
    assert from_file["db"] == from_environ["db"]
    assert from_file["public_base_url"] == from_environ["public_base_url"]


def test_dump_reads_the_migration_url_from_the_init_process_when_unset(
    env_check: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environ = {k: v for k, v in DEV.items() if k != "MIGRATION_DATABASE_URL"}
    data = _dump(env_check, capsys, monkeypatch, environ, tmp_path)
    assert data["keys"]["MIGRATION_DATABASE_URL"] == "unavailable"
    assert data["keys"]["MIGRATION_DATABASE_URL.password"] == "unavailable"
    assert data["db"]["MIGRATION_DATABASE_URL"] == "unavailable"

    proc = tmp_path / "environ"
    proc.write_bytes(
        b"PATH=/usr/bin\0MIGRATION_DATABASE_URL=" + DEV["MIGRATION_DATABASE_URL"].encode() + b"\0"
    )
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(env_check, "PROC_ENVIRON", proc)
    assert env_check.main(["dump"]) == 0
    out = capsys.readouterr().out
    _no_value_in(out)
    data = json.loads(out)
    assert (
        data["keys"]["MIGRATION_DATABASE_URL.password"] == hashlib.sha256(b"DevOwnerPw222").hexdigest()[:12]
    )
    assert data["db"]["MIGRATION_DATABASE_URL"]["role"] == "trader_dev_owner"


# --- 3. compare --rules prod --------------------------------------------------------------------------------


@pytest.fixture
def dumps(
    env_check: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, dict[str, Any]]:
    return {
        "dev": _dump(env_check, capsys, monkeypatch, DEV, tmp_path),
        "prod": _dump(env_check, capsys, monkeypatch, PROD, tmp_path),
        "prod_dev_url": _dump(
            env_check, capsys, monkeypatch, {**PROD, "PUBLIC_BASE_URL": DEV["PUBLIC_BASE_URL"]}, tmp_path
        ),
        "prod_copied_secret": _dump(
            env_check, capsys, monkeypatch, {**PROD, "SESSION_SECRET": DEV["SESSION_SECRET"]}, tmp_path
        ),
        "prod_no_bot": _dump(env_check, capsys, monkeypatch, {**PROD, "TELEGRAM_BOT_TOKEN": ""}, tmp_path),
        "prod_dev_db": _dump(
            env_check,
            capsys,
            monkeypatch,
            {
                **PROD,
                "DATABASE_URL": "postgresql+psycopg://trader_app:ProdAppPw333@192.168.68.86:5432/trader_dev",
            },
            tmp_path,
        ),
    }


def test_rules_prod_pass_on_a_complete_prod_dump(
    env_check: ModuleType,
    capsys: pytest.CaptureFixture[str],
    dumps: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    prod = _write(tmp_path / "prod.json", dumps["prod"])
    dev = _write(tmp_path / "dev.json", dumps["dev"])
    code, lines = _compare(
        env_check, capsys, prod, dev, "--rules", "prod", "--ignore", "QUESTRADE_REFRESH_TOKEN"
    )
    assert code == 0, lines
    assert "SESSION_SECRET different" in lines
    assert "DATABASE_URL.password different" in lines
    assert "PUBLIC_BASE_URL ok" in lines
    assert "DATABASE_URL ok" in lines
    assert all(line.endswith((" ok", " different")) for line in lines), lines
    _no_hash_in("\n".join(lines), DEV, PROD)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("prod_dev_url", "PUBLIC_BASE_URL wrong"),
        ("prod_copied_secret", "SESSION_SECRET SAME AS DEV"),
        ("prod_no_bot", "TELEGRAM_BOT_TOKEN empty (Stephen)"),
        ("prod_dev_db", "DATABASE_URL wrong"),
    ],
)
def test_rules_prod_name_each_problem(
    env_check: ModuleType,
    capsys: pytest.CaptureFixture[str],
    dumps: dict[str, dict[str, Any]],
    tmp_path: Path,
    case: str,
    expected: str,
) -> None:
    prod = _write(tmp_path / "prod.json", dumps[case])
    dev = _write(tmp_path / "dev.json", dumps["dev"])
    code, lines = _compare(env_check, capsys, prod, dev, "--rules", "prod")
    assert code == 1
    assert any(line.startswith(expected) for line in lines), lines
    _no_hash_in("\n".join(lines), DEV, PROD)


def test_rules_prod_refuse_a_missing_admin_password(
    env_check: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    dumps: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    prod = _write(
        tmp_path / "prod.json",
        _dump(env_check, capsys, monkeypatch, {**PROD, "ADMIN_PASSWORD_INITIAL": ""}, tmp_path),
    )
    dev = _write(tmp_path / "dev.json", dumps["dev"])
    code, lines = _compare(env_check, capsys, prod, dev, "--rules", "prod")
    assert code == 1
    assert any(line.startswith("ADMIN_PASSWORD_INITIAL wrong") for line in lines), lines


# --- 4. compare --expect ------------------------------------------------------------------------------------


def test_expect_same_passes_on_identical_dumps_and_names_one_difference(
    env_check: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    dumps: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    a = _write(tmp_path / "a.json", dumps["dev"])
    b = _write(tmp_path / "b.json", {**dumps["dev"], "app_env": None})  # a file dump has no APP_ENV
    code, lines = _compare(env_check, capsys, a, b, "--expect", "same", "--ignore", "QUESTRADE_REFRESH_TOKEN")
    assert code == 0, lines
    assert "SESSION_SECRET same" in lines
    assert not any(line.startswith("QUESTRADE_REFRESH_TOKEN") for line in lines)

    changed = _write(
        tmp_path / "c.json",
        _dump(env_check, capsys, monkeypatch, {**DEV, "TELEGRAM_BOT_TOKEN": "333:other-token"}, tmp_path),
    )
    code, lines = _compare(
        env_check, capsys, a, changed, "--expect", "same", "--ignore", "QUESTRADE_REFRESH_TOKEN"
    )
    assert code == 1
    assert [line for line in lines if not line.endswith(" same")] == ["TELEGRAM_BOT_TOKEN different"]
    _no_hash_in("\n".join(lines), DEV)


def test_expect_same_fails_on_an_unavailable_migration_url_unless_ignored(
    env_check: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    dumps: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    no_migration = {k: v for k, v in DEV.items() if k != "MIGRATION_DATABASE_URL"}
    container = _write(
        tmp_path / "container.json", _dump(env_check, capsys, monkeypatch, no_migration, tmp_path)
    )
    local = _write(tmp_path / "file.json", dumps["dev"])
    code, lines = _compare(env_check, capsys, container, local, "--expect", "same")
    assert code == 1
    assert "MIGRATION_DATABASE_URL unavailable" in lines
    code, lines = _compare(
        env_check,
        capsys,
        container,
        local,
        "--expect",
        "same",
        "--ignore",
        "MIGRATION_DATABASE_URL",
        "--ignore",
        "MIGRATION_DATABASE_URL.password",
    )
    assert code == 0, lines


def test_expect_different(
    env_check: ModuleType,
    capsys: pytest.CaptureFixture[str],
    dumps: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    prod = _write(tmp_path / "prod.json", dumps["prod"])
    dev = _write(tmp_path / "dev.json", dumps["dev"])
    code, lines = _compare(env_check, capsys, prod, dev, "--expect", "different")
    assert code == 1  # ADMIN_USERNAME, TELEGRAM_CHAT_ID ... are the same in both
    assert "SESSION_SECRET different" in lines
    assert "ADMIN_USERNAME same" in lines


def test_compare_needs_exactly_one_of_expect_and_rules(
    env_check: ModuleType,
    capsys: pytest.CaptureFixture[str],
    dumps: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> None:
    a = _write(tmp_path / "a.json", dumps["dev"])
    assert env_check.main(["compare", str(a), str(a)]) == 2
    assert env_check.main(["compare", str(a), str(a), "--expect", "same", "--rules", "prod"]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert env_check.main(["compare", str(bad), str(a), "--expect", "same"]) == 2
    capsys.readouterr()


# --- fix round 1 (gauntlet nits) ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "password"),
    [
        ("postgresql+psycopg://trader_app:1234/5678@192.168.68.86:5432/trader", "1234/5678"),
        ("postgresql+psycopg://trader_app:ab?cd#ef@192.168.68.86:5432/trader", "ab?cd#ef"),
        ("postgresql+psycopg://trader_app:p%40ss%2Fw@192.168.68.86:5432/trader", "p@ss/w"),
    ],
)
def test_a_password_with_url_characters_stays_in_the_password(
    env_check: ModuleType, url: str, password: str
) -> None:
    """Split like SQLAlchemy: the password runs to the `@`, so `/`, `?` and `#` never spill into the database
    or port fields."""
    data = env_check.fingerprints({"DATABASE_URL": url}, "prod")
    assert data["db"]["DATABASE_URL"] == {
        "role": "trader_app",
        "host": "192.168.68.86",
        "port": 5432,
        "database": "trader",
    }
    assert data["keys"]["DATABASE_URL.password"] == hashlib.sha256(password.encode()).hexdigest()[:12]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://trader_app:Leak1@Leak2@192.168.68.86:5432/trader",  # a second, raw @
        "postgresql+psycopg://trader_app:Leak1Leak2@192.168.68.86:notaport/trader",
        "postgresql+psycopg://trader_app:Leak1Leak2@/trader",  # no host
        "not a url Leak1Leak2",
    ],
)
def test_a_malformed_url_prints_no_part_of_its_password(env_check: ModuleType, url: str) -> None:
    data = env_check.fingerprints({"DATABASE_URL": url}, "prod")
    assert data["db"]["DATABASE_URL"] is None
    text = json.dumps(data)
    for fragment in ("Leak1", "Leak2", "notaport"):
        assert fragment not in text


def test_a_quoted_value_with_an_inline_comment_reads_like_compose(env_check: ModuleType) -> None:
    parsed = env_check.parse_env_file(
        'A="quoted value" # a note\nB=\'single # kept\' # note\nC=plain # note\nD="unclosed\nF=""\n'
    )
    assert parsed["A"] == "quoted value"
    assert parsed["B"] == "single # kept"
    assert parsed["C"] == "plain"
    assert parsed["D"] == '"unclosed'
    assert parsed["F"] == ""
