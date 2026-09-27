"""Strategy plug-ins found through the `trader.strategies` entry point (SPEC §5.1, BR-10).

Settings live in strategy_configs, one row per revision. Every change is a new revision, so each signal
records the exact settings (and plug-in version) that produced it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import EntryPoint, entry_points
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock
from trader.strategies.base import Strategy

ENTRY_POINT_GROUP = "trader.strategies"
_REQUIRED = ("key", "version", "kind", "params_model", "schedule", "on_event", "on_fill")


class PluginError(Exception):
    """A plug-in is missing or doesn't satisfy the Strategy protocol."""


def available() -> dict[str, EntryPoint]:
    """Declared plug-ins by name. Nothing is imported until load_plugin()."""
    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def load_plugin(name: str) -> type[Any]:
    eps = available()
    if name not in eps:
        raise PluginError(f"no strategy plug-in named {name!r}")
    cls = eps[name].load()
    missing = [a for a in _REQUIRED if not hasattr(cls, a)]
    if missing:
        raise PluginError(f"plug-in {name!r} lacks {missing}")
    if cls.key != name:
        raise PluginError(f"entry point {name!r} loads a plug-in keyed {cls.key!r}")
    if not (isinstance(cls.params_model, type) and issubclass(cls.params_model, BaseModel)):
        raise PluginError(f"plug-in {name!r}: params_model must be a pydantic model")
    return cast(type[Any], cls)


def load_all() -> dict[str, type[Any]]:
    return {name: load_plugin(name) for name in sorted(available())}


@dataclass(frozen=True, slots=True)
class StrategyConfigView:
    id: int
    strategy_key: str
    version: str
    revision: int
    params: dict[str, Any]
    enabled: bool
    created_at: datetime


def _view(row: m.StrategyConfig) -> StrategyConfigView:
    return StrategyConfigView(
        row.id, row.strategy_key, row.version, row.revision, dict(row.params), row.enabled, row.created_at
    )


def _snapshot(row: m.StrategyConfig) -> dict[str, Any]:
    return {"revision": row.revision, "version": row.version, "params": row.params, "enabled": row.enabled}


class StrategyRegistry:
    def __init__(
        self, factory: sessionmaker[Session], clock: Clock, plugins: Mapping[str, type[Any]] | None = None
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._plugins = dict(plugins) if plugins is not None else load_all()

    def keys(self) -> list[str]:
        return sorted(self._plugins)

    def plugin_class(self, key: str) -> type[Any]:
        if key not in self._plugins:
            raise KeyError(f"unknown strategy {key!r}")
        return self._plugins[key]

    def json_schema(self, key: str) -> dict[str, Any]:
        schema: dict[str, Any] = self.plugin_class(key).params_model.model_json_schema()
        return schema

    @staticmethod
    def _lock(s: Session, key: str) -> None:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"strategy_configs:{key}"})

    @staticmethod
    def _latest(s: Session, key: str) -> m.StrategyConfig | None:
        return s.execute(
            select(m.StrategyConfig)
            .where(m.StrategyConfig.strategy_key == key)
            .order_by(m.StrategyConfig.revision.desc())
            .limit(1)
        ).scalar_one_or_none()

    def ensure_defaults(self, actor: str = "system") -> None:
        now = self._clock.now()
        for key in self.keys():
            cls = self.plugin_class(key)
            with session_scope(self._factory) as s:
                self._lock(s, key)
                latest = self._latest(s, key)
                if latest is not None and latest.version == cls.version:
                    continue
                params = (
                    cls.params_model().model_dump(mode="json")
                    if latest is None
                    else cls.params_model.model_validate(latest.params).model_dump(mode="json")
                )
                s.add(
                    m.StrategyConfig(
                        strategy_key=key,
                        version=cls.version,
                        revision=1 if latest is None else latest.revision + 1,
                        params=params,
                        enabled=True if latest is None else latest.enabled,
                        created_at=now,
                        created_by=actor,
                    )
                )

    def current(self, key: str) -> StrategyConfigView:
        self.plugin_class(key)
        with self._factory() as s:
            row = self._latest(s, key)
            if row is None:
                raise KeyError(f"strategy {key!r} has no settings yet: call ensure_defaults() first")
            return _view(row)

    def update(
        self,
        key: str,
        *,
        params: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        actor: str,
    ) -> StrategyConfigView:
        cls = self.plugin_class(key)
        now = self._clock.now()
        with session_scope(self._factory) as s:
            self._lock(s, key)
            latest = self._latest(s, key)
            if latest is None:
                raise KeyError(f"strategy {key!r} has no settings yet: call ensure_defaults() first")
            merged = {**latest.params, **(params or {})}
            validated = cls.params_model.model_validate(merged).model_dump(mode="json")  # ValidationError
            new_enabled = latest.enabled if enabled is None else enabled
            if validated == latest.params and new_enabled == latest.enabled and latest.version == cls.version:
                return _view(latest)
            row = m.StrategyConfig(
                strategy_key=key,
                version=cls.version,
                revision=latest.revision + 1,
                params=validated,
                enabled=new_enabled,
                created_at=now,
                created_by=actor,
            )
            s.add(row)
            s.flush()
            s.add(
                m.AuditLog(
                    ts=now,
                    actor=actor,
                    action=f"strategy.update:{key}",
                    before=_snapshot(latest),
                    after=_snapshot(row),
                )
            )
            return _view(row)

    def instance(self, key: str) -> tuple[Strategy, StrategyConfigView]:
        cfg = self.current(key)
        cls = self.plugin_class(key)
        return cast(Strategy, cls(cls.params_model.model_validate(cfg.params))), cfg

    def enabled(self) -> list[tuple[Strategy, StrategyConfigView]]:
        return [self.instance(k) for k in self.keys() if self.current(k).enabled]

    def config_ids(self, key: str) -> set[int]:
        with self._factory() as s:
            return set(
                s.execute(select(m.StrategyConfig.id).where(m.StrategyConfig.strategy_key == key)).scalars()
            )

    def config_key(self, config_id: int) -> str | None:
        with self._factory() as s:
            return s.execute(
                select(m.StrategyConfig.strategy_key).where(m.StrategyConfig.id == config_id)
            ).scalar_one_or_none()
