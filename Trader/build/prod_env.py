"""Prod database and generated values (P6-T6, amended: no .env.prod file exists anywhere).

`generate_prod_values()`, `create_db(...)` and `verify_roles(...)` are called in memory by
`gh_secrets.py init-prod` (P6-T13), which pushes the values straight into the GitHub environment `prod`.
The CLI exists for tests and break-glass only:

    uv --directory Trader/app run python ../build/prod_env.py create-db \
        --admin-dsn-file PATH --passwords-file PATH [--dev-compare]

Both files must be mode 600 and outside the repository and ~/Documents. The DSN file holds a PostgreSQL
superuser DSN (`postgresql://user:pw@host:port/postgres`); the passwords file two lines `owner=...` and
`app=...`. Passwords reach the server only as SCRAM-SHA-256 verifiers computed here, so no plaintext password
is in any statement (or the server's statement log). Nothing prints, logs or raises a secret: output is step
names, role names and statuses.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import secrets
import stat
import string
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = Path.home() / "Documents"

DB_HOST = "192.168.68.86"
DB_PORT = 5432
DATABASE = "trader"
OWNER_ROLE = "trader_owner"
APP_ROLE = "trader_app"
ADMIN_USERNAME = "stephen"
DEV_DATABASE = "trader_dev"
DEV_ROLES = {OWNER_ROLE: "trader_dev_owner", APP_ROLE: "trader_dev_app"}
# The admin DSN must name its database: without one libpq connects to the database named like the user.
ADMIN_DATABASES = ("postgres", DATABASE)
SCRAM_ITERATIONS = 4096
PROBE_TABLE = sql.SQL("trader.p6_probe")
# PostgreSQL's list-valued settings (GUC_LIST_INPUT) a role may carry: their rolconfig value
# (`search_path=trader, public`) is re-set element by element. Any other value is one literal, commas
# included.
LIST_SETTINGS = frozenset(
    {"search_path", "temp_tablespaces", "datestyle", "local_preload_libraries", "session_preload_libraries"}
)
_ALNUM = string.ascii_letters + string.digits

Out = Callable[[str], None]


class ProdEnvError(Exception):
    """A refusal or a failed step; the message names keys, roles and steps only, never a value."""


# --- values -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class ProdValues:
    owner_password: str
    app_password: str
    database_url: str
    migration_database_url: str
    app_encryption_key: str
    session_secret: str
    admin_username: str
    admin_password_initial: str

    def __repr__(self) -> str:
        return "ProdValues(" + ", ".join(f"{f.name}=<hidden>" for f in fields(self)) + ")"

    __str__ = __repr__


def _random_alnum(length: int) -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(length))


def database_url(role: str, password: str) -> str:
    return f"postgresql+psycopg://{role}:{password}@{DB_HOST}:{DB_PORT}/{DATABASE}"


def generate_prod_values() -> ProdValues:
    owner_password = _random_alnum(32)
    app_password = _random_alnum(32)
    while app_password == owner_password:  # pragma: no cover - 62**32 values
        app_password = _random_alnum(32)
    return ProdValues(
        owner_password=owner_password,
        app_password=app_password,
        database_url=database_url(APP_ROLE, app_password),
        migration_database_url=database_url(OWNER_ROLE, owner_password),
        app_encryption_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        session_secret=secrets.token_urlsafe(48),
        admin_username=ADMIN_USERNAME,
        admin_password_initial=_random_alnum(24),
    )


def scram_sha256_verifier(
    password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS
) -> str:
    """The `SCRAM-SHA-256$<iter>:<salt>$<StoredKey>:<ServerKey>` verifier PostgreSQL stores (RFC 5802/7677).
    The generated passwords are ASCII letters and digits, which SASLprep leaves unchanged."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = base64.b64encode
    return (
        f"SCRAM-SHA-256${iterations}:{b64(salt).decode()}"
        f"${b64(stored_key).decode()}:{b64(server_key).decode()}"
    )


# --- secret files -------------------------------------------------------------------------------------------


def _is_under(path: Path, folder: Path) -> bool:
    try:
        return path.is_relative_to(folder.resolve())
    except OSError:  # pragma: no cover - an unresolvable folder
        return False


def check_secret_file(path: Path | str, what: str) -> Path:
    """The resolved path of a secret file that is mode 600 and outside the repository and ~/Documents."""
    resolved = Path(path).resolve()
    try:
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except OSError:
        raise ProdEnvError(f"{what} {resolved} can't be read") from None
    if not resolved.is_file():
        raise ProdEnvError(f"{what} {resolved} is not a file")
    if mode != 0o600:
        raise ProdEnvError(f"{what} {resolved} must be mode 600 (it is {mode:o})")
    if _is_under(resolved, REPO_ROOT):
        raise ProdEnvError(f"{what} {resolved} must be outside the repository")
    if _is_under(resolved, DOCUMENTS):
        raise ProdEnvError(f"{what} {resolved} must be outside ~/Documents (iCloud can sync it)")
    return resolved


def read_admin_dsn(path: Path | str) -> str:
    resolved = check_secret_file(path, "the admin DSN file")
    dsn = resolved.read_text().strip()
    if not dsn:
        raise ProdEnvError("the admin DSN file is empty")
    return dsn


def read_passwords(path: Path | str) -> tuple[str, str]:
    """(owner password, app password) from the `owner=...` / `app=...` lines."""
    resolved = check_secret_file(path, "the passwords file")
    found: dict[str, str] = {}
    for line in resolved.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() in ("owner", "app"):
            found[key.strip()] = value.strip()
    missing = [key for key in ("owner", "app") if not found.get(key)]
    if missing:
        raise ProdEnvError(f"the passwords file has no {' or '.join(missing)}= line")
    return found["owner"], found["app"]


# --- database -----------------------------------------------------------------------------------------------


def _libpq(url: str) -> dict[str, Any]:
    """conninfo parameters of a DSN or a SQLAlchemy-style `postgresql+psycopg://` URL."""
    text = url.strip()
    if text.startswith("postgresql+psycopg://"):
        text = "postgresql://" + text[len("postgresql+psycopg://") :]
    try:
        return conninfo_to_dict(text)
    except psycopg.ProgrammingError:
        raise ProdEnvError("a database URL could not be parsed") from None


def _scrub(message: str, secret_values: Sequence[str]) -> str:
    for value in secret_values:
        if value:
            message = message.replace(value, "***")
    return message.strip().splitlines()[0] if message.strip() else ""


def _failure(step: str, exc: BaseException, secret_values: Sequence[str]) -> ProdEnvError:
    return ProdEnvError(f"{step}: {type(exc).__name__}: {_scrub(str(exc), secret_values)}")


def _connect(params: dict[str, Any], **overrides: Any) -> psycopg.Connection[Any]:
    return psycopg.connect(make_conninfo(**{**params, **overrides}), autocommit=True, connect_timeout=10)


def _check_names(database: str, owner_role: str, app_role: str) -> None:
    if database != DATABASE:
        raise ProdEnvError(f"refused: database {database!r} is not {DATABASE!r}")
    if owner_role != OWNER_ROLE:
        raise ProdEnvError(f"refused: owner role {owner_role!r} is not {OWNER_ROLE!r}")
    if app_role != APP_ROLE:
        raise ProdEnvError(f"refused: app role {app_role!r} is not {APP_ROLE!r}")


def _role_setting(role: str, entry: str) -> sql.Composed:
    """ALTER ROLE <role> SET <name> = <value(s)> for one `name=value` rolconfig entry. A list setting
    (LIST_SETTINGS, e.g. search_path `trader, public`) is set element by element, each as a literal; any other
    value (e.g. an application_name with a comma) is one literal."""
    name, _, value = entry.partition("=")
    if name.lower() in LIST_SETTINGS:
        items = [item.strip().strip('"') for item in value.split(",")]
    else:
        items = [value]
    return sql.SQL("ALTER ROLE {} SET {} = {}").format(
        sql.Identifier(role), sql.Identifier(name), sql.SQL(", ").join(sql.Literal(i) for i in items)
    )


def create_db(
    admin_dsn: str,
    *,
    owner_password: str,
    app_password: str,
    dev_compare: bool = False,
    out: Out = print,
    database: str = DATABASE,
    owner_role: str = OWNER_ROLE,
    app_role: str = APP_ROLE,
) -> None:
    """Create or update prod's roles, database, schema and grants, idempotently (P6-T6 Interfaces)."""
    _check_names(database, owner_role, app_role)
    if not owner_password or not app_password:
        raise ProdEnvError("refused: both passwords must be set")
    if owner_password == app_password:
        raise ProdEnvError("refused: the owner and app passwords must differ")
    params = _libpq(admin_dsn)
    admin_db = str(params.get("dbname", ""))
    if admin_db not in ADMIN_DATABASES:
        raise ProdEnvError(f"refused: the admin DSN must name database postgres, not {admin_db!r}")
    hidden = [str(params.get("password", "")), owner_password, app_password, admin_dsn]
    step = "connect as the admin"
    try:
        with _connect(params) as conn:
            step = "read the admin role"
            row = conn.execute("SELECT current_user").fetchone()
            admin_user = str(row[0]) if row else ""
            db_owner, encoding = admin_user, "UTF8"
            role_settings: dict[str, list[str]] = {owner_role: [], app_role: []}
            if dev_compare:
                step = "read dev's setup"
                db_owner, encoding = _read_dev(conn, admin_user, owner_role, role_settings, out)

            for role, password in ((owner_role, owner_password), (app_role, app_password)):
                step = f"role {role}"
                exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
                verb = "ALTER ROLE {} WITH" if exists else "CREATE ROLE {}"
                conn.execute(
                    sql.SQL(verb + " LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(scram_sha256_verifier(password))
                    )
                )
                for entry in role_settings[role]:
                    conn.execute(_role_setting(role, entry))
                out(f"role {role}: {'updated' if exists else 'created'}")

            step = f"database {database}"
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
            if not exists:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} WITH OWNER {} ENCODING {} TEMPLATE template0").format(
                        sql.Identifier(database), sql.Identifier(db_owner), sql.Literal(encoding)
                    )
                )
            out(f"database {database}: {'exists' if exists else 'created'}")
            db = sql.Identifier(database)
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
            conn.execute(
                sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(db, sql.Identifier(owner_role))
            )
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, sql.Identifier(app_role)))
            out(f"database {database}: PUBLIC revoked, {owner_role} connect+create, {app_role} connect")

        step = f"schema trader in {database}"
        with _connect(params, dbname=database) as conn:
            owner, app = sql.Identifier(owner_role), sql.Identifier(app_role)
            had = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'trader'").fetchone()
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS trader AUTHORIZATION {}").format(owner))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA trader TO {}").format(app))
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA trader "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                ).format(owner, app)
            )
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA trader "
                    "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
                ).format(owner, app)
            )
            # PostgreSQL 14 lets PUBLIC create in schema public: only its owner (and superusers) may now.
            conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            out(f"schema trader: {'exists' if had else 'created'}, owner {owner_role}, usage {app_role}")
            out("schema public: CREATE revoked from PUBLIC")
            out(f"default privileges: {app_role} on {owner_role}'s tables and sequences")
    except psycopg.Error as exc:
        raise _failure(step, exc, hidden) from None


def _read_dev(
    conn: psycopg.Connection[Any],
    admin_user: str,
    owner_role: str,
    role_settings: dict[str, list[str]],
    out: Out,
) -> tuple[str, str]:
    """(owner, encoding) for the new database from trader_dev's, and each dev role's settings (read only)."""
    row = conn.execute(
        "SELECT pg_get_userbyid(datdba), pg_encoding_to_char(encoding) FROM pg_database WHERE datname = %s",
        (DEV_DATABASE,),
    ).fetchone()
    db_owner, encoding = admin_user, "UTF8"
    if row is None:
        out(f"dev: database {DEV_DATABASE} not found; owner {admin_user}, encoding UTF8")
    else:
        dev_owner, encoding = str(row[0]), str(row[1])
        db_owner = owner_role if dev_owner == DEV_ROLES[OWNER_ROLE] else admin_user
        out(
            f"dev: database {DEV_DATABASE} owner {dev_owner}, encoding {encoding} "
            f"-> {DATABASE} owner {db_owner}"
        )
    for role, dev_role in DEV_ROLES.items():
        found = conn.execute("SELECT rolconfig FROM pg_roles WHERE rolname = %s", (dev_role,)).fetchone()
        entries = [str(e) for e in (found[0] or [])] if found else []
        target = role if role != OWNER_ROLE else owner_role
        role_settings.setdefault(target, []).extend(entries)
        names = ", ".join(e.split("=", 1)[0] for e in entries) or "none"
        out(f"dev: role {dev_role} {'settings: ' + names if found else 'not found'}")
    return db_owner, encoding


def _check_url(url: str, role: str) -> dict[str, Any]:
    params = _libpq(url)
    if params.get("user") != role:
        raise ProdEnvError(f"refused: the {role} URL names role {params.get('user')!r}")
    if params.get("dbname") != DATABASE:
        raise ProdEnvError(
            f"refused: the {role} URL names database {params.get('dbname')!r}, not {DATABASE!r}"
        )
    return params


def verify_roles(owner_url: str, app_url: str, out: Out = print) -> None:
    """trader_app connects but can't create a table; trader_owner can (the probe table is dropped again)."""
    owner_params = _check_url(owner_url, OWNER_ROLE)
    app_params = _check_url(app_url, APP_ROLE)
    hidden = [owner_url, app_url, str(owner_params.get("password", "")), str(app_params.get("password", ""))]
    step = f"{APP_ROLE}: connect"
    try:
        with _connect(app_params) as conn:
            conn.execute("SELECT 1").fetchone()
            step = f"{APP_ROLE}: create probe"
            try:
                conn.execute(sql.SQL("CREATE TABLE {} (id integer)").format(PROBE_TABLE))
            except psycopg.errors.InsufficientPrivilege:
                out(f"{APP_ROLE}: connect ok, create refused")
            else:
                conn.execute(sql.SQL("DROP TABLE {}").format(PROBE_TABLE))  # leave nothing behind
                raise ProdEnvError(f"{APP_ROLE} could create a table in schema trader (it must not)")
        step = f"{OWNER_ROLE}: create probe"
        with _connect(owner_params) as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(PROBE_TABLE))
            conn.execute(sql.SQL("CREATE TABLE {} (id integer)").format(PROBE_TABLE))
            conn.execute(sql.SQL("DROP TABLE {}").format(PROBE_TABLE))
            out(f"{OWNER_ROLE}: create ok")
    except psycopg.Error as exc:
        raise _failure(step, exc, hidden) from None


# --- CLI ----------------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prod_env.py", description="Prod database setup (P6-T6).")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-db", help="create or update prod's roles, database and grants")
    create.add_argument("--admin-dsn-file", required=True, help="mode-600 file with a superuser DSN")
    create.add_argument("--passwords-file", required=True, help="mode-600 file with owner=... and app=...")
    create.add_argument(
        "--dev-compare", action="store_true", help="copy trader_dev's owner/encoding/settings"
    )
    args = parser.parse_args(argv)
    try:
        dsn = read_admin_dsn(args.admin_dsn_file)
        owner_password, app_password = read_passwords(args.passwords_file)
        create_db(dsn, owner_password=owner_password, app_password=app_password, dev_compare=args.dev_compare)
    except ProdEnvError as exc:
        print(f"prod_env: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
