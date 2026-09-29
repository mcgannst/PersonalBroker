// DB-T8 acceptance test 6 (rejections) and its parts of 8 (panel error) and 9 (touch targets, XSS).
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { RejectionsOut } from "../../api/types";
import { MIN_TOUCH_PX } from "../../components/ui";
import { rejections, withXssText, XSS } from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import { RejectionsPanel } from "./RejectionsPanel";

const riskRule = {
  stage: "risk" as const,
  rule: "max_positions",
  count: 120,
  tickers: ["ZZZ"],
  truncated: false,
  link: "/reports?day=2026-10-06&stage=risk&outcome=rejected",
};

function ruleButtons(): HTMLElement[] {
  return screen.getAllByRole("button").filter((b) => b.hasAttribute("aria-expanded"));
}

describe("RejectionsPanel (acceptance test 6)", () => {
  it("shows the title, the total and the rules sorted by count with stage and rule", () => {
    const data: RejectionsOut = { ...rejections, rules: [rejections.rules[1]!, riskRule, rejections.rules[0]!] };
    renderWithProviders(<RejectionsPanel rejections={data} />);
    const region = screen.getByRole("region", { name: "Rejected today, and why" });
    expect(within(region).getByRole("heading", { name: "Rejected today, and why" })).toBeInTheDocument();
    expect(within(region).getByText("531 rejected")).toBeInTheDocument();
    const buttons = ruleButtons();
    expect(buttons.map((b) => b.textContent)).toEqual(["scan · rvol_below_min498", "risk · max_positions120", "scan · gap_below_min33"]);
    for (const b of buttons) {
      expect(b).toHaveAttribute("aria-expanded", "false");
      expect(parseFloat(b.style.minHeight)).toBeGreaterThanOrEqual(MIN_TOUCH_PX);
    }
  });

  it("tapping a rule reveals its tickers with the Day-view links, and '+n more' when truncated", async () => {
    const { location } = renderWithProviders(<RejectionsPanel rejections={rejections} />, { route: "/dashboard" });
    expect(screen.queryByRole("link", { name: "RV00" })).not.toBeInTheDocument();
    const rvol = ruleButtons()[0]!;
    await userEvent.click(rvol);
    expect(rvol).toHaveAttribute("aria-expanded", "true");
    const list = screen.getByRole("list", { name: "scan · rvol_below_min tickers" });
    const links = within(list).getAllByRole("link");
    expect(links).toHaveLength(50);
    expect(links[0]).toHaveAttribute("href", "/reports?day=2026-10-06&stage=scan&outcome=rejected&ticker=RV00");
    for (const a of links) expect(a).toHaveClass("link-touch");
    expect(screen.getByText("+448 more")).toBeInTheDocument();
    // the rule itself links to the Day view filtered by stage and outcome
    expect(screen.getByRole("link", { name: "Open scan · rvol_below_min in Reports" })).toHaveAttribute(
      "href",
      "/reports?day=2026-10-06&stage=scan&outcome=rejected",
    );
    // a second rule opens too; the first one closes again on a second tap
    await userEvent.click(ruleButtons()[1]!);
    expect(screen.getByRole("list", { name: "scan · gap_below_min tickers" })).toBeInTheDocument();
    expect(screen.queryAllByText(/^\+\d+ more$/)).toHaveLength(1); // gap_below_min is not truncated
    await userEvent.click(rvol);
    expect(screen.queryByRole("list", { name: "scan · rvol_below_min tickers" })).not.toBeInTheDocument();
    await userEvent.click(within(screen.getByRole("list", { name: "scan · gap_below_min tickers" })).getByRole("link", { name: "GP05" }));
    expect(location().pathname + location().search).toBe("/reports?day=2026-10-06&stage=scan&outcome=rejected&ticker=GP05");
  });

  it("encodes the ticker in the link", async () => {
    const data: RejectionsOut = { ...rejections, rules: [{ ...riskRule, tickers: ["BRK.B", "A&B"] }] };
    renderWithProviders(<RejectionsPanel rejections={data} />);
    await userEvent.click(ruleButtons()[0]!);
    expect(screen.getByRole("link", { name: "A&B" })).toHaveAttribute("href", "/reports?day=2026-10-06&stage=risk&outcome=rejected&ticker=A%26B");
  });

  it("notes the candidates source", () => {
    const { unmount } = renderWithProviders(<RejectionsPanel rejections={{ ...rejections, source: "candidates" }} />);
    expect(screen.getByText("from candidates, decision log not recorded yet")).toBeInTheDocument();
    unmount();
    renderWithProviders(<RejectionsPanel rejections={rejections} />);
    expect(screen.queryByText("from candidates, decision log not recorded yet")).not.toBeInTheDocument();
  });

  it("shows 'No rejections recorded today' when there are none", () => {
    const empty: RejectionsOut = { session_date: "2026-10-09", source: "none", total: 0, rules: [], final: true, recorded_at: null };
    renderWithProviders(<RejectionsPanel rejections={empty} />);
    expect(screen.getByText("No rejections recorded today")).toBeInTheDocument();
    expect(screen.queryByText(/rejected$/)).not.toBeInTheDocument();
  });
});

describe("rejections panel error (acceptance test 8)", () => {
  it("null rejections with an error show the error and Retry without throwing", async () => {
    const onRetry = vi.fn();
    renderWithProviders(<RejectionsPanel rejections={null} error="OperationalError: rejections could not be read" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("OperationalError: rejections could not be read");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("null rejections without an error still render a panel", () => {
    renderWithProviders(<RejectionsPanel rejections={null} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Rejections not available");
  });
});

describe("XSS (acceptance test 9)", () => {
  it("renders rules and tickers literally", async () => {
    const { live } = withXssText();
    const { container } = renderWithProviders(<RejectionsPanel rejections={live.rejections} />);
    await userEvent.click(ruleButtons()[0]!);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain(`rvol_below_min${XSS}`);
    expect(container.textContent).toContain(`RV00${XSS}`);
  });
});
