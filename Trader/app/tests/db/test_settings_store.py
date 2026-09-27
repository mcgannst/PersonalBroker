import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select
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


# ---------------------------------------------------------------- P1-T3 fix round regressions


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("finviz.min_interval_seconds", float("inf")),
        ("finviz.min_interval_seconds", float("nan")),
        ("finviz.min_interval_seconds", 60.5),
        ("finviz.cache_hours", float("inf")),
        ("finviz.cache_hours", float("nan")),
        ("finviz.cache_hours", 168.5),
    ],
)
def test_non_finite_and_oversized_floats_rejected(
    db_factory: sessionmaker[Session], key: str, value: float
) -> None:
    st = store(db_factory)
    with pytest.raises(ValidationError):
        st.set(key, value, actor="stephen")
    assert st.load() == RuntimeSettings()


def test_float_upper_bounds_accepted(db_factory: sessionmaker[Session]) -> None:
    st = store(db_factory)
    st.set("finviz.min_interval_seconds", 60, actor="stephen")
    st.set("finviz.cache_hours", 168, actor="stephen")
    loaded = st.load()
    assert loaded.finviz_min_interval_seconds == 60.0
    assert loaded.finviz_cache_hours == 168.0


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "ind_stocksonly,,geo_usa",
        "ind_stocksonly, geo_usa",
        "IND_STOCKSONLY",
        "geo_usa&x=1",
        ",geo_usa",
    ],
)
def test_bad_finviz_filters_rejected(db_factory: sessionmaker[Session], value: str) -> None:
    with pytest.raises(ValidationError):
        store(db_factory).set("universe.finviz_filters", value, actor="stephen")


def test_good_finviz_filters_accepted(db_factory: sessionmaker[Session]) -> None:
    st = store(db_factory)
    st.set("universe.finviz_filters", "sh_price_o5,ta_averagetruerange_o0.5", actor="stephen")
    assert st.load().universe_finviz_filters == "sh_price_o5,ta_averagetruerange_o0.5"


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["QQQ"],
        ["SPY", "spy"],
        ["SPY", "qqq"],
        ["SPY", "SPY"],
        ["SPY", "QQQ", "QQQ"],
        ["SPY", "1ABC"],
        ["SPY", "ABCDEFGHIJK"],
        ["SPY", "BRK B"],
        ["SPY", ""],
    ],
)
def test_bad_extra_symbols_rejected(db_factory: sessionmaker[Session], value: list[str]) -> None:
    with pytest.raises(ValidationError):
        store(db_factory).set("universe.extra_symbols", value, actor="stephen")


def test_good_extra_symbols_accepted(db_factory: sessionmaker[Session]) -> None:
    st = store(db_factory)
    st.set("universe.extra_symbols", ["SPY", "BRK.B", "RCI-B", "ABCDEFGHIJ"], actor="stephen")
    assert st.load().universe_extra_symbols == ["SPY", "BRK.B", "RCI-B", "ABCDEFGHIJ"]


@pytest.mark.parametrize("value", [[], ["US", "US"], ["TSX", "US", "TSX"]])
def test_bad_markets_enabled_rejected(db_factory: sessionmaker[Session], value: list[str]) -> None:
    with pytest.raises(ValidationError):
        store(db_factory).set("markets_enabled", value, actor="stephen")


def test_corrupt_row_fails_load_but_other_keys_can_be_set(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(Setting(key="approval_mode", value="yolo", updated_by="psql"))
        s.commit()
    st = store(db_factory)
    with pytest.raises(ValidationError):
        st.load()
    updated = st.set("open_bar.lookback_sessions", 10, actor="stephen")
    assert updated.open_bar_lookback_sessions == 10
    with pytest.raises(ValidationError):
        st.load()  # still fails closed until the corrupt key itself is repaired


def test_repairing_corrupt_row_audits_raw_before(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(Setting(key="approval_mode", value="yolo", updated_by="psql"))
        s.commit()
    st = store(db_factory)
    st.set("approval_mode", "manual", actor="stephen")
    assert st.load().approval_mode == "manual"
    with db_factory() as s:
        audit = s.execute(select(AuditLog)).scalar_one()
    assert audit.before == {"value": "yolo", "invalid": True}
    assert audit.after == {"value": "manual"}


def test_set_reads_now_once(db_factory: sessionmaker[Session]) -> None:
    calls: list[int] = []

    def now() -> datetime:
        calls.append(1)
        return NOW

    SettingsStore(db_factory, now=now).set("approval_mode", "auto", actor="stephen")
    assert len(calls) == 1


def test_concurrent_set_of_same_new_key(db_factory: sessionmaker[Session]) -> None:
    """Two processes creating the same key at once must not hit a PK IntegrityError, and each
    audit "before" must be read under the row lock, so the later one sees the earlier one's write."""
    st = store(db_factory)
    values = [20, 25]
    for _ in range(5):
        with db_factory() as s:
            s.execute(delete(AuditLog))
            s.execute(delete(Setting))
            s.commit()
        barrier = threading.Barrier(len(values))

        def worker(value: int, barrier: threading.Barrier = barrier) -> None:
            barrier.wait(timeout=30)
            st.set("open_bar.lookback_sessions", value, actor="stephen")

        with ThreadPoolExecutor(max_workers=len(values)) as pool:
            list(pool.map(worker, values))
        with db_factory() as s:
            rows = s.execute(select(Setting)).scalars().all()
            audits = s.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
        assert len(rows) == 1 and len(audits) == 2
        assert rows[0].value == audits[1].after["value"]
        assert audits[0].before == {"value": 14}
        assert audits[1].before == audits[0].after
