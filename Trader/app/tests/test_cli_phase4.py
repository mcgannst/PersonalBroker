"""P4-T18 acceptance tests 5 and 6 (CLI side): `trader create-admin`, `trader user-password`, and the FinViz
cache directory the `nightly` and `premarket` commands give their scraper.

`create-admin` (the container entrypoint runs it on every start) prints `created`, `exists`, `not configured`
(exit 0) or `rejected` (exit 1), never the password. `user-password` resets the single user's password from a
hidden prompt entered twice, signs out every session, and audits `auth.password_reset` with actor `cli`.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
from tests.fakes_api import test_core
from trader.api import auth
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
INITIAL = "initial-admin-password-24"  # noqa: S105 (a throwaway test password)
NEW = "brand-new-password-1"  # noqa: S105
runner = CliRunner()


def use_core(monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session], **env: Any) -> Core:
    core = test_core(factory, FixedClock(NOW), **env)
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    return core


def last_line(output: str) -> str:
    """The command's result line (structlog's console lines come before it in tests)."""
    return output.strip().splitlines()[-1]


def users(factory: sessionmaker[Session]) -> list[m.User]:
    with factory() as s:
        return list(s.execute(select(m.User)).scalars())


# --- 5. create-admin ----------------------------------------------------------------------------------------


def test_create_admin_creates_then_exists(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_core(monkeypatch, db_factory, admin_username="stephen", admin_password_initial=SecretStr(INITIAL))
    first = runner.invoke(app, ["create-admin"])
    assert first.exit_code == 0, first.output
    assert last_line(first.output) == "created"
    second = runner.invoke(app, ["create-admin"])
    assert second.exit_code == 0 and last_line(second.output) == "exists"
    assert [u.username for u in users(db_factory)] == ["stephen"]
    assert INITIAL not in first.output + second.output


def test_create_admin_without_env_is_not_configured_and_exit_0(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_core(monkeypatch, db_factory)
    result = runner.invoke(app, ["create-admin"])
    assert result.exit_code == 0 and last_line(result.output) == "not configured"
    assert users(db_factory) == []


def test_create_admin_with_a_short_password_is_rejected_exit_1(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_core(monkeypatch, db_factory, admin_username="stephen", admin_password_initial=SecretStr("seven77"))
    result = runner.invoke(app, ["create-admin"])
    assert result.exit_code == 1
    assert "rejected" in result.output and "seven77" not in result.output
    assert users(db_factory) == []


def test_create_admin_with_a_bad_username_is_rejected_exit_1(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_core(monkeypatch, db_factory, admin_username="Stephen!", admin_password_initial=SecretStr(INITIAL))
    result = runner.invoke(app, ["create-admin"])
    assert result.exit_code == 1 and "rejected" in result.output
    assert users(db_factory) == []


# --- user-password ------------------------------------------------------------------------------------------


def _admin_with_sessions(factory: sessionmaker[Session], n: int) -> int:
    clock = FixedClock(NOW)
    assert auth.ensure_admin(factory, clock, "stephen", SecretStr(INITIAL)) == "created"
    with factory() as s:
        user = s.execute(select(m.User)).scalar_one()
        user.failed_logins = 4
        for i in range(n):
            s.add(
                m.WebSession(
                    user_id=user.id,
                    token_hash=f"{i:064d}",
                    csrf_token="c" * 32,
                    created_at=NOW,
                    last_seen_at=NOW,
                    expires_at=datetime(2026, 11, 1, tzinfo=UTC),
                )
            )
        s.commit()
        return user.id


def test_user_password_resets_the_password_and_revokes_every_session(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_core(monkeypatch, db_factory)
    user_id = _admin_with_sessions(db_factory, 3)
    result = runner.invoke(app, ["user-password"], input=f"{NEW}\n{NEW}\n")
    assert result.exit_code == 0, result.output
    assert NEW not in result.output and INITIAL not in result.output
    assert "3 sessions signed out" in result.output
    with db_factory() as s:
        user = s.get(m.User, user_id)
        assert user is not None
        assert auth.verify_password(user.password_hash, NEW)[0]
        assert not auth.verify_password(user.password_hash, INITIAL)[0]
        assert user.failed_logins == 0 and user.locked_until is None
        live = s.execute(select(m.WebSession).where(m.WebSession.revoked_at.is_(None))).scalars().all()
        assert live == []
        audit = s.execute(select(m.AuditLog).where(m.AuditLog.action == "auth.password_reset")).scalar_one()
    assert audit.actor == "cli"
    assert NEW not in str(audit.before) + str(audit.after)


def test_user_password_refuses_seven_characters_and_changes_nothing(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_core(monkeypatch, db_factory)
    _admin_with_sessions(db_factory, 1)
    result = runner.invoke(app, ["user-password"], input="seven77\nseven77\n")
    assert result.exit_code == 1
    assert "at least 8 characters" in result.output and "seven77" not in result.output
    with db_factory() as s:
        user = s.execute(select(m.User)).scalar_one()
        assert auth.verify_password(user.password_hash, INITIAL)[0]
        assert s.execute(select(m.WebSession).where(m.WebSession.revoked_at.is_(None))).first() is not None


def test_user_password_with_mismatched_entries_changes_nothing(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_core(monkeypatch, db_factory)
    _admin_with_sessions(db_factory, 1)
    result = runner.invoke(app, ["user-password"], input=f"{NEW}\nsomething-else-2\n")
    assert result.exit_code != 0
    assert NEW not in result.output
    with db_factory() as s:
        user = s.execute(select(m.User)).scalar_one()
        assert auth.verify_password(user.password_hash, INITIAL)[0]


def test_user_password_without_a_user_says_so(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_core(monkeypatch, db_factory)
    result = runner.invoke(app, ["user-password"], input=f"{NEW}\n{NEW}\n")
    assert result.exit_code == 1 and "create-admin" in result.output


# --- 6. the FinViz cache directory of nightly and premarket -------------------------------------------------


class _Stop(Exception):
    pass


def _record_scraper(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    import trader.adapters.finviz.scraper as scraper

    seen: list[Path] = []

    class Recording:
        def __init__(self, *args: Any, cache_dir: Path, **kwargs: Any) -> None:
            seen.append(Path(cache_dir))
            raise _Stop

    monkeypatch.setattr(scraper, "FinvizScraper", Recording)
    return seen


@pytest.mark.parametrize("command", ["nightly", "premarket"])
def test_nightly_and_premarket_use_the_configured_finviz_cache(
    command: str, db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_core(monkeypatch, db_factory)
    monkeypatch.setenv("TRADER_FINVIZ_CACHE_DIR", str(tmp_path / "finviz-cache"))
    seen = _record_scraper(monkeypatch)
    result = runner.invoke(app, [command, "--date", "2026-10-07", "--force"])
    assert isinstance(result.exception, _Stop), result.output
    assert seen == [tmp_path / "finviz-cache"]
