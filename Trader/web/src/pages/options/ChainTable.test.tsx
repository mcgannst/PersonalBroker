// OPTSIM-T15: the chain: a bid click adds a sell leg, an ask click a buy leg; stale quotes are marked.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { optChainQuotes } from "../../test/optionsFixtures";
import OptionsPage from "../Options";
import { ChainTable } from "./ChainTable";
import { EMPTY_TICKET, type TicketLeg } from "./shared";
import { renderOptions } from "./testRender";
import { withLeg } from "./TradeTab";

describe("ChainTable", () => {
  it("shows calls on the left and puts on the right of each strike", () => {
    render(<ChainTable quotes={optChainQuotes} onLeg={vi.fn()} />);
    const heads = within(screen.getAllByRole("row")[1]!).getAllByRole("columnheader").map((h) => h.textContent);
    expect(heads).toEqual(["Volume", "Open int.", "IV", "Delta", "Last", "Ask", "Bid", "Strike", "Bid", "Ask", "Last", "Delta", "IV", "Open int.", "Volume"]);
    const row = screen.getByRole("row", { name: /12\.00/ });
    expect(Array.from(row.children).map((c) => c.textContent?.trim())).toEqual([
      "150", "2,400", "34.00%", "0.72", "1.05", "1.10", "1.05", "12.00", "0.45", "0.48", "0.45", "-0.21", "34.00%", "2,400", "150",
    ]);
  });

  it("a bid click adds a sell leg and an ask click a buy leg", async () => {
    const user = userEvent.setup();
    const onLeg = vi.fn<(leg: TicketLeg) => void>();
    render(<ChainTable quotes={optChainQuotes} onLeg={onLeg} />);
    await user.click(screen.getByRole("button", { name: "Sell F 2026-11-20 P 12.00 at the bid 0.45" }));
    await user.click(screen.getByRole("button", { name: "Buy F 2026-11-20 C 13.00 at the ask 0.55" }));
    expect(onLeg.mock.calls.map(([leg]) => leg)).toEqual([
      { instrument: "option", contract_id: 501, label: "F 2026-11-20 P 12.00", side: "sell", effect: "open", ratio: 1 },
      { instrument: "option", contract_id: 502, label: "F 2026-11-20 C 13.00", side: "buy", effect: "open", ratio: 1 },
    ]);
  });

  it("marks only the stale quote, and shows a side that is not listed without buttons", () => {
    render(<ChainTable quotes={optChainQuotes} onLeg={vi.fn()} />);
    const stale = screen.getAllByText("stale");
    expect(stale).toHaveLength(1);
    expect(stale[0]!.closest("tr")).toBe(screen.getByRole("row", { name: /13\.00/ }));
    expect(stale[0]!.closest("td")).toHaveClass("opt-stale");
    const row14 = screen.getByRole("row", { name: /14\.00/ });
    expect(within(row14).getByText("not listed")).toBeInTheDocument();
    expect(within(row14).getAllByRole("button")).toHaveLength(2);
  });

  it("withLeg: a second click on a contract replaces its leg; another underlying starts a new ticket", () => {
    const sell: TicketLeg = { instrument: "option", contract_id: 501, label: "P", side: "sell", effect: "open", ratio: 1 };
    const one = withLeg(EMPTY_TICKET, "F", sell);
    expect(one).toEqual({ underlying: "F", structure_id: null, legs: [sell], qty: 1 });
    expect(withLeg(one, "F", { ...sell, side: "buy" }).legs).toEqual([{ ...sell, side: "buy" }]);
    expect(withLeg(one, "F", { ...sell, contract_id: 502 }).legs).toHaveLength(2);
    expect(withLeg(one, "SOFI", { ...sell, contract_id: 900 }).legs).toEqual([{ ...sell, contract_id: 900 }]);
  });

  it("on the Trade tab: load an underlying, pick the expiry, click a bid, and the leg is on the ticket", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<OptionsPage />, { route: "/options?tab=trade" });
    await user.type(screen.getByLabelText("Underlying"), "f");
    await user.click(screen.getByRole("button", { name: "Load chain" }));
    expect(await screen.findByRole("combobox", { name: "Expiry" })).toHaveValue("2026-10-30");
    await user.selectOptions(screen.getByRole("combobox", { name: "Expiry" }), "2026-11-20");
    await user.click(await screen.findByRole("button", { name: "Sell F 2026-11-20 P 12.00 at the bid 0.45" }));
    expect(opt.callsTo("optChain")[0]).toEqual([{ underlying: "F" }]);
    expect(opt.callsTo("optChainQuotes").at(-1)).toEqual([{ underlying: "F", expiry: "2026-11-20" }]);
    const legs = within(screen.getByRole("list", { name: "Legs" })).getAllByRole("listitem");
    expect(legs).toHaveLength(1);
    expect(within(legs[0]!).getByText("F 2026-11-20 P 12.00")).toBeInTheDocument();
    expect(within(legs[0]!).getByRole("combobox", { name: "Side of leg 1" })).toHaveValue("sell");
    // A ticket that starts with a sale is a credit until told otherwise.
    expect(screen.getByLabelText("Credit or debit")).toHaveValue("credit");
  });
});
