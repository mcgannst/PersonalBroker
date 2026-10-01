// P4-T14 acceptance test 5: the Journal page and its editor (the daily-summary link `/journal?date=`).
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import JournalPage, { answerText } from "../Journal";
import { NOTES_MAX } from "./JournalEditor";

function editor(): HTMLElement {
  return screen.getByRole("region", { name: /^Journal for / });
}

describe("Journal page", () => {
  it("AUTOJOURNAL: an auto-mode day reads Yes (auto mode)", () => {
    expect(answerText({ rules_followed: true, answered_via: "auto" })).toBe("Yes (auto mode)");
  });

  it("lists the last session days newest first with the answer and where it was given", async () => {
    const r = renderWithProviders(<JournalPage />, { route: "/journal" });
    const rows = await screen.findAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("2026-10-05");
    expect(rows[0]).toHaveTextContent("Yes (Telegram)");
    expect(rows[0]).toHaveTextContent("$19.73");
    expect(rows[0]).toHaveTextContent("1 trade");
    expect(rows[0]).toHaveTextContent("Clean entry, flattened on time.");
    expect(rows[1]).toHaveTextContent("No (web)");
    expect(rows[1]).toHaveTextContent("-$9.90");
    expect(rows[2]).toHaveTextContent("Not answered");
    expect(rows[2]).toHaveTextContent("0 trades");
    expect(r.api.callsTo("journal")).toEqual([[{}]]);
    expect(screen.queryByRole("region", { name: /^Journal for / })).not.toBeInTheDocument();
  });

  it("?date=2026-10-06 opens that editor; Yes plus a note then Save calls putJournal with both", async () => {
    const r = renderWithProviders(<JournalPage />, { route: "/journal?date=2026-10-06" });
    const ed = await screen.findByRole("region", { name: "Journal for 2026-10-06" });
    await userEvent.click(within(ed).getByRole("button", { name: "Yes" }));
    expect(within(ed).getByRole("button", { name: "Yes" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.type(within(ed).getByLabelText("Notes"), "late entry");
    await userEvent.click(within(ed).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(r.api.callsTo("putJournal")).toEqual([["2026-10-06", { rules_followed: true, notes: "late entry" }]]));
    expect(await within(ed).findByText("Saved")).toBeInTheDocument();
  });

  it("the counter blocks more than 5,000 characters", async () => {
    renderWithProviders(<JournalPage />, { route: "/journal?date=2026-10-06" });
    const ed = await screen.findByRole("region", { name: "Journal for 2026-10-06" });
    const notes = within(ed).getByLabelText("Notes") as HTMLTextAreaElement;
    expect(notes.maxLength).toBe(NOTES_MAX);
    fireEvent.change(notes, { target: { value: "x".repeat(NOTES_MAX + 1) } });
    expect(notes.value).toHaveLength(NOTES_MAX);
    expect(within(ed).getByText(`${NOTES_MAX} / ${NOTES_MAX}`)).toBeInTheDocument();
    expect(NOTES_MAX).toBe(5000);
  });

  it("tapping a day opens its editor with the saved answer; only changed fields are sent", async () => {
    const r = renderWithProviders(<JournalPage />, { route: "/journal" });
    await userEvent.click(await screen.findByRole("button", { name: /2026-10-05/ }));
    expect(r.location().search).toBe("?date=2026-10-05");
    const ed = editor();
    expect(within(ed).getByRole("button", { name: "Yes" })).toHaveAttribute("aria-pressed", "true");
    const notes = within(ed).getByLabelText("Notes");
    expect(notes).toHaveValue("Clean entry, flattened on time.");
    expect(within(ed).getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.type(notes, " Good day.");
    await userEvent.click(within(ed).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(r.api.callsTo("putJournal")).toEqual([["2026-10-05", { notes: "Clean entry, flattened on time. Good day." }]]),
    );
  });

  it("Clear answer sends rules_followed null", async () => {
    const r = renderWithProviders(<JournalPage />, { route: "/journal?date=2026-10-02" });
    const ed = await screen.findByRole("region", { name: "Journal for 2026-10-02" });
    await waitFor(() => expect(within(ed).getByRole("button", { name: "No" })).toHaveAttribute("aria-pressed", "true"));
    await userEvent.click(within(ed).getByRole("button", { name: "Clear answer" }));
    await userEvent.click(within(ed).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(r.api.callsTo("putJournal")).toEqual([["2026-10-02", { rules_followed: null }]]));
  });

  it("shows the server's 422 message and keeps the edit", async () => {
    const api = new FakeApiClient().fail("putJournal", new ApiError(422, "validation", "2026-10-10 is not a past session"));
    renderWithProviders(<JournalPage />, { route: "/journal?date=2026-10-10", api });
    const ed = await screen.findByRole("region", { name: "Journal for 2026-10-10" });
    await userEvent.click(within(ed).getByRole("button", { name: "No" }));
    await userEvent.click(within(ed).getByRole("button", { name: "Save" }));
    expect(await within(ed).findByRole("alert")).toHaveTextContent("2026-10-10 is not a past session");
    expect(within(ed).getByRole("button", { name: "No" })).toHaveAttribute("aria-pressed", "true");
  });

  it("Close returns to the list; a malformed date shows a message", async () => {
    const r = renderWithProviders(<JournalPage />, { route: "/journal?date=2026-10-06" });
    await userEvent.click(await screen.findByRole("button", { name: "Close" }));
    expect(r.location().search).toBe("");
    r.unmount();
    renderWithProviders(<JournalPage />, { route: "/journal?date=tomorrow" });
    expect(await screen.findByText(/not a date/i)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /^Journal for / })).not.toBeInTheDocument();
  });
});
