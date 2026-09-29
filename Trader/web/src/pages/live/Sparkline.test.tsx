// DB-T8 acceptance test 4: the inline SVG sparkline.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SparkPointOut } from "../../api/types";
import { Sparkline } from "./Sparkline";

function points(n: number): SparkPointOut[] {
  const t0 = Date.parse("2026-10-06T13:36:00Z");
  return Array.from({ length: n }, (_, i) => ({
    ts: new Date(t0 + i * 60_000).toISOString(),
    price: (21.5 + Math.sin(i / 5) * 0.3).toFixed(4),
  }));
}

describe("Sparkline (acceptance test 4)", () => {
  it("draws 60 points as one polyline with 60 coordinates, the entry and stop lines, and the aria label", () => {
    const { container } = render(<Sparkline points={points(60)} entry="21.5000" stop="20.9000" label="AAA since entry" />);
    const svg = screen.getByRole("img", { name: "AAA since entry" });
    expect(svg.tagName.toLowerCase()).toBe("svg");
    const polylines = container.querySelectorAll("polyline");
    expect(polylines).toHaveLength(1);
    const coords = (polylines[0]!.getAttribute("points") ?? "").trim().split(/\s+/);
    expect(coords).toHaveLength(60);
    for (const c of coords) expect(c).toMatch(/^-?\d+(\.\d+)?,-?\d+(\.\d+)?$/);

    const entry = container.querySelector("line.spark-entry");
    expect(entry).not.toBeNull();
    expect(entry!.getAttribute("stroke-dasharray")).toBeTruthy();
    const stop = container.querySelector("line.spark-stop");
    expect(stop).not.toBeNull();
    // a stop line is a status, not money: the status-bad token, solid
    expect(stop!.getAttribute("stroke")).toBe("var(--status-bad)");
    expect(stop!.getAttribute("stroke-dasharray")).toBeNull();
  });

  it("keeps every coordinate inside the box, with the stop below the entry for a long", () => {
    const { container } = render(<Sparkline points={points(60)} entry="21.5000" stop="20.9000" label="AAA since entry" width={100} height={30} />);
    const coords = (container.querySelector("polyline")!.getAttribute("points") ?? "").trim().split(/\s+/);
    for (const c of coords) {
      const [x, y] = c.split(",").map(Number);
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x).toBeLessThanOrEqual(100);
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y).toBeLessThanOrEqual(30);
    }
    const entryY = Number(container.querySelector("line.spark-entry")!.getAttribute("y1"));
    const stopY = Number(container.querySelector("line.spark-stop")!.getAttribute("y1"));
    expect(stopY).toBeGreaterThan(entryY); // SVG y grows downwards
    expect(container.querySelector("svg")!.getAttribute("width")).toBe("100");
  });

  it("draws no stop line without a stop", () => {
    const { container } = render(<Sparkline points={points(10)} entry="21.5000" stop={null} label="AAA since entry" />);
    expect(container.querySelector("line.spark-stop")).toBeNull();
    expect(container.querySelector("line.spark-entry")).not.toBeNull();
  });

  it("shows a flat no-data placeholder with fewer than 2 points", () => {
    for (const n of [0, 1]) {
      const { container, unmount } = render(<Sparkline points={points(n)} entry="21.5000" stop="20.9000" label="AAA since entry" />);
      expect(container.querySelector("polyline")).toBeNull();
      expect(container.querySelector("line.spark-stop")).toBeNull();
      const svg = screen.getByRole("img", { name: /AAA since entry.*no data/ });
      expect(svg.querySelector("line.spark-empty")).not.toBeNull();
      unmount();
    }
  });
});
