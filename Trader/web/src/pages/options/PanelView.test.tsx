// OPTSIM-T15: the generic strategy panel: any `OptPanelOut` renders, and each kind of action calls
// `optPanelAction` with the row's id.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeOptionsApiClient } from "../../test/optionsFakeApi";
import { richPanel, toyPanel } from "../../test/optionsFixtures";
import { PanelView } from "./PanelView";
import { renderOptions } from "./testRender";

function cellsOf(row: HTMLElement): string[] {
  return Array.from(row.children).map((c) => c.textContent ?? "");
}

async function openRow(user: ReturnType<typeof userEvent.setup>, id: string) {
  await user.click(await screen.findByRole("button", { name: `Show ${id}` }));
  return screen.getByRole("group", { name: `${id} details` });
}

describe("PanelView", () => {
  it("renders the toy plug-in's panel: summary, one table, and its text action on the row", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<PanelView strategyKey="toy_call" />);
    const table = within(await screen.findByRole("region", { name: "Calls bought" })).getByRole("table");
    expect(screen.getByText("Open calls").nextSibling).toHaveTextContent("1");
    const rows = within(table).getAllByRole("row");
    expect(cellsOf(rows[0]!)).toEqual(["Contract", "Quantity", "Cost", ""]);
    expect(cellsOf(rows[1]!).slice(0, 3)).toEqual(["F 2026-11-20 C 13.00", "1", "$55.00"]);
    // The action is offered on its row, so not once more for the whole panel.
    expect(screen.queryByRole("group", { name: "Actions" })).toBeNull();

    const detail = await openRow(user, "41");
    const save = within(detail).getByRole("button", { name: "Leave a note: save" });
    expect(save).toBeDisabled();
    await user.type(within(detail).getByLabelText("Leave a note"), "watch earnings");
    await user.click(save);
    await waitFor(() => expect(opt.callsTo("optPanelAction")).toEqual([["toy_call", { action: "note", row_id: "41", value: "watch earnings" }]]));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
    expect(opt.callsTo("optPanel")).toEqual(expect.arrayContaining([["toy_call"]]));
  });

  it("renders a different panel: toned summary, every column kind, an empty table and row details", async () => {
    const user = userEvent.setup();
    renderOptions(<PanelView strategyKey={richPanel.strategy_key} />);
    const tickers = within(await screen.findByRole("region", { name: "Tickers" })).getByRole("table");
    expect(screen.getByText("Paused").nextSibling).toHaveClass("tone-warn");
    const rows = within(tickers).getAllByRole("row");
    expect(cellsOf(rows[1]!).slice(0, 6)).toEqual(["SOFI", "approved", "No", "2026-11-03", "$9.12", "1"]);
    expect(cellsOf(rows[2]!).slice(0, 6)).toEqual(["F", "candidate", "Yes", "–", "–", "0"]);
    expect(within(rows[1]!).getByText("approved")).toHaveClass("badge");
    expect(within(screen.getByRole("region", { name: "Candidates" })).getByText("Nothing passed the screen")).toBeInTheDocument();

    const detail = await openRow(user, "SOFI");
    expect(within(detail).getByText("Test 2").nextSibling).toHaveTextContent("fail: earnings inside the window");
    expect(within(detail).getByText("Test 2").nextSibling).toHaveClass("tone-bad");
    await user.click(screen.getByRole("button", { name: "Hide SOFI" }));
    expect(screen.queryByRole("group", { name: "SOFI details" })).toBeNull();
  });

  it("toggle and choice actions send the row id and the new value", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<PanelView strategyKey={richPanel.strategy_key} />);
    const sofi = await openRow(user, "SOFI");
    const flagged = within(sofi).getByRole("checkbox", { name: "Flagged" });
    expect(flagged).not.toBeChecked();
    await user.click(flagged);
    await user.selectOptions(within(sofi).getByLabelText("Security type"), "etf");
    await user.click(within(sofi).getByRole("button", { name: "Security type: set" }));
    await waitFor(() =>
      expect(opt.callsTo("optPanelAction")).toEqual([
        [richPanel.strategy_key, { action: "flagged", row_id: "SOFI", value: true }],
        [richPanel.strategy_key, { action: "kind", row_id: "SOFI", value: "etf" }],
      ]),
    );
    // A toggle starts from the row's own cell: F is already flagged, and offers only that action.
    const f = await openRow(user, "F");
    expect(within(f).getByRole("checkbox", { name: "Flagged" })).toBeChecked();
    expect(within(f).queryByRole("button")).toBeNull();
  });

  it("an action that asks for confirmation is sent only after the dialog is confirmed", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<PanelView strategyKey={richPanel.strategy_key} />);
    const sofi = await openRow(user, "SOFI");
    await user.click(within(sofi).getByRole("button", { name: "Drop" }));
    const dialog = within(sofi).getByRole("alertdialog", { name: "Drop" });
    expect(dialog).toHaveTextContent("Drop (SOFI)?");
    expect(opt.callsTo("optPanelAction")).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(within(sofi).queryByRole("alertdialog")).toBeNull();
    expect(opt.callsTo("optPanelAction")).toEqual([]);

    await user.click(within(sofi).getByRole("button", { name: "Drop" }));
    await user.click(within(within(sofi).getByRole("alertdialog")).getByRole("button", { name: "Drop" }));
    await waitFor(() => expect(opt.callsTo("optPanelAction")).toEqual([[richPanel.strategy_key, { action: "drop", row_id: "SOFI", value: null }]]));
  });

  it("actions no row lists are offered for the whole panel, without a row id", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<PanelView strategyKey={richPanel.strategy_key} />);
    const actions = await screen.findByRole("group", { name: "Actions" });
    expect(within(actions).queryByRole("checkbox")).toBeNull();
    await user.click(within(actions).getByRole("button", { name: "Screen again" }));
    await user.type(within(actions).getByLabelText("Add a ticker"), " AAPL ");
    await user.click(within(actions).getByRole("button", { name: "Add a ticker: save" }));
    await waitFor(() =>
      expect(opt.callsTo("optPanelAction")).toEqual([
        [richPanel.strategy_key, { action: "rescreen", row_id: null, value: null }],
        [richPanel.strategy_key, { action: "add", row_id: null, value: "AAPL" }],
      ]),
    );
  });

  it("shows the plug-in's refusal and a server error", async () => {
    const user = userEvent.setup();
    const opt = new FakeOptionsApiClient({ optPanel: toyPanel, optPanelAction: { ok: false, message: "A note needs some text" } });
    renderOptions(<PanelView strategyKey="toy_call" />, { opt });
    const detail = await openRow(user, "41");
    await user.type(within(detail).getByLabelText("Leave a note"), "x");
    await user.click(within(detail).getByRole("button", { name: "Leave a note: save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A note needs some text");
    // A refused note stays in the box.
    expect(within(detail).getByLabelText("Leave a note")).toHaveValue("x");

    opt.fail("optPanelAction", new ApiError(503, "unavailable", "The options service is not running."));
    await user.click(within(detail).getByRole("button", { name: "Leave a note: save" }));
    expect(await screen.findByText("The options service is not running.")).toBeInTheDocument();
  });
});
