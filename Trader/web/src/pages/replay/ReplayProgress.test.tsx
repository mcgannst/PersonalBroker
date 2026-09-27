// P5-T8 acceptance test 5: progress and Cancel; the 5 s refetch while the stream is down.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { replayQueued, replayRunning } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import ReplayPage from "../Replay";
import { DISCONNECTED_REPLAY_REFETCH_MS, replayRefetchInterval } from "./ReplayDetail";

describe("progress and cancel (acceptance test 5)", () => {
  it("the running fixture shows the progress bar (3 of 10) and a Cancel button that, after confirmation, calls cancelReplay once", async () => {
    const r = renderWithProviders(<ReplayPage />, { route: "/replay?id=13" });
    const bar = await screen.findByRole("progressbar", { name: "Progress" });
    expect(bar).toHaveAttribute("value", "3");
    expect(bar).toHaveAttribute("max", "10");
    expect(screen.getByText("3 of 10 sessions, at 2026-11-18")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Cancel replay" }));
    const confirm = screen.getByRole("alertdialog", { name: "Stop replay" });
    expect(within(confirm).getByText("Stop this replay after the current day?")).toBeInTheDocument();
    expect(r.api.callsTo("cancelReplay")).toHaveLength(0);
    await userEvent.click(within(confirm).getByRole("button", { name: "Stop replay" }));
    await waitFor(() => expect(r.api.callsTo("cancelReplay")).toEqual([[13]]));
    expect(await screen.findByText("Stopping after the current day.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel replay" })).toBeNull();
    expect(r.api.callsTo("cancelReplay")).toHaveLength(1);
  });

  it("Keep running closes the confirmation without a call", async () => {
    const r = renderWithProviders(<ReplayPage />, { route: "/replay?id=13" });
    await userEvent.click(await screen.findByRole("button", { name: "Cancel replay" }));
    await userEvent.click(screen.getByRole("button", { name: "Keep running" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(r.api.callsTo("cancelReplay")).toHaveLength(0);
  });

  it("a failed cancel shows the server message", async () => {
    const { ApiError } = await import("../../api/client");
    const api = new FakeApiClient().fail("cancelReplay", new ApiError(409, "conflict", "The replay is not running."));
    renderWithProviders(<ReplayPage />, { route: "/replay?id=13", api });
    await userEvent.click(await screen.findByRole("button", { name: "Cancel replay" }));
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Stop replay" }));
    expect(await screen.findByText("The replay is not running.")).toBeInTheDocument();
  });

  it("a synchronous double click on Stop sends one cancel, and a retry after a failure sends another (fix round 1)", async () => {
    const { ApiError } = await import("../../api/client");
    const api = new FakeApiClient().fail("cancelReplay", new ApiError(503, "unavailable", "try again"));
    renderWithProviders(<ReplayPage />, { route: "/replay?id=13", api });
    await userEvent.click(await screen.findByRole("button", { name: "Cancel replay" }));
    const stop = within(screen.getByRole("alertdialog")).getByRole("button", { name: "Stop replay" });
    fireEvent.click(stop);
    fireEvent.click(stop);
    expect(await screen.findByText("try again")).toBeInTheDocument();
    expect(api.callsTo("cancelReplay")).toHaveLength(1);
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Stop replay" }));
    await waitFor(() => expect(api.callsTo("cancelReplay")).toHaveLength(2));
  });

  it("a queued replay shows 0 of its sessions and can be cancelled too", async () => {
    renderWithProviders(<ReplayPage />, { route: "/replay?id=12" });
    expect(await screen.findByText("0 of 4 sessions")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel replay" })).toBeInTheDocument();
  });
});

describe("refetch while the stream is down", () => {
  it("polls every 5 s only while queued or running and not connected", () => {
    expect(DISCONNECTED_REPLAY_REFETCH_MS).toBe(5000);
    expect(replayRefetchInterval(replayRunning, false)).toBe(5000);
    expect(replayRefetchInterval(replayQueued, false)).toBe(5000);
    expect(replayRefetchInterval(replayRunning, true)).toBe(false);
    expect(replayRefetchInterval({ ...replayRunning, status: "completed" }, false)).toBe(false);
    expect(replayRefetchInterval(undefined, false)).toBe(false);
  });
});
