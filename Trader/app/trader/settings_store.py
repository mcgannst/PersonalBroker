"""Runtime settings stored one key per row in trader.settings (SPEC §13), with an audit trail.

load() fails closed: any invalid stored row raises ValidationError (the invalid keys are logged,
never their values). set() ignores invalid rows of OTHER keys, so a corrupt row never locks out
writes, and setting the corrupt key itself to a valid value repairs the store.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import AuditLog, Setting
from trader.db.session import session_scope

log = structlog.get_logger("settings_store")

DEFAULT_UNIVERSE_FILTERS = "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa"
# A comma-separated list of FinViz filter tokens, for example "sh_price_5to50,geo_usa".
FINVIZ_FILTERS_PATTERN = r"^[a-z0-9_.]+(,[a-z0-9_.]+)*$"
TICKER_PATTERN = r"^[A-Z][A-Z0-9.\-]{0,9}$"
OVERLAY_SYMBOL = "SPY"  # the market overlay reads SPY bars, so it must always be in the universe

Market = Literal["US", "TSX"]
Ticker = Annotated[str, StringConstraints(pattern=TICKER_PATTERN)]


def _default_markets() -> list[Market]:
    return ["US"]


class RuntimeSettings(BaseModel):
    # The DB key of a setting is its alias. Phase 2 keys must use `alias=` (not `validation_alias`),
    # because _DB_KEYS and model_dump(by_alias=True) are built from `alias`.
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    approval_mode: Literal["manual", "auto"] = "manual"
    markets_enabled: list[Market] = Field(default_factory=_default_markets, min_length=1)
    universe_finviz_filters: str = Field(
        DEFAULT_UNIVERSE_FILTERS, pattern=FINVIZ_FILTERS_PATTERN, alias="universe.finviz_filters"
    )
    universe_extra_symbols: list[Ticker] = Field(
        default_factory=lambda: [OVERLAY_SYMBOL], alias="universe.extra_symbols"
    )
    finviz_min_interval_seconds: float = Field(
        2.0, ge=2.0, le=60, allow_inf_nan=False, alias="finviz.min_interval_seconds"
    )
    finviz_cache_hours: float = Field(12.0, ge=0, le=168, allow_inf_nan=False, alias="finviz.cache_hours")
    open_bar_lookback_sessions: int = Field(14, ge=5, le=30, alias="open_bar.lookback_sessions")

    @field_validator("markets_enabled")
    @classmethod
    def _unique_markets(cls, v: list[Market]) -> list[Market]:
        if len(set(v)) != len(v):
            raise ValueError("markets_enabled must not contain duplicates")
        return v

    @field_validator("universe_extra_symbols")
    @classmethod
    def _extra_symbols(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("universe.extra_symbols must not contain duplicates")
        if OVERLAY_SYMBOL not in v:
            raise ValueError(f"universe.extra_symbols must include {OVERLAY_SYMBOL}")
        return v


# DB key -> field name.
_DB_KEYS: dict[str, str] = {(f.alias or name): name for name, f in RuntimeSettings.model_fields.items()}
_DEFAULTS: dict[str, Any] = RuntimeSettings().model_dump(mode="json")


def _key_is_valid(key: str, value: Any) -> bool:
    try:
        RuntimeSettings.model_validate({key: value})
    except ValidationError:
        return False
    return True


def _split_rows(rows: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate each known stored key on its own. Returns (valid rows, invalid keys)."""
    valid: dict[str, Any] = {}
    invalid: list[str] = []
    for key, value in rows.items():
        if key not in _DB_KEYS:
            continue
        if _key_is_valid(key, value):
            valid[key] = value
        else:
            invalid.append(key)
    return valid, invalid


class SettingsStore:
    def __init__(self, factory: sessionmaker[Session], now: Callable[[], datetime]) -> None:
        self._factory = factory
        self._now = now

    def _rows(self, session: Session) -> dict[str, Any]:
        return {row.key: row.value for row in session.execute(select(Setting)).scalars()}

    def load(self) -> RuntimeSettings:
        with self._factory() as session:
            rows = self._rows(session)
        try:
            return RuntimeSettings.model_validate(rows)
        except ValidationError:
            _, invalid = _split_rows(rows)
            log.error("settings.invalid_stored_rows", keys=sorted(invalid))
            raise

    def set(self, key: str, value: Any, actor: str) -> RuntimeSettings:
        if key not in _DB_KEYS:
            raise KeyError(key)
        field = _DB_KEYS[key]
        now = self._now()
        with session_scope(self._factory) as session:
            valid, _ = _split_rows(self._rows(session))
            updated = RuntimeSettings.model_validate({**valid, key: value})  # raises ValidationError
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
                        "value": RuntimeSettings.model_validate({key: row.value}).model_dump(mode="json")[
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
