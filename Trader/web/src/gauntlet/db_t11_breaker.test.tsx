// DB-T11 gauntlet (attempt 1): breaker tests for the web wiring (Dashboard on /api/live, Control route and
// /system redirect, nav, theme, 2 s throttle and 15 s polling fallback, Settings move, test migration, smoke
// spec), written against the plan (DB-T11, S9, S11-S16, open questions 1, 6, 9) and design D9. FakeApiClient,
// fake EventSource and liveFixtures only; no network.
import { QueryClientProvider, useQuery, type QueryClient } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import ts from "typescript";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes } from "../App";
import { ApiProvider } from "../api/client";
import { qk } from "../api/queryKeys";
import type { KillSwitchesOut } from "../api/types";
import { ROUTER_FUTURE } from "../layout/routerFuture";
import { DISCONNECTED_REFETCH_MS, LIVE_THROTTLE_MS, LiveUpdatesProvider, type EventSourceLike } from "../live/useLiveUpdates";
import ControlPage from "../pages/Control";
import DashboardPage from "../pages/Dashboard";
import SettingsPage from "../pages/Settings";
import { FakeApiClient } from "../test/fakeApi";
import * as fx from "../test/fixtures";
import * as lfx from "../test/liveFixtures";
import { createTestQueryClient, renderWithProviders } from "../test/render";

// ---------------------------------------------------------------- helpers

function webPath(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return found;
}

function readWebFile(relative: string): string {
  return readFileSync(webPath(relative), "utf8");
}

/** Every file under `dir` (relative to web/) whose name matches `re`. */
function webFiles(dir: string, re: RegExp): string[] {
  const root = webPath(dir);
  const out: string[] = [];
  const walk = (abs: string, rel: string) => {
    for (const entry of readdirSync(abs, { withFileTypes: true })) {
      const a = join(abs, entry.name);
      const r = `${rel}/${entry.name}`;
      if (entry.isDirectory()) walk(a, r);
      else if (re.test(entry.name)) out.push(r);
    }
  };
  walk(root, dir);
  return out;
}

interface CssRule {
  file: string;
  selectors: string[];
  decls: string;
}

/** The innermost `selector { decls }` blocks of a CSS file (media queries flattened), comments removed. */
function cssRules(file: string): CssRule[] {
  const css = readWebFile(file).replace(/\/\*[\s\S]*?\*\//g, "");
  const out: CssRule[] = [];
  const re = /([^{}@;]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css))) {
    const selectors = m[1]!.split(",").map((s) => s.trim()).filter(Boolean);
    out.push({ file, selectors, decls: m[2]! });
  }
  return out;
}

type Listener = (ev: MessageEvent) => void;

class FakeSource implements EventSourceLike {
  static all: FakeSource[] = [];
  readyState = 0;
  onerror: ((ev: Event) => void) | null = null;
  private listeners = new Map<string, Listener[]>();
  constructor(readonly url: string = "/api/stream") {
    FakeSource.all.push(this);
  }
  static last(): FakeSource {
    const es = FakeSource.all.at(-1);
    if (!es) throw new Error("no EventSource opened");
    return es;
  }
  addEventListener(type: string, listener: Listener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  close(): void {
    this.readyState = 2;
  }
  emit(type: string, data: unknown = {}): void {
    if (type === "hello") this.readyState = 1;
    const ev = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const l of this.listeners.get(type) ?? []) l(ev);
  }
  fail(readyState = 0): void {
    this.readyState = readyState;
    this.onerror?.(new Event("error"));
  }
}

function providerTree(api: FakeApiClient, queryClient: QueryClient, route: string, children: ReactNode) {
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider client={api}>
        <MemoryRouter initialEntries={[route]} future={ROUTER_FUTURE}>
          <LiveUpdatesProvider createEventSource={(url) => new FakeSource(url)}>{children}</LiveUpdatesProvider>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>
  );
}

const trippedDrawdown: KillSwitchesOut = {
  switches: fx.killswitchStates.map((s) =>
    s.switch === "max_drawdown_pct"
      ? { ...s, tripped: true, tripped_at: "2026-10-06T14:00:00Z", value: "0.2100", threshold: "0.2000", automatic: true, needs_web_reset: true }
      : s,
  ),
  history: fx.killswitchEvents,
};

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeSource.all = [];
  document.documentElement.removeAttribute("data-theme");
});

// ================================================================ test migration integrity

describe("B1-B2 test migration (S13) and the Settings move (open question 6)", () => {
  it("B1: the System page's 'Token error' assertion still has an equivalent on Control (not weakened away)", async () => {
    // SystemPage.test pinned `status "Token error"` for a failed token; SystemCarryOver.test only checks the
    // paste link. The state itself must still be visible on Control's Health card, with the error text.
    const api = new FakeApiClient({
      control: lfx.controlWith({ health: { ...lfx.healthPanel, token: { ...fx.tokenOut, ok: false, seeded: true, error: "refresh token expired" } } }),
    });
    renderWithProviders(<ControlPage />, { api, route: "/control" });
    const health = await screen.findByRole("region", { name: "Health" });
    expect(within(health).getByText("Token error")).toBeInTheDocument();
    expect(within(health).getByText("refresh token expired")).toBeInTheDocument();
    expect(within(health).queryByText("Token OK")).toBeNull();
  });

  it("B2: Settings has no engine control left; the Telegram test is on both pages; Control has exactly one Pause/Resume", async () => {
    const settings = renderWithProviders(<SettingsPage />, { route: "/settings" });
    await screen.findByRole("article", { name: "risk_pct" });
    for (const name of [/^pause/i, /^resume/i, /^reset/i, /^manual$/i, /^auto$/i]) {
      expect(screen.queryByRole("button", { name }), String(name)).toBeNull();
    }
    expect(screen.queryByRole("radio", { name: /manual|auto/i })).toBeNull();
    expect(screen.getAllByRole("button", { name: "Send a test message" })).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Control page" })).toHaveAttribute("href", "/control");
    expect(settings.api.callsTo("killswitches")).toEqual([]);
    settings.unmount();

    renderWithProviders(<ControlPage />, { api: new FakeApiClient({ killswitches: trippedDrawdown }), route: "/control" });
    await screen.findByRole("region", { name: "Health" });
    await screen.findByRole("button", { name: "Reset Max drawdown" });
    expect(screen.getAllByRole("button", { name: /^(Pause|Resume)$/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Send a test message" })).toHaveLength(1);
  });
});

// ================================================================ URL state

describe("B3-B5 URL handling", () => {
  it("B3: a hostile ?expand= (thousands of ids, duplicates, junk, unsafe integers) sends at most 3 valid distinct ids", async () => {
    const tail = Array.from({ length: 5000 }, (_, i) => (i % 7 === 0 ? "x" : String((i % 5) + 1))).join(",");
    const expand = ["9007199254740993", "00", "%2B1", "-1", "1.0", "1e3", "%3Cscript%3E", "9007199254740991", " 6 ", "6", "6", tail].join(",");
    const api = new FakeApiClient();
    const started = performance.now();
    renderWithProviders(<DashboardPage />, { api, route: `/dashboard?expand=${expand}` });
    await screen.findByRole("region", { name: "Session" });
    expect(performance.now() - started).toBeLessThan(5_000);
    const calls = api.callsTo("live") as [{ range: string; expand?: string }][];
    expect(calls).toHaveLength(1);
    const sent = calls[0]![0].expand!.split(",");
    expect(sent.length).toBeLessThanOrEqual(3);
    expect(new Set(sent).size).toBe(sent.length);
    for (const id of sent) {
      expect(id).toMatch(/^[1-9][0-9]{0,18}$/);
      expect(Number.isSafeInteger(Number(id))).toBe(true);
    }
    expect(calls[0]![0]).toEqual({ range: "today", expand: "9007199254740991,6,2" });
  });

  it("B4: an invalid ?range= falls back to today; ?proposal= still highlights; auto mode with a failed pending part still shows the panel", async () => {
    const api = new FakeApiClient({ live: lfx.liveWith({ pending: [fx.pendingProposal] }) });
    const r = renderWithProviders(<DashboardPage />, { api, route: "/dashboard?range=%3Cscript%3E&proposal=12&expand=0,-1" });
    await screen.findByRole("region", { name: "Session" });
    expect(api.callsTo("live")).toEqual([[{ range: "today" }]]);
    expect(screen.getByRole("article", { name: /proposal 12/i })).toHaveClass("is-highlighted");
    await userEvent.click(within(screen.getByRole("region", { name: "Equity" })).getByRole("button", { name: "Whole run" }));
    expect(new URLSearchParams(r.location().search).get("proposal")).toBe("12");
    expect(new URLSearchParams(r.location().search).get("range")).toBe("run");
    r.unmount();

    const failed = new FakeApiClient({
      live: lfx.liveWith({ approval_mode: "auto", pending: null, part_errors: [{ part: "pending", message: "OperationalError: pending could not be read" }] }),
    });
    renderWithProviders(<DashboardPage />, { api: failed, route: "/dashboard" });
    const pending = await screen.findByRole("region", { name: "Pending approvals" });
    expect(within(pending).getByText("OperationalError: pending could not be read")).toBeInTheDocument();
    expect(within(pending).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("B5: /system with an encoded query and a hash lands on /control with both unchanged", async () => {
    const r = renderWithProviders(<AppRoutes />, { route: "/system?x=a%20b&next=%2Fsettings%23q&x=2#health" });
    expect(await screen.findByRole("heading", { level: 1, name: "Control" })).toBeInTheDocument();
    const l = r.location();
    expect(`${l.pathname}${l.search}${l.hash}`).toBe("/control?x=a%20b&next=%2Fsettings%23q&x=2#health");
  });
});

// ================================================================ throttle and polling

describe("B6-B9 throttle, mutations and the polling fallback", () => {
  it("B6: a mixed burst (events messages, marks, system and killswitch topics) refetches live and control at most once per 2 s, with one trailing", async () => {
    vi.useFakeTimers();
    const api = new FakeApiClient();
    const queryClient = createTestQueryClient();
    const wrapper = ({ children }: { children?: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, createElement(ApiProvider, { client: api }, createElement(LiveUpdatesProvider, { createEventSource: (u: string) => new FakeSource(u) }, children)));
    renderHook(
      () => {
        useQuery({ queryKey: qk.live({ range: "today" }), queryFn: () => api.live({ range: "today" }) });
        useQuery({ queryKey: qk.control(), queryFn: () => api.control() });
      },
      { wrapper },
    );
    await act(async () => {
      FakeSource.last().emit("hello", { server_time: fx.SERVER_TIME });
      await vi.advanceTimersByTimeAsync(10);
    });
    const counts = () => [api.callsTo("live").length, api.callsTo("control").length];
    expect(counts()).toEqual([1, 1]);
    const burst: [string, unknown][] = [
      ["events", { items: [] }],
      ["invalidate", { topics: ["marks"] }],
      ["invalidate", { topics: ["system"] }],
      ["invalidate", { topics: ["killswitch", "activity"] }],
      ["events", { items: [] }],
      ["invalidate", { topics: ["jobs", "marks"] }],
      ["invalidate", { topics: ["system", "events"] }],
    ];
    for (const [type, data] of burst) {
      await act(async () => {
        FakeSource.last().emit(type, data);
        await vi.advanceTimersByTimeAsync(130);
      });
    }
    // ~0.9 s in: exactly one leading refetch each
    expect(counts()).toEqual([2, 2]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_THROTTLE_MS);
    });
    expect(counts()).toEqual([3, 3]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * LIVE_THROTTLE_MS);
    });
    expect(counts()).toEqual([3, 3]);
  });

  it("B7: Approve is never delayed by an open throttle window: the live refetch follows at once, not at 2 s", async () => {
    const api = new FakeApiClient({ live: lfx.liveWith({ pending: [fx.pendingProposal] }) });
    const queryClient = createTestQueryClient();
    render(providerTree(api, queryClient, "/dashboard", <DashboardPage />));
    await screen.findByRole("region", { name: "Session" });
    act(() => FakeSource.last().emit("hello", { server_time: fx.SERVER_TIME }));
    act(() => FakeSource.last().emit("invalidate", { topics: ["marks"] })); // leading; opens the window
    await waitFor(() => expect(api.callsTo("live")).toHaveLength(2));
    act(() => FakeSource.last().emit("invalidate", { topics: ["marks"] })); // pending trailing at 2 s
    const clicked = performance.now();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(api.callsTo("live").length).toBeGreaterThanOrEqual(3), { timeout: 1_000 });
    expect(performance.now() - clicked).toBeLessThan(LIVE_THROTTLE_MS);
    expect(api.callsTo("approve")).toEqual([[12]]);
  });

  // Known failure (gauntlet finding): it.fails keeps the shared gate green and fails once fixed; drop ".fails" in the fix round.
  it("B8: a kill-switch reset on Control refreshes the Control data at once (the lights come from /api/control)", async () => {
    const api = new FakeApiClient({ killswitches: trippedDrawdown });
    renderWithProviders(<ControlPage />, { api, route: "/control" });
    const region = await screen.findByRole("region", { name: "Kill switches" });
    await userEvent.click(await within(region).findByRole("button", { name: "Reset Max drawdown" }));
    await userEvent.type(within(region).getByLabelText("Reason for the reset"), "checked the books, drawdown understood");
    await userEvent.click(within(region).getByRole("button", { name: "Reset…" }));
    const before = api.callsTo("control").length;
    await userEvent.click(within(region).getByRole("button", { name: "Confirm reset" }));
    await waitFor(() => expect(api.callsTo("resetKillSwitch")).toHaveLength(1));
    // KillSwitchCard's lights are LiveOut/ControlOut data under the `system` prefix; the SSE `killswitch`
    // topic maps to ["dashboard", "killswitches"] only, so without an invalidation here they stay "Tripped".
    await waitFor(() => expect(api.callsTo("control").length).toBeGreaterThan(before), { timeout: 1_000 });
  });

  it("B9: a Control/Dashboard query mounted while already disconnected polls every 15 s; other queries do not; hello stops it", async () => {
    vi.useFakeTimers();
    const api = new FakeApiClient();
    const queryClient = createTestQueryClient();
    const wrapper = ({ children }: { children?: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, createElement(ApiProvider, { client: api }, createElement(LiveUpdatesProvider, { createEventSource: (u: string) => new FakeSource(u) }, children)));
    const { rerender } = renderHook(
      ({ mounted }: { mounted: boolean }) => {
        useQuery({ queryKey: qk.candidates(), queryFn: () => api.candidates() });
        useQuery({ queryKey: qk.control(), queryFn: () => api.control(), enabled: mounted });
        useQuery({ queryKey: qk.live({ range: "run" }), queryFn: () => api.live({ range: "run" }), enabled: mounted });
      },
      { wrapper, initialProps: { mounted: false } },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    rerender({ mounted: true }); // navigated to the page after the stream was already down
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    const counts = () => [api.callsTo("control").length, api.callsTo("live").length, api.callsTo("candidates").length];
    expect(counts()).toEqual([1, 1, 1]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2 * DISCONNECTED_REFETCH_MS);
    });
    expect(counts()).toEqual([3, 3, 1]);
    act(() => FakeSource.last().emit("hello", { server_time: fx.SERVER_TIME }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3 * DISCONNECTED_REFETCH_MS);
    });
    expect(counts()).toEqual([3, 3, 1]);
  });
});

// ================================================================ theme and tokens

describe("B10-B11 theme and tokens", () => {
  it("B10: main.tsx applies the remembered theme before the first render, and dark when storage is blocked", async () => {
    const seen: (string | null)[] = [];
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    vi.doMock("react-dom/client", () => ({
      createRoot: () => ({ render: () => seen.push(document.documentElement.getAttribute("data-theme")) }),
    }));
    try {
      const data = new Map([["trader.theme", "light"]]);
      vi.stubGlobal("localStorage", { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) });
      vi.resetModules();
      await import("../main");
      vi.stubGlobal("localStorage", {
        getItem: () => {
          throw new Error("SecurityError: blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
      });
      document.documentElement.setAttribute("data-theme", "light");
      vi.resetModules();
      await import("../main");
    } finally {
      vi.doUnmock("react-dom/client");
      vi.resetModules();
      root.remove();
    }
    expect(seen).toEqual(["light", "dark"]);
  });

  it("B11: every CSS variable the app reads is defined by tokens.css in every theme block; styles.css defines none of its own", () => {
    const tokens = readWebFile("src/theme/tokens.css").replace(/\/\*[\s\S]*?\*\//g, "");
    const blocks = tokens.match(/:root[^{]*\{[^}]*\}/g) ?? [];
    expect(blocks.length).toBe(4); // dark (default), light, auto+dark, auto+light
    const definedIn = (block: string) => new Set(Array.from(block.matchAll(/(--[a-z0-9-]+)\s*:/g), (m) => m[1]!));
    const sets = blocks.map(definedIn);
    const localVars = new Set(["--tone", "--tone-bg"]); // set per element by the tone classes
    const used = new Set<string>();
    for (const f of [...webFiles("src", /\.(css|tsx|ts)$/)].filter((p) => !/\.test\.tsx?$/.test(p))) {
      for (const m of readWebFile(f).matchAll(/var\((--[a-z0-9-]+)/g)) used.add(m[1]!);
    }
    const missing: string[] = [];
    for (const v of used) {
      if (localVars.has(v)) continue;
      sets.forEach((s, i) => {
        if (!s.has(v)) missing.push(`${v} (theme block ${i})`);
      });
    }
    expect(missing).toEqual([]);
    for (const v of ["--money-up", "--money-down", "--money-flat", "--panel-border", "--panel-radius", "--touch"]) {
      for (const s of sets) expect(s.has(v), v).toBe(true);
    }
    const styles = readWebFile("src/styles.css").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(styles).not.toMatch(/:root\s*\{/);
    expect(styles).not.toMatch(/prefers-color-scheme/);
    const main = readWebFile("src/main.tsx");
    expect(main.indexOf('import "./theme/tokens.css"')).toBeGreaterThan(-1);
    expect(main.indexOf('import "./theme/tokens.css"')).toBeLessThan(main.indexOf('import "./styles.css"'));
    expect(main.indexOf("applyTheme(readThemeChoice())")).toBeLessThan(main.indexOf("createRoot("));
  });
});

// ================================================================ design D9 on the Dashboard

const DIRECT_RED_GREEN = /(^|;)\s*(?!--)[a-z-]+\s*:[^;]*var\(--(ok|bad|ok-bg|bad-bg|money-up|money-down)\)/;

describe("B12-B13 design D9: green and red only for money on the Dashboard", () => {
  // Known failure (gauntlet finding): it.fails keeps the shared gate green and fails once fixed; drop ".fails" in the fix round.
  it("B12: no element of the Dashboard is painted by a rule that uses the legacy green/red directly (without a .live-page override)", async () => {
    const timeline = [{ ...fx.timeline[0]!, status: "failed" as const }, ...fx.timeline.slice(1)];
    const api = new FakeApiClient({
      live: lfx.liveWith({ pending: [fx.pendingProposal], timeline, worker_stale: true, telegram_configured: false }),
    });
    renderWithProviders(<DashboardPage />, { api, route: "/dashboard?proposal=11" });
    await screen.findByRole("region", { name: "Pending approvals" });
    await screen.findByRole("region", { name: /^proposal 11$/i });
    const page = document.querySelector("main.live-page")!;
    expect(page).not.toBeNull();

    const files = ["src/styles.css", "src/pages/dashboard/dashboard.css", "src/pages/live/liveA.css", "src/pages/live/liveB.css", "src/theme/tokens.css"];
    const rules = files.flatMap(cssRules);
    const allSelectors = new Set(rules.flatMap((r) => r.selectors));
    const violations: string[] = [];
    for (const rule of rules) {
      if (!DIRECT_RED_GREEN.test(rule.decls)) continue;
      for (const sel of rule.selectors) {
        if (/\.money\b/.test(sel)) continue; // the money classes are the one allowed use
        let hits: Element[] = [];
        try {
          hits = [page, ...Array.from(page.querySelectorAll("*"))].filter((el) => el.matches(sel));
        } catch {
          continue; // pseudo-elements
        }
        if (hits.length === 0) continue;
        if (allSelectors.has(`.live-page ${sel}`)) continue; // remapped to the status colours on this page
        violations.push(`${rule.file}: "${sel}" paints ${hits.length} element(s), e.g. <${hits[0]!.tagName.toLowerCase()} class="${hits[0]!.getAttribute("class")}">${(hits[0]!.textContent ?? "").slice(0, 30)}`);
      }
    }
    expect(violations).toEqual([]);
  });

  // Known failure (gauntlet finding): it.fails keeps the shared gate green and fails once fixed; drop ".fails" in the fix round.
  it("B13: the app frame around the Dashboard (live dot, header) uses no legacy green/red tone once the stream is live", async () => {
    vi.stubGlobal(
      "EventSource",
      class extends FakeSource {
        constructor(url: string) {
          super(url);
        }
      },
    );
    renderWithProviders(<AppRoutes />, { api: new FakeApiClient(), route: "/dashboard" });
    await screen.findByRole("region", { name: "Session" });
    act(() => FakeSource.last().emit("hello", { server_time: fx.SERVER_TIME }));
    const banner = document.querySelector<HTMLElement>(".shell-header")!;
    await within(banner).findByRole("status", { name: "Live" });
    const outside = Array.from(document.querySelectorAll(".tone-ok, .tone-bad")).filter((el) => !el.closest(".live-page"));
    expect(outside.map((el) => `${el.getAttribute("class")} "${el.textContent}"`)).toEqual([]);
  });
});

// ================================================================ the smoke spec

describe("B14 smoke spec: the Server-Timing budget helpers and what it prints", () => {
  const spec = readWebFile("tests/smoke.spec.ts");

  function helpers(): { appDuration: (h: string | null | undefined) => number | null; median: (v: readonly number[]) => number } {
    const start = spec.indexOf("export function appDuration");
    const end = spec.indexOf("/** GET /api/live once");
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const source = spec.slice(start, end).replace(/export function/g, "function");
    const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.None } }).outputText;
    return new Function(`${js}\nreturn { appDuration, median };`)() as ReturnType<typeof helpers>;
  }

  it("B14: parses app;dur wherever it sits, rejects look-alikes, takes a true median, budget 300 ms strict, logs only numbers", () => {
    const { appDuration, median } = helpers();
    expect(appDuration("app;dur=123.4, periods;dur=5.1, equity;dur=40")).toBe(123.4);
    expect(appDuration("periods;dur=5, app;dur=250")).toBe(250);
    expect(appDuration('cfL4;desc="?proto=TCP&rtt=1234&sent=5", app;dur=7.25')).toBe(7.25);
    expect(appDuration('app;desc="total";dur=42')).toBe(42);
    expect(appDuration(" app ; dur=9")).toBe(9);
    for (const bad of [null, undefined, "", "app", "app;dur=", "appx;dur=5", "xapp;dur=5", "periods;dur=5", "app;dur=abc"]) {
      expect(appDuration(bad), String(bad)).toBeNull();
    }
    expect(median([300, 1, 299, 1000, 2])).toBe(299);
    expect(median([5, 1])).toBe(3);
    expect(median([7])).toBe(7);

    expect(spec).toMatch(/LIVE_BUDGET_MS = 300\b/);
    expect(spec).toMatch(/toBeLessThan\(LIVE_BUDGET_MS\)/);
    expect(spec).toMatch(/LIVE_SAMPLES = 5\b/);
    // the timings come through the page's own logged-in request context, one warm-up first
    expect(spec).toMatch(/const warm = await page\.request\.get\("\/api\/live"\)/);
    // nothing but the numbers is printed: every console call in the spec prints only timings
    const logs = spec.split("\n").filter((l) => /console\.(log|info|warn|error|debug)\(/.test(l));
    expect(logs.length).toBeGreaterThanOrEqual(1);
    for (const line of logs) {
      expect(line).not.toMatch(/headers\(\)|process\.env|password|PASSWORD|cookie|creds|credentials|token|storageState|JSON\.stringify/);
      expect(line).toMatch(/timings|mid/);
    }
  });
});
