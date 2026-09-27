"""Strategy plug-ins found through the `trader.strategies` entry point (SPEC §5.1, BR-10).

Settings live in strategy_configs, one row per revision. Every change is a new revision, so each signal
records the exact settings (and plug-in version) that produced it.

A broken plug-in never takes the others down: load_all(), ensure_defaults() and enabled() log it (structlog
error, plus an event_log row where there is a database) and carry on without it. An explicit
load_plugin(name) or instance(key) for a broken plug-in raises PluginError.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import EntryPoint, entry_points
from typing import Any, cast

import structlog
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock
from trader.strategies.base import Strategy

ENTRY_POINT_GROUP = "trader.strategies"
LIVE_SCOPE = "live"
REPLAY_SCOPE = "replay"
KINDS = ("entry", "overlay")
SOURCE = "strategies.registry"
_REQUIRED = ("key", "version", "kind", "params_model", "schedule", "on_event", "on_fill")

log = structlog.get_logger(SOURCE)


class PluginError(Exception):
    """A plug-in is missing, declared twice, fails to import, or doesn't satisfy the Strategy protocol."""


def available() -> dict[str, EntryPoint]:
    """Declared plug-ins by name. Nothing is imported until load_plugin()."""
    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def load_plugin(name: str) -> type[Any]:
    declared = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not declared:
        raise PluginError(f"no strategy plug-in named {name!r}")
    targets = sorted({ep.value for ep in declared})
    if len(targets) > 1:
        raise PluginError(f"strategy plug-in {name!r} is declared more than once: {targets}")
    try:
        cls: Any = declared[0].load()
    except Exception as exc:  # an import error, a missing attribute, an error raised at import time
        raise PluginError(f"plug-in {name!r} failed to load: {type(exc).__name__}: {exc}") from exc
    if not isinstance(cls, type):
        raise PluginError(f"plug-in {name!r} loads a {type(cls).__name__}, not a class")
    cls = cast(Any, cls)  # a plug-in class: its protocol attributes are checked below
    missing = [a for a in _REQUIRED if not hasattr(cls, a)]
    if missing:
        raise PluginError(f"plug-in {name!r} lacks {missing}")
    if cls.key != name:
        raise PluginError(f"entry point {name!r} loads a plug-in keyed {cls.key!r}")
    if cls.kind not in KINDS:
        raise PluginError(f"plug-in {name!r}: kind must be one of {KINDS}, not {cls.kind!r}")
    if not (isinstance(cls.params_model, type) and issubclass(cls.params_model, BaseModel)):
        raise PluginError(f"plug-in {name!r}: params_model must be a pydantic model")
    return cast(type[Any], cls)


def load_all() -> dict[str, type[Any]]:
    """Every plug-in that loads. A broken one is logged and skipped, so it can't stop the others."""
    out: dict[str, type[Any]] = {}
    for name in sorted(available()):
        try:
            out[name] = load_plugin(name)
        except PluginError as exc:
            log.error("strategy.plugin_failed", plugin=name, stage="load", error=str(exc))
    return out


@dataclass(frozen=True, slots=True)
class StrategyConfigView:
    id: int
    strategy_key: str
    version: str
    revision: int
    params: dict[str, Any]
    enabled: bool
    created_at: datetime
    scope: str = "live"  # live | replay (migration 0006): a replay's override row is never a live setting


def _view(row: m.StrategyConfig) -> StrategyConfigView:
    return StrategyConfigView(
        row.id,
        row.strategy_key,
        row.version,
        row.revision,
        dict(row.params),
        row.enabled,
        row.created_at,
        row.scope,
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

    def _report(self, key: str, stage: str, exc: BaseException) -> None:
        """Log a plug-in that was skipped: structlog always, event_log when the database takes it."""
        error = f"{type(exc).__name__}: {exc}"[:500]
        log.error("strategy.plugin_failed", plugin=key, stage=stage, error=error)
        try:
            with session_scope(self._factory) as s:
                log_event(
                    s,
                    self._clock,
                    "error",
                    SOURCE,
                    f"strategy {key} skipped ({stage})",
                    {"strategy": key, "stage": stage, "error": error},
                )
        except Exception as db_exc:  # the database may be the reason the plug-in failed
            log.error("strategy.plugin_failed_unrecorded", plugin=key, error=str(db_exc)[:300])

    @staticmethod
    def _lock(s: Session, key: str) -> None:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"strategy_configs:{key}"})

    @staticmethod
    def _latest(s: Session, key: str) -> m.StrategyConfig | None:
        """The newest LIVE row of `key`. A replay's override rows (scope `replay`) are never live settings."""
        return s.execute(
            select(m.StrategyConfig)
            .where(m.StrategyConfig.strategy_key == key, m.StrategyConfig.scope == LIVE_SCOPE)
            .order_by(m.StrategyConfig.revision.desc())
            .limit(1)
        ).scalar_one_or_none()

    def ensure_defaults(self, actor: str = "system") -> None:
        """Revision 1 for a new plug-in; a new, audited revision when a plug-in's version changed.

        Each plug-in is handled on its own: one that fails (e.g. its stored params no longer validate
        against the new version's model) is logged and skipped, and the others still get their rows.
        """
        for key in self.keys():
            try:
                self._ensure_one(key, actor)
            except Exception as exc:
                self._report(key, "ensure_defaults", exc)

    def _ensure_one(self, key: str, actor: str) -> None:
        cls = self.plugin_class(key)
        now = self._clock.now()
        with session_scope(self._factory) as s:
            self._lock(s, key)
            latest = self._latest(s, key)
            if latest is not None and latest.version == cls.version:
                return
            params = (
                cls.params_model().model_dump(mode="json")
                if latest is None
                else cls.params_model.model_validate(latest.params).model_dump(mode="json")
            )
            row = m.StrategyConfig(
                strategy_key=key,
                version=cls.version,
                revision=1 if latest is None else latest.revision + 1,
                params=params,
                enabled=True if latest is None else latest.enabled,
                created_at=now,
                created_by=actor,
            )
            s.add(row)
            if latest is not None:  # a version bump changes what runs, so it's audited like update()
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

    def create_replay_config(
        self,
        key: str,
        *,
        base: StrategyConfigView,
        params: Mapping[str, Any],
        enabled: bool,
        created_by: str,
    ) -> StrategyConfigView:
        """A `replay`-scoped row for a replay's parameter override (P5-T4): `{**base.params, **params}`
        validated by the plug-in's model (ValidationError), the base live row's revision and version, no audit
        row (the replay's `replay.start` audit row describes it). Live settings never see it. ValueError when
        `base` is not a live row of `key`."""
        cls = self.plugin_class(key)
        now = self._clock.now()
        with session_scope(self._factory) as s:
            row = s.get(m.StrategyConfig, base.id)
            if row is None or row.strategy_key != key or row.scope != LIVE_SCOPE:
                raise ValueError(
                    f"config {base.id} is not a live {key!r} config: a replay's base must be one"
                )
            merged = {**row.params, **params}
            validated = cls.params_model.model_validate(merged).model_dump(mode="json")  # ValidationError
            replay_row = m.StrategyConfig(
                strategy_key=key,
                version=row.version,
                revision=row.revision,
                params=validated,
                enabled=enabled,
                created_at=now,
                created_by=created_by,
                scope=REPLAY_SCOPE,
            )
            s.add(replay_row)
            s.flush()
            return _view(replay_row)

    def _build(self, key: str, cfg: StrategyConfigView) -> Strategy:
        cls = self.plugin_class(key)
        try:
            return cast(Strategy, cls(cls.params_model.model_validate(cfg.params)))
        except Exception as exc:
            raise PluginError(
                f"plug-in {key!r} (revision {cfg.revision}) failed to start: {type(exc).__name__}: {exc}"
            ) from exc

    def instance(self, key: str) -> tuple[Strategy, StrategyConfigView]:
        """The plug-in with its current settings; PluginError if its params don't validate or it fails."""
        cfg = self.current(key)
        return self._build(key, cfg), cfg

    def enabled(self) -> list[tuple[Strategy, StrategyConfigView]]:
        """Every enabled plug-in that starts. A broken one is logged and skipped."""
        out: list[tuple[Strategy, StrategyConfigView]] = []
        for key in self.keys():
            try:
                cfg = self.current(key)
                if cfg.enabled:
                    out.append((self._build(key, cfg), cfg))
            except Exception as exc:
                self._report(key, "enabled", exc)
        return out

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
