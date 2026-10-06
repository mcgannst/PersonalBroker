// OPTSIM-T15: the Strategies tab: one section per plug-in with its switch, its generated form and its panel.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeOptionsApiClient } from "../../test/optionsFakeApi";
import { toyStrategy } from "../../test/optionsFixtures";
import { StrategiesTab } from "./StrategiesTab";
import { renderOptions } from "./testRender";

async function section(key: string) {
  return screen.findByRole("region", { name: key });
}

describe("StrategiesTab", () => {
  it("shows one section per plug-in, each with its own panel", async () => {
    const { opt } = renderOptions(<StrategiesTab />);
    const toy = await section("toy_call");
    const second = await section("second_plugin");
    expect(within(toy).getByText(/v0\.1\.0 · revision 2 · 1 open/)).toBeInTheDocument();
    expect(within(toy).getByRole("checkbox", { name: "Enabled" })).toBeChecked();
    expect(within(second).getByRole("checkbox", { name: "Enabled" })).not.toBeChecked();
    expect(await within(toy).findByRole("region", { name: "Calls bought" })).toBeInTheDocument();
    expect(await within(second).findByRole("region", { name: "Tickers" })).toBeInTheDocument();
    expect(opt.callsTo("optPanel").map(([key]) => key).sort()).toEqual(["second_plugin", "toy_call"]);
  });

  it("an ASSUMPTION note in a field's description is visible", async () => {
    renderOptions(<StrategiesTab />);
    const toy = await section("toy_call");
    expect(within(toy).getByText("ASSUMPTION: 0.99 per contract until the broker's schedule is confirmed.")).toBeVisible();
  });

  it("the Enabled switch saves only `enabled`, and warns while positions are open", async () => {
    const user = userEvent.setup();
    const opt = new FakeOptionsApiClient({ optPutStrategy: { ...toyStrategy, enabled: false, revision: 3 } });
    renderOptions(<StrategiesTab />, { opt });
    const toy = await section("toy_call");
    const save = within(toy).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    await user.click(within(toy).getByRole("checkbox", { name: "Enabled" }));
    expect(within(toy).getByRole("note")).toHaveTextContent(/opens nothing new/);
    await user.click(save);
    await waitFor(() => expect(opt.callsTo("optPutStrategy")).toEqual([["toy_call", { enabled: false }]]));
    expect(await within(toy).findByText("Saved as revision 3.")).toBeInTheDocument();
  });

  it("a changed param is sent alone; a 422 shows the server's message under that field", async () => {
    const user = userEvent.setup();
    const opt = new FakeOptionsApiClient().fail(
      "optPutStrategy",
      new ApiError(422, "validation", "Invalid input", [{ loc: ["body", "params", "min_dte"], msg: "Input should be less than or equal to 60" }]),
    );
    renderOptions(<StrategiesTab />, { opt });
    const toy = await section("toy_call");
    const minDte = within(toy).getByLabelText("Min DTE");
    await user.clear(minDte);
    await user.type(minDte, "0");
    expect(within(toy).getByText("Must be at least 1 and at most 365")).toBeInTheDocument();
    expect(within(toy).getByRole("button", { name: "Save" })).toBeDisabled();
    await user.clear(minDte);
    await user.type(minDte, "90");
    await user.click(within(toy).getByRole("button", { name: "Save" }));
    expect(await within(toy).findByText("Input should be less than or equal to 60")).toBeInTheDocument();
    expect(opt.callsTo("optPutStrategy")).toEqual([["toy_call", { params: { min_dte: 90 } }]]);
  });

  it("no plug-ins: says so", async () => {
    renderOptions(<StrategiesTab />, { opt: new FakeOptionsApiClient({ optStrategies: { items: [] } }) });
    expect(await screen.findByText("No option strategies are installed")).toBeInTheDocument();
  });
});
