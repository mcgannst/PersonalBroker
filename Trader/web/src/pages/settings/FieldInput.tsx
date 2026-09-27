// One form control generated from a `FieldOut` descriptor (P4-T15; descriptors from P4-T8 `trader.api.forms`).
// decimal → text with inputmode decimal (a string); integer/number → number input (a number);
// boolean → checkbox; enum → select; string → text with the pattern; string_list → comma-separated text
// (an array); enum_list → checkboxes (an array); nullable adds a "None" checkbox (null).
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import type { FieldOut } from "../../api/types";
import { validateValue } from "./validate";

/** A checkbox and its text side by side (the global `label` style stacks a label's children). */
export const CHECK_LABEL: CSSProperties = { flexDirection: "row", alignItems: "center", gap: 8 };

export interface FieldInputProps {
  /** A unique id prefix for this control (labels and messages hang off it). */
  id: string;
  field: FieldOut;
  value: unknown;
  onChange: (value: unknown) => void;
  /** The server's 422 messages for this field. */
  errors?: readonly string[];
  disabled?: boolean;
  /** Overrides the label (the descriptor's title by default). */
  label?: ReactNode;
}

function emptyFor(field: FieldOut): unknown {
  switch (field.kind) {
    case "boolean":
      return false;
    case "string_list":
    case "enum_list":
      return [];
    case "enum":
      return field.enum?.[0] ?? "";
    default:
      return "";
  }
}

function splitList(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s !== "");
}

function joinList(value: unknown): string {
  return Array.isArray(value) ? value.join(", ") : "";
}

/** A text box for a list: keeps what Stephen types (commas, spaces) and reports the parsed array. */
function ListText({
  id,
  value,
  onChange,
  disabled,
  describedBy,
  invalid,
}: {
  id: string;
  value: unknown;
  onChange: (v: string[]) => void;
  disabled: boolean;
  describedBy: string | undefined;
  invalid: boolean;
}) {
  const [text, setText] = useState(() => joinList(value));
  useEffect(() => {
    // Follow a value changed from outside (a refetch), but never fight the text being typed.
    if (JSON.stringify(splitList(text)) !== JSON.stringify(value)) setText(joinList(value));
  }, [JSON.stringify(value)]); // keyed on the value's content, not its identity
  return (
    <input
      id={id}
      type="text"
      value={text}
      disabled={disabled}
      aria-describedby={describedBy}
      aria-invalid={invalid || undefined}
      placeholder="comma-separated"
      onChange={(e) => {
        setText(e.target.value);
        onChange(splitList(e.target.value));
      }}
    />
  );
}

export function FieldInput({ id, field, value, onChange, errors = [], disabled = false, label }: FieldInputProps) {
  const isNull = field.nullable && value === null;
  const lastNonNull = useRef<unknown>(value === null ? field.default ?? emptyFor(field) : value);
  if (value !== null) lastNonNull.current = value;
  const shown = isNull ? lastNonNull.current : value;
  const controlDisabled = disabled || isNull;

  const rule = validateValue(field, value);
  const invalid = rule !== null || errors.length > 0;
  const controlId = `${id}-input`;
  const descId = field.description ? `${id}-desc` : undefined;
  const msgId = invalid ? `${id}-msg` : undefined;
  const describedBy = [descId, msgId].filter(Boolean).join(" ") || undefined;
  const title = label ?? field.title;

  let control: ReactNode;
  switch (field.kind) {
    case "decimal":
      control = (
        <input
          id={controlId}
          type="text"
          inputMode="decimal"
          autoComplete="off"
          value={typeof shown === "string" ? shown : shown === null || shown === undefined ? "" : String(shown)}
          disabled={controlDisabled}
          aria-describedby={describedBy}
          aria-invalid={invalid || undefined}
          onChange={(e) => onChange(e.target.value)}
        />
      );
      break;
    case "integer":
    case "number":
      control = (
        <input
          id={controlId}
          type="number"
          step={field.kind === "integer" ? 1 : "any"}
          min={field.minimum ?? undefined}
          max={field.maximum ?? undefined}
          value={typeof shown === "number" ? String(shown) : typeof shown === "string" ? shown : ""}
          disabled={controlDisabled}
          aria-describedby={describedBy}
          aria-invalid={invalid || undefined}
          onChange={(e) => {
            const raw = e.target.value;
            const n = Number(raw);
            onChange(raw.trim() === "" || !Number.isFinite(n) ? raw : n);
          }}
        />
      );
      break;
    case "boolean":
      control = (
        <input
          id={controlId}
          type="checkbox"
          checked={shown === true}
          disabled={controlDisabled}
          aria-describedby={describedBy}
          onChange={(e) => onChange(e.target.checked)}
        />
      );
      break;
    case "enum": {
      const options = field.enum ?? [];
      const current = typeof shown === "string" ? shown : "";
      control = (
        <select
          id={controlId}
          value={current}
          disabled={controlDisabled}
          aria-describedby={describedBy}
          aria-invalid={invalid || undefined}
          onChange={(e) => onChange(e.target.value)}
        >
          {!options.includes(current) && <option value={current}>{current || "(choose)"}</option>}
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      );
      break;
    }
    case "string_list":
      control = (
        <ListText id={controlId} value={shown} onChange={onChange} disabled={controlDisabled} describedBy={describedBy} invalid={invalid} />
      );
      break;
    case "enum_list": {
      const items = field.item_enum ?? [];
      const selected = Array.isArray(shown) ? shown.map(String) : [];
      control = (
        <div className="row" id={controlId}>
          {items.map((item) => (
            <label key={item} style={CHECK_LABEL}>
              <input
                type="checkbox"
                checked={selected.includes(item)}
                disabled={controlDisabled}
                onChange={(e) => {
                  const next = e.target.checked ? [...selected, item] : selected.filter((x) => x !== item);
                  onChange(items.filter((x) => next.includes(x)));
                }}
              />{" "}
              {item}
            </label>
          ))}
        </div>
      );
      break;
    }
    default:
      control = (
        <input
          id={controlId}
          type="text"
          autoComplete="off"
          pattern={field.pattern ?? undefined}
          value={typeof shown === "string" ? shown : shown === null || shown === undefined ? "" : String(shown)}
          disabled={controlDisabled}
          aria-describedby={describedBy}
          aria-invalid={invalid || undefined}
          onChange={(e) => onChange(e.target.value)}
        />
      );
  }

  const noneBox = field.nullable ? (
    <label className="small" style={CHECK_LABEL}>
      <input
        type="checkbox"
        checked={isNull}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked ? null : lastNonNull.current ?? emptyFor(field))}
      />{" "}
      None
    </label>
  ) : null;

  const messages = (
    <>
      {field.description && (
        <p id={descId} className="small muted">
          {field.description}
        </p>
      )}
      {invalid && (
        <div id={msgId} className="small tone-bad" role={errors.length ? "alert" : undefined}>
          {rule !== null && <p>{rule}</p>}
          {errors.map((m, i) => (
            <p key={i}>{m}</p>
          ))}
        </div>
      )}
    </>
  );

  if (field.kind === "enum_list") {
    return (
      <fieldset className="field stack" aria-describedby={describedBy}>
        <legend>{title}</legend>
        {control}
        {noneBox}
        {messages}
      </fieldset>
    );
  }
  if (field.kind === "boolean") {
    return (
      <div className="field stack">
        <label htmlFor={controlId} style={CHECK_LABEL}>
          {control} {title}
        </label>
        {noneBox}
        {messages}
      </div>
    );
  }
  return (
    <div className="field stack">
      <label htmlFor={controlId}>{title}</label>
      {control}
      {noneBox}
      {messages}
    </div>
  );
}
