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
