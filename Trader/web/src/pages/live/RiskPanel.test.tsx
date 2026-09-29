// DB-T7 acceptance test 6: the risk panel.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { RiskOut } from "../../api/types";
import { riskOut } from "../../test/liveFixtures";
import { RiskPanel } from "./RiskPanel";

function renderRisk(risk: RiskOut | null, extra: { error?: string; onRetry?: () => void } = {}) {
  return render(
    <MemoryRouter>
      <RiskPanel risk={risk} {...extra} />
    </MemoryRouter>,
  );
}

function tripDrawdown(): RiskOut {
  return {
    ...riskOut,
    killswitches: riskOut.killswitches.map((k) =>
      k.switch === "max_drawdown_pct"
        ? { ...k, tripped: true, tripped_at: "2026-10-06T13:50:00Z", value: "0.2150", trip_value: "0.2150", trip_threshold: "0.2000" }
        : k,
    ),
  };
}

describe("RiskPanel (acceptance test 6)", () => {
  it("shows the four kill-switch lights with value vs threshold in their units", () => {
    renderRisk(riskOut);
    const region = screen.getByRole("region", { name: "Risk" });
    const lights = within(region).getAllByTestId(/^killswitch-/);
    expect(lights).toHaveLength(4);
    expect(screen.getByTestId("killswitch-daily_loss_pct")).toHaveTextContent("Daily loss0.0% of 5.0%");
    expect(screen.getByTestId("killswitch-max_drawdown_pct")).toHaveTextContent("Max drawdown0.1% of 20.0%");
    expect(screen.getByTestId("killswitch-expectancy")).toHaveTextContent("Expectancy0.71 R vs 0.00 R");
    expect(screen.getByTestId("killswitch-expectancy")).toHaveTextContent("1 of 20 trades");
    expect(screen.getByTestId("killswitch-manual_pause")).toHaveTextContent("PausedNo");
    for (const light of lights) {
      const dot = light.querySelector(".lva-dot")!;
      expect(dot).toHaveClass("status-ok");
    }
    expect(within(region).queryByRole("link")).not.toBeInTheDocument();
  });

  it("puts a tripped switch first, with its time in MT and a Reset on Control link", () => {
    renderRisk(tripDrawdown());
    const lights = screen.getAllByTestId(/^killswitch-/);
    expect(lights[0]).toHaveAttribute("data-testid", "killswitch-max_drawdown_pct");
    const tripped = lights[0]!;
    expect(tripped.querySelector(".lva-dot")).toHaveClass("status-bad");
    expect(tripped).toHaveTextContent("Tripped 07:50 MT");
    expect(tripped).toHaveTextContent("21.5% of 20.0%");
    expect(tripped).toHaveTextContent("reset it on the Control page with a reason");
    const link = within(tripped).getByRole("link", { name: "Reset on Control" });
    expect(link).toHaveAttribute("href", "/control");
    expect(link).toHaveClass("link-touch");
  });

  it("shows a manual pause in amber", () => {
    renderRisk({ ...riskOut, killswitches: riskOut.killswitches.map((k) => (k.switch === "manual_pause" ? { ...k, tripped: true, tripped_at: "2026-10-06T13:40:00Z" } : k)) });
    const paused = screen.getByTestId("killswitch-manual_pause");
    expect(paused.querySelector(".lva-dot")).toHaveClass("status-warn");
    expect(paused).toHaveTextContent("PausedYes");
  });

  it("shows open risk against its cap as a bar, the slots and the open positions", () => {
    renderRisk(riskOut);
    const risk = screen.getByTestId("open-risk");
    expect(risk).toHaveTextContent("$17.40 of $37.50");
    const bar = within(risk).getByRole("progressbar", { name: "Open risk against the cap" });
    expect(bar.querySelector<HTMLElement>(".lva-meter-fill")!.style.width).toBe("46.4%");
    expect(screen.getByTestId("slots")).toHaveTextContent("Slots1 / 3");
    expect(screen.getByTestId("open-positions")).toHaveTextContent("Open positions1");
  });

  it("with no cap: the open risk as text only", () => {
    renderRisk({ ...riskOut, open_risk_cap: null });
    expect(screen.getByTestId("open-risk")).toHaveTextContent("$17.40 (no cap)");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("shows its error with Retry", async () => {
    const onRetry = vi.fn();
    renderRisk(null, { error: "OperationalError: risk could not be read", onRetry });
    expect(screen.getByRole("alert")).toHaveTextContent("OperationalError: risk could not be read");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
