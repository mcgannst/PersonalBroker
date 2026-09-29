// DB-GWEB fix round 1 (live dashboard components): safe API links, the top bar's Retry on its failed parts
// (P&L, Claude spend, books), the expand clamp, decimal R / percent formatting and flat near-zero money.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { LiveOut, LivePositionOut, RejectionsOut, RiskOut } from "../../api/types";
import * as lfx from "../../test/liveFixtures";
import { ActivityFeed } from "./ActivityFeed";
import { CostBar } from "./CostBar";
import { PositionRow } from "./PositionRow";
import { PositionsTable } from "./PositionsTable";
import { RejectionsPanel } from "./RejectionsPanel";
import { RiskPanel, pct1, r2 } from "./RiskPanel";
import { SafeLink, safeLink } from "./safeLink";
import { TopBar } from "./TopBar";

const NOW = Date.parse(lfx.liveOut.server_time);

describe("safeLink", () => {
  it("keeps same-site paths only", () => {
    expect(safeLink("/reports?view=day&ticker=GP")).toBe("/reports?view=day&ticker=GP");
    expect(safeLink("/trades/12")).toBe("/trades/12");
    for (const bad of ["javascript:alert(1)", " javascript:alert(1)", "//evil.example/x", "/\\evil.example", "https://evil.example", "", "/a\nb", "/a b", "trades/12", null, undefined]) {
      expect(safeLink(bad), String(bad)).toBeNull();
    }
  });

  it("renders an unsafe link as text: an <a> without href, never a URL", () => {
    const { container } = render(
      <MemoryRouter>
        <SafeLink className="link-touch" to="javascript:alert(1)">
          evil
        </SafeLink>
        <SafeLink className="link-touch" to="/trades/1">
          good
        </SafeLink>
      </MemoryRouter>,
    );
    const anchors = Array.from(container.querySelectorAll("a"));
    expect(anchors.map((a) => a.getAttribute("href"))).toEqual([null, "/trades/1"]);
    expect(screen.queryByRole("link", { name: "evil" })).toBeNull();
    expect(screen.getByRole("link", { name: "good" })).toBeInTheDocument();
  });

  it("ActivityFeed, RejectionsPanel and PositionRow never emit a javascript: href", async () => {
    const evil = "javascript:alert(1)";
    const rejections: RejectionsOut = { ...lfx.rejections, rules: [{ ...lfx.rejections.rules[0]!, link: evil, tickers: ["DUP", "DUP"], count: 2 }] };
    const p: LivePositionOut = { ...lfx.livePosition, link: evil };
    const { container } = render(
      <MemoryRouter>
        <ActivityFeed items={[{ ...lfx.activity[0]!, link: evil }]} />
        <RejectionsPanel rejections={rejections} />
        <PositionRow p={p} expanded onToggle={() => undefined} />
      </MemoryRouter>,
    );
    await userEvent.click(screen.getByRole("button", { expanded: false }));
    // duplicate tickers both render (distinct keys)
    expect(screen.getAllByText("DUP")).toHaveLength(2);
    const hrefs = Array.from(container.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs.length).toBeGreaterThan(0);
    expect(hrefs.filter((h) => h !== null && /javascript:/i.test(h))).toEqual([]);
  });
});

describe("TopBar and CostBar: failed parts with Retry", () => {
  function failedTop(): LiveOut {
    return lfx.liveWith({
      periods: null,
      claude_today: null,
      books: null,
      part_errors: [
        { part: "periods", message: "periods broke" },
        { part: "claude_today", message: "claude broke" },
        { part: "books", message: "books broke" },
      ],
    });
  }

  it("shows each failed part's message with a Retry that calls onRetry", async () => {
    const onRetry = vi.fn();
    render(<TopBar live={failedTop()} connected updatedAt={NOW} nowMs={NOW} onRetry={onRetry} />);
    const session = screen.getByRole("region", { name: "Session" });
    for (const text of ["periods broke", "claude broke", "books broke"]) expect(within(session).getByText(text)).toBeInTheDocument();
    const retries = within(session).getAllByRole("button", { name: "Retry" });
    expect(retries).toHaveLength(3);
    for (const b of retries) await userEvent.click(b);
    expect(onRetry).toHaveBeenCalledTimes(3);
  });

  it("without onRetry and without a QueryClient, the errors still show (no Retry button)", () => {
    render(<TopBar live={failedTop()} connected updatedAt={NOW} nowMs={NOW} />);
    expect(screen.getByText("claude broke")).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: "Retry" })).toHaveLength(0);
  });

  it("CostBar keeps the period cost rows when only Claude spend failed", async () => {
    const onRetry = vi.fn();
    render(<CostBar claude={null} periods={lfx.periods} error="claude broke" onRetry={onRetry} />);
    expect(screen.getByText("claude broke")).toBeInTheDocument();
    expect(screen.getByTestId("costs-today")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("numbers", () => {
  it("r2 and pct1 round the decimal digits, never a float", () => {
    expect(r2("0.7050")).toBe("0.71 R");
    expect(r2("-0.0040")).toBe("0.00 R");
    expect(r2("-1.2350")).toBe("-1.24 R");
    expect(r2(null)).toBe("–");
    expect(pct1("0.0013")).toBe("0.1%");
    expect(pct1("0.00125")).toBe("0.1%");
    expect(pct1("0.00150")).toBe("0.2%");
    expect(pct1("0.0005")).toBe("0.1%");
    expect(pct1("-0.00004")).toBe("0.0%");
    expect(pct1("-0.0250")).toBe("-2.5%");
    expect(pct1("1E-2")).toBe("1.0%");
    expect(pct1("0.05")).toBe("5.0%");
    expect(pct1(null)).toBe("–");
  });

  it("a tiny negative unrealised and a tiny negative exit amount show $0.00, flat", () => {
    const { container } = render(
      <MemoryRouter>
        <PositionRow p={{ ...lfx.livePosition, unrealized: "-0.0040" }} expanded={false} onToggle={() => undefined} />
        <ActivityFeed items={[{ ...lfx.exitActivity, amount: "-0.0040" }]} />
      </MemoryRouter>,
    );
    for (const el of Array.from(container.querySelectorAll(".money"))) {
      expect(el.textContent).toBe("$0.00");
      expect(el).toHaveClass("flat");
    }
    expect(container.querySelectorAll(".money").length).toBeGreaterThanOrEqual(2);
  });

  it("each kill-switch dot names its switch", () => {
    const risk: RiskOut = lfx.riskOut;
    render(
      <MemoryRouter>
        <RiskPanel risk={risk} />
      </MemoryRouter>,
    );
    for (const k of risk.killswitches) expect(screen.getByRole("img", { name: `${k.label}: not tripped` })).toBeInTheDocument();
  });
});

describe("PositionsTable expand clamp", () => {
  it("shows at most EXPAND_MAX expanded rows, the first three ids, and a tap works from the clamped list", async () => {
    const onExpand = vi.fn();
    render(
      <MemoryRouter>
        <PositionsTable positions={lfx.positionsN(5)} closedToday={0} staleAfterSeconds={30} expanded={[104, 100, 101, 102, 103]} onExpand={onExpand} />
      </MemoryRouter>,
    );
    const open = Array.from(document.querySelectorAll<HTMLLIElement>("li.pos-row.is-expanded")).map((li) => Number(li.dataset.id));
    expect(open.sort()).toEqual([100, 101, 104]);
    await userEvent.click(document.querySelector<HTMLButtonElement>('li.pos-row[data-id="103"] > button.pos-summary')!);
    expect(onExpand).toHaveBeenLastCalledWith([100, 101, 103]);
  });
});
