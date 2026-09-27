// P5-T8 acceptance tests 2, 3 (offline), 4 and 7: the start form.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import type { ReplayOptionsOut, SettingOut } from "../../api/types";
import { replayOptions, replayQueued, settingsItems, settingsOut } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { ReplayForm } from "./ReplayForm";

const catalystMode: SettingOut = {
  key: "replay.catalyst_mode",
  value: "stored",
  default: "stored",
  is_default: true,
  group: "Replay",
  field: {
    name: "replay.catalyst_mode",
    kind: "enum",
    title: "Replay Catalyst Mode",
    description: null,
    default: "stored",
    minimum: null,
    maximum: null,
    exclusive_minimum: false,
    exclusive_maximum: false,
    enum: ["stored", "unknown"],
    item_enum: null,
    pattern: null,
    nullable: false,
  },
  updated_at: null,
  updated_by: null,
};

function renderForm(options: ReplayOptionsOut = replayOptions, api = new FakeApiClient()) {
  return renderWithProviders(<ReplayForm options={options} />, { api, route: "/replay" });
}

async function form() {
  return screen.findByRole("form", { name: "New replay" });
}

describe("the start form (acceptance test 2)", () => {
  it("takes its defaults and data notes from replayOptions", async () => {
    renderForm();
    const f = await form();
    const from = within(f).getByLabelText("From") as HTMLInputElement;
    const to = within(f).getByLabelText("To") as HTMLInputElement;
    // 20 weekdays ending at latest_allowed (2026-11-27).
    expect(to.value).toBe("2026-11-27");
    expect(from.value).toBe("2026-11-02");
    expect(to.max).toBe("2026-11-27");
    expect(from.max).toBe("2026-11-27");
    expect(
      within(f).getByText(
        "Questrade data only from 2026-09-03; archive from 2026-10-06; universe snapshots from 2026-09-28; earlier days use today's universe (biased)",
      ),
    ).toBeInTheDocument();
    const offline = within(f).getByRole("checkbox", { name: "Offline (database only)" });
    expect(offline).not.toBeChecked();
    expect(offline).toBeEnabled();
    // Only the override keys that have a descriptor are shown, with the current value as the default.
    expect((within(f).getByLabelText("Risk Pct") as HTMLInputElement).value).toBe("0.02");
    expect(within(f).queryByLabelText("Approval Mode")).toBeNull();
  });

  it("sends only the changed setting and strategy param, then opens the new replay", async () => {
    const r = renderForm();
    const f = await form();
    const risk = within(f).getByLabelText("Risk Pct");
    await userEvent.clear(risk);
    await userEvent.type(risk, "0.01");
    const orb = within(f).getByRole("region", { name: "Strategy orb_sip" });
    const topN = within(orb).getByLabelText("Top N");
    await userEvent.clear(topN);
    await userEvent.type(topN, "10");
    await userEvent.click(within(f).getByRole("button", { name: "Start replay" }));
    await waitFor(() => expect(r.api.callsTo("startReplay")).toHaveLength(1));
    expect(r.api.callsTo("startReplay")[0]).toEqual([
      {
        date_from: "2026-11-02",
        date_to: "2026-11-27",
        overrides: { risk_pct: "0.01" },
        strategies: { orb_sip: { params: { top_n: 10 } } },
        offline: false,
      },
    ]);
    await waitFor(() => expect(r.location().search).toBe(`?id=${replayQueued.id}`));
    expect(r.location().pathname).toBe("/replay");
  });

  it("sends the label, the range typed and a disabled strategy", async () => {
    const r = renderForm();
    const f = await form();
    await userEvent.type(within(f).getByLabelText("Label"), "Short week");
    fireEvent.change(within(f).getByLabelText("From"), { target: { value: "2026-11-23" } });
    const spy = within(f).getByRole("region", { name: "Strategy spy_overlay" });
    await userEvent.click(within(spy).getByRole("checkbox", { name: "Enabled" }));
    await userEvent.click(within(f).getByRole("checkbox", { name: "Offline (database only)" }));
    await userEvent.click(within(f).getByRole("button", { name: "Start replay" }));
    await waitFor(() => expect(r.api.callsTo("startReplay")).toHaveLength(1));
    expect(r.api.callsTo("startReplay")[0]).toEqual([
      {
        date_from: "2026-11-23",
        date_to: "2026-11-27",
        label: "Short week",
        overrides: {},
        strategies: { spy_overlay: { enabled: false } },
        offline: true,
      },
    ]);
  });
});

describe("offline (acceptance test 3)", () => {
  it("offline_now: true checks and locks the Offline box and shows the market-hours note", async () => {
    const r = renderForm({ ...replayOptions, offline_now: true });
    const f = await form();
    const offline = within(f).getByRole("checkbox", { name: "Offline (database only)" });
    expect(offline).toBeChecked();
    expect(offline).toBeDisabled();
    expect(within(f).getByText("Market hours: the replay uses stored data only")).toBeInTheDocument();
    await userEvent.click(within(f).getByRole("button", { name: "Start replay" }));
    await waitFor(() => expect(r.api.callsTo("startReplay")).toHaveLength(1));
    expect((r.api.callsTo("startReplay")[0]![0] as { offline: boolean }).offline).toBe(true);
  });

  it("names missing archive and snapshot dates", async () => {
    renderForm({ ...replayOptions, archive_from: null, snapshots_from: null });
    const f = await form();
    expect(
      within(f).getByText(
        "Questrade data only from 2026-09-03; no candle archive yet; no universe snapshots yet; days without a snapshot use today's universe (biased)",
      ),
    ).toBeInTheDocument();
  });
});

describe("server errors (acceptance test 4)", () => {
  it("a 422 for overrides.risk_pct shows the message under that input, others under theirs", async () => {
    const api = new FakeApiClient().fail(
      "startReplay",
      new ApiError(422, "validation", "The replay request is invalid", [
        { loc: ["body", "overrides", "risk_pct"], msg: "must be at most 0.10" },
        { loc: ["body", "date_to"], msg: "must be on or before 2026-11-27" },
        { loc: ["body", "strategies", "orb_sip", "params", "top_n"], msg: "must be at least 1" },
        { loc: ["body", "strategies"], msg: "at least one entry strategy must be enabled" },
      ]),
    );
    renderForm(replayOptions, api);
    const f = await form();
    await userEvent.click(within(f).getByRole("button", { name: "Start replay" }));
    const msg = await within(f).findByText("must be at most 0.10");
    const riskField = within(f).getByLabelText("Risk Pct").closest(".field");
    expect(riskField).not.toBeNull();
    expect(riskField).toContainElement(msg);
    const toField = within(f).getByLabelText("To").closest(".field");
    expect(toField).toContainElement(within(f).getByText("must be on or before 2026-11-27"));
    const orb = within(f).getByRole("region", { name: "Strategy orb_sip" });
    expect(within(orb).getByLabelText("Top N").closest(".field")).toContainElement(within(orb).getByText("must be at least 1"));
    expect(within(f).getByText("at least one entry strategy must be enabled")).toBeInTheDocument();
    expect(within(f).getByText("The replay request is invalid")).toBeInTheDocument();
  });

  it("a 409 shows the server message", async () => {
    const api = new FakeApiClient().fail("startReplay", new ApiError(409, "conflict", "A replay is already running."));
    const r = renderForm(replayOptions, api);
    const f = await form();
    await userEvent.click(within(f).getByRole("button", { name: "Start replay" }));
    expect(await within(f).findByRole("alert")).toHaveTextContent("A replay is already running.");
    expect(r.location().search).toBe("");
  });
});

describe("catalyst mode warning (acceptance test 7)", () => {
  it("the catalyst-mode unknown override with require_catalyst on says no entries will be taken", async () => {
    const api = new FakeApiClient({ settings: { items: [...settingsItems, catalystMode] } });
    renderForm(replayOptions, api);
    const f = await form();
    const mode = await within(f).findByLabelText("Replay Catalyst Mode");
    const warning = /no entries will be taken/;
    expect(within(f).queryByText(warning)).toBeNull();
    await userEvent.selectOptions(mode, "unknown");
    expect(within(f).getByText(warning)).toBeInTheDocument();
    // Turning require_catalyst off removes the warning.
    const orb = within(f).getByRole("region", { name: "Strategy orb_sip" });
    await userEvent.click(within(orb).getByRole("checkbox", { name: "Require Catalyst" }));
    expect(within(f).queryByText(warning)).toBeNull();
  });

  it("no descriptor for the catalyst mode: no warning and nothing to change", async () => {
    renderForm(replayOptions, new FakeApiClient({ settings: settingsOut }));
    const f = await form();
    expect(within(f).queryByText(/no entries will be taken/)).toBeNull();
  });
});
