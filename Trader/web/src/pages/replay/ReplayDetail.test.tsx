// P5-T8 acceptance test 6: the completed replay's detail (comparison, warnings, trades, events).
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { fmtDateTime } from "../../lib/format";
import { replayCompleted, replayRunning } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import ReplayPage from "../Replay";

function cellsOf(table: HTMLElement, label: string): string[] {
  const row = within(table).getByRole("rowheader", { name: label }).closest("tr")!;
  return within(row)
    .getAllByRole("cell")
    .map((c) => c.textContent ?? "");
}

describe("the completed replay (acceptance test 6)", () => {
  it("shows the header, the comparison with replay, live and difference for each metric", async () => {
    renderWithProviders(<ReplayPage />, { route: "/replay?id=11" });
    expect(await screen.findByRole("heading", { level: 2, name: "Thanksgiving week (biased universe)" })).toBeInTheDocument();
    const header = screen.getByRole("region", { name: "Replay 11" });
    expect(within(header).getByText("completed")).toHaveClass("badge", "tone-ok");
    expect(within(header).getByText("2026-11-23 → 2026-11-27")).toBeInTheDocument();
    expect(within(header).getByText("Data: full · catalysts: stored · half spread 5 bps")).toBeInTheDocument();
    expect(within(header).getByText(`Started ${fmtDateTime(replayCompleted.created_at)}, finished ${fmtDateTime(replayCompleted.finished_at)}`)).toBeInTheDocument();

    const table = await screen.findByRole("table", { name: "Replay compared with live" });
    const heads = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(heads).toEqual(["Metric", "Replay", "Live (same dates)", "Difference"]);
    expect(cellsOf(table, "Trades")).toEqual(["5", "4", "+1"]);
    expect(cellsOf(table, "Win rate")).toEqual(["40.00%", "50.00%", "-10.00%"]);
    expect(cellsOf(table, "Expectancy")).toEqual(["+0.13R", "+0.10R", "+0.03R"]);
    expect(cellsOf(table, "Profit factor")).toEqual(["1.087", "1.20", "-0.113"]);
    expect(cellsOf(table, "Total P&L")).toEqual(["$2.00", "$4.10", "-$2.10"]);
    expect(cellsOf(table, "Max drawdown")).toEqual(["3.10%", "2.50%", "+0.60%"]);
    expect(cellsOf(table, "Avg slippage")).toEqual(["$0.20", "$0.15", "+$0.05"]);
  });

  it("shows the biased-days warning, the other warnings and the pinned strategies", async () => {
    renderWithProviders(<ReplayPage />, { route: "/replay?id=11" });
    const warnings = await screen.findByRole("region", { name: "Warnings" });
    expect(within(warnings).getByText("biased universe")).toBeInTheDocument();
    expect(within(warnings).getByText("1 biased day (today's universe was used): 2026-11-23")).toBeInTheDocument();
    expect(within(warnings).getByText("1 forced close at the end of a day")).toBeInTheDocument();
    expect(within(warnings).getByText("Missing data: 2 opening bars and 1 symbol-day of 1-minute bars")).toBeInTheDocument();

    const pinned = screen.getByRole("region", { name: "Strategies" });
    const orb = within(pinned).getByText("orb_sip").closest("li")!;
    expect(orb).toHaveTextContent("revision 2");
    expect(within(orb).getByText("override")).toHaveClass("badge");
    const spy = within(pinned).getByText("spy_overlay").closest("li")!;
    expect(within(spy).queryByText("override")).toBeNull();
  });

  it("shows the trades table, the equity curve and the events", async () => {
    const r = renderWithProviders(<ReplayPage />, { route: "/replay?id=11" });
    const trades = await screen.findByRole("table", { name: "Replay trades" });
    const rows = within(trades).getAllByRole("row");
    expect(rows.map((row) => within(row).queryAllByRole("columnheader").map((h) => h.textContent)).at(0)).toEqual([
      "Date",
      "Ticker",
      "Entry",
      "Exit",
      "R",
      "P&L",
      "Exit reason",
    ]);
    expect(within(rows[1]!).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "2026-10-05",
      "BBB",
      "14.215",
      "14.88",
      "+1.60R",
      "$19.73",
      "flatten",
    ]);
    expect(within(rows[2]!).getByText("-$9.90")).toBeInTheDocument();
    expect(r.api.callsTo("trades")).toEqual([[{ run: "11", limit: 100 }]]);
    expect(r.api.callsTo("equity")).toEqual([[{ run: "11" }]]);
    expect(await screen.findByRole("figure", { name: "Equity" })).toBeInTheDocument();

    const events = screen.getByRole("list", { name: "Events" });
    const items = within(events).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Forced close of 1 position at 12:59 ET");
    expect(items[0]).toHaveTextContent(fmtDateTime(replayCompleted.events[0]!.ts));
    expect(items[1]).toHaveTextContent("2 opening bars missing on 2026-11-23");
  });

  it("a running replay has no comparison yet", async () => {
    renderWithProviders(<ReplayPage />, { route: "/replay?id=13" });
    await screen.findByRole("progressbar", { name: "Progress" });
    expect(screen.queryByRole("table", { name: "Replay compared with live" })).toBeNull();
    expect(screen.getByText(/The comparison with the live run appears when the replay has finished/)).toBeInTheDocument();
    expect(replayRunning.metrics).toBeNull();
  });

  it("a failed replay shows its error", async () => {
    const api = new FakeApiClient({ replay: { ...replayCompleted, status: "failed", error: "RuntimeError: boom", metrics: null, live_metrics: null } });
    renderWithProviders(<ReplayPage />, { route: "/replay?id=11", api });
    const warnings = await screen.findByRole("region", { name: "Warnings" });
    expect(within(warnings).getByText("Failed: RuntimeError: boom")).toBeInTheDocument();
  });

  it("an unknown id shows the 404 message and a bad id never calls the API", async () => {
    const r = renderWithProviders(<ReplayPage />, { route: "/replay?id=999" });
    expect(await screen.findByText("Replay 999 not found")).toBeInTheDocument();
    r.unmount();
    const api = new FakeApiClient().fail("replay", new ApiError(500, "internal", "nope"));
    const r2 = renderWithProviders(<ReplayPage />, { route: "/replay?id=abc", api });
    expect(await screen.findByText("“abc” is not a valid replay id.")).toBeInTheDocument();
    expect(r2.api.callsTo("replay")).toHaveLength(0);
  });
});
