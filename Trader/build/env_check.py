"""Fingerprint an environment's secrets and compare two fingerprints (P6-T6, amended; stdlib, Python 3.10+).

Inside a container (reads its environment; nothing is printed but hash prefixes and non-secret parts):
    docker --context shared-docker-server exec -i <container> python - dump < Trader/build/env_check.py \
        > x.json
From a local env file (before P6-T14 retires .env.dev):
    python3 Trader/build/env_check.py dump --env-file PATH > y.json
Compare:
    python3 Trader/build/env_check.py compare A.json B.json --expect same|different [--ignore KEY ...]
    python3 Trader/build/env_check.py compare prod.json dev.json --rules prod [--ignore KEY ...]

`dump` prints one JSON object: `app_env`, `public_base_url`, the parsed parts of the two database URLs
(role, host, port, database) and, per key, "empty" or the first 12 hex characters of the value's SHA-256
(the URL passwords as `DATABASE_URL.password` / `MIGRATION_DATABASE_URL.password`). The container's
entrypoint unsets MIGRATION_DATABASE_URL and ADMIN_PASSWORD_INITIAL in its own process tree; a `docker exec`
starts from the container's configured environment and still has them, and when a key is missing anyway
`dump` reads the init process's environment (/proc/1/environ) and reports "unavailable" when it can't.

`compare` prints one line per key (`KEY same`, `KEY different`, `KEY ok`, `KEY SAME AS DEV`,
`KEY empty (Stephen)`, `KEY unavailable`, `KEY wrong: <rule>`) and never a value or a hash. Exit 0 only when
every expectation holds, 1 when one doesn't, 2 on bad usage or an unreadable dump.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

KEYS = (
    "DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "APP_ENCRYPTION_KEY",
    "SESSION_SECRET",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD_INITIAL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ANTHROPIC_API_KEY",
    "QUESTRADE_REFRESH_TOKEN",
    "PUBLIC_BASE_URL",
    "TZ_DISPLAY",
    "TRADER_FORWARDED_ALLOW_IPS",
)
URL_KEYS = ("DATABASE_URL", "MIGRATION_DATABASE_URL")
UNSET_BY_ENTRYPOINT = ("MIGRATION_DATABASE_URL", "ADMIN_PASSWORD_INITIAL")
PROC_ENVIRON = Path("/proc/1/environ")
EMPTY = "empty"
UNAVAILABLE = "unavailable"

# The prod rules (A = prod, B = dev): the first plan's `prod_env.py check`, on container fingerprints.
PROD_REQUIRED = (
    "DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "APP_ENCRYPTION_KEY",
    "SESSION_SECRET",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD_INITIAL",
    "PUBLIC_BASE_URL",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_BOT_TOKEN",
    "ANTHROPIC_API_KEY",
)
STEPHENS_KEYS = ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY")
PROD_DIFFERENT_FROM_DEV = (
    "APP_ENCRYPTION_KEY",
    "SESSION_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL.password",
    "MIGRATION_DATABASE_URL.password",
)
PROD_PUBLIC_BASE_URL = "https://trader.sunspinner.ca"
PROD_DB = {
    "DATABASE_URL": {"role": "trader_app", "host": "192.168.68.86", "port": 5432, "database": "trader"},
    "MIGRATION_DATABASE_URL": {
        "role": "trader_owner",
        "host": "192.168.68.86",
        "port": 5432,
        "database": "trader",
    },
}


class UsageError(Exception):
    """Bad arguments or an unreadable dump (exit 2)."""


# --- dump ---------------------------------------------------------------------------------------------------


def _fingerprint(value: str | None) -> str:
    if value is None:
        return UNAVAILABLE
    if value == "":
        return EMPTY
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _url_parts(url: str) -> tuple[dict[str, object] | None, str]:
    """(role/host/port/database, password) of a database URL; (None, "") when it can't be parsed."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None, ""
    if not parts.scheme or not parts.hostname:
        return None, ""
    info: dict[str, object] = {
        "role": unquote(parts.username or ""),
        "host": parts.hostname,
        "port": port,
        "database": parts.path.lstrip("/"),
    }
    return info, unquote(parts.password or "")


def fingerprints(values: dict[str, str | None], app_env: str | None) -> dict[str, object]:
    """The dump object for `values` (a key's value, "" when unset, None when it can't be known)."""
    keys: dict[str, str] = {}
    db: dict[str, object] = {}
    for key in KEYS:
        value = values.get(key, "")
        keys[key] = _fingerprint(value)
        if key in URL_KEYS:
            if value is None:
                db[key] = UNAVAILABLE
                keys[f"{key}.password"] = UNAVAILABLE
            elif value == "":
                db[key] = None
                keys[f"{key}.password"] = EMPTY
            else:
                parts, password = _url_parts(value)
                db[key] = parts
                keys[f"{key}.password"] = _fingerprint(password)
    public = values.get("PUBLIC_BASE_URL")
    return {"app_env": app_env or None, "public_base_url": public or None, "db": db, "keys": keys}


def _read_proc_environ(path: Path) -> dict[str, str] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    result: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep:
            result[key.decode(errors="replace")] = value.decode(errors="replace")
    return result


def values_from_environ(environ: dict[str, str], proc_environ: Path) -> dict[str, str | None]:
    values: dict[str, str | None] = {key: environ.get(key, "") for key in KEYS}
    missing = [key for key in UNSET_BY_ENTRYPOINT if key not in environ]
    if missing:
        init_env = _read_proc_environ(proc_environ)
        for key in missing:
            values[key] = None if init_env is None else init_env.get(key, "")
    return values


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=value lines the way compose reads an env file: comments and blank lines skipped, an optional
    `export `, the value stripped, surrounding single or double quotes removed, and for an unquoted value an
    inline ` #` comment dropped."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        result[key] = value
    return result


def dump(env_file: str | None) -> dict[str, object]:
    if env_file is None:
        environ = dict(os.environ)
        return fingerprints(values_from_environ(environ, PROC_ENVIRON), environ.get("APP_ENV"))
    try:
        text = Path(env_file).read_text()
    except OSError as exc:
        raise UsageError(f"can't read the env file ({type(exc).__name__})") from None
    parsed = parse_env_file(text)
    values: dict[str, str | None] = {key: parsed.get(key, "") for key in KEYS}
    return fingerprints(values, parsed.get("APP_ENV"))


# --- compare ------------------------------------------------------------------------------------------------


def _load(path: str) -> dict[str, object]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise UsageError(f"can't read the dump {path} ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
        raise UsageError(f"{path} is not an env_check dump")
    return data


def _ignored(key: str, ignore: set[str]) -> bool:
    return key in ignore or key.split(".", 1)[0] in ignore


def _ordered_keys(a: dict[str, str], b: dict[str, str]) -> list[str]:
    order = []
    for key in KEYS:
        order.append(key)
        if key in URL_KEYS:
            order.append(f"{key}.password")
    extra = sorted((set(a) | set(b)) - set(order))
    return [k for k in order if k in a or k in b] + extra


def compare_expect(
    a: dict[str, object], b: dict[str, object], expect: str, ignore: set[str]
) -> tuple[list[str], bool]:
    keys_a: dict[str, str] = a["keys"]  # type: ignore[assignment]
    keys_b: dict[str, str] = b["keys"]  # type: ignore[assignment]
    lines: list[str] = []
    ok = True
    for key in _ordered_keys(keys_a, keys_b):
        if _ignored(key, ignore):
            continue
        va, vb = keys_a.get(key, EMPTY), keys_b.get(key, EMPTY)
        if UNAVAILABLE in (va, vb):
            lines.append(f"{key} unavailable")
            ok = False
            continue
        state = "same" if va == vb else "different"
        lines.append(f"{key} {state}")
        ok = ok and state == expect
    return lines, ok


def _db_rule(key: str, parts: object) -> str | None:
    """None when the URL's parts match prod's, else the rule it breaks."""
    if parts == UNAVAILABLE:
        return UNAVAILABLE
    expected = PROD_DB[key]
    if not isinstance(parts, dict):
        return "not a database URL"
    for name in ("role", "host", "port", "database"):
        if parts.get(name) != expected[name]:
            return f"{name} must be {expected[name]}"
    return None


def compare_prod(prod: dict[str, object], dev: dict[str, object], ignore: set[str]) -> tuple[list[str], bool]:
    keys_p: dict[str, str] = prod["keys"]  # type: ignore[assignment]
    keys_d: dict[str, str] = dev["keys"]  # type: ignore[assignment]
    db_p: dict[str, object] = prod.get("db") or {}  # type: ignore[assignment]
    checked = []
    for key in PROD_REQUIRED:
        checked.append(key)
        if key in URL_KEYS:
            checked.append(f"{key}.password")
    lines: list[str] = []
    ok = True

    def bad(line: str) -> None:
        nonlocal ok
        lines.append(line)
        ok = False

    for key in checked:
        if _ignored(key, ignore):
            continue
        value = keys_p.get(key, EMPTY)
        if value == UNAVAILABLE:
            bad(f"{key} unavailable")
            continue
        if value == EMPTY:
            bad(f"{key} empty (Stephen)" if key in STEPHENS_KEYS else f"{key} wrong: required, empty")
            continue
        if key == "PUBLIC_BASE_URL":
            if prod.get("public_base_url") != PROD_PUBLIC_BASE_URL:
                bad(f"{key} wrong: must be {PROD_PUBLIC_BASE_URL}")
                continue
        if key in URL_KEYS:
            rule = _db_rule(key, db_p.get(key))
            if rule is not None:
                bad(f"{key} wrong: {rule}")
                continue
        if key in PROD_DIFFERENT_FROM_DEV:
            dev_value = keys_d.get(key, EMPTY)
            if dev_value == UNAVAILABLE:
                bad(f"{key} unavailable (dev)")
            elif dev_value == value:
                bad(f"{key} SAME AS DEV")
            else:
                lines.append(f"{key} different")
            continue
        lines.append(f"{key} ok")
    return lines, ok


# --- CLI ----------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="env_check.py", description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dump", help="fingerprint this environment (or --env-file) as JSON")
    d.add_argument("--env-file", help="a local env file instead of the process environment")
    c = sub.add_parser("compare", help="compare two dumps")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--expect", choices=("same", "different"))
    c.add_argument("--rules", choices=("prod",), help="A = prod, B = dev: the prod rules")
    c.add_argument("--ignore", action="append", default=[], metavar="KEY")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if args.command == "dump":
            print(json.dumps(dump(args.env_file), indent=2, sort_keys=True))
            return 0
        if (args.expect is None) == (args.rules is None):
            raise UsageError("compare needs exactly one of --expect and --rules")
        a, b = _load(args.a), _load(args.b)
        ignore = set(args.ignore)
        if args.rules == "prod":
            lines, ok = compare_prod(a, b, ignore)
        else:
            lines, ok = compare_expect(a, b, args.expect, ignore)
    except UsageError as exc:
        print(f"env_check: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
