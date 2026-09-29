"""P4-T18 acceptance test 3: `web/src/api/types.ts` mirrors `trader/api/schemas.py`.

For every pydantic model in `trader.api.schemas`, types.ts declares a type of the same name whose field names
equal the model's, and whose nullability agrees: a field that accepts None is `X | null` in TypeScript, and
one that doesn't is not. An optional TypeScript field (`name?:`) must have a default in Python. Every value of
the `Topic`, `FieldKind`, `TimelineStatus`, `ManualJob` and `SessionPhase` literals (and, P6-T9,
`DecisionStage`, `DecisionOutcome` and `CheckOp`, and DB-T1's live dashboard literals) appears in the TS
union.
"""

import inspect
import re
import types
import typing
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import pytest
from pydantic import BaseModel

from trader.api import schemas
from trader.market.sessions import SessionPhase

TYPES_TS = Path(__file__).resolve().parents[3] / "web" / "src" / "api" / "types.ts"

# `export interface Name {` / `export interface Name<T> {` / `export type Name = {` ... up to a closing brace
# at the start of a line.
_BLOCK = re.compile(
    r"^export (?:interface|type) (\w+)(?:<[^>]*>)?\s*(?:=\s*)?\{\n(.*?)^\}", re.MULTILINE | re.DOTALL
)
_FIELD = re.compile(r"^\s{2}(\w+)(\??):\s*(.+?);\s*$")
_UNION = re.compile(r"^export type (\w+)\s*=\s*(.*?);", re.MULTILINE | re.DOTALL)


class TsField(typing.NamedTuple):
    optional: bool
    type: str

    @property
    def nullable(self) -> bool:
        return "null" in _top_level_union(self.type)


def _top_level_union(ts_type: str) -> list[str]:
    """The members of a TypeScript union, split only at the top level (not inside <...>, (...) or [...])."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in ts_type:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    parts.append(current.strip())
    return [p for p in parts if p]


def ts_types() -> dict[str, dict[str, TsField]]:
    text = TYPES_TS.read_text(encoding="utf-8")
    out: dict[str, dict[str, TsField]] = {}
    for name, body in _BLOCK.findall(text):
        fields: dict[str, TsField] = {}
        for line in body.splitlines():
            if line.strip().startswith(("//", "/*", "*")) or not line.strip():
                continue
            match = _FIELD.match(line)
            assert match, f"types.ts {name}: cannot parse field line {line!r}"
            fields[match.group(1)] = TsField(match.group(2) == "?", match.group(3))
        out[name] = fields
    return out


def ts_unions() -> dict[str, set[str]]:
    text = TYPES_TS.read_text(encoding="utf-8")
    return {
        name: set(re.findall(r'"([^"]+)"', body)) for name, body in _UNION.findall(text) if "{" not in body
    }


def schema_models() -> list[type[BaseModel]]:
    models = [
        obj
        for _, obj in inspect.getmembers(schemas, inspect.isclass)
        if issubclass(obj, BaseModel)
        and obj.__module__ == schemas.__name__
        and obj is not schemas.ApiModel
        and obj.__pydantic_generic_metadata__["origin"] is None  # `Items`, not `Items[EventOut]` (routers)
    ]
    assert len(models) > 50, [m.__name__ for m in models]
    return models


def accepts_none(annotation: Any) -> bool:
    if annotation is None or annotation is type(None):
        return True
    origin = get_origin(annotation)
    if origin is typing.Annotated:
        return accepts_none(get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return any(accepts_none(arg) for arg in get_args(annotation))
    if origin is Literal:
        return None in get_args(annotation)
    return annotation is Any


def test_types_ts_exists_and_parses() -> None:
    parsed = ts_types()
    assert "ProposalOut" in parsed and "Items" in parsed
    assert parsed["ProposalOut"]["decided_at"] == TsField(False, "IsoTime | null")


@pytest.mark.parametrize("model", schema_models(), ids=lambda m: m.__name__)
def test_every_schema_model_is_mirrored_with_the_same_fields_and_nullability(model: type[BaseModel]) -> None:
    ts = ts_types()
    name = model.__name__
    assert name in ts, f"types.ts has no type {name}"
    fields = ts[name]
    assert set(fields) == set(model.model_fields), f"{name}: field names differ"
    for field_name, info in model.model_fields.items():
        py_nullable = accepts_none(info.annotation)
        ts_field = fields[field_name]
        if info.annotation is Any:
            assert ts_field.type == "unknown", f"{name}.{field_name}: Any is `unknown` in TS"
            continue
        assert ts_field.nullable == py_nullable, (
            f"{name}.{field_name}: Python {'accepts' if py_nullable else 'refuses'} None, "
            f"TypeScript type is {ts_field.type!r}"
        )
        if ts_field.optional:
            assert not info.is_required(), f"{name}.{field_name}: optional in TS but required in Python"


@pytest.mark.parametrize(
    ("name", "literal"),
    [
        ("Topic", schemas.Topic),
        ("FieldKind", schemas.FieldKind),
        ("TimelineStatus", schemas.TimelineStatus),
        ("ManualJob", schemas.ManualJob),
        ("SessionPhase", SessionPhase),
        ("DecisionStage", schemas.DecisionStage),  # P6-T9
        ("DecisionOutcome", schemas.DecisionOutcome),
        ("CheckOp", schemas.CheckOp),
        ("PeriodKey", schemas.PeriodKey),  # DB-T1 (live dashboard)
        ("LiveRange", schemas.LiveRange),
        ("MarkState", schemas.MarkState),
        ("TradingState", schemas.TradingState),
        ("ActivityKind", schemas.ActivityKind),
        ("ActivityChip", schemas.ActivityChip),
        ("ActivityTone", schemas.ActivityTone),
        ("EquitySource", schemas.EquitySource),
        ("BarSource", schemas.BarSource),
        ("KillSwitchUnit", schemas.KillSwitchUnit),
        ("RejectionSource", schemas.RejectionSource),
    ],
)
def test_every_literal_value_is_in_the_ts_union(name: str, literal: Any) -> None:
    unions = ts_unions()
    assert name in unions, f"types.ts has no union {name}"
    assert unions[name] == set(get_args(literal))


def test_the_mirror_catches_a_nullability_mismatch() -> None:
    """The check itself works: a non-null TS type for a Python `X | None` field is caught."""
    assert TsField(False, "IsoDate | null").nullable
    assert not TsField(False, "IsoDate").nullable
    assert not TsField(False, "Record<string, number | null>").nullable  # nested null is not the field's
    assert accepts_none(schemas.JobLaunchOut.model_fields["session_date"].annotation)
