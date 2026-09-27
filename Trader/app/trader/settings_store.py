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

Market = Literal["US", "TSX"]


def _default_markets() -> list[Market]:
    return ["US"]


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    approval_mode: Literal["manual", "auto"] = "manual"
    markets_enabled: list[Market] = Field(default_factory=_default_markets)
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
            session.add(
                AuditLog(
                    ts=self._now(),
                    actor=actor,
                    action=f"settings.set:{key}",
                    before={"value": before.model_dump(mode="json")[keys[key]]},
                    after={"value": stored},
                )
            )
        return updated
