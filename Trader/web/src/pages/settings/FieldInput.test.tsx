import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { FieldOut, SettingOut } from "../../api/types";
import { orbSipStrategy, settingsItems } from "../../test/fixtures";
import { FieldInput } from "./FieldInput";

function settingField(key: string): SettingOut {
  const s = settingsItems.find((x) => x.key === key);
  if (!s) throw new Error(`no fixture setting ${key}`);
  return s;
}

function strategyField(name: string): FieldOut {
  const fo = orbSipStrategy.fields.find((x) => x.name === name);
  if (!fo) throw new Error(`no fixture field ${name}`);
  return fo;
}

function Harness({ field, initial, onValue }: { field: FieldOut; initial: unknown; onValue: (v: unknown) => void }) {
  const [value, setValue] = useState<unknown>(initial);
  return (
    <FieldInput
      id="f"
      field={field}
      value={value}
      onChange={(v) => {
        setValue(v);
        onValue(v);
      }}
    />
  );
}

function setup(field: FieldOut, initial: unknown) {
  const onValue = vi.fn();
  render(<Harness field={field} initial={initial} onValue={onValue} />);
  const last = () => onValue.mock.calls[onValue.mock.calls.length - 1]?.[0];
  return { onValue, last };
}

describe("FieldInput (acceptance test 2)", () => {
  it("decimal: a text input with inputmode decimal that sends a string", async () => {
    const s = settingField("risk_pct");
    const { last } = setup(s.field, s.value);
    const input = screen.getByLabelText("Risk Pct");
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveAttribute("inputmode", "decimal");
    expect(input).toHaveValue("0.02");
    await userEvent.clear(input);
    await userEvent.type(input, "0.03");
    expect(last()).toBe("0.03");
    expect(typeof last()).toBe("string");
    expect(screen.getByText("Fraction of equity risked per trade")).toBeInTheDocument();
  });

  it("decimal above the maximum shows the rule", async () => {
    const s = settingField("risk_pct");
    setup(s.field, s.value);
    const input = screen.getByLabelText("Risk Pct");
    await userEvent.clear(input);
    await userEvent.type(input, "0.5");
    expect(screen.getByText("Must be more than 0 and at most 0.10")).toBeInTheDocument();
  });

  it("integer: a number input that sends a number", async () => {
    const s = settingField("no_entry_before_close_minutes");
    const { last } = setup(s.field, s.value);
    const input = screen.getByLabelText("No Entry Before Close Minutes");
    expect(input).toHaveAttribute("type", "number");
    expect(input).toHaveAttribute("step", "1");
    await userEvent.clear(input);
    await userEvent.type(input, "45");
    expect(last()).toBe(45);
  });

  it("number: a number input that sends a number", async () => {
    const s = settingField("quote_poll_seconds");
    const { last } = setup(s.field, s.value);
    const input = screen.getByLabelText("Quote Poll Seconds");
    expect(input).toHaveAttribute("type", "number");
    await userEvent.clear(input);
    await userEvent.type(input, "2.5");
    expect(last()).toBe(2.5);
  });

  it("boolean: a checkbox that sends a boolean", async () => {
    const s = settingField("cash_account_mode");
    const { last } = setup(s.field, s.value);
    const box = screen.getByRole("checkbox", { name: "Cash Account Mode" });
    expect(box).toBeChecked();
    await userEvent.click(box);
    expect(last()).toBe(false);
  });

  it("enum: a select that sends the option", async () => {
    const s = settingField("approval_mode");
    const { last } = setup(s.field, s.value);
    const select = screen.getByRole("combobox", { name: "Approval Mode" });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["manual", "auto"]);
    await userEvent.selectOptions(select, "auto");
    expect(last()).toBe("auto");
  });

  it("string: a text input with the pattern", async () => {
    const s = settingField("universe.finviz_filters");
    const { last } = setup(s.field, s.value);
    const input = screen.getByLabelText("Universe Finviz Filters");
    expect(input).toHaveAttribute("pattern", "^[a-z0-9_.]+(,[a-z0-9_.]+)*$");
    await userEvent.clear(input);
    await userEvent.type(input, "BAD FILTER");
    expect(last()).toBe("BAD FILTER");
    expect(screen.getByText("Must match the pattern ^[a-z0-9_.]+(,[a-z0-9_.]+)*$")).toBeInTheDocument();
  });

  it("string_list: comma-separated text that sends an array", async () => {
    const s = settingField("scheduler.always_fire_late");
    const { last } = setup(s.field, s.value);
    const input = screen.getByLabelText("Scheduler Always Fire Late");
    expect(input).toHaveValue("flatten, entry_cancel, overlay_decision");
    await userEvent.clear(input);
    await userEvent.type(input, "flatten, entry_cancel");
    expect(last()).toEqual(["flatten", "entry_cancel"]);
    expect(input).toHaveValue("flatten, entry_cancel");
  });

  it("enum_list: one checkbox per item that sends an array in item order", async () => {
    const s = settingField("markets_enabled");
    const { last } = setup(s.field, s.value);
    const group = screen.getByRole("group", { name: "Markets Enabled" });
    const us = within(group).getByRole("checkbox", { name: "US" });
    const tsx = within(group).getByRole("checkbox", { name: "TSX" });
    expect(us).toBeChecked();
    expect(tsx).not.toBeChecked();
    await userEvent.click(tsx);
    expect(last()).toEqual(["US", "TSX"]);
    await userEvent.click(us);
    expect(last()).toEqual(["TSX"]);
  });

  it("nullable: a none checkbox sends null and restores the last value", async () => {
    const fo = strategyField("entry_cancel_at");
    const { last } = setup(fo, "open+120m");
    const input = screen.getByLabelText("Entry Cancel At");
    const none = screen.getByRole("checkbox", { name: "None" });
    expect(none).not.toBeChecked();
    await userEvent.click(none);
    expect(last()).toBeNull();
    expect(input).toBeDisabled();
    await userEvent.click(none);
    expect(last()).toBe("open+120m");
    expect(input).toBeEnabled();
  });

  it("shows server messages under the input", () => {
    const s = settingField("risk_pct");
    render(<FieldInput id="r" field={s.field} value="0.02" onChange={() => {}} errors={["Input should be less than 0.1"]} />);
    expect(screen.getByText("Input should be less than 0.1")).toBeInTheDocument();
  });
});
