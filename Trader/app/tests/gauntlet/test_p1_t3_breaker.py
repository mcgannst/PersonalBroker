"""P1-T3 Breaker: try to break trader.crypto and trader.settings_store.

DB tests use the throwaway testcontainers database (db_factory), never trader_dev.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trader.crypto import Crypto
from trader.db.models import AuditLog, Setting
from trader.settings_store import RuntimeSettings, SettingsStore

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _store(factory: sessionmaker[Session]) -> SettingsStore:
    return SettingsStore(factory, now=lambda: NOW)


def _counts(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as s:
        settings = s.execute(select(func.count()).select_from(Setting)).scalar_one()
        audits = s.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    return settings, audits


# ---------------------------------------------------------------- settings store


@pytest.mark.db
def test_set_by_python_field_name_is_rejected(db_factory: sessionmaker[Session]) -> None:
    """The DB key is the alias. Setting by the field name must not create a second, shadow row
    (which load() would then read through populate_by_name, giving two keys for one setting)."""
    st = _store(db_factory)
    for field_name, value in [
        ("finviz_min_interval_seconds", 5.0),
        ("open_bar_lookback_sessions", 10),
        ("universe_extra_symbols", ["SPY", "QQQ"]),
    ]:
        with pytest.raises(KeyError):
            st.set(field_name, value, actor="breaker")
    assert _counts(db_factory) == (0, 0)
    assert st.load() == RuntimeSettings()


@pytest.mark.db
def test_concurrent_set_on_different_keys_loses_no_update(db_factory: sessionmaker[Session]) -> None:
    """api, worker and Telegram may change different settings at the same moment."""
    st = _store(db_factory)
    changes: dict[str, Any] = {
        "approval_mode": "auto",
        "markets_enabled": ["TSX", "US"],
        "universe.finviz_filters": "ind_stocksonly,geo_usa",
        "universe.extra_symbols": ["SPY", "QQQ"],
        "finviz.min_interval_seconds": 3.5,
        "finviz.cache_hours": 6.0,
        "open_bar.lookback_sessions": 20,
    }
    barrier = threading.Barrier(len(changes))

    def worker(item: tuple[str, Any]) -> None:
        key, value = item
        barrier.wait(timeout=30)
        st.set(key, value, actor="breaker")

    with ThreadPoolExecutor(max_workers=len(changes)) as pool:
        list(pool.map(worker, changes.items()))

    loaded = st.load().model_dump(by_alias=True, mode="json")
    for key, value in changes.items():
        assert loaded[key] == value, key
    assert _counts(db_factory) == (len(changes), len(changes))


@pytest.mark.db
def test_list_values_round_trip_order_and_duplicates(db_factory: sessionmaker[Session]) -> None:
    st = _store(db_factory)
    st.set("markets_enabled", ["TSX", "US"], actor="breaker")
    st.set("universe.extra_symbols", ["QQQ", "SPY", "IWM"], actor="breaker")
    loaded = st.load()
    assert loaded.markets_enabled == ["TSX", "US"]
    assert loaded.universe_extra_symbols == ["QQQ", "SPY", "IWM"]

    # A duplicated market would make every per-market job run twice for that market
    # (Review Focus 5). It must be rejected or collapsed, never stored as-is.
    try:
        result = st.set("markets_enabled", ["US", "US"], actor="breaker")
    except ValidationError:
        assert st.load().markets_enabled == ["TSX", "US"]
    else:
        assert result.markets_enabled == ["US"]
        assert st.load().markets_enabled == ["US"]


@pytest.mark.db
def test_corrupt_stored_value_fails_closed_and_is_repairable(db_factory: sessionmaker[Session]) -> None:
    """A row edited by hand to an invalid value. Behaviour pinned: load() raises (fail closed,
    never a silent default such as approval_mode flipping), and set() on that key repairs it."""
    with db_factory() as s:
        s.add(Setting(key="approval_mode", value="yolo", updated_by="psql"))
        s.add(Setting(key="finviz.min_interval_seconds", value=0.1, updated_by="psql"))
        s.commit()
    st = _store(db_factory)
    with pytest.raises(ValidationError):
        st.load()
    st.set("approval_mode", "manual", actor="breaker")
    st.set("finviz.min_interval_seconds", 2.0, actor="breaker")
    loaded = st.load()
    assert loaded.approval_mode == "manual"
    assert loaded.finviz_min_interval_seconds == 2.0


@pytest.mark.db
def test_failed_validation_writes_no_setting_and_no_audit(db_factory: sessionmaker[Session]) -> None:
    st = _store(db_factory)
    st.set("approval_mode", "auto", actor="breaker")
    assert _counts(db_factory) == (1, 1)
    bad: list[tuple[str, Any]] = [
        ("approval_mode", "AUTO"),
        ("approval_mode", None),
        ("markets_enabled", ["NYSE"]),
        ("markets_enabled", "US"),
        ("finviz.min_interval_seconds", 1.999),
        ("finviz.cache_hours", -1),
        ("open_bar.lookback_sessions", 4),
        ("open_bar.lookback_sessions", 31),
        ("open_bar.lookback_sessions", 10.5),
    ]
    for key, value in bad:
        with pytest.raises(ValidationError):
            st.set(key, value, actor="breaker")
    assert _counts(db_factory) == (1, 1)
    assert st.load().approval_mode == "auto"


# ---------------------------------------------------------------- crypto


def test_invalid_fernet_key_fails_at_construction_without_leaking_it() -> None:
    bad_keys = ["", "not-a-key", "x" * 44, Fernet.generate_key().decode()[:-2]]
    for key in bad_keys:
        with pytest.raises(ValueError) as exc:
            Crypto(key)
        if key:
            assert key not in str(exc.value)


def test_unicode_and_very_long_secrets_round_trip() -> None:
    c = Crypto(Fernet.generate_key().decode())
    for plain in ["tökén-🔑-秘密-​-\n\t", "a" * 1_000_000, " ", "0"]:
        token = c.encrypt(plain)
        if len(plain) > 8:  # a 1-char plaintext can occur in base64 by chance
            assert plain not in token
        assert c.decrypt(token) == plain
    # A valid Fernet token whose payload is not UTF-8 must read as None, not raise.
    key = Fernet.generate_key()
    raw = Fernet(key).encrypt(b"\xff\xfe\x00bad").decode()
    assert Crypto(key.decode()).decrypt(raw) is None


def test_decrypt_after_key_rotation_returns_none() -> None:
    old, new = Crypto(Fernet.generate_key().decode()), Crypto(Fernet.generate_key().decode())
    token = old.encrypt("refresh-token-value")
    assert new.decrypt(token) is None
    assert old.decrypt(token) == "refresh-token-value"
    # A truncated or tampered token is also unreadable, not an exception.
    assert old.decrypt(token[:-4]) is None
    assert old.decrypt(token[:10] + ("A" if token[10] != "A" else "B") + token[11:]) is None
