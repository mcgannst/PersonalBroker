// DB-T9 acceptance test 5: today's schedule (MT times, status, duration, attempts, summary) and Re-run,
// which opens the reused RunJob preselected; RunJob for every manual job below.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MANUAL_JOBS } from "../../api/types";
import * as lfx from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import { JobsCard } from "./JobsCard";

const session = lfx.controlOut.session;

function region() {
  return screen.getByRole("region", { name: "Today's schedule and jobs" });
}

function item(label: string): HTMLElement {
  const items = within(region()).getAllByRole("listitem");
  const found = items.find((li) => li.textContent?.includes(label));
  if (!found) throw new Error(`no schedule item ${label}`);
  return found;
}

describe("JobsCard", () => {
  it("lists every schedule item with its MT time, status, duration, attempts and summary", () => {
    renderWithProviders(<JobsCard schedule={lfx.schedule} manualJobs={[...MANUAL_JOBS]} session={session} />);
    expect(within(region()).getAllByRole("listitem")).toHaveLength(lfx.schedule.length);
    const premarket = item("Pre-market scan");
    expect(within(premarket).getByText("06:00 MT")).toBeInTheDocument();
    expect(within(premarket).getByText("done")).toBeInTheDocument();
    expect(within(premarket).getByText("took 1m 41s")).toBeInTheDocument();
    expect(within(premarket).getByText("2 attempts")).toBeInTheDocument();
    expect(within(premarket).getByText("8 catalysts classified")).toBeInTheDocument();
    const orb = item("ORB entry (orb_open)");
    expect(within(orb).getByText("07:35 MT")).toBeInTheDocument();
    expect(within(orb).getByText("543 bars in 31 s")).toBeInTheDocument();
    expect(within(orb).queryByText(/took/)).toBeNull();
    const nightly = item("Nightly universe");
    expect(within(nightly).getByText("took 4m 12s")).toBeInTheDocument();
    expect(within(nightly).queryByText(/attempt/)).toBeNull();
    expect(within(item("Flatten")).getByText("upcoming")).toBeInTheDocument();
    expect(within(region()).getByText(/Session 2026-10-06/)).toBeInTheDocument();
  });

  it("offers Re-run only on items with a rerun job", () => {
    renderWithProviders(<JobsCard schedule={lfx.schedule} manualJobs={[...MANUAL_JOBS]} session={session} />);
    for (const s of lfx.schedule) {
      const button = within(item(s.label)).queryByRole("button", { name: `Re-run ${s.label}` });
      if (s.rerun) expect(button, s.label).toBeInTheDocument();
      else expect(button, s.label).toBeNull();
    }
  });

  it("Re-run on premarket opens RunJob preselected on premarket; running it still confirms", async () => {
    const { api } = renderWithProviders(<JobsCard schedule={lfx.schedule} manualJobs={[...MANUAL_JOBS]} session={session} />);
    const select = within(region()).getByLabelText<HTMLSelectElement>("Job to run");
    expect(select.value).toBe("nightly");
    await userEvent.click(within(item("Pre-market scan")).getByRole("button", { name: "Re-run Pre-market scan" }));
    const reselected = within(region()).getByLabelText<HTMLSelectElement>("Job to run");
    expect(reselected.value).toBe("premarket");
    expect(Array.from(reselected.options).map((o) => o.value).sort()).toEqual([...MANUAL_JOBS].sort());
    await waitFor(() => expect(document.activeElement).toBe(reselected));
    await userEvent.click(within(region()).getByRole("button", { name: "Run" }));
    expect(api.callsTo("runJob")).toHaveLength(0);
    await userEvent.click(within(region()).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.callsTo("runJob")).toEqual([["premarket", {}]]));
  });

  it("with the schedule part failed shows its error and Retry, and RunJob still works", async () => {
    const onRetry = vi.fn();
    renderWithProviders(
      <JobsCard schedule={null} manualJobs={[...MANUAL_JOBS]} session={session} error="OperationalError: schedule could not be read" onRetry={onRetry} />,
    );
    expect(within(region()).getByRole("alert")).toHaveTextContent("OperationalError: schedule could not be read");
    await userEvent.click(within(region()).getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(within(region()).getByLabelText("Job to run")).toBeInTheDocument();
  });

  it("an empty schedule says so; a non-session day is labelled", () => {
    renderWithProviders(<JobsCard schedule={[]} manualJobs={[...MANUAL_JOBS]} session={{ ...session, is_session: false, date: "2026-10-12" }} />);
    expect(within(region()).getByText("Nothing scheduled.")).toBeInTheDocument();
    expect(within(region()).getByText(/Next session 2026-10-12/)).toBeInTheDocument();
  });

  it("renders free text as plain text", () => {
    const { control } = lfx.withXssText();
    const { container } = renderWithProviders(<JobsCard schedule={control.schedule} manualJobs={[...MANUAL_JOBS]} session={session} />);
    expect(within(region()).getByText(`8 catalysts classified${lfx.XSS}`)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
});
