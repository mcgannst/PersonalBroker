import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { settingsItems } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { ApprovalMode } from "./ApprovalMode";

const autoSettings = {
  items: settingsItems.map((s) => (s.key === "approval_mode" ? { ...s, value: "auto", is_default: false } : s)),
};

describe("ApprovalMode (acceptance test 1)", () => {
  it("shows the current mode", async () => {
    renderWithProviders(<ApprovalMode />);
    expect(await screen.findByRole("button", { name: "Manual" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Auto" })).toHaveAttribute("aria-pressed", "false");
  });

  it("Manual to Auto asks for confirmation and confirming saves auto", async () => {
    const api = new FakeApiClient({ putSetting: { ...settingsItems[0]!, value: "auto", is_default: false } });
    renderWithProviders(<ApprovalMode />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Auto" }));
    expect(screen.getByText("Every order will be placed without asking you. Continue?")).toBeInTheDocument();
    expect(api.callsTo("putSetting")).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Switch to Auto" }));
    expect(api.callsTo("putSetting")).toEqual([["approval_mode", "auto"]]);
    expect(await screen.findByRole("button", { name: "Auto" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("Every order will be placed without asking you. Continue?")).toBeNull();
  });

  it("cancelling calls nothing", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<ApprovalMode />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Auto" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.callsTo("putSetting")).toEqual([]);
    expect(screen.queryByText("Every order will be placed without asking you. Continue?")).toBeNull();
    expect(screen.getByRole("button", { name: "Manual" })).toHaveAttribute("aria-pressed", "true");
  });

  it("Auto to Manual saves at once, and a server error is shown", async () => {
    const api = new FakeApiClient({ settings: autoSettings });
    api.fail("putSetting", new ApiError(500, "internal", "Could not save the setting."));
    renderWithProviders(<ApprovalMode />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Manual" }));
    expect(api.callsTo("putSetting")).toEqual([["approval_mode", "manual"]]);
    expect(await screen.findByText("Could not save the setting.")).toBeInTheDocument();
  });
});
