"""P6-T6 tests 7 and 9 (integration): `build/prod_env.py create_db` and `verify_roles` against a throwaway
PostgreSQL 14 superuser (its own container, with every statement logged so the test can prove no plaintext
password reached the server). Never trader_dev, never the real server."""

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from testcontainers.community.postgres import PostgresContainer

from tests.build import load_build_script

pytestmark = pytest.mark.db

OWNER_PW = "OwnerPwFirstRun0123456789abcdefg"
APP_PW = "AppPwFirstRun0123456789abcdefghi"
OWNER_PW_2 = "OwnerPwSecondRun0123456789abcdef"
APP_PW_2 = "AppPwSecondRun0123456789abcdefgh"
STRANGER_PW = "StrangerPw0123"


@pytest.fixture(scope="module")
def pg() -> Iterator[PostgresContainer]:
    container = PostgresContainer("postgres:14-alpine", driver="psycopg").with_command(
        "postgres -c log_statement=all"
    )
    with container as started:
        yield started


@pytest.fixture(scope="module")
def admin(pg: PostgresContainer) -> dict[str, Any]:
    return {
        "host": pg.get_container_host_ip(),
        "port": int(pg.get_exposed_port(5432)),
        "user": pg.username,
        "password": pg.password,
        "dbname": "postgres",
    }


@pytest.fixture(scope="module")
def prod_env() -> ModuleType:
    return load_build_script("prod_env")


def _url(admin: dict[str, Any], role: str, password: str) -> str:
    return f"postgresql+psycopg://{role}:{password}@{admin['host']}:{admin['port']}/trader"


def _connect(admin: dict[str, Any], **overrides: Any) -> psycopg.Connection[Any]:
    return psycopg.connect(make_conninfo(**{**admin, **overrides}), autocommit=True)


def _server_log(pg: PostgresContainer) -> str:
    stdout, stderr = pg.get_logs()
    return (stdout + stderr).decode(errors="replace")


def test_create_db_twice_verify_roles_and_no_plaintext_in_the_statement_log(
    pg: PostgresContainer, admin: dict[str, Any], prod_env: ModuleType
) -> None:
    # A stand-in for dev's setup, which --dev-compare copies (owner, encoding, role settings).
    with _connect(admin) as conn:
        conn.execute("CREATE ROLE trader_dev_owner LOGIN")
        conn.execute("ALTER ROLE trader_dev_owner SET search_path = trader, public")
        conn.execute("CREATE ROLE trader_dev_app LOGIN")
        conn.execute("ALTER ROLE trader_dev_app SET statement_timeout = '30s'")
        conn.execute("CREATE DATABASE trader_dev OWNER trader_dev_owner")
        conn.execute(f"CREATE ROLE stranger LOGIN PASSWORD '{STRANGER_PW}'")
    admin_dsn = make_conninfo(**admin)

    first: list[str] = []
    prod_env.create_db(
        admin_dsn, owner_password=OWNER_PW, app_password=APP_PW, dev_compare=True, out=first.append
    )
    assert "role trader_owner: created" in first
    assert "role trader_app: created" in first
    assert "database trader: created" in first

    second: list[str] = []
    prod_env.create_db(
        admin_dsn, owner_password=OWNER_PW_2, app_password=APP_PW_2, dev_compare=True, out=second.append
    )
    assert "role trader_owner: updated" in second
    assert "role trader_app: updated" in second
    assert "database trader: exists" in second
    assert any(line.startswith("schema trader: exists") for line in second)

    with _connect(admin) as conn:
        roles = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolconfig FROM pg_roles "
                "WHERE rolname IN ('trader_owner', 'trader_app')"
            ).fetchall()
        }
        db = conn.execute(
            "SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding) FROM pg_database "
            "WHERE datname = 'trader'"
        ).fetchone()
    assert roles["trader_owner"][:4] == (True, False, False, False)
    assert roles["trader_app"][:4] == (True, False, False, False)
    assert roles["trader_owner"][4] == ["search_path=trader, public"]
    assert roles["trader_app"][4] == ["statement_timeout=30s"]
    assert db == ("trader_owner", "UTF8")

    # The second run's passwords are the live ones: roles log in with them although only verifiers were sent.
    checks: list[str] = []
    prod_env.verify_roles(
        _url(admin, "trader_owner", OWNER_PW_2), _url(admin, "trader_app", APP_PW_2), out=checks.append
    )
    assert checks == ["trader_app: connect ok, create refused", "trader_owner: create ok"]
    with pytest.raises(psycopg.OperationalError):
        _connect(admin, user="trader_app", password=APP_PW, dbname="trader")

    # As trader_app, rows can be written and read in a table the owner creates (default privileges).
    with _connect(admin, user="trader_owner", password=OWNER_PW_2, dbname="trader") as conn:
        conn.execute("CREATE TABLE trader.p6_rows (id serial PRIMARY KEY, v text)")
    with _connect(admin, user="trader_app", password=APP_PW_2, dbname="trader") as conn:
        conn.execute("INSERT INTO trader.p6_rows (v) VALUES ('x')")
        assert conn.execute("SELECT v FROM trader.p6_rows").fetchall() == [("x",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DROP TABLE trader.p6_rows")

    # PUBLIC can't connect to trader: a role without a grant is refused.
    with _connect(admin, user="stranger", password=STRANGER_PW, dbname="postgres"):
        pass
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        _connect(admin, user="stranger", password=STRANGER_PW, dbname="trader")

    log = _server_log(pg)
    assert "ALTER ROLE" in log  # statements really are logged
    assert "SCRAM-SHA-256$" in log
    for password in (OWNER_PW, APP_PW, OWNER_PW_2, APP_PW_2):
        assert password not in log


def test_the_cli_runs_create_db_from_600_files(
    pg: PostgresContainer,
    admin: dict[str, Any],
    prod_env: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = tmp_path / "dsn"
    dsn.write_text(make_conninfo(**admin))
    dsn.chmod(0o600)
    pws = tmp_path / "pws"
    pws.write_text(f"owner={OWNER_PW}\napp={APP_PW}\n")
    pws.chmod(0o600)
    assert prod_env.main(["create-db", "--admin-dsn-file", str(dsn), "--passwords-file", str(pws)]) == 0
    out = capsys.readouterr()
    assert "role trader_owner:" in out.out and "database trader:" in out.out
    for value in (OWNER_PW, APP_PW):
        assert value not in out.out + out.err


def test_a_failed_login_error_names_no_secret(admin: dict[str, Any], prod_env: ModuleType) -> None:
    wrong = "WrongPw0123456789"
    with pytest.raises(prod_env.ProdEnvError) as err:
        prod_env.verify_roles(_url(admin, "trader_owner", wrong), _url(admin, "trader_app", wrong + "x"))
    assert wrong not in str(err.value)


def test_public_schema_is_closed_and_a_failed_probe_leaves_nothing(
    admin: dict[str, Any], prod_env: ModuleType
) -> None:
    """Fix round 1: trader_app can't create in schema public either (PostgreSQL 14 grants PUBLIC CREATE there
    by default); when trader_app wrongly can create in schema trader, verify_roles fails and drops its
    probe."""
    owner_pw, app_pw = "OwnerPwThirdRun0123456789abcdefg", "AppPwThirdRun0123456789abcdefghi"
    lines: list[str] = []
    prod_env.create_db(make_conninfo(**admin), owner_password=owner_pw, app_password=app_pw, out=lines.append)
    assert "schema public: CREATE revoked from PUBLIC" in lines
    with _connect(admin, user="trader_app", password=app_pw, dbname="trader") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE public.p6_public_probe (id integer)")

    with _connect(admin, dbname="trader") as conn:
        conn.execute("GRANT CREATE ON SCHEMA trader TO trader_app")
    try:
        with pytest.raises(prod_env.ProdEnvError, match="could create a table"):
            prod_env.verify_roles(
                _url(admin, "trader_owner", owner_pw), _url(admin, "trader_app", app_pw), out=lambda _: None
            )
        with _connect(admin, dbname="trader") as conn:
            assert conn.execute("SELECT to_regclass('trader.p6_probe')").fetchone() == (None,)
    finally:
        with _connect(admin, dbname="trader") as conn:
            conn.execute("REVOKE CREATE ON SCHEMA trader FROM trader_app")
