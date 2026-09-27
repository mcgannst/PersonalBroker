import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import * as fx from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { RunJob } from "./RunJob";

describe("RunJob (acceptance test 4)", () => {
  it("choosing nightly, Force and confirming calls runJob('nightly', {force: true}) and shows the message", async () => {
    const { api } = renderWithProviders(<RunJob jobs={fx.systemOut.manual_jobs} />);
    await userEvent.selectOptions(screen.getByLabelText("Job to run"), "nightly");
    await userEvent.click(screen.getByLabelText("Force"));
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(api.callsTo("runJob")).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.callsTo("runJob")).toEqual([["nightly", { force: true }]]));
    expect(await screen.findByText("nightly launched for 2026-10-07")).toBeInTheDocument();
  });

  it("sends the date when one is given, and cancelling calls nothing", async () => {
    const { api } = renderWithProviders(<RunJob jobs={fx.systemOut.manual_jobs} />);
    await userEvent.selectOptions(screen.getByLabelText("Job to run"), "premarket");
    await userEvent.type(screen.getByLabelText(/Date/), "2026-10-07");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.callsTo("runJob")).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.callsTo("runJob")).toEqual([["premarket", { date: "2026-10-07" }]]));
  });

  it("a 409 shows the server's 'already running' message", async () => {
    const api = new FakeApiClient().fail("runJob", new ApiError(409, "conflict", "nightly is already running"));
    renderWithProviders(<RunJob jobs={fx.systemOut.manual_jobs} />, { api });
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already running");
  });

  it("token-refresh takes no date or force", async () => {
    const { api } = renderWithProviders(<RunJob jobs={fx.systemOut.manual_jobs} />);
    await userEvent.click(screen.getByLabelText("Force"));
    await userEvent.selectOptions(screen.getByLabelText("Job to run"), "token-refresh");
    expect(screen.getByLabelText("Force")).toBeDisabled();
    expect(screen.getByLabelText(/Date/)).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.callsTo("runJob")).toEqual([["token-refresh", {}]]));
  });
});
