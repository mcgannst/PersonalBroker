from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from trader.settings_store import RuntimeSettings, SettingsStore

UNALIASED_PHASE1_FIELDS = {"approval_mode", "markets_enabled"}  # the only fields whose key is their name
PHASE2_KEYS = {
    "starting_cash",
    "starting_cash_currency",
    "account_currency",
    "fx.cad_usd_rate",
    "fx.fee_pct",
    "cash_account_mode",
    "risk_pct",
    "slippage_buffer",
    "no_entry_before_close_minutes",
    "quote_poll_seconds",
    "stale_quote_seconds",
    "slippage_min",
    "slippage_bps",
    "fees.commission",
    "fees.direct_route",
    "fees.ecn_per_share",
    "fees.sec_rate",
    "proposal_ttl_entry_seconds",
    "proposal_ttl_stop_seconds",
    "proposal_ttl_exit_seconds",
    "stop_escalation_seconds",
    "auto_flatten_on_expiry",
    "killswitch.daily_loss_pct",
    "killswitch.max_drawdown_pct",
    "killswitch.expectancy_min_trades",
    "killswitch.expectancy_threshold_r",
    "claude.model",
    "claude.daily_budget_usd",
    "claude.premarket_max_candidates",
    "premarket.gap_min_pct",
    "premarket.news_filter",
    "premarket.earnings_filter",
}


def test_phase2_defaults() -> None:
    s = RuntimeSettings()
    assert s.starting_cash == Decimal("720") and s.account_currency == "USD"
    assert s.starting_cash_currency == "USD" and s.fx_fee_pct == Decimal("0.015")
    assert s.cash_account_mode is True
    assert s.risk_pct == Decimal("0.02")
    assert (s.slippage_min, s.slippage_bps) == (Decimal("0.01"), Decimal("5"))
    assert (s.proposal_ttl_entry_seconds, s.proposal_ttl_stop_seconds, s.proposal_ttl_exit_seconds) == (
        300,
        180,
        300,
    )
    assert s.killswitch_daily_loss_pct == Decimal("0.05")
    assert s.killswitch_max_drawdown_pct == Decimal("0.15")
    assert s.killswitch_expectancy_min_trades == 50
    assert s.killswitch_expectancy_threshold_r == Decimal("0")
    assert s.no_entry_before_close_minutes == 30
    assert s.quote_poll_seconds == 2.0 and s.stale_quote_seconds == 10.0
    assert s.auto_flatten_on_expiry is True
    assert s.claude_model == "claude-sonnet-5"
    assert s.claude_daily_budget_usd == Decimal("1.00")
    assert s.claude_premarket_max_candidates == 50
    assert s.fees_sec_rate == Decimal("0.0000206") and s.fees_direct_route is False
    assert s.premarket_gap_min_pct == Decimal("0.03")


def test_every_phase2_field_has_an_alias_key() -> None:
    fields = RuntimeSettings.model_fields
    missing = [name for name, f in fields.items() if f.alias is None and name not in UNALIASED_PHASE1_FIELDS]
    assert missing == []
    assert PHASE2_KEYS <= {f.alias for f in fields.values()}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("risk_pct", "0"),
        ("risk_pct", "0.5"),
        ("risk_pct", "NaN"),
        ("starting_cash", "-1"),
        ("starting_cash_currency", "EUR"),
        ("slippage_bps", "101"),
        ("stale_quote_seconds", 0.5),
        ("proposal_ttl_entry_seconds", 10),
        ("killswitch.daily_loss_pct", "0"),
        ("killswitch.expectancy_min_trades", 0),
        ("claude.model", "gpt-4"),
        ("claude.daily_budget_usd", "Infinity"),
        ("premarket.news_filter", "news date"),
    ],
)
def test_invalid_values_rejected(key: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({key: value})


def test_round_trips_through_json() -> None:
    s = RuntimeSettings()
    assert RuntimeSettings.model_validate(s.model_dump(mode="json", by_alias=True)) == s


@pytest.mark.db
def test_store_sets_dotted_phase2_key(db_factory: sessionmaker[Session]) -> None:
    store = SettingsStore(db_factory, now=lambda: datetime(2026, 10, 6, 12, 0, tzinfo=UTC))
    store.set("killswitch.daily_loss_pct", "0.04", actor="stephen")
    store.set("claude.model", "claude-haiku-4-5", actor="stephen")
    loaded = store.load()
    assert loaded.killswitch_daily_loss_pct == Decimal("0.04")
    assert loaded.claude_model == "claude-haiku-4-5"
