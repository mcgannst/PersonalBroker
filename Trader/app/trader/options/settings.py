"""The options simulation's settings (OPTSIM task plan §3.8): keys `options.*`, one row per key in
`trader.settings`, the table the stock `SettingsStore` uses.

The two stores share the table safely: each reads and writes only its own keys. The stock store ignores
unknown keys, and this one filters to `options.*` before validating (so the stock rows `starting_cash`,
`max_position_pct`, ... never reach the option fields of the same name).

Same rules as `trader.settings_store`: `load()` fails closed (an invalid stored row raises ValidationError;
the invalid keys are logged, never their values); `set()` ignores invalid rows of OTHER keys, and setting a
corrupt key to a valid value repairs it; every `set` writes one `audit_log` row `settings.set:<key>`.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import AuditLog, Setting
from trader.db.session import session_scope
from trader.settings_store import TICKER_PATTERN

log = structlog.get_logger("options.settings")

KEY_PREFIX = "options."
SETTINGS_GROUP = "Options"  # the group every option setting is shown in (task plan §3.9)
Ticker = Annotated[str, StringConstraints(pattern=TICKER_PATTERN)]


def _money(default: str, alias: str, description: str, **bounds: Any) -> Any:
    return Field(Decimal(default), allow_inf_nan=False, alias=alias, description=description, **bounds)


class OptionSettings(BaseModel):
    # The DB key of a setting is its alias. Field names are the key without the `options.` prefix.
    # No model_validator: keys are validated one at a time.
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    starting_cash: Decimal = _money(
        "5000",
        "options.starting_cash",
        "Default cash (USD) of `trader options-run new`.",
        gt=0,
        le=Decimal("10000000"),
    )
    max_position_pct: Decimal = _money(
        "0.50",
        "options.max_position_pct",
        "Cap per underlying, as a fraction of the account value.",
        gt=0,
        le=Decimal("1"),
    )
    fee_per_contract: Decimal = _money(
        "0.99",
        "options.fee_per_contract",
        "Fee per contract, per leg, each way.",
        ge=0,
        le=Decimal("10"),
    )
    assignment_fee: Decimal = _money(
        "0",
        "options.assignment_fee",
        "ASSUMPTION: fee per assignment or exercise; verify against the broker's fee schedule.",
        ge=0,
        le=Decimal("100"),
    )
    share_commission: Decimal = _money(
        "0", "options.share_commission", "Commission per shares leg.", ge=0, le=Decimal("100")
    )
    quote_poll_seconds: int = Field(
        5, ge=1, le=60, alias="options.quote_poll_seconds", description="Working-order quote poll interval."
    )
    mark_seconds: int = Field(
        60, ge=10, le=3600, alias="options.mark_seconds", description="Open-position mark interval."
    )
    snapshot_seconds: int = Field(
        300,
        ge=60,
        le=3600,
        alias="options.snapshot_seconds",
        description="Equity snapshot spacing in the session.",
    )
    stale_quote_seconds: int = Field(
        15,
        ge=1,
        le=300,
        alias="options.stale_quote_seconds",
        description="A quote older than this (by the time it was fetched) never fills.",
    )
    reprice_seconds: int = Field(
        60, ge=5, le=3600, alias="options.reprice_seconds", description="Walk step interval."
    )
    tick_size: Decimal = _money(
        "0.01",
        "options.tick_size",
        "ASSUMPTION: one price tick for every contract.",
        ge=Decimal("0.01"),
        le=Decimal("0.10"),
    )
    itm_threshold: Decimal = _money(
        "0.01",
        "options.itm_threshold",
        "In the money at expiry when the close is this far through the strike.",
        ge=0,
        le=Decimal("1"),
    )
    early_assignment_enabled: bool = Field(
        True,
        alias="options.early_assignment_enabled",
        description="Simulate early assignment of a covered call before an ex-dividend date.",
    )
    max_legs: int = Field(4, ge=1, le=4, alias="options.max_legs", description="Legs per order.")
    max_contracts_per_order: int = Field(
        10, ge=1, le=100, alias="options.max_contracts_per_order", description="Units per order."
    )
    allow_market_orders: bool = Field(
        True, alias="options.allow_market_orders", description="Accept market orders."
    )
    watchlist: list[Ticker] = Field(
        default_factory=list,
        alias="options.watchlist",
        description="Underlyings kept fresh for the manual desk.",
    )
    benchmark_ticker: Ticker = Field(
        "SOFI",
        alias="options.benchmark_ticker",
        description="Benchmark shown on the Account tab and used by strategies.",
    )
    strategies_paused: bool = Field(
        False,
        alias="options.strategies_paused",
        description="When on, no strategy event fires and no strategy order is accepted.",
    )
    prompt_repeat_hours: int = Field(
        24,
        ge=1,
        le=168,
        alias="options.prompt_repeat_hours",
        description="An unanswered question is sent again after this many hours.",
    )
    facts_max_age_hours: int = Field(
        36,
        ge=1,
        le=240,
        alias="options.facts_max_age_hours",
        description="Underlying facts older than this count as missing.",
    )
    chain_cache_hours: int = Field(
        24,
        ge=1,
        le=168,
        alias="options.chain_cache_hours",
        description="How long a chain's structure is cached.",
    )
    web_quote_cache_seconds: int = Field(
        5,
        ge=1,
        le=60,
        alias="options.web_quote_cache_seconds",
        description="How long chain quotes served to the web are cached.",
    )
    heartbeat_seconds: int = Field(
        15, ge=5, le=300, alias="options.heartbeat_seconds", description="Options worker heartbeat interval."
    )
    strike_touch_alerts: bool = Field(
        True,
        alias="options.strike_touch_alerts",
        description="Alert when the underlying touches the strike of a short option.",
    )

    @field_validator("watchlist")
    @classmethod
    def _unique_watchlist(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("options.watchlist must not contain duplicates")
        return v


# DB key -> field name.
_DB_KEYS: dict[str, str] = {(f.alias or name): name for name, f in OptionSettings.model_fields.items()}
_DEFAULTS: dict[str, Any] = OptionSettings().model_dump(mode="json")
OPTION_SETTING_KEYS: tuple[str, ...] = tuple(_DB_KEYS)


def _key_is_valid(key: str, value: Any) -> bool:
    try:
        OptionSettings.model_validate({key: value})
    except ValidationError:
        return False
    return True


def _own(rows: dict[str, Any]) -> dict[str, Any]:
    """Only the option keys: a stock row never populates an option field of the same name."""
    return {key: value for key, value in rows.items() if key in _DB_KEYS}


def _split_rows(rows: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate each stored option key on its own. Returns (valid rows, invalid keys)."""
    valid: dict[str, Any] = {}
    invalid: list[str] = []
    for key, value in _own(rows).items():
        if _key_is_valid(key, value):
            valid[key] = value
        else:
            invalid.append(key)
    return valid, invalid


class OptionSettingsStore:
    def __init__(self, factory: sessionmaker[Session], now: Callable[[], datetime]) -> None:
        self._factory = factory
        self._now = now

    def _rows(self, session: Session) -> dict[str, Any]:
        stmt = select(Setting).where(Setting.key.like(f"{KEY_PREFIX}%"))
        return {row.key: row.value for row in session.execute(stmt).scalars()}

    def load(self) -> OptionSettings:
        with self._factory() as session:
            rows = _own(self._rows(session))
        try:
            return OptionSettings.model_validate(rows)
        except ValidationError:
            _, invalid = _split_rows(rows)
            log.error("options.settings.invalid_stored_rows", keys=sorted(invalid))
            raise

    def set(self, key: str, value: Any, actor: str) -> OptionSettings:
        if key not in _DB_KEYS:
            raise KeyError(key)
        field = _DB_KEYS[key]
        now = self._now()
        with session_scope(self._factory) as session:
            valid, _ = _split_rows(self._rows(session))
            updated = OptionSettings.model_validate({**valid, key: value})  # raises ValidationError
            stored = updated.model_dump(mode="json")[field]
            # Create the row if it is new. A concurrent creator makes this wait for its commit, then
            # do nothing, so two first writes of one key never collide on the primary key.
            inserted = session.execute(
                pg_insert(Setting)
                .values(key=key, value=stored, updated_at=now, updated_by=actor)
                .on_conflict_do_nothing(index_elements=[Setting.key])
                .returning(Setting.key)
            ).first()
            if inserted is not None:
                before: dict[str, Any] = {"value": _DEFAULTS[field]}
            else:
                # The row exists: lock it and read "before" under the lock.
                row = session.execute(
                    select(Setting)
                    .where(Setting.key == key)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one()
                if _key_is_valid(key, row.value):
                    before = {
                        "value": OptionSettings.model_validate({key: row.value}).model_dump(mode="json")[
                            field
                        ]
                    }
                else:
                    before = {"value": row.value, "invalid": True}
                row.value, row.updated_at, row.updated_by = stored, now, actor
            session.add(
                AuditLog(
                    ts=now, actor=actor, action=f"settings.set:{key}", before=before, after={"value": stored}
                )
            )
        return updated
