// DB-T11 acceptance tests 1, 2 and 6: the new Dashboard page (`GET /api/live`) composed from the DB-T7/DB-T8
// components, with the URL state (`?range=`, `?expand=`, `?proposal=`), the pending approvals, part isolation and
// the live indicator. It also carries over every assertion of the deleted pages/dashboard/DashboardPage.test.tsx
// that still applies (plan S13), each marked "carried over".
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { ApiError, ApiProvider } from "../../api/client";
import type { LiveOut } from "../../api/types";
import { ROUTER_FUTURE } from "../../layout/routerFuture";
import { LiveUpdatesProvider, type EventSourceLike } from "../../live/useLiveUpdates";
import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import * as lfx from "../../test/liveFixtures";
import { createTestQueryClient, renderWithProviders } from "../../test/render";
import Dashboard, { liveQuery, parseExpand, parseRange } from "../Dashboard";

const REJECTIONS = "Rejected today, and why";
const SECTIONS = ["Session", "Equity", "Risk", "Positions", "Activity", REJECTIONS, "Today", "Pending approvals"];

async function renderLive(live: LiveOut | FakeApiClient = lfx.liveOut, route = "/dashboard") {
  const api = live instanceof FakeApiClient ? live : new FakeApiClient({ live });
  const r = renderWithProviders(<Dashboard />, { api, route });
  await screen.findByRole("region", { name: "Session" });
  return r;
}

function region(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

function positionRows(): HTMLElement[] {
  const list = screen.queryByRole("list", { name: "Open positions" });
  return list ? within(list).getAllByRole("listitem").filter((li) => li.classList.contains("pos-row")) : [];
}

// ================================================================ test 1: composition and URL state

describe("Dashboard page composition (test 1)", () => {
  it("composes every section from liveOut under the heading Dashboard, asking for today", async () => {
    const { api } = await renderLive();
    expect(screen.getByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    for (const name of SECTIONS) expect(region(name), name).toBeInTheDocument();
    expect(api.callsTo("live")).toEqual([[{ range: "today" }]]);
    expect(within(region("Positions")).getByText("AAA")).toBeInTheDocument();
    expect(within(region("Activity")).getAllByRole("listitem").length).toBeGreaterThanOrEqual(lfx.activity.length);
    expect(within(region(REJECTIONS)).getByText(/rvol_below_min/)).toBeInTheDocument();
    // the page never asks for the old aggregate
    expect(api.callsTo("dashboard")).toEqual([]);
  });

  it("does not render CostBar or BooksCheck twice (TopBar embeds them) and has no separate period table", async () => {
    await renderLive();
    expect(screen.getAllByRole("region", { name: "Costs" })).toHaveLength(1);
    expect(screen.getAllByRole("region", { name: "Books" })).toHaveLength(1);
    expect(within(region("Session")).getByRole("region", { name: "Costs" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Gains by period" })).toBeNull();
  });

  it.each([0, 1, 20])("renders %i positions", async (n) => {
    await renderLive(lfx.liveWith({ positions: lfx.positionsN(n), closed_today: 2 }));
    if (n === 0) {
      expect(within(region("Positions")).getByText("No open positions")).toBeInTheDocument();
      expect(within(region("Positions")).getByText("2 trades closed today")).toBeInTheDocument();
    }
    expect(positionRows()).toHaveLength(n);
  });

  it("the non-session empty day: closed market, no positions, activity or rejections", async () => {
    await renderLive(lfx.liveEmptyDay);
    expect(within(region("Today")).getByText(/Market closed today; next session/)).toBeInTheDocument();
    expect(within(region("Positions")).getByText("No open positions")).toBeInTheDocument();
    expect(within(region("Activity")).getByText("No activity yet today")).toBeInTheDocument();
    expect(within(region(REJECTIONS)).getByText("No rejections recorded today")).toBeInTheDocument();
    expect(within(region("Equity")).getByText("No equity data for this range")).toBeInTheDocument();
  });

  it("?range=run reaches api.live; the range toggle writes the URL and refetches", async () => {
    const r = await renderLive(new FakeApiClient(), "/dashboard?range=run&proposal=12");
    expect(r.api.callsTo("live")).toEqual([[{ range: "run" }]]);
    await userEvent.click(within(region("Equity")).getByRole("button", { name: "Today" }));
    expect(r.location().search).toBe("?proposal=12");
    await waitFor(() => expect(r.api.callsTo("live").at(-1)).toEqual([{ range: "today" }]));
    await userEvent.click(within(region("Equity")).getByRole("button", { name: "Whole run" }));
    expect(r.location().search).toBe("?proposal=12&range=run");
    await waitFor(() => expect(r.api.callsTo("live").at(-1)).toEqual([{ range: "run" }]));
    // the page stays up while the new range loads
    expect(region("Session")).toBeInTheDocument();
  });

  it("?expand= reaches api.live; tapping a row writes the URL and asks for its bars", async () => {
    const api = new FakeApiClient({ live: lfx.liveWith({ positions: lfx.positionsN(4) }) });
    const r = await renderLive(api, "/dashboard?expand=101");
    expect(api.callsTo("live")).toEqual([[{ range: "today", expand: "101" }]]);
    const row = (ticker: string) => positionRows().find((li) => li.dataset.ticker === ticker)!;
    expect(within(row("BBB")).getByRole("button")).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(within(row("AAA")).getAllByRole("button")[0]!);
    expect(r.location().search).toBe("?expand=101%2C100");
    await waitFor(() => expect(api.callsTo("live").at(-1)).toEqual([{ range: "today", expand: "101,100" }]));
    await userEvent.click(within(row("BBB")).getAllByRole("button")[0]!);
    expect(r.location().search).toBe("?expand=100");
    await userEvent.click(within(row("AAA")).getAllByRole("button")[0]!);
    expect(r.location().search).toBe("");
    await waitFor(() => expect(api.callsTo("live").at(-1)).toEqual([{ range: "today" }]));
  });

  it("clamps a hand-edited ?expand= to its first 3 valid ids before calling api.live", async () => {
    const r = await renderLive(new FakeApiClient(), "/dashboard?expand=1,2,3,4,5");
    expect(r.api.callsTo("live")).toEqual([[{ range: "today", expand: "1,2,3" }]]);
    const bad = await renderLive(new FakeApiClient(), "/dashboard?expand=abc,0,-4,1e3,7,7,%3Cscript%3E,12345678901234567890");
    expect(bad.api.callsTo("live")).toEqual([[{ range: "today", expand: "7" }]]);
  });

  it("parseRange, parseExpand and liveQuery", () => {
    expect(parseRange("run")).toBe("run");
    for (const v of [null, "", "today", "RUN", "week"]) expect(parseRange(v)).toBe("today");
    expect(parseExpand(null)).toEqual([]);
    expect(parseExpand("12,15")).toEqual([12, 15]);
    expect(parseExpand("1,2,3,4")).toEqual([1, 2, 3]);
    expect(parseExpand("0,01,1.5,x,9")).toEqual([9]);
    expect(liveQuery("today", [])).toEqual({ range: "today" });
    expect(liveQuery("run", [4, 5, 6, 7])).toEqual({ range: "run", expand: "4,5,6" });
  });

  it("pending approvals: shown in manual mode (empty), hidden in auto mode with none, shown in auto mode when one waits", async () => {
    const { unmount } = await renderLive();
    expect(within(region("Pending approvals")).getByText("Nothing waiting for approval")).toBeInTheDocument();
    unmount();
    const auto = await renderLive(lfx.liveWith({ approval_mode: "auto", pending: [] }));
    expect(screen.queryByRole("region", { name: "Pending approvals" })).toBeNull();
    auto.unmount();
    await renderLive(lfx.liveWith({ approval_mode: "auto", pending: [fx.pendingProposal] }));
    expect(within(region("Pending approvals")).getByRole("article", { name: /proposal 12/i })).toBeInTheDocument();
  });

  it("the Dashboard asks nothing of the old routes and links no System page", async () => {
    await renderLive();
    for (const a of Array.from(document.querySelectorAll("a"))) expect(a.getAttribute("href") ?? "").not.toMatch(/^\/system/);
  });
});

// ================================================================ carried over from DashboardPage.test.tsx

describe("carried over: header, timeline and engine state", () => {
  it("shows the session date and phase, the MANUAL chip and the timeline in order with MT times", async () => {
    await renderLive();
    const session = region("Session");
    expect(within(session).getByRole("heading", { name: /2026-10-06/ })).toHaveTextContent("Open");
    expect(within(session).getByTestId("engine-chip")).toHaveTextContent("MANUAL · RUNNING");

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

  it("shows AUTO as a warning-toned chip when paused, and the closed-day phase label", async () => {
    await renderLive(
      lfx.liveWith({ approval_mode: "auto", trading: "paused", session: { ...lfx.liveOut.session, phase: "closed_day", is_session: false } }),
    );
    const chip = within(region("Session")).getByTestId("engine-chip");
    expect(chip).toHaveTextContent("AUTO · PAUSED");
    expect(chip).toHaveClass("status-warn");
    expect(within(region("Session")).getByText(/Market closed today/)).toBeInTheDocument();
  });

  it("marks failed and missed timeline items in the bad tone", async () => {
    const timeline = [
      { ...fx.timeline[0]!, status: "failed" as const },
      { ...fx.timeline[1]!, status: "missed" as const },
    ];
    await renderLive(lfx.liveWith({ timeline }));
    const items = within(screen.getByRole("list", { name: "Timeline" })).getAllByRole("listitem");
    expect(items[0]).toHaveClass("tone-bad");
    expect(items[0]).toHaveTextContent("failed");
    expect(items[1]).toHaveClass("tone-bad");
    expect(items[1]).toHaveTextContent("missed");
  });

  it("links a worker problem to the Control page (was: System)", async () => {
    await renderLive(lfx.liveWith({ worker_stale: true, worker: { ...lfx.liveOut.worker, ok: false, age_seconds: 95 } }));
    expect(within(region("Session")).getByTestId("heartbeat-badge")).toHaveTextContent("95 s");
    const links = screen.getAllByRole("link", { name: /check control/i });
    expect(links.length).toBeGreaterThanOrEqual(1);
    for (const link of links) expect(link).toHaveAttribute("href", "/control");
  });

  it("shows no Control link when the worker is OK", async () => {
    await renderLive();
    expect(screen.queryByTestId("heartbeat-badge")).toBeNull();
    expect(screen.queryByRole("link", { name: /check control/i })).toBeNull();
  });
});

describe("carried over: approvals", () => {
  const withPending = () => lfx.liveWith({ pending: [fx.pendingProposal] });

  it("approving refetches the live data", async () => {
    const api = new FakeApiClient({ live: withPending() });
    await renderLive(api);
    expect(api.callsTo("live")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Approved")).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("live")).toHaveLength(2));
  });

  it("keeps the decision message after the proposal leaves the pending list", async () => {
    const api = new FakeApiClient({ live: withPending() });
    api.respond("approve", () => {
      api.set("live", lfx.liveWith({ pending: [] }));
      return fx.decisionOut;
    });
    await renderLive(api);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull());
    const decisions = screen.getByRole("list", { name: "Decisions" });
    expect(decisions).toHaveTextContent("AAA");
    expect(decisions).toHaveTextContent("Approved");
  });

  it("keeps the decision message in auto mode too, after the pending list empties", async () => {
    const api = new FakeApiClient({ live: lfx.liveWith({ approval_mode: "auto", pending: [fx.pendingProposal] }) });
    api.respond("approve", () => {
      api.set("live", lfx.liveWith({ approval_mode: "auto", pending: [] }));
      return fx.decisionOut;
    });
    await renderLive(api);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull());
    expect(screen.getByRole("list", { name: "Decisions" })).toHaveTextContent("Approved");
  });

  it("?proposal=<pending id> highlights that card", async () => {
    await renderLive(withPending(), "/dashboard?proposal=12");
    const card = screen.getByRole("article", { name: /proposal 12/i });
    expect(card).toHaveClass("is-highlighted");
    expect(screen.queryByRole("region", { name: /^proposal 12$/i })).toBeNull();
  });

  it("?proposal=<decided id> shows the panel with status, decided_at in MT and decided_via", async () => {
    const api = new FakeApiClient({ live: withPending() });
    await renderLive(api, "/dashboard?proposal=11");
    const panel = await screen.findByRole("region", { name: /^proposal 11$/i });
    expect(within(panel).getByText(/submitted/)).toBeInTheDocument();
    expect(within(panel).getByText(/2026-10-05 07:36 MT/)).toBeInTheDocument();
    expect(within(panel).getByText(/via web/)).toBeInTheDocument();
    expect(within(panel).getByText(/web:stephen/)).toBeInTheDocument();
    expect(api.callsTo("proposal")).toEqual([[11]]);
    expect(screen.getByRole("article", { name: /proposal 12/i })).not.toHaveClass("is-highlighted");
  });

  it("?proposal=<decided id> in auto mode with nothing pending still shows the panel", async () => {
    const api = new FakeApiClient({ live: lfx.liveWith({ approval_mode: "auto", pending: [] }) });
    await renderLive(api, "/dashboard?proposal=11");
    expect(await screen.findByRole("region", { name: /^proposal 11$/i })).toBeInTheDocument();
  });

  it("?proposal=<unknown id> shows the server's error in the panel", async () => {
    await renderLive(new FakeApiClient(), "/dashboard?proposal=999");
    expect(await screen.findByText("Proposal 999 not found")).toBeInTheDocument();
  });

  it("ignores a non-numeric ?proposal", async () => {
    const api = new FakeApiClient();
    await renderLive(api, "/dashboard?proposal=abc");
    expect(api.callsTo("proposal")).toEqual([]);
  });

  it("shows the Telegram notice only when Telegram is not configured", async () => {
    const { unmount } = await renderLive();
    expect(screen.queryByText("Telegram is not configured: approve here")).toBeNull();
    unmount();
    await renderLive(lfx.liveWith({ telegram_configured: false }));
    expect(within(region("Pending approvals")).getByText("Telegram is not configured: approve here")).toBeInTheDocument();
  });
});

describe("carried over: positions, P&L, activity and kill switches", () => {
  it("flags a position without a working stop order, and a missing mark as stale", async () => {
    const [a, b] = lfx.positionsN(2);
    const unprotected = { ...b!, ticker: "DDD", stop_working: false, unprotected_seconds: 200, mark: null, mark_state: "missing" as const, unrealized: null };
    await renderLive(lfx.liveWith({ positions: [a!, unprotected] }));
    const notice = screen.getByTestId(`unprotected-${unprotected.id}`);
    expect(notice).toHaveTextContent("DDD: no working stop order");
    expect(notice).toHaveTextContent("unprotected 3m 20s");
    expect(notice).toHaveClass("status-bad");
    expect(within(notice).getByRole("link", { name: "Open DDD" })).toHaveAttribute("href", `/trades?position=${unprotected.id}`);
    expect(screen.queryByTestId(`unprotected-${a!.id}`)).toBeNull();
    const ddd = positionRows().find((li) => li.dataset.ticker === "DDD")!;
    expect(within(ddd).getByText("prices stale")).toBeInTheDocument();
  });

  it("shows the P&L with a partial marker", async () => {
    const periods = lfx.periods.map((p) => ({ ...p, unrealized_partial: true }));
    await renderLive(lfx.liveWith({ periods }));
    const session = region("Session");
    expect(within(session).getAllByText(/partial/).length).toBeGreaterThanOrEqual(1);
    expect(within(session).getByTestId("topbar-period-today")).toHaveTextContent("$11.00");
    expect(within(session).getByTestId("topbar-period-run")).toHaveTextContent("$19.73");
  });

  it("lists the day's activity with MT times, newest first (was: latest events)", async () => {
    await renderLive();
    const items = within(screen.getByRole("list", { name: "Activity items" })).getAllByRole("listitem");
    expect(items).toHaveLength(lfx.activity.length);
    expect(items[0]).toHaveTextContent("07:45 MT");
    expect(items[0]).toHaveTextContent("HTTP 429 from Questrade");
  });

  it("shows a tripped max_drawdown_pct with its clears text and a link to reset it on Control (was: Settings)", async () => {
    const killswitches = lfx.killswitchLights.map((k) =>
      k.switch === "max_drawdown_pct" ? { ...k, tripped: true, tripped_at: "2026-10-06T14:00:00Z", trip_value: "0.2100", trip_threshold: "0.2000" } : k,
    );
    await renderLive(lfx.liveWith({ risk: { ...lfx.riskOut, killswitches } }));
    const risk = region("Risk");
    expect(within(risk).getByRole("img", { name: "Max drawdown: tripped" })).toHaveClass("status-bad");
    expect(within(risk).getByText("reset it on the Control page with a reason")).toBeInTheDocument();
    expect(within(risk).getByRole("link", { name: /reset on control/i })).toHaveAttribute("href", "/control");
    expect(within(risk).getByRole("img", { name: "Daily loss: not tripped" })).toHaveClass("status-ok");
  });

  it("shows a manual pause in amber", async () => {
    const killswitches = lfx.killswitchLights.map((k) => (k.switch === "manual_pause" ? { ...k, tripped: true } : k));
    await renderLive(lfx.liveWith({ risk: { ...lfx.riskOut, killswitches }, trading: "paused" }));
    expect(within(region("Risk")).getByRole("img", { name: "Paused: tripped" })).toHaveClass("status-warn");
  });
});

// ================================================================ test 2: failures

describe("part isolation and request failures (test 2)", () => {
  it("one failing part shows that panel's error with Retry; every other panel renders", async () => {
    const message = "OperationalError: risk could not be read";
    const api = new FakeApiClient({ live: lfx.liveWith({ risk: null, part_errors: [{ part: "risk", message }] }) });
    await renderLive(api);
    const risk = region("Risk");
    expect(within(risk).getByText(message)).toBeInTheDocument();
    for (const name of SECTIONS.filter((s) => s !== "Risk")) expect(within(region(name)).queryByText(message), name).toBeNull();
    expect(positionRows()).toHaveLength(1);
    api.set("live", lfx.liveOut);
    await userEvent.click(within(risk).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.callsTo("live")).toHaveLength(2));
    expect(await within(region("Risk")).findByTestId("open-risk")).toBeInTheDocument();
  });

  it("every part failed: each panel shows its own error and the page frame stays", async () => {
    const api = new FakeApiClient({ live: lfx.liveAllPartsFailed });
    await renderLive(api);
    expect(screen.getByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    const byPanel: [string, string][] = [
      ["Equity", "equity"],
      ["Risk", "risk"],
      ["Positions", "positions"],
      ["Activity", "activity"],
      [REJECTIONS, "rejections"],
      ["Today", "timeline"],
      ["Pending approvals", "pending"],
      ["Session", "periods"],
      ["Session", "claude_today"],
      ["Session", "books"],
    ];
    for (const [panel, part] of byPanel) {
      expect(within(region(panel)).getByText(`OperationalError: ${part} could not be read`), part).toBeInTheDocument();
    }
    const retries = screen.getAllByRole("button", { name: "Retry" });
    expect(retries.length).toBeGreaterThanOrEqual(byPanel.length);
    await userEvent.click(retries[0]!);
    await waitFor(() => expect(api.callsTo("live")).toHaveLength(2));
  });

  it("the top bar's Retry refetches the page's live query", async () => {
    const api = new FakeApiClient({ live: lfx.liveWith({ books: null, part_errors: [{ part: "books", message: "books failed" }] }) });
    await renderLive(api);
    await userEvent.click(within(region("Session")).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.callsTo("live")).toHaveLength(2));
  });

  it("a failing api.live shows the error box with Retry (carried over); Retry loads the page", async () => {
    const api = new FakeApiClient();
    api.fail("live", new ApiError(503, "unavailable", "Database unavailable"));
    renderWithProviders(<Dashboard />, { api, route: "/dashboard" });
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(await screen.findByText("Database unavailable")).toBeInTheDocument();
    api.succeed("live");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("region", { name: "Session" })).toBeInTheDocument();
    expect(api.callsTo("live")).toHaveLength(2);
  });

  it("a failed refetch keeps the last data under the error box", async () => {
    const api = new FakeApiClient();
    const r = await renderLive(api);
    api.fail("live", new ApiError(500, "internal", "Something broke"));
    await act(async () => {
      await r.queryClient.refetchQueries();
    });
    expect(await screen.findByText("Something broke")).toBeInTheDocument();
    expect(region("Session")).toBeInTheDocument();
    expect(positionRows()).toHaveLength(1);
  });

  it("shows the empty states with no pending proposals and no positions (carried over)", async () => {
    await renderLive(lfx.liveWith({ pending: [], positions: [] }));
    expect(screen.getByText("Nothing waiting for approval")).toBeInTheDocument();
    expect(screen.getByText("No open positions")).toBeInTheDocument();
  });
});

// ================================================================ test 6: the live indicator

class HelloSource implements EventSourceLike {
  static last: HelloSource | null = null;
  readyState = 0;
  onerror: ((ev: Event) => void) | null = null;
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>();
  constructor() {
    HelloSource.last = this;
  }
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  close(): void {
    this.readyState = 2;
  }
  emit(type: string): void {
    for (const l of this.listeners.get(type) ?? []) l(new MessageEvent(type, { data: "{}" }));
  }
  fail(): void {
    this.readyState = 0;
    this.onerror?.(new Event("error"));
  }
}

function renderInProvider(ui: ReactElement) {
  const api = new FakeApiClient();
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <ApiProvider client={api}>
        <MemoryRouter initialEntries={["/dashboard"]} future={ROUTER_FUTURE}>
          <LiveUpdatesProvider createEventSource={() => new HelloSource()}>{ui}</LiveUpdatesProvider>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>,
  );
}

describe("the live indicator follows the stream (test 6)", () => {
  it("disconnected: Degraded: polling every 15 s; after hello: Live; the stream lost: degraded again", async () => {
    renderInProvider(<Dashboard />);
    const indicator = await screen.findByTestId("live-indicator");
    expect(indicator).toHaveTextContent("Degraded: polling every 15 s");
    act(() => HelloSource.last!.emit("hello"));
    expect(screen.getByTestId("live-indicator")).toHaveTextContent("Live");
    act(() => HelloSource.last!.fail());
    expect(screen.getByTestId("live-indicator")).toHaveTextContent("Degraded: polling every 15 s");
  });

  it("outside a provider the page reads as degraded", async () => {
    await renderLive();
    expect(screen.getByTestId("live-indicator")).toHaveTextContent("Degraded: polling every 15 s");
  });
});
