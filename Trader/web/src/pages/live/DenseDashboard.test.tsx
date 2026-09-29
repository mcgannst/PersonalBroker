// DB-DENSE: the dense Dashboard layout. The stat strip's tiles, the equity hero, the flat line, cards up to 6
// positions and rows above, the sidebar and timeline placement, and the phone stacking (DOM order plus the
// stylesheet's breakpoints: jsdom has no layout engine). FakeApiClient and liveFixtures only.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { LiveOut } from "../../api/types";
import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import * as lfx from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import Dashboard from "../Dashboard";
import { changePct } from "./EquityChart";
import { Panel } from "./Panel";
import { CARDS_MAX, flatNext } from "./PositionsTable";

async function renderLive(live: LiveOut = lfx.liveOut, route = "/dashboard") {
  const api = new FakeApiClient({ live });
  const r = renderWithProviders(<Dashboard />, { api, route });
  await screen.findByRole("region", { name: "Session" });
  return r;
}

function region(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

function readWebFile(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return readFileSync(found, "utf8");
}

/** True when `a` comes before `b` in the document. */
function before(a: Element, b: Element): boolean {
  return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

describe("DB-DENSE stat strip", () => {
  it("is one strip of tiles in the approved order: P&L today, week, since start, costs, run stats, open risk, engine, books, live", async () => {
    await renderLive();
    const strip = region("Session");
    expect(strip).toHaveClass("dash-strip");
    const tiles = Array.from(strip.children);
    const key = (el: Element) =>
      el.getAttribute("data-testid") ?? el.getAttribute("aria-label") ?? Array.from(el.classList).find((c) => c.startsWith("st-") && c !== "st-label") ?? "";
    expect(tiles.map(key)).toEqual([
      "topbar-period-today",
      "topbar-period-week",
      "topbar-period-run",
      "Costs",
      "topbar-run-stats",
      "strip-risk",
      "st-engine",
      "Books",
      "st-live",
    ]);
    for (const t of tiles) expect(t).toHaveClass("st");
    // big tabular values with small-caps labels
    expect(within(tiles[0] as HTMLElement).getByText("+$11.00", { selector: ".st-value .money" })).toHaveClass("money", "up");
    expect(within(strip).getByTestId("engine-chip")).toHaveTextContent("MANUAL · RUNNING");
    expect(within(strip).getByRole("heading", { level: 2, name: /Session 2026-10-06 · Open/ })).toHaveClass("st-label");
  });

  it("shows open risk against its cap with the slots, a thin bar, and the last update time beside the live indicator", async () => {
    await renderLive();
    const risk = screen.getByTestId("strip-risk");
    expect(risk).toHaveTextContent("Open risk$17.40of $37.50 · slots 1/3");
    for (const m of Array.from(risk.querySelectorAll(".money"))) expect(m).toHaveClass("flat");
    expect(within(risk).getByRole("progressbar", { name: "Open risk used of its cap" })).toHaveAttribute("aria-valuenow", "46.4");
    const live = region("Session").querySelector(".st-live") as HTMLElement;
    expect(within(live).getByTestId("live-indicator")).toBeInTheDocument();
    expect(live).toHaveTextContent(/last update \d{2}:\d{2} MT/);
  });

  it("a failed risk part shows a dash in the strip (its error and Retry stay on the Risk panel)", async () => {
    const message = "OperationalError: risk could not be read";
    await renderLive(lfx.liveWith({ risk: null, part_errors: [{ part: "risk", message }] }));
    const risk = screen.getByTestId("strip-risk");
    expect(risk).toHaveTextContent("Open risk–");
    expect(within(region("Session")).queryByText(message)).toBeNull();
    expect(within(region("Risk")).getByText(message)).toBeInTheDocument();
  });

  it("a failed P&L part shows inline in the strip with Retry, the other tiles stay", async () => {
    const r = await renderLive(lfx.liveWith({ periods: null, part_errors: [{ part: "periods", message: "periods broke" }] }));
    const strip = region("Session");
    const tile = strip.querySelector(".st-periods-error") as HTMLElement;
    expect(within(tile).getByText("periods broke")).toBeInTheDocument();
    expect(screen.queryByTestId("topbar-period-today")).toBeNull();
    expect(within(strip).getByTestId("strip-risk")).toBeInTheDocument();
    expect(within(strip).getByRole("region", { name: "Books" })).toBeInTheDocument();
    await userEvent.click(within(tile).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(r.api.callsTo("live").length).toBeGreaterThanOrEqual(2));
  });
});

describe("DB-DENSE equity hero", () => {
  it("shows the current equity large with today's change in $ and %", async () => {
    await renderLive();
    const hero = within(region("Equity")).getByTestId("equity-hero");
    expect(hero).toHaveTextContent("$769.73");
    expect(within(hero).getByText("+$11.00")).toHaveClass("money", "up");
    expect(hero).toHaveTextContent("(+1.45%)");
    expect(within(hero).getByText("$769.73")).toHaveClass("money", "flat");
    expect(within(region("Equity")).getByRole("figure", { name: "Equity today" })).toHaveClass("is-tall");
  });

  it("changePct: change over the day's start equity; null when it cannot be computed", () => {
    expect(changePct("769.7250", "11.0000")).toBe("+1.45%");
    expect(changePct("740.0000", "-10.0000")).toBe("-1.33%");
    expect(changePct("750.0000", "0.0000")).toBe("0.00%");
    expect(changePct(null, "1.0000")).toBeNull();
    expect(changePct("10.0000", "10.0000")).toBeNull();
  });

  it("a flat day's change is flat money, never green or red", async () => {
    await renderLive(lfx.liveEmptyDay);
    const hero = within(region("Equity")).getByTestId("equity-hero");
    expect(within(hero).getByText("$0.00")).toHaveClass("money", "flat");
    expect(within(region("Equity")).getByText("No equity data for this range")).toBeInTheDocument();
  });
});

describe("DB-DENSE positions: the flat line, cards up to 6, rows above", () => {
  it("flat: one slim line (FLAT, no open positions, closed today, the next step), no list and no sort chips", async () => {
    await renderLive(lfx.liveWith({ positions: [], closed_today: 1 }));
    const panel = region("Positions");
    expect(panel).toHaveClass("is-flat");
    const line = within(panel).getByTestId("positions-flat");
    expect(line).toHaveTextContent("FLAT · No open positions · 1 trade closed today · waiting for Cancel unfilled entries 09:30 MT");
    expect(within(panel).queryByRole("list")).toBeNull();
    expect(within(panel).queryByRole("button")).toBeNull();
  });

  it("flat on a closed day names the next session", async () => {
    await renderLive(lfx.liveEmptyDay);
    expect(within(region("Positions")).getByTestId("positions-flat")).toHaveTextContent("market closed · next session 2026-10-12");
    expect(flatNext(undefined, [])).toBeNull();
    expect(flatNext(lfx.liveOut.session, fx.timeline)).toBe("waiting for Cancel unfilled entries 09:30 MT");
  });

  it.each([1, CARDS_MAX])("%i positions are cards in a grid", async (n) => {
    await renderLive(lfx.liveWith({ positions: lfx.positionsN(n) }));
    const panel = region("Positions");
    const list = within(panel).getByRole("list", { name: "Open positions" });
    expect(list).toHaveClass("pos-cards");
    const cards = within(list).getAllByRole("listitem");
    expect(cards).toHaveLength(n);
    for (const c of cards) expect(c).toHaveClass("pos-row", "pos-card");
    expect(panel.querySelector(".pos-list")).toBeNull();
    const aaa = cards.find((c) => c.dataset.ticker === "AAA")!;
    const toggle = within(aaa).getByRole("button", { name: /^AAA\b/ });
    expect(toggle).toHaveClass("pos-summary");
    // the card's sparkline stretches to the card and fits a phone
    const spark = within(aaa).getByRole("img", { name: "AAA since entry" });
    expect(spark).toHaveClass("is-stretched");
    expect(Number(spark.getAttribute("width"))).toBeLessThanOrEqual(390);
  });

  it(`${CARDS_MAX + 1} positions switch to the compact rows`, async () => {
    await renderLive(lfx.liveWith({ positions: lfx.positionsN(CARDS_MAX + 1) }));
    const list = within(region("Positions")).getByRole("list", { name: "Open positions" });
    expect(list).toHaveClass("pos-list");
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(CARDS_MAX + 1);
    for (const r of rows) expect(r).not.toHaveClass("pos-card");
    expect(document.querySelector(".pos-cards")).toBeNull();
  });

  it("a card shows ticker, side and qty, big unrealised $ and R, entry → now, stop, held time; it keeps near-stop and stale; a tap expands it", async () => {
    const [a, b] = lfx.positionsN(2, { nearStop: true });
    const stale = { ...lfx.livePosition, id: 7, mark_state: "stale" as const };
    const r = await renderLive(lfx.liveWith({ positions: [stale, { ...a!, ticker: "NEAR" }, b!] }));
    const card = (ticker: string) => within(region("Positions")).getAllByRole("listitem").find((li) => li.dataset.ticker === ticker)!;
    const aaa = card("AAA");
    expect(aaa).toHaveTextContent("long 30");
    expect(aaa).toHaveTextContent("21.56 → 21.96");
    expect(aaa).toHaveTextContent("stop / tgt 20.98 / –");
    expect(aaa).toHaveTextContent("+0.69R");
    expect(aaa).toHaveTextContent("23m 45s");
    expect(within(aaa).getByText("$12.00", { selector: ".pc-pnl .money" })).toHaveClass("money", "up");
    expect(within(aaa).getByText("prices stale")).toBeInTheDocument();
    expect(card("NEAR")).toHaveClass("near-stop");
    expect(within(card("NEAR")).getByText("near stop")).toHaveClass("live-sr-only");
    const toggle = within(aaa).getByRole("button", { name: /^AAA\b/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(r.location().search).toBe("?expand=7");
  });

  it("an expanded card (from ?expand=) spans the grid with its details and chart", async () => {
    await renderLive(lfx.liveWith({ positions: lfx.positionsN(3) }), "/dashboard?expand=101");
    const bbb = within(region("Positions")).getAllByRole("listitem").find((li) => li.dataset.ticker === "BBB")!;
    expect(bbb).toHaveClass("pos-card", "is-expanded");
    expect(within(bbb).getByRole("button", { name: /^BBB\b/ })).toHaveAttribute("aria-expanded", "true");
    expect(within(bbb).getByRole("group", { name: "BBB details" })).toBeInTheDocument();
    expect(bbb.querySelector(".pos-chart")).not.toBeNull();
  });
});

describe("DB-DENSE page layout and phone stacking", () => {
  it("strip first, then the main column (equity, positions), the sidebar (pending, risk, activity, rejections), then the timeline", async () => {
    await renderLive();
    const main = document.querySelector(".dash-main")!;
    const side = document.querySelector("aside.dash-side")!;
    expect(side).toHaveAttribute("aria-label");
    const order = [region("Session"), region("Equity"), region("Positions"), region("Pending approvals"), region("Risk"), region("Activity"), region("Rejected today, and why"), region("Today")];
    for (let i = 1; i < order.length; i += 1) expect(before(order[i - 1]!, order[i]!), `${i}`).toBe(true);
    for (const name of ["Equity", "Positions"]) expect(main.contains(region(name)), name).toBe(true);
    for (const name of ["Pending approvals", "Risk", "Activity", "Rejected today, and why"]) expect(side.contains(region(name)), name).toBe(true);
    // pending approvals sit at the top of the sidebar
    expect(side.querySelector(".panel")).toBe(region("Pending approvals"));
    expect(main.contains(region("Today")) || side.contains(region("Today"))).toBe(false);
  });

  it("the page's panels are dense (8 px padding, small-caps titles); a panel outside the Dashboard keeps the default", async () => {
    await renderLive();
    for (const name of ["Equity", "Positions", "Risk", "Activity", "Today"]) {
      const panel = region(name);
      expect(panel.style.padding, name).toBe("var(--space-2)");
      expect((panel.querySelector(".panel-title") as HTMLElement).style.textTransform, name).toBe("uppercase");
    }
    const { container } = renderWithProviders(<Panel title="Plain" />);
    expect((container.querySelector("section") as HTMLElement).style.padding).toBe("var(--space-3)");
  });

  it("the timeline is one row of chips with the status first", () => {
    const css = readWebFile("src/pages/live/dash.css");
    expect(css).toMatch(/\.dash-page \.today-timeline \.timeline\s*\{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap/);
    expect(css).toMatch(/\.dash-page \.today-timeline \.timeline-item\s*\{[^}]*border-radius:\s*999px/);
    expect(css).toMatch(/\.dash-page \.today-timeline \.timeline-status\s*\{[^}]*order:\s*-1/);
  });

  it("phone and tablet portrait stack: one column below 1024 px, two (about 2/3 and 1/3) from 1024 px; phone tiles 2-3 per row", () => {
    const css = readWebFile("src/pages/live/dash.css").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css).toMatch(/(^|\n)\.dash-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);/);
    const wide = /@media \(min-width: 1024px\)\s*\{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";
    expect(wide).toMatch(/\.dash-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 2fr\) minmax\(300px, 1fr\)/);
    const phone = /@media \(max-width: 719px\)\s*\{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";
    expect(phone).toMatch(/\.dash-strip \.st\s*\{[^}]*flex-basis:\s*calc\(50%/);
    expect(phone).toMatch(/\.dash-strip \.st-period\s*\{[^}]*flex-basis:\s*calc\(33%/);
    // flat: no shadows or gradients, no money green/red outside the money classes
    expect(css).not.toMatch(/box-shadow|gradient|--money-(up|down)|var\(--(ok|bad)\)/);
    // nothing fixed wider than a 390 px phone outside the wide breakpoint
    const narrow = css.replace(/@media \(min-width: 1024px\)\s*\{[\s\S]*?\n\}/, "");
    for (const m of narrow.matchAll(/(?:^|[;{])\s*((?:min-)?width|flex-basis|grid-template-columns)\s*:\s*([^;}]+)/g)) {
      const px = Array.from(m[2]!.matchAll(/(\d+(?:\.\d+)?)px/g)).reduce((s, x) => s + Number(x[1]), 0);
      expect(px, `${m[1]}: ${m[2]}`).toBeLessThanOrEqual(390);
    }
  });

  it("at 390 px nothing is fixed wider than the phone, with cards expanded", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    await renderLive(lfx.liveWith({ positions: lfx.positionsN(4), pending: [fx.pendingProposal] }), "/dashboard?expand=100,101");
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("[style]"))) {
      for (const prop of ["width", "minWidth"] as const) {
        const px = /^(\d+(?:\.\d+)?)px$/.exec(el.style[prop]);
        if (px) expect(Number(px[1]), `${el.tagName} ${prop}`).toBeLessThanOrEqual(390);
      }
    }
    for (const svg of Array.from(document.querySelectorAll("svg[width]"))) expect(Number(svg.getAttribute("width"))).toBeLessThanOrEqual(390);
    for (const b of Array.from(document.querySelectorAll<HTMLButtonElement>("button"))) {
      expect(parseFloat(b.style.minHeight || "0"), `button "${b.textContent}"`).toBeGreaterThanOrEqual(44);
    }
  });
});
