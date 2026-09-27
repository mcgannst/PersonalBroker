# Phase 1: Data Layer Implementation Plan

> **For agentic workers:** Run under the gauntlet process in [`2026-09-26-build-master-plan.md`](2026-09-26-build-master-plan.md) (§4–§6). Read its **Global Constraints** and **Review Focus** first; they apply to every task here. Steps use checkbox (`- [ ]`) syntax: tick each one in this file as you complete it and commit the file with your code.

**Goal:** A tested Python package that can build the nightly universe from FinViz, resolve symbols and fetch candles from Questrade with a safely rotating token, compute ATR and opening-bar statistics, and store everything in `trader_dev`, run from a `trader` CLI.

**Architecture:** `Trader/app/trader/` holds config, DB models and Alembic migrations, a market calendar and clock, crypto and runtime settings, the FinViz adapter (sync, polite), the Questrade adapter (sync token owner plus an async rate-limited data client), pure indicator functions, a job runner and the nightly job. Tests use respx for HTTP and a PostgreSQL 14 testcontainer for the database.

**Tech stack:** Python 3.12 via uv; SQLAlchemy 2, Alembic, psycopg 3; httpx; selectolax; pydantic v2 + pydantic-settings; cryptography (Fernet); exchange_calendars; pandas; Typer; pytest, pytest-asyncio, respx, testcontainers.

**Spec:** [`../BRD.md`](../BRD.md) BR-01, BR-02, BR-04 (data part); [`../SPEC.md`](../SPEC.md) §2, §3, §4.1, §4.2, §9 (nightly and token-refresh rows), §10 (Phase 1 tables), §13; [`../../spikes/README.md`](../../spikes/README.md).

## Task list, dependencies and parallel lanes

| ID | Task | Depends on | Lane |
|---|---|---|---|
| P1-T1 | Toolchain, project scaffold, env keys, quality gate | none | A |
| P1-T2 | Database models, migration 0001, test database fixture | T1 | A |
| P1-T3 | Crypto and runtime settings store | T2 | B |
| P1-T4 | Market types, clock and session calendar | T1 | C |
| P1-T5 | FinViz parser and scraper | T1 | D |
| P1-T6 | Questrade auth (token owner), bootstrap, seed and keep-alive CLI | T2, T3, T4 | A |
| P1-T7 | Questrade data client and `questrade-check` CLI | T6 | A |
| P1-T8 | Indicators | T4 | C |
| P1-T9 | Job runner, market data repository, nightly job, `notify` CLI | T5, T7, T8 | A |

After T2, lanes B, C and D can run in parallel with each other (disjoint files). Every command below runs from `Trader/app/` unless it says otherwise.

## File map

| Path (under `Trader/`) | Responsibility | Task |
|---|---|---|
| `app/pyproject.toml`, `app/uv.lock`, `app/.python-version` | Project, dependencies, tool config | T1 |
| `app/scripts/check.sh` | Quality gate | T1 |
| `app/scripts/trader-dev.sh` | Run the CLI with `docker/.env.dev` loaded | T1 |
| `build/env_setup.py` | One-off: add/rename keys in `docker/.env.dev` without printing them | T1 |
| `build/notify.py` | Telegram notice for the orchestrator before `trader notify` exists | T1 |
| `app/trader/__init__.py`, `app/trader/config.py`, `app/trader/cli.py` | Package, env settings, CLI entry | T1 (cli extended in T6, T7, T9) |
| `app/alembic.ini`, `app/trader/db/{__init__,session,models}.py`, `app/trader/db/migrations/{env.py,script.py.mako,versions/0001_phase1_core.py}` | DB layer | T2 |
| `app/tests/conftest.py` | Test DB fixtures | T2 |
| `app/trader/crypto.py`, `app/trader/settings_store.py` | Fernet wrapper; runtime settings with audit | T3 |
| `app/trader/market/{__init__,types,clock,calendar}.py` | Candle type, clock, exchange calendar | T4 |
| `app/trader/adapters/__init__.py`, `app/trader/adapters/finviz/{__init__,parser,scraper}.py` | FinViz | T5 |
| `app/tests/fixtures/finviz/*` | Saved FinViz pages (copied from `spikes/fixtures/finviz/`) | T5 |
| `app/trader/adapters/questrade/{__init__,auth}.py`, `app/trader/bootstrap.py` | Token owner; wiring | T6 |
| `app/trader/adapters/questrade/{client,models}.py` | Data client | T7 |
| `app/trader/market/indicators.py` | ATR, RTH filter, opening bar, rvol, doji | T8 |
| `app/trader/events.py`, `app/trader/jobs/{__init__,runner,nightly}.py`, `app/trader/market/repository.py` | Jobs and persistence | T9 |

---

### Task P1-T1: Toolchain, project scaffold, env keys, quality gate

**Files:**
- Create: `Trader/app/pyproject.toml`, `Trader/app/.python-version`, `Trader/app/trader/__init__.py`, `Trader/app/trader/config.py`, `Trader/app/trader/cli.py`, `Trader/app/scripts/check.sh`, `Trader/app/scripts/trader-dev.sh`, `Trader/app/tests/__init__.py`, `Trader/app/tests/test_config.py`, `Trader/app/tests/test_cli.py`, `Trader/build/env_setup.py`, `Trader/build/notify.py`
- Modify: `Trader/docker/.env.dev` (git-ignored; via `env_setup.py` only)

**Interfaces:**
- Produces: `trader.__version__: str`; `trader.config.EnvSettings` (fields below); `trader.config.get_env() -> EnvSettings`; `trader.cli.app: typer.Typer`; `scripts/check.sh`; `scripts/trader-dev.sh <command>`.

- [x] **Step 1: Install the toolchain**

Run (from anywhere):
```bash
brew install uv
uv python install 3.12
```
Expected: `uv --version` prints a version; `uv python find 3.12` prints a path.

- [x] **Step 2: Create `Trader/app/pyproject.toml`**

```toml
[project]
name = "trader"
version = "0.1.0"
description = "Trader simulation platform"
requires-python = ">=3.12,<3.13"
dependencies = [
  "sqlalchemy>=2.0.35,<2.1",
  "alembic>=1.13",
  "psycopg[binary]>=3.2",
  "httpx>=0.27",
  "selectolax>=0.3.21",
  "pydantic>=2.8",
  "pydantic-settings>=2.4",
  "cryptography>=43",
  "exchange-calendars>=4.5",
  "pandas>=2.2",
  "typer>=0.12",
  "structlog>=24.4",
]

[project.scripts]
trader = "trader.cli:app"

[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "respx>=0.21",
  "testcontainers[postgres]>=4.8",
  "ruff>=0.6",
  "mypy>=1.11",
  "pandas-stubs>=2.2",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["trader"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = ["db: uses the PostgreSQL test container (needs Docker Desktop)"]

[tool.ruff]
line-length = 110
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "S", "DTZ"]
ignore = ["S101"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S", "DTZ"]

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["exchange_calendars.*", "selectolax.*", "testcontainers.*", "respx.*"]
ignore_missing_imports = true
```

Create `Trader/app/.python-version` containing `3.12`.

- [x] **Step 3: Write the failing tests**

`Trader/app/tests/__init__.py`: empty file.

`Trader/app/tests/test_config.py`:
```python
import pytest

from trader.config import EnvSettings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://app:pw@db:5432/trader_dev")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://owner:pw@db:5432/trader_dev")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "k" * 44)
    monkeypatch.setenv("SESSION_SECRET", "super-secret-value")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


def test_reads_environment(env: None) -> None:
    s = EnvSettings()  # type: ignore[call-arg]
    assert s.database_url.get_secret_value().endswith("/trader_dev")
    assert s.telegram_chat_id == 42
    assert s.tz_display == "America/Edmonton"


def test_secrets_are_hidden_in_repr(env: None) -> None:
    s = EnvSettings()  # type: ignore[call-arg]
    assert "super-secret-value" not in repr(s)
    assert s.session_secret.get_secret_value() == "super-secret-value"


def test_missing_required_value_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(Exception):
        EnvSettings()  # type: ignore[call-arg]
```

`Trader/app/tests/test_cli.py`:
```python
from typer.testing import CliRunner

from trader import __version__
from trader.cli import app


def test_version_command() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
```

- [x] **Step 4: Run the tests to see them fail**

Run: `uv sync && uv run pytest -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader'` (or import errors for `trader.config`).

- [x] **Step 5: Implement the package, config and CLI**

`Trader/app/trader/__init__.py`:
```python
"""Trader simulation platform."""

__version__ = "0.1.0"
```

`Trader/app/trader/config.py`:
```python
"""Process configuration from environment variables (SPEC §13).

Runtime settings that Stephen edits in the UI live in the database instead
(see trader.settings_store).
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # The URLs embed the DB role passwords, so they are secrets too: use .get_secret_value().
    database_url: SecretStr
    migration_database_url: SecretStr
    app_encryption_key: SecretStr
    session_secret: SecretStr
    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    questrade_refresh_token: SecretStr | None = None
    public_base_url: str = "https://trader-dev.sunspinner.ca"
    tz_display: str = "America/Edmonton"


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    return EnvSettings()  # type: ignore[call-arg]
```

`Trader/app/trader/cli.py`:
```python
"""`trader <command>` entry points."""

import typer

from trader import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Trader simulation platform."""


@app.command()
def version() -> None:
    """Print the Trader version."""
    typer.echo(__version__)
```

- [x] **Step 6: Create the scripts**

`Trader/app/scripts/check.sh`:
```bash
#!/usr/bin/env bash
# Quality gate: every builder runs this before committing; the Verifier runs it on trunk.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --quiet
uv run ruff check .
uv run ruff format --check .
uv run mypy trader
uv run pytest -q "$@"
```

`Trader/app/scripts/trader-dev.sh`:
```bash
#!/usr/bin/env bash
# Run the trader CLI against the dev environment (secrets from docker/.env.dev).
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --env-file ../docker/.env.dev trader "$@"
```

Run: `chmod +x scripts/check.sh scripts/trader-dev.sh`

- [x] **Step 7: Run the gate**

Run: `uv run ruff format . && bash scripts/check.sh`
Expected: ruff and mypy report no errors; `4 passed`.

- [x] **Step 8: Prepare `docker/.env.dev` keys (no values printed)**

The SPEC names the owner URL `MIGRATION_DATABASE_URL` (the file currently calls it `DATABASE_OWNER_URL`) and needs `APP_ENCRYPTION_KEY` and `SESSION_SECRET`. Create `Trader/build/env_setup.py`:
```python
"""Idempotently align Trader/docker/.env.dev with SPEC §13. Prints key names only, never values."""

import base64
import os
import re
import secrets
import tempfile
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / "docker" / ".env.dev"


def main() -> None:
    text = ENV.read_text()
    changed: list[str] = []
    if re.search(r"^DATABASE_OWNER_URL=", text, re.M) and not re.search(r"^MIGRATION_DATABASE_URL=", text, re.M):
        text = re.sub(r"^DATABASE_OWNER_URL=", "MIGRATION_DATABASE_URL=", text, flags=re.M)
        changed.append("MIGRATION_DATABASE_URL (renamed)")
    if not re.search(r"^APP_ENCRYPTION_KEY=", text, re.M):
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        text += f"\n# Fernet key for tokens at rest. Added by build/env_setup.py.\nAPP_ENCRYPTION_KEY={key}\n"
        changed.append("APP_ENCRYPTION_KEY")
    if not re.search(r"^SESSION_SECRET=", text, re.M):
        text += f"SESSION_SECRET={secrets.token_urlsafe(48)}\n"
        changed.append("SESSION_SECRET")
    if changed:
        fd, tmp = tempfile.mkstemp(dir=ENV.parent, prefix=".env.dev.")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, ENV)
    print("changed:", ", ".join(changed) or "nothing")


if __name__ == "__main__":
    main()
```

Run (from `Trader/`): `python3 build/env_setup.py`
Expected: `changed: MIGRATION_DATABASE_URL (renamed), APP_ENCRYPTION_KEY, SESSION_SECRET`. Running it again prints `changed: nothing`.

- [x] **Step 9: Create the orchestrator's notify script**

`Trader/build/notify.py`:
```python
"""Send a Telegram message to Stephen via the dev bot. Used by the build orchestrator
until `trader notify` exists (P1-T9). Usage: python3 build/notify.py "text"."""

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENV = (Path(__file__).resolve().parents[1] / "docker" / ".env.dev").read_text()


def _get(key: str) -> str:
    m = re.search(rf"^{key}=(\S+)$", ENV, re.M)
    if not m:
        raise SystemExit(f"{key} missing from .env.dev")
    return m.group(1)


def main() -> None:
    text = " ".join(sys.argv[1:]) or "(empty)"
    data = urllib.parse.urlencode({"chat_id": _get("TELEGRAM_CHAT_ID"), "text": f"🛠 Trader build: {text}"})
    url = f"https://api.telegram.org/bot{_get('TELEGRAM_BOT_TOKEN')}/sendMessage"
    with urllib.request.urlopen(url, data=data.encode(), timeout=15) as r:  # noqa: S310
        print("sent" if json.loads(r.read()).get("ok") else "failed")


if __name__ == "__main__":
    main()
```

Run (from `Trader/`): `python3 build/notify.py "P1-T1 toolchain ready"`
Expected: `sent`.

- [x] **Step 10: Commit and push**

```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/pyproject.toml Trader/app/uv.lock Trader/app/.python-version Trader/app/trader/__init__.py \
  Trader/app/trader/config.py Trader/app/trader/cli.py Trader/app/scripts/check.sh Trader/app/scripts/trader-dev.sh \
  Trader/app/tests/__init__.py Trader/app/tests/test_config.py Trader/app/tests/test_cli.py \
  Trader/build/env_setup.py Trader/build/notify.py Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T1: toolchain, package scaffold, env settings, quality gate

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

---

### Task P1-T2: Database models, migration 0001, test database fixture

**Files:**
- Create: `Trader/app/alembic.ini`, `Trader/app/trader/db/__init__.py`, `Trader/app/trader/db/session.py`, `Trader/app/trader/db/models.py`, `Trader/app/trader/db/migrations/env.py`, `Trader/app/trader/db/migrations/script.py.mako`, `Trader/app/trader/db/migrations/versions/0001_phase1_core.py`, `Trader/app/tests/conftest.py`, `Trader/app/tests/db/__init__.py`, `Trader/app/tests/db/test_migration.py`

**Interfaces:**
- Consumes: nothing from earlier tasks except the project.
- Produces:
  - `trader.db.session.make_engine(url: str) -> Engine`, `make_session_factory(engine: Engine) -> sessionmaker[Session]`, `session_scope(factory) -> ContextManager[Session]` (commits on success, rolls back on error).
  - `trader.db.models`: `SCHEMA = "trader"`, `Base`, and ORM classes `Setting`, `ApiCredential`, `Symbol`, `UniverseSnapshot`, `DailyCandle`, `IntradayCandle`, `CandleArchive`, `OpenBarStat`, `JobRun`, `EventLog`, `AuditLog` (columns below).
  - Test fixtures in `tests/conftest.py`: `pg_url: str` (session), `migrated_engine: Engine` (session), `db_factory: sessionmaker[Session]` (function; truncates every table afterwards).

Notes: `api_credentials` has two columns the SPEC table omits, `last_error` and `updated_at`, because the ported refresh logic needs them (SPEC §4.1 cooldowns). `intraday_candles` is partitioned by month (SPEC §10); the app role can't create tables, so the migration creates monthly partitions from 2026-06 to 2028-12 plus a default partition.

- [x] **Step 1: Write the failing migration test**

`Trader/app/tests/db/__init__.py`: empty file.

`Trader/app/tests/db/test_migration.py`:
```python
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import IntradayCandle, Symbol

pytestmark = pytest.mark.db

PHASE1_TABLES = {
    "settings", "api_credentials", "symbols", "universe_snapshots", "daily_candles", "intraday_candles",
    "candle_archive", "open_bar_stats", "job_runs", "event_log", "audit_log", "alembic_version",
}


def test_all_phase1_tables_exist(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names(schema="trader"))
    assert PHASE1_TABLES <= tables


def test_intraday_candles_route_to_monthly_partition(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = Symbol(ticker="AAPL", exchange="NASDAQ", questrade_id=8049, currency="USD", name="Apple")
        s.add(sym)
        s.flush()
        s.add(IntradayCandle(
            symbol_id=sym.id, interval="5m", ts=datetime(2026, 9, 25, 13, 30, tzinfo=UTC),
            open=Decimal("336.04"), high=Decimal("336.80"), low=Decimal("334.53"), close=Decimal("334.92"),
            volume=403790, vwap=Decimal("335.5643"),
        ))
        s.commit()
        part = s.execute(text(
            "SELECT tableoid::regclass::text FROM trader.intraday_candles LIMIT 1"
        )).scalar_one()
    assert part == "trader.intraday_candles_202609"


def test_money_keeps_four_decimals(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = Symbol(ticker="X", exchange="NYSE", questrade_id=1, currency="USD", name=None)
        s.add(sym)
        s.commit()
        s.add(IntradayCandle(
            symbol_id=sym.id, interval="1m", ts=datetime(2026, 9, 25, 14, 0, tzinfo=UTC),
            open=Decimal("1.2345"), high=Decimal("1.2345"), low=Decimal("1.2345"), close=Decimal("1.2345"),
            volume=1, vwap=None,
        ))
        s.commit()
        got = s.execute(text("SELECT close FROM trader.intraday_candles")).scalar_one()
    assert got == Decimal("1.2345")


def test_downgrade_and_upgrade_again(pg_url: str) -> None:
    from alembic import command

    from tests.conftest import alembic_config

    cfg = alembic_config(pg_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
```

- [x] **Step 2: Write the fixtures in `Trader/app/tests/conftest.py`**

```python
"""Shared fixtures. Database tests use a throwaway PostgreSQL 14 container, never trader_dev."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from trader.db.models import Base
from trader.db.session import make_engine, make_session_factory

APP_DIR = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> Config:
    cfg = Config(str(APP_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(APP_DIR / "trader" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:14-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_engine(pg_url: str) -> Iterator[Engine]:
    from alembic import command

    command.upgrade(alembic_config(pg_url), "head")
    engine = make_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db_factory(migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield make_session_factory(migrated_engine)
    names = ", ".join(f"trader.{t.name}" for t in Base.metadata.sorted_tables)
    with migrated_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
```

- [x] **Step 3: Run the test to see it fail**

Run: `uv run pytest tests/db -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.db'`.

- [x] **Step 4: Implement the session helpers**

`Trader/app/trader/db/__init__.py`: empty file.

`Trader/app/trader/db/session.py`:
```python
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [x] **Step 5: Implement the models**

`Trader/app/trader/db/models.py`:
```python
"""ORM models (SPEC §10). Phase 1 tables only; later phases add theirs in new migrations."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "trader"
Money = Numeric(14, 4)
TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_by: Mapped[str] = mapped_column(String(50))


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    api_server: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    last_refresh_at: Mapped[datetime | None] = mapped_column(TS)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(TS)


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (UniqueConstraint("ticker", "exchange"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20))
    exchange: Mapped[str] = mapped_column(String(20))
    questrade_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    name: Mapped[str | None] = mapped_column(String(200))


class UniverseSnapshot(Base):
    __tablename__ = "universe_snapshots"
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("trader.symbols.id"), primary_key=True)
    price: Mapped[Decimal | None] = mapped_column(Money)
    avg_volume: Mapped[int | None] = mapped_column(BigInteger)
    atr14: Mapped[Decimal | None] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(20))  # finviz | fallback | manual


class DailyCandle(Base):
    __tablename__ = "daily_candles"
    symbol_id: Mapped[int] = mapped_column(ForeignKey("trader.symbols.id"), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[int] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Money)


class IntradayCandle(Base):
    __tablename__ = "intraday_candles"
    __table_args__ = {"postgresql_partition_by": "RANGE (ts)"}
    symbol_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    interval: Mapped[str] = mapped_column(String(3), primary_key=True)  # 1m | 5m
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[int] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Money)


class CandleArchive(Base):
    __tablename__ = "candle_archive"
    symbol_id: Mapped[int] = mapped_column(ForeignKey("trader.symbols.id"), primary_key=True)
    interval: Mapped[str] = mapped_column(String(3), primary_key=True)
    start_ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[int] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Money)


class OpenBarStat(Base):
    __tablename__ = "open_bar_stats"
    symbol_id: Mapped[int] = mapped_column(ForeignKey("trader.symbols.id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    avg_open_vol_14d: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    atr14: Mapped[Decimal | None] = mapped_column(Money)


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job: Mapped[str] = mapped_column(String(50), index=True)
    session_date: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(20))  # running | succeeded | failed
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[Any] = mapped_column(JSONB, nullable=True)


class EventLog(Base):
    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS)
    level: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(50))
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Any] = mapped_column(JSONB, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS)
    actor: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(100))
    before: Mapped[Any] = mapped_column(JSONB, nullable=True)
    after: Mapped[Any] = mapped_column(JSONB, nullable=True)
```

- [x] **Step 6: Implement Alembic**

`Trader/app/alembic.ini`:
```ini
[alembic]
script_location = %(here)s/trader/db/migrations
version_path_separator = os

[loggers]
keys = root

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
formatter = generic

[formatter_generic]
format = %(levelname)s %(name)s %(message)s
```

`Trader/app/trader/db/migrations/env.py`:
```python
"""Alembic environment. Runs as the owner role (MIGRATION_DATABASE_URL), or the URL tests set."""

import os

from alembic import context
from sqlalchemy import create_engine, text

from trader.db.models import SCHEMA, Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ["MIGRATION_DATABASE_URL"]

engine = create_engine(url)
with engine.connect() as connection:
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
```

`Trader/app/trader/db/migrations/script.py.mako`:
```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [x] **Step 7: Write migration 0001**

`Trader/app/trader/db/migrations/versions/0001_phase1_core.py`:
```python
"""Phase 1 core tables.

Revision ID: 0001
Revises:
"""

from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

S = "trader"
MONEY = sa.Numeric(14, 4)
TS = sa.DateTime(timezone=True)
FIRST_PARTITION = date(2026, 6, 1)
LAST_PARTITION = date(2028, 12, 1)


def _ohlcv() -> list[sa.Column[object]]:
    return [
        sa.Column("open", MONEY, nullable=False),
        sa.Column("high", MONEY, nullable=False),
        sa.Column("low", MONEY, nullable=False),
        sa.Column("close", MONEY, nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
        sa.Column("vwap", MONEY),
    ]


def _months(first: date, last: date) -> list[date]:
    out, d = [], first
    while d <= last:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.String(50), nullable=False),
        schema=S,
    )
    op.create_table(
        "api_credentials",
        sa.Column("provider", sa.String(30), primary_key=True),
        sa.Column("refresh_token_enc", sa.Text),
        sa.Column("access_token_enc", sa.Text),
        sa.Column("api_server", sa.String(200)),
        sa.Column("expires_at", TS),
        sa.Column("last_refresh_at", TS),
        sa.Column("last_error", sa.Text),
        sa.Column("updated_at", TS),
        schema=S,
    )
    op.create_table(
        "symbols",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("questrade_id", sa.BigInteger, unique=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("name", sa.String(200)),
        sa.UniqueConstraint("ticker", "exchange"),
        schema=S,
    )
    sym_fk = sa.ForeignKey(f"{S}.symbols.id")
    op.create_table(
        "universe_snapshots",
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("symbol_id", sa.BigInteger, sym_fk, primary_key=True),
        sa.Column("price", MONEY),
        sa.Column("avg_volume", sa.BigInteger),
        sa.Column("atr14", MONEY),
        sa.Column("source", sa.String(20), nullable=False),
        schema=S,
    )
    op.create_table(
        "daily_candles",
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        *_ohlcv(),
        schema=S,
    )
    op.execute(f"""
        CREATE TABLE {S}.intraday_candles (
            symbol_id bigint NOT NULL,
            interval varchar(3) NOT NULL,
            ts timestamptz NOT NULL,
            open numeric(14,4) NOT NULL, high numeric(14,4) NOT NULL,
            low numeric(14,4) NOT NULL, close numeric(14,4) NOT NULL,
            volume bigint NOT NULL, vwap numeric(14,4),
            PRIMARY KEY (symbol_id, interval, ts)
        ) PARTITION BY RANGE (ts)
    """)
    months = _months(FIRST_PARTITION, LAST_PARTITION)
    for start in months:
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        op.execute(
            f"CREATE TABLE {S}.intraday_candles_{start:%Y%m} PARTITION OF {S}.intraday_candles "
            f"FOR VALUES FROM ('{start:%Y-%m-%d}') TO ('{end:%Y-%m-%d}')"
        )
    op.execute(f"CREATE TABLE {S}.intraday_candles_default PARTITION OF {S}.intraday_candles DEFAULT")
    op.create_table(
        "candle_archive",
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("interval", sa.String(3), primary_key=True),
        sa.Column("start_ts", TS, primary_key=True),
        *_ohlcv(),
        schema=S,
    )
    op.create_table(
        "open_bar_stats",
        sa.Column("symbol_id", sa.BigInteger, sa.ForeignKey(f"{S}.symbols.id"), primary_key=True),
        sa.Column("session_date", sa.Date, primary_key=True),
        sa.Column("avg_open_vol_14d", sa.Numeric(18, 2)),
        sa.Column("atr14", MONEY),
        schema=S,
    )
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job", sa.String(50), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("detail", JSONB),
        schema=S,
    )
    op.create_index("ix_job_runs_job_session", "job_runs", ["job", "session_date"], schema=S)
    op.create_table(
        "event_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("run_id", sa.BigInteger),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("data", JSONB),
        schema=S,
    )
    op.create_index("ix_event_log_ts", "event_log", ["ts"], schema=S)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("actor", sa.String(50), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("before", JSONB),
        sa.Column("after", JSONB),
        schema=S,
    )


def downgrade() -> None:
    for table in ("audit_log", "event_log", "job_runs", "open_bar_stats", "candle_archive"):
        op.drop_table(table, schema=S)
    op.execute(f"DROP TABLE {S}.intraday_candles CASCADE")
    for table in ("daily_candles", "universe_snapshots", "symbols", "api_credentials", "settings"):
        op.drop_table(table, schema=S)
```

- [x] **Step 8: Run the tests to see them pass**

Docker Desktop must be running. Run: `uv run pytest tests/db -q`
Expected: `4 passed`.

- [x] **Step 9: Run the gate, commit and push**

Run: `uv run ruff format . && bash scripts/check.sh` → all pass.
```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/alembic.ini Trader/app/trader/db Trader/app/tests/conftest.py Trader/app/tests/db \
  Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T2: models, migration 0001 with monthly candle partitions, test DB fixtures

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

- [x] **Step 10: LIVE: apply the migration to `trader_dev`**

Run: `uv run --env-file ../docker/.env.dev alembic upgrade head`
Expected: `INFO ... Running upgrade  -> 0001`. Then check the app role sees the tables:
```bash
uv run --env-file ../docker/.env.dev python -c "
import os; from sqlalchemy import create_engine, text
e=create_engine(os.environ['DATABASE_URL'])
with e.connect() as c: print(c.execute(text('select count(*) from trader.symbols')).scalar_one())"
```
Expected: `0` (no permission error).

---

### Task P1-T3: Crypto and runtime settings store

**Files:**
- Create: `Trader/app/trader/crypto.py`, `Trader/app/trader/settings_store.py`, `Trader/app/tests/test_crypto.py`, `Trader/app/tests/db/test_settings_store.py`

**Interfaces:**
- Consumes: `trader.db.models.Setting`, `AuditLog`; `db_factory` fixture.
- Produces:
  - `trader.crypto.Crypto(key: str)` with `encrypt(plain: str) -> str` and `decrypt(token: str | None) -> str | None` (returns `None` for empty or unreadable input).
  - `trader.settings_store.RuntimeSettings` (pydantic, frozen; DB keys are the aliases): `approval_mode: Literal["manual","auto"] = "manual"`; `markets_enabled: list[Literal["US","TSX"]] = ["US"]`; `universe_finviz_filters` (key `universe.finviz_filters`) default `"ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa"`; `universe_extra_symbols` (key `universe.extra_symbols`) default `["SPY"]`; `finviz_min_interval_seconds` (key `finviz.min_interval_seconds`, ≥ 2.0) default `2.0`; `finviz_cache_hours` (key `finviz.cache_hours`) default `12.0`; `open_bar_lookback_sessions` (key `open_bar.lookback_sessions`, 5–30) default `14`.
  - `trader.settings_store.SettingsStore(factory: sessionmaker[Session], now: Callable[[], datetime])` with `load() -> RuntimeSettings` and `set(key: str, value: Any, actor: str) -> RuntimeSettings` (raises `KeyError` for unknown keys, `pydantic.ValidationError` for bad values; writes `settings` and `audit_log` in one transaction).

- [x] **Step 1: Write the failing tests**

`Trader/app/tests/test_crypto.py`:
```python
from cryptography.fernet import Fernet

from trader.crypto import Crypto


def test_round_trip() -> None:
    c = Crypto(Fernet.generate_key().decode())
    token = c.encrypt("refresh-abc")
    assert token != "refresh-abc"
    assert c.decrypt(token) == "refresh-abc"


def test_wrong_key_or_garbage_returns_none() -> None:
    a, b = Crypto(Fernet.generate_key().decode()), Crypto(Fernet.generate_key().decode())
    assert b.decrypt(a.encrypt("x")) is None
    assert a.decrypt("not-a-token") is None
    assert a.decrypt(None) is None
    assert a.decrypt("") is None
```

`Trader/app/tests/db/test_settings_store.py`:
```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import AuditLog, Setting
from trader.settings_store import RuntimeSettings, SettingsStore

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def store(factory: sessionmaker[Session]) -> SettingsStore:
    return SettingsStore(factory, now=lambda: NOW)


def test_defaults_when_table_empty(db_factory: sessionmaker[Session]) -> None:
    s = store(db_factory).load()
    assert s == RuntimeSettings()
    assert s.approval_mode == "manual"
    assert s.universe_finviz_filters.startswith("ind_stocksonly,")
    assert s.universe_extra_symbols == ["SPY"]


def test_set_persists_and_audits(db_factory: sessionmaker[Session]) -> None:
    st = store(db_factory)
    updated = st.set("approval_mode", "auto", actor="stephen")
    assert updated.approval_mode == "auto"
    assert st.load().approval_mode == "auto"
    with db_factory() as s:
        row = s.get(Setting, "approval_mode")
        assert row is not None and row.value == "auto" and row.updated_by == "stephen"
        audit = s.execute(select(AuditLog)).scalar_one()
    assert audit.action == "settings.set:approval_mode"
    assert audit.before == {"value": "manual"} and audit.after == {"value": "auto"}
    assert audit.ts == NOW


def test_dotted_key(db_factory: sessionmaker[Session]) -> None:
    st = store(db_factory)
    st.set("open_bar.lookback_sessions", 10, actor="stephen")
    assert st.load().open_bar_lookback_sessions == 10


def test_invalid_value_rejected_and_not_saved(db_factory: sessionmaker[Session]) -> None:
    st = store(db_factory)
    with pytest.raises(ValidationError):
        st.set("finviz.min_interval_seconds", 1.0, actor="stephen")
    assert st.load().finviz_min_interval_seconds == 2.0


def test_unknown_key_rejected(db_factory: sessionmaker[Session]) -> None:
    with pytest.raises(KeyError):
        store(db_factory).set("no.such.key", 1, actor="stephen")


def test_unknown_rows_in_db_are_ignored(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(Setting(key="future.phase.key", value=1, updated_by="x"))
        s.commit()
    assert store(db_factory).load() == RuntimeSettings()
```

- [x] **Step 2: Run to see them fail**

Run: `uv run pytest tests/test_crypto.py tests/db/test_settings_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.crypto'`.

- [x] **Step 3: Implement `trader/crypto.py`**

```python
"""Fernet encryption for secrets at rest (SPEC §14)."""

from cryptography.fernet import Fernet, InvalidToken


class Crypto:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        """Plain text, or None when the token is empty or can't be read with this key."""
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None
```

- [x] **Step 4: Implement `trader/settings_store.py`**

```python
"""Runtime settings stored one key per row in trader.settings (SPEC §13), with an audit trail."""

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import AuditLog, Setting
from trader.db.session import session_scope

DEFAULT_UNIVERSE_FILTERS = "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa"


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    approval_mode: Literal["manual", "auto"] = "manual"
    markets_enabled: list[Literal["US", "TSX"]] = Field(default_factory=lambda: ["US"])
    universe_finviz_filters: str = Field(DEFAULT_UNIVERSE_FILTERS, alias="universe.finviz_filters")
    universe_extra_symbols: list[str] = Field(default_factory=lambda: ["SPY"], alias="universe.extra_symbols")
    finviz_min_interval_seconds: float = Field(2.0, ge=2.0, alias="finviz.min_interval_seconds")
    finviz_cache_hours: float = Field(12.0, ge=0, alias="finviz.cache_hours")
    open_bar_lookback_sessions: int = Field(14, ge=5, le=30, alias="open_bar.lookback_sessions")


def _db_keys() -> dict[str, str]:
    """DB key -> field name."""
    return {(f.alias or name): name for name, f in RuntimeSettings.model_fields.items()}


class SettingsStore:
    def __init__(self, factory: sessionmaker[Session], now: Callable[[], datetime]) -> None:
        self._factory = factory
        self._now = now

    def _rows(self, session: Session) -> dict[str, Any]:
        return {row.key: row.value for row in session.execute(select(Setting)).scalars()}

    def load(self) -> RuntimeSettings:
        with self._factory() as session:
            return RuntimeSettings.model_validate(self._rows(session))

    def set(self, key: str, value: Any, actor: str) -> RuntimeSettings:
        keys = _db_keys()
        if key not in keys:
            raise KeyError(key)
        with session_scope(self._factory) as session:
            rows = self._rows(session)
            before = RuntimeSettings.model_validate(rows)
            updated = RuntimeSettings.model_validate({**rows, key: value})  # raises ValidationError
            stored = updated.model_dump(mode="json")[keys[key]]
            row = session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value=stored, updated_at=self._now(), updated_by=actor))
            else:
                row.value, row.updated_at, row.updated_by = stored, self._now(), actor
            session.add(AuditLog(
                ts=self._now(), actor=actor, action=f"settings.set:{key}",
                before={"value": before.model_dump(mode="json")[keys[key]]}, after={"value": stored},
            ))
        return updated
```

- [x] **Step 5: Run the tests, then the gate**

Run: `uv run pytest tests/test_crypto.py tests/db/test_settings_store.py -q` → `8 passed`.
Run: `uv run ruff format . && bash scripts/check.sh` → all pass.

- [x] **Step 6: Commit and push**

```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/crypto.py Trader/app/trader/settings_store.py Trader/app/tests/test_crypto.py \
  Trader/app/tests/db/test_settings_store.py Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T3: Fernet crypto and audited runtime settings store

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

---

### Task P1-T4: Market types, clock and session calendar

**Files:**
- Create: `Trader/app/trader/market/__init__.py`, `Trader/app/trader/market/types.py`, `Trader/app/trader/market/clock.py`, `Trader/app/trader/market/calendar.py`, `Trader/app/tests/market/__init__.py`, `Trader/app/tests/market/test_clock.py`, `Trader/app/tests/market/test_calendar.py`, `Trader/app/tests/test_no_wall_clock.py`

**Interfaces:**
- Produces:
  - `trader.market.types.Candle` (frozen dataclass): `start: datetime` (UTC), `end: datetime` (UTC), `open: Decimal`, `high: Decimal`, `low: Decimal`, `close: Decimal`, `volume: int`, `vwap: Decimal | None`.
  - `trader.market.types.Interval = Literal["OneMinute", "FiveMinutes", "FifteenMinutes", "OneHour", "OneDay"]`; `INTERVAL_CODES: dict[Interval, str]` = `{"OneMinute": "1m", "FiveMinutes": "5m", ...}` (DB `interval` column values).
  - `trader.market.clock`: `ET = ZoneInfo("America/New_York")`; `Clock` protocol with `now() -> datetime` (UTC-aware); `RealClock`; `FixedClock(at: datetime)` with `advance(delta: timedelta) -> None` and `set(at: datetime) -> None` (naive datetimes raise `ValueError`); `et_date(at: datetime) -> date`.
  - `trader.market.calendar.SessionCalendar(exchange: str = "XNYS")`: `is_session(d: date) -> bool`; `session_open(d: date) -> datetime` and `session_close(d: date) -> datetime` (UTC-aware; `ValueError` if `d` isn't a session); `next_session(d: date) -> date` (first session strictly after `d`); `previous_session(d: date) -> date` (last session strictly before `d`); `sessions_before(d: date, n: int) -> list[date]` (the `n` sessions strictly before `d`, oldest first).

- [x] **Step 1: Write the failing tests**

`Trader/app/tests/market/__init__.py`: empty file.

`Trader/app/tests/market/test_clock.py`:
```python
from datetime import UTC, date, datetime, timedelta

import pytest

from trader.market.clock import FixedClock, RealClock, et_date


def test_real_clock_is_utc_aware() -> None:
    assert RealClock().now().tzinfo is not None


def test_fixed_clock_advance_and_set() -> None:
    c = FixedClock(datetime(2026, 9, 28, 13, 35, tzinfo=UTC))
    c.advance(timedelta(seconds=5))
    assert c.now() == datetime(2026, 9, 28, 13, 35, 5, tzinfo=UTC)
    c.set(datetime(2026, 9, 29, 0, 0, tzinfo=UTC))
    assert c.now().day == 29


def test_fixed_clock_rejects_naive() -> None:
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 9, 28, 13, 35))


def test_et_date_crosses_midnight_utc() -> None:
    # 01:00 UTC on the 29th is still the 28th in New York
    assert et_date(datetime(2026, 9, 29, 1, 0, tzinfo=UTC)) == date(2026, 9, 28)
```

`Trader/app/tests/market/test_calendar.py`:
```python
from datetime import UTC, date, datetime

import pytest

from trader.market.calendar import SessionCalendar

CAL = SessionCalendar()


def test_regular_session_times_utc_during_dst() -> None:
    assert CAL.session_open(date(2026, 10, 30)) == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert CAL.session_close(date(2026, 10, 30)) == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def test_session_times_after_dst_ends() -> None:
    assert CAL.session_open(date(2026, 11, 2)) == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def test_holiday_is_not_a_session() -> None:
    assert not CAL.is_session(date(2026, 11, 26))  # Thanksgiving
    assert not CAL.is_session(date(2026, 9, 26))  # Saturday
    with pytest.raises(ValueError):
        CAL.session_open(date(2026, 11, 26))


def test_early_close_day() -> None:
    # Day after Thanksgiving closes at 13:00 ET = 18:00 UTC
    assert CAL.session_close(date(2026, 11, 27)) == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_next_and_previous_session_skip_weekends_and_holidays() -> None:
    assert CAL.next_session(date(2026, 9, 25)) == date(2026, 9, 28)  # Fri -> Mon
    assert CAL.next_session(date(2026, 11, 25)) == date(2026, 11, 27)  # skips Thanksgiving
    assert CAL.next_session(date(2026, 9, 26)) == date(2026, 9, 28)  # from a Saturday
    assert CAL.previous_session(date(2026, 9, 28)) == date(2026, 9, 25)


def test_sessions_before() -> None:
    got = CAL.sessions_before(date(2026, 9, 28), 3)
    assert got == [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
```

`Trader/app/tests/test_no_wall_clock.py`:
```python
"""Global constraint: only trader/market/clock.py may read the wall clock."""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "trader"
PATTERN = re.compile(r"datetime\.now\(|date\.today\(|datetime\.utcnow\(")


def test_no_direct_wall_clock_reads() -> None:
    offenders = [
        str(p.relative_to(PKG))
        for p in PKG.rglob("*.py")
        if p.name != "clock.py" and PATTERN.search(p.read_text())
    ]
    assert offenders == []
```

- [x] **Step 2: Run to see them fail**

Run: `uv run pytest tests/market tests/test_no_wall_clock.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.market'`.

- [x] **Step 3: Implement `trader/market/types.py`, `clock.py`, `calendar.py`**

`Trader/app/trader/market/__init__.py`: empty file.

`Trader/app/trader/market/types.py`:
```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

Interval = Literal["OneMinute", "FiveMinutes", "FifteenMinutes", "OneHour", "OneDay"]
INTERVAL_CODES: dict[Interval, str] = {
    "OneMinute": "1m",
    "FiveMinutes": "5m",
    "FifteenMinutes": "15m",
    "OneHour": "1h",
    "OneDay": "1d",
}


@dataclass(frozen=True, slots=True)
class Candle:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None
```

`Trader/app/trader/market/clock.py`:
```python
"""The only module allowed to read the wall clock (Global Constraints)."""

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock tests and replay can move by hand."""

    def __init__(self, at: datetime) -> None:
        self.set(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._at = at.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self._at += delta


def et_date(at: datetime) -> date:
    return at.astimezone(ET).date()
```

`Trader/app/trader/market/calendar.py`:
```python
"""Exchange sessions, holidays and early closes (SPEC §2: exchange_calendars)."""

from datetime import UTC, date, datetime

import exchange_calendars as xcals
import pandas as pd


class SessionCalendar:
    def __init__(self, exchange: str = "XNYS") -> None:
        self._cal = xcals.get_calendar(exchange, start="2020-01-01")

    def is_session(self, d: date) -> bool:
        return bool(self._cal.is_session(pd.Timestamp(d)))

    def _require(self, d: date) -> pd.Timestamp:
        if not self.is_session(d):
            raise ValueError(f"{d} is not a trading session")
        return pd.Timestamp(d)

    def session_open(self, d: date) -> datetime:
        ts: pd.Timestamp = self._cal.session_open(self._require(d))
        return ts.to_pydatetime().astimezone(UTC)

    def session_close(self, d: date) -> datetime:
        ts: pd.Timestamp = self._cal.session_close(self._require(d))
        return ts.to_pydatetime().astimezone(UTC)

    def next_session(self, d: date) -> date:
        ts = pd.Timestamp(d)
        nxt = self._cal.next_session(ts) if self.is_session(d) else self._cal.date_to_session(ts, "next")
        return nxt.date()  # type: ignore[no-any-return]

    def previous_session(self, d: date) -> date:
        ts = pd.Timestamp(d)
        prev = self._cal.previous_session(ts) if self.is_session(d) else self._cal.date_to_session(ts, "previous")
        return prev.date()  # type: ignore[no-any-return]

    def sessions_before(self, d: date, n: int) -> list[date]:
        last = self.previous_session(d)
        window = self._cal.sessions_window(pd.Timestamp(last), -n)
        return [ts.date() for ts in window]
```

- [x] **Step 4: Run the tests, then the gate**

Run: `uv run pytest tests/market tests/test_no_wall_clock.py -q` → `11 passed`.
Run: `uv run ruff format . && bash scripts/check.sh` → all pass. If mypy complains about pandas return types in `calendar.py`, add a precise `# type: ignore[<code>]` on that line only; do not loosen the global config.

- [x] **Step 5: Commit and push**

```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/market Trader/app/tests/market Trader/app/tests/test_no_wall_clock.py \
  Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T4: Candle type, clock and NYSE session calendar

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

---

### Task P1-T5: FinViz parser and scraper

**Files:**
- Create: `Trader/app/trader/adapters/__init__.py`, `Trader/app/trader/adapters/finviz/__init__.py`, `Trader/app/trader/adapters/finviz/parser.py`, `Trader/app/trader/adapters/finviz/scraper.py`, `Trader/app/tests/adapters/__init__.py`, `Trader/app/tests/adapters/test_finviz_parser.py`, `Trader/app/tests/adapters/test_finviz_scraper.py`
- Copy: `Trader/spikes/fixtures/finviz/raw_screener_p1.html`, `raw_quote_AAPL.html`, `raw_quote_AMD.html` → `Trader/app/tests/fixtures/finviz/`

**Interfaces:**
- Produces (`trader.adapters.finviz.parser`):
  - `ScreenerPage` (frozen dataclass): `total: int`, `header: list[str]`, `rows: list[dict[str, str]]`.
  - `UniverseRow` (frozen dataclass): `ticker: str` (Questrade form), `company: str`, `sector: str`, `industry: str`, `price: Decimal | None`, `volume: int | None`.
  - `Headline` (frozen dataclass): `ts: datetime` (UTC-aware; FinViz times are ET), `title: str`, `source: str`, `url: str`.
  - `parse_screener(html: str) -> ScreenerPage`; `parse_universe_row(rec: dict[str, str]) -> UniverseRow`; `parse_news(html: str, today_et: date) -> list[Headline]`; `blocked_reason(status: int, body: str) -> str | None`; `to_questrade_ticker(ticker: str) -> str`.
- Produces (`trader.adapters.finviz.scraper`):
  - Exceptions `FinvizError`, `FinvizBlocked(FinvizError)`, `FinvizFilterIgnored(FinvizError)`.
  - `FinvizScraper(http: httpx.Client | None = None, *, min_interval_s: float = 2.0, cache_dir: Path | None = None, cache_ttl_s: float = 43200, sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time)` with `screen(filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage` (all pages merged), `universe(filters: str) -> list[UniverseRow]`, `news(ticker: str, today_et: date) -> list[Headline]`, `close() -> None`.

- [x] **Step 1: Copy the fixtures**

Run (from `Trader/`):
```bash
mkdir -p app/tests/fixtures/finviz
cp spikes/fixtures/finviz/raw_screener_p1.html spikes/fixtures/finviz/raw_quote_AAPL.html \
   spikes/fixtures/finviz/raw_quote_AMD.html app/tests/fixtures/finviz/
```

- [x] **Step 2: Write the failing parser tests**

`Trader/app/tests/adapters/__init__.py`: empty file.

`Trader/app/tests/adapters/test_finviz_parser.py`:
```python
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trader.adapters.finviz.parser import (
    blocked_reason,
    parse_news,
    parse_screener,
    parse_universe_row,
    to_questrade_ticker,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "finviz"


def test_screener_fixture_total_header_and_first_row() -> None:
    page = parse_screener((FIX / "raw_screener_p1.html").read_text())
    assert page.total == 695
    assert page.header == ["No.", "Ticker", "Company", "Sector", "Industry", "Country",
                           "Market Cap", "P/E", "Price", "Change %", "Volume"]
    assert len(page.rows) == 20
    assert page.rows[0] == {
        "No.": "1", "Ticker": "AA", "Company": "Alcoa Corp", "Sector": "Basic Materials",
        "Industry": "Aluminum", "Country": "USA", "Market Cap": "11.31B", "P/E": "8.79",
        "Price": "42.85", "Change %": "0.40%", "Volume": "3,401,413",
    }


def test_universe_row_typed() -> None:
    page = parse_screener((FIX / "raw_screener_p1.html").read_text())
    row = parse_universe_row(page.rows[0])
    assert row.ticker == "AA"
    assert row.price == Decimal("42.85")
    assert row.volume == 3401413


def test_universe_row_missing_values() -> None:
    row = parse_universe_row({"Ticker": "BF-B", "Company": "Brown-Forman", "Sector": "", "Industry": "",
                              "Price": "-", "Volume": ""})
    assert row.ticker == "BF.B"
    assert row.price is None and row.volume is None


def test_news_dates_carry_forward_and_convert_to_utc() -> None:
    items = parse_news((FIX / "raw_quote_AAPL.html").read_text(), today_et=date(2026, 9, 26))
    assert len(items) >= 10
    first, second, third = items[:3]
    assert first.title == "China, U.S. agree to $30 billion tariff cut, launch AI dialogue"
    assert first.source == "Investing.com"
    assert first.ts == datetime(2026, 9, 26, 10, 7, tzinfo=UTC)  # 06:07 ET "Today"
    assert second.ts == datetime(2026, 9, 25, 20, 18, tzinfo=UTC)  # "Sep-25-26 04:18PM"
    assert third.ts == datetime(2026, 9, 25, 20, 2, tzinfo=UTC)  # "04:02PM", date carried forward
    assert third.url.startswith("https://")


def test_news_without_table_is_empty() -> None:
    assert parse_news("<html><body>nothing</body></html>", today_et=date(2026, 9, 26)) == []


def test_empty_body_is_blocked() -> None:
    assert blocked_reason(200, "") is not None
    assert blocked_reason(403, "x" * 5000) == "HTTP 403"
    assert blocked_reason(200, "<title>Just a moment...</title>" + "x" * 5000) is not None
    assert blocked_reason(200, (FIX / "raw_screener_p1.html").read_text()) is None


def test_ticker_mapping() -> None:
    assert to_questrade_ticker("BF-B") == "BF.B"
    assert to_questrade_ticker("AAPL") == "AAPL"
```

- [x] **Step 3: Run to see them fail**

Run: `uv run pytest tests/adapters/test_finviz_parser.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.adapters'`.

- [x] **Step 4: Implement the parser**

`Trader/app/trader/adapters/__init__.py` and `Trader/app/trader/adapters/finviz/__init__.py`: empty files.

`Trader/app/trader/adapters/finviz/parser.py`:
```python
"""All FinViz HTML parsing lives here (SPEC §4.2 isolation). Tested against saved pages.

Page markers relied on (spike S5): table.screener_table with a <th> header row; the ticker in
td[data-boxover-ticker]; ".count-text" containing "#1 / N Total"; table#news-table rows whose
first cell is "Sep-25-26 04:18PM", "Today 06:07AM" or just "04:02PM"; a.tab-link-news headlines;
the source in a span inside div.news-link-right.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from selectolax.parser import HTMLParser, Node

from trader.market.clock import ET

BASE = "https://finviz.com"
_TOTAL_RE = re.compile(r"/\s*([\d,]+)\s*Total")
_DATE_RE = re.compile(r"^(?:(Today)|([A-Z][a-z]{2}-\d{2}-\d{2}))?\s*(\d{1,2}:\d{2}[AP]M)$")
_BLOCK_MARKERS = ("just a moment", "cf-challenge", "captcha", "attention required")


@dataclass(frozen=True, slots=True)
class ScreenerPage:
    total: int
    header: list[str]
    rows: list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class UniverseRow:
    ticker: str
    company: str
    sector: str
    industry: str
    price: Decimal | None
    volume: int | None


@dataclass(frozen=True, slots=True)
class Headline:
    ts: datetime
    title: str
    source: str
    url: str


def to_questrade_ticker(ticker: str) -> str:
    """FinViz writes share classes with '-', Questrade with '.' (spike S4: BF-B -> BF.B)."""
    return ticker.strip().replace("-", ".")


def blocked_reason(status: int, body: str) -> str | None:
    if status in (403, 429, 503):
        return f"HTTP {status}"
    if len(body) < 1000:
        return f"empty body ({len(body)} bytes)"
    head = body[:5000].lower()
    for marker in _BLOCK_MARKERS:
        if marker in head:
            return f"interstitial: {marker}"
    return None


def _cell_text(node: Node) -> str:
    return " ".join(node.text(separator=" ").split())


def parse_screener(html: str) -> ScreenerPage:
    tree = HTMLParser(html)
    total = 0
    for node in tree.css(".count-text"):
        m = _TOTAL_RE.search(node.text())
        if m:
            total = int(m.group(1).replace(",", ""))
            break
    table = tree.css_first("table.screener_table")
    if table is None:
        return ScreenerPage(total, [], [])
    trs = table.css("tr")
    header = [_cell_text(th) for th in trs[0].css("th")] if trs else []
    rows: list[dict[str, str]] = []
    for tr in trs[1:]:
        tds = tr.css("td")
        if len(tds) != len(header):
            continue
        rec: dict[str, str] = {}
        for name, td in zip(header, tds, strict=True):
            if name == "Ticker":
                rec[name] = (td.attributes.get("data-boxover-ticker") or _cell_text(td)).strip()
            else:
                rec[name] = _cell_text(td)
        rows.append(rec)
    return ScreenerPage(total, header, rows)


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "")) if text not in ("", "-") else None
    except InvalidOperation:
        return None


def parse_universe_row(rec: dict[str, str]) -> UniverseRow:
    volume = _decimal(rec.get("Volume", ""))
    return UniverseRow(
        ticker=to_questrade_ticker(rec["Ticker"]),
        company=rec.get("Company", ""),
        sector=rec.get("Sector", ""),
        industry=rec.get("Industry", ""),
        price=_decimal(rec.get("Price", "")),
        volume=int(volume) if volume is not None else None,
    )


def parse_news(html: str, today_et: date) -> list[Headline]:
    """FinViz shows the date only on each day's first row; later rows carry it forward."""
    table = HTMLParser(html).css_first("table#news-table")
    if table is None:
        return []
    out: list[Headline] = []
    current: date | None = None
    for tr in table.css("tr"):
        tds = tr.css("td")
        link = tr.css_first("a.tab-link-news")
        if len(tds) < 2 or link is None:
            continue
        m = _DATE_RE.match(_cell_text(tds[0]))
        if m is None:
            continue
        if m.group(1):
            current = today_et
        elif m.group(2):
            current = datetime.strptime(m.group(2), "%b-%d-%y").date()  # noqa: DTZ007
        if current is None:
            continue
        local = datetime.combine(current, datetime.strptime(m.group(3), "%I:%M%p").time(), tzinfo=ET)  # noqa: DTZ007
        src = tr.css_first("div.news-link-right span")
        href = link.attributes.get("href") or ""
        out.append(Headline(
            ts=local.astimezone(UTC),
            title=_cell_text(link),
            source=_cell_text(src).strip("()") if src else "",
            url=BASE + href if href.startswith("/") else href,
        ))
    return out
```

- [x] **Step 5: Run the parser tests**

Run: `uv run pytest tests/adapters/test_finviz_parser.py -q`
Expected: `7 passed`. If a value differs from the spike's output (for example `_cell_text` joining nested text differently), fix the parser, not the expected value: the expected values come from the working spike parser.

- [x] **Step 6: Commit the parser**

```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/adapters Trader/app/tests/adapters/__init__.py Trader/app/tests/adapters/test_finviz_parser.py \
  Trader/app/tests/fixtures/finviz Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T5: FinViz parser (screener, universe rows, news, block detection)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

- [x] **Step 7: Write the failing scraper tests**

`Trader/app/tests/adapters/test_finviz_scraper.py`:
```python
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from trader.adapters.finviz.scraper import FinvizBlocked, FinvizFilterIgnored, FinvizScraper


def screener_html(total: int, tickers: list[str]) -> str:
    rows = "".join(
        f'<tr><td>{i}</td><td data-boxover-ticker="{t}">{t}</td><td>{t} Inc</td><td>Tech</td>'
        f"<td>Software</td><td>USA</td><td>1B</td><td>10</td><td>20.00</td><td>1.00%</td><td>2,000,000</td></tr>"
        for i, t in enumerate(tickers, start=1)
    )
    head = "".join(f"<th>{h}</th>" for h in ["No.", "Ticker", "Company", "Sector", "Industry", "Country",
                                               "Market Cap", "P/E", "Price", "Change %", "Volume"])
    pad = "<!--" + "x" * 2000 + "-->"
    return (f'<html><body>{pad}<div class="count-text">#1 / {total} Total</div>'
            f'<table class="screener_table"><tr>{head}</tr>{rows}</table></body></html>')


class Timer:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def scraper(timer: Timer, tmp: Path | None = None) -> FinvizScraper:
    return FinvizScraper(min_interval_s=2.0, cache_dir=tmp, sleep=timer.sleep, monotonic=timer.monotonic,
                         wall=lambda: 1_000_000.0)


@respx.mock
def test_pages_through_all_results_politely() -> None:
    tickers = [f"T{i}" for i in range(25)]
    unfiltered = respx.get("https://finviz.com/screener.ashx", params={"v": "111", "f": "", "r": "1"})
    unfiltered.mock(return_value=httpx.Response(200, text=screener_html(9000, ["Z"])))
    p1 = respx.get("https://finviz.com/screener.ashx", params={"v": "111", "f": "geo_usa", "r": "1"})
    p1.mock(return_value=httpx.Response(200, text=screener_html(25, tickers[:20])))
    p2 = respx.get("https://finviz.com/screener.ashx", params={"v": "111", "f": "geo_usa", "r": "21"})
    p2.mock(return_value=httpx.Response(200, text=screener_html(25, tickers[20:])))
    timer = Timer()
    rows = scraper(timer).universe("geo_usa")
    assert [r.ticker for r in rows] == tickers
    assert all(s >= 1.99 for s in timer.sleeps)  # ≥ 2 s between requests after the first
    assert len(timer.sleeps) == 2


@respx.mock
def test_filter_ignored_raises() -> None:
    respx.get("https://finviz.com/screener.ashx").mock(
        return_value=httpx.Response(200, text=screener_html(9000, ["A"]))
    )
    with pytest.raises(FinvizFilterIgnored):
        scraper(Timer()).screen("bogus_filter_code")


@respx.mock
def test_block_page_raises() -> None:
    respx.get("https://finviz.com/screener.ashx").mock(return_value=httpx.Response(403, text="denied"))
    with pytest.raises(FinvizBlocked):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_sends_browser_user_agent() -> None:
    route = respx.get("https://finviz.com/quote.ashx", params={"t": "AAPL"}).mock(
        return_value=httpx.Response(200, text=(Path(__file__).parents[1] / "fixtures/finviz/raw_quote_AAPL.html")
                                    .read_text())
    )
    items = scraper(Timer()).news("AAPL", today_et=date(2026, 9, 26))
    assert items
    assert "Mozilla/5.0" in route.calls.last.request.headers["User-Agent"]


@respx.mock
def test_cache_avoids_second_request(tmp_path: Path) -> None:
    html = (Path(__file__).parents[1] / "fixtures/finviz/raw_quote_AMD.html").read_text()
    route = respx.get("https://finviz.com/quote.ashx", params={"t": "AMD"}).mock(
        return_value=httpx.Response(200, text=html)
    )
    s = scraper(Timer(), tmp_path)
    s.news("AMD", today_et=date(2026, 9, 26))
    s.news("AMD", today_et=date(2026, 9, 26))
    assert route.call_count == 1
```

- [x] **Step 8: Run to see them fail**

Run: `uv run pytest tests/adapters/test_finviz_scraper.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.adapters.finviz.scraper'`.

- [x] **Step 9: Implement the scraper**

`Trader/app/trader/adapters/finviz/scraper.py`:
```python
"""Polite FinViz client (SPEC §4.2): ≥ 2 s between requests, browser User-Agent, 12-hour cache."""

import hashlib
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx

from trader.adapters.finviz.parser import (
    BASE,
    Headline,
    ScreenerPage,
    UniverseRow,
    blocked_reason,
    parse_news,
    parse_screener,
    parse_universe_row,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
PAGE_SIZE = 20
MAX_PAGES = 100


class FinvizError(Exception):
    pass


class FinvizBlocked(FinvizError):
    pass


class FinvizFilterIgnored(FinvizError):
    """FinViz silently ignores unknown filter codes and returns everything (spike S5)."""


class FinvizScraper:
    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        min_interval_s: float = 2.0,
        cache_dir: Path | None = None,
        cache_ttl_s: float = 43200,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._http = http or httpx.Client(timeout=20, follow_redirects=True)
        self._http.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._min_interval = min_interval_s
        self._cache_dir = cache_dir
        self._cache_ttl = cache_ttl_s
        self._sleep, self._monotonic, self._wall = sleep, monotonic, wall
        self._last: float | None = None
        self._unfiltered_totals: dict[int, int] = {}

    def close(self) -> None:
        self._http.close()

    def _cache_path(self, url: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".html")

    def _get(self, path: str, params: dict[str, str]) -> str:
        url = str(httpx.URL(BASE + path, params=params))
        cached = self._cache_path(url)
        if cached and cached.exists() and self._wall() - cached.stat().st_mtime < self._cache_ttl:
            return cached.read_text()
        if self._last is not None:
            wait = self._min_interval - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        response = self._http.get(BASE + path, params=params)
        self._last = self._monotonic()
        reason = blocked_reason(response.status_code, response.text)
        if reason:
            raise FinvizBlocked(f"{path}?{params}: {reason}")
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(response.text)
        return response.text

    def _page(self, filters: str, view: int, start: int, signal: str | None) -> ScreenerPage:
        params = {"v": str(view), "f": filters, "r": str(start)}
        if signal:
            params["s"] = signal
        return parse_screener(self._get("/screener.ashx", params))

    def _unfiltered_total(self, view: int) -> int:
        if view not in self._unfiltered_totals:
            self._unfiltered_totals[view] = self._page("", view, 1, None).total
        return self._unfiltered_totals[view]

    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage:
        first = self._page(filters, view, 1, signal)
        if filters and first.total == self._unfiltered_total(view):
            raise FinvizFilterIgnored(f"filters {filters!r} returned the whole market ({first.total})")
        rows = list(first.rows)
        start = 1 + PAGE_SIZE
        while len(rows) < first.total and start <= PAGE_SIZE * MAX_PAGES:
            page = self._page(filters, view, start, signal)
            if not page.rows:
                break
            rows.extend(page.rows)
            start += PAGE_SIZE
        return ScreenerPage(first.total, first.header, rows)

    def universe(self, filters: str) -> list[UniverseRow]:
        return [parse_universe_row(r) for r in self.screen(filters).rows]

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        return parse_news(self._get("/quote.ashx", {"t": ticker}), today_et)
```

Request order in `test_pages_through_all_results_politely`: page 1, then the unfiltered total (the ignored-filter guard), then page 2. That's three requests and two waits, which is what the test asserts.

- [x] **Step 10: Run the tests, then the gate**

Run: `uv run pytest tests/adapters -q` → `12 passed`.
Run: `uv run ruff format . && bash scripts/check.sh` → all pass.

- [x] **Step 11: Commit and push**

```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/adapters/finviz/scraper.py Trader/app/tests/adapters/test_finviz_scraper.py \
  Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T5: polite FinViz scraper with paging, cache, block and ignored-filter detection

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

---

### Task P1-T6: Questrade auth (token owner), bootstrap, seed and keep-alive CLI

**Files:**
- Create: `Trader/app/trader/adapters/questrade/__init__.py`, `Trader/app/trader/adapters/questrade/auth.py`, `Trader/app/trader/bootstrap.py`, `Trader/app/tests/adapters/test_questrade_auth.py`
- Modify: `Trader/app/trader/cli.py` (add `questrade-seed`, `token-refresh`)

**Interfaces:**
- Consumes: `ApiCredential` (T2), `session_scope` (T2), `Crypto` (T3), `Clock`/`FixedClock` (T4), `db_factory` fixture.
- Produces (`trader.adapters.questrade.auth`):
  - `TOKEN_URL = "https://login.questrade.com/oauth2/token"`, `EXPIRY_SKEW = timedelta(seconds=120)`, `FORCED_REFRESH_COOLDOWN = timedelta(seconds=90)`, `FAILED_REFRESH_COOLDOWN = timedelta(seconds=60)`, `PROVIDER = "questrade"`.
  - `QuestradeAuthError(Exception)`.
  - `AccessToken` (frozen dataclass): `token: str`, `api_base: str` (always ends `/v1/`), `expires_at: datetime`.
  - `TokenHealth` (frozen dataclass): `seeded: bool`, `expires_at: datetime | None`, `last_refresh_at: datetime | None`, `last_error: str | None`.
  - `TokenSource` protocol: `access() -> AccessToken`, `force_refresh() -> AccessToken`.
  - `QuestradeAuth(factory: sessionmaker[Session], crypto: Crypto, clock: Clock, http: httpx.Client | None = None)` implementing `TokenSource`, plus `seed(refresh_token: str) -> None`, `keep_alive(min_age: timedelta = timedelta(hours=1)) -> AccessToken`, `health() -> TokenHealth`.
- Produces (`trader.bootstrap`): `Core` dataclass (`env`, `engine`, `factory`, `crypto`, `clock`, `calendar`, `settings: SettingsStore`) and `build_core(env: EnvSettings | None = None) -> Core`.
- CLI: `trader questrade-seed` (reads `QUESTRADE_REFRESH_TOKEN`), `trader token-refresh`.

- [ ] **Step 1: Write the failing tests**

`Trader/app/tests/adapters/test_questrade_auth.py`:
```python
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import TOKEN_URL, QuestradeAuth, QuestradeAuthError
from trader.crypto import Crypto
from trader.db.models import ApiCredential
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
CRYPTO = Crypto(Fernet.generate_key().decode())


def ok(n: int) -> httpx.Response:
    return httpx.Response(200, json={
        "access_token": f"access-{n}", "refresh_token": f"refresh-{n}", "token_type": "Bearer",
        "expires_in": 1800, "api_server": "https://api05.iq.questrade.com/",
    })


def make(factory: sessionmaker[Session], clock: FixedClock) -> QuestradeAuth:
    auth = QuestradeAuth(factory, CRYPTO, clock)
    auth.seed("refresh-0")
    return auth


def stored_refresh(factory: sessionmaker[Session]) -> str | None:
    with factory() as s:
        row = s.get(ApiCredential, "questrade")
        assert row is not None
        return CRYPTO.decrypt(row.refresh_token_enc)


@respx.mock
def test_first_access_exchanges_and_stores_rotated_token(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(return_value=ok(1))
    auth = make(db_factory, FixedClock(T0))
    tok = auth.access()
    assert tok.token == "access-1"
    assert tok.api_base == "https://api05.iq.questrade.com/v1/"
    assert route.calls.last.request.url.params["refresh_token"] == "refresh-0"
    assert stored_refresh(db_factory) == "refresh-1"


@respx.mock
def test_fresh_token_is_reused(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    clock.advance(timedelta(minutes=20))
    assert auth.access().token == "access-1"
    assert route.call_count == 1


@respx.mock
def test_refreshes_inside_expiry_skew(db_factory: sessionmaker[Session]) -> None:
    respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    clock.advance(timedelta(seconds=1800 - 119))
    assert auth.access().token == "access-2"
    assert stored_refresh(db_factory) == "refresh-2"


@respx.mock
def test_dead_token_error_is_stored_and_throttled(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(return_value=httpx.Response(400, text="bad"))
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    with pytest.raises(QuestradeAuthError, match="already been used or has expired"):
        auth.access()
    clock.advance(timedelta(seconds=30))
    with pytest.raises(QuestradeAuthError):
        auth.access()
    assert route.call_count == 1  # second call throttled by FAILED_REFRESH_COOLDOWN
    assert auth.health().last_error is not None


@respx.mock
def test_force_refresh_respects_cooldown(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.access()
    clock.advance(timedelta(seconds=60))
    assert auth.force_refresh().token == "access-1"  # within 90 s: no exchange
    clock.advance(timedelta(seconds=31))
    assert auth.force_refresh().token == "access-2"
    assert route.call_count == 2


@respx.mock
def test_seed_clears_previous_error(db_factory: sessionmaker[Session]) -> None:
    respx.get(TOKEN_URL).mock(side_effect=[httpx.Response(400), ok(5)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    with pytest.raises(QuestradeAuthError):
        auth.access()
    auth.seed("refresh-new")
    assert auth.access().token == "access-5"


@respx.mock
def test_keep_alive_exchanges_only_when_older_than_min_age(db_factory: sessionmaker[Session]) -> None:
    route = respx.get(TOKEN_URL).mock(side_effect=[ok(1), ok(2)])
    clock = FixedClock(T0)
    auth = make(db_factory, clock)
    auth.keep_alive()
    clock.advance(timedelta(minutes=30))
    auth.keep_alive()
    assert route.call_count == 1
    clock.advance(timedelta(minutes=31))
    auth.keep_alive()
    assert route.call_count == 2


@respx.mock
def test_concurrent_refresh_exchanges_once(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 1: two processes race on an expired token; exactly one exchange happens."""

    def slow_ok(request: httpx.Request) -> httpx.Response:
        time.sleep(0.3)
        return ok(1)

    route = respx.get(TOKEN_URL).mock(side_effect=slow_ok)
    clock = FixedClock(T0)
    make(db_factory, clock)
    results: list[str] = []

    def worker() -> None:
        results.append(QuestradeAuth(db_factory, CRYPTO, clock).access().token)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["access-1", "access-1"]
    assert route.call_count == 1
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/adapters/test_questrade_auth.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.adapters.questrade'`.

- [ ] **Step 3: Implement `trader/adapters/questrade/auth.py`**

`Trader/app/trader/adapters/questrade/__init__.py`: empty file.

```python
"""Questrade token owner (SPEC §4.1), ported from FinanceTracker backend/app/core/questrade.py.

Exchanging a refresh token returns a NEW one and invalidates the old. api, worker and cron all
share this token, so every exchange happens under a row lock, re-checks freshness under the lock,
and commits the rotated token before anyone uses the access token.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.crypto import Crypto
from trader.db.models import ApiCredential
from trader.market.clock import Clock

log = structlog.get_logger("questrade.auth")

TOKEN_URL = "https://login.questrade.com/oauth2/token"
TIMEOUT = 20.0
EXPIRY_SKEW = timedelta(seconds=120)
FORCED_REFRESH_COOLDOWN = timedelta(seconds=90)
FAILED_REFRESH_COOLDOWN = timedelta(seconds=60)
PROVIDER = "questrade"


class QuestradeAuthError(Exception):
    """The connection is unusable; the message is fit to show Stephen."""


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    api_base: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenHealth:
    seeded: bool
    expires_at: datetime | None
    last_refresh_at: datetime | None
    last_error: str | None


class TokenSource(Protocol):
    def access(self) -> AccessToken: ...
    def force_refresh(self) -> AccessToken: ...


def api_base(api_server: str | None) -> str:
    base = (api_server or "").rstrip("/")
    if not base:
        raise QuestradeAuthError("Questrade did not return an API server URL.")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/"


class QuestradeAuth:
    def __init__(
        self, factory: sessionmaker[Session], crypto: Crypto, clock: Clock, http: httpx.Client | None = None
    ) -> None:
        self._factory = factory
        self._crypto = crypto
        self._clock = clock
        self._http = http or httpx.Client(timeout=TIMEOUT)

    # --- public -------------------------------------------------------------------
    def seed(self, refresh_token: str) -> None:
        now = self._clock.now()
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER) or ApiCredential(provider=PROVIDER)
            row.refresh_token_enc = self._crypto.encrypt(refresh_token.strip())
            row.access_token_enc = None
            row.api_server = None
            row.expires_at = None
            row.last_error = None
            row.updated_at = now
            s.merge(row)
            s.commit()

    def access(self) -> AccessToken:
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER)
            if row is not None and self._is_fresh(row):
                return self._token(row)
        return self._refresh(forced=False)

    def force_refresh(self) -> AccessToken:
        return self._refresh(forced=True)

    def keep_alive(self, min_age: timedelta = timedelta(hours=1)) -> AccessToken:
        """Daily job: extend the refresh-token chain unless it was extended recently."""
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER)
            if row is not None and row.last_refresh_at is not None and self._is_fresh(row) \
                    and self._clock.now() - row.last_refresh_at < min_age:
                return self._token(row)
        return self._refresh(forced=True, cooldown=timedelta(0))

    def health(self) -> TokenHealth:
        with self._factory() as s:
            row = s.get(ApiCredential, PROVIDER)
            if row is None:
                return TokenHealth(False, None, None, None)
            return TokenHealth(bool(row.refresh_token_enc), row.expires_at, row.last_refresh_at, row.last_error)

    # --- internals ------------------------------------------------------------------
    def _is_fresh(self, row: ApiCredential) -> bool:
        return bool(
            self._crypto.decrypt(row.access_token_enc)
            and row.api_server
            and row.expires_at is not None
            and row.expires_at - EXPIRY_SKEW > self._clock.now()
        )

    def _token(self, row: ApiCredential) -> AccessToken:
        token = self._crypto.decrypt(row.access_token_enc)
        if not token or row.expires_at is None:
            raise QuestradeAuthError("No usable access token stored.")
        return AccessToken(token, api_base(row.api_server), row.expires_at)

    def _refresh(self, forced: bool, cooldown: timedelta = FORCED_REFRESH_COOLDOWN) -> AccessToken:
        now = self._clock.now()
        with self._factory() as s:
            # populate_existing is load-bearing: without it a process that waited on the lock
            # would wake up holding the token the first one just spent.
            row = s.execute(
                select(ApiCredential)
                .where(ApiCredential.provider == PROVIDER)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if row is None:
                raise QuestradeAuthError("Questrade isn't set up. Paste a refresh token in Settings.")
            if not forced and self._is_fresh(row):
                return self._token(row)
            if forced and row.last_refresh_at is not None and now - row.last_refresh_at < cooldown \
                    and self._is_fresh(row):
                return self._token(row)
            if row.last_error and row.updated_at and now - row.updated_at < FAILED_REFRESH_COOLDOWN:
                raise QuestradeAuthError(row.last_error)
            refresh = self._crypto.decrypt(row.refresh_token_enc)
            if not refresh:
                raise QuestradeAuthError("No usable refresh token stored. Paste a new one from Questrade.")
            try:
                resp = self._http.get(TOKEN_URL, params={"grant_type": "refresh_token", "refresh_token": refresh})
            except httpx.HTTPError as exc:
                raise QuestradeAuthError(f"Could not reach Questrade: {exc}") from exc
            if resp.status_code != 200:
                detail = ("the refresh token has already been used or has expired"
                          if resp.status_code == 400 else f"HTTP {resp.status_code}")
                row.last_error = f"Token refresh failed ({detail}). Generate a new manual token in Questrade."
                row.updated_at = now
                s.commit()
                raise QuestradeAuthError(row.last_error)
            data = resp.json()
            if not data.get("refresh_token") or not data.get("access_token"):
                raise QuestradeAuthError("Questrade's response was missing a token.")
            row.refresh_token_enc = self._crypto.encrypt(data["refresh_token"])
            row.access_token_enc = self._crypto.encrypt(data["access_token"])
            row.api_server = data.get("api_server")
            row.expires_at = now + timedelta(seconds=int(data.get("expires_in", 1800)))
            row.last_refresh_at = now
            row.last_error = None
            row.updated_at = now
            try:
                s.commit()
            except Exception as exc:
                s.rollback()
                log.critical("questrade_rotated_token_not_saved", error=str(exc))
                raise QuestradeAuthError(
                    "The token was refreshed but couldn't be saved, so the connection is broken. "
                    "Paste a new manual token."
                ) from exc
            return self._token(row)
```

- [ ] **Step 4: Run the auth tests**

Run: `uv run pytest tests/adapters/test_questrade_auth.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Implement `trader/bootstrap.py` and the CLI commands**

`Trader/app/trader/bootstrap.py`:
```python
"""Wires the long-lived objects from the environment. Every process builds one Core."""

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trader.config import EnvSettings, get_env
from trader.crypto import Crypto
from trader.db.session import make_engine, make_session_factory
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, RealClock
from trader.settings_store import SettingsStore


@dataclass
class Core:
    env: EnvSettings
    engine: Engine
    factory: sessionmaker[Session]
    crypto: Crypto
    clock: Clock
    calendar: SessionCalendar
    settings: SettingsStore


def build_core(env: EnvSettings | None = None) -> Core:
    env = env or get_env()
    engine = make_engine(env.database_url.get_secret_value())
    factory = make_session_factory(engine)
    clock = RealClock()
    return Core(
        env=env,
        engine=engine,
        factory=factory,
        crypto=Crypto(env.app_encryption_key.get_secret_value()),
        clock=clock,
        calendar=SessionCalendar(),
        settings=SettingsStore(factory, now=clock.now),
    )
```

Add to `Trader/app/trader/cli.py` (below `version`):
```python
@app.command("questrade-seed")
def questrade_seed() -> None:
    """Store QUESTRADE_REFRESH_TOKEN (from the environment) as the start of the token chain."""
    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.bootstrap import build_core

    core = build_core()
    if core.env.questrade_refresh_token is None:
        typer.echo("QUESTRADE_REFRESH_TOKEN is not set", err=True)
        raise typer.Exit(1)
    QuestradeAuth(core.factory, core.crypto, core.clock).seed(core.env.questrade_refresh_token.get_secret_value())
    typer.echo("seeded")


@app.command("token-refresh")
def token_refresh() -> None:
    """Keep the Questrade refresh-token chain alive (daily job, SPEC §9)."""
    from trader.adapters.questrade.auth import QuestradeAuth, QuestradeAuthError
    from trader.bootstrap import build_core

    core = build_core()
    try:
        token = QuestradeAuth(core.factory, core.crypto, core.clock).keep_alive()
    except QuestradeAuthError as exc:
        typer.echo(f"token refresh failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"ok; access token valid until {token.expires_at.isoformat()}")
```

- [ ] **Step 6: Run the gate, commit and push**

Run: `uv run ruff format . && bash scripts/check.sh` → all pass.
```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/adapters/questrade Trader/app/trader/bootstrap.py Trader/app/trader/cli.py \
  Trader/app/tests/adapters/test_questrade_auth.py Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T6: Questrade token owner with row-locked rotation, bootstrap, seed and keep-alive CLI

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

- [ ] **Step 7: LIVE: hand the token chain from `.env.dev` to the database**

This is the one-time ownership transfer in the Global Constraints. Do these three commands in order, without running anything else that uses Questrade in between:
```bash
bash scripts/trader-dev.sh questrade-seed      # expected: seeded
bash scripts/trader-dev.sh token-refresh       # expected: ok; access token valid until ...
python3 - <<'PY'
import re, os, tempfile
p = "../docker/.env.dev"
t = open(p).read()
t = re.sub(r"^# Questrade Trader-dev refresh token, rotated.*\n", "", t, flags=re.M)
t = re.sub(r"^QUESTRADE_REFRESH_TOKEN=\S+\n", "", t, flags=re.M)
t += "# Questrade token chain is now owned by trader_dev.trader.api_credentials (P1-T6). Don't add it back.\n"
fd, tmp = tempfile.mkstemp(dir="../docker", prefix=".env.dev.")
with os.fdopen(fd, "w") as f: f.write(t)
os.chmod(tmp, 0o600); os.replace(tmp, p)
print("removed QUESTRADE_REFRESH_TOKEN from .env.dev")
PY
```
If `token-refresh` fails with "already been used or has expired", stop and escalate: Stephen must generate a new manual token for the Trader-dev app, which goes into `.env.dev` as `QUESTRADE_REFRESH_TOKEN` before repeating this step.

Append to the activity log that the chain is now owned by the database and `spikes/qt.py` must not be run again.

---

### Task P1-T7: Questrade data client and `questrade-check` CLI

**Files:**
- Create: `Trader/app/trader/adapters/questrade/models.py`, `Trader/app/trader/adapters/questrade/client.py`, `Trader/app/tests/adapters/test_questrade_client.py`
- Modify: `Trader/app/trader/cli.py` (add `questrade-check`)

**Interfaces:**
- Consumes: `TokenSource`, `AccessToken`, `QuestradeAuth` (T6); `Candle`, `Interval` (T4); `Clock` (T4).
- Produces (`trader.adapters.questrade.models`): frozen dataclasses `QtSymbol(symbol_id: int, symbol: str, listing_exchange: str, currency: str, description: str, is_tradable: bool, is_quotable: bool)`, `QtQuote(symbol_id: int, symbol: str, bid: Decimal | None, ask: Decimal | None, last: Decimal | None, last_regular: Decimal | None, volume: int, last_trade_time: datetime | None, delay: int, is_halted: bool, vwap: Decimal | None)`, `CandleRequest(symbol_id: int, start: datetime, end: datetime, interval: Interval)`.
- Produces (`trader.adapters.questrade.client`): `INTRADAY_HISTORY = timedelta(days=88)`; `QuestradeApiError(Exception)` with `.status: int`; `TokenBucket(rate: float, monotonic=time.monotonic, sleep=asyncio.sleep)` with `async acquire()`; `QuestradeClient(tokens: TokenSource, clock: Clock, *, market_rps: float = 20.0, account_rps: float = 30.0, http: httpx.AsyncClient | None = None, sleep=asyncio.sleep)`, an async context manager, with `async server_time() -> datetime`, `async symbols_by_names(names: Sequence[str]) -> dict[str, QtSymbol]`, `async quotes(ids: Sequence[int]) -> list[QtQuote]`, `async candles(symbol_id, start, end, interval) -> list[Candle]`, `async candles_many(reqs: Sequence[CandleRequest]) -> dict[CandleRequest, list[Candle] | QuestradeApiError]`, and attribute `rate_limit_remaining: dict[str, int]` (keys `"market"`, `"account"`).
- CLI: `trader questrade-check` prints server time, SPY's quote with `delay` and `lastTradeTime`, and remaining rate limits (used for S2 on Monday).

- [ ] **Step 1: Write the failing tests**

`Trader/app/tests/adapters/test_questrade_client.py`:
```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import QuestradeApiError, QuestradeClient, TokenBucket
from trader.adapters.questrade.models import CandleRequest
from trader.market.clock import FixedClock

BASE = "https://api05.iq.questrade.com/v1/"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class FakeTokens:
    def __init__(self) -> None:
        self.n = 1
        self.forced = 0

    def access(self) -> AccessToken:
        return AccessToken(f"tok-{self.n}", BASE, NOW + timedelta(minutes=30))

    def force_refresh(self) -> AccessToken:
        self.forced += 1
        self.n += 1
        return self.access()


async def no_sleep(_: float) -> None:
    return None


def client(tokens: FakeTokens | None = None) -> QuestradeClient:
    return QuestradeClient(tokens or FakeTokens(), FixedClock(NOW), sleep=no_sleep)


def candle_json(start: str, o: float, v: int) -> dict[str, object]:
    return {"start": start, "end": start, "low": o - 1, "high": o + 1, "open": o, "close": o + 0.5,
            "volume": v, "VWAP": o + 0.25}


@respx.mock
async def test_symbols_by_names_chunks_and_skips_unknown() -> None:
    names = [f"S{i}" for i in range(150)] + ["BAD"]

    def handler(request: httpx.Request) -> httpx.Response:
        asked = request.url.params["names"].split(",")
        if "BAD" in asked and len(asked) > 1:
            return httpx.Response(400, json={"code": 1002, "message": "Invalid or malformed argument"})
        if asked == ["BAD"]:
            return httpx.Response(400, json={"code": 1002, "message": "Invalid or malformed argument"})
        return httpx.Response(200, json={"symbols": [
            {"symbol": n, "symbolId": i + 1, "listingExchange": "NASDAQ", "currency": "USD",
             "description": n, "isTradable": True, "isQuotable": True} for i, n in enumerate(asked)]})

    route = respx.get(BASE + "symbols").mock(side_effect=handler)
    async with client() as c:
        got = await c.symbols_by_names(names)
    assert set(got) == set(names) - {"BAD"}
    assert all(len(call.request.url.params["names"].split(",")) <= 100 for call in route.calls)


@respx.mock
async def test_quotes_parse_decimals_and_delay() -> None:
    respx.get(BASE + "markets/quotes").mock(return_value=httpx.Response(200, json={"quotes": [{
        "symbol": "SPY", "symbolId": 34987, "bidPrice": None, "askPrice": None, "lastTradePrice": 771.35,
        "lastTradePriceTrHrs": 771.35, "volume": 57884, "lastTradeTime": "2026-09-25T00:00:00.000000-04:00",
        "delay": 0, "isHalted": False, "VWAP": 770.1}]}))
    async with client() as c:
        (q,) = await c.quotes([34987])
    assert q.last == Decimal("771.35") and q.bid is None and q.delay == 0
    assert q.last_trade_time == datetime(2026, 9, 25, 4, 0, tzinfo=UTC)


@respx.mock
async def test_candles_parse_to_utc_decimal() -> None:
    respx.get(BASE + "markets/candles/8049").mock(return_value=httpx.Response(200, json={"candles": [
        candle_json("2026-09-25T09:30:00.000000-04:00", 336.04, 403790)]}))
    start = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    async with client() as c:
        (bar,) = await c.candles(8049, start, start + timedelta(minutes=5), "FiveMinutes")
    assert bar.start == start
    assert bar.open == Decimal("336.04") and bar.volume == 403790


@respx.mock
async def test_intraday_start_is_clamped_to_available_history() -> None:
    route = respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": []}))
    async with client() as c:
        await c.candles(1, NOW - timedelta(days=400), NOW, "OneMinute")
        await c.candles(1, NOW - timedelta(days=400), NOW, "OneDay")
    intraday_start = datetime.fromisoformat(route.calls[0].request.url.params["startTime"])
    daily_start = datetime.fromisoformat(route.calls[1].request.url.params["startTime"])
    assert intraday_start >= NOW - timedelta(days=88, seconds=1)
    assert daily_start == NOW - timedelta(days=400)


@respx.mock
async def test_window_entirely_before_history_returns_empty_without_request() -> None:
    route = respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": []}))
    async with client() as c:
        got = await c.candles(1, NOW - timedelta(days=200), NOW - timedelta(days=150), "FiveMinutes")
    assert got == [] and route.call_count == 0


@respx.mock
async def test_401_forces_one_refresh_then_succeeds() -> None:
    tokens = FakeTokens()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer tok-1":
            return httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})
        return httpx.Response(200, json={"time": "2026-09-27T08:00:00.000000-04:00"})

    respx.get(BASE + "time").mock(side_effect=handler)
    async with client(tokens) as c:
        t = await c.server_time()
    assert tokens.forced == 1
    assert t == datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@respx.mock
async def test_429_then_success_and_rate_limit_recorded() -> None:
    respx.get(BASE + "markets/quotes").mock(side_effect=[
        httpx.Response(429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}),
        httpx.Response(200, headers={"X-RateLimit-Remaining": "14990"}, json={"quotes": []}),
    ])
    async with client() as c:
        assert await c.quotes([1]) == []
        assert c.rate_limit_remaining["market"] == 14990


@respx.mock
async def test_candles_many_reports_missing() -> None:
    """Review Focus 4: a symbol with no bar and a symbol that errors don't break the scan."""
    respx.get(BASE + "markets/candles/1").mock(return_value=httpx.Response(200, json={"candles": [
        candle_json("2026-09-25T09:30:00.000000-04:00", 10.0, 100)]}))
    respx.get(BASE + "markets/candles/2").mock(return_value=httpx.Response(200, json={"candles": []}))
    respx.get(BASE + "markets/candles/3").mock(return_value=httpx.Response(400, json={"code": 1002}))
    start = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    reqs = [CandleRequest(i, start, start + timedelta(minutes=5), "FiveMinutes") for i in (1, 2, 3)]
    async with client() as c:
        got = await c.candles_many(reqs)
    assert len(got[reqs[0]]) == 1  # type: ignore[arg-type]
    assert got[reqs[1]] == []
    assert isinstance(got[reqs[2]], QuestradeApiError) and got[reqs[2]].status == 400


async def test_token_bucket_spaces_requests() -> None:
    t = {"now": 0.0}
    waits: list[float] = []

    async def fake_sleep(s: float) -> None:
        waits.append(s)
        t["now"] += s

    bucket = TokenBucket(20.0, monotonic=lambda: t["now"], sleep=fake_sleep)
    for _ in range(3):
        await bucket.acquire()
    assert waits == pytest.approx([0.05, 0.05])
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/adapters/test_questrade_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.adapters.questrade.client'`.

- [ ] **Step 3: Implement the models**

`Trader/app/trader/adapters/questrade/models.py`:
```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trader.market.types import Interval


@dataclass(frozen=True, slots=True)
class QtSymbol:
    symbol_id: int
    symbol: str
    listing_exchange: str
    currency: str
    description: str
    is_tradable: bool
    is_quotable: bool


@dataclass(frozen=True, slots=True)
class QtQuote:
    symbol_id: int
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    last_regular: Decimal | None
    volume: int
    last_trade_time: datetime | None
    delay: int
    is_halted: bool
    vwap: Decimal | None


@dataclass(frozen=True, slots=True)
class CandleRequest:
    symbol_id: int
    start: datetime
    end: datetime
    interval: Interval
```

- [ ] **Step 4: Implement the client**

`Trader/app/trader/adapters/questrade/client.py`:
```python
"""Async, rate-limited Questrade market-data client (SPEC §4.1; spikes S1–S4).

Findings built in: at most 100 names per symbols call; intraday history only ~3 months, and windows
entirely before it return HTTP 400, so start times are clamped; candles include 04:00–20:00 ET
(filtering to regular hours is the caller's job, see trader.market.indicators.regular_hours).
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Self

import httpx

from trader.adapters.questrade.auth import TokenSource
from trader.adapters.questrade.models import CandleRequest, QtQuote, QtSymbol
from trader.market.clock import Clock
from trader.market.types import Candle, Interval

INTRADAY_HISTORY = timedelta(days=88)
NAMES_PER_CALL = 100
MAX_ATTEMPTS = 5
Category = Literal["market", "account"]


class QuestradeApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class TokenBucket:
    """Spaces calls evenly at `rate` per second."""

    def __init__(
        self,
        rate: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._monotonic = monotonic
        self._sleep = sleep
        self._next: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._monotonic()
            slot = now if self._next is None else max(now, self._next)
            self._next = slot + self._interval
        if slot > now:
            await self._sleep(slot - now)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class QuestradeClient:
    def __init__(
        self,
        tokens: TokenSource,
        clock: Clock,
        *,
        market_rps: float = 20.0,
        account_rps: float = 30.0,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._tokens = tokens
        self._clock = clock
        self._http = http or httpx.AsyncClient(timeout=30)
        self._sleep = sleep
        self._buckets: dict[Category, TokenBucket] = {
            "market": TokenBucket(market_rps, sleep=sleep),
            "account": TokenBucket(account_rps, sleep=sleep),
        }
        self.rate_limit_remaining: dict[str, int] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, str], category: Category) -> Any:
        refreshed = False
        last_status, last_text = 0, ""
        for attempt in range(MAX_ATTEMPTS):
            await self._buckets[category].acquire()
            token = await asyncio.to_thread(self._tokens.access)
            resp = await self._http.get(
                token.api_base + path, params=params, headers={"Authorization": f"Bearer {token.token}"}
            )
            if "X-RateLimit-Remaining" in resp.headers:
                self.rate_limit_remaining[category] = int(resp.headers["X-RateLimit-Remaining"])
            last_status, last_text = resp.status_code, resp.text[:300]
            if resp.status_code == 200:
                return json.loads(resp.text, parse_float=Decimal)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                await asyncio.to_thread(self._tokens.force_refresh)
                continue
            if resp.status_code == 429:
                reset = float(resp.headers.get("X-RateLimit-Reset", "0"))
                await self._sleep(min(5.0, max(0.5, reset - self._clock.now().timestamp())))
                continue
            if resp.status_code >= 500:
                await self._sleep(0.5 * 2**attempt)
                continue
            raise QuestradeApiError(resp.status_code, last_text)
        raise QuestradeApiError(last_status, last_text)

    async def server_time(self) -> datetime:
        data = await self._get("time", {}, "account")
        value = _dt(data["time"])
        assert value is not None
        return value

    async def _symbols_call(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        data = await self._get("symbols", {"names": ",".join(names)}, "market")
        return {
            s["symbol"]: QtSymbol(
                symbol_id=int(s["symbolId"]), symbol=s["symbol"], listing_exchange=s.get("listingExchange", ""),
                currency=s.get("currency", ""), description=s.get("description", ""),
                is_tradable=bool(s.get("isTradable")), is_quotable=bool(s.get("isQuotable")),
            )
            for s in data.get("symbols", [])
        }

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        """Unknown names are left out. A chunk that fails is retried name by name."""
        out: dict[str, QtSymbol] = {}
        for i in range(0, len(names), NAMES_PER_CALL):
            chunk = list(names[i : i + NAMES_PER_CALL])
            try:
                out.update(await self._symbols_call(chunk))
            except QuestradeApiError as exc:
                if exc.status != 400:
                    raise
                for name in chunk:
                    try:
                        out.update(await self._symbols_call([name]))
                    except QuestradeApiError as one:
                        if one.status != 400:
                            raise
        return out

    async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
        out: list[QtQuote] = []
        for i in range(0, len(ids), NAMES_PER_CALL):
            chunk = ids[i : i + NAMES_PER_CALL]
            data = await self._get("markets/quotes", {"ids": ",".join(str(x) for x in chunk)}, "market")
            out.extend(
                QtQuote(
                    symbol_id=int(q["symbolId"]), symbol=q["symbol"], bid=_dec(q.get("bidPrice")),
                    ask=_dec(q.get("askPrice")), last=_dec(q.get("lastTradePrice")),
                    last_regular=_dec(q.get("lastTradePriceTrHrs")), volume=int(q.get("volume") or 0),
                    last_trade_time=_dt(q.get("lastTradeTime")), delay=int(q.get("delay") or 0),
                    is_halted=bool(q.get("isHalted")), vwap=_dec(q.get("VWAP")),
                )
                for q in data.get("quotes", [])
            )
        return out

    async def candles(self, symbol_id: int, start: datetime, end: datetime, interval: Interval) -> list[Candle]:
        if interval != "OneDay":
            start = max(start, self._clock.now() - INTRADAY_HISTORY)
        if start >= end:
            return []
        data = await self._get(
            f"markets/candles/{symbol_id}",
            {"startTime": start.isoformat(), "endTime": end.isoformat(), "interval": interval},
            "market",
        )
        out = []
        for c in data.get("candles", []):
            s, e = _dt(c["start"]), _dt(c["end"])
            assert s is not None and e is not None
            out.append(Candle(start=s, end=e, open=Decimal(c["open"]), high=Decimal(c["high"]),
                              low=Decimal(c["low"]), close=Decimal(c["close"]), volume=int(c["volume"]),
                              vwap=_dec(c.get("VWAP"))))
        return out

    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        async def one(r: CandleRequest) -> list[Candle] | QuestradeApiError:
            try:
                return await self.candles(r.symbol_id, r.start, r.end, r.interval)
            except QuestradeApiError as exc:
                return exc

        results = await asyncio.gather(*(one(r) for r in reqs))
        return dict(zip(reqs, results, strict=True))
```

Note: `json.loads(..., parse_float=Decimal)` keeps prices exact; `Decimal(c["open"])` then works whether the value arrived as an int or a Decimal.

- [ ] **Step 5: Run the client tests**

Run: `uv run pytest tests/adapters/test_questrade_client.py -q`
Expected: `9 passed`.

- [ ] **Step 6: Add `questrade-check` to the CLI**

Add to `Trader/app/trader/cli.py`:
```python
@app.command("questrade-check")
def questrade_check(symbol: str = "SPY") -> None:
    """Show Questrade server time, one quote's freshness, and remaining rate limits (spike S2)."""
    import asyncio

    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.adapters.questrade.client import QuestradeClient
    from trader.bootstrap import build_core

    core = build_core()
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)

    async def run() -> None:
        async with QuestradeClient(auth, core.clock) as qt:
            server = await qt.server_time()
            sym = (await qt.symbols_by_names([symbol]))[symbol]
            (quote,) = await qt.quotes([sym.symbol_id])
            now = core.clock.now()
            typer.echo(f"server time {server.isoformat()} (local clock differs by {(now - server).total_seconds():.1f}s)")
            age = (now - quote.last_trade_time).total_seconds() if quote.last_trade_time else None
            typer.echo(f"{symbol}: last={quote.last} bid={quote.bid} ask={quote.ask} delay={quote.delay} "
                       f"lastTradeTime={quote.last_trade_time} age_s={age}")
            typer.echo(f"rate limit remaining: {qt.rate_limit_remaining}")

    asyncio.run(run())
```

- [ ] **Step 7: Run the gate, commit and push**

Run: `uv run ruff format . && bash scripts/check.sh` → all pass.
```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/adapters/questrade/models.py Trader/app/trader/adapters/questrade/client.py \
  Trader/app/trader/cli.py Trader/app/tests/adapters/test_questrade_client.py \
  Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T7: async rate-limited Questrade client with history clamp and questrade-check CLI

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

- [ ] **Step 8: LIVE: smoke-test against Questrade**

Run: `bash scripts/trader-dev.sh questrade-check`
Expected: three lines (server time, an SPY quote with `delay=0`, and remaining limits). Record the output in the activity log.

---

### Task P1-T8: Indicators

**Files:**
- Create: `Trader/app/trader/market/indicators.py`, `Trader/app/tests/market/test_indicators.py`

**Interfaces:**
- Consumes: `Candle` (T4), `SessionCalendar` (T4).
- Produces (`trader.market.indicators`), all pure:
  - `regular_hours(candles: Sequence[Candle], cal: SessionCalendar, session: date) -> list[Candle]` (bars starting at or after the open and before the close).
  - `opening_bar(candles: Sequence[Candle], cal: SessionCalendar, session: date) -> Candle | None` (the bar whose start equals the session open).
  - `atr(daily: Sequence[Candle], period: int = 14) -> Decimal | None` (Wilder; needs `period + 1` candles; rounded to 4 dp).
  - `average_volume(bars: Sequence[Candle]) -> Decimal | None` (2 dp).
  - `rvol(volume: int, average: Decimal | None) -> Decimal | None` (4 dp; `None` when average is `None` or 0).
  - `is_doji(c: Candle, max_body_pct: Decimal = Decimal("0.10")) -> bool` (a zero-range bar counts as a doji).
  - `is_bearish(c: Candle) -> bool` (close < open).

- [x] **Step 1: Write the failing tests**

`Trader/app/tests/market/test_indicators.py`:
```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from trader.market.calendar import SessionCalendar
from trader.market.indicators import (
    atr,
    average_volume,
    is_bearish,
    is_doji,
    opening_bar,
    regular_hours,
    rvol,
)
from trader.market.types import Candle

D = Decimal
CAL = SessionCalendar()


def bar(start: datetime, o: str, h: str, low: str, c: str, v: int = 100, minutes: int = 5) -> Candle:
    return Candle(start, start + timedelta(minutes=minutes), D(o), D(h), D(low), D(c), v, None)


def day(n: int, h: str, low: str, c: str) -> Candle:
    start = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=n)
    return Candle(start, start + timedelta(days=1), D(c), D(h), D(low), D(c), 1000, None)


WILDER = [day(0, "10", "9", "9.5"), day(1, "11", "9.5", "10.5"), day(2, "10.8", "10", "10.2"),
          day(3, "12", "10.1", "11.8"), day(4, "12.2", "11", "11.1")]


def test_atr_first_value_is_simple_average_of_true_ranges() -> None:
    # TRs: 1.5, 0.8, 1.9 -> 1.4
    assert atr(WILDER[:4], period=3) == D("1.4000")


def test_atr_then_wilder_smoothing() -> None:
    # next TR 1.2 -> (1.4*2 + 1.2)/3 = 1.3333
    assert atr(WILDER, period=3) == D("1.3333")


def test_atr_needs_period_plus_one_candles() -> None:
    assert atr(WILDER[:3], period=3) is None


def test_regular_hours_and_opening_bar() -> None:
    session = date(2026, 9, 25)
    open_ = CAL.session_open(session)
    bars = [bar(open_ - timedelta(minutes=5), "1", "1", "1", "1"),  # 09:25 pre-market
            bar(open_, "10", "11", "9", "10.5", v=500),
            bar(CAL.session_close(session), "1", "1", "1", "1")]  # 16:00 after-hours
    rth = regular_hours(bars, CAL, session)
    assert [b.start for b in rth] == [open_]
    ob = opening_bar(bars, CAL, session)
    assert ob is not None and ob.volume == 500


def test_opening_bar_missing() -> None:
    assert opening_bar([], CAL, date(2026, 9, 25)) is None


def test_average_volume_and_rvol() -> None:
    t = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    assert average_volume([bar(t, "1", "1", "1", "1", v=100), bar(t, "1", "1", "1", "1", v=301)]) == D("200.50")
    assert average_volume([]) is None
    assert rvol(403790, D("200000")) == D("2.0190")
    assert rvol(100, None) is None
    assert rvol(100, D("0")) is None


def test_doji_and_bearish() -> None:
    t = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    assert is_doji(bar(t, "10", "11", "9", "10.2"))  # body 0.2 / range 2 = 10%
    assert not is_doji(bar(t, "10", "11", "9", "10.3"))
    assert is_doji(bar(t, "10", "10", "10", "10"))  # zero range
    assert is_bearish(bar(t, "10", "11", "9", "9.5"))
    assert not is_bearish(bar(t, "10", "11", "9", "10"))
```

- [x] **Step 2: Run to see them fail**

Run: `uv run pytest tests/market/test_indicators.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.market.indicators'`.

- [x] **Step 3: Implement `trader/market/indicators.py`**

```python
"""Pure indicator maths for the ORB strategy (SPEC §5.2). No I/O, no clock."""

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from trader.market.calendar import SessionCalendar
from trader.market.types import Candle

FOUR = Decimal("0.0001")
TWO = Decimal("0.01")


def regular_hours(candles: Sequence[Candle], cal: SessionCalendar, session: date) -> list[Candle]:
    open_, close = cal.session_open(session), cal.session_close(session)
    return [c for c in candles if open_ <= c.start < close]


def opening_bar(candles: Sequence[Candle], cal: SessionCalendar, session: date) -> Candle | None:
    open_ = cal.session_open(session)
    return next((c for c in candles if c.start == open_), None)


def atr(daily: Sequence[Candle], period: int = 14) -> Decimal | None:
    if len(daily) < period + 1:
        return None
    trs = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(daily, daily[1:], strict=False)
    ]
    value = sum(trs[:period], Decimal(0)) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value.quantize(FOUR, ROUND_HALF_UP)


def average_volume(bars: Sequence[Candle]) -> Decimal | None:
    if not bars:
        return None
    return (Decimal(sum(b.volume for b in bars)) / len(bars)).quantize(TWO, ROUND_HALF_UP)


def rvol(volume: int, average: Decimal | None) -> Decimal | None:
    if average is None or average == 0:
        return None
    return (Decimal(volume) / average).quantize(FOUR, ROUND_HALF_UP)


def is_doji(c: Candle, max_body_pct: Decimal = Decimal("0.10")) -> bool:
    rng = c.high - c.low
    if rng == 0:
        return True
    return abs(c.close - c.open) / rng <= max_body_pct


def is_bearish(c: Candle) -> bool:
    return c.close < c.open
```

- [x] **Step 4: Run the tests, then the gate**

Run: `uv run pytest tests/market/test_indicators.py -q` → `7 passed`.
Run: `uv run ruff format . && bash scripts/check.sh` → all pass.

- [x] **Step 5: Commit and push**

```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/market/indicators.py Trader/app/tests/market/test_indicators.py \
  Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T8: ATR (Wilder), regular-hours filter, opening bar, rvol, doji and bearish checks

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

---

### Task P1-T9: Job runner, market data repository, nightly job, `notify` CLI

**Files:**
- Create: `Trader/app/trader/events.py`, `Trader/app/trader/jobs/__init__.py`, `Trader/app/trader/jobs/runner.py`, `Trader/app/trader/jobs/nightly.py`, `Trader/app/trader/market/repository.py`, `Trader/app/tests/jobs/__init__.py`, `Trader/app/tests/jobs/test_runner.py`, `Trader/app/tests/jobs/test_nightly.py`
- Modify: `Trader/app/trader/cli.py` (add `nightly`, `notify`)

**Interfaces:**
- Consumes: everything above: `FinvizScraper.universe`, `UniverseRow`, `FinvizError` (T5); `QuestradeClient.symbols_by_names`, `candles_many`, `CandleRequest`, `QtSymbol`, `QuestradeApiError` (T7); `atr`, `opening_bar`, `average_volume` (T8); `SessionCalendar`, `Clock`, `et_date` (T4); `RuntimeSettings` (T3); models (T2).
- Produces:
  - `trader.events.log_event(session: Session, clock: Clock, level: str, source: str, message: str, data: dict[str, Any] | None = None, run_id: int | None = None) -> None`.
  - `trader.jobs.runner.JobOutcome(status: Literal["succeeded","failed","skipped"], detail: dict[str, Any], error: str | None)`; `run_job(factory, clock, job: str, session_date: date, fn: Callable[[], dict[str, Any]], force: bool = False) -> JobOutcome`. A succeeded run for `(job, session_date)` makes later runs `skipped` unless `force`.
  - `trader.market.repository`: `upsert_symbols(session, symbols: Iterable[QtSymbol]) -> dict[str, int]` (Questrade ticker → symbols.id); `upsert_daily_candles(session, symbol_id: int, candles: Iterable[Candle]) -> int`; `upsert_intraday_candles(session, symbol_id: int, interval_code: str, candles: Iterable[Candle]) -> int`; `save_universe_snapshot(session, session_date: date, rows: Iterable[UniverseSnapshotRow]) -> int`; `save_open_bar_stats(session, session_date: date, stats: Iterable[tuple[int, Decimal | None, Decimal | None]]) -> int`; `latest_universe_tickers(session, before: date) -> tuple[date, list[str]] | None`. `UniverseSnapshotRow(symbol_id: int, price: Decimal | None, avg_volume: int | None, atr14: Decimal | None, source: str)`.
  - `trader.jobs.nightly`: protocols `UniverseSource` (`universe(filters: str) -> list[UniverseRow]`) and `MarketData` (`symbols_by_names`, `candles_many`); `NightlyDeps(factory, clock, calendar, finviz: UniverseSource, market: MarketData, settings: RuntimeSettings)`; `target_session(calendar, clock) -> date` (the next session after today in ET); `async run_nightly(deps: NightlyDeps, session_date: date) -> dict[str, Any]`.
  - CLI: `trader nightly [--date YYYY-MM-DD] [--force]`, `trader notify TEXT`.

**Nightly semantics:** it runs at 20:00 ET and prepares the **next** session. `session_date` is that next session. The universe snapshot, `open_bar_stats` and `job_runs` rows are keyed by it. The opening-bar average uses the `open_bar.lookback_sessions` sessions strictly before it.

- [ ] **Step 1: Write the failing runner tests**

`Trader/app/tests/jobs/__init__.py`: empty file.

`Trader/app/tests/jobs/test_runner.py`:
```python
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import EventLog, JobRun
from trader.jobs.runner import run_job
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db
CLOCK = FixedClock(datetime(2026, 9, 27, 0, 0, tzinfo=UTC))
D = date(2026, 9, 28)


def test_success_is_recorded(db_factory: sessionmaker[Session]) -> None:
    out = run_job(db_factory, CLOCK, "nightly", D, lambda: {"n": 3})
    assert out.status == "succeeded" and out.detail == {"n": 3}
    with db_factory() as s:
        row = s.execute(select(JobRun)).scalar_one()
    assert row.status == "succeeded" and row.detail == {"n": 3} and row.finished_at is not None


def test_second_run_is_skipped_unless_forced(db_factory: sessionmaker[Session]) -> None:
    calls: list[int] = []

    def fn() -> dict[str, Any]:
        calls.append(1)
        return {}

    run_job(db_factory, CLOCK, "nightly", D, fn)
    assert run_job(db_factory, CLOCK, "nightly", D, fn).status == "skipped"
    assert run_job(db_factory, CLOCK, "nightly", D, fn, force=True).status == "succeeded"
    assert len(calls) == 2


def test_failure_is_recorded_and_logged(db_factory: sessionmaker[Session]) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("FinViz down")

    out = run_job(db_factory, CLOCK, "nightly", D, boom)
    assert out.status == "failed" and out.error == "RuntimeError: FinViz down"
    with db_factory() as s:
        assert s.execute(select(JobRun.status)).scalar_one() == "failed"
        assert s.execute(select(EventLog.level)).scalar_one() == "error"
    # a failed run doesn't block a retry
    assert run_job(db_factory, CLOCK, "nightly", D, lambda: {}).status == "succeeded"
```

- [ ] **Step 2: Run to see it fail**

Run: `uv run pytest tests/jobs/test_runner.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.jobs'`.

- [ ] **Step 3: Implement `trader/events.py` and `trader/jobs/runner.py`**

`Trader/app/trader/events.py`:
```python
from typing import Any

from sqlalchemy.orm import Session

from trader.db.models import EventLog
from trader.market.clock import Clock


def log_event(
    session: Session,
    clock: Clock,
    level: str,
    source: str,
    message: str,
    data: dict[str, Any] | None = None,
    run_id: int | None = None,
) -> None:
    session.add(EventLog(ts=clock.now(), level=level, source=source, run_id=run_id, message=message, data=data))
```

`Trader/app/trader/jobs/__init__.py`: empty file.

`Trader/app/trader/jobs/runner.py`:
```python
"""Runs a job once per (job, session_date) and records it in job_runs (SPEC §9)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import JobRun
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock


@dataclass(frozen=True)
class JobOutcome:
    status: Literal["succeeded", "failed", "skipped"]
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def run_job(
    factory: sessionmaker[Session],
    clock: Clock,
    job: str,
    session_date: date,
    fn: Callable[[], dict[str, Any]],
    force: bool = False,
) -> JobOutcome:
    with session_scope(factory) as s:
        done = s.execute(
            select(JobRun.id).where(JobRun.job == job, JobRun.session_date == session_date,
                                    JobRun.status == "succeeded").limit(1)
        ).scalar_one_or_none()
        if done is not None and not force:
            return JobOutcome("skipped")
        run = JobRun(job=job, session_date=session_date, started_at=clock.now(), status="running")
        s.add(run)
        s.flush()
        run_id = run.id
    try:
        detail = fn()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with session_scope(factory) as s:
            row = s.get(JobRun, run_id)
            assert row is not None
            row.status, row.finished_at, row.error = "failed", clock.now(), error
            log_event(s, clock, "error", f"job.{job}", f"{job} failed for {session_date}", {"error": error})
        return JobOutcome("failed", error=error)
    with session_scope(factory) as s:
        row = s.get(JobRun, run_id)
        assert row is not None
        row.status, row.finished_at, row.detail = "succeeded", clock.now(), detail
    return JobOutcome("succeeded", detail)
```

- [ ] **Step 4: Run the runner tests and commit**

Run: `uv run pytest tests/jobs/test_runner.py -q` → `3 passed`.
```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/events.py Trader/app/trader/jobs/__init__.py Trader/app/trader/jobs/runner.py \
  Trader/app/tests/jobs/__init__.py Trader/app/tests/jobs/test_runner.py Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T9: idempotent job runner and event log helper

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

- [ ] **Step 5: Write the failing nightly tests**

`Trader/app/tests/jobs/test_nightly.py`:
```python
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.parser import UniverseRow
from trader.adapters.finviz.scraper import FinvizBlocked
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtSymbol
from trader.db.models import DailyCandle, IntradayCandle, OpenBarStat, Symbol, UniverseSnapshot
from trader.jobs.nightly import NightlyDeps, run_nightly, target_session
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
CLOCK = FixedClock(datetime(2026, 9, 28, 0, 0, tzinfo=UTC))  # Sun 27 Sep, 20:00 ET
TARGET = date(2026, 9, 28)


class FakeFinviz:
    def __init__(self, tickers: list[str] | None, error: Exception | None = None) -> None:
        self.tickers, self.error, self.calls = tickers or [], error, 0

    def universe(self, filters: str) -> list[UniverseRow]:
        self.calls += 1
        if self.error:
            raise self.error
        return [UniverseRow(t, f"{t} Inc", "Tech", "Software", Decimal("20"), 2_000_000) for t in self.tickers]


class FakeMarket:
    """Every symbol gets 20 daily bars (TR = 1) and a 9:30 bar of volume 1000 per session."""

    def __init__(self, unknown: set[str] = frozenset(), failing: set[int] = frozenset()) -> None:
        self.unknown, self.failing = unknown, failing

    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]:
        return {n: QtSymbol(1000 + i, n, "NASDAQ", "USD", n, True, True)
                for i, n in enumerate(names) if n not in self.unknown}

    async def candles_many(self, reqs: Sequence[CandleRequest]) -> dict[CandleRequest, list[Candle] | QuestradeApiError]:
        out: dict[CandleRequest, list[Candle] | QuestradeApiError] = {}
        for r in reqs:
            if r.symbol_id in self.failing:
                out[r] = QuestradeApiError(400, "bad")
            elif r.interval == "OneDay":
                days = [r.end - timedelta(days=i) for i in range(20, 0, -1)]
                out[r] = [Candle(d, d + timedelta(days=1), Decimal("10"), Decimal("10.5"), Decimal("9.5"),
                                 Decimal("10"), 1_500_000, None) for d in days]
            else:
                sessions = CAL.sessions_before(TARGET, 14)
                out[r] = [Candle(CAL.session_open(s), CAL.session_open(s) + timedelta(minutes=5), Decimal("10"),
                                 Decimal("11"), Decimal("9"), Decimal("10.5"), 1000, None) for s in sessions]
        return out


def deps(factory: sessionmaker[Session], finviz: FakeFinviz, market: FakeMarket | None = None) -> NightlyDeps:
    return NightlyDeps(factory, CLOCK, CAL, finviz, market or FakeMarket(), RuntimeSettings())


def count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as s:
        return int(s.execute(select(func.count()).select_from(model)).scalar_one())


def test_target_session_is_next_trading_day() -> None:
    assert target_session(CAL, CLOCK) == TARGET  # Sunday evening -> Monday


async def test_nightly_builds_universe_stats_and_candles(db_factory: sessionmaker[Session]) -> None:
    detail = await run_nightly(deps(db_factory, FakeFinviz(["AAPL", "BF-B"])), TARGET)
    assert detail["source"] == "finviz"
    assert detail["universe"] == 3  # AAPL, BF.B, plus SPY
    with db_factory() as s:
        tickers = set(s.execute(select(Symbol.ticker)).scalars())
        stat = s.execute(select(OpenBarStat).join(Symbol, Symbol.id == OpenBarStat.symbol_id)
                         .where(Symbol.ticker == "AAPL")).scalar_one()
    assert tickers == {"AAPL", "BF.B", "SPY"}
    assert stat.session_date == TARGET
    assert stat.avg_open_vol_14d == Decimal("1000.00")
    assert stat.atr14 == Decimal("1.0000")
    assert count(db_factory, UniverseSnapshot) == 3
    assert count(db_factory, DailyCandle) == 60
    assert count(db_factory, IntradayCandle) == 3 * 14  # opening bars kept


async def test_nightly_is_idempotent(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 5: running twice gives the same rows, no duplicates."""
    d = deps(db_factory, FakeFinviz(["AAPL"]))
    await run_nightly(d, TARGET)
    before = [count(db_factory, m) for m in (Symbol, UniverseSnapshot, DailyCandle, IntradayCandle, OpenBarStat)]
    await run_nightly(d, TARGET)
    after = [count(db_factory, m) for m in (Symbol, UniverseSnapshot, DailyCandle, IntradayCandle, OpenBarStat)]
    assert before == after


async def test_nightly_falls_back_to_previous_universe(db_factory: sessionmaker[Session]) -> None:
    """Review Focus 3: FinViz blocked -> use the last stored universe, marked as a fallback."""
    await run_nightly(deps(db_factory, FakeFinviz(["AAPL", "MSFT"])), date(2026, 9, 25))
    detail = await run_nightly(deps(db_factory, FakeFinviz(None, FinvizBlocked("HTTP 403"))), TARGET)
    assert detail["source"] == "fallback"
    assert detail["fallback_from"] == "2026-09-25"
    with db_factory() as s:
        sources = set(s.execute(select(UniverseSnapshot.source).where(UniverseSnapshot.session_date == TARGET))
                      .scalars())
    assert sources == {"fallback"}


async def test_nightly_without_any_universe_raises(db_factory: sessionmaker[Session]) -> None:
    with pytest.raises(FinvizBlocked):
        await run_nightly(deps(db_factory, FakeFinviz(None, FinvizBlocked("HTTP 403"))), TARGET)


async def test_unknown_and_failing_symbols_are_reported_not_fatal(db_factory: sessionmaker[Session]) -> None:
    market = FakeMarket(unknown={"ZZZZ"}, failing={1000})
    detail = await run_nightly(deps(db_factory, FakeFinviz(["AAPL", "ZZZZ", "MSFT"]), market), TARGET)
    assert detail["unresolved"] == ["ZZZZ"]
    assert detail["candle_errors"] == 2  # AAPL's daily and 5-minute requests both failed
    assert detail["universe"] == 3  # AAPL, MSFT, SPY resolved
```

- [ ] **Step 6: Run to see them fail**

Run: `uv run pytest tests/jobs/test_nightly.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.jobs.nightly'`.

- [ ] **Step 7: Implement `trader/market/repository.py`**

```python
"""Idempotent writes of market data (every write is an upsert)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trader.adapters.questrade.models import QtSymbol
from trader.db.models import DailyCandle, IntradayCandle, OpenBarStat, Symbol, UniverseSnapshot
from trader.market.types import Candle


@dataclass(frozen=True, slots=True)
class UniverseSnapshotRow:
    symbol_id: int
    price: Decimal | None
    avg_volume: int | None
    atr14: Decimal | None
    source: str


def upsert_symbols(session: Session, symbols: Iterable[QtSymbol]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for sym in symbols:
        stmt = insert(Symbol).values(
            ticker=sym.symbol, exchange=sym.listing_exchange, questrade_id=sym.symbol_id,
            currency=sym.currency, name=sym.description,
        ).on_conflict_do_update(
            index_elements=[Symbol.questrade_id],
            set_={"ticker": sym.symbol, "exchange": sym.listing_exchange, "currency": sym.currency,
                  "name": sym.description},
        ).returning(Symbol.id)
        ids[sym.symbol] = int(session.execute(stmt).scalar_one())
    return ids


def _ohlcv(c: Candle) -> dict[str, object]:
    return {"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume, "vwap": c.vwap}


def upsert_daily_candles(session: Session, symbol_id: int, candles: Iterable[Candle]) -> int:
    rows = [{"symbol_id": symbol_id, "date": c.start.date(), **_ohlcv(c)} for c in candles]
    if not rows:
        return 0
    stmt = insert(DailyCandle).values(rows)
    session.execute(stmt.on_conflict_do_update(
        index_elements=[DailyCandle.symbol_id, DailyCandle.date],
        set_={k: stmt.excluded[k] for k in ("open", "high", "low", "close", "volume", "vwap")},
    ))
    return len(rows)


def upsert_intraday_candles(session: Session, symbol_id: int, interval_code: str, candles: Iterable[Candle]) -> int:
    rows = [{"symbol_id": symbol_id, "interval": interval_code, "ts": c.start, **_ohlcv(c)} for c in candles]
    if not rows:
        return 0
    stmt = insert(IntradayCandle).values(rows)
    session.execute(stmt.on_conflict_do_update(
        index_elements=[IntradayCandle.symbol_id, IntradayCandle.interval, IntradayCandle.ts],
        set_={k: stmt.excluded[k] for k in ("open", "high", "low", "close", "volume", "vwap")},
    ))
    return len(rows)


def save_universe_snapshot(session: Session, session_date: date, rows: Iterable[UniverseSnapshotRow]) -> int:
    values = [{"session_date": session_date, "symbol_id": r.symbol_id, "price": r.price,
               "avg_volume": r.avg_volume, "atr14": r.atr14, "source": r.source} for r in rows]
    if not values:
        return 0
    stmt = insert(UniverseSnapshot).values(values)
    session.execute(stmt.on_conflict_do_update(
        index_elements=[UniverseSnapshot.session_date, UniverseSnapshot.symbol_id],
        set_={k: stmt.excluded[k] for k in ("price", "avg_volume", "atr14", "source")},
    ))
    return len(values)


def save_open_bar_stats(
    session: Session, session_date: date, stats: Iterable[tuple[int, Decimal | None, Decimal | None]]
) -> int:
    values = [{"symbol_id": sid, "session_date": session_date, "avg_open_vol_14d": avg, "atr14": a}
              for sid, avg, a in stats]
    if not values:
        return 0
    stmt = insert(OpenBarStat).values(values)
    session.execute(stmt.on_conflict_do_update(
        index_elements=[OpenBarStat.symbol_id, OpenBarStat.session_date],
        set_={"avg_open_vol_14d": stmt.excluded.avg_open_vol_14d, "atr14": stmt.excluded.atr14},
    ))
    return len(values)


def latest_universe_tickers(session: Session, before: date) -> tuple[date, list[str]] | None:
    last = session.execute(
        select(func.max(UniverseSnapshot.session_date)).where(UniverseSnapshot.session_date < before)
    ).scalar_one_or_none()
    if last is None:
        return None
    tickers = session.execute(
        select(Symbol.ticker).join(UniverseSnapshot, UniverseSnapshot.symbol_id == Symbol.id)
        .where(UniverseSnapshot.session_date == last).order_by(Symbol.ticker)
    ).scalars().all()
    return last, list(tickers)
```

- [ ] **Step 8: Implement `trader/jobs/nightly.py`**

```python
"""Nightly job (SPEC §9, 20:00 ET Sun–Thu): FinViz universe → symbol IDs → daily candles and ATR14 →
opening-bar history → cache. Prepares the NEXT session."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.parser import UniverseRow
from trader.adapters.finviz.scraper import FinvizError
from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import CandleRequest, QtSymbol
from trader.db.session import session_scope
from trader.events import log_event
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.market.indicators import atr, average_volume, opening_bar
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

DAILY_LOOKBACK = timedelta(days=30)


class UniverseSource(Protocol):
    def universe(self, filters: str) -> list[UniverseRow]: ...


class MarketData(Protocol):
    async def symbols_by_names(self, names: Sequence[str]) -> dict[str, QtSymbol]: ...
    async def candles_many(
        self, reqs: Sequence[CandleRequest]
    ) -> dict[CandleRequest, list[Candle] | QuestradeApiError]: ...


@dataclass
class NightlyDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    finviz: UniverseSource
    market: MarketData
    settings: RuntimeSettings


def target_session(calendar: SessionCalendar, clock: Clock) -> date:
    return calendar.next_session(et_date(clock.now()))


async def _universe(deps: NightlyDeps, session_date: date) -> tuple[list[UniverseRow], str, str | None]:
    try:
        rows = await asyncio.to_thread(deps.finviz.universe, deps.settings.universe_finviz_filters)
        return rows, "finviz", None
    except FinvizError as exc:
        with session_scope(deps.factory) as s:
            prev = repo.latest_universe_tickers(s, before=session_date)
            if prev is None:
                log_event(s, deps.clock, "error", "job.nightly", "FinViz failed and no previous universe exists",
                          {"error": str(exc)})
                raise
            log_event(s, deps.clock, "warning", "job.nightly", "FinViz failed; using previous universe",
                      {"error": str(exc), "from": prev[0].isoformat()})
        rows = [UniverseRow(t, "", "", "", None, None) for t in prev[1]]
        return rows, "fallback", prev[0].isoformat()


async def run_nightly(deps: NightlyDeps, session_date: date) -> dict[str, Any]:
    rows, source, fallback_from = await _universe(deps, session_date)
    by_ticker = {r.ticker: r for r in rows}
    wanted = list(dict.fromkeys([*by_ticker, *deps.settings.universe_extra_symbols]))
    resolved = await deps.market.symbols_by_names(wanted)
    unresolved = sorted(set(wanted) - set(resolved))

    lookback = deps.calendar.sessions_before(session_date, deps.settings.open_bar_lookback_sessions)
    end_of_prev = datetime.combine(lookback[-1] + timedelta(days=1), time(0), tzinfo=ET)
    daily_reqs = {s.symbol_id: CandleRequest(s.symbol_id, end_of_prev - DAILY_LOOKBACK, end_of_prev, "OneDay")
                  for s in resolved.values()}
    bar_reqs = {s.symbol_id: CandleRequest(s.symbol_id, deps.calendar.session_open(lookback[0]),
                                           deps.calendar.session_close(lookback[-1]), "FiveMinutes")
                for s in resolved.values()}
    results = await deps.market.candles_many([*daily_reqs.values(), *bar_reqs.values()])
    errors = sum(isinstance(v, QuestradeApiError) for v in results.values())

    with session_scope(deps.factory) as s:
        ids = repo.upsert_symbols(s, resolved.values())
        snapshot, stats = [], []
        for ticker, sym in resolved.items():
            sid = ids[ticker]
            daily = results[daily_reqs[sym.symbol_id]]
            bars = results[bar_reqs[sym.symbol_id]]
            daily_ok = daily if isinstance(daily, list) else []
            opening = [b for d in lookback if (b := opening_bar(bars, deps.calendar, d))] \
                if isinstance(bars, list) else []
            repo.upsert_daily_candles(s, sid, daily_ok)
            repo.upsert_intraday_candles(s, sid, "5m", opening)
            atr14 = atr(daily_ok, 14)  # full ~20-session history so Wilder smoothing applies
            avg_vol = average_volume(daily_ok[-14:])
            stats.append((sid, average_volume(opening), atr14))
            row = by_ticker.get(ticker)
            snapshot.append(repo.UniverseSnapshotRow(
                symbol_id=sid, price=row.price if row else (daily_ok[-1].close if daily_ok else None),
                avg_volume=int(avg_vol) if avg_vol is not None else None, atr14=atr14, source=source,
            ))
        repo.save_universe_snapshot(s, session_date, snapshot)
        repo.save_open_bar_stats(s, session_date, stats)
        detail: dict[str, Any] = {
            "session_date": session_date.isoformat(), "source": source, "universe": len(snapshot),
            "unresolved": unresolved, "candle_errors": errors,
        }
        if fallback_from:
            detail["fallback_from"] = fallback_from
        log_event(s, deps.clock, "info", "job.nightly", f"universe ready for {session_date}", detail)
    return detail
```

- [ ] **Step 9: Run the nightly tests**

Run: `uv run pytest tests/jobs -q`
Expected: `9 passed`. If `test_nightly_builds_universe_stats_and_candles` shows a different `atr14`, check the fake: every daily bar has high−low = 1 and close = open = 10, so every true range is 1 and ATR must be exactly `1.0000`.

- [ ] **Step 10: Add `nightly` and `notify` to the CLI**

Add to `Trader/app/trader/cli.py`:
```python
@app.command()
def nightly(date_: str = typer.Option(None, "--date", help="Target session YYYY-MM-DD"),
            force: bool = typer.Option(False, "--force")) -> None:
    """Build the universe and caches for the next session (SPEC §9, 20:00 ET)."""
    import asyncio
    from datetime import date as date_cls
    from pathlib import Path

    from trader.adapters.finviz.scraper import FinvizScraper
    from trader.adapters.questrade.auth import QuestradeAuth
    from trader.adapters.questrade.client import QuestradeClient
    from trader.bootstrap import build_core
    from trader.jobs.nightly import NightlyDeps, run_nightly, target_session
    from trader.jobs.runner import run_job

    core = build_core()
    settings = core.settings.load()
    session_date = date_cls.fromisoformat(date_) if date_ else target_session(core.calendar, core.clock)
    finviz = FinvizScraper(min_interval_s=settings.finviz_min_interval_seconds,
                           cache_dir=Path("/tmp/trader-finviz-cache"),  # noqa: S108
                           cache_ttl_s=settings.finviz_cache_hours * 3600)
    auth = QuestradeAuth(core.factory, core.crypto, core.clock)

    def job() -> dict[str, object]:
        async def go() -> dict[str, object]:
            async with QuestradeClient(auth, core.clock) as qt:
                return await run_nightly(NightlyDeps(core.factory, core.clock, core.calendar, finviz, qt, settings),
                                         session_date)
        return asyncio.run(go())

    out = run_job(core.factory, core.clock, "nightly", session_date, job, force=force)
    finviz.close()
    typer.echo(f"nightly {session_date}: {out.status} {out.detail or out.error or ''}")
    if out.status == "failed":
        raise typer.Exit(1)


@app.command()
def notify(text: str) -> None:
    """Send a Telegram message to Stephen through the configured bot."""
    import httpx

    from trader.config import get_env

    env = get_env()
    if env.telegram_bot_token is None or env.telegram_chat_id is None:
        typer.echo("Telegram isn't configured", err=True)
        raise typer.Exit(1)
    r = httpx.post(f"https://api.telegram.org/bot{env.telegram_bot_token.get_secret_value()}/sendMessage",
                   data={"chat_id": env.telegram_chat_id, "text": text}, timeout=15)
    typer.echo("sent" if r.status_code == 200 else f"failed: HTTP {r.status_code}")
```

(`job()` returns `dict[str, object]`; if mypy wants `dict[str, Any]` to match `run_job`, change both annotations to `dict[str, Any]` and import `Any`.)

- [ ] **Step 11: Run the gate, commit and push**

Run: `uv run ruff format . && bash scripts/check.sh` → all pass.
```bash
cd "/Users/stephen/Documents/Code/Claude Code/Trader"
git add Trader/app/trader/market/repository.py Trader/app/trader/jobs/nightly.py Trader/app/trader/cli.py \
  Trader/app/tests/jobs/test_nightly.py Trader/docs/plans/2026-09-26-phase-1-data-layer.md
git commit -m "P1-T9: nightly job (universe, symbols, daily candles, ATR14, opening-bar stats) and notify CLI

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git pull --rebase && git push
```

- [ ] **Step 12: LIVE: run the nightly job against `trader_dev`**

Run: `bash scripts/trader-dev.sh nightly`
Expected, within about 3 minutes (FinViz ~28 pages at 2 s each, then ~1,100 Questrade calls at 20/s): `nightly <next session>: succeeded {... 'source': 'finviz', 'universe': ~543, 'unresolved': [...], 'candle_errors': 0 ...}`. Then run it again and expect `skipped`.

Record the detail in the activity log. `unresolved` should be a handful at most; if it's more than 5% of the universe, escalate with the list.

Then: `bash scripts/trader-dev.sh notify "Phase 1 nightly job ran against trader_dev"` → `sent`.

---

## Self-review (done by the plan author)

- **Spec coverage (Phase 1 scope):** BR-01 → T6, T7; BR-02 → T5, T9; BR-04 data part (opening-bar averages, ATR) → T8, T9; SPEC §4.1 auth rules → T6 (lock, re-check, skew, both cooldowns, commit-before-use); §4.1 endpoints → T7; §4.2 politeness, isolation, fallback → T5, T9 (manual CSV upload is Phase 4, web app); §9 nightly and token-refresh commands → T6, T9 (cron itself is Phase 3); §10 Phase 1 tables → T2; §13 env → T1.
- **Placeholders:** none. The one conditional note (mypy annotations in T9 Step 10) states the exact fix.
- **Type consistency:** `CandleRequest`, `QtSymbol`, `Candle`, `UniverseRow`, `RuntimeSettings` field names and `INTERVAL_CODES` values (`"5m"`) are used identically across T4, T5, T7, T9.
- **Review Focus tests:** 1 → T6 `test_concurrent_refresh_exchanges_once`; 2 → T4 `test_early_close_day`, `test_holiday_is_not_a_session`; 3 → T5 `test_filter_ignored_raises`, `test_empty_body_is_blocked`, T9 `test_nightly_falls_back_to_previous_universe`; 4 → T7 `test_candles_many_reports_missing`; 5 → T9 `test_nightly_is_idempotent`.
