"""P6-T6 tests 1, 8 and 9 (unit half): `build/prod_env.py` generates prod's values in memory, refuses to
touch anything but database `trader` and roles `trader_owner`/`trader_app`, and reads its DSN and passwords
only from mode-600 files outside the repository and ~/Documents. No database here (test 7 is
tests/integration/test_prod_db_setup.py): `psycopg.connect` is replaced by a tripwire."""

import base64
import hashlib
import hmac
import os
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest
from cryptography.fernet import Fernet

from tests.build import BUILD_DIR, load_build_script

REPO_ROOT = BUILD_DIR.parents[1]
ADMIN_DSN = "postgresql://stephen:Admin-Dsn-Pw-5150@192.168.68.86:5432/postgres"
OWNER_PW = "OwnerPasswordAbc123OwnerPassword"
APP_PW = "AppPasswordXyz789AppPasswordXyz7"


@pytest.fixture
def prod_env(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load_build_script("prod_env")

    def tripwire(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("prod_env connected to a database")

    monkeypatch.setattr(module.psycopg, "connect", tripwire)
    return module


def _secret_file(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


# --- 1. generated values ------------------------------------------------------------------------------------


def test_generated_values_have_the_prod_shape(prod_env: ModuleType) -> None:
    values = prod_env.generate_prod_values()
    assert len(values.owner_password) == 32 and values.owner_password.isalnum()
    assert len(values.app_password) == 32 and values.app_password.isalnum()
    assert values.owner_password != values.app_password
    app, owner = urlsplit(values.database_url), urlsplit(values.migration_database_url)
    assert values.database_url.startswith("postgresql+psycopg://")
    assert (app.username, app.password, app.hostname, app.port, app.path) == (
        "trader_app",
        values.app_password,
        "192.168.68.86",
        5432,
        "/trader",
    )
    assert (owner.username, owner.password, owner.hostname, owner.port, owner.path) == (
        "trader_owner",
        values.owner_password,
        "192.168.68.86",
        5432,
        "/trader",
    )
    Fernet(values.app_encryption_key.encode())  # accepted as a key
    assert len(base64.urlsafe_b64decode(values.app_encryption_key)) == 32
    assert len(values.session_secret) >= 64
    assert values.admin_username == "stephen"
    assert len(values.admin_password_initial) == 24 and values.admin_password_initial.isalnum()


def test_two_calls_differ_and_repr_shows_no_value(prod_env: ModuleType) -> None:
    first, second = prod_env.generate_prod_values(), prod_env.generate_prod_values()
    for field in (
        "owner_password",
        "app_password",
        "database_url",
        "migration_database_url",
        "app_encryption_key",
        "session_secret",
        "admin_password_initial",
    ):
        assert getattr(first, field) != getattr(second, field)
    for text in (repr(first), str(first), f"{first}", f"{first!r}"):
        assert "owner_password" in text  # key names only
        for field in ("owner_password", "app_password", "app_encryption_key", "session_secret"):
            assert getattr(first, field) not in text
        assert "stephen" not in text and "192.168.68.86" not in text


def test_scram_verifier_matches_rfc_5802(prod_env: ModuleType) -> None:
    salt = b"0123456789abcdef"
    verifier = prod_env.scram_sha256_verifier("pencil", salt=salt)
    method, rest = verifier.split("$", 1)
    params, keys = rest.split("$")
    iterations, salt_b64 = params.split(":")
    stored_b64, server_b64 = keys.split(":")
    assert method == "SCRAM-SHA-256" and iterations == "4096"
    assert base64.b64decode(salt_b64) == salt
    salted = hashlib.pbkdf2_hmac("sha256", b"pencil", salt, 4096)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    assert base64.b64decode(stored_b64) == hashlib.sha256(client_key).digest()
    assert base64.b64decode(server_b64) == hmac.new(salted, b"Server Key", "sha256").digest()
    assert "pencil" not in verifier
    assert prod_env.scram_sha256_verifier("pencil") != prod_env.scram_sha256_verifier("pencil")  # random salt


# --- 8. only trader / trader_owner / trader_app -------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"database": "trader_dev"},
        {"owner_role": "trader_dev_owner"},
        {"app_role": "trader_dev_app"},
        {"database": "financetracker"},
    ],
)
def test_create_db_refuses_other_databases_and_roles(prod_env: ModuleType, overrides: dict[str, str]) -> None:
    with pytest.raises(prod_env.ProdEnvError) as err:
        prod_env.create_db(ADMIN_DSN, owner_password=OWNER_PW, app_password=APP_PW, out=print, **overrides)
    assert list(overrides.values())[0] in str(err.value)
    assert OWNER_PW not in str(err.value) and "Admin-Dsn-Pw-5150" not in str(err.value)


def test_create_db_refuses_an_admin_dsn_naming_another_database(prod_env: ModuleType) -> None:
    with pytest.raises(prod_env.ProdEnvError) as err:
        prod_env.create_db(
            ADMIN_DSN.replace("/postgres", "/trader_dev"), owner_password=OWNER_PW, app_password=APP_PW
        )
    assert "trader_dev" in str(err.value) and "Admin-Dsn-Pw-5150" not in str(err.value)


def test_create_db_refuses_empty_or_equal_passwords(prod_env: ModuleType) -> None:
    with pytest.raises(prod_env.ProdEnvError):
        prod_env.create_db(ADMIN_DSN, owner_password="", app_password=APP_PW)
    with pytest.raises(prod_env.ProdEnvError):
        prod_env.create_db(ADMIN_DSN, owner_password=APP_PW, app_password=APP_PW)


@pytest.mark.parametrize(
    ("owner_url", "app_url", "named"),
    [
        (
            f"postgresql+psycopg://trader_owner:{OWNER_PW}@192.168.68.86:5432/trader_dev",
            f"postgresql+psycopg://trader_app:{APP_PW}@192.168.68.86:5432/trader",
            "trader_dev",
        ),
        (
            f"postgresql+psycopg://trader_owner:{OWNER_PW}@192.168.68.86:5432/trader",
            f"postgresql+psycopg://trader_dev_app:{APP_PW}@192.168.68.86:5432/trader",
            "trader_dev_app",
        ),
        (
            f"postgresql+psycopg://trader_app:{OWNER_PW}@192.168.68.86:5432/trader",
            f"postgresql+psycopg://trader_owner:{APP_PW}@192.168.68.86:5432/trader",
            "trader_app",
        ),
    ],
)
def test_verify_roles_refuses_other_urls(
    prod_env: ModuleType, owner_url: str, app_url: str, named: str
) -> None:
    with pytest.raises(prod_env.ProdEnvError) as err:
        prod_env.verify_roles(owner_url, app_url, out=print)
    assert named in str(err.value)
    assert OWNER_PW not in str(err.value) and APP_PW not in str(err.value)


# --- 9. secret files ----------------------------------------------------------------------------------------


def test_the_cli_refuses_a_644_dsn_file_before_connecting(
    prod_env: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn = _secret_file(tmp_path / "dsn", ADMIN_DSN, mode=0o644)
    pws = _secret_file(tmp_path / "pws", f"owner={OWNER_PW}\napp={APP_PW}\n")
    code = prod_env.main(["create-db", "--admin-dsn-file", str(dsn), "--passwords-file", str(pws)])
    out = capsys.readouterr()
    assert code == 1
    assert "mode 600" in out.err
    for value in (ADMIN_DSN, "Admin-Dsn-Pw-5150", OWNER_PW, APP_PW):
        assert value not in out.out + out.err


def test_the_cli_refuses_a_644_passwords_file(
    prod_env: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn = _secret_file(tmp_path / "dsn", ADMIN_DSN)
    pws = _secret_file(tmp_path / "pws", f"owner={OWNER_PW}\napp={APP_PW}\n", mode=0o644)
    code = prod_env.main(["create-db", "--admin-dsn-file", str(dsn), "--passwords-file", str(pws)])
    out = capsys.readouterr()
    assert code == 1 and "mode 600" in out.err
    assert OWNER_PW not in out.out + out.err


def test_a_600_file_inside_the_repository_is_refused(prod_env: ModuleType, tmp_path: Path) -> None:
    inside = REPO_ROOT / "Trader" / "app" / f".p6t6-probe-{os.getpid()}"
    try:
        _secret_file(inside, ADMIN_DSN)
        with pytest.raises(prod_env.ProdEnvError) as err:
            prod_env.read_admin_dsn(inside)
        assert "repository" in str(err.value) or "Documents" in str(err.value)
        assert "Admin-Dsn-Pw-5150" not in str(err.value)
    finally:
        inside.unlink(missing_ok=True)


def test_a_600_file_under_documents_is_refused(
    prod_env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = tmp_path / "Documents"
    documents.mkdir()
    monkeypatch.setattr(prod_env, "DOCUMENTS", documents)
    dsn = _secret_file(documents / "dsn", ADMIN_DSN)
    with pytest.raises(prod_env.ProdEnvError) as err:
        prod_env.read_admin_dsn(dsn)
    assert "Documents" in str(err.value)


def test_secret_files_are_read_when_600_and_outside(prod_env: ModuleType, tmp_path: Path) -> None:
    dsn = _secret_file(tmp_path / "dsn", ADMIN_DSN + "\n")
    pws = _secret_file(tmp_path / "pws", f"owner={OWNER_PW}\napp={APP_PW}\n")
    assert prod_env.read_admin_dsn(dsn) == ADMIN_DSN
    assert prod_env.read_passwords(pws) == (OWNER_PW, APP_PW)


def test_a_malformed_passwords_file_names_keys_never_content(prod_env: ModuleType, tmp_path: Path) -> None:
    pws = _secret_file(tmp_path / "pws", f"owner={OWNER_PW}\nnot-a-line {APP_PW}\n")
    with pytest.raises(prod_env.ProdEnvError) as err:
        prod_env.read_passwords(pws)
    assert "app" in str(err.value)
    assert OWNER_PW not in str(err.value) and APP_PW not in str(err.value)


def test_a_missing_file_is_refused(prod_env: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(prod_env.ProdEnvError):
        prod_env.read_admin_dsn(tmp_path / "absent")


# --- fix round 1 (gauntlet nits) ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "rendered"),
    [
        ("search_path=trader, public", "ALTER ROLE \"r\" SET \"search_path\" = 'trader', 'public'"),
        ('search_path="$user", public', "ALTER ROLE \"r\" SET \"search_path\" = '$user', 'public'"),
        ("DateStyle=ISO, MDY", "ALTER ROLE \"r\" SET \"DateStyle\" = 'ISO', 'MDY'"),
        ("statement_timeout=30s", 'ALTER ROLE "r" SET "statement_timeout" = \'30s\''),
        ("application_name=trader, dev", 'ALTER ROLE "r" SET "application_name" = \'trader, dev\''),
    ],
)
def test_only_list_settings_are_split(prod_env: ModuleType, entry: str, rendered: str) -> None:
    assert prod_env._role_setting("r", entry).as_string(None) == rendered


def test_an_admin_dsn_without_a_database_is_refused(prod_env: ModuleType) -> None:
    """Without a dbname libpq would connect to the database named like the user: refused before connecting."""
    with pytest.raises(prod_env.ProdEnvError) as err:
        prod_env.create_db(
            "host=192.168.68.86 user=stephen password=Admin-Dsn-Pw-5150",
            owner_password=OWNER_PW,
            app_password=APP_PW,
        )
    assert "must name database postgres" in str(err.value)
    assert "Admin-Dsn-Pw-5150" not in str(err.value)
