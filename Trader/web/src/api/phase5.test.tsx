// P5-T1 acceptance test 8: the Phase 5 web contracts: the six new client methods (real HTTP client against a
// mocked `fetch`, and the fake), the query keys and SSE topics, and the /replay route and its navigation entry.
import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppRoutes } from "../App";
import { NAV_ITEMS } from "../layout/Layout";
import { FakeApiClient } from "../test/fakeApi";
import {
  replayCompleted,
  replayOptions,
  replayQueued,
  replayRunning,
  replaySummaries,
  sessionOut,
  weeklyReportBudget,
  weeklyReportOk,
} from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import { API_METHODS, ApiError, type ApiClient } from "./client";
import { createHttpClient } from "./http";
import { prefixesFor, qk, TOPIC_KEYS } from "./queryKeys";
import { MANUAL_JOBS, TOPICS, type ReplayIn } from "./types";

type FetchArgs = [string, RequestInit];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

let fetchMock: ReturnType<typeof vi.fn>;

function calls(): FetchArgs[] {
  return fetchMock.mock.calls as FetchArgs[];
}

class FakeEventSource {
  readyState = 0;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  addEventListener(): void {}
  close(): void {
    this.readyState = 2;
  }
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function loggedIn() {
  const client = createHttpClient();
  fetchMock.mockResolvedValueOnce(json(sessionOut));
  await client.me();
  fetchMock.mockClear();
  return client;
}

describe("the HTTP client's Phase 5 methods", () => {
  it("startReplay posts the JSON body to /api/replays with the CSRF header", async () => {
    const client = await loggedIn();
    const body: ReplayIn = {
      date_from: "2026-11-23",
      date_to: "2026-11-27",
      label: "Risk 1%",
      overrides: { risk_pct: "0.01" },
      strategies: { orb_sip: { params: { top_n: 10 } } },
    };
    fetchMock.mockResolvedValueOnce(json(replayQueued, 202));
    const out = await client.startReplay(body);
    expect(out.id).toBe(replayQueued.id);
    const [url, init] = calls()[0]!;
    expect(url).toBe("/api/replays");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe(sessionOut.csrf_token);
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual(body);
  });

  it("the read routes and cancel use the documented paths", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(json(replayOptions));
    fetchMock.mockResolvedValueOnce(json({ items: replaySummaries }));
    fetchMock.mockResolvedValueOnce(json(replayCompleted));
    fetchMock.mockResolvedValueOnce(json({ ...replayRunning, cancel_requested: true }));
    fetchMock.mockResolvedValueOnce(json(weeklyReportOk));
    await client.replayOptions();
    await client.replays({ limit: 50 });
    await client.replay(11);
    await client.cancelReplay(13);
    await client.weeklyReport("2026-11-23");
    expect(calls().map(([url, init]) => [init.method, url])).toEqual([
      ["GET", "/api/replays/options"],
      ["GET", "/api/replays?limit=50"],
      ["GET", "/api/replays/11"],
      ["POST", "/api/replays/13/cancel"],
      ["GET", "/api/reports/weekly?week=2026-11-23"],
    ]);
    expect(new Headers(calls()[3]![1].headers).get("X-CSRF-Token")).toBe(sessionOut.csrf_token);
  });

  it("weeklyReport resolves null on a 404 and rejects other errors", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(json({ error: { code: "not_found", message: "No report" } }, 404));
    await expect(client.weeklyReport("2026-11-30")).resolves.toBeNull();
    fetchMock.mockResolvedValueOnce(json({ error: { code: "internal", message: "Boom" } }, 500));
    await expect(client.weeklyReport("2026-11-30")).rejects.toBeInstanceOf(ApiError);
  });

  it("a 422 from startReplay keeps its field errors", async () => {
    const client = await loggedIn();
    const fields = [{ loc: ["body", "overrides", "risk_pct"], msg: "must be at most 0.10" }];
    fetchMock.mockResolvedValueOnce(json({ error: { code: "validation", message: "Invalid", fields } }, 422));
    const err = await client.startReplay({ date_from: "2026-11-23", date_to: "2026-11-27" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).fields).toEqual(fields);
  });
});

describe("the fake client's Phase 5 methods", () => {
  it("implements every ApiClient method, records calls and answers from the fixtures", async () => {
    const fake: ApiClient = new FakeApiClient();
    for (const m of ["replayOptions", "replays", "replay", "startReplay", "cancelReplay", "weeklyReport"] as const) {
      expect(API_METHODS).toContain(m);
      expect(typeof fake[m]).toBe("function");
    }
    const api = new FakeApiClient();
    expect(await api.replayOptions()).toEqual(replayOptions);
    expect((await api.replays({ limit: 50 })).items).toEqual(replaySummaries);
    expect(await api.replay(13)).toEqual(replayRunning);
    await expect(api.replay(999)).rejects.toMatchObject({ status: 404 });
    expect(await api.startReplay({ date_from: "2026-11-23", date_to: "2026-11-27" })).toEqual(replayQueued);
    expect((await api.cancelReplay(13)).cancel_requested).toBe(true);
    expect(await api.weeklyReport("2026-11-23")).toEqual(weeklyReportOk);
    api.set("weeklyReport", null);
    expect(await api.weeklyReport("2026-11-30")).toBeNull();
    api.set("weeklyReport", weeklyReportBudget);
    expect((await api.weeklyReport("2026-11-23"))?.commentary_status).toBe("budget");
    expect(api.callsTo("replays")).toEqual([[{ limit: 50 }]]);
    expect(api.callsTo("startReplay")).toEqual([[{ date_from: "2026-11-23", date_to: "2026-11-27" }]]);
  });

  it("the completed fixture has metrics, live metrics, a biased day and two events", () => {
    expect(replayCompleted.metrics).not.toBeNull();
    expect(replayCompleted.live_metrics).not.toBeNull();
    expect(replayCompleted.biased).toBe(true);
    expect(replayCompleted.progress.biased_days).toEqual(["2026-11-23"]);
    expect(replayCompleted.events).toHaveLength(2);
  });
});

describe("query keys and SSE topics", () => {
  it("has keys for the new resources and topics that refresh them", () => {
    expect(qk.replayOptions()).toEqual(["replayOptions"]);
    expect(qk.replays({ limit: 50 })).toEqual(["replays", { limit: 50 }]);
    expect(qk.replay(11)).toEqual(["replay", 11]);
    expect(qk.weeklyReport("2026-11-23")).toEqual(["weeklyReport", "2026-11-23"]);
    expect(TOPIC_KEYS.replays).toEqual(["replays", "replay", "replayOptions"]);
    expect(TOPIC_KEYS.reports).toEqual(["weeklyReport"]);
    expect(prefixesFor(["reports"])).toEqual(["weeklyReport"]);
    expect(TOPICS).toContain("replays");
    expect(TOPICS).toContain("reports");
    expect(MANUAL_JOBS).toContain("weekly");
  });
});

describe("the /replay route", () => {
  it("navigation lists Replay after Reports", () => {
    const labels = NAV_ITEMS.map((i) => i.label);
    expect(labels.indexOf("Replay")).toBe(labels.indexOf("Reports") + 1);
    expect(NAV_ITEMS.find((i) => i.label === "Replay")?.to).toBe("/replay");
  });

  it("renders the placeholder page when logged in, with Replay in the side navigation", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const r = renderWithProviders(<AppRoutes />, { route: "/replay" });
    expect(await screen.findByRole("heading", { level: 1, name: "Replay" })).toBeInTheDocument();
    expect(r.location().pathname).toBe("/replay");
    const side = screen.getByRole("navigation", { name: /main/i });
    expect(within(side).getByRole("link", { name: "Replay" })).toHaveAttribute("href", "/replay");
  });

  it("sends a logged-out visitor to the login page with next=/replay", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const api = new FakeApiClient().fail("me", new ApiError(401, "unauthorized", "Please log in"));
    const r = renderWithProviders(<AppRoutes />, { api, route: "/replay" });
    await screen.findByRole("heading", { name: "Login" });
    expect(r.location().search).toBe("?next=%2Freplay");
  });
});
