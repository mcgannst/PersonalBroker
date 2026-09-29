// DB-GWEB fix round 1 (Control): one Pause/Resume pair (the Engine card's), KillSwitchPanel in status tones
// on Control and unchanged on Settings, and Re-run only for jobs RunJob offers.
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MANUAL_JOBS, type ManualJob } from "../../api/types";
import * as fx from "../../test/fixtures";
import * as lfx from "../../test/liveFixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { KillSwitchPanel } from "../settings/KillSwitchPanel";
import { JobsCard } from "./JobsCard";
import { KillSwitchCard } from "./KillSwitchCard";

const switches = { switches: [...fx.killswitchStates.slice(0, 1), fx.killswitchDrawdownTripped], history: [] };

describe("KillSwitchPanel props", () => {
  it("on Control (KillSwitchCard): no Pause/Resume and no legacy green/red tone classes", async () => {
    renderWithProviders(<KillSwitchCard lights={lfx.killswitchLights} history={[]} />, { api: new FakeApiClient({ killswitches: switches }) });
    const region = screen.getByRole("region", { name: "Kill switches" });
    await within(region).findByRole("button", { name: "Reset Max drawdown" });
    expect(within(region).queryByRole("button", { name: "Pause" })).toBeNull();
    expect(within(region).queryByRole("button", { name: "Resume" })).toBeNull();
    expect(region.querySelectorAll(".tone-ok, .tone-bad")).toHaveLength(0);
    expect(region.querySelectorAll(".light.status-ok, .light.status-bad")).toHaveLength(2);
  });

  it("by default (Settings) keeps Pause/Resume and the legacy tone lights", async () => {
    renderWithProviders(<KillSwitchPanel />, { api: new FakeApiClient({ killswitches: switches }) });
    await screen.findByRole("button", { name: "Pause" });
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(document.querySelectorAll(".light.tone-ok, .light.tone-bad")).toHaveLength(2);
  });
});

describe("JobsCard Re-run", () => {
  it("offers Re-run only for a job in manual_jobs", () => {
    const manualJobs = MANUAL_JOBS.filter((j) => j !== "premarket") as ManualJob[];
    renderWithProviders(<JobsCard schedule={lfx.schedule} manualJobs={manualJobs} session={lfx.controlOut.session} />);
    const region = screen.getByRole("region", { name: "Today's schedule and jobs" });
    expect(within(region).queryByRole("button", { name: "Re-run Pre-market scan" })).toBeNull();
    const expected = lfx.schedule.filter((s) => s.rerun !== null && manualJobs.includes(s.rerun as ManualJob)).map((s) => `Re-run ${s.label}`);
    expect(within(region).getAllByRole("button", { name: /^Re-run / }).map((b) => b.getAttribute("aria-label"))).toEqual(expected);
  });
});
