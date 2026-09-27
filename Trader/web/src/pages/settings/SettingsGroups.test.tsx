import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { settingsItems } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { SettingsGroups } from "./SettingsGroups";

async function row(key: string) {
  return screen.findByRole("article", { name: key });
}

describe("SettingsGroups", () => {
  it("renders one collapsible group per SettingOut.group, in order, without approval_mode", async () => {
    renderWithProviders(<SettingsGroups />);
    await row("risk_pct");
    const summaries = screen.getAllByText(/^(Account|Risk|Fill model|Screening|Worker and Telegram|Kill switches|Approvals)$/, {
      selector: "summary",
    });
    expect(summaries.map((s) => s.textContent)).toEqual(["Account", "Risk", "Fill model", "Screening", "Worker and Telegram", "Kill switches"]);
    expect(screen.queryByRole("article", { name: "approval_mode" })).toBeNull();
  });

  it("shows the current value, the default marker and who changed it", async () => {
    renderWithProviders(<SettingsGroups />);
    const risk = await row("risk_pct");
    expect(within(risk).getByText("Risk Pct")).toBeInTheDocument();
    expect(within(risk).getByText("default")).toBeInTheDocument();
    expect(within(risk).getByText(/Current: 0\.02/)).toBeInTheDocument();
    const daily = await row("killswitch.daily_loss_pct");
    expect(within(daily).queryByText("default")).toBeNull();
    expect(within(daily).getByText("Changed by web:stephen, 2026-10-01 09:00 MT")).toBeInTheDocument();
  });

  it("Save is enabled only for a changed, valid field and sends the typed value", async () => {
    const api = new FakeApiClient({ putSetting: { ...settingsItems.find((s) => s.key === "risk_pct")!, value: "0.03", is_default: false } });
    renderWithProviders(<SettingsGroups />, { api });
    const risk = await row("risk_pct");
    const save = within(risk).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    const input = within(risk).getByLabelText("Risk Pct");
    await userEvent.clear(input);
    await userEvent.type(input, "0.03");
    expect(save).toBeEnabled();
    await userEvent.click(save);
    expect(api.callsTo("putSetting")).toEqual([["risk_pct", "0.03"]]);
    expect(await within(risk).findByText("Saved.")).toBeInTheDocument();
    expect(within(risk).getByText(/Current: 0\.03/)).toBeInTheDocument();
  });

  it("a value above the maximum shows the rule and disables Save (acceptance test 2)", async () => {
    renderWithProviders(<SettingsGroups />);
    const risk = await row("risk_pct");
    const input = within(risk).getByLabelText("Risk Pct");
    await userEvent.clear(input);
    await userEvent.type(input, "0.5");
    expect(within(risk).getByText("Must be more than 0 and at most 0.10")).toBeInTheDocument();
    expect(within(risk).getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("a string_list setting is sent as an array", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<SettingsGroups />, { api });
    const extra = await row("universe.extra_symbols");
    const input = within(extra).getByLabelText("Universe Extra Symbols");
    await userEvent.clear(input);
    await userEvent.type(input, "SPY, QQQ");
    await userEvent.click(within(extra).getByRole("button", { name: "Save" }));
    expect(api.callsTo("putSetting")).toEqual([["universe.extra_symbols", ["SPY", "QQQ"]]]);
  });

  it("a server 422 with fields shows the message under the field (acceptance test 3)", async () => {
    const api = new FakeApiClient();
    api.fail(
      "putSetting",
      new ApiError(422, "validation_error", "Invalid input", [{ loc: ["body", "value"], msg: "Input should be less than or equal to 0.05" }]),
    );
    renderWithProviders(<SettingsGroups />, { api });
    const risk = await row("risk_pct");
    const input = within(risk).getByLabelText("Risk Pct");
    await userEvent.clear(input);
    await userEvent.type(input, "0.06");
    await userEvent.click(within(risk).getByRole("button", { name: "Save" }));
    const msg = await within(risk).findByText("Input should be less than or equal to 0.05");
    expect(msg).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(within(risk).queryByText("Saved.")).toBeNull();
  });

  it("a load failure shows the error with Retry", async () => {
    const api = new FakeApiClient();
    api.fail("settings", new ApiError(500, "internal", "Database unavailable"));
    renderWithProviders(<SettingsGroups />, { api });
    expect(await screen.findByText("Database unavailable")).toBeInTheDocument();
    api.succeed("settings");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await row("risk_pct")).toBeInTheDocument();
  });
});
