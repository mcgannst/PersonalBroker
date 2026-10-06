// OPTSIM-T15: the ticket: debit and credit in words, the preview, a rejected or out-of-date preview blocks
// Submit, and Submit sends the order once.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { OPT_REJECT_REASONS, type OptOrderIn } from "../../api/types";
import { FakeOptionsApiClient } from "../../test/optionsFakeApi";
import { optPreviewRejected, rejectedOrder } from "../../test/optionsFixtures";
import { REJECT_WORDS, netWords, signedNet, type TicketDraft, type TicketLeg } from "./shared";
import { renderOptions } from "./testRender";
import { Ticket, buildOrder, termsWords } from "./Ticket";

const SELL_PUT: TicketLeg = { instrument: "option", contract_id: 501, label: "F 2026-11-20 P 12.00", side: "sell", effect: "open", ratio: 1 };
const BUY_CALL: TicketLeg = { instrument: "option", contract_id: 502, label: "F 2026-11-20 C 13.00", side: "buy", effect: "open", ratio: 1 };
const DRAFT: TicketDraft = { underlying: "F", structure_id: null, legs: [SELL_PUT], qty: 1 };

const ORDER: OptOrderIn = {
  underlying: "F",
  intent: "open",
  structure_id: null,
  legs: [{ instrument: "option", contract_id: 501, side: "sell", effect: "open", ratio: 1 }],
  qty: 1,
  order_type: "limit",
  net_limit: "0.45",
  tif: "day",
  walk: false,
};

function Harness({ initial = DRAFT }: { initial?: TicketDraft }) {
  const [draft, setDraft] = useState(initial);
  return <Ticket draft={draft} onChange={setDraft} />;
}

function setup(opt = new FakeOptionsApiClient(), initial: TicketDraft = DRAFT) {
  const user = userEvent.setup();
  const r = renderOptions(<Harness initial={initial} />, { opt });
  return { user, opt: r.opt };
}

const submitButton = () => screen.getByRole("button", { name: "Submit" });
const previewButton = () => screen.getByRole("button", { name: "Preview" });

describe("net prices in words (never signed)", () => {
  it.each([
    ["0.4500", "credit 0.45"],
    ["-1.2000", "debit 1.20"],
    ["-0.3050", "debit 0.305"],
    ["0", "even"],
    ["-0.0000", "even"],
    [null, "n/a"],
  ])("netWords(%j) is %j", (net, words) => {
    expect(netWords(net)).toBe(words);
    expect(words).not.toMatch(/-/);
  });

  it.each([
    ["0.45", "credit", "0.45"],
    ["1.20", "debit", "-1.20"],
    [" 0.5 ", "debit", "-0.5"],
    ["0", "debit", "0"],
    ["-1", "debit", null],
    ["abc", "credit", null],
    ["0.12345", "credit", null],
    ["", "credit", null],
  ] as const)("signedNet(%j, %s) is %j", (amount, direction, net) => {
    expect(signedNet(amount, direction)).toBe(net);
  });

  it.each([
    [{ ...ORDER }, "Limit: credit 0.45 per share."],
    [{ ...ORDER, net_limit: "-1.20" }, "Limit: debit 1.20 per share."],
    [{ ...ORDER, net_limit: "-1.20", walk: true }, "Limit: debit 1.20 per share, then walking toward the market."],
    [{ ...ORDER, net_limit: null, walk: true }, "Limit order starting at the midpoint, then walking toward the market."],
    [{ ...ORDER, order_type: "market", net_limit: null } as OptOrderIn, "Market order: fills at the bid or the ask of each leg."],
  ])("termsWords", (order, words) => {
    expect(termsWords(order)).toBe(words);
  });
});

describe("buildOrder", () => {
  const terms = { orderType: "limit", amount: "0.45", direction: "credit", tif: "day", walk: false } as const;

  it("builds the order body; open and close legs together are a roll that keeps the structure", () => {
    expect(buildOrder(DRAFT, terms)).toEqual({ order: ORDER });
    const roll: TicketDraft = { underlying: "F", structure_id: 21, legs: [{ ...SELL_PUT, side: "buy", effect: "close" }, { ...SELL_PUT, contract_id: 520 }], qty: 1 };
    const built = buildOrder(roll, { ...terms, tif: "gtc" });
    expect("order" in built && [built.order.intent, built.order.structure_id, built.order.tif]).toEqual(["roll", 21, "gtc"]);
    // An order that closes nothing never names a structure.
    const opening = buildOrder({ ...DRAFT, structure_id: 21 }, terms);
    expect("order" in opening && opening.order.structure_id).toBeNull();
  });

  it.each([
    ["no legs", { ...DRAFT, legs: [] }, terms, /Add a leg/],
    ["five legs", { ...DRAFT, legs: [1, 2, 3, 4, 5].map((n) => ({ ...SELL_PUT, contract_id: n })) }, terms, /at most 4 legs/],
    ["quantity 0", { ...DRAFT, qty: 0 }, terms, /Quantity/],
    ["quantity 101", { ...DRAFT, qty: 101 }, terms, /Quantity/],
    ["no limit", DRAFT, { ...terms, amount: "" }, /Enter the limit price/],
  ])("%s is a problem, not an order", (_name, draft, t, text) => {
    const built = buildOrder(draft, t);
    expect("problem" in built && built.problem).toMatch(text);
  });

  it("a market order has no limit and never walks; a walking limit may start without a price", () => {
    const market = buildOrder(DRAFT, { ...terms, orderType: "market", walk: true });
    expect("order" in market && [market.order.net_limit, market.order.walk]).toEqual([null, false]);
    const walking = buildOrder(DRAFT, { ...terms, amount: "", walk: true });
    expect("order" in walking && [walking.order.net_limit, walking.order.walk]).toEqual([null, true]);
  });
});

describe("Ticket", () => {
  it("says the limit as a credit or a debit in words, following the side chosen", async () => {
    const { user } = setup();
    const terms = screen.getByTestId("opt-ticket-terms");
    expect(terms).toHaveTextContent("Enter the limit price.");
    await user.type(screen.getByLabelText("Limit (per share)"), "0.45");
    expect(terms).toHaveTextContent("Limit: credit 0.45 per share.");
    await user.selectOptions(screen.getByLabelText("Credit or debit"), "debit");
    expect(terms).toHaveTextContent("Limit: debit 0.45 per share.");
    await user.selectOptions(screen.getByLabelText("Order type"), "market");
    expect(terms).toHaveTextContent("Market order");
    expect(screen.queryByLabelText("Limit (per share)")).toBeNull();
  });

  it("a ticket that starts with a buy defaults to a debit", () => {
    setup(undefined, { ...DRAFT, legs: [BUY_CALL] });
    expect(screen.getByLabelText("Credit or debit")).toHaveValue("debit");
  });

  it("shows the preview and enables Submit only for an accepted preview; Submit sends the order once", async () => {
    const { user, opt } = setup();
    expect(submitButton()).toBeDisabled();
    expect(previewButton()).toBeDisabled();
    await user.type(screen.getByLabelText("Limit (per share)"), "0.45");
    expect(submitButton()).toBeDisabled();
    await user.click(previewButton());

    const preview = await screen.findByLabelText("Preview");
    expect(opt.callsTo("optPreview")).toEqual([[ORDER]]);
    expect(within(preview).getByText("Accepted")).toBeInTheDocument();
    expect(within(preview).getByText("Cash-secured put")).toBeInTheDocument();
    for (const [label, value] of [
      ["Net at the market", "credit 0.45"],
      ["Max loss", "$1,155.99"],
      ["Max profit", "$44.01"],
      ["Breakeven", "11.55"],
      ["Reserve", "$1,200.00"],
      ["Fees", "$0.99"],
      ["Cash after", "$5,167.46"],
    ]) {
      expect(within(preview).getByText(label!).nextSibling, label).toHaveTextContent(value!);
    }

    expect(submitButton()).toBeEnabled();
    await user.dblClick(submitButton());
    expect(await screen.findByText(/Order 31 is working/)).toBeInTheDocument();
    expect(opt.callsTo("optSubmit")).toEqual([[ORDER]]);
    // The ticket is empty again.
    expect(screen.queryByRole("list", { name: "Legs" })).toBeNull();
    expect(submitButton()).toBeDisabled();
  });

  it.each(OPT_REJECT_REASONS.map((r) => [r]))("a preview rejected for %s disables Submit and says why in plain words", async (reason) => {
    const opt = new FakeOptionsApiClient({ optPreview: optPreviewRejected(reason) });
    const { user } = setup(opt);
    await user.type(screen.getByLabelText("Limit (per share)"), "0.45");
    await user.click(previewButton());
    const preview = await screen.findByLabelText("Preview");
    expect(within(preview).getByText("Rejected")).toBeInTheDocument();
    const words = REJECT_WORDS[reason];
    expect(words.length).toBeGreaterThan(20);
    expect(words).not.toContain("_");
    expect(within(preview).getByRole("alert")).toHaveTextContent(words);
    expect(within(preview).getByText("The engine's own explanation.")).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
    expect(opt.callsTo("optSubmit")).toEqual([]);
  });

  it.each([
    ["the side of a leg", async (user: ReturnType<typeof userEvent.setup>) => user.selectOptions(screen.getByRole("combobox", { name: "Side of leg 1" }), "buy")],
    ["whether a leg opens or closes", async (user: ReturnType<typeof userEvent.setup>) => user.selectOptions(screen.getByRole("combobox", { name: "Leg 1 opens or closes" }), "close")],
    ["the quantity", async (user: ReturnType<typeof userEvent.setup>) => user.type(screen.getByLabelText("Quantity"), "0")],
    ["the limit", async (user: ReturnType<typeof userEvent.setup>) => user.type(screen.getByLabelText("Limit (per share)"), "1")],
    ["how long it is good for", async (user: ReturnType<typeof userEvent.setup>) => user.selectOptions(screen.getByLabelText("Good for"), "gtc")],
  ])("changing %s makes the preview out of date: Submit is disabled until it is previewed again", async (_name, change) => {
    const { user, opt } = setup();
    await user.type(screen.getByLabelText("Limit (per share)"), "0.45");
    await user.click(previewButton());
    await screen.findByLabelText("Preview");
    expect(submitButton()).toBeEnabled();

    await change(user);
    expect(submitButton()).toBeDisabled();
    expect(screen.queryByLabelText("Preview")).toBeNull();
    expect(screen.getByText(/The order changed since the preview/)).toBeInTheDocument();

    await user.click(previewButton());
    await waitFor(() => expect(submitButton()).toBeEnabled());
    expect(opt.callsTo("optPreview")).toHaveLength(2);
  });

  it("removing a leg also invalidates the preview", async () => {
    const { user } = setup(undefined, { ...DRAFT, legs: [SELL_PUT, BUY_CALL] });
    await user.type(screen.getByLabelText("Limit (per share)"), "0.10");
    await user.click(previewButton());
    await screen.findByLabelText("Preview");
    await user.click(screen.getByRole("button", { name: "Remove leg 2" }));
    expect(submitButton()).toBeDisabled();
    expect(within(screen.getByRole("list", { name: "Legs" })).getAllByRole("listitem")).toHaveLength(1);
  });

  it("an order the server rejects on submit keeps the ticket and shows the reason", async () => {
    const opt = new FakeOptionsApiClient({ optSubmit: rejectedOrder });
    const { user } = setup(opt);
    await user.type(screen.getByLabelText("Limit (per share)"), "0.45");
    await user.click(previewButton());
    await screen.findByLabelText("Preview");
    await user.click(submitButton());
    expect(await screen.findByText(/Order 27 was rejected/)).toHaveTextContent(REJECT_WORDS.insufficient_cash);
    expect(screen.getByRole("list", { name: "Legs" })).toBeInTheDocument();
  });

  it("a 422 from the preview shows the server's field messages and no preview", async () => {
    const opt = new FakeOptionsApiClient().fail("optPreview", new ApiError(422, "validation", "Invalid input", [{ loc: ["body", "qty"], msg: "At most 10 contracts per order" }]));
    const { user } = setup(opt);
    await user.type(screen.getByLabelText("Limit (per share)"), "0.45");
    await user.click(previewButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("At most 10 contracts per order");
    expect(screen.queryByLabelText("Preview")).toBeNull();
    expect(submitButton()).toBeDisabled();
  });
});
