// DB-T11 (plan S13): the System page's tests (pages/system/SystemPage.test.tsx, deleted with the page) carried
// over to the Control page, which absorbed it. Each case keeps its assertion against the equivalent Control
// element: headings of the same sections, the token paste still only on Settings, the time-zone check read from
// /api/meta (checking while it loads, never a stale OK, the server-offset warning), and the error box with
// Retry. Changed on purpose (open question 6): the Telegram test now sits on Control as well as Settings.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import type { MetaOut } from "../../api/types";
import { setDisplayZone } from "../../lib/format";
import * as fx from "../../test/fixtures";
import * as lfx from "../../test/liveFixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import ControlPage from "../Control";

function health(): HTMLElement {
  return screen.getByRole("region", { name: "Health" });
}

describe("Control page: carried over from the System page", () => {
  it("renders every System section (from /api/control)", async () => {
    const { api } = renderWithProviders(<ControlPage />, { route: "/control" });
    expect(await screen.findByRole("heading", { name: "Control", level: 1 })).toBeInTheDocument();
    await screen.findByRole("region", { name: "Health" });
    // System's cards -> Control's: token, worker, Telegram, version (Engine), time zone, rate limits (Questrade
    // today), job runs and Run a job (the schedule and jobs card), errors and the event log (Error log),
    // failed Telegram sends (Health), watchlist upload.
    for (const title of ["Questrade token", "Worker", "Telegram", "Run and version", "Time zone", "Questrade today", "Error log", "Today's schedule and jobs"]) {
      expect(screen.getByRole("heading", { name: title }), title).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { name: /failed telegram sends/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Watchlist upload" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Run a job" })).toBeInTheDocument();
    expect(api.callsTo("control")).toHaveLength(1);
    expect(api.callsTo("system")).toEqual([]);
  });

  it("has no token paste of its own: it links to it on Settings", async () => {
    const api = new FakeApiClient({
      control: lfx.controlWith({ health: { ...lfx.healthPanel, token: { ...fx.tokenOut, ok: false, error: "expired" } } }),
    });
    renderWithProviders(<ControlPage />, { api, route: "/control" });
    await screen.findByRole("region", { name: "Health" });
    expect(screen.queryByLabelText(/refresh token/i)).toBeNull();
    expect(within(health()).getByRole("link", { name: "Paste a new token" })).toHaveAttribute("href", "/settings#questrade");
    // open question 6: the Telegram test appears on Control too
    expect(within(health()).getByRole("button", { name: "Send a test message" })).toBeInTheDocument();
  });

  it("the time-zone check follows /api/meta: checking while it loads, then OK, never a stale OK", async () => {
    // Leave the display zone in the fixed mode another page might have left behind: the card must not read it.
    setDisplayZone({ fixedOffsetMinutes: -360, label: "MT" }, { browserOffsetMinutes: -420, serverOffsetMinutes: -360 });
    const api = new FakeApiClient();
    let release: (m: MetaOut) => void = () => undefined;
    api.respond("meta", () => new Promise<MetaOut>((resolve) => (release = resolve)));
    renderWithProviders(<ControlPage />, { api, route: "/control" });
    expect(await screen.findByText("Time zone: checking")).toBeInTheDocument();
    expect(screen.queryByText("Time zone OK")).toBeNull();
    await act(async () => release(fx.metaOut));
    expect(await screen.findByText("Time zone OK")).toHaveClass("ctl-chip-ok");
  });

  it("a server offset the browser disagrees with shows the amber warning from meta", async () => {
    setDisplayZone({ zone: "America/Edmonton", label: "MT" }, { browserOffsetMinutes: -360, serverOffsetMinutes: -360 });
    const api = new FakeApiClient({ meta: { ...fx.metaOut, tz_offset_minutes: -420 } });
    renderWithProviders(<ControlPage />, { api, route: "/control" });
    expect(await screen.findByText("Time zone: server offset")).toHaveClass("ctl-chip-warn");
    expect(screen.queryByText("Time zone OK")).toBeNull();
  });

  it("shows an error box with Retry when /api/control fails", async () => {
    const api = new FakeApiClient().fail("control", new ApiError(503, "unavailable", "Database unavailable"));
    renderWithProviders(<ControlPage />, { api, route: "/control" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    api.succeed("control");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("region", { name: "Health" })).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("control")).toHaveLength(2));
  });
});
