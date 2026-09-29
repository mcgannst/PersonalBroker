// DB-T7 acceptance tests 1, 2 (TopBar) and the cross-component tests 7, 8 and 9 (every A component).
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { LiveOut } from "../../api/types";
import { MIN_TOUCH_PX } from "../../components/ui";
import { XSS, liveOut, liveWith, withXssText } from "../../test/liveFixtures";
import { BooksCheck } from "./BooksCheck";
import { CostBar } from "./CostBar";
import { EquityChart } from "./EquityChart";
import { PeriodPnl } from "./PeriodPnl";
import { RiskPanel } from "./RiskPanel";
import { TopBar } from "./TopBar";

const NOW = Date.parse("2026-10-06T14:00:00Z");

function renderTopBar(live: LiveOut, opts: { connected?: boolean; updatedAt?: number } = {}) {
  return render(<TopBar live={live} connected={opts.connected ?? true} updatedAt={opts.updatedAt ?? NOW - 2_000} nowMs={NOW} />);
}

/** Every A component with the parts of `live`, as the Dashboard composes them. */
function renderAll(live: LiveOut) {
  return render(
    <MemoryRouter>
      <TopBar live={live} connected updatedAt={NOW} nowMs={NOW} />
      <PeriodPnl periods={live.periods} />
      <CostBar claude={live.claude_today} periods={live.periods} />
      <BooksCheck books={live.books} />
      <EquityChart equity={live.equity} range="today" onRange={() => {}} />
      <RiskPanel risk={live.risk} />
    </MemoryRouter>,
  );
}

/** A formatted money value: `$1,234.56`, `-$12.30`, `+$11.00`. */
const MONEY_RE = /^[+-]?\$\d{1,3}(,\d{3})*\.\d{2}$/;

describe("TopBar (acceptance test 1)", () => {
  it("is the region labelled Session with the session date and phase", () => {
    renderTopBar(liveOut);
    const region = screen.getByRole("region", { name: "Session" });
    expect(region).toHaveTextContent("2026-10-06");
    expect(region).toHaveTextContent("Open");
  });

  it("shows the three periods' P&L after fees with realised and open, each with its money tone", () => {
    const live = liveWith({
      periods: liveOut.periods!.map((p) => (p.period === "today" ? { ...p, pnl_after_fees: "-12.3000", realized: "-23.3000" } : p)),
    });
    renderTopBar(live);
    const today = screen.getByTestId("topbar-period-today");
    const headline = within(today).getByText("-$12.30");
    expect(headline).toHaveClass("money", "down");
    expect(within(today).getByText("-$23.30")).toHaveClass("money", "down");
    expect(within(today).getByText("+$11.00")).toHaveClass("money", "up");
    const week = screen.getByTestId("topbar-period-week");
    expect(within(week).getAllByText("+$19.73")[0]).toHaveClass("money", "up");
    const run = screen.getByTestId("topbar-period-run");
    expect(within(run).getByText("Since start")).toBeInTheDocument();
    expect(within(run).getAllByText("+$19.73")[0]).toHaveClass("money", "up");
  });

  it("shows fees, Claude spend, net after AI, win rate, trades and expectancy of the run", () => {
    renderTopBar(liveOut);
    const region = screen.getByRole("region", { name: "Session" });
    const costs = within(region).getByRole("region", { name: "Costs" });
    expect(within(costs).getByText("$0.12", { selector: ".lva-spent" })).toBeInTheDocument(); // Claude today
    expect(within(costs).getByText("$1.00", { selector: ".lva-cap" })).toBeInTheDocument();
    expect(within(costs).getByText("+$10.88")).toHaveClass("money", "up"); // net after AI, today
    expect(within(costs).getAllByText("$3.00").length).toBeGreaterThan(0); // fees, week and run
    const stats = screen.getByTestId("topbar-run-stats");
    expect(stats).toHaveTextContent("Win rate100.00%");
    expect(stats).toHaveTextContent("Trades1");
    expect(stats).toHaveTextContent("Expectancy+0.71R");
  });

  it("shows the engine chip for auto/running, manual/paused and blocked", () => {
    const { rerender } = renderTopBar(liveWith({ approval_mode: "auto", trading: "running" }));
    expect(screen.getByTestId("engine-chip")).toHaveTextContent("AUTO · RUNNING");
    expect(screen.getByTestId("engine-chip")).toHaveClass("status-ok");
    rerender(<TopBar live={liveWith({ approval_mode: "manual", trading: "paused" })} connected updatedAt={NOW} nowMs={NOW} />);
    expect(screen.getByTestId("engine-chip")).toHaveTextContent("MANUAL · PAUSED");
    expect(screen.getByTestId("engine-chip")).toHaveClass("status-warn");
    rerender(<TopBar live={liveWith({ approval_mode: "manual", trading: "blocked" })} connected updatedAt={NOW} nowMs={NOW} />);
    expect(screen.getByTestId("engine-chip")).toHaveTextContent("MANUAL · BLOCKED");
    expect(screen.getByTestId("engine-chip")).toHaveClass("status-bad");
  });

  it("includes the books check and says when a part failed", () => {
    renderTopBar(liveOut);
    expect(screen.getByRole("region", { name: "Books" })).toHaveTextContent("✓");
    renderTopBar(
      liveWith({ periods: null, books: null, part_errors: [{ part: "periods", message: "OperationalError: periods could not be read" }, { part: "books", message: "OperationalError: books could not be read" }] }),
    );
    expect(screen.getByText("OperationalError: periods could not be read")).toBeInTheDocument();
    expect(screen.getByText("OperationalError: books could not be read")).toBeInTheDocument();
  });

  it("labels a non-session day with the last session's date", () => {
    renderTopBar(liveWith({ session: { date: "2026-10-10", phase: "closed_day", is_session: false, open_at: null, close_at: null }, session_day: "2026-10-09" }));
    const region = screen.getByRole("region", { name: "Session" });
    expect(region).toHaveTextContent("2026-10-09");
    expect(region).toHaveTextContent("Market closed today");
  });
});

describe("TopBar live indicator (acceptance test 2)", () => {
  it("is Live when connected and the data is at most 10 s old", () => {
    renderTopBar(liveOut, { connected: true, updatedAt: NOW - 10_000 });
    expect(screen.getByTestId("live-indicator")).toHaveTextContent(/^Live$/);
    expect(screen.getByTestId("live-indicator")).toHaveClass("status-ok");
  });

  it("is degraded when not connected", () => {
    renderTopBar(liveOut, { connected: false, updatedAt: NOW });
    expect(screen.getByTestId("live-indicator")).toHaveTextContent("Degraded: polling every 15 s");
    expect(screen.getByTestId("live-indicator")).toHaveClass("status-warn");
  });

  it("shows the data's age when connected but not fresh", () => {
    renderTopBar(liveOut, { connected: true, updatedAt: NOW - 25_000 });
    expect(screen.getByTestId("live-indicator")).toHaveTextContent("Updated 25 s ago");
  });

  it("shows the heartbeat badge only when the worker is stale", () => {
    renderTopBar(liveOut);
    expect(screen.queryByTestId("heartbeat-badge")).not.toBeInTheDocument();
    renderTopBar(liveWith({ worker_stale: true, worker: { ...liveOut.worker, ok: false, age_seconds: 95 } }));
    const badge = screen.getByTestId("heartbeat-badge");
    expect(badge).toHaveTextContent("Worker heartbeat 95 s old");
    expect(badge).toHaveClass("status-bad");
  });
});

describe("D9 colour rule (acceptance test 7)", () => {
  it("green/red money classes appear only on formatted money values", () => {
    const { container } = renderAll(liveOut);
    const money = Array.from(container.querySelectorAll(".money"));
    expect(money.length).toBeGreaterThan(20);
    for (const el of money) {
      expect(el.textContent ?? "", `a .money element with "${el.textContent}"`).toMatch(MONEY_RE);
      expect(el.className).toMatch(/\bmoney (up|down|flat)\b/);
    }
    // the tone words appear only on money values
    for (const el of Array.from(container.querySelectorAll(".up, .down"))) expect(el).toHaveClass("money");
  });

  it("status lights and chips use only status classes", () => {
    const { container } = renderAll(liveWith({ trading: "blocked", worker_stale: true }));
    const lights = Array.from(container.querySelectorAll(".lva-dot, .lva-chip, .lva-glyph, .lva-meter-fill"));
    expect(lights.length).toBeGreaterThan(5);
    for (const el of lights) {
      expect(el.className, el.className).toMatch(/\bstatus-(ok|warn|bad|muted)\b/);
      expect(el.className).not.toMatch(/\b(money|tone-ok|tone-bad|up|down)\b/);
    }
  });
});

function readWebFile(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return readFileSync(found, "utf8");
}

describe("touch targets and a 390 px phone (acceptance test 8)", () => {
  it("every button is at least 44 px and every link uses the touch class", () => {
    const tripped = liveWith({
      risk: { ...liveOut.risk!, killswitches: liveOut.risk!.killswitches.map((k) => (k.switch === "max_drawdown_pct" ? { ...k, tripped: true, tripped_at: "2026-10-06T13:50:00Z" } : k)) },
    });
    const { container } = renderAll(tripped);
    const buttons = Array.from(container.querySelectorAll<HTMLButtonElement>("button"));
    expect(buttons.length).toBeGreaterThanOrEqual(3); // books breakdown, Today, Whole run
    for (const b of buttons) {
      expect((b.textContent ?? b.getAttribute("aria-label") ?? "").trim()).not.toBe("");
      expect(parseFloat(b.style.minHeight || "0"), `button "${b.textContent}"`).toBeGreaterThanOrEqual(MIN_TOUCH_PX);
    }
    const links = Array.from(container.querySelectorAll("a"));
    expect(links.length).toBeGreaterThanOrEqual(1);
    for (const a of links) expect(a, `link "${a.textContent}"`).toHaveClass("link-touch");
  });

  it("nothing is wider than the phone: no fixed width over 390 px, charts fit, and the grids stack", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    const { container } = renderAll(liveOut);
    for (const el of Array.from(container.querySelectorAll<HTMLElement>("[style]"))) {
      for (const prop of ["width", "minWidth"] as const) {
        const px = /^(\d+(?:\.\d+)?)px$/.exec(el.style[prop]);
        if (px) expect(Number(px[1]), `${el.tagName} ${prop}`).toBeLessThanOrEqual(390);
      }
    }
    for (const svg of Array.from(container.querySelectorAll("svg[width]"))) {
      expect(Number(svg.getAttribute("width"))).toBeLessThanOrEqual(390);
    }
    expect(container.querySelectorAll("table").length).toBe(0);
    const css = readWebFile("src/pages/live/liveA.css");
    // one column on a phone, and long text wraps instead of widening the page
    expect(css).toMatch(/@media\s*\(max-width:\s*\d+px\)\s*\{[^@]*\.lva-periods\s*\{[^}]*grid-template-columns:\s*1fr/);
    expect(css).toMatch(/overflow-wrap:\s*anywhere/);
    expect(css).not.toMatch(/gradient|box-shadow|text-shadow/);
  });
});

describe("XSS (acceptance test 9)", () => {
  it("renders free text from the API as plain text", () => {
    const { live } = withXssText();
    const tripped: LiveOut = {
      ...live,
      risk: { ...live.risk!, killswitches: live.risk!.killswitches.map((k) => ({ ...k, tripped: true, tripped_at: "2026-10-06T13:50:00Z" })) },
      part_errors: [{ part: "books", message: `OperationalError${XSS}` }],
      books: null,
    };
    const { container } = renderAll(tripped);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain(XSS);
    expect(container.textContent).toContain(`Daily loss${XSS}`);
    expect(container.textContent).toContain(`OperationalError${XSS}`);
  });
});
