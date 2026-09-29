// DB-T8 acceptance test 5 (activity feed) and its parts of 8 (panel error) and 9 (touch targets, XSS).
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ActivityItemOut } from "../../api/types";
import { MIN_TOUCH_PX } from "../../components/ui";
import { activity, exitActivity, withXssText, XSS } from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import { ActivityFeed } from "./ActivityFeed";

const ITEMS: ActivityItemOut[] = [exitActivity, ...activity];

function itemIds(): string[] {
  return screen.queryAllByRole("listitem").map((li) => li.getAttribute("data-id") ?? "");
}

describe("ActivityFeed (acceptance test 5)", () => {
  it("shows every item newest first with its MT time, text and chip filter All pressed", () => {
    renderWithProviders(<ActivityFeed items={ITEMS} />);
    expect(itemIds()).toEqual(ITEMS.map((i) => i.id));
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
    const first = screen.getAllByRole("listitem")[0]!;
    expect(first).toHaveTextContent("07:50 MT");
    expect(first).toHaveTextContent("Exit HHH (stop): -5.80, -1.00 R");
  });

  it("keeps the order when the server's order is not by time (never re-sorts)", () => {
    const shuffled = [ITEMS[3]!, ITEMS[0]!, ITEMS[5]!];
    renderWithProviders(<ActivityFeed items={shuffled} />);
    expect(itemIds()).toEqual(shuffled.map((i) => i.id));
  });

  it("filters by chip and keeps the order", async () => {
    renderWithProviders(<ActivityFeed items={ITEMS} />);
    const cases: [string, ActivityItemOut["chip"]][] = [
      ["Trades", "trades"],
      ["Proposals", "proposals"],
      ["Alerts", "alerts"],
      ["Scan", "scan"],
    ];
    for (const [label, chip] of cases) {
      await userEvent.click(screen.getByRole("button", { name: label }));
      expect(screen.getByRole("button", { name: label })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "false");
      expect(itemIds()).toEqual(ITEMS.filter((i) => i.chip === chip).map((i) => i.id));
    }
    await userEvent.click(screen.getByRole("button", { name: "All" }));
    expect(itemIds()).toHaveLength(ITEMS.length);
  });

  it("has 44 px chips", () => {
    renderWithProviders(<ActivityFeed items={ITEMS} />);
    for (const name of ["All", "Trades", "Proposals", "Alerts", "Scan"]) {
      expect(parseFloat(screen.getByRole("button", { name }).style.minHeight)).toBeGreaterThanOrEqual(MIN_TOUCH_PX);
    }
  });

  it("shows 'No activity yet today', and per chip 'Nothing in <chip> today'", async () => {
    const { unmount } = renderWithProviders(<ActivityFeed items={[]} />);
    expect(screen.getByText("No activity yet today")).toBeInTheDocument();
    // the chips stay usable on an empty day
    expect(screen.getByRole("button", { name: "Scan" })).toBeInTheDocument();
    unmount();
    renderWithProviders(<ActivityFeed items={ITEMS.filter((i) => i.chip !== "scan")} />);
    await userEvent.click(screen.getByRole("button", { name: "Scan" }));
    expect(screen.getByText("Nothing in Scan today")).toBeInTheDocument();
    expect(screen.queryByText("No activity yet today")).not.toBeInTheDocument();
  });

  it("gives exit amounts the money tone and no other item a money class", () => {
    const up: ActivityItemOut = { ...exitActivity, id: "trade:10", amount: "4.2500", tone: "up", text: "Exit III (flatten): +4.25, +0.40 R" };
    // an item that carries an amount but is not an exit gets no money tone
    const other: ActivityItemOut = { ...activity[5]!, amount: "1.0000" };
    const { container } = renderWithProviders(<ActivityFeed items={[exitActivity, up, other]} />);
    const money = Array.from(container.querySelectorAll(".money"));
    expect(money).toHaveLength(2);
    expect(money[0]).toHaveClass("down");
    expect(money[0]).toHaveTextContent("-$5.80");
    expect(money[1]).toHaveClass("up");
    expect(money[1]).toHaveTextContent("$4.25");
    const rows = screen.getAllByRole("listitem");
    expect(rows[2]!.querySelector(".money")).toBeNull();
    // warn items carry the status tone class (amber), not a money colour
    renderWithProviders(<ActivityFeed items={[activity[0]!]} />);
    expect(document.querySelector('[data-id="event_log:207"]')).toHaveClass("tone-warn");
  });

  it("renders links as router links (44 px) and items without a link as text", async () => {
    const { location } = renderWithProviders(<ActivityFeed items={ITEMS} />, { route: "/dashboard" });
    const row = document.querySelector<HTMLElement>('[data-id="trade:9"]')!;
    const link = within(row).getByRole("link");
    expect(link).toHaveAttribute("href", "/trades?position=9");
    expect(link).toHaveClass("link-touch");
    const noLink = document.querySelector<HTMLElement>('[data-id="order:23:cancelled"]')!;
    expect(within(noLink).queryByRole("link")).not.toBeInTheDocument();
    await userEvent.click(link);
    expect(location().pathname + location().search).toBe("/trades?position=9");
  });
});

describe("activity panel error (acceptance test 8)", () => {
  it("null items with an error show the error and Retry without throwing", async () => {
    const onRetry = vi.fn();
    renderWithProviders(<ActivityFeed items={null} error="OperationalError: activity could not be read" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("OperationalError: activity could not be read");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("null items without an error still render a panel", () => {
    renderWithProviders(<ActivityFeed items={null} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Activity not available");
  });
});

describe("XSS (acceptance test 9)", () => {
  it("renders every free-text field literally", () => {
    const { live } = withXssText();
    const { container } = renderWithProviders(<ActivityFeed items={live.activity} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain(`Exit HHH (stop): -5.80, -1.00 R${XSS}`);
  });
});
