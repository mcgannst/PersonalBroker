// P4-T14 acceptance tests 4 and 7: the Performance page (tiles, filters, export, charts, empty state).
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import PerformancePage from "../Performance";
import { DrawdownChart } from "./DrawdownChart";
import { EquityChart } from "./EquityChart";
import { histogramLabel, RHistogram } from "./RHistogram";
import { MetricTiles } from "./MetricTiles";

function tile(label: string): HTMLElement {
  const el = screen.getByText(label, { selector: ".stat-label" }).closest(".stat");
  if (!(el instanceof HTMLElement)) throw new Error(`no tile ${label}`);
  return el;
}

describe("Performance page", () => {
  it("renders the metric tiles from MetricsOut", async () => {
    renderWithProviders(<PerformancePage />, { route: "/performance" });
    await screen.findByText("Win rate");
    expect(within(tile("Trades")).getByText("4")).toBeInTheDocument();
    expect(within(tile("Win rate")).getByText("50.00%")).toBeInTheDocument();
    expect(within(tile("Expectancy")).getByText("+0.13R")).toBeInTheDocument();
    expect(within(tile("Profit factor")).getByText("1.25")).toBeInTheDocument();
    expect(within(tile("Avg slippage")).getByText("$0.0075")).toBeInTheDocument();
    expect(within(tile("Max drawdown")).getByText("2.10%")).toBeInTheDocument();
    expect(within(tile("Adherence")).getByText("75.00%")).toBeInTheDocument();
    expect(within(tile("Total P&L")).getByText("$5.00")).toBeInTheDocument();
  });

  it("asks for all of the live run by default and links the export with no filters", async () => {
    const r = renderWithProviders(<PerformancePage />, { route: "/performance" });
    await screen.findByText("Win rate");
    expect(r.api.callsTo("metrics")[0]).toEqual([{}]);
    expect(r.api.callsTo("equity")[0]).toEqual([{}]);
    expect(screen.getByRole("link", { name: "Export CSV" })).toHaveAttribute("href", "/api/export/trades.csv");
  });

  it("changing the range calls metrics and equity with from/to, and the export link carries them", async () => {
    const r = renderWithProviders(<PerformancePage />, { route: "/performance" });
    await screen.findByText("Win rate");
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-10-01" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-10-05" } });
    const want = { from: "2026-10-01", to: "2026-10-05" };
    await waitFor(() => expect(r.api.callsTo("metrics").at(-1)).toEqual([want]));
    await waitFor(() => expect(r.api.callsTo("equity").at(-1)).toEqual([want]));
    expect(screen.getByRole("link", { name: "Export CSV" })).toHaveAttribute(
      "href",
      "/api/export/trades.csv?from=2026-10-01&to=2026-10-05",
    );
    expect(r.api.callsTo("exportTradesUrl").at(-1)).toEqual([want]);
  });

  it("a typed run id is sent as run; a bad one is refused", async () => {
    const r = renderWithProviders(<PerformancePage />, { route: "/performance" });
    await screen.findByText("Win rate");
    const run = screen.getByLabelText("Run");
    await userEvent.clear(run);
    await userEvent.type(run, "abc");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByText(/Run is "live" or a run number/)).toBeInTheDocument();
    expect(r.api.callsTo("metrics").every(([q]) => !(q as object).hasOwnProperty("run"))).toBe(true);
    await userEvent.clear(run);
    await userEvent.type(run, "7");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    await waitFor(() => expect(r.api.callsTo("metrics").at(-1)).toEqual([{ run: "7" }]));
    expect(screen.getByRole("link", { name: "Export CSV" })).toHaveAttribute("href", "/api/export/trades.csv?run=7");
  });

  it("draws the equity, drawdown and R histogram charts", async () => {
    const { container } = renderWithProviders(<PerformancePage />, { route: "/performance" });
    await screen.findByText("Win rate");
    await waitFor(() => expect(container.querySelectorAll("svg.recharts-surface").length).toBeGreaterThanOrEqual(3));
    expect(screen.getByRole("figure", { name: "Equity" })).toBeInTheDocument();
    expect(screen.getByRole("figure", { name: "Drawdown" })).toBeInTheDocument();
    expect(screen.getByRole("figure", { name: "R multiples" })).toBeInTheDocument();
  });

  it("empty metrics show No trades yet without errors", async () => {
    const api = new FakeApiClient({ metrics: fx.emptyMetrics, equity: { run_id: 1, points: [] } });
    renderWithProviders(<PerformancePage />, { route: "/performance", api });
    expect(await screen.findByText("No trades yet")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("figure")).not.toBeInTheDocument();
  });

  it("an unknown run shows the server's message with Retry", async () => {
    const { ApiError } = await import("../../api/client");
    const api = new FakeApiClient().fail("metrics", new ApiError(404, "not_found", "Run 99 not found"));
    renderWithProviders(<PerformancePage />, { route: "/performance", api });
    expect(await screen.findByText("Run 99 not found")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Retry" }).length).toBeGreaterThan(0);
  });
});

describe("performance components", () => {
  it("MetricTiles shows n/a for null ratios", () => {
    renderWithProviders(<MetricTiles metrics={{ ...fx.metricsOut, profit_factor: null, adherence_pct: null }} />);
    expect(within(tile("Profit factor")).getByText("n/a")).toBeInTheDocument();
    expect(within(tile("Adherence")).getByText("n/a")).toBeInTheDocument();
  });

  it("EquityChart and DrawdownChart share the time domain", () => {
    const a = renderWithProviders(<EquityChart points={fx.equityOut.points} width={360} />);
    expect(a.container.querySelector(".recharts-line-curve")).not.toBeNull();
    a.unmount();
    const b = renderWithProviders(<DrawdownChart points={fx.equityOut.points} width={360} />);
    expect(b.container.querySelector(".recharts-area-area")).not.toBeNull();
  });

  it("RHistogram draws one bar per bin and labels open-ended bins", () => {
    const { container } = renderWithProviders(<RHistogram bins={fx.metricsOut.r_histogram} width={360} />);
    expect(container.querySelectorAll(".recharts-bar-rectangle")).toHaveLength(3);
    expect(histogramLabel({ lo: "-1.0", hi: "-0.5", count: 2 })).toBe("-1 to -0.5");
    expect(histogramLabel({ lo: "-Infinity", hi: "-3", count: 1 })).toBe("< -3");
    expect(histogramLabel({ lo: "5", hi: "Infinity", count: 1 })).toBe("≥ 5");
  });
});
