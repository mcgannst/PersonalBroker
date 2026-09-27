// P4-T14 acceptance tests 6 and 7 (Reports side): the weekly Reports page the Telegram weekly link opens.
// P5-T13 acceptance tests 3-5: the weekly Claude commentary on it.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import ReportsPage from "../Reports";
import { addDays, isIsoDate, todayEt, tradingWeek } from "./dates";

afterEach(() => {
  vi.useRealTimers();
});

describe("Reports page", () => {
  it("?week=2026-10-09 asks for that Monday-Friday week and shows its heading and links", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-10-09" });
    expect(await screen.findByRole("heading", { name: "Week of 2026-10-05 to 2026-10-09" })).toBeInTheDocument();
    const want = { from: "2026-10-05", to: "2026-10-09" };
    await waitFor(() => expect(r.api.callsTo("metrics")).toEqual([[want]]));
    expect(r.api.callsTo("journal")).toEqual([[want]]);
    expect(r.api.callsTo("trades")).toEqual([[{ ...want, limit: 50 }]]);
    expect(screen.getByRole("link", { name: /Previous week/ })).toHaveAttribute("href", "/reports?week=2026-10-02");
    expect(screen.getByRole("link", { name: /Next week/ })).toHaveAttribute("href", "/reports?week=2026-10-16");
  });

  it("shows the week's metrics, trades and each day's journal answer", async () => {
    renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-10-09" });
    expect(await screen.findByText("Win rate")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "BBB" })).toHaveAttribute("href", "/trades?position=3");
    const journal = screen.getByRole("region", { name: "Journal" });
    const days = within(journal).getAllByRole("listitem");
    expect(days).toHaveLength(5);
    expect(days[0]).toHaveTextContent("2026-10-05");
    expect(days[0]).toHaveTextContent("Yes (Telegram)");
    expect(days[1]).toHaveTextContent("2026-10-06");
    expect(days[1]).toHaveTextContent("Not answered");
    expect(within(days[0] as HTMLElement).getByRole("link")).toHaveAttribute("href", "/journal?date=2026-10-05");
  });

  // P5-T13: the Phase 4 note line ("The Claude commentary arrives with the weekly report.") is replaced by the
  // commentary itself or the reason there is none.
  it("shows the week's Claude commentary instead of the Phase 4 note line", async () => {
    renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-11-25" });
    const card = await screen.findByRole("region", { name: "Commentary" });
    expect(card).toHaveTextContent(/A short holiday week with 4 sessions/);
    expect(card).toHaveTextContent("Generated 2026-11-28 08:00 MT by claude-sonnet-5, US$0.0123");
    expect(screen.queryByText(/Claude commentary arrives with the weekly report/)).toBeNull();
  });

  it("a report without commentary shows the reason; the rest of the page is unchanged", async () => {
    const api = new FakeApiClient({ weeklyReport: fx.weeklyReportBudget });
    renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-11-25", api });
    expect(await screen.findByText("The daily Claude budget was used up")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Commentary" })).toBeNull();
    expect(await screen.findByText("Win rate")).toBeInTheDocument();
  });

  it("no weekly report (404) says so and the metrics, trades and journal still render", async () => {
    const api = new FakeApiClient({ weeklyReport: null });
    renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-10-09", api });
    expect(await screen.findByText(/No weekly report for this week yet/)).toBeInTheDocument();
    expect(await screen.findByText("Win rate")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "BBB" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Journal" })).getAllByRole("listitem")).toHaveLength(5);
    expect(screen.queryByRole("region", { name: "Commentary" })).toBeNull();
  });

  it("?week=2026-11-25 asks for the report of Monday 2026-11-23; Previous week asks for that Monday", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-11-25" });
    await waitFor(() => expect(r.api.callsTo("weeklyReport")).toEqual([["2026-11-23"]]));
    await userEvent.click(screen.getByRole("link", { name: /Previous week/ }));
    expect(await screen.findByRole("heading", { name: "Week of 2026-11-16 to 2026-11-20" })).toBeInTheDocument();
    await waitFor(() => expect(r.api.callsTo("weeklyReport")).toEqual([["2026-11-23"], ["2026-11-16"]]));
  });

  it("a Saturday link (the Telegram weekly link's week ending + 1) asks for the week just ended", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-11-28" });
    await waitFor(() => expect(r.api.callsTo("weeklyReport")).toEqual([["2026-11-23"]]));
  });

  it("a malformed week never asks for a weekly report", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-13-40" });
    expect(await screen.findByText(/not a date/i)).toBeInTheDocument();
    expect(r.api.callsTo("weeklyReport")).toEqual([]);
  });

  it("a Saturday opens the week that just ended; following Previous moves the week", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-10-10" });
    expect(await screen.findByRole("heading", { name: "Week of 2026-10-05 to 2026-10-09" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: /Previous week/ }));
    expect(r.location().search).toBe("?week=2026-10-02");
    expect(await screen.findByRole("heading", { name: "Week of 2026-09-28 to 2026-10-02" })).toBeInTheDocument();
  });

  it("empty metrics show No trades yet without errors", async () => {
    const api = new FakeApiClient({ metrics: fx.emptyMetrics, trades: { items: [] }, journal: { items: [] } });
    renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-10-09", api });
    expect(await screen.findByText("No trades yet")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a malformed week shows a message and requests nothing", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-13-40" });
    expect(await screen.findByText(/not a date/i)).toBeInTheDocument();
    expect(r.api.callsTo("metrics")).toEqual([]);
  });

  it("without ?week it shows the current week", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-07T16:00:00Z"));
    renderWithProviders(<ReportsPage />, { route: "/reports" });
    expect(await screen.findByRole("heading", { name: "Week of 2026-10-05 to 2026-10-09" })).toBeInTheDocument();
  });
});

describe("dates", () => {
  it("tradingWeek maps every day to its Monday-Friday week", () => {
    expect(tradingWeek("2026-10-05")).toEqual({
      monday: "2026-10-05",
      friday: "2026-10-09",
      days: ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"],
    });
    expect(tradingWeek("2026-10-09").monday).toBe("2026-10-05");
    expect(tradingWeek("2026-10-11").monday).toBe("2026-10-05"); // Sunday
    expect(tradingWeek("2026-10-12").monday).toBe("2026-10-12");
    expect(tradingWeek("2026-12-31").friday).toBe("2027-01-01");
  });

  it("isIsoDate and addDays", () => {
    expect(isIsoDate("2026-10-06")).toBe(true);
    expect(isIsoDate("2026-02-30")).toBe(false);
    expect(isIsoDate("2026-10-6")).toBe(false);
    expect(isIsoDate(null)).toBe(false);
    expect(addDays("2026-10-09", -7)).toBe("2026-10-02");
    expect(addDays("2026-02-28", 1)).toBe("2026-03-01");
  });

  it("todayEt is the New York date", () => {
    expect(todayEt(new Date("2026-10-07T16:00:00Z"))).toBe("2026-10-07");
  });
});
