import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fx from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { ErrorList, EventLog } from "./EventLog";

describe("EventLog (acceptance test 5)", () => {
  it("loads the newest events and shows them with MT times, level and source", async () => {
    const { api } = renderWithProviders(<EventLog />);
    expect(await screen.findByText("Proposal 12 created: BUY 30 AAA stop 21.56")).toBeInTheDocument();
    expect(api.callsTo("events")).toEqual([[{}]]);
    expect(screen.getByText("2026-10-06 07:36 MT")).toBeInTheDocument();
    expect(screen.getByText("premarket")).toBeInTheDocument();
  });

  it("the level filter calls events({level: 'error'})", async () => {
    const { api } = renderWithProviders(<EventLog />);
    await screen.findByText("Proposal 12 created: BUY 30 AAA stop 21.56");
    await userEvent.selectOptions(screen.getByLabelText("Level"), "error");
    await waitFor(() => expect(api.callsTo("events")).toContainEqual([{ level: "error" }]));
  });

  it("'Load older' calls events({before: <oldest id>}) and appends the page", async () => {
    const api = new FakeApiClient();
    api.respond("events", (q) =>
      q.before === undefined
        ? { items: fx.events }
        : { items: [{ ...fx.events[2]!, id: 99, message: "An older event" }] },
    );
    renderWithProviders(<EventLog />, { api });
    await screen.findByText("Proposal 12 created: BUY 30 AAA stop 21.56");
    await userEvent.click(screen.getByRole("button", { name: "Load older" }));
    await waitFor(() => expect(api.callsTo("events")).toContainEqual([{ before: 101 }]));
    expect(await screen.findByText("An older event")).toBeInTheDocument();
    expect(screen.getByText("Proposal 12 created: BUY 30 AAA stop 21.56")).toBeInTheDocument();
  });

  it("keeps the level filter when loading older events, and stops when a page is empty", async () => {
    const api = new FakeApiClient();
    api.respond("events", (q) => (q.before === undefined ? { items: fx.errorEvents } : { items: [] }));
    renderWithProviders(<EventLog />, { api });
    await userEvent.selectOptions(screen.getByLabelText("Level"), "error");
    await screen.findByText("Candle archive failed for 1 symbol");
    await userEvent.click(screen.getByRole("button", { name: "Load older" }));
    await waitFor(() => expect(api.callsTo("events")).toContainEqual([{ level: "error", before: 90 }]));
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
