// OPTSIM-T15: the Positions tab groups structures by underlying, and Close fills the ticket with the reversed legs.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { cspStructure, sharesStructure, spreadStructure } from "../../test/optionsFixtures";
import OptionsPage from "../Options";
import { PositionsTab, groupByUnderlying } from "./PositionsTab";
import { closingTicket } from "./shared";
import { renderOptions } from "./testRender";

describe("PositionsTab", () => {
  it("groups the structures by underlying, with legs, entry, reserve and source", async () => {
    expect(groupByUnderlying([sharesStructure, cspStructure, spreadStructure]).map(([u, items]) => [u, items.map((s) => s.id)])).toEqual([
      ["F", [21, 22]],
      ["SOFI", [23]],
    ]);
    renderOptions(<PositionsTab onClose={vi.fn()} />);
    const csp = await screen.findByRole("article", { name: "Position 21" });
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["F", "SOFI"]);
    expect(within(csp).getByText("1 × Cash-secured put")).toBeInTheDocument();
    expect(within(csp).getByText("toy_call")).toBeInTheDocument();
    expect(within(csp).getByText(/Entry credit 0\.45 · 45 days to expiry · reserve \$1,200\.00/)).toBeInTheDocument();
    const leg = within(csp).getAllByRole("row")[1]!;
    expect(Array.from(leg.children).map((c) => c.textContent)).toEqual(["F 2026-11-20 P 12.00", "-1", "0.45", "0.33", "$12.00", "0.21"]);
    const spread = screen.getByRole("article", { name: "Position 22" });
    expect(within(spread).getByText(/Entry debit 0\.30/)).toBeInTheDocument();
  });

  it.each([
    ["a short put", cspStructure, [{ instrument: "option", contract_id: 501, label: "F 2026-11-20 P 12.00", side: "buy", effect: "close", ratio: 1 }], 1],
    [
      "a two-contract spread",
      spreadStructure,
      [
        { instrument: "option", contract_id: 502, label: "F 2026-11-20 C 13.00", side: "sell", effect: "close", ratio: 1 },
        { instrument: "option", contract_id: 503, label: "F 2026-11-20 C 14.00", side: "buy", effect: "close", ratio: 1 },
      ],
      2,
    ],
    ["100 shares", sharesStructure, [{ instrument: "shares", contract_id: null, label: "SOFI shares", side: "sell", effect: "close", ratio: 100 }], 1],
  ])("closingTicket reverses %s", (_name, structure, legs, qty) => {
    expect(closingTicket(structure)).toEqual({ underlying: structure.underlying, structure_id: structure.id, legs, qty });
  });

  it("Close opens the Trade tab with the ticket prefilled, and the preview sends a closing order", async () => {
    const user = userEvent.setup();
    const r = renderOptions(<OptionsPage />, { route: "/options?tab=positions" });
    const spread = await screen.findByRole("article", { name: "Position 22" });
    await user.click(within(spread).getByRole("button", { name: "Close" }));
    expect(r.location().search).toBe("?tab=trade");
    const legs = within(screen.getByRole("list", { name: "Legs" })).getAllByRole("listitem");
    expect(legs).toHaveLength(2);
    expect(within(legs[0]!).getByText("F 2026-11-20 C 13.00")).toBeInTheDocument();
    expect(within(legs[0]!).getByRole("combobox", { name: "Side of leg 1" })).toHaveValue("sell");
    expect(within(legs[1]!).getByRole("combobox", { name: "Side of leg 2" })).toHaveValue("buy");
    expect(screen.getByLabelText("Quantity")).toHaveValue(2);

    await user.type(screen.getByLabelText("Limit (per share)"), "0.25");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(r.opt.callsTo("optPreview")).toEqual([
      [
        {
          underlying: "F",
          intent: "close",
          structure_id: 22,
          legs: [
            { instrument: "option", contract_id: 502, side: "sell", effect: "close", ratio: 1 },
            { instrument: "option", contract_id: 503, side: "buy", effect: "close", ratio: 1 },
          ],
          qty: 2,
          order_type: "limit",
          net_limit: "0.25",
          tif: "day",
          walk: false,
        },
      ],
    ]);
  });

  it("the closed list has no Close button", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<PositionsTab onClose={vi.fn()} />);
    await screen.findByRole("article", { name: "Position 21" });
    await user.click(screen.getByRole("checkbox", { name: "Show closed positions" }));
    const closed = await screen.findByRole("article", { name: "Position 19" });
    expect(within(closed).getByText("expired")).toBeInTheDocument();
    expect(within(closed).queryByRole("button", { name: "Close" })).toBeNull();
    expect(opt.callsTo("optPositions").at(-1)).toEqual([{ state: "closed", limit: 50 }]);
  });
});
