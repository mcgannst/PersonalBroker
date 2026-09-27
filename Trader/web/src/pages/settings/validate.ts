// Client-side checks for form fields generated from `FieldOut` descriptors (P4-T15). The server is the
// authority; these only stop an obviously bad value before it is sent and say what the rule is.
// Decimals are compared as strings (exactly), never through floating point.
import { isApiError } from "../../api/client";
import type { FieldOut } from "../../api/types";

const DECIMAL_RE = /^[+-]?(\d+\.?\d*|\.\d+)$/;

/** True when `s` is a plain decimal number such as `0.02`, `-1`, `.5`. */
export function isDecimalString(s: unknown): s is string {
  return typeof s === "string" && DECIMAL_RE.test(s.trim());
}

function splitDecimal(s: string): { neg: boolean; int: string; frac: string } {
  let t = s.trim();
  let neg = false;
  if (t.startsWith("+") || t.startsWith("-")) {
    neg = t.startsWith("-");
    t = t.slice(1);
  }
  const [intPart = "", fracPart = ""] = t.split(".");
  const int = intPart.replace(/^0+/, "") || "0";
  const frac = fracPart.replace(/0+$/, "");
  if (int === "0" && frac === "") neg = false; // -0 is 0
  return { neg, int, frac };
}

/** Compares two decimal strings exactly: -1, 0 or 1. Both must pass `isDecimalString`. */
export function compareDecimal(a: string, b: string): -1 | 0 | 1 {
  const x = splitDecimal(a);
  const y = splitDecimal(b);
  const width = Math.max(x.frac.length, y.frac.length);
  const scaled = (d: { neg: boolean; int: string; frac: string }) => {
    const v = BigInt(d.int + d.frac.padEnd(width, "0"));
    return d.neg ? -v : v;
  };
  const vx = scaled(x);
  const vy = scaled(y);
  return vx < vy ? -1 : vx > vy ? 1 : 0;
}

/** The range rule of a field, for example "Must be more than 0 and at most 0.10"; null when unbounded. */
function rangeText(field: FieldOut): string | null {
  const parts: string[] = [];
  if (field.minimum !== null) parts.push(`${field.exclusive_minimum ? "more than" : "at least"} ${field.minimum}`);
  if (field.maximum !== null) parts.push(`${field.exclusive_maximum ? "less than" : "at most"} ${field.maximum}`);
  return parts.length ? `Must be ${parts.join(" and ")}` : null;
}

/** The rule a field's value must follow, in words (shown when the value breaks it). */
export function ruleText(field: FieldOut): string | null {
  switch (field.kind) {
    case "decimal":
    case "integer":
    case "number":
      return rangeText(field);
    case "string":
      return field.pattern ? `Must match the pattern ${field.pattern}` : null;
    case "enum":
      return field.enum ? `Choose one of: ${field.enum.join(", ")}` : null;
    case "enum_list":
      return field.item_enum ? `Choose from: ${field.item_enum.join(", ")}` : null;
    default:
      return null;
  }
}

function inRange(field: FieldOut, cmp: (bound: string) => -1 | 0 | 1): boolean {
  if (field.minimum !== null) {
    const c = cmp(field.minimum);
    if (c < 0 || (c === 0 && field.exclusive_minimum)) return false;
  }
  if (field.maximum !== null) {
    const c = cmp(field.maximum);
    if (c > 0 || (c === 0 && field.exclusive_maximum)) return false;
  }
  return true;
}

function numberCmp(value: number): (bound: string) => -1 | 0 | 1 {
  return (bound) => {
    const b = Number(bound);
    return value < b ? -1 : value > b ? 1 : 0;
  };
}

function patternMatches(pattern: string, value: string): boolean {
  try {
    return new RegExp(pattern).test(value);
  } catch {
    return true; // a pattern JavaScript cannot parse is left to the server
  }
}

/** The message to show when `value` breaks the field's rules, or null when it is acceptable. */
export function validateValue(field: FieldOut, value: unknown): string | null {
  if (value === null || value === undefined) return field.nullable ? null : "A value is required";
  switch (field.kind) {
    case "decimal": {
      if (!isDecimalString(value)) return "Must be a number";
      const v = value.trim();
      return inRange(field, (bound) => (isDecimalString(bound) ? compareDecimal(v, bound) : 0)) ? null : ruleText(field);
    }
    case "integer":
      if (typeof value !== "number" || !Number.isInteger(value)) return "Must be a whole number";
      return inRange(field, numberCmp(value)) ? null : ruleText(field);
    case "number":
      if (typeof value !== "number" || !Number.isFinite(value)) return "Must be a number";
      return inRange(field, numberCmp(value)) ? null : ruleText(field);
    case "boolean":
      return typeof value === "boolean" ? null : "Must be on or off";
    case "enum":
      if (field.enum && !field.enum.includes(String(value))) return ruleText(field);
      return null;
    case "string":
      if (typeof value !== "string") return "Must be text";
      if (field.pattern && !patternMatches(field.pattern, value)) return ruleText(field);
      return null;
    case "string_list":
      if (!Array.isArray(value) || value.some((x) => typeof x !== "string")) return "Must be a list";
      return null;
    case "enum_list":
      if (!Array.isArray(value)) return "Must be a list";
      if (field.item_enum && value.some((x) => !field.item_enum!.includes(String(x)))) return ruleText(field);
      return null;
    default:
      return null;
  }
}

/** Structural equality of JSON values (what the server sent vs. what the form holds). */
export function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** A value as short text: lists joined, booleans as Yes/No, null as "none". */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "none";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "(empty)";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** The value to send: decimals trimmed, everything else as it is. */
export function wireValue(field: FieldOut, value: unknown): unknown {
  return field.kind === "decimal" && typeof value === "string" ? value.trim() : value;
}

/**
 * Splits a 422 `ApiError`'s field messages by field name: a message goes to the last string in its `loc`
 * that is one of `names`; the rest are `other`. Any other error gives no messages.
 */
export function fieldErrorsFor(error: unknown, names: readonly string[]): { fields: Record<string, string[]>; other: string[] } {
  const fields: Record<string, string[]> = {};
  const other: string[] = [];
  if (!isApiError(error) || !error.fields) return { fields, other };
  for (const fe of error.fields) {
    const name = [...fe.loc].reverse().find((part): part is string => typeof part === "string" && names.includes(part));
    if (name) (fields[name] ??= []).push(fe.msg);
    else other.push(fe.msg);
  }
  return { fields, other };
}
