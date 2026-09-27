// P5-T8: the comparison table and its exact decimal differences (never through floating point).
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { emptyMetrics, metricsOut } from "../../test/fixtures";
import { CompareTable } from "./CompareTable";
import { decimalDiff, defaultRange, fieldPaths } from "./shared";
import { ApiError } from "../../api/client";

describe("decimalDiff", () => {
  it("subtracts exactly with the larger scale", () => {
    expect(decimalDiff("0.4000", "0.5000")).toBe("-0.1000");
    expect(decimalDiff("2.0000", "4.1")).toBe("-2.1000");
    expect(decimalDiff("0.1", "0.2")).toBe("-0.1");
    expect(decimalDiff("0.3", "0.1")).toBe("0.2");
    expect(decimalDiff("5", "4")).toBe("1");
    expect(decimalDiff("-0.0001", "0")).toBe("-0.0001");
    expect(decimalDiff("98765432109876.5449", "-0.0001")).toBe("98765432109876.5450");
    expect(decimalDiff("1.0", "1")).toBe("0.0");
  });

  it("is null for a null side or a value that is not a decimal", () => {
    expect(decimalDiff(null, "1")).toBeNull();
    expect(decimalDiff("1", undefined)).toBeNull();
    expect(decimalDiff("Infinity", "1")).toBeNull();
    expect(decimalDiff("1e3", "1")).toBeNull();
  });
});

describe("defaultRange", () => {
  it("is the 20 weekdays ending at the latest allowed session", () => {
    expect(defaultRange("2026-11-27")).toEqual({ from: "2026-11-02", to: "2026-11-27" });
    expect(defaultRange("2026-11-30")).toEqual({ from: "2026-11-03", to: "2026-11-30" });
  });
});

describe("fieldPaths", () => {
  it("joins the location after body with dots", () => {
    const err = new ApiError(422, "validation", "bad", [
      { loc: ["body", "overrides", "fees", "commission"], msg: "a" },
      { loc: ["body", "date_to"], msg: "b" },
      { loc: [], msg: "c" },
    ]);
    expect(fieldPaths(err)).toEqual({ "overrides.fees.commission": ["a"], date_to: ["b"], "": ["c"] });
    expect(fieldPaths(new Error("x"))).toEqual({});
  });
});

describe("CompareTable", () => {
  it("shows n/a where a side has no value, and the difference only when both have one", () => {
    render(<CompareTable replay={metricsOut} live={emptyMetrics} />);
    const table = screen.getByRole("table", { name: "Replay compared with live" });
    const row = within(table).getByRole("rowheader", { name: "Win rate" }).closest("tr")!;
    expect(within(row).getAllByRole("cell").map((c) => c.textContent)).toEqual(["50.00%", "n/a", "n/a"]);
    const trades = within(table).getByRole("rowheader", { name: "Trades" }).closest("tr")!;
    expect(within(trades).getAllByRole("cell").map((c) => c.textContent)).toEqual(["4", "0", "+4"]);
    const pnl = within(table).getByRole("rowheader", { name: "Total P&L" }).closest("tr")!;
    expect(within(pnl).getAllByRole("cell").map((c) => c.textContent)).toEqual(["$5.00", "$0.00", "+$5.00"]);
  });

  it("a missing live side shows n/a throughout", () => {
    render(<CompareTable replay={metricsOut} live={null} />);
    const table = screen.getByRole("table", { name: "Replay compared with live" });
    const row = within(table).getByRole("rowheader", { name: "Trades" }).closest("tr")!;
    expect(within(row).getAllByRole("cell").map((c) => c.textContent)).toEqual(["4", "n/a", "n/a"]);
  });
});
