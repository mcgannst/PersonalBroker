// DB-T8 acceptance test 7 (today's timeline) and its parts of 8 (panel error) and 9 (XSS).
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SessionInfoOut, TimelineItemOut } from "../../api/types";
import { timeline } from "../../test/fixtures";
import { liveOut, withXssText, XSS } from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import { TodayTimeline } from "./TodayTimeline";

/** The next session after Saturday 2026-10-10 (what the API's session info carries on a closed day). */
const closedDay: SessionInfoOut = { date: "2026-10-12", phase: "closed_day", is_session: false, open_at: null, close_at: null };

describe("TodayTimeline (acceptance test 7)", () => {
  it("lists the session day's jobs and events with MT times and ✓ / next / ✗ marks", () => {
    const items: TimelineItemOut[] = [
      ...timeline.slice(0, 4),
      { key: "checkin@11:30", label: "Check-in", kind: "job", at: "2026-10-06T15:30:00Z", status: "failed", detail: "Telegram timed out" },
      ...timeline.slice(5),
    ];
    renderWithProviders(<TodayTimeline timeline={items} session={liveOut.session} />);
    const region = screen.getByRole("region", { name: "Today" });
    const list = within(region).getByRole("list", { name: "Timeline" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(items.length);
    expect(rows[0]).toHaveTextContent("06:00 MT");
    expect(rows[0]).toHaveTextContent("Pre-market scan");
    expect(rows[0]).toHaveTextContent("✓");
    expect(rows[3]).toHaveAttribute("aria-current", "step");
    expect(rows[3]).toHaveTextContent("next");
    expect(rows[4]).toHaveTextContent("✗ failed");
    expect(rows[4]).toHaveTextContent("Telegram timed out");
    expect(within(region).queryByText(/Market closed today/)).not.toBeInTheDocument();
  });

  it("says the market is closed and names the next session on a non-session day", () => {
    renderWithProviders(<TodayTimeline timeline={[]} session={closedDay} />);
    const region = screen.getByRole("region", { name: "Today" });
    expect(within(region).getByText("Market closed today; next session 2026-10-12")).toBeInTheDocument();
    expect(within(region).queryByRole("list")).not.toBeInTheDocument();
  });

  it("a session day with no schedule says so", () => {
    renderWithProviders(<TodayTimeline timeline={[]} session={liveOut.session} />);
    expect(screen.getByText("No schedule for this day")).toBeInTheDocument();
  });
});

describe("today panel error (acceptance test 8)", () => {
  it("null timeline with an error shows the error and Retry without throwing", async () => {
    const onRetry = vi.fn();
    renderWithProviders(<TodayTimeline timeline={null} session={liveOut.session} error="OperationalError: timeline could not be read" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("OperationalError: timeline could not be read");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("null timeline without an error still renders a panel", () => {
    renderWithProviders(<TodayTimeline timeline={null} session={liveOut.session} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Timeline not available");
  });
});

describe("XSS (acceptance test 9)", () => {
  it("renders labels and details literally", () => {
    const { live } = withXssText();
    const { container } = renderWithProviders(<TodayTimeline timeline={live.timeline} session={live.session} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain(`Pre-market scan${XSS}`);
  });
});
