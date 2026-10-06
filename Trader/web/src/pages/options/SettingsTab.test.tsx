// OPTSIM-T15: the Settings tab: the `options.*` settings, edited through the options client.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeApiClient } from "../../test/fakeApi";
import { FakeOptionsApiClient } from "../../test/optionsFakeApi";
import { optSettingsItems } from "../../test/optionsFixtures";
import { SettingsTab } from "./SettingsTab";
import { renderOptions } from "./testRender";

const CAP = "options.max_position_pct";

describe("SettingsTab", () => {
  it("lists every options setting with its description (ASSUMPTION notes included)", async () => {
    renderOptions(<SettingsTab />);
    await screen.findByRole("article", { name: CAP });
    expect(screen.getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual(optSettingsItems.map((s) => s.key));
    expect(screen.getByText("ASSUMPTION: no fee per assignment; verify with the broker.")).toBeVisible();
    expect(within(screen.getByRole("article", { name: "options.watchlist" })).getByText(/Changed by web:stephen/)).toBeInTheDocument();
  });

  it("an edit is saved through the options client, never the stock settings route", async () => {
    const user = userEvent.setup();
    const api = new FakeApiClient();
    const opt = new FakeOptionsApiClient({ optPutSetting: { ...optSettingsItems[0]!, value: "0.40", is_default: false } });
    renderOptions(<SettingsTab />, { api, opt });
    const row = await screen.findByRole("article", { name: CAP });
    const save = within(row).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    const input = within(row).getByLabelText("Max Position Pct");
    await user.clear(input);
    await user.type(input, "0.40");
    await user.click(save);
    await waitFor(() => expect(opt.callsTo("optPutSetting")).toEqual([[CAP, "0.40"]]));
    expect(await within(row).findByText("Saved.")).toBeInTheDocument();
    expect(within(row).getByText(/Current: 0\.40/)).toBeInTheDocument();
    expect(api.callsTo("putSetting")).toEqual([]);
  });

  it("a value outside the bounds cannot be saved, and the server's 422 is shown", async () => {
    const user = userEvent.setup();
    const opt = new FakeOptionsApiClient().fail(
      "optPutSetting",
      new ApiError(422, "validation", "Invalid input", [{ loc: ["body", "value"], msg: "Input should be less than or equal to 1" }]),
    );
    renderOptions(<SettingsTab />, { opt });
    const row = await screen.findByRole("article", { name: CAP });
    const input = within(row).getByLabelText("Max Position Pct");
    await user.clear(input);
    await user.type(input, "1.5");
    expect(within(row).getByText("Must be more than 0 and at most 1")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(opt.callsTo("optPutSetting")).toEqual([]);

    await user.clear(input);
    await user.type(input, "0.9");
    await user.click(within(row).getByRole("button", { name: "Save" }));
    expect(await within(row).findByRole("alert")).toHaveTextContent("Input should be less than or equal to 1");
  });
});
