import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import * as fx from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { WatchlistUpload } from "./WatchlistUpload";

function csv(): File {
  return new File(["Symbol\naapl\nBF-B\n"], "watchlist.csv", { type: "text/csv" });
}

describe("WatchlistUpload (acceptance test 6)", () => {
  it("sends the file with runNightly when checked, then shows the count, rejected rows and launch message", async () => {
    const api = new FakeApiClient().set("uploadWatchlist", { ...fx.watchlistUploadOut, launched: fx.jobLaunchOut });
    renderWithProviders(<WatchlistUpload />, { api });
    const file = csv();
    await userEvent.upload(screen.getByLabelText("CSV file"), file);
    await userEvent.click(screen.getByLabelText("Run nightly now"));
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(api.callsTo("uploadWatchlist")).toHaveLength(1));
    const [sent, opts] = api.callsTo("uploadWatchlist")[0]!;
    expect(sent).toBe(file);
    expect(opts).toEqual({ runNightly: true });
    expect(await screen.findByText("3 tickers stored for 2026-10-07.")).toBeInTheDocument();
    const rejected = screen.getByRole("table", { name: "Rejected rows" });
    expect(within(rejected).getByText("$$$")).toBeInTheDocument();
    expect(within(rejected).getByText("invalid ticker")).toBeInTheDocument();
    expect(within(rejected).getByText("duplicate")).toBeInTheDocument();
    expect(screen.getByText("nightly launched for 2026-10-07")).toBeInTheDocument();
  });

  it("sends the chosen date and no runNightly when unchecked", async () => {
    const { api } = renderWithProviders(<WatchlistUpload />);
    await userEvent.upload(screen.getByLabelText("CSV file"), csv());
    await userEvent.type(screen.getByLabelText(/Session date/), "2026-10-07");
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(api.callsTo("uploadWatchlist")).toHaveLength(1));
    expect(api.callsTo("uploadWatchlist")[0]![1]).toEqual({ date: "2026-10-07" });
  });

  it("Upload is disabled until a file is chosen, and a 422 shows the server's message", async () => {
    const api = new FakeApiClient().fail("uploadWatchlist", new ApiError(422, "validation", "No valid ticker in the file"));
    renderWithProviders(<WatchlistUpload />, { api });
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled();
    await userEvent.upload(screen.getByLabelText("CSV file"), csv());
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No valid ticker in the file");
  });

  it("shows the current watchlist for the date and Delete calls deleteWatchlist(date)", async () => {
    const { api } = renderWithProviders(<WatchlistUpload />);
    expect(await screen.findByText(/3 tickers for 2026-10-07/)).toBeInTheDocument();
    expect(screen.getByText(/replaces FinViz/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.callsTo("deleteWatchlist")).toEqual([["2026-10-07"]]));
  });

  it("with no watchlist stored says FinViz is used", async () => {
    const api = new FakeApiClient().set("watchlist", null);
    renderWithProviders(<WatchlistUpload />, { api });
    expect(await screen.findByText(/No uploaded watchlist: the nightly job uses FinViz/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });
});
