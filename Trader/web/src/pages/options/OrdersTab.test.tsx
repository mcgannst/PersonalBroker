// OPTSIM-T15: the Orders tab: cancel and reprice on manual orders only, and the fill quotes in the history.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeOptionsApiClient } from "../../test/optionsFakeApi";
import { filledOrder } from "../../test/optionsFixtures";
import { OrdersTab, fillWords } from "./OrdersTab";
import { REJECT_WORDS } from "./shared";
import { renderOptions } from "./testRender";

describe("OrdersTab", () => {
  it("lists the working orders with the limit in words", async () => {
    const { opt } = renderOptions(<OrdersTab />);
    const manual = await screen.findByRole("article", { name: "Order 31" });
    expect(within(manual).getByText("Sell to open F 2026-11-20 P 12.00")).toBeInTheDocument();
    expect(within(manual).getByText(/limit credit 0\.47 · day · sent 2026-10-06 07:45 MT · reserves \$1,200\.00/)).toBeInTheDocument();
    const strategy = screen.getByRole("article", { name: "Order 32" });
    expect(within(strategy).getByText(/limit debit 0\.225, walking · good till cancelled/)).toBeInTheDocument();
    expect(opt.callsTo("optOrders")).toEqual(expect.arrayContaining([[{ status: "working" }], [{ status: "history", limit: 50 }]]));
  });

  it("Cancel calls the API for that order", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<OrdersTab />);
    const manual = await screen.findByRole("article", { name: "Order 31" });
    await user.click(within(manual).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(opt.callsTo("optCancel")).toEqual([[31]]));
  });

  it("Reprice sends the new limit with its sign (a debit is negative)", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<OrdersTab />);
    const manual = await screen.findByRole("article", { name: "Order 31" });
    await user.click(within(manual).getByRole("button", { name: "Reprice" }));
    const form = within(manual).getByRole("group", { name: "Reprice order 31" });
    const amount = within(form).getByLabelText("Limit (per share)");
    expect(amount).toHaveValue("0.47");
    expect(within(form).getByLabelText("Credit or debit")).toHaveValue("credit");
    await user.clear(amount);
    expect(within(form).getByRole("button", { name: "Set limit" })).toBeDisabled();
    await user.type(amount, "0.44");
    await user.click(within(form).getByRole("button", { name: "Set limit" }));
    await waitFor(() => expect(opt.callsTo("optReprice")).toEqual([[31, { net_limit: "0.44" }]]));
    await waitFor(() => expect(within(manual).queryByRole("group", { name: "Reprice order 31" })).toBeNull());

    await user.click(within(manual).getByRole("button", { name: "Reprice" }));
    const again = within(manual).getByRole("group", { name: "Reprice order 31" });
    await user.selectOptions(within(again).getByLabelText("Credit or debit"), "debit");
    await user.click(within(again).getByRole("button", { name: "Set limit" }));
    await waitFor(() => expect(opt.callsTo("optReprice")[1]).toEqual([31, { net_limit: "-0.47" }]));
  });

  it("a strategy's order has no buttons", async () => {
    renderOptions(<OrdersTab />);
    const strategy = await screen.findByRole("article", { name: "Order 32" });
    expect(within(strategy).queryByRole("button")).toBeNull();
    expect(within(strategy).getByText(/Placed by a strategy/)).toBeInTheDocument();
  });

  it("a 409 from Cancel (the order is no longer working) is shown", async () => {
    const user = userEvent.setup();
    const opt = new FakeOptionsApiClient().fail("optCancel", new ApiError(409, "conflict", "Order 31 is not working any more."));
    renderOptions(<OrdersTab />, { opt });
    const manual = await screen.findByRole("article", { name: "Order 31" });
    await user.click(within(manual).getByRole("button", { name: "Cancel" }));
    expect(await within(manual).findByRole("alert")).toHaveTextContent("Order 31 is not working any more.");
  });

  it("the history shows each leg's fill quote, the fill net in words and a rejection's reason; no buttons", async () => {
    renderOptions(<OrdersTab />);
    const filled = await screen.findByRole("article", { name: "Order 28" });
    expect(fillWords(filledOrder.legs[0]!)).toBe("filled 0.55 against bid 0.52, ask 0.55, last 0.53 (07:40 MT)");
    const legs = within(filled).getAllByRole("listitem");
    expect(legs[0]).toHaveTextContent("Buy to open F 2026-11-20 C 13.00 · filled 0.55 against bid 0.52, ask 0.55, last 0.53 (07:40 MT)");
    expect(legs[1]).toHaveTextContent("Sell to open F 2026-11-20 C 14.00 · filled 0.25 against bid 0.25, ask 0.27, last 0.26 (07:40 MT)");
    expect(within(filled).getByText(/Filled at debit 0\.30 per share · fees \$3\.96/)).toBeInTheDocument();
    expect(within(filled).queryByRole("button")).toBeNull();
    const rejected = screen.getByRole("article", { name: "Order 27" });
    expect(within(rejected).getByText(new RegExp(REJECT_WORDS.insufficient_cash))).toHaveTextContent("Needs 1,200.00 free cash; 800.00 is free.");
    expect(within(rejected).queryByRole("button")).toBeNull();
  });
});
