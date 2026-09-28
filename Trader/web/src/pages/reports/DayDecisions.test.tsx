// P6-T12 acceptance test 6: the Reports page "Day" view (the decision log of one session) over the fake
// client: summary and rows, filters, a row expanded to its checks, MT times, the CSV link, empty and error
// states, and the `/reports?day=` deep link.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import ReportsPage from "../Reports";
import { DAY_PAGE_SIZE, dayHref } from "./DayDecisions";

function table(): HTMLElement {
  return screen.getByRole("table", { name: "Decisions" });
}

function bodyRows(): HTMLElement[] {
  return within(table()).getAllByRole("row").slice(1);
}

describe("Reports Day view", () => {
  it("the deep link /reports?day= opens the day with its summary and rows", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    expect(await screen.findByRole("heading", { name: "Decisions by day" })).toBeInTheDocument();
    expect(await screen.findByText(fx.decisionSummaryText)).toBeInTheDocument();
    await waitFor(() => expect(r.api.callsTo("decisionDay")).toEqual([[{ date: "2026-10-06", limit: DAY_PAGE_SIZE, offset: 0 }]]));
    // the week view is not shown
    expect(r.api.callsTo("metrics")).toEqual([]);
    const counts = screen.getByLabelText("Day counts");
    expect(within(counts).getByText("Scanned").parentElement).toHaveTextContent("811");
    expect(within(counts).getByText("Passed").parentElement).toHaveTextContent("1");
    expect(within(counts).getByText("P&L").parentElement).toHaveTextContent("$12.50");
    expect(screen.getByText(/Top rejects: rvol_below_min 790, catalyst_low_quality 6, doji 3/)).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(fx.decisionRows.length);
  });

  it("shows each row's time in MT, stage, ticker, outcome, rule and reason", async () => {
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    await screen.findByRole("table", { name: "Decisions" });
    const rejected = bodyRows()[3] as HTMLElement;
    expect(rejected).toHaveTextContent("07:35 MT"); // 13:35:05Z
    expect(rejected).toHaveTextContent("9:35 scan");
    expect(rejected).toHaveTextContent("AMD");
    expect(rejected).toHaveTextContent("rejected");
    expect(rejected).toHaveTextContent("rvol_below_min");
    const exit = bodyRows()[5] as HTMLElement;
    expect(exit).toHaveTextContent("13:55 MT");
    expect(exit).toHaveTextContent("flatten_close");
  });

  it("stored text is plain text, never HTML", async () => {
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    await screen.findByRole("table", { name: "Decisions" });
    expect(screen.getByText("<b>Guidance raised</b> after the close")).toBeInTheDocument();
    expect(document.querySelector("td b")).toBeNull();
  });

  it("expands a row to its checks and data", async () => {
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    await screen.findByRole("table", { name: "Decisions" });
    expect(screen.queryByRole("table", { name: "Checks" })).toBeNull();
    const button = screen.getByRole("button", { name: "Show details of decision 3" });
    expect(button).toHaveStyle({ minHeight: "44px" });
    await userEvent.click(button);
    const detail = screen.getByRole("region", { name: "Details of decision 3" });
    const checks = within(detail).getByRole("table", { name: "Checks" });
    const lines = within(checks).getAllByRole("row").slice(1);
    expect(lines).toHaveLength(3);
    expect(lines[0]).toHaveTextContent(/rvol\s*3\.20\s*>=\s*1\.00\s*pass/);
    expect(lines[1]).toHaveTextContent(/price\s*22\.40\s*between\s*5-50\s*pass/);
    expect(lines[2]).toHaveTextContent(/atr14\s*n\/a\s*present\s*n\/a\s*n\/a/);
    expect(detail).toHaveTextContent('"entry": "10.2500"');
    await userEvent.click(screen.getByRole("button", { name: "Hide details of decision 3" }));
    expect(screen.queryByRole("region", { name: "Details of decision 3" })).toBeNull();
  });

  it("the filters ask the server for a narrower day", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    await screen.findByRole("table", { name: "Decisions" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Stage" }), "scan");
    await waitFor(() =>
      expect(r.api.callsTo("decisionDay").at(-1)).toEqual([{ date: "2026-10-06", stage: "scan", limit: DAY_PAGE_SIZE, offset: 0 }]),
    );
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Outcome" }), "rejected");
    await waitFor(() =>
      expect(r.api.callsTo("decisionDay").at(-1)).toEqual([
        { date: "2026-10-06", stage: "scan", outcome: "rejected", limit: DAY_PAGE_SIZE, offset: 0 },
      ]),
    );
    await userEvent.type(screen.getByRole("searchbox", { name: "Ticker" }), "amd");
    await waitFor(() =>
      expect(r.api.callsTo("decisionDay").at(-1)).toEqual([
        { date: "2026-10-06", stage: "scan", outcome: "rejected", ticker: "AMD", limit: DAY_PAGE_SIZE, offset: 0 },
      ]),
    );
  });

  it("a filter that matches nothing says so and keeps the summary", async () => {
    const api = new FakeApiClient();
    api.respond("decisionDay", (q) => (q.stage ? { ...fx.decisionDayOut, rows: [], total: 0 } : fx.decisionDayOut));
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06", api });
    await screen.findByRole("table", { name: "Decisions" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Stage" }), "kill_switch");
    expect(await screen.findByText("No decisions match these filters.")).toBeInTheDocument();
    expect(screen.getByText(fx.decisionSummaryText)).toBeInTheDocument();
  });

  it("links the CSV download for the day (and the run when given)", async () => {
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    const link = await screen.findByRole("link", { name: "Download CSV" });
    expect(link).toHaveAttribute("href", "/api/export/decisions.csv?date=2026-10-06");
    expect(link).toHaveClass("link-touch");
  });

  it("a run in the URL is passed to every request and shown on a replay", async () => {
    const api = new FakeApiClient({ decisionDay: { ...fx.decisionDayOut, run_id: 42, run_mode: "replay" } });
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06&run=42", api });
    expect(await screen.findByText("replay run 42")).toBeInTheDocument();
    expect(r.api.callsTo("decisionDay")[0]).toEqual([{ date: "2026-10-06", run_id: 42, limit: DAY_PAGE_SIZE, offset: 0 }]);
    expect(r.api.callsTo("decisionDays")[0]).toEqual([{ limit: 60, run_id: 42 }]);
    expect(screen.getByRole("link", { name: "Download CSV" })).toHaveAttribute(
      "href",
      "/api/export/decisions.csv?date=2026-10-06&run_id=42",
    );
  });

  it("without a date it opens the latest day with rows", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?day=" });
    expect(await screen.findByText(fx.decisionSummaryText)).toBeInTheDocument();
    expect(r.api.callsTo("decisionDay")[0]).toEqual([{ date: fx.decisionDaysOut.days[0]?.session_date, limit: DAY_PAGE_SIZE, offset: 0 }]);
    expect(screen.getByLabelText("Day")).toHaveValue("2026-10-06");
  });

  it("picking another recent day moves the URL", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    await screen.findByRole("table", { name: "Decisions" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Recent days" }), "2026-10-05");
    expect(r.location().search).toBe("?day=2026-10-05");
    await waitFor(() => expect(r.api.callsTo("decisionDay").at(-1)).toEqual([{ date: "2026-10-05", limit: DAY_PAGE_SIZE, offset: 0 }]));
  });

  it("no rows at all says so", async () => {
    const api = new FakeApiClient({ decisionDays: { days: [] } });
    renderWithProviders(<ReportsPage />, { route: "/reports?day=", api });
    expect(await screen.findByText(/No decisions recorded yet/)).toBeInTheDocument();
    expect(api.callsTo("decisionDay")).toEqual([]);
  });

  it("a day without rows (404) says so", async () => {
    const api = new FakeApiClient({ decisionDay: null });
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-03", api });
    expect(await screen.findByText("No decisions recorded for 2026-10-03.")).toBeInTheDocument();
  });

  it("an error shows the message and Retry asks again", async () => {
    const api = new FakeApiClient().fail("decisionDay", new ApiError(500, "internal", "The server had a problem"));
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06", api });
    expect(await screen.findByRole("alert")).toHaveTextContent("The server had a problem");
    api.succeed("decisionDay");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(fx.decisionSummaryText)).toBeInTheDocument();
  });

  it("a day without a summary says it is not written yet", async () => {
    const api = new FakeApiClient({ decisionDay: { ...fx.decisionDayOut, summary: null, final: false } });
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06", api });
    expect(await screen.findByText(/No summary for this day yet/)).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(fx.decisionRows.length);
  });

  it("pages through a long day", async () => {
    const api = new FakeApiClient({ decisionDay: { ...fx.decisionDayOut, total: 450 } });
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06", api });
    await screen.findByRole("table", { name: "Decisions" });
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() =>
      expect(api.callsTo("decisionDay").at(-1)).toEqual([{ date: "2026-10-06", limit: DAY_PAGE_SIZE, offset: DAY_PAGE_SIZE }]),
    );
  });

  it("a malformed day or run requests nothing", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-13-40" });
    expect(await screen.findByText(/not a date/)).toBeInTheDocument();
    expect(r.api.callsTo("decisionDay")).toEqual([]);
    r.unmount();
    const r2 = renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06&run=abc" });
    expect(await screen.findByText(/not a run id/)).toBeInTheDocument();
    expect(r2.api.callsTo("decisionDays")).toEqual([]);
  });

  it("the week view links to the Day view and back", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?week=2026-10-09" });
    await userEvent.click(await screen.findByRole("link", { name: "Decisions by day" }));
    expect(r.location().search).toBe("?day=");
    expect(await screen.findByText(fx.decisionSummaryText)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Week view" }));
    expect(r.location().search).toBe("?week=2026-10-06");
  });

  it("every link has a 44 px touch class, every button its min-height (phone layout)", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    await screen.findByRole("table", { name: "Decisions" });
    await userEvent.click(screen.getByRole("button", { name: "Show details of decision 3" }));
    const links = Array.from(document.querySelectorAll("a"));
    expect(links.length).toBeGreaterThanOrEqual(2);
    for (const a of links) expect(a.classList.contains("link-touch"), a.textContent ?? "").toBe(true);
    for (const b of screen.getAllByRole("button")) expect(b).toHaveStyle({ minHeight: "44px" });
    // wide tables scroll inside their wrapper, never the page
    for (const t of screen.getAllByRole("table")) expect(t.parentElement).toHaveClass("table-scroll");
  });

  it("dayHref keeps the run", () => {
    expect(dayHref("2026-10-06", null)).toBe("/reports?day=2026-10-06");
    expect(dayHref("2026-10-06", 7)).toBe("/reports?day=2026-10-06&run=7");
  });
});
