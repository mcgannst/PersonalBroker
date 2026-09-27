// P5-GW web gauntlet, attempt 1 (Breaker): the Replay page (P5-T8) and the Reports commentary (P5-T13).
// Hostile text in every server string the pages show, a double-submitted start and a double-clicked cancel,
// exact decimal differences, 44 px targets in the error and empty states, and the weekly report's week around
// DST. FakeApiClient and fixtures only: no network.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import type { MetricsOut, ReplayOut, ReplaySummaryOut } from "../api/types";
import ReplayPage from "../pages/Replay";
import ReportsPage from "../pages/Reports";
import { CompareTable } from "../pages/replay/CompareTable";
import { NO_REPORT_TEXT } from "../pages/reports/Commentary";
import { FakeApiClient } from "../test/fakeApi";
import * as fx from "../test/fixtures";
import { renderWithProviders } from "../test/render";

// ---------------------------------------------------------------- helpers

const XSS = `<img src=x onerror="alert(1)"><script>alert(1)</script><a href="javascript:alert(1)">x</a><svg onload=alert(1)>`;
const tagged = (label: string): string => `${label}${XSS}`;
const URL_ATTRS = new Set(["href", "src", "action", "formaction", "xlink:href"]);

/** No injected markup: no script/img/iframe/svg[onload], no on* attribute, no javascript: URL. */
function expectInert(root: ParentNode = document.body): void {
  expect(root.querySelectorAll("script, iframe, object, embed, img, svg[onload]")).toHaveLength(0);
  for (const el of Array.from(root.querySelectorAll("*"))) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      expect(name.startsWith("on"), `<${el.tagName.toLowerCase()}> has ${name}`).toBe(false);
      if (URL_ATTRS.has(name)) expect(attr.value.replace(/[\u0000- ]/g, "")).not.toMatch(/^(javascript|data|vbscript):/i);
    }
  }
}

async function settle(): Promise<void> {
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "Loading" })).toHaveLength(0));
}

/** Every button and touch link in the document is a 44 px target with a name. */
function expectTouchTargets(where: string): void {
  for (const b of Array.from(document.querySelectorAll<HTMLButtonElement>("button"))) {
    const name = (b.getAttribute("aria-label") ?? b.textContent ?? "").trim();
    expect(name, `${where}: a button without a name`).not.toBe("");
    expect(parseFloat(b.style.minHeight || "0"), `${where}: button "${name}" under 44 px`).toBeGreaterThanOrEqual(44);
  }
  for (const a of Array.from(document.querySelectorAll("a"))) {
    expect(["link-touch", "btn"].some((c) => a.classList.contains(c)), `${where}: link "${a.textContent}"`).toBe(true);
  }
}

function cells(table: HTMLElement, label: string): string[] {
  const row = within(table).getByRole("rowheader", { name: label }).closest("tr")!;
  return within(row)
    .getAllByRole("cell")
    .map((c) => c.textContent ?? "");
}

// ---------------------------------------------------------------- XSS

describe("hostile server text on the Replay page", () => {
  it("replay names, overrides, strategy params, events and errors render as inert text in the list and the detail", async () => {
    const hostileSummary: ReplaySummaryOut = { ...fx.replaySummaries[1]!, id: 11, label: tagged("list label") };
    const hostile: ReplayOut = {
      ...fx.replayCompleted,
      label: tagged("detail label"),
      status: "failed",
      error: tagged("RuntimeError"),
      metrics: null,
      live_metrics: null,
      overrides: { risk_pct: XSS, [XSS]: XSS },
      strategies: [{ key: tagged("orb"), config_id: 9, revision: 2, version: tagged("v"), scope: "replay", enabled: true, params: { top_n: XSS } }],
      events: [{ id: 1, ts: "2026-11-27T17:50:00Z", level: "error", source: tagged("src"), message: tagged("event"), data: { x: XSS } }],
    };
    const api = new FakeApiClient({ replays: { items: [hostileSummary] }, replay: hostile });

    const list = renderWithProviders(<ReplayPage />, { api, route: "/replay" });
    const table = await screen.findByRole("table", { name: "Replays" });
    expect(within(table).getByRole("link", { name: /list label/ })).toHaveTextContent("<script>alert(1)</script>");
    expectInert();
    list.unmount();

    renderWithProviders(<ReplayPage />, { api, route: "/replay?id=11" });
    await screen.findByRole("heading", { level: 2, name: /detail label/ });
    await settle();
    expect(document.body).toHaveTextContent("Failed: RuntimeError<img");
    expect(screen.getByRole("list", { name: "Events" })).toHaveTextContent("event<img");
    expectInert();
  });

  it("422 field messages and the server message of a failed start render as text", async () => {
    const api = new FakeApiClient().fail(
      "startReplay",
      new ApiError(422, "validation", tagged("top message"), [
        { loc: ["body", "overrides", "risk_pct"], msg: tagged("under risk") },
        { loc: ["body", "strategies", "orb_sip", "params", "top_n"], msg: tagged("under top_n") },
        { loc: ["body", "strategies", XSS], msg: tagged("unplaced") },
      ]),
    );
    renderWithProviders(<ReplayPage />, { api, route: "/replay" });
    await userEvent.click(await screen.findByRole("button", { name: "New replay" }));
    const form = await screen.findByRole("form", { name: "New replay" });
    await settle();
    await userEvent.click(within(form).getByRole("button", { name: "Start replay" }));
    expect(await within(form).findByText(/under risk/)).toBeInTheDocument();
    expect(form).toHaveTextContent("top message<img");
    expect(form).toHaveTextContent("unplaced<img");
    expectInert();
  });
});

describe("hostile commentary on the Reports page, and the week it asks for", () => {
  it("commentary, model and cost from the server render as text", async () => {
    const api = new FakeApiClient({
      weeklyReport: { ...fx.weeklyReportOk, commentary: `${tagged("para one")}\n\n${tagged("para two")}`, model: tagged("model") },
    });
    renderWithProviders(<ReportsPage />, { api, route: "/reports?week=2026-11-25" });
    const card = await screen.findByRole("region", { name: "Commentary" });
    expect(card.querySelectorAll("p.commentary-paragraph")).toHaveLength(2);
    expect(card).toHaveTextContent("para two<img");
    expect(card).toHaveTextContent("by model<img");
    await settle();
    expectInert();
  });

  it.each([
    ["2026-11-01", "2026-10-26"], // the Sunday DST ends: the week just ended
    ["2026-11-02", "2026-11-02"],
    ["2026-03-08", "2026-03-02"], // the Sunday DST starts
    ["2026-03-09", "2026-03-09"],
    ["2026-09-07", "2026-09-07"], // Labor Day Monday
    ["2026-04-03", "2026-03-30"], // Good Friday
    ["2026-11-28", "2026-11-23"], // the Saturday the report is written
  ])("?week=%s asks for the report of the week starting %s", async (week, monday) => {
    const r = renderWithProviders(<ReportsPage />, { route: `/reports?week=${week}` });
    await waitFor(() => expect(r.api.callsTo("weeklyReport")).toEqual([[monday]]));
  });
});

// ---------------------------------------------------------------- double submit and cancel

describe("double submit and cancel", () => {
  it("a double-clicked Start (the request still in flight) starts one replay", async () => {
    const api = new FakeApiClient();
    api.respond("startReplay", () => new Promise<ReplayOut>(() => undefined)); // never answers
    renderWithProviders(<ReplayPage />, { api, route: "/replay" });
    await userEvent.click(await screen.findByRole("button", { name: "New replay" }));
    const form = await screen.findByRole("form", { name: "New replay" });
    await settle();
    const start = within(form).getByRole("button", { name: "Start replay" });
    fireEvent.click(start);
    fireEvent.click(start);
    fireEvent.submit(form);
    await waitFor(() => expect(start).toBeDisabled());
    fireEvent.submit(form);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(api.callsTo("startReplay")).toHaveLength(1);
  });

  it("Cancel asks first; a double-clicked Stop sends one cancel; Keep running sends none", async () => {
    const api = new FakeApiClient();
    api.respond("cancelReplay", () => new Promise<ReplayOut>(() => undefined));
    renderWithProviders(<ReplayPage />, { api, route: "/replay?id=13" });
    const cancel = await screen.findByRole("button", { name: "Cancel replay" });
    await userEvent.click(cancel);
    await userEvent.click(screen.getByRole("button", { name: "Keep running" }));
    expect(api.callsTo("cancelReplay")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Cancel replay" }));
    const dialog = screen.getByRole("alertdialog", { name: "Stop replay" });
    expect(dialog).toHaveTextContent("Stop this replay after the current day?");
    const stop = within(dialog).getByRole("button", { name: "Stop replay" });
    fireEvent.click(stop);
    fireEvent.click(stop);
    await waitFor(() => expect(stop).toBeDisabled());
    fireEvent.click(stop);
    expect(api.callsTo("cancelReplay")).toEqual([[13]]);
    expectTouchTargets("/replay?id=13 (confirm)");
  });
});

// ---------------------------------------------------------------- decimals

describe("the comparison is exact decimal arithmetic", () => {
  it("differences beyond float precision and float traps are exact", () => {
    const replay: MetricsOut = {
      ...fx.metricsOut,
      trades: 3,
      win_rate: "0.3",
      expectancy_r: "0.0150",
      profit_factor: "1.005",
      total_pnl: "90071992547409.93",
      max_drawdown_pct: "0.1",
      avg_slippage: "0.0003",
    };
    const live: MetricsOut = {
      ...fx.metricsOut,
      trades: 3,
      win_rate: "0.1",
      expectancy_r: "0.0050",
      profit_factor: "NaN",
      total_pnl: "0.01",
      max_drawdown_pct: "0.3",
      avg_slippage: "0.0001",
    };
    render(<CompareTable replay={replay} live={live} />);
    const table = screen.getByRole("table", { name: "Replay compared with live" });
    expect(cells(table, "Trades")).toEqual(["3", "3", "0"]);
    expect(cells(table, "Win rate")).toEqual(["30.00%", "10.00%", "+20.00%"]);
    expect(cells(table, "Expectancy")).toEqual(["+0.02R", "+0.01R", "+0.01R"]);
    expect(cells(table, "Profit factor")[2]).toBe("n/a");
    expect(cells(table, "Total P&L")).toEqual(["$90,071,992,547,409.93", "$0.01", "+$90,071,992,547,409.92"]);
    expect(cells(table, "Max drawdown")).toEqual(["10.00%", "30.00%", "-20.00%"]);
    expect(cells(table, "Avg slippage")[2]).toBe("+$0.0002");
  });
});

// ---------------------------------------------------------------- empty and error states

describe("empty and error states", () => {
  it("no replays, a failing list and failing options: a message each, New replay disabled, 44 px retries", async () => {
    const empty = renderWithProviders(<ReplayPage />, { api: new FakeApiClient({ replays: { items: [] } }), route: "/replay" });
    expect(await screen.findByText("No replays yet.")).toBeInTheDocument();
    empty.unmount();

    const api = new FakeApiClient()
      .fail("replays", new ApiError(500, "internal", "list broke"))
      .fail("replayOptions", new ApiError(503, "unavailable", "Replays are not available"));
    renderWithProviders(<ReplayPage />, { api, route: "/replay" });
    expect(await screen.findByText("list broke")).toBeInTheDocument();
    expect(await screen.findByText("Replays are not available")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New replay" })).toBeDisabled();
    expect(screen.queryByRole("form", { name: "New replay" })).toBeNull();
    expectTouchTargets("/replay (errors)");
  });

  it("a finished replay whose trades and equity fail shows errors, not an empty table", async () => {
    const api = new FakeApiClient()
      .fail("trades", new ApiError(500, "internal", "trades broke"))
      .fail("equity", new ApiError(500, "internal", "equity broke"));
    renderWithProviders(<ReplayPage />, { api, route: "/replay?id=11" });
    expect(await screen.findByText("trades broke")).toBeInTheDocument();
    expect(await screen.findByText("equity broke")).toBeInTheDocument();
    expect(screen.queryByText("No trades yet.")).toBeNull();
    expectTouchTargets("/replay?id=11 (errors)");
  });

  it("a failing weekly report is an error with a retry, never the 'no report yet' line", async () => {
    const api = new FakeApiClient().fail("weeklyReport", new ApiError(500, "internal", "report broke"));
    renderWithProviders(<ReportsPage />, { api, route: "/reports?week=2026-11-25" });
    expect(await screen.findByText("report broke")).toBeInTheDocument();
    expect(screen.queryByText(NO_REPORT_TEXT)).toBeNull();
    await settle();
    expectTouchTargets("/reports (error)");
  });
});
