import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { EventOut } from "../../api/types";
import * as fx from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { ErrorList, EVENTS_PAGE_SIZE, EventLog } from "./EventLog";

/** A full page of events with ids counting down from `top` (the first keeps the fixture's message). */
function fullPage(base: EventOut, top: number): EventOut[] {
  return Array.from({ length: EVENTS_PAGE_SIZE }, (_, i) => ({ ...base, id: top - i, message: i === 0 ? base.message : `filler ${top - i}` }));
}

describe("EventLog (acceptance test 5)", () => {
  it("loads the newest events and shows them with MT times, level and source", async () => {
    const { api } = renderWithProviders(<EventLog />);
    expect(await screen.findByText("Proposal 12 created: BUY 30 AAA stop 21.56")).toBeInTheDocument();
    expect(api.callsTo("events")).toEqual([[{ limit: EVENTS_PAGE_SIZE }]]);
    expect(screen.getByText("2026-10-06 07:36 MT")).toBeInTheDocument();
    expect(screen.getByText("premarket")).toBeInTheDocument();
  });

  it("the level filter calls events({level: 'error'})", async () => {
    const { api } = renderWithProviders(<EventLog />);
    await screen.findByText("Proposal 12 created: BUY 30 AAA stop 21.56");
    await userEvent.selectOptions(screen.getByLabelText("Level"), "error");
    await waitFor(() => expect(api.callsTo("events")).toContainEqual([{ level: "error", limit: EVENTS_PAGE_SIZE }]));
  });

  it("'Load older' calls events({before: <oldest id>}) and appends the page", async () => {
    const api = new FakeApiClient();
    const first = fullPage(fx.events[0]!, 1000);
    api.respond("events", (q) =>
      q.before === undefined ? { items: first } : { items: [{ ...fx.events[2]!, id: 99, message: "An older event" }] },
    );
    renderWithProviders(<EventLog />, { api });
    await screen.findByText("Proposal 12 created: BUY 30 AAA stop 21.56");
    await userEvent.click(screen.getByRole("button", { name: "Load older" }));
    await waitFor(() => expect(api.callsTo("events")).toContainEqual([{ limit: EVENTS_PAGE_SIZE, before: 1000 - EVENTS_PAGE_SIZE + 1 }]));
    expect(await screen.findByText("An older event")).toBeInTheDocument();
    expect(screen.getByText("Proposal 12 created: BUY 30 AAA stop 21.56")).toBeInTheDocument();
    // The second page was short, so it was the last one.
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
    expect(screen.getByText("No older events.")).toBeInTheDocument();
  });

  it("a first page shorter than the limit offers no 'Load older'", async () => {
    const { api } = renderWithProviders(<EventLog />);
    await screen.findByText("Proposal 12 created: BUY 30 AAA stop 21.56");
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
    expect(screen.getByText("No older events.")).toBeInTheDocument();
    expect(api.callsTo("events")).toHaveLength(1);
  });

  it("keeps the level filter when loading older events, and stops when a page is empty", async () => {
    const api = new FakeApiClient();
    const first = fullPage(fx.errorEvents[0]!, 500);
    api.respond("events", (q) => (q.before === undefined ? { items: first } : { items: [] }));
    renderWithProviders(<EventLog />, { api });
    await userEvent.selectOptions(screen.getByLabelText("Level"), "error");
    await screen.findByText("Candle archive failed for 1 symbol");
    await userEvent.click(await screen.findByRole("button", { name: "Load older" }));
    await waitFor(() =>
      expect(api.callsTo("events")).toContainEqual([{ level: "error", limit: EVENTS_PAGE_SIZE, before: 500 - EVENTS_PAGE_SIZE + 1 }]),
    );
    expect(await screen.findByText("No older events.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
  });
});

describe("ErrorList", () => {
  it("shows each error newest first with its MT time and source", () => {
    renderWithProviders(<ErrorList errors={fx.errorEvents} />);
    expect(screen.getByText("Candle archive failed for 1 symbol")).toBeInTheDocument();
    expect(screen.getByText("2026-10-05 14:16 MT")).toBeInTheDocument();
    expect(screen.getByText("postclose")).toBeInTheDocument();
  });

  it("says so when there are none", () => {
    renderWithProviders(<ErrorList errors={[]} />);
    expect(screen.getByText("No errors.")).toBeInTheDocument();
  });
});
