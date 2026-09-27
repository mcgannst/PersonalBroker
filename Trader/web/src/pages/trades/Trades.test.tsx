// P4-T14 acceptance tests 1-3: the Trades page (list, detail chain, chart).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { TradeOut } from "../../api/types";
import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import TradesPage from "../Trades";
import { TradeChart } from "./TradeChart";

function manyTrades(n: number): TradeOut[] {
  return Array.from({ length: n }, (_, i) => ({ ...fx.trade, id: 1000 + i, position_id: 2000 + i }));
}

describe("Trades list", () => {
  it("renders fixture trades with fmtMoney and fmtR", async () => {
    renderWithProviders(<TradesPage />, { route: "/trades" });
    const bbb = await screen.findByRole("row", { name: /BBB/ });
    expect(within(bbb).getByText("2026-10-05")).toBeInTheDocument();
    expect(within(bbb).getByText("$19.73")).toBeInTheDocument();
    expect(within(bbb).getByText("+1.60R")).toBeInTheDocument();
    expect(within(bbb).getByText("14.215")).toBeInTheDocument();
    expect(within(bbb).getByText("14.88")).toBeInTheDocument();
    expect(within(bbb).getByText("flatten")).toBeInTheDocument();
    const eee = screen.getByRole("row", { name: /EEE/ });
    expect(within(eee).getByText("-$9.90")).toBeInTheDocument();
    expect(within(eee).getByText("-1.00R")).toBeInTheDocument();
  });

  it("asks for 50 per page, newest first, and Next requests offset=50", async () => {
    const api = new FakeApiClient({ trades: { items: manyTrades(50) } });
    renderWithProviders(<TradesPage />, { route: "/trades", api });
    await screen.findAllByRole("row", { name: /BBB/ });
    expect(api.callsTo("trades")[0]?.[0]).toEqual({ limit: 50, offset: 0 });
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(api.callsTo("trades").at(-1)?.[0]).toEqual({ limit: 50, offset: 50 }));
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    await waitFor(() => expect(api.callsTo("trades").at(-1)?.[0]).toEqual({ limit: 50, offset: 0 }));
  });

  it("disables Next on a short page and Previous on the first page", async () => {
    renderWithProviders(<TradesPage />, { route: "/trades" });
    await screen.findByRole("row", { name: /BBB/ });
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  });

  it("filters by date range from the first page", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<TradesPage />, { route: "/trades", api });
    await screen.findByRole("row", { name: /BBB/ });
    await userEvent.type(screen.getByLabelText("From"), "2026-10-02");
    await userEvent.type(screen.getByLabelText("To"), "2026-10-05");
    await waitFor(() =>
      expect(api.callsTo("trades").at(-1)?.[0]).toEqual({ from: "2026-10-02", to: "2026-10-05", limit: 50, offset: 0 }),
    );
  });

  it("opening a row sets ?position=<id> and shows the detail", async () => {
    const r = renderWithProviders(<TradesPage />, { route: "/trades" });
    await userEvent.click(await screen.findByRole("link", { name: "BBB" }));
    expect(r.location().search).toBe("?position=3");
    expect(await screen.findByRole("heading", { name: /BBB/ })).toBeInTheDocument();
  });

  it("shows an error with Retry when the list fails", async () => {
    const api = new FakeApiClient().fail("trades", new Error("boom"));
    renderWithProviders(<TradesPage />, { route: "/trades", api });
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong.");
    api.succeed("trades");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("row", { name: /BBB/ })).toBeInTheDocument();
  });
});

describe("Position detail (/trades?position=3)", () => {
  it("renders the whole chain", async () => {
    const r = renderWithProviders(<TradesPage />, { route: "/trades?position=3" });
    expect(await screen.findByRole("heading", { name: /BBB/ })).toBeInTheDocument();
    expect(r.api.callsTo("position")).toEqual([[3]]);

    const signal = screen.getByRole("region", { name: "Signal" });
    expect(within(signal).getByText(/orb_open/)).toBeInTheDocument();
    expect(within(signal).getByText("rvol")).toBeInTheDocument();
    expect(within(signal).getByText("2.40")).toBeInTheDocument();
    expect(within(signal).getByText("atr")).toBeInTheDocument();
    expect(within(signal).getByText("0.52")).toBeInTheDocument();
    expect(within(signal).getByText(/high 14\.20/)).toBeInTheDocument();

    const proposals = screen.getByRole("region", { name: "Proposals" });
    const items = within(proposals).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent(/entry/i);
    expect(items[0]).toHaveTextContent("via web");
    expect(items[0]).toHaveTextContent("web:stephen");
    expect(items[0]).toHaveTextContent("35s");
    expect(items[1]).toHaveTextContent("via auto");

    const orders = screen.getByRole("region", { name: "Orders" });
    const orderItems = within(orders).getAllByRole("listitem");
    expect(orderItems).toHaveLength(3);
    expect(orderItems[1]).toHaveTextContent("position flattened");

    const fills = screen.getByRole("region", { name: "Fills" });
    const fillItems = within(fills).getAllByRole("listitem");
    expect(fillItems).toHaveLength(2);
    expect(fillItems[0]).toHaveTextContent("bid 14.20");
    expect(fillItems[0]).toHaveTextContent("ask 14.22");
    expect(fillItems[0]).toHaveTextContent("07:37 MT");

    const result = screen.getByRole("region", { name: "Result" });
    expect(result).toHaveTextContent("$19.73");
    expect(result).toHaveTextContent("+1.60R");
  });

  it("an open position shows Open and no result block", async () => {
    renderWithProviders(<TradesPage />, { route: "/trades?position=4" });
    expect(await screen.findByRole("heading", { name: /CCC/ })).toBeInTheDocument();
    expect(screen.getByText("Open")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Result" })).not.toBeInTheDocument();
  });

  it("an unknown position shows the server's message", async () => {
    renderWithProviders(<TradesPage />, { route: "/trades?position=999" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Position 999 not found");
  });

  it("a malformed id shows a message without calling the API", async () => {
    const r = renderWithProviders(<TradesPage />, { route: "/trades?position=abc" });
    expect(await screen.findByText(/not a valid position/i)).toBeInTheDocument();
    expect(r.api.callsTo("position")).toEqual([]);
  });

  it("Back to trades clears the position", async () => {
    const r = renderWithProviders(<TradesPage />, { route: "/trades?position=3" });
    await userEvent.click(await screen.findByRole("link", { name: /Back to trades/ }));
    expect(r.location().search).toBe("");
  });
});

describe("TradeChart", () => {
  const chartProps = {
    candles: fx.candles,
    entry: "14.2150",
    stop: "13.8000",
    exit: "14.8800",
    fills: fx.fills,
    chartError: null,
    width: 400,
  };

  it("draws the candles and reference lines for entry, stop and exit", () => {
    const { container } = renderWithProviders(<TradeChart {...chartProps} />);
    const lines = container.querySelectorAll(".recharts-reference-line");
    expect(lines).toHaveLength(3);
    const labels = [...lines].map((l) => l.textContent);
    expect(labels).toEqual(["Entry 14.215", "Stop 13.80", "Exit 14.88"]);
    expect(container.querySelector(".recharts-line-curve")).not.toBeNull();
    expect(container.querySelector(".recharts-area-area")).not.toBeNull();
    expect(container.querySelectorAll(".recharts-reference-dot")).toHaveLength(2);
  });

  it("leaves out a reference line whose price is missing", () => {
    const { container } = renderWithProviders(<TradeChart {...chartProps} exit={null} />);
    expect(container.querySelectorAll(".recharts-reference-line")).toHaveLength(2);
  });

  it("shows Chart unavailable on chart_error or no candles", () => {
    const a = renderWithProviders(<TradeChart {...chartProps} chartError="quotes unavailable" />);
    expect(a.getByText("Chart unavailable")).toBeInTheDocument();
    expect(a.container.querySelector("svg.recharts-surface")).toBeNull();
    a.unmount();
    const b = renderWithProviders(<TradeChart {...chartProps} candles={[]} />);
    expect(b.getByText("Chart unavailable")).toBeInTheDocument();
  });

  it("the detail page passes chart_error through", async () => {
    const api = new FakeApiClient({ position: { ...fx.positionDetail, chart_error: "no candles" } });
    renderWithProviders(<TradesPage />, { route: "/trades?position=3", api });
    expect(await screen.findByText("Chart unavailable")).toBeInTheDocument();
  });
});
