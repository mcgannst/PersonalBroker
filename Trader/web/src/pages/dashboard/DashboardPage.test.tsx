import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import type { DashboardOut } from "../../api/types";
import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import Dashboard from "../Dashboard";

function dashboard(patch: Partial<DashboardOut>): DashboardOut {
  return { ...fx.dashboardOut, ...patch };
}

async function renderDashboard(api = new FakeApiClient(), route = "/dashboard") {
  const r = renderWithProviders(<Dashboard />, { api, route });
  await screen.findByRole("list", { name: "Timeline" });
  return r;
}

describe("Dashboard header and timeline (acceptance test 1)", () => {
  it("shows the session phase, the MANUAL badge and the timeline in order with MT times", async () => {
    await renderDashboard();
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("2026-10-06")).toBeInTheDocument();
    expect(screen.getByText("Open")).toBeInTheDocument();
    expect(screen.getByText("MANUAL")).toHaveClass("badge", "tone-info");

    const items = within(screen.getByRole("list", { name: "Timeline" })).getAllByRole("listitem");
    expect(items.map((li) => li.getAttribute("data-key"))).toEqual(fx.timeline.map((t) => t.key));
    expect(items[0]).toHaveTextContent("06:00 MT");
    expect(items[2]).toHaveTextContent("07:35 MT");
    expect(items[2]).toHaveTextContent("ORB entry (orb_open)");
    expect(items[2]).toHaveTextContent("✓");
    const next = items[3] as HTMLElement;
    expect(next).toHaveClass("is-next");
    expect(next).toHaveAttribute("aria-current", "step");
    expect(items.filter((li) => li.classList.contains("is-next"))).toHaveLength(1);
  });

  it("shows the AUTO badge as a warning, and each phase label", async () => {
    const api = new FakeApiClient({ dashboard: dashboard({ approval_mode: "auto", session: { ...fx.dashboardOut.session, phase: "closed_day" } }) });
    await renderDashboard(api);
    expect(screen.getByText("AUTO")).toHaveClass("badge", "tone-warn");
    expect(screen.getByText("Market closed today")).toBeInTheDocument();
  });

  it("marks failed and missed timeline items in red", async () => {
    const timeline = [
      { ...fx.timeline[0]!, status: "failed" as const },
      { ...fx.timeline[1]!, status: "missed" as const },
    ];
    await renderDashboard(new FakeApiClient({ dashboard: dashboard({ timeline }) }));
    const items = within(screen.getByRole("list", { name: "Timeline" })).getAllByRole("listitem");
    expect(items[0]).toHaveClass("tone-bad");
    expect(items[0]).toHaveTextContent("failed");
    expect(items[1]).toHaveClass("tone-bad");
    expect(items[1]).toHaveTextContent("missed");
  });

  it("links token and worker problems to the System page", async () => {
    const api = new FakeApiClient({
      dashboard: dashboard({ token: { ...fx.tokenOut, ok: false }, worker: { ...fx.workerOut, ok: false } }),
    });
    await renderDashboard(api);
    expect(screen.getByRole("status", { name: /token/i })).toHaveClass("tone-bad");
    expect(screen.getByRole("status", { name: /worker/i })).toHaveClass("tone-bad");
    const links = screen.getAllByRole("link", { name: /system/i });
    expect(links.length).toBeGreaterThanOrEqual(1);
    for (const link of links) expect(link).toHaveAttribute("href", "/system");
  });

  it("shows no System link when token and worker are OK", async () => {
    await renderDashboard();
    expect(screen.getByRole("status", { name: /token/i })).toHaveClass("tone-ok");
    expect(screen.queryByRole("link", { name: /system/i })).toBeNull();
  });
});

describe("Dashboard approvals (acceptance tests 2 and 5)", () => {
  it("approving refetches the dashboard", async () => {
    const api = new FakeApiClient();
    await renderDashboard(api);
    expect(api.callsTo("dashboard")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Approved")).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("dashboard")).toHaveLength(2));
  });

  it("keeps the decision message after the proposal leaves the pending list", async () => {
    const api = new FakeApiClient();
    api.respond("approve", () => {
      api.set("dashboard", dashboard({ pending: [] }));
      return fx.decisionOut;
    });
    await renderDashboard(api);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull());
    const decisions = screen.getByRole("list", { name: "Decisions" });
    expect(decisions).toHaveTextContent("AAA");
    expect(decisions).toHaveTextContent("Approved");
  });

  it("?proposal=<pending id> highlights that card", async () => {
    await renderDashboard(new FakeApiClient(), "/dashboard?proposal=12");
    const card = screen.getByRole("article", { name: /proposal 12/i });
    expect(card).toHaveClass("is-highlighted");
    expect(screen.queryByRole("region", { name: /proposal 12/i })).toBeNull();
  });

  it("?proposal=<decided id> shows the panel with status, decided_at in MT and decided_via", async () => {
    const api = new FakeApiClient();
    await renderDashboard(api, "/dashboard?proposal=11");
    const panel = await screen.findByRole("region", { name: /proposal 11/i });
    expect(within(panel).getByText(/submitted/)).toBeInTheDocument();
    expect(within(panel).getByText(/2026-10-05 07:36 MT/)).toBeInTheDocument();
    expect(within(panel).getByText(/via web/)).toBeInTheDocument();
    expect(within(panel).getByText(/web:stephen/)).toBeInTheDocument();
    expect(api.callsTo("proposal")).toEqual([[11]]);
    expect(screen.getByRole("article", { name: /proposal 12/i })).not.toHaveClass("is-highlighted");
  });

  it("?proposal=<unknown id> shows the server's error in the panel", async () => {
    await renderDashboard(new FakeApiClient(), "/dashboard?proposal=999");
    expect(await screen.findByText("Proposal 999 not found")).toBeInTheDocument();
  });

  it("ignores a non-numeric ?proposal", async () => {
    const api = new FakeApiClient();
    await renderDashboard(api, "/dashboard?proposal=abc");
    expect(api.callsTo("proposal")).toEqual([]);
  });
});

describe("Dashboard positions, P&L and events (acceptance test 6)", () => {
  it("flags a position without a stop order and shows n/a for a missing quote", async () => {
    await renderDashboard(new FakeApiClient({ dashboard: dashboard({ positions: [fx.openPosition, fx.unprotectedPosition] }) }));
    const ddd = screen.getByRole("article", { name: /position DDD/i });
    const noStop = within(ddd).getByText("(no stop order)");
    expect(noStop).toHaveClass("tone-bad");
    expect(within(ddd).getByText(/Unprotected 3m 20s/)).toBeInTheDocument();
    expect(within(ddd).getByText(/Last n\/a/)).toBeInTheDocument();
    expect(within(ddd).getByRole("link", { name: /details/i })).toHaveAttribute("href", "/trades?position=5");

    const ccc = screen.getByRole("article", { name: /position CCC/i });
    expect(within(ccc).queryByText("(no stop order)")).toBeNull();
    expect(within(ccc).getByText(/Last 18.90/)).toBeInTheDocument();
    expect(within(ccc).getByText("$12.00")).toBeInTheDocument();
  });

  it("shows the P&L tiles with a partial marker", async () => {
    const api = new FakeApiClient({ dashboard: dashboard({ pnl: { ...fx.pnlOut, unrealized_partial: true, drawdown_pct: "0.0138" } }) });
    await renderDashboard(api);
    const tiles = screen.getByRole("region", { name: "P&L" });
    expect(within(tiles).getByText("Realized today")).toBeInTheDocument();
    expect(within(tiles).getByText("$0.00")).toBeInTheDocument();
    expect(within(tiles).getByText("$12.00")).toBeInTheDocument();
    expect(within(tiles).getByText("partial")).toBeInTheDocument();
    expect(within(tiles).getByText("$19.73")).toBeInTheDocument();
    expect(within(tiles).getByText("$751.73")).toBeInTheDocument();
    expect(within(tiles).getByText("1.38%")).toBeInTheDocument();
  });

  it("lists the latest events with MT time, level and source", async () => {
    await renderDashboard();
    const list = screen.getByRole("list", { name: "Events" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("07:36 MT");
    expect(items[0]).toHaveTextContent("proposals");
    expect(items[0]).toHaveTextContent("Proposal 12 created");
    expect(within(items[2] as HTMLElement).getByText("warning")).toHaveClass("tone-warn");
  });

  it("shows at most 20 events", async () => {
    const many = Array.from({ length: 25 }, (_, i) => ({ ...fx.events[0]!, id: 1000 + i, message: `event ${i}` }));
    await renderDashboard(new FakeApiClient({ dashboard: dashboard({ events: many }) }));
    expect(within(screen.getByRole("list", { name: "Events" })).getAllByRole("listitem")).toHaveLength(20);
  });
});

describe("Dashboard kill switches and Telegram notice (acceptance test 7)", () => {
  it("shows a tripped max_drawdown_pct in red with its clears text and a Settings link", async () => {
    const killswitches = fx.killswitchStates.map((k) => (k.switch === "max_drawdown_pct" ? fx.killswitchDrawdownTripped : k));
    await renderDashboard(new FakeApiClient({ dashboard: dashboard({ killswitches }) }));
    const light = screen.getByRole("status", { name: "Max drawdown: tripped" });
    expect(light).toHaveClass("tone-bad");
    expect(screen.getByText(fx.killswitchDrawdownTripped.clears)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /reset in settings/i })).toHaveAttribute("href", "/settings#killswitches");
    expect(screen.getByRole("status", { name: "Daily loss: off" })).toHaveClass("tone-ok");
  });

  it("shows a manual pause in amber, with no Settings link", async () => {
    const killswitches = fx.killswitchStates.map((k) =>
      k.switch === "manual_pause" ? { ...k, tripped: true, clears: "/resume in Telegram or Resume on the web" } : k,
    );
    await renderDashboard(new FakeApiClient({ dashboard: dashboard({ killswitches }) }));
    expect(screen.getByRole("status", { name: "Paused: tripped" })).toHaveClass("tone-warn");
    expect(screen.queryByRole("link", { name: /reset in settings/i })).toBeNull();
  });

  it("shows the Telegram notice only when Telegram is not configured", async () => {
    const { unmount } = await renderDashboard();
    expect(screen.queryByText("Telegram is not configured: approve here")).toBeNull();
    unmount();
    await renderDashboard(new FakeApiClient({ dashboard: dashboard({ telegram_configured: false }) }));
    expect(screen.getByText("Telegram is not configured: approve here")).toBeInTheDocument();
  });
});

describe("Dashboard loading and errors", () => {
  it("shows the server's error with Retry, and Retry refetches", async () => {
    const api = new FakeApiClient();
    api.fail("dashboard", new ApiError(503, "unavailable", "Database unavailable"));
    renderWithProviders(<Dashboard />, { api });
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(await screen.findByText("Database unavailable")).toBeInTheDocument();
    api.succeed("dashboard");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("list", { name: "Timeline" })).toBeInTheDocument();
    expect(api.callsTo("dashboard")).toHaveLength(2);
  });

  it("shows an empty state with no pending proposals and no positions", async () => {
    await renderDashboard(new FakeApiClient({ dashboard: dashboard({ pending: [], positions: [] }) }));
    expect(screen.getByText("Nothing waiting for approval")).toBeInTheDocument();
    expect(screen.getByText("No open positions")).toBeInTheDocument();
  });
});
