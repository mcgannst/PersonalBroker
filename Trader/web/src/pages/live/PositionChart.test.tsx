// DB-T8 acceptance test 2 (the expanded chart) and its part of 9 (XSS, width).
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { BarOut } from "../../api/types";
import { livePosition, withXssText, XSS } from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import { PositionChart } from "./PositionChart";

function bars(n: number, source: BarOut["source"] = "candle", from = "2026-10-06T13:36:00Z"): BarOut[] {
  const t0 = Date.parse(from);
  return Array.from({ length: n }, (_, i) => {
    const c = (21.5 + i * 0.01).toFixed(4);
    return { start: new Date(t0 + i * 60_000).toISOString(), open: c, high: c, low: c, close: c, source };
  });
}

describe("PositionChart", () => {
  it("draws the closes, entry, stop and target lines and the fill markers; notes bars from quotes", () => {
    const p = { ...livePosition, target: "22.5000", bars: [...bars(10), ...bars(3, "marks", "2026-10-06T13:46:00Z")] };
    const { container } = renderWithProviders(<PositionChart p={p} />);
    expect(screen.getByRole("figure", { name: "AAA 1-minute chart" })).toBeInTheDocument();
    expect(container.querySelector(".recharts-line-curve")).not.toBeNull();
    expect(container.querySelectorAll(".recharts-reference-line")).toHaveLength(3);
    expect(container.querySelectorAll(".recharts-reference-dot")).toHaveLength(1);
    expect(screen.getByText("bars from quotes")).toBeInTheDocument();
  });

  it("has no quotes note when every bar is a candle, and no target line without a target", () => {
    const { container } = renderWithProviders(<PositionChart p={{ ...livePosition, bars: bars(10) }} />);
    expect(screen.queryByText("bars from quotes")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".recharts-reference-line")).toHaveLength(2);
  });

  it("says 'Chart data not available yet' for null or empty bars, and still links the trade", () => {
    for (const b of [null, []]) {
      const { container, unmount } = renderWithProviders(<PositionChart p={{ ...livePosition, bars: b }} />);
      expect(screen.getByText("Chart data not available yet")).toBeInTheDocument();
      expect(container.querySelector("svg.recharts-surface")).toBeNull();
      const link = screen.getByRole("link", { name: "Open trade" });
      expect(link).toHaveAttribute("href", "/trades?position=7");
      expect(link).toHaveClass("link-touch");
      unmount();
    }
  });

  it("fits a phone and renders the ticker as text", () => {
    const { live } = withXssText();
    const p = { ...live.positions![0]!, bars: bars(30) };
    const { container } = renderWithProviders(<PositionChart p={p} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("figure", { name: `AAA${XSS} 1-minute chart` })).toBeInTheDocument();
    for (const svg of Array.from(container.querySelectorAll("svg[width]"))) expect(Number(svg.getAttribute("width"))).toBeLessThanOrEqual(390);
  });
});
