"""GET /api/strategies, PUT /api/strategies/{key} (P4-T8; BR-10, BR-22, SPEC §5, §11).

Each strategy plug-in's current settings, its JSON Schema and the form descriptors of its params model.
A change goes through `StrategyRegistry.update`: a new versioned revision with an audit row (actor
`web:<username>`). Disabling a strategy that owns an open position or a working order is allowed (it
then runs exits-only); `owns_open_positions` lets the web warn. The router is registered under `/api` by
`trader.api.routers.ROUTERS`.

A 422 never echoes an input value (the Web API contract): `safe_field_message` drops pydantic's
"Value error, " prefix and replaces any message that still contains a submitted value with a generic
text. The plug-in validators already leave the value out; this is the defence in depth for a plug-in that
does not (P5-GW fix round 1). `trader.api.routers.replays` uses it for the replay start form as well.
"""

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import structlog
from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import exists, or_, select

from trader.api.deps import ApiServices, CsrfUser, CurrentUser, Services, actor, live_run_id
from trader.api.errors import ApiError
from trader.api.forms import model_fields_out
from trader.api.schemas import Items, StrategyIn, StrategyOut
from trader.db import models as m
from trader.strategies.registry import StrategyConfigView

log = structlog.get_logger("api.strategies")
router = APIRouter(tags=["strategies"])

VALUE_ERROR_PREFIX = "Value error, "
GENERIC_MESSAGE = "invalid value"
RAW_MATCH_MIN = 6  # an unquoted submitted value this long found in a message counts as an echo


def input_leaves(value: Any) -> Iterator[str]:
    """Every scalar value (not key) in a submitted structure, as text."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, Mapping):
        for v in value.values():
            yield from input_leaves(v)
    elif isinstance(value, list | tuple | set | frozenset):
        for v in value:
            yield from input_leaves(v)
    else:
        yield str(value)


def safe_field_message(msg: str, inputs: Iterable[Any]) -> str:
    """`msg` without pydantic's "Value error, " prefix, or GENERIC_MESSAGE when it contains any submitted
    value (quoted, or unquoted and at least RAW_MATCH_MIN characters long)."""
    text = msg.removeprefix(VALUE_ERROR_PREFIX)
    for leaf in input_leaves(list(inputs)):
        if leaf and (repr(leaf) in text or (len(leaf) >= RAW_MATCH_MIN and leaf in text)):
            return GENERIC_MESSAGE
    return text


def _owns_open_positions(services: ApiServices, key: str, run_id: int) -> bool:
    """True when an open position or a working order of the live run came from any revision of `key`."""
    ids = services.registry.config_ids(key)
    if not ids:
        return False
    with services.core.factory() as s:
        open_position = exists().where(
            m.Position.run_id == run_id,
            m.Position.strategy_config_id.in_(ids),
            m.Position.closed_at.is_(None),
        )
        working_order = exists().where(
            m.Order.run_id == run_id,
            m.Order.strategy_config_id.in_(ids),
            m.Order.status == "working",
        )
        return bool(s.execute(select(or_(open_position, working_order))).scalar())


def _strategy_out(services: ApiServices, key: str, cfg: StrategyConfigView, run_id: int) -> StrategyOut:
    registry = services.registry
    cls = registry.plugin_class(key)
    with services.core.factory() as s:
        row = s.get(m.StrategyConfig, cfg.id)
        created_by = row.created_by if row is not None else None
    return StrategyOut(
        key=key,
        version=cfg.version,
        kind=cls.kind,
        enabled=cfg.enabled,
        revision=cfg.revision,
        params=cfg.params,
        schema=registry.json_schema(key),
        fields=model_fields_out(cls.params_model, by_alias=False),
        updated_at=cfg.created_at,
        updated_by=created_by,
        owns_open_positions=_owns_open_positions(services, key, run_id),
    )


@router.get("/strategies")
def get_strategies(services: Services, user: CurrentUser) -> Items[StrategyOut]:
    run_id = live_run_id(services)
    items: list[StrategyOut] = []
    for key in services.registry.keys():
        try:
            cfg = services.registry.current(key)
        except KeyError:  # no settings row yet: the worker's ensure_defaults() writes revision 1
            log.warning("api.strategy_without_settings", strategy=key)
            continue
        items.append(_strategy_out(services, key, cfg, run_id))
    return Items[StrategyOut](items=items)


@router.put("/strategies/{key}")
def put_strategy(key: str, body: StrategyIn, services: Services, user: CsrfUser) -> StrategyOut:
    if key not in services.registry.keys():
        raise ApiError(404, "not_found", "Unknown strategy")
    if not body.params and body.enabled is None:
        raise ApiError(
            422,
            "validation",
            "Nothing to change",
            fields=[{"loc": ["body"], "msg": "Send params or enabled"}],
        )
    try:
        cfg = services.registry.update(key, params=body.params, enabled=body.enabled, actor=actor(user))
    except ValidationError as exc:
        fields = [
            {"loc": ["params", *e["loc"]], "msg": safe_field_message(e["msg"], [e.get("input")])}
            for e in exc.errors()
        ]
        raise ApiError(422, "validation", "Invalid strategy settings", fields=fields) from None
    except KeyError:
        raise ApiError(409, "conflict", "The strategy has no settings yet") from None
    return _strategy_out(services, key, cfg, live_run_id(services))
