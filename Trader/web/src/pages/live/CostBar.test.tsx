// DB-T7 acceptance test 4 (CostBar part): today's Claude spend against the cap, fees and net after AI.
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ClaudeTodayOut } from "../../api/types";
import { claudeToday, periods } from "../../test/liveFixtures";
import { CostBar } from "./CostBar";

function claude(spent: string, cap: string, fraction: string | null): ClaudeTodayOut {
  return { ...claudeToday, spent_usd: spent, cap_usd: cap, used_fraction: fraction };
}

function fill(): HTMLElement {
  return screen.getByRole("progressbar", { name: "Claude spend today against the cap" }).querySelector<HTMLElement>(".lva-meter-fill")!;
}

describe("CostBar (acceptance test 4)", () => {
  it("at 0 %: an empty bar and the exact numbers", () => {
    render(<CostBar claude={claude("0.0000", "1.0000", "0.0000")} periods={periods} />);
    expect(fill().style.width).toBe("0%");
    expect(fill()).toHaveClass("status-ok");
    expect(screen.getByTestId("claude-today")).toHaveTextContent("$0.00 of $1.00 cap (0.00%)");
  });

  it("at 50 %: half a bar in the accent", () => {
    render(<CostBar claude={claude("0.5000", "1.0000", "0.5000")} periods={periods} />);
    expect(fill().style.width).toBe("50%");
    expect(fill()).toHaveClass("status-ok");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  });

  it("at 80 %: amber", () => {
    render(<CostBar claude={claude("0.8000", "1.0000", "0.8000")} periods={periods} />);
    expect(fill().style.width).toBe("80%");
    expect(fill()).toHaveClass("status-warn");
  });

  it("at 130 %: the bar is capped at 100 % and the text stays exact", () => {
    render(<CostBar claude={claude("1.3000", "1.0000", "1.3000")} periods={periods} />);
    expect(fill().style.width).toBe("100%");
    expect(fill()).toHaveClass("status-warn");
    expect(screen.getByTestId("claude-today")).toHaveTextContent("$1.30 of $1.00 cap (130.00%)");
  });

  it("with cap 0: no bar, text only", () => {
    render(<CostBar claude={claude("0.1200", "0.0000", null)} periods={periods} />);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.getByTestId("claude-today")).toHaveTextContent("$0.12 today (no cap set)");
  });

  it("shows fees, Claude spend and net after AI per period", () => {
    render(<CostBar claude={claudeToday} periods={periods} />);
    const run = screen.getByTestId("costs-run");
    expect(within(run).getByText("Since start")).toBeInTheDocument();
    expect(within(run).getByText("$3.00")).toHaveClass("money", "flat"); // fees
    expect(within(run).getByText("$0.96")).toHaveClass("money", "flat"); // Claude
    expect(within(run).getByText("+$18.77")).toHaveClass("money", "up"); // net after AI
    expect(screen.getByTestId("costs-today")).toHaveTextContent("+$10.88");
  });

  it("with no data at all: an empty state", () => {
    render(<CostBar claude={null} periods={null} />);
    expect(screen.getByText("No cost data")).toBeInTheDocument();
  });
});
