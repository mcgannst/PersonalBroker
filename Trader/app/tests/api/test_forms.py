"""P4-T8 acceptance test 6 (and the groups): `trader.api.forms` turns pydantic fields into `FieldOut` form
descriptors, so the web renders forms without parsing JSON Schema quirks."""

from decimal import Decimal
from typing import Annotated, Literal

from annotated_types import Ge, Gt, Le, Lt
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from trader.api import forms
from trader.api.forms import field_out, group_of, model_fields_out
from trader.settings_store import RuntimeSettings
from trader.strategies.orb_sip import OrbSipParams

# --- field_out: one case per kind ---------------------------------------------------------------------------


def test_decimal_bounds_are_exact_strings_with_exclusive_minimum() -> None:
    f = field_out("risk_pct", {}, Annotated[Decimal, Gt(0), Le(Decimal("0.10"))], Decimal("0.02"))
    assert f.kind == "decimal" and f.name == "risk_pct"
    assert (f.minimum, f.exclusive_minimum, f.maximum, f.exclusive_maximum) == ("0", True, "0.10", False)
    assert f.default == "0.02"  # JSON mode: a Decimal is a string on the wire
    assert f.nullable is False and f.pattern is None and f.enum is None


def test_integer_with_exclusive_maximum() -> None:
    f = field_out("n", {}, Annotated[int, Ge(1), Lt(200)], 20)
    assert f.kind == "integer"
    assert (f.minimum, f.exclusive_minimum, f.maximum, f.exclusive_maximum) == ("1", False, "200", True)
    assert f.default == 20


def test_float_is_number_and_bool_is_boolean() -> None:
    num = field_out("quote_poll_seconds", {}, Annotated[float, Ge(1.0), Le(60)], 2.0)
    assert (num.kind, num.minimum, num.maximum, num.default) == ("number", "1.0", "60", 2.0)
    flag = field_out("cash_account_mode", {}, bool, True)
    assert (flag.kind, flag.default, flag.minimum, flag.maximum) == ("boolean", True, None, None)


def test_literal_is_enum() -> None:
    f = field_out("approval_mode", {}, Literal["manual", "auto"], "manual")
    assert f.kind == "enum" and f.enum == ["manual", "auto"] and f.nullable is False


def test_string_with_pattern_from_the_schema_or_the_annotation() -> None:
    from_schema = field_out("benchmark", {"type": "string", "pattern": "^[A-Z]+$"}, str, "SPY")
    assert (from_schema.kind, from_schema.pattern, from_schema.default) == ("string", "^[A-Z]+$", "SPY")
    from_annotation = field_out("t", {}, Annotated[str, StringConstraints(pattern="^[a-z]+$")], "x")
    assert (from_annotation.kind, from_annotation.pattern) == ("string", "^[a-z]+$")


def test_lists_of_str_and_of_literal() -> None:
    ticker = Annotated[str, StringConstraints(pattern="^[A-Z]+$")]
    strings = field_out("universe.extra_symbols", {}, list[ticker], ["SPY"])
    assert strings.kind == "string_list" and strings.item_enum is None and strings.default == ["SPY"]
    enums = field_out("markets_enabled", {}, list[Literal["US", "TSX"]], ["US"])
    assert enums.kind == "enum_list" and enums.item_enum == ["US", "TSX"] and enums.enum is None


def test_nullable_string_and_nullable_literal() -> None:
    s = field_out("entry_cancel_at", {}, str | None, "open+120m")
    assert (s.kind, s.nullable, s.default) == ("string", True, "open+120m")
    lit = field_out("mode", {}, Literal["a", "b"] | None, None)
    assert (lit.kind, lit.nullable, lit.enum, lit.default) == ("enum", True, ["a", "b"], None)


def test_an_unknown_annotation_is_a_string() -> None:
    f = field_out("odd", {"type": "object"}, dict[str, int], {})
    assert f.kind == "string" and f.nullable is False


def test_bounds_fall_back_to_the_json_schema() -> None:
    # A Decimal's JSON Schema is an anyOf of number and string; the bounds are in the number branch.
    schema = {"anyOf": [{"type": "number", "exclusiveMinimum": 0, "maximum": 5}, {"type": "string"}]}
    f = field_out("x", schema, Decimal, Decimal("1"))
    assert (f.minimum, f.exclusive_minimum, f.maximum, f.exclusive_maximum) == ("0", True, "5", False)
    assert f.pattern is None  # never the Decimal string regex


def test_title_and_description() -> None:
    f = field_out("fx.fee_pct", {"title": "Fx.Fee Pct", "description": "FX fee."}, Decimal, Decimal("0"))
    assert f.title == "Fx fee pct" and f.description == "FX fee."
    custom = field_out("x", {"title": "Custom title"}, int, 1)
    assert custom.title == "Custom title"


# --- model_fields_out ---------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    size: Decimal = Field(Decimal("1.5"), gt=0, le=Decimal("10.00"), alias="shape.size")
    tags: list[str] = Field(default_factory=lambda: ["a"], alias="shape.tags")
    note: str | None = Field(None, description="Free text.", alias="shape.note")


def test_model_fields_out_by_alias_and_by_name() -> None:
    by_alias = {f.name: f for f in model_fields_out(_Model, by_alias=True)}
    assert list(by_alias) == ["shape.size", "shape.tags", "shape.note"]
    assert (by_alias["shape.size"].kind, by_alias["shape.size"].maximum) == ("decimal", "10.00")
    assert by_alias["shape.size"].exclusive_minimum is True and by_alias["shape.size"].default == "1.5"
    assert by_alias["shape.tags"].default == ["a"]  # the default factory is called
    note = by_alias["shape.note"]
    assert note.nullable and note.default is None and note.description == "Free text."
    assert [f.name for f in model_fields_out(_Model, by_alias=False)] == ["size", "tags", "note"]


def test_runtime_settings_descriptors() -> None:
    fields = {f.name: f for f in model_fields_out(RuntimeSettings, by_alias=True)}
    assert set(fields) == {(f.alias or n) for n, f in RuntimeSettings.model_fields.items()}
    assert fields["risk_pct"].maximum == "0.10" and fields["risk_pct"].exclusive_minimum is True
    assert fields["scheduler.always_fire_late"].kind == "string_list"
    assert fields["universe.finviz_filters"].pattern is not None
    assert fields["claude.model"].kind == "enum"


def test_orb_sip_descriptors() -> None:
    fields = {f.name: f for f in model_fields_out(OrbSipParams, by_alias=False)}
    assert fields["top_n"].kind == "integer" and (fields["top_n"].minimum, fields["top_n"].maximum) == (
        "1",
        "200",
    )
    assert fields["price_min"].kind == "decimal" and fields["price_min"].exclusive_minimum is True
    assert fields["require_catalyst"].kind == "boolean"


# --- groups -------------------------------------------------------------------------------------------------


def test_group_of_exact_before_prefix() -> None:
    assert group_of("approval_mode") == "Approvals"
    assert group_of("slippage_buffer") == "Risk"  # exact entry beats the "slippage_" prefix
    assert group_of("slippage_bps") == "Fill model"
    assert group_of("fx.fee_pct") == "Account"
    assert group_of("proposal_ttl_entry_seconds") == "Proposals"
    assert group_of("web.sse_poll_seconds") == "Web app"
    assert group_of("something.new") == forms.OTHER_GROUP


def test_every_runtime_setting_has_a_group() -> None:
    keys = [(f.alias or n) for n, f in RuntimeSettings.model_fields.items()]
    assert [k for k in keys if group_of(k) == forms.OTHER_GROUP] == []
