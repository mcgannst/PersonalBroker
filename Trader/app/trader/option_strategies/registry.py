"""Option strategy plug-ins found through the `trader.option_strategies` entry point (OPTSIM task plan T7).

The option twin of `trader.strategies.registry`, without the replay scope. Settings live in
`option_strategy_configs`, one row per revision: every change is a new, audited revision, so each order
records the exact settings (and plug-in version) that produced it.

A broken plug-in never takes the others down: load_all(), ensure_defaults() and enabled() log it (structlog
error, plus an event_log row where there is a database) and carry on without it. An explicit
load_plugin(name) or instance(key) for a broken plug-in raises PluginError.
"""

import re
from collections.abc import Mapping
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
from trader.option_strategies.base import (
    ENTRY_POINT_GROUP,
    KEY_PATTERN,
    OptionStrategy,
    OptionStrategyConfigView,
)

SOURCE = "options.registry"
# Every attribute and hook of the framework (§3.4): a plug-in lacking one is refused at load.
_REQUIRED = (
    "key",
    "version",
    "params_model",
    "manual_events",
    "schedule",
    "watch_underlyings",
    "on_event",
    "on_fill",
    "on_lifecycle",
    "on_answer",
    "prompts",
    "panel",
    "on_action",
)
_KEY = re.compile(KEY_PATTERN)

log = structlog.get_logger(SOURCE)


class PluginError(Exception):
    """A plug-in is missing, declared twice, fails to import, or isn't an OptionStrategy."""


def available() -> dict[str, EntryPoint]:
    """Declared plug-ins by name. Nothing is imported until load_plugin()."""
    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def load_plugin(name: str) -> type[Any]:
    declared = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not declared:
        raise PluginError(f"no option strategy plug-in named {name!r}")
    targets = sorted({ep.value for ep in declared})
    if len(targets) > 1:
        raise PluginError(f"option strategy plug-in {name!r} is declared more than once: {targets}")
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
    if not _KEY.fullmatch(name):
        raise PluginError(f"plug-in key {name!r} does not match {KEY_PATTERN}")
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
            log.error("option_strategy.plugin_failed", plugin=name, stage="load", error=str(exc))
    return out


def _view(row: m.OptionStrategyConfig) -> OptionStrategyConfigView:
    return OptionStrategyConfigView(
        row.id,
        row.strategy_key,
        row.version,
        row.revision,
        dict(row.params),
        row.enabled,
        row.created_at,
        row.created_by,
    )


def _snapshot(row: m.OptionStrategyConfig) -> dict[str, Any]:
    return {"revision": row.revision, "version": row.version, "params": row.params, "enabled": row.enabled}


class OptionStrategyRegistry:
    """`OptionStrategyRegistryView`, plus what the host needs: `ensure_defaults`, `instance`, `enabled`."""

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
            raise KeyError(f"unknown option strategy {key!r}")
        return self._plugins[key]

    def json_schema(self, key: str) -> dict[str, Any]:
        schema: dict[str, Any] = self.plugin_class(key).params_model.model_json_schema()
        return schema

    def _report(self, key: str, stage: str, exc: BaseException) -> None:
        """Log a plug-in that was skipped: structlog always, event_log when the database takes it."""
        error = f"{type(exc).__name__}: {exc}"[:500]
        log.error("option_strategy.plugin_failed", plugin=key, stage=stage, error=error)
        try:
            with session_scope(self._factory) as s:
                log_event(
                    s,
                    self._clock,
                    "error",
                    SOURCE,
                    f"option strategy {key} skipped ({stage})",
                    {"strategy": key, "stage": stage, "error": error},
                )
        except Exception as db_exc:  # the database may be the reason the plug-in failed
            log.error("option_strategy.plugin_failed_unrecorded", plugin=key, error=str(db_exc)[:300])

    @staticmethod
    def _lock(s: Session, key: str) -> None:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"option_strategy_configs:{key}"})

    @staticmethod
    def _latest(s: Session, key: str) -> m.OptionStrategyConfig | None:
        return s.execute(
            select(m.OptionStrategyConfig)
            .where(m.OptionStrategyConfig.strategy_key == key)
            .order_by(m.OptionStrategyConfig.revision.desc())
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
            row = m.OptionStrategyConfig(
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
                        action=f"option_strategy.update:{key}",
                        before=_snapshot(latest),
                        after=_snapshot(row),
                    )
                )

    def current(self, key: str) -> OptionStrategyConfigView:
        self.plugin_class(key)
        with self._factory() as s:
            row = self._latest(s, key)
            if row is None:
                raise KeyError(f"option strategy {key!r} has no settings yet: call ensure_defaults() first")
            return _view(row)

    def update(
        self,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        enabled: bool | None = None,
        actor: str,
    ) -> OptionStrategyConfigView:
        cls = self.plugin_class(key)
        now = self._clock.now()
        with session_scope(self._factory) as s:
            self._lock(s, key)
            latest = self._latest(s, key)
            if latest is None:
                raise KeyError(f"option strategy {key!r} has no settings yet: call ensure_defaults() first")
            merged = {**latest.params, **(params or {})}
            validated = cls.params_model.model_validate(merged).model_dump(mode="json")  # ValidationError
            new_enabled = latest.enabled if enabled is None else enabled
            if validated == latest.params and new_enabled == latest.enabled and latest.version == cls.version:
                return _view(latest)
            row = m.OptionStrategyConfig(
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
                    action=f"option_strategy.update:{key}",
                    before=_snapshot(latest),
                    after=_snapshot(row),
                )
            )
            return _view(row)

    def _build(self, key: str, cfg: OptionStrategyConfigView) -> OptionStrategy:
        cls = self.plugin_class(key)
        try:
            return cast(OptionStrategy, cls(cls.params_model.model_validate(cfg.params)))
        except Exception as exc:
            raise PluginError(
                f"plug-in {key!r} (revision {cfg.revision}) failed to start: {type(exc).__name__}: {exc}"
            ) from exc

    def instance(self, key: str) -> tuple[OptionStrategy, OptionStrategyConfigView]:
        """The plug-in with its current settings (enabled or not); PluginError if its params don't
        validate or it fails to start."""
        cfg = self.current(key)
        return self._build(key, cfg), cfg

    def enabled(self) -> list[tuple[OptionStrategy, OptionStrategyConfigView]]:
        """Every enabled plug-in that starts, in key order. A broken one is logged and skipped."""
        out: list[tuple[OptionStrategy, OptionStrategyConfigView]] = []
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
                s.execute(
                    select(m.OptionStrategyConfig.id).where(m.OptionStrategyConfig.strategy_key == key)
                ).scalars()
            )

    def config_key(self, config_id: int) -> str | None:
        with self._factory() as s:
            return s.execute(
                select(m.OptionStrategyConfig.strategy_key).where(m.OptionStrategyConfig.id == config_id)
            ).scalar_one_or_none()
