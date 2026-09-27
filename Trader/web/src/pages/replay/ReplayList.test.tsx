// P5-T8 acceptance tests 1 and 3 (busy): the replay list and the "New replay" button.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { fmtDateTime } from "../../lib/format";
import { replayOptions, replaySummaries } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import ReplayPage from "../Replay";

describe("the replay list (acceptance test 1)", () => {
  it("renders the fixtures in order with status badges, the biased badge and MT times", async () => {
    const r = renderWithProviders(<ReplayPage />, { route: "/replay" });
    const table = await screen.findByRole("table", { name: "Replays" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(replaySummaries.length);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0]?.textContent)).toEqual(replaySummaries.map((s) => s.label));
    expect(r.api.callsTo("replays")).toEqual([[{ limit: 50 }]]);

    const [running, completed, failed, cancelled] = rows as [HTMLElement, HTMLElement, HTMLElement, HTMLElement];
    expect(within(running).getByText("running")).toHaveClass("badge", "tone-info");
    expect(within(completed).getByText("completed")).toHaveClass("badge", "tone-ok");
    expect(within(failed).getByText("failed")).toHaveClass("badge", "tone-bad");
    expect(within(cancelled).getByText("cancelled")).toHaveClass("badge", "tone-muted");
    expect(within(completed).getByText("biased universe")).toHaveClass("badge", "tone-warn");
    expect(within(running).queryByText("biased universe")).toBeNull();

    // Range, trades, expectancy, P&L and the created time in MT.
    expect(within(completed).getByText("2026-11-23 → 2026-11-27")).toBeInTheDocument();
    expect(within(completed).getByText("+0.13R")).toBeInTheDocument();
    expect(within(completed).getByText("$2.00")).toBeInTheDocument();
    expect(within(cancelled).getByText("-$7.20")).toBeInTheDocument();
    const created = fmtDateTime(replaySummaries[1]!.created_at);
    expect(created).toMatch(/ MT$/);
    expect(within(completed).getByText(created)).toBeInTheDocument();
  });

  it("the running row shows its progress from the detail", async () => {
    renderWithProviders(<ReplayPage />, { route: "/replay" });
    const table = await screen.findByRole("table", { name: "Replays" });
    const running = within(table).getAllByRole("row")[1]!;
    expect(await within(running).findByText("3/10")).toBeInTheDocument();
  });

  it("tapping a row opens ?id=", async () => {
    const r = renderWithProviders(<ReplayPage />, { route: "/replay" });
    const table = await screen.findByRole("table", { name: "Replays" });
    const completed = within(table).getAllByRole("row")[2]!;
    await userEvent.click(within(completed).getAllByRole("cell")[1]!);
    await waitFor(() => expect(r.location().search).toBe("?id=11"));
    expect(await screen.findByRole("heading", { level: 2, name: /Thanksgiving week \(biased universe\)/ })).toBeInTheDocument();
  });

  it("an empty list says so", async () => {
    renderWithProviders(<ReplayPage />, { route: "/replay", api: new FakeApiClient({ replays: { items: [] } }) });
    expect(await screen.findByText("No replays yet.")).toBeInTheDocument();
  });
});

describe("New replay (acceptance test 3, busy)", () => {
  it("busy: true disables New replay with the reason", async () => {
    const api = new FakeApiClient({ replayOptions: { ...replayOptions, busy: true } });
    renderWithProviders(<ReplayPage />, { route: "/replay", api });
    const button = await screen.findByRole("button", { name: "New replay" });
    await waitFor(() => expect(button).toBeDisabled());
    expect(screen.getByText("A replay is already running. Start a new one when it has finished.")).toBeInTheDocument();
  });

  it("not busy: New replay opens the form", async () => {
    renderWithProviders(<ReplayPage />, { route: "/replay" });
    const button = await screen.findByRole("button", { name: "New replay" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    expect(await screen.findByRole("form", { name: "New replay" })).toBeInTheDocument();
  });
});
