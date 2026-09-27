import { describe, expect, it } from "vitest";

import type { FieldOut } from "../../api/types";
import { compareDecimal, fieldErrorsFor, ruleText, sameValue, validateValue } from "./validate";
import { ApiError } from "../../api/client";

function f(partial: Partial<FieldOut> & Pick<FieldOut, "kind">): FieldOut {
  return {
    name: "x",
    title: "X",
    description: null,
    default: null,
    minimum: null,
    maximum: null,
    exclusive_minimum: false,
    exclusive_maximum: false,
    enum: null,
    item_enum: null,
    pattern: null,
    nullable: false,
    ...partial,
  };
}

describe("compareDecimal", () => {
  it("compares decimal strings exactly", () => {
    expect(compareDecimal("0.5", "0.10")).toBe(1);
    expect(compareDecimal("0.10", "0.1")).toBe(0);
    expect(compareDecimal("-1", "0")).toBe(-1);
    expect(compareDecimal("-0.5", "-0.25")).toBe(-1);
    expect(compareDecimal(".5", "0.5")).toBe(0);
    expect(compareDecimal("10000000", "9999999.9999")).toBe(1);
    expect(compareDecimal("0.1000000000000000000001", "0.1")).toBe(1);
  });
});

describe("validateValue and ruleText", () => {
  const risk = f({ kind: "decimal", minimum: "0", exclusive_minimum: true, maximum: "0.10" });

  it("checks decimal ranges and format", () => {
    expect(validateValue(risk, "0.02")).toBeNull();
    expect(validateValue(risk, "0.10")).toBeNull();
    expect(validateValue(risk, "0.5")).toBe("Must be more than 0 and at most 0.10");
    expect(validateValue(risk, "0")).toBe("Must be more than 0 and at most 0.10");
    expect(validateValue(risk, "abc")).toBe("Must be a number");
    expect(validateValue(risk, "")).toBe("Must be a number");
    expect(ruleText(risk)).toBe("Must be more than 0 and at most 0.10");
  });

  it("checks integers and numbers", () => {
    const top = f({ kind: "integer", minimum: "1", maximum: "200" });
    expect(validateValue(top, 10)).toBeNull();
    expect(validateValue(top, 1.5)).toBe("Must be a whole number");
    expect(validateValue(top, 0)).toBe("Must be at least 1 and at most 200");
    expect(validateValue(top, "")).toBe("Must be a whole number");
    const poll = f({ kind: "number", minimum: "1", maximum: "60", exclusive_maximum: true });
    expect(validateValue(poll, 2.5)).toBeNull();
    expect(validateValue(poll, 60)).toBe("Must be at least 1 and less than 60");
  });

  it("checks patterns, enums, lists and null", () => {
    const pat = f({ kind: "string", pattern: "^[A-Z][A-Z0-9.\\-]{0,9}$" });
    expect(validateValue(pat, "SPY")).toBeNull();
    expect(validateValue(pat, "spy")).toBe("Must match the pattern ^[A-Z][A-Z0-9.\\-]{0,9}$");
    const en = f({ kind: "enum", enum: ["skip", "trade"] });
    expect(validateValue(en, "trade")).toBeNull();
    expect(validateValue(en, "other")).toBe("Choose one of: skip, trade");
    const el = f({ kind: "enum_list", item_enum: ["US", "TSX"] });
    expect(validateValue(el, ["US", "TSX"])).toBeNull();
    expect(validateValue(el, ["XX"])).toBe("Choose from: US, TSX");
    const sl = f({ kind: "string_list" });
    expect(validateValue(sl, ["a", "b"])).toBeNull();
    expect(validateValue(sl, "a")).toBe("Must be a list");
    expect(validateValue(f({ kind: "boolean" }), true)).toBeNull();
    expect(validateValue(f({ kind: "string" }), null)).toBe("A value is required");
    expect(validateValue(f({ kind: "string", nullable: true }), null)).toBeNull();
  });
});

describe("sameValue and fieldErrorsFor", () => {
  it("compares JSON values structurally", () => {
    expect(sameValue(["a", "b"], ["a", "b"])).toBe(true);
    expect(sameValue(["a"], ["a", "b"])).toBe(false);
    expect(sameValue(null, null)).toBe(true);
    expect(sameValue(10, "10")).toBe(false);
  });

  it("maps 422 field messages by their last named location", () => {
    const err = new ApiError(422, "validation_error", "Invalid input", [
      { loc: ["body", "params", "top_n"], msg: "Input should be greater than 0" },
      { loc: ["body"], msg: "Something else" },
    ]);
    const byField = fieldErrorsFor(err, ["top_n", "price_min"]);
    expect(byField.fields).toEqual({ top_n: ["Input should be greater than 0"] });
    expect(byField.other).toEqual(["Something else"]);
    expect(fieldErrorsFor(new Error("x"), ["top_n"])).toEqual({ fields: {}, other: [] });
  });
});
