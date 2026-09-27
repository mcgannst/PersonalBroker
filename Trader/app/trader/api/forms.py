"""Form descriptors (`FieldOut`) for runtime settings and strategy parameters, so the web renders forms
without parsing JSON Schema quirks, and the Settings page's groups (P4-T8, BR-53, SPEC §13).

The kind comes from the field's Python annotation: `Decimal` -> `decimal`, `bool` -> `boolean`, `int` ->
`integer`, `float` -> `number`, a `Literal` -> `enum`, `str` -> `string`, `list[str]` -> `string_list`,
`list[Literal]` -> `enum_list`, and `X | None` -> X's kind with `nullable`. Anything else is a `string` (the
raw JSON Schema is still in `StrategyOut.schema`). Bounds come from the field's constraints (so a
`Decimal` bound keeps its exact digits, e.g. "0.10"), falling back to the JSON Schema.
"""

import re
import types
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic_core import PydanticUndefined, to_jsonable_python

from trader.api.schemas import FieldKind, FieldOut

# DB key, or key prefix ending in "." or "_", -> the Settings page group.
SETTING_GROUPS: Mapping[str, str] = {
    "approval_mode": "Approvals",
    "starting_cash": "Account",
    "starting_cash_currency": "Account",
    "account_currency": "Account",
    "fx.": "Account",
    "cash_account_mode": "Account",
    "markets_enabled": "Account",
    "risk_pct": "Risk",
    "slippage_buffer": "Risk",
    "no_entry_before_close_minutes": "Risk",
    "quote_poll_seconds": "Fill model",
    "stale_quote_seconds": "Fill model",
    "slippage_": "Fill model",
    "fees.": "Fill model",
    "proposal_ttl_": "Proposals",
    "stop_escalation_seconds": "Proposals",
    "auto_flatten_on_expiry": "Proposals",
    "killswitch.": "Kill switches",
    "claude.": "Claude",
    "premarket.": "Screening",
    "universe.": "Screening",
    "finviz.": "Screening",
    "open_bar.": "Screening",
    "worker.": "Worker and Telegram",
    "scheduler.": "Worker and Telegram",
    "telegram.": "Worker and Telegram",
    "preopen.": "Worker and Telegram",
    "postclose.": "Worker and Telegram",
    "web.": "Web app",
    "replay.": "Replay",
    "reports.": "Reports",
    "jobs.": "Operations",
    "logging.": "Operations",
}
OTHER_GROUP = "Other"  # a key no entry matches (none today; a test checks every setting has a group)
# The groups in the Settings page's order (their first appearance above), then OTHER_GROUP.
GROUP_ORDER: tuple[str, ...] = (*dict.fromkeys(SETTING_GROUPS.values()), OTHER_GROUP)

_BOUNDS = (
    ("gt", "minimum", True),
    ("ge", "minimum", False),
    ("lt", "maximum", True),
    ("le", "maximum", False),
)
_SCHEMA_BOUNDS = (
    ("exclusiveMinimum", "minimum", True),
    ("minimum", "minimum", False),
    ("exclusiveMaximum", "maximum", True),
    ("maximum", "maximum", False),
)


def _unwrap(annotation: Any) -> tuple[Any, list[Any]]:
    """`Annotated[X, *metadata]` -> (X, metadata); anything else -> (it, [])."""
    metadata: list[Any] = []
    while get_origin(annotation) is Annotated:
        args = get_args(annotation)
        annotation, metadata = args[0], [*metadata, *args[1:]]
    return annotation, metadata


def _strip_none(annotation: Any) -> tuple[Any, bool]:
    """`X | None` -> (X, True); anything else -> (it, False). A union of several types stays as it is."""
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) < len(get_args(annotation)) and len(args) == 1:
            return args[0], True
    return annotation, False


def _is_literal(annotation: Any) -> bool:
    return get_origin(annotation) is Literal


def _literal_values(annotation: Any) -> list[str]:
    return [str(v) for v in get_args(annotation)]


def _kind(base: Any) -> FieldKind:
    if _is_literal(base):
        return "enum"
    if base is bool:  # before int: bool is a subclass of int
        return "boolean"
    if base is Decimal:
        return "decimal"
    if base is int:
        return "integer"
    if base is float:
        return "number"
    if get_origin(base) is list:
        (item,) = get_args(base) or (Any,)
        item, _ = _unwrap(item)
        if _is_literal(item):
            return "enum_list"
        if item is str:
            return "string_list"
    return "string"


def _bound_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    return str(value)


def _branches(schema: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The schema itself and the non-null branches of its anyOf (a Decimal is number-or-string)."""
    out: list[Mapping[str, Any]] = [schema]
    for branch in schema.get("anyOf", []):
        if isinstance(branch, Mapping) and branch.get("type") != "null":
            out.append(branch)
    return out


def _bounds(metadata: list[Any], schema: Mapping[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for md in metadata:
        for attr, side, exclusive in _BOUNDS:
            text = _bound_text(getattr(md, attr, None))
            if text is not None and side not in found:
                found[side] = text
                found[f"exclusive_{side}"] = exclusive
    for branch in _branches(schema):
        for key, side, exclusive in _SCHEMA_BOUNDS:
            text = _bound_text(branch.get(key))
            if text is not None and side not in found:
                found[side] = text
                found[f"exclusive_{side}"] = exclusive
    return found


def _pattern(metadata: list[Any], schema: Mapping[str, Any]) -> str | None:
    for md in metadata:
        pattern = getattr(md, "pattern", None)
        if isinstance(pattern, str):
            return pattern
    for branch in _branches(schema):
        if isinstance(branch.get("pattern"), str) and branch.get("type", "string") == "string":
            return str(branch["pattern"])
    return None


def _auto_title(name: str) -> str:
    """The title pydantic generates for a field it was given no title for."""
    return name.title().replace("_", " ").strip()


def _title(name: str, schema: Mapping[str, Any]) -> str:
    given = schema.get("title")
    if isinstance(given, str) and given and given != _auto_title(name):
        return given
    words = re.sub(r"[._]+", " ", name).strip()
    return words[:1].upper() + words[1:] if words else name


def field_out(name: str, schema: Mapping[str, Any], annotation: Any, default: Any) -> FieldOut:
    """The form descriptor of one field. `schema` is the field's JSON Schema property (may be empty),
    `annotation` its Python type (`Annotated[...]` carries its constraints), `default` its default."""
    base, metadata = _unwrap(annotation)
    base, nullable = _strip_none(base)
    base, inner_metadata = _unwrap(base)
    metadata = [*metadata, *inner_metadata]
    kind = _kind(base)

    enum: list[str] | None = None
    item_enum: list[str] | None = None
    pattern: str | None = None
    bounds: dict[str, Any] = {}
    if kind == "enum":
        enum = _literal_values(base)
    elif kind == "enum_list":
        item_enum = _literal_values(_unwrap(get_args(base)[0])[0])
    elif kind == "string_list":
        item_schema = schema.get("items")
        pattern = _pattern(
            _unwrap(get_args(base)[0])[1], item_schema if isinstance(item_schema, Mapping) else {}
        )
    elif base is str:
        pattern = _pattern(metadata, schema)
    elif kind in ("decimal", "integer", "number"):
        bounds = _bounds(metadata, schema)

    description = schema.get("description")
    return FieldOut(
        name=name,
        kind=kind,
        title=_title(name, schema),
        description=description if isinstance(description, str) else None,
        default=None if default is PydanticUndefined else to_jsonable_python(default),
        enum=enum,
        item_enum=item_enum,
        pattern=pattern,
        nullable=nullable,
        **bounds,
    )


def model_fields_out(model: type[BaseModel], *, by_alias: bool) -> list[FieldOut]:
    """Every field of `model`, in declaration order, named by its alias (runtime settings: the DB key) or
    by its Python name (strategy parameters)."""
    properties: Mapping[str, Any] = model.model_json_schema(by_alias=by_alias).get("properties", {})
    out: list[FieldOut] = []
    for field_name, info in model.model_fields.items():
        name = (info.alias or field_name) if by_alias else field_name
        annotation: Any = info.annotation
        if info.metadata:
            annotation = Annotated[annotation, *info.metadata]
        schema = dict(properties.get(name, {}))
        if info.description and "description" not in schema:
            schema["description"] = info.description
        out.append(field_out(name, schema, annotation, info.get_default(call_default_factory=True)))
    return out


def group_of(key: str) -> str:
    """The group of a setting's DB key (an exact entry first, then the longest matching prefix)."""
    if key in SETTING_GROUPS:
        return SETTING_GROUPS[key]
    prefixes = [p for p in SETTING_GROUPS if p.endswith((".", "_")) and key.startswith(p)]
    return SETTING_GROUPS[max(prefixes, key=len)] if prefixes else OTHER_GROUP
