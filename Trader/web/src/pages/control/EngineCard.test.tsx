// DB-T9 acceptance tests 2 and 3 (engine part): Pause / Resume with the Confirm step, approval mode through
// the reused ApprovalMode component, the engine facts and the part error.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import { qk } from "../../api/queryKeys";
import * as lfx from "../../test/liveFixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { EngineCard } from "./EngineCard";

function region() {
  return screen.getByRole("region", { name: "Engine" });
}

describe("EngineCard", () => {
  it("shows the trading state, the live run, the deployed version and the alembic revision", async () => {
    renderWithProviders(<EngineCard engine={lfx.engineOut} />);
    const r = region();
    expect(within(r).getByText("Running")).toBeInTheDocument();
    expect(within(r).getByText(`#${lfx.engineOut.run_id}`, { exact: false })).toBeInTheDocument();
    expect(within(r).getByText(/since 2026-09-29/)).toBeInTheDocument();
    expect(within(r).getByText("phase-5-complete-24-g459e172 (dev)")).toBeInTheDocument();
    expect(within(r).getByText("0008")).toBeInTheDocument();
    // approval mode comes from the reused component (its own settings query)
    expect(await within(r).findByRole("group", { name: "Approval mode" })).toBeInTheDocument();
    expect(within(r).queryByRole("button", { name: "Resume" })).toBeNull();
  });

  it("Pause asks first; confirming calls api.pause() once and invalidates dashboard, system and killswitches", async () => {
    const { api, queryClient } = renderWithProviders(<EngineCard engine={lfx.engineOut} />);
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    await userEvent.click(within(region()).getByRole("button", { name: "Pause" }));
    const dialog = within(region()).getByRole("alertdialog", { name: "Pause new entries" });
    expect(api.callsTo("pause")).toHaveLength(0);
    await userEvent.click(within(dialog).getByRole("button", { name: "Pause new entries" }));
    await waitFor(() => expect(api.callsTo("pause")).toHaveLength(1));
    const keys = spy.mock.calls.map(([f]) => JSON.stringify(f?.queryKey));
    await waitFor(() => {
      expect(keys).toEqual(expect.arrayContaining([JSON.stringify(qk.dashboard()), JSON.stringify(qk.system()), JSON.stringify(qk.killswitches())]));
    });
    expect(await within(region()).findByText("Paused: no new entries.")).toBeInTheDocument();
    expect(api.callsTo("resume")).toHaveLength(0);
  });

  it("cancelling the pause calls nothing", async () => {
    const { api } = renderWithProviders(<EngineCard engine={lfx.engineOut} />);
    await userEvent.click(within(region()).getByRole("button", { name: "Pause" }));
    await userEvent.click(within(region()).getByRole("button", { name: "Cancel" }));
    expect(within(region()).queryByRole("alertdialog")).toBeNull();
    expect(api.callsTo("pause")).toHaveLength(0);
    expect(api.callsTo("resume")).toHaveLength(0);
  });

  it("when paused shows since when, and Resume with its own confirm", async () => {
    const engine = { ...lfx.engineOut, trading: "paused" as const, paused_at: "2026-10-06T14:10:00Z" };
    const { api } = renderWithProviders(<EngineCard engine={engine} />);
    expect(within(region()).getByText("Paused")).toBeInTheDocument();
    expect(within(region()).getByText("since 2026-10-06 08:10 MT")).toBeInTheDocument();
    expect(within(region()).queryByRole("button", { name: "Pause" })).toBeNull();
    await userEvent.click(within(region()).getByRole("button", { name: "Resume" }));
    await userEvent.click(within(region()).getByRole("button", { name: "Cancel" }));
    expect(api.callsTo("resume")).toHaveLength(0);
    await userEvent.click(within(region()).getByRole("button", { name: "Resume" }));
    const dialog = within(region()).getByRole("alertdialog", { name: "Resume new entries" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Resume new entries" }));
    await waitFor(() => expect(api.callsTo("resume")).toHaveLength(1));
    expect(api.callsTo("pause")).toHaveLength(0);
  });

  it("blocked by a kill switch still offers Pause", () => {
    renderWithProviders(<EngineCard engine={{ ...lfx.engineOut, trading: "blocked" }} />);
    expect(within(region()).getByText("Blocked by a kill switch")).toBeInTheDocument();
    expect(within(region()).getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it("shows the server's message when the pause is refused", async () => {
    const api = new FakeApiClient().fail("pause", new ApiError(409, "conflict", "Already paused."));
    renderWithProviders(<EngineCard engine={lfx.engineOut} />, { api });
    await userEvent.click(within(region()).getByRole("button", { name: "Pause" }));
    await userEvent.click(within(region()).getByRole("button", { name: "Pause new entries" }));
    expect(await within(region()).findByRole("alert")).toHaveTextContent("Already paused.");
  });

  it("approval mode: Auto needs the confirm, then putSetting('approval_mode', 'auto')", async () => {
    const { api } = renderWithProviders(<EngineCard engine={lfx.engineOut} />);
    const group = await within(region()).findByRole("group", { name: "Approval mode" });
    await userEvent.click(within(group).getByRole("button", { name: "Auto" }));
    expect(api.callsTo("putSetting")).toHaveLength(0);
    await userEvent.click(within(region()).getByRole("button", { name: "Switch to Auto" }));
    await waitFor(() => expect(api.callsTo("putSetting")).toEqual([["approval_mode", "auto"]]));
  });

  it("a failed engine part shows its error and Retry", async () => {
    const onRetry = vi.fn();
    renderWithProviders(<EngineCard engine={null} error="OperationalError: engine could not be read" onRetry={onRetry} />);
    expect(within(region()).getByRole("alert")).toHaveTextContent("OperationalError: engine could not be read");
    await userEvent.click(within(region()).getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
