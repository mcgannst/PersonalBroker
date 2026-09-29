// DB-T9 acceptance test 3 (kill switches): the lights with value vs threshold and last trip, and the reset
// through the reused KillSwitchPanel (typed reason 3-500 characters, then confirm).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { KillSwitchesOut, KillSwitchLightOut } from "../../api/types";
import * as fx from "../../test/fixtures";
import * as lfx from "../../test/liveFixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { KillSwitchCard } from "./KillSwitchCard";

function region() {
  return screen.getByRole("region", { name: "Kill switches" });
}

function lights() {
  return within(region()).getByRole("list", { name: "Kill switch lights" });
}

const trippedDrawdown: KillSwitchesOut = {
  switches: fx.killswitchStates.map((s) =>
    s.switch === "max_drawdown_pct"
      ? { ...s, tripped: true, tripped_at: "2026-10-06T14:00:00Z", value: "0.2100", threshold: "0.2000", automatic: true, needs_web_reset: true }
      : s,
  ),
  history: fx.killswitchEvents,
};

describe("KillSwitchCard", () => {
  it("lists each switch with its state, value vs threshold and how it clears", () => {
    renderWithProviders(<KillSwitchCard lights={lfx.killswitchLights} history={fx.killswitchEvents} />);
    const list = lights();
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(within(items[0]!).getByText("Daily loss")).toBeInTheDocument();
    expect(within(items[0]!).getByText("0.00% of 5.00% limit")).toBeInTheDocument();
    expect(within(items[0]!).getByText("OK")).toBeInTheDocument();
    // the last trip comes from the history when the switch is not tripped now
    expect(within(items[0]!).getByText("last trip 2026-10-02 11:20 MT")).toBeInTheDocument();
    expect(within(items[2]!).getByText("+0.71R (limit 0.00R), 1 of 20 trades")).toBeInTheDocument();
    expect(within(items[2]!).getByText("never tripped")).toBeInTheDocument();
    expect(within(items[3]!).getByText("Paused")).toBeInTheDocument();
    expect(within(items[3]!).getByText("Off")).toBeInTheDocument();
    expect(within(items[1]!).getByText("reset it on the Control page with a reason")).toBeInTheDocument();
  });

  it("a tripped switch shows Tripped with its trip time and trip value", () => {
    const tripped: KillSwitchLightOut[] = lfx.killswitchLights.map((l) =>
      l.switch === "daily_loss_pct"
        ? { ...l, tripped: true, tripped_at: "2026-10-06T15:00:00Z", value: "0.0520", trip_value: "0.0520", trip_threshold: "0.0500" }
        : l,
    );
    renderWithProviders(<KillSwitchCard lights={tripped} history={[]} />);
    const first = within(lights()).getAllByRole("listitem")[0]!;
    expect(within(first).getByText("Tripped")).toBeInTheDocument();
    expect(within(first).getByText("tripped 2026-10-06 09:00 MT at 5.20% (limit 5.00%)")).toBeInTheDocument();
  });

  it("resets through KillSwitchPanel only with a valid typed reason, after the confirm", async () => {
    const api = new FakeApiClient({ killswitches: trippedDrawdown });
    renderWithProviders(<KillSwitchCard lights={lfx.killswitchLights} history={fx.killswitchEvents} />, { api });
    await userEvent.click(await within(region()).findByRole("button", { name: "Reset Max drawdown" }));
    const box = within(region()).getByLabelText("Reason for the reset");
    await userEvent.type(box, "  a ");
    expect(within(region()).getByRole("button", { name: "Reset…" })).toBeDisabled();
    await userEvent.clear(box);
    await userEvent.type(box, "checked the books, drawdown understood");
    await userEvent.click(within(region()).getByRole("button", { name: "Reset…" }));
    expect(api.callsTo("resetKillSwitch")).toHaveLength(0);
    await userEvent.click(within(region()).getByRole("button", { name: "Confirm reset" }));
    await waitFor(() =>
      expect(api.callsTo("resetKillSwitch")).toEqual([["max_drawdown_pct", { reason: "checked the books, drawdown understood" }]]),
    );
  });

  it("with the lights part failed, shows its error and Retry and keeps the reset panel", async () => {
    const onRetry = vi.fn();
    renderWithProviders(<KillSwitchCard lights={null} history={null} error="OperationalError: killswitches could not be read" onRetry={onRetry} />);
    expect(within(region()).getAllByRole("alert")[0]).toHaveTextContent("OperationalError: killswitches could not be read");
    await userEvent.click(within(region()).getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(await within(region()).findByText("History")).toBeInTheDocument();
  });

  it("renders free text as plain text", () => {
    const { control } = lfx.withXssText();
    const { container } = renderWithProviders(<KillSwitchCard lights={control.killswitches} history={control.killswitch_history} />);
    expect(within(lights()).getByText(`Daily loss${lfx.XSS}`)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
});
