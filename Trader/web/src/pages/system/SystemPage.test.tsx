import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import type { MetaOut } from "../../api/types";
import { setDisplayZone } from "../../lib/format";
import * as fx from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import SystemPage from "../System";

describe("System page", () => {
  it("renders every section from /api/system", async () => {
    const { api } = renderWithProviders(<SystemPage />, { route: "/system" });
    expect(await screen.findByRole("heading", { name: "System", level: 1 })).toBeInTheDocument();
    expect(await screen.findByRole("status", { name: "Token OK" })).toBeInTheDocument();
    for (const title of [
      "Questrade token",
      "Worker",
      "Telegram",
      "Version",
      "Time zone",
      "Rate limits",
      "Job runs",
      "Run a job",
      "Errors",
      "Event log",
      "Failed Telegram sends",
      "Watchlist upload",
    ]) {
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    }
    expect(api.callsTo("system")).toHaveLength(1);
  });

  it("has no token paste or Telegram test of its own: it links to them on Settings", async () => {
    const api = new FakeApiClient({ system: { ...fx.systemOut, token: { ...fx.tokenOut, ok: false, error: "expired" } } });
    renderWithProviders(<SystemPage />, { api, route: "/system" });
    expect(await screen.findByRole("status", { name: "Token error" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/refresh token/i)).toBeNull();
    expect(screen.queryByRole("button", { name: "Send a test message" })).toBeNull();
    expect(screen.getByRole("link", { name: "Paste a new token" })).toHaveAttribute("href", "/settings#questrade");
    expect(screen.getByRole("link", { name: "Send a test message" })).toHaveAttribute("href", "/settings#telegram");
  });

  it("the time-zone card follows /api/meta: checking while it loads, then OK, never a stale OK", async () => {
    // Leave the display zone in the fixed mode another page might have left behind: the card must not read it.
    setDisplayZone({ fixedOffsetMinutes: -360, label: "MT" }, { browserOffsetMinutes: -420, serverOffsetMinutes: -360 });
    const api = new FakeApiClient();
    let release: (m: MetaOut) => void = () => undefined;
    api.respond("meta", () => new Promise<MetaOut>((resolve) => (release = resolve)));
    renderWithProviders(<SystemPage />, { api, route: "/system" });
    expect(await screen.findByRole("status", { name: "Time zone: checking" })).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Time zone OK" })).toBeNull();
    await act(async () => release(fx.metaOut));
    expect(await screen.findByRole("status", { name: "Time zone OK" })).toHaveClass("tone-ok");
  });

  it("a server offset the browser disagrees with shows the amber warning from meta", async () => {
    setDisplayZone({ zone: "America/Edmonton", label: "MT" }, { browserOffsetMinutes: -360, serverOffsetMinutes: -360 });
    const api = new FakeApiClient({ meta: { ...fx.metaOut, tz_offset_minutes: -420 } });
    renderWithProviders(<SystemPage />, { api, route: "/system" });
    expect(await screen.findByRole("status", { name: "Time zone: server offset" })).toHaveClass("tone-warn");
    expect(screen.queryByRole("status", { name: "Time zone OK" })).toBeNull();
  });

  it("shows an error box with Retry when /api/system fails", async () => {
    const api = new FakeApiClient().fail("system", new ApiError(503, "unavailable", "Database unavailable"));
    renderWithProviders(<SystemPage />, { api, route: "/system" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    api.succeed("system");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("status", { name: "Token OK" })).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("system")).toHaveLength(2));
  });
});
