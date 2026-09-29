// DB-T7 acceptance test 5: the equity chart.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { EquitySeriesOut } from "../../api/types";
import { equityRun, equityToday } from "../../test/liveFixtures";
import { EquityChart } from "./EquityChart";

function series500(): EquitySeriesOut {
  const t0 = Date.parse("2026-09-29T13:30:00Z");
  return {
    range: "run",
    start_equity: "750.0000",
    points: Array.from({ length: 500 }, (_, i) => ({
      ts: new Date(t0 + i * 30 * 60_000).toISOString(), // to 2026-10-09, past every fill of the run
      equity: (750 + Math.sin(i / 20) * 15).toFixed(4),
      source: i === 499 ? ("now" as const) : ("snapshot" as const),
    })),
    fills: equityRun.fills,
    downsampled: true,
  };
}

describe("EquityChart (acceptance test 5)", () => {
  it("renders 500 points with the start line, buy and sell markers and the downsampled note", () => {
    const { container } = render(<EquityChart equity={series500()} range="run" onRange={() => {}} />);
    const region = screen.getByRole("region", { name: "Equity" });
    const svg = region.querySelector("svg.recharts-surface");
    expect(svg).not.toBeNull();
    expect(Number(svg!.getAttribute("width"))).toBeLessThanOrEqual(390);
    expect(container.querySelector(".recharts-line-curve")?.getAttribute("d")).toBeTruthy();
    expect(container.querySelectorAll(".lva-start-line").length).toBe(1);
    expect(container.querySelectorAll(".lva-fill-buy").length).toBe(2);
    expect(container.querySelectorAll(".lva-fill-sell").length).toBe(1);
    // buy and sell markers have different shapes
    const buy = container.querySelector(".lva-fill-buy")!;
    const sell = container.querySelector(".lva-fill-sell")!;
    expect(buy.getAttribute("d")).not.toBe(sell.getAttribute("d"));
    expect(within(region).getByText(/downsampled/i)).toBeInTheDocument();
    expect(within(region).getByTestId("equity-legend")).toHaveTextContent("Start $750.00");
  });

  it("renders today's series without the downsampled note", () => {
    const { container } = render(<EquityChart equity={equityToday} range="today" onRange={() => {}} />);
    expect(container.querySelectorAll(".lva-fill-buy").length).toBe(1);
    expect(screen.queryByText(/downsampled/i)).not.toBeInTheDocument();
  });

  it("toggles the range with two 44 px buttons", async () => {
    const onRange = vi.fn();
    render(<EquityChart equity={equityToday} range="today" onRange={onRange} />);
    const today = screen.getByRole("button", { name: "Today" });
    const run = screen.getByRole("button", { name: "Whole run" });
    expect(today).toHaveAttribute("aria-pressed", "true");
    expect(run).toHaveAttribute("aria-pressed", "false");
    for (const b of [today, run]) expect(parseFloat(b.style.minHeight)).toBeGreaterThanOrEqual(44);
    await userEvent.click(run);
    expect(onRange).toHaveBeenCalledWith("run");
    await userEvent.click(today);
    expect(onRange).toHaveBeenLastCalledWith("today");
  });

  it("shows the empty state for an empty series and keeps the toggle", () => {
    render(<EquityChart equity={{ ...equityToday, points: [], fills: [] }} range="today" onRange={() => {}} />);
    expect(screen.getByText("No equity data for this range")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Whole run" })).toBeInTheDocument();
  });

  it("shows its error with Retry", async () => {
    const onRetry = vi.fn();
    render(<EquityChart equity={null} range="today" onRange={() => {}} error="OperationalError: equity could not be read" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("OperationalError: equity could not be read");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
