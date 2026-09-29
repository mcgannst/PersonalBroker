// DB-T8 acceptance tests 1-3 (positions: 0/1/20 rows, sort, near stop, expand, stale marks) and the positions
// part of 8 (panel error) and 9 (touch targets, 390 px, XSS).
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { BarOut, LivePositionOut } from "../../api/types";
import { MIN_TOUCH_PX } from "../../components/ui";
import { livePosition, positionsN, withXssText, XSS } from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import { PositionsTable } from "./PositionsTable";

function rowTickers(): string[] {
  return screen.getAllByRole("listitem").map((li) => li.getAttribute("data-ticker") ?? "");
}

function Controlled({ positions, onExpand }: { positions: LivePositionOut[]; onExpand: (ids: number[]) => void }) {
  const [expanded, setExpanded] = useState<number[]>([]);
  return (
    <PositionsTable
      positions={positions}
      closedToday={0}
      staleAfterSeconds={30}
      expanded={expanded}
      onExpand={(ids) => {
        onExpand(ids);
        setExpanded(ids);
      }}
    />
  );
}

function bars(n: number, source: BarOut["source"] = "candle"): BarOut[] {
  const t0 = Date.parse("2026-10-06T13:36:00Z");
  return Array.from({ length: n }, (_, i) => {
    const c = (21.5 + i * 0.01).toFixed(4);
    return { start: new Date(t0 + i * 60_000).toISOString(), open: c, high: c, low: c, close: c, source };
  });
}

function readWebFile(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return readFileSync(found, "utf8");
}

describe("PositionsTable with 0, 1 and 20 positions (acceptance test 1)", () => {
  it("shows the empty state with today's closed count", () => {
    renderWithProviders(<PositionsTable positions={[]} closedToday={2} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    const region = screen.getByRole("region", { name: "Positions" });
    expect(within(region).getByText("No open positions")).toBeInTheDocument();
    expect(within(region).getByText("2 trades closed today")).toBeInTheDocument();
    expect(within(region).queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("uses the singular for one closed trade", () => {
    renderWithProviders(<PositionsTable positions={[]} closedToday={1} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    expect(screen.getByText("1 trade closed today")).toBeInTheDocument();
  });

  it("shows one position's ticker, side, qty, entry → mark, stop / target, unrealised $ and R, time held and sparkline", () => {
    renderWithProviders(<PositionsTable positions={[livePosition]} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    const row = screen.getByRole("listitem");
    expect(row).toHaveTextContent("AAA");
    expect(row).toHaveTextContent("long 30");
    expect(row).toHaveTextContent("21.56 → 21.96");
    expect(row).toHaveTextContent("20.98 / –"); // orb_sip has no target (open question 8)
    expect(row).toHaveTextContent("$12.00");
    expect(row).toHaveTextContent("+0.69R");
    expect(row).toHaveTextContent("23m 45s");
    expect(within(row).getByRole("img", { name: "AAA since entry" })).toBeInTheDocument();
    // money is coloured by its sign through the .money classes only
    expect(within(row).getByText("$12.00")).toHaveClass("money", "up");
  });

  it("renders 20 rows with sparklines, sorted by unrealised $ descending by default", () => {
    const positions = positionsN(20);
    renderWithProviders(<PositionsTable positions={positions} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(20);
    expect(screen.getAllByRole("img", { name: /since entry$/ })).toHaveLength(20);
    const expected = [...positions].sort((a, b) => Number(b.unrealized) - Number(a.unrealized)).map((p) => p.ticker);
    expect(rowTickers()).toEqual(expected);
    expect(screen.getByRole("button", { name: "Unrealised $" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Distance to stop" })).toHaveAttribute("aria-pressed", "false");
  });

  it("sorts by distance to stop ascending with missing last, and back", async () => {
    const positions = positionsN(20, { staleMarks: true }); // the last one has no mark (missing)
    const missing = positions[19]!;
    expect(missing.distance_to_stop_r).toBeNull();
    renderWithProviders(<PositionsTable positions={positions} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    expect(rowTickers()[19]).toBe(missing.ticker); // unrealised: missing last too
    await userEvent.click(screen.getByRole("button", { name: "Distance to stop" }));
    expect(screen.getByRole("button", { name: "Distance to stop" })).toHaveAttribute("aria-pressed", "true");
    const withDistance = positions.filter((p) => p.distance_to_stop_r !== null);
    const expected = [...withDistance].sort((a, b) => Number(a.distance_to_stop_r) - Number(b.distance_to_stop_r)).map((p) => p.ticker);
    expect(rowTickers()).toEqual([...expected, missing.ticker]);
    await userEvent.click(screen.getByRole("button", { name: "Unrealised $" }));
    expect(rowTickers()[0]).toBe([...withDistance].sort((a, b) => Number(b.unrealized) - Number(a.unrealized))[0]!.ticker);
  });

  it("highlights near-stop rows and announces them", () => {
    const positions = [...positionsN(2, { nearStop: true }), { ...livePosition }];
    renderWithProviders(<PositionsTable positions={positions} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    const near = screen.getAllByRole("listitem").filter((li) => li.classList.contains("near-stop"));
    expect(near).toHaveLength(2);
    for (const li of near) expect(within(li).getByText("near stop")).toBeInTheDocument();
    const aaa = screen.getAllByRole("listitem").find((li) => li.getAttribute("data-ticker") === "AAA")!;
    expect(aaa).not.toHaveClass("near-stop");
    expect(within(aaa).queryByText("near stop")).not.toBeInTheDocument();
  });
});

describe("expanding rows (acceptance test 2)", () => {
  it("calls onExpand with up to 3 ids; a fourth tap drops the oldest; a second tap collapses", async () => {
    const positions = positionsN(5);
    const onExpand = vi.fn();
    renderWithProviders(<Controlled positions={positions} onExpand={onExpand} />);
    const toggle = (ticker: string) => screen.getByRole("button", { name: new RegExp(`^${ticker}\\b`) });
    await userEvent.click(toggle("AAA"));
    expect(onExpand).toHaveBeenLastCalledWith([100]);
    await userEvent.click(toggle("BBB"));
    await userEvent.click(toggle("CCC"));
    expect(onExpand).toHaveBeenLastCalledWith([100, 101, 102]);
    expect(toggle("AAA")).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(toggle("DDD"));
    expect(onExpand).toHaveBeenLastCalledWith([101, 102, 103]);
    expect(toggle("AAA")).toHaveAttribute("aria-expanded", "false");
    expect(toggle("DDD")).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(toggle("CCC"));
    expect(onExpand).toHaveBeenLastCalledWith([101, 103]);
  });

  it("shows the expanded row's chart with its bars, or 'Chart data not available yet'", () => {
    const withBars = { ...livePosition, bars: bars(24) };
    const other = { ...positionsN(2)[1]!, bars: null }; // BBB (positionsN(1) would reuse the ticker AAA)
    const { container } = renderWithProviders(
      <PositionsTable positions={[withBars, other]} closedToday={0} staleAfterSeconds={30} expanded={[withBars.id, other.id]} onExpand={vi.fn()} />,
    );
    const aaa = screen.getAllByRole("listitem").find((li) => li.getAttribute("data-ticker") === "AAA")!;
    expect(within(aaa).getByRole("figure", { name: "AAA 1-minute chart" })).toBeInTheDocument();
    expect(aaa.querySelector(".recharts-line-curve")).not.toBeNull();
    const second = screen.getAllByRole("listitem").find((li) => li.getAttribute("data-ticker") === other.ticker)!;
    expect(within(second).getByText("Chart data not available yet")).toBeInTheDocument();
    expect(container.querySelectorAll(".pos-expanded")).toHaveLength(2);
  });

  it("an expanded row shows the details a phone hides, and the 'Open trade' link", () => {
    renderWithProviders(<PositionsTable positions={[livePosition]} closedToday={0} staleAfterSeconds={30} expanded={[7]} onExpand={vi.fn()} />);
    const details = screen.getByRole("group", { name: "AAA details" });
    expect(details).toHaveTextContent("Entry21.56");
    expect(details).toHaveTextContent("Mark21.96");
    expect(details).toHaveTextContent("Stop20.98");
    expect(details).toHaveTextContent("Target–");
    const link = screen.getByRole("link", { name: "Open trade" });
    expect(link).toHaveAttribute("href", "/trades?position=7");
    expect(link).toHaveClass("link-touch");
  });
});

describe("stale marks (acceptance test 3)", () => {
  it("a stale row and a missing row show the row badge and the panel badge", () => {
    const [a, b, c] = positionsN(3);
    const rows = [{ ...a!, mark_state: "stale" as const }, { ...b!, mark_state: "missing" as const, mark: null, mark_at: null, unrealized: null, unrealized_r: null, distance_to_stop_r: null }, c!];
    renderWithProviders(<PositionsTable positions={rows} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    const region = screen.getByRole("region", { name: "Positions" });
    const head = region.querySelector(".panel-head")!;
    expect(within(head as HTMLElement).getByText("prices stale")).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    const badged = items.filter((li) => within(li).queryByText("prices stale") !== null).map((li) => li.getAttribute("data-ticker"));
    expect(badged.sort()).toEqual([a!.ticker, b!.ticker].sort());
    // the row is still shown, never hidden
    expect(items).toHaveLength(3);
  });

  it("all live marks: no badge anywhere", () => {
    renderWithProviders(<PositionsTable positions={positionsN(20)} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    expect(screen.queryByText("prices stale")).not.toBeInTheDocument();
  });

  it("the panel badge names the threshold", () => {
    renderWithProviders(<PositionsTable positions={positionsN(3, { staleMarks: true })} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    const head = screen.getByRole("region", { name: "Positions" }).querySelector(".panel-head")!;
    expect(within(head as HTMLElement).getByText("prices stale")).toHaveAttribute("title", "a price is older than 30 s or missing");
  });
});

describe("positions panel error (acceptance test 8)", () => {
  it("null positions with an error show the error and Retry without throwing", async () => {
    const onRetry = vi.fn();
    renderWithProviders(
      <PositionsTable positions={null} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} error="OperationalError: positions could not be read" onRetry={onRetry} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("OperationalError: positions could not be read");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Unrealised $" })).not.toBeInTheDocument();
  });

  it("null positions without an error still render a panel", () => {
    renderWithProviders(<PositionsTable positions={null} closedToday={0} staleAfterSeconds={30} expanded={[]} onExpand={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Positions not available");
  });
});

describe("touch targets, 390 px and XSS (acceptance test 9)", () => {
  it("20 expanded-capable rows: every button ≥ 44 px, links touch-sized, nothing wider than the phone", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    const positions = positionsN(20);
    positions[0] = { ...positions[0]!, bars: bars(30) };
    renderWithProviders(<PositionsTable positions={positions} closedToday={0} staleAfterSeconds={30} expanded={[100, 101, 102]} onExpand={vi.fn()} />);
    for (const button of screen.getAllByRole("button")) {
      expect(parseFloat(button.style.minHeight || "0"), button.textContent ?? "").toBeGreaterThanOrEqual(MIN_TOUCH_PX);
    }
    for (const a of Array.from(document.querySelectorAll("a"))) expect(a).toHaveClass("link-touch");
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("[style]"))) {
      for (const prop of ["width", "minWidth"] as const) {
        const px = /^(\d+(?:\.\d+)?)px$/.exec(el.style[prop]);
        if (px) expect(Number(px[1])).toBeLessThanOrEqual(390);
      }
    }
    for (const svg of Array.from(document.querySelectorAll("svg[width]"))) expect(Number(svg.getAttribute("width"))).toBeLessThanOrEqual(390);
    expect(document.querySelectorAll("table")).toHaveLength(0); // rows are a list, not a wide table
  });

  it("liveB.css hides the detail cells on a phone and keeps the grid inside the width", () => {
    const css = readWebFile("src/pages/live/liveB.css");
    const phone = /@media \(max-width: 719px\)\s*\{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";
    expect(phone).toMatch(/\.pos-detail\s*\{[^}]*display:\s*none/);
    expect(css).toMatch(/\.pos-summary\s*\{[^}]*min-height:\s*var\(--touch\)/);
    expect(css).toMatch(/\.pos-summary\s*\{[^}]*grid-template-columns:[^;]*minmax\(0/);
    expect(css).toMatch(/\.pos-row\.near-stop\s*\{[^}]*var\(--status-warn\)/);
    // D9: no green or red outside the money classes, no shadows or gradients
    expect(css).not.toMatch(/--money-|--ok\b|--bad\b|box-shadow|gradient/);
  });

  it("the XSS fixture renders as text", () => {
    const { live } = withXssText();
    const { container } = renderWithProviders(
      <PositionsTable positions={live.positions} closedToday={0} staleAfterSeconds={30} expanded={[7]} onExpand={vi.fn()} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain(`AAA${XSS}`);
  });
});
