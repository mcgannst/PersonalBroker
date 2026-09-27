// P4 web gauntlet, attempt 1 (Breaker): tests aimed at the weak points of the web pages (P4-T2, T13, T14, T15,
// T16) and the web shell (P4-T12). FakeApiClient, fixtures and a mocked `fetch` only: no network.
import type { QueryClient } from "@tanstack/react-query";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import userEvent from "@testing-library/user-event";
import { createElement, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { AppRoutes } from "../App";
import { ApiError, ApiProvider } from "../api/client";
import { createHttpClient } from "../api/http";
import { qk } from "../api/queryKeys";
import type {
  CandidateOut,
  CatalystOut,
  DashboardOut,
  DecisionOut,
  KillSwitchesOut,
  PnlOut,
  PositionDetailOut,
  SessionPhase,
  SettingOut,
  SystemOut,
  TokenOut,
  WorkerOut,
} from "../api/types";
import { ErrorBox } from "../components/ui";
import { notifyUnauthorized } from "../layout/AuthContext";
import { safeNext } from "../layout/safeNext";
import { fmtDateTime, fmtMoney, fmtPct, fmtPrice, fmtR, fmtTime } from "../lib/format";
import { DISCONNECTED_REFETCH_MS, LiveUpdatesProvider, REOPEN_DELAY_MS, type EventSourceLike } from "../live/useLiveUpdates";
import CandidatesPage from "../pages/Candidates";
import DashboardPage from "../pages/Dashboard";
import PendingProposal from "../pages/dashboard/PendingProposal";
import PnlTiles from "../pages/dashboard/PnlTiles";
import PositionCard from "../pages/dashboard/PositionCard";
import JournalPage from "../pages/Journal";
import PerformancePage from "../pages/Performance";
import ReportsPage from "../pages/Reports";
import SettingsPage from "../pages/Settings";
import { ApprovalMode } from "../pages/settings/ApprovalMode";
import { KillSwitchPanel } from "../pages/settings/KillSwitchPanel";
import { QuestradeToken } from "../pages/settings/QuestradeToken";
import { Security } from "../pages/settings/Security";
import SystemPage from "../pages/System";
import TradesPage from "../pages/Trades";
import { FakeApiClient, type FakeResponses } from "../test/fakeApi";
import * as fx from "../test/fixtures";
import { createTestQueryClient, renderWithProviders } from "../test/render";

// ---------------------------------------------------------------- helpers

/** A payload that would create elements, handlers or a javascript: link if it were ever parsed as HTML. */
const XSS = `<img src=x onerror="alert(1)"><script>alert(1)</script><a href="javascript:alert(1)">x</a><svg onload=alert(1)>`;
const tagged = (label: string): string => `${label}${XSS}`;
const UNSAFE_SCHEME = /^(javascript|data|vbscript):/i;
const URL_ATTRS = new Set(["href", "src", "action", "formaction", "xlink:href", "poster", "background"]);

/** No injected markup anywhere: no script/img/iframe elements, no on* handlers, no dangerous URL schemes. */
function expectInert(root: ParentNode = document.body): void {
  expect(root.querySelectorAll("script, iframe, object, embed, img, svg[onload]")).toHaveLength(0);
  for (const el of Array.from(root.querySelectorAll("*"))) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      expect(name.startsWith("on"), `<${el.tagName.toLowerCase()}> has a ${name} attribute`).toBe(false);
      if (URL_ATTRS.has(name)) {
        // Browsers ignore ASCII whitespace and control characters inside a scheme.
        expect(attr.value.replace(/[\u0000- ]/g, "")).not.toMatch(UNSAFE_SCHEME);
      }
    }
  }
}

function bodyText(): string {
  return document.body.textContent ?? "";
}

/** Waits until no Loading spinner is left on the page. */
async function settle(): Promise<void> {
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "Loading" })).toHaveLength(0));
}

function fixtureSkew(): number {
  return Date.parse(fx.SERVER_TIME) - Date.now();
}

/** Everything the query client holds (queries and mutations), as text. */
function cacheText(queryClient: QueryClient): string {
  return JSON.stringify({
    mutations: queryClient
      .getMutationCache()
      .getAll()
      .map((m) => ({ variables: m.state.variables ?? null, data: m.state.data ?? null, context: m.state.context ?? null })),
    queries: queryClient
      .getQueryCache()
      .getAll()
      .map((q) => q.state.data ?? null),
  });
}

/** A file of the web project read from disk (the tests run with Trader/web or the repository root as cwd). */
function readWebFile(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return readFileSync(found, "utf8");
}

const nullToken: TokenOut = { ok: false, seeded: false, age_hours: null, expires_at: null, last_refresh_at: null, error: null };
const nullWorker: WorkerOut = { ok: false, phase: null, beat_at: null, age_seconds: null, session_date: null, pid: null, host: null, detail: null };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function errorResponse(status: number, code: string, message: string): Response {
  return json({ error: { code, message, fields: null, request_id: "req-1" } }, status);
}

type FetchArgs = [string, RequestInit];

function headerOf(init: RequestInit | undefined, name: string): string | null {
  return new Headers(init?.headers).get(name);
}

/** A controllable EventSource for the live-update tests. */
class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  readyState = 0;
  closed = false;
  onerror: ((ev: Event) => void) | null = null;
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  emitRaw(type: string, data: string): void {
    if (type === "hello") this.readyState = 1;
    const ev = new MessageEvent(type, { data });
    for (const l of this.listeners.get(type) ?? []) l(ev);
  }

  emit(type: string, data: unknown): void {
    this.emitRaw(type, JSON.stringify(data));
  }

  fail(readyState: number): void {
    this.readyState = readyState;
    this.onerror?.(new Event("error"));
  }

  static open(): FakeEventSource[] {
    return FakeEventSource.instances.filter((e) => !e.closed);
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

// ================================================================ pages (P4-T13 to P4-T16)

describe("XSS: every server string renders as text", () => {
  it("Dashboard: reasons, strategy names, events, kill-switch text, timeline, decision messages and error boxes", async () => {
    const dash: DashboardOut = {
      ...fx.dashboardOut,
      telegram_configured: false,
      session: { ...fx.dashboardOut.session, phase: tagged("phase") as SessionPhase },
      timeline: [{ ...fx.timeline[0]!, label: tagged("timeline"), detail: tagged("detail") }],
      pending: [{ ...fx.pendingProposal, reason: tagged("reason"), strategy_key: tagged("strategy"), order_type: tagged("ordertype") }],
      positions: [{ ...fx.openPosition, ticker: tagged("ticker"), strategy_key: tagged("pos-strategy") }],
      killswitches: [{ ...fx.killswitchDrawdownTripped, label: tagged("ks-label"), clears: tagged("clears") }],
      events: [{ ...fx.events[0]!, level: tagged("level"), source: tagged("source"), message: tagged("event") }],
      candidates_top: [{ ...fx.candidatesRanking[0]!, ticker: tagged("cand") }],
    };
    const api = new FakeApiClient({ dashboard: dash, approve: { ...fx.decisionOut, message: tagged("decision") } });
    renderWithProviders(<DashboardPage />, { api, route: "/dashboard" });
    await screen.findByText(tagged("reason"));
    for (const label of ["phase", "timeline", "detail", "strategy", "ordertype", "ticker", "pos-strategy", "ks-label", "clears", "level", "source", "event", "cand"]) {
      expect(bodyText()).toContain(tagged(label));
    }
    expectInert();

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(tagged("decision"))).toBeInTheDocument();
    expectInert();

    cleanup();
    const failing = new FakeApiClient().fail("dashboard", new ApiError(500, "internal", tagged("boom")));
    renderWithProviders(<DashboardPage />, { api: failing, route: "/dashboard" });
    expect(await screen.findByRole("alert")).toHaveTextContent(tagged("boom"));
    expectInert();
  });

  it("Candidates: only http(s) headline URLs become links; javascript:, data:, vbscript: and tricks stay text", async () => {
    const urls = [
      "javascript:alert(1)",
      "JaVaScRiPt:alert(1)",
      " javascript:alert(1)",
      "java\tscript:alert(1)",
      "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
      "vbscript:msgbox(1)",
      "//evil.example/x",
      "https://news.example/ok",
      "HTTP://news.example/upper",
    ];
    const headlines = urls.map((url, i) => ({ ts: null, title: tagged(`h${i}`), source: tagged(`src${i}`), url }));
    const base = fx.catalysts[0]!;
    const catalysts: CatalystOut[] = [
      { ...base, headlines: headlines.slice(0, 5), reason: tagged("cat-reason"), catalyst_type: tagged("type"), direction: tagged("dir") },
      { ...base, symbol_id: 999, ticker: "ZZZ", headlines: headlines.slice(5), reason: null },
    ];
    const ranking: CandidateOut[] = [{ ...fx.candidatesRanking[1]!, reject_reason: tagged("reject"), strategy_key: tagged("rank-strategy") }];
    const api = new FakeApiClient({ candidates: { ...fx.candidatesOut, brief: tagged("brief"), catalysts, ranking } });
    renderWithProviders(<CandidatesPage />, { api, route: "/candidates" });
    await screen.findByText(tagged("h0"));
    for (const label of ["brief", "cat-reason", "type", "reject", "rank-strategy"]) expect(bodyText()).toContain(tagged(label));

    const region = screen.getByRole("region", { name: "Catalysts" });
    const links = within(region).queryAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://news.example/ok", "HTTP://news.example/upper"]);
    for (const a of links) {
      expect(a).toHaveAttribute("target", "_blank");
      expect(a.getAttribute("rel")?.split(/\s+/)).toEqual(expect.arrayContaining(["noopener", "noreferrer"]));
    }
    for (const i of [0, 1, 2, 3, 4, 5, 6]) expect(screen.getByText(tagged(`h${i}`)).closest("a")).toBeNull();
    expectInert();
  });

  it("Trades, Journal, Reports, Settings and System: notes, evidence, errors, strategy names, filenames and messages", async () => {
    const detail: PositionDetailOut = {
      ...fx.positionDetail,
      signal: { ...fx.signal, event_key: tagged("event-key"), evidence: { [tagged("ev-key")]: tagged("ev-value"), nested: { k: tagged("ev-nested") } } },
      proposals: [{ ...fx.decidedProposal, error: tagged("prop-error"), status: tagged("prop-status") }],
      orders: [{ ...fx.orders[1]!, cancel_reason: tagged("cancel"), purpose: tagged("purpose") }],
      fills: [{ ...fx.fills[0]!, fees: { [tagged("fee")]: "0.10" }, quote_snapshot: { bid: "1.00", last_trade_time: tagged("qt") } }],
      trade: { ...fx.trade, exit_reason: tagged("exit") },
      chart_error: tagged("chart"),
    };
    const journalDays = fx.journalDays.map((d, i) => ({ ...d, notes: tagged(`note${i}`), answered_via: tagged(`via${i}`) }));
    const settingsItems: SettingOut[] = fx.settingsItems.map((s) => ({
      ...s,
      group: tagged(`group-${s.group}`),
      updated_by: tagged("updated-by"),
      field: { ...s.field, title: tagged(`title-${s.key}`), description: tagged(`desc-${s.key}`) },
    }));
    const killswitches: KillSwitchesOut = {
      switches: [{ ...fx.killswitchDrawdownTripped, label: tagged("ks"), clears: tagged("ks-clears") }],
      history: [{ ...fx.killswitchEvents[0]!, reset_reason: tagged("reset-reason"), reset_by: tagged("reset-by") }],
    };
    const system: SystemOut = {
      ...fx.systemOut,
      version: tagged("version"),
      token: { ...fx.tokenOut, ok: false, error: tagged("token-error") },
      worker: { ...fx.workerOut, phase: tagged("worker-phase"), host: tagged("host") },
      rate_limit: { [tagged("rate")]: 3 },
      last_runs: [{ ...fx.jobRuns[3]!, job: tagged("job"), error: tagged("job-error") }],
      errors: [{ ...fx.errorEvents[0]!, message: tagged("error-event") }],
      notifications_failed: [{ ...fx.notificationsFailed[0]!, kind: tagged("kind"), error: tagged("send-error") }],
    };
    const api = new FakeApiClient({
      position: detail,
      trades: { items: [{ ...fx.trade, ticker: tagged("trade-ticker"), exit_reason: tagged("trade-exit") }] },
      journal: { items: journalDays },
      settings: { items: settingsItems },
      strategies: { items: [{ ...fx.orbSipStrategy, key: tagged("strategy-key"), updated_by: tagged("strat-by") }] },
      killswitches,
      me: { ...fx.sessionOut, user: { username: tagged("user"), totp_enabled: false } },
      telegramTest: { sent: false, message: tagged("tg") },
      system,
      events: { items: [{ ...fx.events[0]!, message: tagged("log-event"), source: tagged("log-source") }] },
      watchlist: { ...fx.watchlistOut, filename: tagged("file"), uploaded_by: tagged("uploader"), tickers: [tagged("tick")] },
    });

    const pages: [ReactElement, string, string[]][] = [
      [<TradesPage />, "/trades?position=3", ["event-key", "ev-key", "ev-value", "ev-nested", "prop-error", "prop-status", "cancel", "purpose", "fee", "exit"]],
      [<TradesPage />, "/trades", ["trade-ticker", "trade-exit"]],
      [<JournalPage />, "/journal", ["note0", "via0", "note1"]],
      [<ReportsPage />, "/reports?week=2026-10-09", ["note0", "trade-exit"]],
      [<SettingsPage />, "/settings", ["strategy-key", "strat-by", "ks", "ks-clears", "reset-reason", "reset-by", "user", "updated-by", "desc-risk_pct", "title-risk_pct"]],
      [<SystemPage />, "/system", ["version", "token-error", "worker-phase", "host", "rate", "job", "job-error", "error-event", "kind", "send-error", "log-event", "log-source", "file", "uploader", "tick"]],
    ];
    for (const [ui, route, labels] of pages) {
      renderWithProviders(ui, { api, route });
      await settle();
      for (const label of labels) expect(bodyText(), `${route} shows ${label}`).toContain(tagged(label));
      expectInert();
      cleanup();
    }

    // The Telegram test message and a server error message (Settings) are text too.
    renderWithProviders(<SettingsPage />, { api, route: "/settings" });
    await settle();
    fireEvent.click(within(document.getElementById("telegram")!).getByRole("button", { name: "Send a test message" }));
    expect(await screen.findByText(tagged("tg"))).toBeInTheDocument();
    expectInert();
  });

  it("two-step setup never turns a non-otpauth URI into a link, and shows the secret as text", async () => {
    const api = new FakeApiClient({ totpSetup: { secret: tagged("secret"), otpauth_uri: "javascript:alert(document.cookie)" } });
    renderWithProviders(<Security />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Set up two-step" }));
    await userEvent.type(screen.getByLabelText("Password"), "my-password");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText(tagged("secret"))).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /authenticator/i })).toBeNull();
    expectInert();
  });

  it("no raw-HTML sink (dangerouslySetInnerHTML, innerHTML, outerHTML, insertAdjacentHTML, document.write) in any web source", () => {
    const sources = import.meta.glob<string>(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}"], { query: "?raw", import: "default", eager: true });
    const files = Object.entries(sources);
    expect(files.length).toBeGreaterThan(40);
    const sink = /dangerouslySetInnerHTML|\.innerHTML\s*=|\bouterHTML\b|insertAdjacentHTML|document\.write\(/;
    const offenders = files.filter(([, text]) => sink.test(text)).map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});

describe("decisions", () => {
  it("double-tap race: approve and reject tapped together send exactly one decision", async () => {
    const api = new FakeApiClient();
    let release: (d: DecisionOut) => void = () => undefined;
    api.respond("approve", () => new Promise<DecisionOut>((resolve) => (release = resolve)));
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={fixtureSkew()} />, { api });
    const approve = screen.getByRole("button", { name: "Approve" });
    const reject = screen.getByRole("button", { name: "Reject" });

    // All four taps land before React re-renders (one batch), so only the in-flight guard can stop them.
    act(() => {
      approve.click();
      approve.click();
      reject.click();
      reject.click();
    });
    expect(api.callsTo("approve")).toEqual([[12]]);
    expect(api.callsTo("reject")).toEqual([]);

    await act(async () => release(fx.decisionOut));
    expect(await screen.findByText("Approved")).toBeInTheDocument();
    act(() => {
      approve.click();
      reject.click();
    });
    expect(api.calls.filter(([m]) => m === "approve" || m === "reject")).toHaveLength(1);
  });

  it("a Telegram tap that won the race shows already-decided and keeps both buttons locked", async () => {
    const api = new FakeApiClient({
      reject: {
        proposal: { ...fx.pendingProposal, status: "submitted", decided_via: "telegram", decided_by: "telegram:1" },
        already_decided: true,
        blocked: null,
        message: "Already approved on Telegram",
      },
    });
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={fixtureSkew()} />, { api });
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    const msg = await screen.findByText("Already approved on Telegram");
    expect(msg).toHaveClass("tone-warn");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });

  it("approve after expiry: the countdown locks the buttons, an already-expired card starts locked, and a stale page shows the server's answer", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-06T14:04:08Z"));
    const api = new FakeApiClient();
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={0} />, { api });
    expect(screen.getByText("Expires in 2s")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByText("Expired, waiting for the server")).toBeInTheDocument();
    act(() => {
      screen.getByRole("button", { name: "Approve" }).click();
    });
    expect(api.callsTo("approve")).toEqual([]);
    cleanup();

    // Already expired when the card mounts (server clock 14:05, expiry 14:04:10).
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={Date.parse("2026-10-06T14:05:00Z") - Date.now()} />, { api });
    expect(screen.getByText("Expired, waiting for the server")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    cleanup();
    vi.useRealTimers();

    // A stale page (the browser thinks 4 minutes are left) approves; the server says it already expired.
    const stale = new FakeApiClient({
      approve: { proposal: { ...fx.pendingProposal, status: "expired" }, already_decided: true, blocked: null, message: "Already expired" },
    });
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={fixtureSkew()} />, { api: stale });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Already expired")).toBeInTheDocument();
    expect(stale.callsTo("approve")).toEqual([[12]]);
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("an approval blocked by the kill switch keeps showing the reason after the dashboard refetches", async () => {
    const api = new FakeApiClient({ approve: fx.decisionBlocked });
    let reads = 0;
    api.respond("dashboard", () => (reads++ === 0 ? fx.dashboardOut : { ...fx.dashboardOut, pending: [] }));
    renderWithProviders(<DashboardPage />, { api, route: "/dashboard" });
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(api.callsTo("dashboard").length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(screen.queryByRole("article", { name: /proposal 12/i })).toBeNull());
    const notices = screen.getByRole("list", { name: "Decisions" });
    expect(notices).toHaveTextContent("AAA ENTRY: Entry blocked: kill switch manual_pause is tripped");
    expect(api.callsTo("approve")).toEqual([[12]]);
  });
});

describe("settings safety", () => {
  it("secrets never stay behind after submit: token and passwords are gone from the DOM, the query client and the console", async () => {
    const TOKEN = "QT-refresh-7d9c1e2f-SECRET";
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    const consoleText = () => JSON.stringify(spies.map((s) => s.mock.calls));

    // Questrade token, success.
    const api = new FakeApiClient();
    const r1 = renderWithProviders(<QuestradeToken />, { api });
    const input = screen.getByLabelText("Refresh token") as HTMLInputElement;
    expect(input).toHaveAttribute("type", "password");
    await userEvent.click(input);
    await userEvent.paste(TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    await screen.findByRole("status");
    expect(api.callsTo("putQuestradeToken")).toEqual([[TOKEN]]);
    expect(input.value).toBe("");
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(consoleText()).not.toContain(TOKEN);
    expect.soft(cacheText(r1.queryClient), "the token is kept in the query client (mutation variables)").not.toContain(TOKEN);
    cleanup();

    // Questrade token, server error.
    const failing = new FakeApiClient().fail("putQuestradeToken", new ApiError(422, "validation", "The refresh token was rejected"));
    const r2 = renderWithProviders(<QuestradeToken />, { api: failing });
    await userEvent.click(screen.getByLabelText("Refresh token"));
    await userEvent.paste(TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The refresh token was rejected");
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect.soft(cacheText(r2.queryClient), "the token is kept after an error (mutation variables)").not.toContain(TOKEN);
    cleanup();

    // Password change.
    const OLD = "Old-Passw0rd-XYZ";
    const NEW = "New-Passw0rd-QRS";
    const r3 = renderWithProviders(<Security />, { api: new FakeApiClient() });
    await userEvent.type(await screen.findByLabelText("Current password"), OLD);
    await userEvent.type(screen.getByLabelText("New password"), NEW);
    await userEvent.type(screen.getByLabelText("New password again"), NEW);
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));
    await screen.findByText(/Password changed/);
    expect(document.body.innerHTML).not.toContain(OLD);
    expect(document.body.innerHTML).not.toContain(NEW);
    expect(consoleText()).not.toContain(NEW);
    expect.soft(cacheText(r3.queryClient), "passwords are kept in the query client (mutation variables)").not.toMatch(new RegExp(`${OLD}|${NEW}`));
  });

  it("password change: 7 characters or a mismatch sends nothing, exactly 8 is sent", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<Security />, { api });
    const user = userEvent.setup();
    const current = await screen.findByLabelText("Current password");
    const next = screen.getByLabelText("New password");
    const again = screen.getByLabelText("New password again");
    const submit = screen.getByRole("button", { name: "Change password" });

    await user.type(current, "current-pass");
    await user.type(next, "abcdefg");
    await user.type(again, "abcdefg");
    await user.click(submit);
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 8 characters");

    await user.clear(next);
    await user.clear(again);
    await user.type(next, "abcdefgh");
    await user.type(again, "abcdefgX");
    await user.click(submit);
    expect(await screen.findByRole("alert")).toHaveTextContent("don't match");
    expect(api.callsTo("changePassword")).toEqual([]);

    await user.clear(again);
    await user.type(again, "abcdefgh");
    await user.click(submit);
    await waitFor(() => expect(api.callsTo("changePassword")).toEqual([[{ current_password: "current-pass", new_password: "abcdefgh" }]]));
  });

  it("kill-switch reset needs a 3-500 character reason (trimmed), then a confirm step", async () => {
    const tripped: KillSwitchesOut = {
      switches: fx.killswitchStates.map((s) => (s.switch === "max_drawdown_pct" ? fx.killswitchDrawdownTripped : s)),
      history: [],
    };
    const api = new FakeApiClient({ killswitches: tripped });
    renderWithProviders(<KillSwitchPanel />, { api });
    fireEvent.click(await screen.findByRole("button", { name: "Reset Max drawdown" }));
    const reason = screen.getByLabelText("Reason for the reset");
    const next = () => screen.getByRole("button", { name: "Reset…" });
    const cases: [string, boolean][] = [
      ["", false],
      ["ab", false],
      ["  ab  ", false],
      ["\n\t ab \t\n", false],
      ["abc", true],
      ["x".repeat(500), true],
      [`  ${"x".repeat(500)}  `, true],
      ["x".repeat(501), false],
    ];
    for (const [value, enabled] of cases) {
      fireEvent.change(reason, { target: { value } });
      expect(next().hasAttribute("disabled"), `reason of ${value.length} chars`).toBe(!enabled);
    }
    fireEvent.change(reason, { target: { value: "   reviewed the drawdown   " } });
    fireEvent.click(next());
    expect(api.callsTo("resetKillSwitch")).toEqual([]);
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Confirm reset" }));
    await waitFor(() => expect(api.callsTo("resetKillSwitch")).toEqual([["max_drawdown_pct", { reason: "reviewed the drawdown" }]]));
  });

  it("Auto mode asks first (exact wording), Cancel sends nothing, a server error leaves Manual, Manual needs no confirmation", async () => {
    const api = new FakeApiClient();
    api.respond("putSetting", (key, value) => ({ ...fx.settingsItems[0]!, key, value }));
    renderWithProviders(<ApprovalMode />, { api });
    expect(await screen.findByText("MANUAL")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Auto" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("Every order will be placed without asking you. Continue?");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.callsTo("putSetting")).toEqual([]);

    api.fail("putSetting", new ApiError(422, "validation", "Auto mode is not allowed now"));
    fireEvent.click(screen.getByRole("button", { name: "Auto" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Switch to Auto" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Auto mode is not allowed now");
    expect(screen.getByText("MANUAL")).toBeInTheDocument();
    api.succeed("putSetting");

    fireEvent.click(screen.getByRole("button", { name: "Auto" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Switch to Auto" }));
    expect(await screen.findByText("AUTO")).toBeInTheDocument();
    expect(api.callsTo("putSetting")).toEqual([
      ["approval_mode", "auto"],
      ["approval_mode", "auto"],
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Manual" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    await waitFor(() => expect(api.callsTo("putSetting").at(-1)).toEqual(["approval_mode", "manual"]));
  });
});

describe("money and times", () => {
  it("Decimal strings are never rounded through a float (large, negative, 4 dp) and MT times use the fixed offset across DST dates", () => {
    // 17 significant digits: a float would print ...567.90 as ...568.00.
    expect(fmtMoney("12345678901234567.8950")).toBe("$12,345,678,901,234,567.90");
    expect(fmtMoney("-98765432109876.5449")).toBe("-$98,765,432,109,876.54");
    expect(fmtMoney("-0.005")).toBe("-$0.01");
    expect(fmtMoney("-0.004")).toBe("$0.00");
    expect(fmtMoney("0.0050")).toBe("$0.01");
    expect(fmtMoney("1.005")).toBe("$1.01"); // 1.005 is 1.00499999... as a float
    expect(fmtMoney("abc")).toBe("n/a");
    expect(fmtMoney("")).toBe("n/a");
    expect(fmtPrice("0.0001")).toBe("0.0001");
    expect(fmtPrice("-0.0050")).toBe("-0.005");
    expect(fmtPrice("123456789012.3456")).toBe("123456789012.3456");
    expect(fmtPrice("9007199254740993.1250")).toBe("9007199254740993.125");
    expect(fmtR("-0.005")).toBe("-0.01R");
    expect(fmtPct("-0.00005")).toBe("-0.01%");
    expect(fmtPct("1.23456789")).toBe("+123.46%");

    // Fixed -360 mode (test setup): no DST jumps on the US change dates, midnight crossings stay right.
    expect(fmtDateTime("2026-11-01T07:30:00Z")).toBe("2026-11-01 01:30 MT");
    expect(fmtDateTime("2026-11-01T08:30:00Z")).toBe("2026-11-01 02:30 MT");
    expect(fmtDateTime("2027-03-14T08:59:00Z")).toBe("2027-03-14 02:59 MT");
    expect(fmtDateTime("2027-03-14T09:00:00Z")).toBe("2027-03-14 03:00 MT");
    expect(fmtDateTime("2026-10-07T05:59:59Z")).toBe("2026-10-06 23:59 MT");
    expect(fmtTime("2026-10-06T07:35:05-06:00")).toBe("07:35 MT");
    expect(fmtTime("not a time")).toBe("n/a");

    render(
      <PnlTiles
        pnl={{
          ...fx.pnlOut,
          realized_today: "-98765432109876.5449",
          equity: "12345678901234567.8950",
          peak_equity: "12345678901234567.8950",
          drawdown_pct: "0.00005",
        } satisfies PnlOut}
      />,
    );
    expect(screen.getByText("-$98,765,432,109,876.54")).toBeInTheDocument();
    expect(screen.getByText("$12,345,678,901,234,567.90")).toBeInTheDocument();
    expect(screen.getByText("0.01%")).toBeInTheDocument();
    cleanup();

    renderWithProviders(<PositionCard position={{ ...fx.openPosition, entry: "123456789.1234", last: "0.0001", unrealized_pnl: "-0.0050" }} />);
    expect(screen.getByText("Entry 123456789.1234")).toBeInTheDocument();
    expect(screen.getByText("Last 0.0001")).toBeInTheDocument();
    expect(screen.getByText("-$0.01")).toBeInTheDocument();
  });
});

describe("empty and null data", () => {
  it("every page renders empty lists and null fields without an error", async () => {
    const emptyDetail: PositionDetailOut = {
      position: { ...fx.closedPosition, stop: null, last: null, unrealized_pnl: null, closed_at: null },
      trade: null,
      signal: null,
      proposals: [],
      orders: [],
      fills: [],
      candles: [],
      chart_error: null,
    };
    const nullCatalyst: CatalystOut = {
      ...fx.catalysts[0]!,
      quality: null,
      confirmed: null,
      reason: null,
      gap_pct: null,
      earnings_date: null,
      headlines: [],
      model: null,
      classified_at: null,
    };
    const nullCandidate: CandidateOut = { ...fx.candidatesRanking[2]!, rvol: null, rank: null, reject_reason: null, candle: null, data: null };
    const empties: Partial<FakeResponses> = {
      dashboard: {
        ...fx.dashboardOut,
        session: { ...fx.dashboardOut.session, phase: "closed_day", is_session: false, open_at: null, close_at: null },
        timeline: [],
        pending: [],
        positions: [],
        killswitches: [],
        events: [],
        candidates_top: [],
        candidates_count: 0,
        pnl: { ...fx.pnlOut, unrealized: null },
        token: nullToken,
        worker: nullWorker,
      },
      candidates: { session_date: fx.SESSION_DATE, brief: null, catalysts: [nullCatalyst], ranking: [nullCandidate] },
      trades: { items: [] },
      position: emptyDetail,
      metrics: fx.emptyMetrics,
      equity: { run_id: fx.RUN_ID, points: [] },
      journal: { items: [] },
      settings: { items: [] },
      strategies: { items: [] },
      killswitches: { switches: [], history: [] },
      system: {
        ...fx.systemOut,
        rate_limit: null,
        last_runs: [],
        errors: [],
        notifications_failed: [],
        manual_jobs: [],
        alembic_revision: null,
        tz_iana_version: null,
        token: nullToken,
        worker: nullWorker,
      },
      events: { items: [] },
      watchlist: null,
    };
    const pages: [ReactElement, string, string[]][] = [
      [<DashboardPage />, "/dashboard", ["Nothing waiting for approval", "No open positions", "No events yet", "No schedule for this day"]],
      [<CandidatesPage />, "/candidates", ["No brief for this session", "Quality n/a", "Gap n/a"]],
      [<TradesPage />, "/trades", ["No trades yet"]],
      [<TradesPage />, "/trades?position=3", ["Chart unavailable", "No signal recorded.", "No proposals.", "No orders.", "No fills."]],
      [<PerformancePage />, "/performance", ["No trades yet"]],
      [<JournalPage />, "/journal", ["No session days yet."]],
      [<JournalPage />, "/journal?date=2026-10-06", ["Journal for 2026-10-06"]],
      [<ReportsPage />, "/reports?week=2026-10-09", ["No trades yet", "No trades this week."]],
      [<SettingsPage />, "/settings", ["Approval mode", "Kill switches"]],
      [<SystemPage />, "/system", ["not reported yet", "No job runs.", "No errors.", "No failed sends.", "No events.", "No uploaded watchlist"]],
    ];
    for (const [ui, route, texts] of pages) {
      renderWithProviders(ui, { api: new FakeApiClient(empties), route });
      await settle();
      for (const t of texts) expect(bodyText(), `${route} shows "${t}"`).toContain(t);
      expect(bodyText(), `${route} shows no generic error`).not.toContain("Something went wrong");
      cleanup();
    }
  });
});

describe("long lists and a 390 px phone", () => {
  it("very long lists stay bounded or scroll inside tables; every page has 44 px buttons, names and labels, and nothing wider than the phone", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));
    const manyEvents = Array.from({ length: 500 }, (_, i) => ({ ...fx.events[0]!, id: 10_000 - i, message: `event ${i}` }));
    const manyTimeline = Array.from({ length: 300 }, (_, i) => ({ ...fx.timeline[5]!, key: `k${i}`, label: `item ${i}` }));
    const manyRanking = Array.from({ length: 1000 }, (_, i) => ({ ...fx.candidatesRanking[1]!, id: 5000 + i, rank: 1000 - i, ticker: `T${1000 - i}` }));
    const fiftyTrades = Array.from({ length: 50 }, (_, i) => ({ ...fx.trade, id: 100 + i, position_id: 200 + i }));
    const api = new FakeApiClient({
      dashboard: { ...fx.dashboardOut, events: manyEvents, timeline: manyTimeline, positions: [fx.openPosition, fx.unprotectedPosition] },
      candidates: { ...fx.candidatesOut, ranking: manyRanking },
      trades: { items: fiftyTrades },
      events: { items: manyEvents.slice(0, 200) },
      system: { ...fx.systemOut, last_runs: Array.from({ length: 300 }, (_, i) => ({ ...fx.jobRuns[0]!, id: i + 1 })) },
    });
    const pages: [ReactElement, string][] = [
      [<DashboardPage />, "/dashboard"],
      [<CandidatesPage />, "/candidates"],
      [<TradesPage />, "/trades"],
      [<TradesPage />, "/trades?position=3"],
      [<PerformancePage />, "/performance"],
      [<JournalPage />, "/journal?date=2026-10-05"],
      [<ReportsPage />, "/reports?week=2026-10-09"],
      [<SettingsPage />, "/settings"],
      [<SystemPage />, "/system"],
    ];
    for (const [ui, route] of pages) {
      renderWithProviders(ui, { api, route });
      await settle();
      for (const table of Array.from(document.querySelectorAll("table"))) {
        expect(table.parentElement?.classList.contains("table-scroll"), `${route}: a table outside .table-scroll`).toBe(true);
      }
      for (const el of Array.from(document.querySelectorAll<HTMLElement>("[style]"))) {
        for (const prop of ["width", "minWidth"] as const) {
          const px = /^(\d+(?:\.\d+)?)px$/.exec(el.style[prop]);
          if (px) expect(Number(px[1]), `${route}: ${el.tagName} ${prop} ${el.style[prop]}`).toBeLessThanOrEqual(390);
        }
      }
      for (const svg of Array.from(document.querySelectorAll("svg[width]"))) {
        expect(Number(svg.getAttribute("width")), `${route}: chart wider than the phone`).toBeLessThanOrEqual(390);
      }
      for (const button of Array.from(document.querySelectorAll<HTMLButtonElement>("button"))) {
        const name = (button.getAttribute("aria-label") ?? button.textContent ?? "").trim();
        expect(name, `${route}: a button without a name`).not.toBe("");
        expect(parseFloat(button.style.minHeight || "0"), `${route}: button "${name}" under 44 px`).toBeGreaterThanOrEqual(44);
      }
      for (const control of Array.from(document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>("input, select, textarea"))) {
        if (control instanceof HTMLInputElement && control.type === "hidden") continue;
        const labelled = (control.labels?.length ?? 0) > 0 || control.hasAttribute("aria-label") || control.hasAttribute("aria-labelledby");
        expect(labelled, `${route}: an unlabelled <${control.tagName.toLowerCase()} id=${control.id}>`).toBe(true);
      }
      for (const a of Array.from(document.querySelectorAll("a"))) {
        expect((a.textContent ?? "").trim() || a.getAttribute("aria-label"), `${route}: a link without a name`).toBeTruthy();
      }
      if (route === "/dashboard") {
        expect(within(screen.getByRole("list", { name: "Events" })).getAllByRole("listitem")).toHaveLength(20);
        expect(within(screen.getByRole("list", { name: "Timeline" })).getAllByRole("listitem")).toHaveLength(300);
      }
      if (route === "/candidates") {
        const rows = within(screen.getByRole("table", { name: "Ranking" })).getAllByRole("row").slice(1);
        expect(rows).toHaveLength(1000);
        expect(rows[0]).toHaveTextContent("T1");
        expect(rows[999]).toHaveTextContent("T1000");
      }
      if (route === "/trades") {
        fireEvent.click(screen.getByRole("button", { name: "Next" }));
        await waitFor(() => expect(api.callsTo("trades").at(-1)).toEqual([{ limit: 50, offset: 50 }]));
      }
      cleanup();
    }
  });
});

describe("deep links with malformed ids and dates", () => {
  it("never calls the API with a malformed id or date, and shows a plain message instead", async () => {
    for (const raw of ["abc", "-1", "0", "1e3", "12abc", "99999999999999999999", "<script>alert(1)</script>", "1.5"]) {
      const api = new FakeApiClient();
      renderWithProviders(<DashboardPage />, { api, route: `/dashboard?proposal=${encodeURIComponent(raw)}` });
      await screen.findByRole("list", { name: "Timeline" });
      expect(api.callsTo("proposal"), `?proposal=${raw}`).toEqual([]);
      expect(screen.queryByRole("region", { name: /^Proposal / })).toBeNull();
      cleanup();
    }
    const unknown = new FakeApiClient();
    renderWithProviders(<DashboardPage />, { api: unknown, route: "/dashboard?proposal=999" });
    expect(await screen.findByText("Proposal 999 not found")).toBeInTheDocument();
    cleanup();

    for (const raw of ["abc", "0", "-3", "3.0", "<script>alert(1)</script>"]) {
      const api = new FakeApiClient();
      renderWithProviders(<TradesPage />, { api, route: `/trades?position=${encodeURIComponent(raw)}` });
      expect(await screen.findByText(/is not a valid position id/)).toHaveTextContent(raw);
      expect(api.callsTo("position")).toEqual([]);
      expectInert();
      cleanup();
    }

    for (const raw of ["2026-02-30", "2026-13-01", "20261006", "0001-01-01", "<img src=x onerror=alert(1)>"]) {
      const api = new FakeApiClient();
      renderWithProviders(<JournalPage />, { api, route: `/journal?date=${encodeURIComponent(raw)}` });
      expect(await screen.findByText(/is not a date/)).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: /Journal for/ })).toBeNull();
      expectInert();
      cleanup();

      renderWithProviders(<ReportsPage />, { api, route: `/reports?week=${encodeURIComponent(raw)}` });
      expect(await screen.findByText(/is not a date/)).toBeInTheDocument();
      expect(api.callsTo("metrics")).toEqual([]);
      expectInert();
      cleanup();
    }

    // A Saturday belongs to the week just ended.
    const sat = new FakeApiClient();
    renderWithProviders(<ReportsPage />, { api: sat, route: "/reports?week=2026-10-10" });
    expect(await screen.findByRole("heading", { name: "Week of 2026-10-05 to 2026-10-09" })).toBeInTheDocument();
    cleanup();

    // The Candidates page must validate ?date= like Journal and Reports do, not pass garbage to the API.
    for (const raw of ["not-a-date", "2026-02-30", "<script>"]) {
      const api = new FakeApiClient();
      renderWithProviders(<CandidatesPage />, { api, route: `/candidates?date=${encodeURIComponent(raw)}` });
      await settle();
      expect.soft(api.callsTo("candidates"), `?date=${raw} reached the API`).not.toContainEqual([raw]);
      cleanup();
    }
  });
});

// ================================================================ web shell (P4-T12)

describe("http client (T12)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const calls = () => fetchMock.mock.calls as FetchArgs[];

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  it("CSRF: only writes carry the token, never login; it is forgotten on logout and on a 401; uploads are multipart with the token", async () => {
    const onUnauthorized = vi.fn();
    const client = createHttpClient({ onUnauthorized });

    fetchMock.mockResolvedValueOnce(json(fx.sessionOut));
    await client.login({ username: "stephen", password: "pw" });
    expect(headerOf(calls()[0]![1], "X-CSRF-Token")).toBeNull();
    expect(calls()[0]![1].credentials).toBe("same-origin");

    fetchMock.mockResolvedValueOnce(json(fx.dashboardOut));
    await client.dashboard();
    expect(headerOf(calls()[1]![1], "X-CSRF-Token")).toBeNull();

    fetchMock.mockResolvedValueOnce(json(fx.decisionOut));
    await client.approve(12);
    expect(headerOf(calls()[2]![1], "X-CSRF-Token")).toBe("csrf-test-token");

    fetchMock.mockResolvedValueOnce(json(fx.watchlistUploadOut));
    await client.uploadWatchlist(new File(["AAPL\n"], "w.csv", { type: "text/csv" }), { runNightly: true });
    const upload = calls()[3]![1];
    expect(upload.body).toBeInstanceOf(FormData);
    expect(headerOf(upload, "Content-Type")).toBeNull();
    expect(headerOf(upload, "X-CSRF-Token")).toBe("csrf-test-token");

    fetchMock.mockResolvedValueOnce(json(fx.okOut));
    await client.logout();
    expect(headerOf(calls()[4]![1], "X-CSRF-Token")).toBe("csrf-test-token");
    fetchMock.mockResolvedValueOnce(errorResponse(403, "csrf", "Missing CSRF token"));
    fetchMock.mockResolvedValueOnce(errorResponse(401, "unauthorized", "Please log in"));
    await expect(client.approve(12)).rejects.toMatchObject({ status: 401 });
    expect(headerOf(calls()[5]![1], "X-CSRF-Token")).toBeNull();

    // A wrong password on login is not a session expiry; a 401 anywhere else is, once, and drops the token.
    fetchMock.mockResolvedValueOnce(errorResponse(401, "unauthorized", "Invalid username or password"));
    onUnauthorized.mockClear();
    await expect(client.login({ username: "stephen", password: "bad" })).rejects.toMatchObject({ message: "Invalid username or password" });
    expect(onUnauthorized).not.toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(json(fx.sessionOut));
    await client.login({ username: "stephen", password: "pw" });
    fetchMock.mockResolvedValueOnce(errorResponse(401, "unauthorized", "Please log in"));
    await expect(client.dashboard()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(client.csrfToken()).toBeNull();
  });

  it("403 csrf: a write refreshes /auth/me once and retries once with the NEW token; GETs, other 403s and a second 403 are not retried", async () => {
    const onUnauthorized = vi.fn();
    const client = createHttpClient({ onUnauthorized });
    client.setCsrfToken("old-token");

    fetchMock
      .mockResolvedValueOnce(errorResponse(403, "csrf", "CSRF token missing or wrong"))
      .mockResolvedValueOnce(json({ ...fx.sessionOut, csrf_token: "new-token" }))
      .mockResolvedValueOnce(json(fx.decisionOut));
    await expect(client.approve(12)).resolves.toMatchObject({ message: "Approved" });
    expect(calls().map(([u]) => u)).toEqual(["/api/proposals/12/approve", "/api/auth/me", "/api/proposals/12/approve"]);
    expect(headerOf(calls()[0]![1], "X-CSRF-Token")).toBe("old-token");
    expect(headerOf(calls()[2]![1], "X-CSRF-Token")).toBe("new-token");

    fetchMock.mockReset();
    fetchMock
      .mockResolvedValueOnce(errorResponse(403, "csrf", "CSRF"))
      .mockResolvedValueOnce(json(fx.sessionOut))
      .mockResolvedValueOnce(errorResponse(403, "csrf", "CSRF again"));
    await expect(client.reject(12)).rejects.toMatchObject({ status: 403, code: "csrf", message: "CSRF again" });
    expect(calls()).toHaveLength(3);

    fetchMock.mockReset();
    fetchMock.mockResolvedValueOnce(errorResponse(403, "csrf", "CSRF"));
    await expect(client.dashboard()).rejects.toMatchObject({ status: 403 });
    expect(calls()).toHaveLength(1);

    fetchMock.mockReset();
    fetchMock.mockResolvedValueOnce(errorResponse(403, "forbidden", "Origin not allowed"));
    await expect(client.pause()).rejects.toMatchObject({ code: "forbidden" });
    expect(calls()).toHaveLength(1);

    fetchMock.mockReset();
    fetchMock.mockResolvedValueOnce(errorResponse(403, "csrf", "CSRF")).mockResolvedValueOnce(errorResponse(401, "unauthorized", "Please log in"));
    await expect(client.resume()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(calls()).toHaveLength(2);
  });

  it("error bodies are never shown raw: proxy HTML, FastAPI detail, odd envelopes and HTML success bodies become safe messages", async () => {
    const client = createHttpClient();
    const cases: [Response, string][] = [
      [new Response("<html><body><h1>502 Bad Gateway</h1><script>x()</script></body></html>", { status: 502, headers: { "Content-Type": "text/html" } }), "The server had a problem (HTTP 502). Try again."],
      [json({ detail: "Not Found" }, 404), "The request failed (HTTP 404)."],
      [json({ error: { code: "validation", message: { html: "<b>x</b>" } } }, 422), "The request failed (HTTP 422)."],
      [new Response("Internal Server Error", { status: 500 }), "The server had a problem (HTTP 500). Try again."],
      [new Response("<html>login</html>", { status: 200, headers: { "Content-Type": "text/html" } }), "The server sent an unreadable answer."],
    ];
    for (const [res, message] of cases) {
      fetchMock.mockResolvedValueOnce(res);
      const err = await client.system().catch((e: unknown) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).message).toBe(message);
    }
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch https://internal.host:8000"));
    await expect(client.system()).rejects.toMatchObject({ status: 0, code: "network", message: "Can't reach the server" });

    // A server message with markup is kept as a string and shown as text.
    fetchMock.mockResolvedValueOnce(errorResponse(422, "validation", tagged("server")));
    const err = await client.system().catch((e: unknown) => e);
    render(<ErrorBox error={err} />);
    expect(screen.getByRole("alert")).toHaveTextContent(tagged("server"));
    expectInert();
  });
});

describe("login redirect and deep links (T12)", () => {
  it.each([
    ["/%09/evil.example"],
    ["/%0a/evil.example"],
    ["///evil.example"],
    ["/%2f%5cevil.example"],
    [" /dashboard"],
    ["\t//evil.example"],
    ["%2F%2Fevil.example"],
    ["http:/evil.example"],
    [`/${"a".repeat(3000)}`],
    ["/login"],
    ["/login/"],
    ["/login#x"],
  ])("safeNext rejects %j", (raw) => {
    expect(safeNext(raw)).toBe("/dashboard");
  });

  it("login with a hostile next lands on /dashboard; a failed login keeps no password", async () => {
    for (const next of ["/%2F%2Fevil.example", "/%5C%5Cevil.example", "javascript:alert(document.cookie)", "/%09/evil.example"]) {
      const api = new FakeApiClient().fail("me", new ApiError(401, "unauthorized", "Please log in"));
      const r = renderWithProviders(<AppRoutes />, { api, route: `/login?next=${encodeURIComponent(next)}` });
      await userEvent.type(await screen.findByLabelText(/username/i), "stephen");
      await userEvent.type(screen.getByLabelText(/^password/i), "pw-12345678");
      await userEvent.click(screen.getByRole("button", { name: /log in/i }));
      await screen.findByRole("heading", { name: "Dashboard" });
      expect(`${r.location().pathname}${r.location().search}`, `next=${next}`).toBe("/dashboard");
      cleanup();
    }

    const api = new FakeApiClient()
      .fail("me", new ApiError(401, "unauthorized", "Please log in"))
      .fail("login", new ApiError(401, "unauthorized", "Invalid username or password"));
    const r = renderWithProviders(<AppRoutes />, { api, route: "/login" });
    await userEvent.type(await screen.findByLabelText(/username/i), "stephen");
    await userEvent.type(screen.getByLabelText(/^password/i), "wrong-password-1");
    await userEvent.click(screen.getByRole("button", { name: /log in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password");
    expect(document.body.innerHTML).not.toContain("wrong-password-1");
    expect(r.location().pathname).toBe("/login");
  });

  it("with the real HTTP client: a session expiring mid-page goes to login with the deep link, and logging in returns to it with a working CSRF token", async () => {
    let dashboardReads = 0;
    let loggedIn = true;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://localhost").pathname;
      if (path === "/api/auth/me") return loggedIn ? json(fx.sessionOut) : errorResponse(401, "unauthorized", "Please log in");
      if (path === "/api/auth/login") {
        loggedIn = true;
        return json({ ...fx.sessionOut, csrf_token: "csrf-after-login" });
      }
      if (path === "/api/meta") return json(fx.metaOut);
      if (path === "/api/dashboard") {
        dashboardReads += 1;
        if (dashboardReads === 1) {
          loggedIn = false;
          return errorResponse(401, "unauthorized", "Please log in");
        }
        return json(fx.dashboardOut);
      }
      if (path === "/api/proposals/12/approve") {
        return headerOf(init, "X-CSRF-Token") === "csrf-after-login" ? json(fx.decisionOut) : errorResponse(403, "csrf", "CSRF");
      }
      return errorResponse(404, "not_found", "Not found");
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState(null, "", "/dashboard?proposal=12");
    render(<App api={createHttpClient({ onUnauthorized: notifyUnauthorized })} />);

    await screen.findByRole("heading", { name: "Login" });
    expect(window.location.pathname).toBe("/login");
    expect(window.location.search).toBe("?next=%2Fdashboard%3Fproposal%3D12");

    await userEvent.type(screen.getByLabelText(/username/i), "stephen");
    await userEvent.type(screen.getByLabelText(/^password/i), "correct horse");
    await userEvent.click(screen.getByRole("button", { name: /log in/i }));
    const card = await screen.findByRole("article", { name: /proposal 12/i });
    expect(card).toHaveClass("is-highlighted");
    expect(`${window.location.pathname}${window.location.search}`).toBe("/dashboard?proposal=12");
    const loginCall = (fetchMock.mock.calls as unknown as FetchArgs[]).find(([u]) => String(u).endsWith("/api/auth/login"))!;
    expect(headerOf(loginCall[1], "X-CSRF-Token")).toBeNull();

    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    expect(await within(card).findByText("Approved")).toBeInTheDocument();
  });

  it("an unreachable server on the first /auth/me keeps the deep link and Retry opens it", async () => {
    let up = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "http://localhost").pathname;
        if (!up) throw new TypeError("Failed to fetch");
        if (path === "/api/auth/me") return json(fx.sessionOut);
        if (path === "/api/meta") return json(fx.metaOut);
        if (path === "/api/positions/3") return json(fx.positionDetail);
        return errorResponse(404, "not_found", "Not found");
      }),
    );
    window.history.replaceState(null, "", "/trades?position=3");
    render(<App api={createHttpClient({ onUnauthorized: notifyUnauthorized })} />);
    expect(await screen.findByText("Can't reach the server")).toBeInTheDocument();
    expect(`${window.location.pathname}${window.location.search}`).toBe("/trades?position=3");
    up = true;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/BBB · position #3/)).toBeInTheDocument();
    expect(`${window.location.pathname}${window.location.search}`).toBe("/trades?position=3");
  });
});

describe("live updates (T12)", () => {
  function DashboardProbe() {
    const q = useQuery({ queryKey: qk.dashboard(), queryFn: () => apiRef.current!.dashboard() });
    return createElement("p", null, q.data ? "loaded" : "loading");
  }
  const apiRef: { current: FakeApiClient | null } = { current: null };

  function mount(api: FakeApiClient, queryClient: QueryClient, onUnauthorized = vi.fn(), children: ReactNode = null) {
    apiRef.current = api;
    const factory = (url: string) => new FakeEventSource(url);
    const r = render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider client={api}>
          <LiveUpdatesProvider createEventSource={factory} onUnauthorized={onUnauthorized}>
            {children}
          </LiveUpdatesProvider>
        </ApiProvider>
      </QueryClientProvider>,
    );
    return { ...r, onUnauthorized };
  }

  it("one EventSource for the whole session: navigating every tab keeps it, logout closes it", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const r = renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    await screen.findByRole("heading", { name: "Dashboard" });
    const tabs = screen.getByRole("navigation", { name: "Tabs" });
    for (const name of ["Candidates", "Trades", "Journal", "Dashboard"]) {
      fireEvent.click(within(tabs).getByRole("link", { name }));
      await screen.findByRole("heading", { level: 1, name });
    }
    for (const name of ["Performance", "Reports", "Settings", "System"]) {
      fireEvent.click(within(tabs).getByRole("button", { name: "More" }));
      fireEvent.click(screen.getByRole("menuitem", { name }));
      await screen.findByRole("heading", { level: 1, name });
    }
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0]!.url).toBe("/api/stream");
    expect(FakeEventSource.open()).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: /log out/i }));
    await screen.findByRole("heading", { name: "Login" });
    expect(FakeEventSource.open()).toHaveLength(0);
    expect(r.location().pathname).toBe("/login");
  });

  it("invalidates exactly the topic's queries and ignores unknown, prototype and malformed topics", () => {
    const queryClient = createTestQueryClient();
    mount(new FakeApiClient(), queryClient);
    const es = FakeEventSource.instances[0]!;
    const keys = [qk.dashboard(), qk.killswitches(), qk.trades({}), qk.settings(), qk.events({}), qk.system(), qk.journal({}), qk.proposal(3)];
    const seed = () => {
      for (const k of keys) queryClient.setQueryData(k, { seeded: true });
    };
    const invalidated = () =>
      queryClient
        .getQueryCache()
        .getAll()
        .filter((q) => q.state.isInvalidated)
        .map((q) => q.queryKey[0])
        .sort();

    seed();
    act(() => es.emit("invalidate", { topics: ["killswitch"] }));
    expect(invalidated()).toEqual(["dashboard", "killswitches"]);

    seed();
    act(() => es.emit("invalidate", { topics: ["__proto__", "constructor", "toString", "hasOwnProperty", 42, null] }));
    act(() => es.emit("invalidate", { topics: "proposals" }));
    act(() => es.emitRaw("invalidate", "{not json"));
    act(() => es.emit("invalidate", null));
    expect(invalidated()).toEqual([]);

    seed();
    act(() => es.emit("events", { items: [] }));
    expect(invalidated()).toEqual(["dashboard", "events"]);

    seed();
    act(() => es.emit("invalidate", { topics: ["journal", "bogus"] }));
    expect(invalidated()).toEqual(["journal"]);
  });

  it("while disconnected the dashboard polls every 15 s; hello stops it; losing the stream refetches once and polls again", async () => {
    vi.useFakeTimers();
    const api = new FakeApiClient();
    const queryClient = createTestQueryClient();
    mount(api, queryClient, vi.fn(), <DashboardProbe />);
    const es = FakeEventSource.instances[0]!;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    const reads = () => api.callsTo("dashboard").length;
    expect(reads()).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(DISCONNECTED_REFETCH_MS);
    });
    expect(reads()).toBe(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DISCONNECTED_REFETCH_MS);
    });
    expect(reads()).toBe(3);

    act(() => es.emit("hello", { server_time: fx.SERVER_TIME }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4 * DISCONNECTED_REFETCH_MS);
    });
    expect(reads()).toBe(3);

    act(() => es.fail(0));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(reads()).toBe(4);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DISCONNECTED_REFETCH_MS);
    });
    expect(reads()).toBe(5);
  });

  it("errors: a reconnecting stream is left to the browser; a closed one asks /auth/me and reopens ONE stream, or logs out on 401", async () => {
    vi.useFakeTimers();
    const api = new FakeApiClient();
    const onUnauthorized = vi.fn();
    mount(api, createTestQueryClient(), onUnauthorized);
    const first = FakeEventSource.instances[0]!;

    act(() => first.fail(0));
    expect(api.callsTo("me")).toEqual([]);
    expect(first.closed).toBe(false);

    // Two error events while CLOSED (a browser may fire more than one): still one reopened stream.
    act(() => {
      first.fail(2);
      first.fail(2);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REOPEN_DELAY_MS + 10);
    });
    expect(FakeEventSource.open()).toHaveLength(1);
    const second = FakeEventSource.open()[0]!;
    expect(second).not.toBe(first);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3 * REOPEN_DELAY_MS);
    });
    expect(FakeEventSource.open()).toEqual([second]);

    api.fail("me", new ApiError(401, "unauthorized", "Please log in"));
    act(() => second.fail(2));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3 * REOPEN_DELAY_MS);
    });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.open()).toHaveLength(0);
  });
});

describe("phone tab bar (T12)", () => {
  it("five tabs (four pages and More), More reaches the other four and closes on navigation and Escape; the CSS makes it a fixed 5-column bar of 44 px targets below 720 px", async () => {
    const r = renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    await screen.findByRole("heading", { name: "Dashboard" });
    const tabs = screen.getByRole("navigation", { name: "Tabs" });
    const items = Array.from(tabs.children).map((el) => (el.textContent ?? "").trim());
    expect(items).toEqual(["Dashboard", "Candidates", "Trades", "Journal", "More"]);

    const more = within(tabs).getByRole("button", { name: "More" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    expect(within(screen.getByRole("menu")).getAllByRole("menuitem").map((a) => a.textContent)).toEqual(["Performance", "Reports", "Replay", "Settings", "System"]); // Replay: P5-T1
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();

    fireEvent.click(more);
    fireEvent.click(screen.getByRole("menuitem", { name: "Reports" }));
    await screen.findByRole("heading", { level: 1, name: "Reports" });
    expect(r.location().pathname).toBe("/reports");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(within(tabs).getByRole("button", { name: "More" })).toHaveClass("is-active");

    // Vitest turns CSS imports into empty modules, so the stylesheets are read from disk.
    const layoutCss = readWebFile("src/layout/layout.css");
    const stylesCss = readWebFile("src/styles.css");
    const phone = /@media \(max-width: 719px\) \{([\s\S]*?)\n\}/.exec(layoutCss)?.[1] ?? "";
    expect(phone).toMatch(/\.side-nav\s*\{[^}]*display:\s*none/);
    expect(phone).toMatch(/\.tab-bar\s*\{[^}]*position:\s*fixed[^}]*grid-template-columns:\s*repeat\(5,/);
    expect(phone).toMatch(/\.shell-main\s*\{[^}]*padding-bottom:[^}]*var\(--touch\)/);
    expect(layoutCss).toMatch(/\.nav-link\s*\{[^}]*min-height:\s*var\(--touch\)/);
    expect(stylesCss).toMatch(/--touch:\s*44px/);
  });
});
