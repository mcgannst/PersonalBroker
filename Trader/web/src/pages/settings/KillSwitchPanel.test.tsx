import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import type { KillSwitchesOut } from "../../api/types";
import { killswitchDrawdownTripped, killswitchEvents, killswitchStates } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { KillSwitchPanel } from "./KillSwitchPanel";

const tripped: KillSwitchesOut = {
  switches: killswitchStates.map((s) => (s.switch === "max_drawdown_pct" ? killswitchDrawdownTripped : s)),
  history: killswitchEvents,
};

const paused: KillSwitchesOut = {
  switches: killswitchStates.map((s) =>
    s.switch === "manual_pause" ? { ...s, tripped: true, tripped_at: "2026-10-06T14:00:00Z", clears: "lift it with Resume (web) or /resume" } : s,
  ),
  history: [],
};

describe("KillSwitchPanel", () => {
  it("shows every switch with its state, and a tripped one with its clears text", async () => {
    renderWithProviders(<KillSwitchPanel />, { api: new FakeApiClient({ killswitches: tripped }) });
    expect(await screen.findByRole("status", { name: "Max drawdown: tripped" })).toHaveClass("tone-bad");
    expect(screen.getByRole("status", { name: "Daily loss: off" })).toHaveClass("tone-ok");
    expect(screen.getByRole("status", { name: "Paused: off" })).toHaveClass("tone-ok");
    expect(screen.getByText(killswitchDrawdownTripped.clears)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Reset/ })).toHaveLength(1);
  });

  it("a manual pause shows amber", async () => {
    renderWithProviders(<KillSwitchPanel />, { api: new FakeApiClient({ killswitches: paused }) });
    expect(await screen.findByRole("status", { name: "Paused: on" })).toHaveClass("tone-warn");
  });

  it("reset: disabled until the reason has 3 characters, then a confirm step calls resetKillSwitch (acceptance test 5)", async () => {
    const api = new FakeApiClient({ killswitches: tripped });
    renderWithProviders(<KillSwitchPanel />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Reset Max drawdown" }));
    const reason = screen.getByLabelText("Reason for the reset");
    const next = screen.getByRole("button", { name: "Reset…" });
    expect(next).toBeDisabled();
    await userEvent.type(reason, "ok");
    expect(next).toBeDisabled();
    await userEvent.type(reason, "!");
    expect(next).toBeEnabled();
    await userEvent.clear(reason);
    await userEvent.type(reason, "reviewed the drawdown");
    await userEvent.click(next);
    expect(api.callsTo("resetKillSwitch")).toEqual([]);
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("Reset Max drawdown? New entries can start again.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Confirm reset" }));
    expect(api.callsTo("resetKillSwitch")).toEqual([["max_drawdown_pct", { reason: "reviewed the drawdown" }]]);
    expect(await screen.findByText("Max drawdown reset.")).toBeInTheDocument();
  });

  it("a reason of only spaces does not count", async () => {
    renderWithProviders(<KillSwitchPanel />, { api: new FakeApiClient({ killswitches: tripped }) });
    await userEvent.click(await screen.findByRole("button", { name: "Reset Max drawdown" }));
    await userEvent.type(screen.getByLabelText("Reason for the reset"), "     ");
    expect(screen.getByRole("button", { name: "Reset…" })).toBeDisabled();
  });

  it("Pause asks for confirmation then calls pause() (acceptance test 5)", async () => {
    const api = new FakeApiClient({ pause: paused });
    renderWithProviders(<KillSwitchPanel />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("Block new entries? Exits and stops keep working.");
    expect(api.callsTo("pause")).toEqual([]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Pause new entries" }));
    expect(api.callsTo("pause")).toEqual([[]]);
    expect(await screen.findByRole("status", { name: "Paused: on" })).toBeInTheDocument();
  });

  it("cancelling the pause calls nothing", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<KillSwitchPanel />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.callsTo("pause")).toEqual([]);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("shows the server's 409 message and Resume calls resume()", async () => {
    const api = new FakeApiClient({ killswitches: paused });
    api.fail("pause", new ApiError(409, "conflict", "Already paused."));
    renderWithProviders(<KillSwitchPanel />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await userEvent.click(screen.getByRole("button", { name: "Pause new entries" }));
    expect(await screen.findByText("Already paused.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(api.callsTo("resume")).toEqual([[]]);
  });
});
