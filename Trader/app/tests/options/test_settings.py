"""OPTSIM T1: the option settings (task plan §3.8). Every key has its default and bounds; the option store
validates, audits and repairs like the stock `SettingsStore`; the two stores share `trader.settings` and
neither reads the other's keys."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.api.forms import model_fields_out
from trader.db import models as m
from trader.options.settings import OPTION_SETTING_KEYS, OptionSettings, OptionSettingsStore
from trader.settings_store import RuntimeSettings, SettingsStore

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
D = Decimal

# key, default, values accepted (the bounds themselves), values refused (just outside, or the wrong type)
CASES: list[tuple[str, Any, list[Any], list[Any]]] = [
    ("options.starting_cash", D("5000"), ["0.01", "10000000"], ["0", "-1", "10000000.01"]),
    ("options.max_position_pct", D("0.50"), ["0.0001", "1"], ["0", "1.01"]),
    ("options.fee_per_contract", D("0.99"), ["0", "10"], ["-0.01", "10.01"]),
    ("options.assignment_fee", D("0"), ["0", "100"], ["-0.01", "100.01"]),
    ("options.share_commission", D("0"), ["0", "100"], ["-0.01", "100.01"]),
    ("options.quote_poll_seconds", 5, [1, 60], [0, 61]),
    ("options.mark_seconds", 60, [10, 3600], [9, 3601]),
    ("options.snapshot_seconds", 300, [60, 3600], [59, 3601]),
    ("options.stale_quote_seconds", 15, [1, 300], [0, 301]),
    ("options.reprice_seconds", 60, [5, 3600], [4, 3601]),
    ("options.tick_size", D("0.01"), ["0.01", "0.10"], ["0.009", "0.11"]),
    ("options.itm_threshold", D("0.01"), ["0", "1"], ["-0.01", "1.01"]),
    ("options.early_assignment_enabled", True, [True, False], ["perhaps"]),
    ("options.max_legs", 4, [1, 4], [0, 5]),
    ("options.max_contracts_per_order", 10, [1, 100], [0, 101]),
    ("options.allow_market_orders", True, [True, False], ["perhaps"]),
    ("options.watchlist", [], [[], ["F", "SOFI", "BRK.B"]], [["f"], ["F", "F"], "F", [""]]),
    ("options.benchmark_ticker", "SOFI", ["SPY", "BRK.B"], ["sofi", "", "TOOLONGTICKER"]),
    ("options.strategies_paused", False, [True, False], ["perhaps"]),
    ("options.prompt_repeat_hours", 24, [1, 168], [0, 169]),
    ("options.facts_max_age_hours", 36, [1, 240], [0, 241]),
    ("options.chain_cache_hours", 24, [1, 168], [0, 169]),
    ("options.web_quote_cache_seconds", 5, [1, 60], [0, 61]),
    ("options.heartbeat_seconds", 15, [5, 300], [4, 301]),
    ("options.strike_touch_alerts", True, [True, False], ["perhaps"]),
]


def test_the_table_covers_every_key_once() -> None:
    assert [c[0] for c in CASES] == list(OPTION_SETTING_KEYS)
    assert len(OPTION_SETTING_KEYS) == 25 and all(k.startswith("options.") for k in OPTION_SETTING_KEYS)
    # the field name is the key without its prefix, and every field has an alias (the DB key)
    for name, info in OptionSettings.model_fields.items():
        assert info.alias == f"options.{name}"


@pytest.mark.parametrize(("key", "default", "accepted", "refused"), CASES, ids=[c[0] for c in CASES])
def test_setting_defaults_and_bounds(key: str, default: Any, accepted: list[Any], refused: list[Any]) -> None:
    field = key.removeprefix("options.")
    assert getattr(OptionSettings(), field) == default
    assert type(getattr(OptionSettings(), field)) is type(default)  # Decimal stays Decimal, never float
    for value in accepted:
        OptionSettings.model_validate({key: value})
    for value in refused:
        with pytest.raises(ValidationError):
            OptionSettings.model_validate({key: value})
    for value in ("nan", "inf"):
        if isinstance(default, Decimal):
            with pytest.raises(ValidationError):
                OptionSettings.model_validate({key: value})


def test_settings_are_frozen_dump_by_key_and_describe_themselves() -> None:
    settings = OptionSettings(max_legs=2)  # by field name too
    with pytest.raises(ValidationError):
        settings.max_legs = 3  # type: ignore[misc]
    dumped = settings.model_dump(mode="json", by_alias=True)
    assert list(dumped) == list(OPTION_SETTING_KEYS) and dumped["options.max_legs"] == 2
    assert OptionSettings.model_validate(dumped) == settings
    fields = {fld.name: fld for fld in model_fields_out(OptionSettings, by_alias=True)}  # what the API serves
    assert list(fields) == list(OPTION_SETTING_KEYS)
    assert (fields["options.tick_size"].kind, fields["options.tick_size"].minimum) == ("decimal", "0.01")
    assert fields["options.max_position_pct"].exclusive_minimum is True
    assert fields["options.watchlist"].kind == "string_list"
    for key in ("options.assignment_fee", "options.tick_size"):
        assert (fields[key].description or "").startswith("ASSUMPTION:")
    assert all(fld.description for fld in fields.values())


def _store(factory: sessionmaker[Session]) -> OptionSettingsStore:
    return OptionSettingsStore(factory, now=lambda: NOW)


def _audit(factory: sessionmaker[Session]) -> list[tuple[str, str, Any, Any]]:
    with factory() as s:
        rows = s.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars().all()
        return [(a.actor, a.action, a.before, a.after) for a in rows]


@pytest.mark.db
def test_option_store_sets_audits_and_repairs(db_factory: sessionmaker[Session]) -> None:
    store = _store(db_factory)
    assert store.load() == OptionSettings()
    updated = store.set("options.max_position_pct", "0.25", "web:stephen")
    assert updated.max_position_pct == D("0.25") and store.load().max_position_pct == D("0.25")
    store.set("options.max_position_pct", D("0.4"), "cli")
    store.set("options.watchlist", ["F", "SOFI"], "web:stephen")
    assert store.load().watchlist == ["F", "SOFI"]
    assert _audit(db_factory) == [
        ("web:stephen", "settings.set:options.max_position_pct", {"value": "0.50"}, {"value": "0.25"}),
        ("cli", "settings.set:options.max_position_pct", {"value": "0.25"}, {"value": "0.4"}),
        ("web:stephen", "settings.set:options.watchlist", {"value": []}, {"value": ["F", "SOFI"]}),
    ]
    with db_factory() as s:
        row = s.get_one(m.Setting, "options.watchlist")
        assert (row.value, row.updated_by, row.updated_at) == (["F", "SOFI"], "web:stephen", NOW)

    # invalid values and unknown keys change nothing
    with pytest.raises(ValidationError):
        store.set("options.max_legs", 9, "web:stephen")
    for key in ("options.nope", "max_position_pct", "starting_cash"):
        with pytest.raises(KeyError):
            store.set(key, 1, "web:stephen")
    assert len(_audit(db_factory)) == 3 and store.load().max_legs == 4

    # a corrupt stored row: load fails closed, other keys can still be set, and setting it repairs it
    with db_factory() as s:
        s.add(m.Setting(key="options.tick_size", value="lots", updated_at=NOW, updated_by="psql"))
        s.commit()
    with pytest.raises(ValidationError):
        store.load()
    assert store.set("options.max_legs", 2, "web:stephen").max_legs == 2
    repaired = store.set("options.tick_size", "0.05", "web:stephen")
    assert repaired.tick_size == D("0.05") and store.load() == repaired
    assert _audit(db_factory)[-1] == (
        "web:stephen",
        "settings.set:options.tick_size",
        {"value": "lots", "invalid": True},
        {"value": "0.05"},
    )


@pytest.mark.db
def test_stock_store_ignores_option_keys_and_the_reverse(db_factory: sessionmaker[Session]) -> None:
    stock = SettingsStore(db_factory, now=lambda: NOW)
    options = _store(db_factory)
    # The two models share four field names: each store must read only its own key.
    stock.set("starting_cash", "1000", "web:stephen")
    stock.set("max_position_pct", "0.2", "web:stephen")
    stock.set("quote_poll_seconds", 3, "web:stephen")
    stock.set("stale_quote_seconds", 20, "web:stephen")
    assert options.load() == OptionSettings()
    options.set("options.starting_cash", "7500", "web:stephen")
    options.set("options.max_position_pct", "0.3", "web:stephen")
    options.set("options.quote_poll_seconds", 9, "web:stephen")
    options.set("options.strategies_paused", True, "web:stephen")
    loaded = stock.load()
    assert (loaded.starting_cash, loaded.max_position_pct) == (D("1000"), D("0.2"))
    assert (loaded.quote_poll_seconds, loaded.stale_quote_seconds) == (3.0, 20.0)
    assert loaded.model_dump(by_alias=True).keys() == RuntimeSettings().model_dump(by_alias=True).keys()
    mine = options.load()
    assert (mine.starting_cash, mine.max_position_pct, mine.quote_poll_seconds) == (D("7500"), D("0.3"), 9)
    assert mine.stale_quote_seconds == 15 and mine.strategies_paused is True
    # a corrupt row of one store never breaks the other
    with db_factory() as s:
        s.add(m.Setting(key="options.max_legs", value="many", updated_at=NOW, updated_by="psql"))
        s.add(m.Setting(key="risk_pct", value="huge", updated_at=NOW, updated_by="psql"))
        s.commit()
    with pytest.raises(ValidationError):
        options.load()
    with pytest.raises(ValidationError):
        stock.load()
    stock.set("risk_pct", "0.02", "web:stephen")
    assert stock.load().risk_pct == D("0.02")  # the stock store is whole again; the option row is still bad
    options.set("options.max_legs", 3, "web:stephen")
    assert options.load().max_legs == 3
    for key in ("options.max_legs", "options.starting_cash"):
        with pytest.raises(KeyError):
            stock.set(key, 1, "web:stephen")
