import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fx from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { JobRuns } from "./JobRuns";

describe("JobRuns (acceptance test 3)", () => {
  it("shows the latest runs with MT start times, durations and the error's first line", () => {
    renderWithProviders(<JobRuns lastRuns={fx.jobRuns} />);
    const table = screen.getByRole("table");
    // nightly started 00:30Z on 2026-10-06 = 18:30 the day before at UTC-6, 252 s long.
    expect(within(table).getByText("2026-10-05 18:30 MT")).toBeInTheDocument();
    expect(within(table).getByText("4m 12s")).toBeInTheDocument();
    expect(within(table).getByText("41s")).toBeInTheDocument();
    expect(within(table).getByText("Candle archive failed for 1 symbol")).toBeInTheDocument();
    expect(table.textContent).not.toContain("Traceback omitted");
  });

  it("filtering by nightly loads that job's history", async () => {
    const api = new FakeApiClient();
    api.set("jobs", { items: [{ ...fx.jobRuns[0]!, id: 250, started_at: "2026-10-05T00:30:00Z" }, fx.jobRuns[0]!] });
    renderWithProviders(<JobRuns lastRuns={fx.jobRuns} />, { api });
    expect(api.callsTo("jobs")).toEqual([]);
    await userEvent.selectOptions(screen.getByLabelText("Job"), "nightly");
    await waitFor(() => expect(api.callsTo("jobs")).toEqual([[{ job: "nightly" }]]));
    expect(await screen.findByText("2026-10-04 18:30 MT")).toBeInTheDocument();
    expect(screen.queryByText("41s")).toBeNull();
  });

  it("offers every job in the latest runs as a filter", () => {
    renderWithProviders(<JobRuns lastRuns={fx.jobRuns} />);
    const options = within(screen.getByLabelText("Job")).getAllByRole("option").map((o) => o.getAttribute("value"));
    expect(options).toEqual(expect.arrayContaining(["", "nightly", "premarket", "preopen", "postclose"]));
  });
});
