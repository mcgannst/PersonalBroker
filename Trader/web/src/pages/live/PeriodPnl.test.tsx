// DB-T7 acceptance test 4 (PeriodPnl part): three columns, "partial" and "–" for the open P&L.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { periods } from "../../test/liveFixtures";
import { PeriodPnl } from "./PeriodPnl";

describe("PeriodPnl (acceptance test 4)", () => {
  it("shows today, week and since start as three columns", () => {
    render(<PeriodPnl periods={periods} />);
    const region = screen.getByRole("region", { name: "Gains by period" });
    const cols = within(region).getAllByTestId(/^period-/);
    expect(cols.map((c) => c.getAttribute("data-testid"))).toEqual(["period-today", "period-week", "period-run"]);
    const week = screen.getByTestId("period-week");
    expect(within(week).getByText("This week")).toBeInTheDocument();
    expect(within(week).getByText("+$19.73")).toHaveClass("money", "up");
    expect(within(week).getByText("+$8.73")).toHaveClass("money", "up"); // realised
    expect(within(week).getByText("+$11.00")).toHaveClass("money", "up"); // open
    expect(week).toHaveTextContent("Trades1");
    expect(week).toHaveTextContent("W / L1 / 0");
    expect(week).toHaveTextContent("Win rate100.00%");
    // expectancy only in the run column
    expect(within(week).queryByText(/Expectancy/)).not.toBeInTheDocument();
    expect(screen.getByTestId("period-run")).toHaveTextContent("Expectancy+0.71R");
  });

  it("shows – for a missing open P&L, win rate or expectancy, and marks a partial open P&L", () => {
    const rows = periods.map((p) =>
      p.period === "today"
        ? { ...p, unrealized: null, pnl_after_fees: "0.0000" }
        : p.period === "week"
          ? { ...p, unrealized_partial: true }
          : { ...p, expectancy_r: null, win_rate: null },
    );
    render(<PeriodPnl periods={rows} />);
    const today = screen.getByTestId("period-today");
    expect(within(today).getByTestId("open-today")).toHaveTextContent("–");
    expect(within(today).getByTestId("open-today").querySelector(".money")).toBeNull();
    for (const zero of within(today).getAllByText("$0.00")) expect(zero).toHaveClass("money", "flat");
    expect(today).toHaveTextContent("Win rate–");
    expect(screen.getByTestId("open-week")).toHaveTextContent("+$11.00 partial");
    expect(screen.getByTestId("period-run")).toHaveTextContent("Expectancy–");
    expect(screen.getByTestId("period-run")).toHaveTextContent("Win rate–");
  });

  it("shows its error with Retry, and an empty state without data", async () => {
    const onRetry = vi.fn();
    const { rerender } = render(<PeriodPnl periods={null} error="OperationalError: periods could not be read" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("OperationalError: periods could not be read");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    rerender(<PeriodPnl periods={null} />);
    expect(screen.getByText("No P&L data")).toBeInTheDocument();
  });
});
