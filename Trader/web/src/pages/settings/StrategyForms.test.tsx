import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { orbSipStrategy, spyOverlayStrategy } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { StrategyForms } from "./StrategyForms";

async function card(key: string) {
  return screen.findByRole("article", { name: key });
}

describe("StrategyForms (acceptance test 4)", () => {
  it("shows each strategy's key, version, revision, kind and enabled state", async () => {
    renderWithProviders(<StrategyForms />);
    const orb = await card("orb_sip");
    expect(within(orb).getByText("orb_sip")).toBeInTheDocument();
    expect(within(orb).getByText("v1.0.0 · revision 3 · entry")).toBeInTheDocument();
    expect(within(orb).getByRole("checkbox", { name: "Enabled" })).toBeChecked();
    const spy = await card("spy_overlay");
    expect(within(spy).getByText("v1.0.0 · revision 1 · overlay")).toBeInTheDocument();
  });

  it("changing top_n and saving sends only that param", async () => {
    const api = new FakeApiClient({ putStrategy: { ...orbSipStrategy, revision: 4, params: { ...orbSipStrategy.params, top_n: 10 } } });
    renderWithProviders(<StrategyForms />, { api });
    const orb = await card("orb_sip");
    const save = within(orb).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    const topN = within(orb).getByLabelText("Top N");
    await userEvent.clear(topN);
    await userEvent.type(topN, "10");
    await userEvent.click(save);
    expect(api.callsTo("putStrategy")).toEqual([["orb_sip", { params: { top_n: 10 } }]]);
    expect(await within(await card("orb_sip")).findByText("v1.0.0 · revision 4 · entry")).toBeInTheDocument();
  });

  it("disabling a strategy that owns an open position shows the warning and sends enabled only", async () => {
    const api = new FakeApiClient({ putStrategy: { ...orbSipStrategy, enabled: false, revision: 4 } });
    renderWithProviders(<StrategyForms />, { api });
    const orb = await card("orb_sip");
    expect(within(orb).queryByText(/keeps managing its open position until flat/)).toBeNull();
    await userEvent.click(within(orb).getByRole("checkbox", { name: "Enabled" }));
    expect(within(orb).getByText(/keeps managing its open position until flat/)).toBeInTheDocument();
    await userEvent.click(within(orb).getByRole("button", { name: "Save" }));
    expect(api.callsTo("putStrategy")).toEqual([["orb_sip", { enabled: false }]]);
  });

  it("disabling a strategy without open positions shows no warning", async () => {
    renderWithProviders(<StrategyForms />);
    const spy = await card("spy_overlay");
    await userEvent.click(within(spy).getByRole("checkbox", { name: "Enabled" }));
    expect(within(spy).queryByText(/keeps managing its open position until flat/)).toBeNull();
  });

  it("an invalid param disables Save, and a server 422 shows under the param", async () => {
    const api = new FakeApiClient();
    api.fail(
      "putStrategy",
      new ApiError(422, "validation_error", "Invalid input", [{ loc: ["body", "params", "top_n"], msg: "Input should be greater than 0" }]),
    );
    renderWithProviders(<StrategyForms />, { api });
    const orb = await card("orb_sip");
    const topN = within(orb).getByLabelText("Top N");
    await userEvent.clear(topN);
    await userEvent.type(topN, "0");
    expect(within(orb).getByText("Must be at least 1 and at most 200")).toBeInTheDocument();
    expect(within(orb).getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.clear(topN);
    await userEvent.type(topN, "5");
    await userEvent.click(within(orb).getByRole("button", { name: "Save" }));
    expect(await within(orb).findByText("Input should be greater than 0")).toBeInTheDocument();
  });

  it("a string param with a pattern and the enabled switch are sent together", async () => {
    const api = new FakeApiClient({ putStrategy: spyOverlayStrategy });
    renderWithProviders(<StrategyForms />, { api });
    const spy = await card("spy_overlay");
    const bench = within(spy).getByLabelText("Benchmark");
    await userEvent.clear(bench);
    await userEvent.type(bench, "QQQ");
    await userEvent.click(within(spy).getByRole("checkbox", { name: "Enabled" }));
    await userEvent.click(within(spy).getByRole("button", { name: "Save" }));
    expect(api.callsTo("putStrategy")).toEqual([["spy_overlay", { params: { benchmark: "QQQ" }, enabled: false }]]);
  });
});
