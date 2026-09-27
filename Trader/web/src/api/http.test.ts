// P4-T12 tests 1-2: the real fetch-based ApiClient against a mocked `fetch` (no network).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./client";
import { createHttpClient } from "./http";
import { sessionOut } from "../test/fixtures";

type FetchArgs = [string, RequestInit];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function errorBody(status: number, code: string, message: string, fields: unknown = null): Response {
  return json({ error: { code, message, fields, request_id: "req-1" } }, status);
}

let fetchMock: ReturnType<typeof vi.fn>;

function calls(): FetchArgs[] {
  return fetchMock.mock.calls as FetchArgs[];
}

function headerOf(init: RequestInit, name: string): string | null {
  return new Headers(init.headers).get(name);
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function loggedIn(opts: Parameters<typeof createHttpClient>[0] = {}) {
  const client = createHttpClient(opts);
  fetchMock.mockResolvedValueOnce(json(sessionOut));
  await client.me();
  fetchMock.mockClear();
  return client;
}

describe("createHttpClient: requests (test 1)", () => {
  it("approve(5) posts to /api/proposals/5/approve with the CSRF header and same-origin credentials", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(json({ proposal: {}, status: "approved", message: "Approved" }));
    await client.approve(5);
    expect(calls()).toHaveLength(1);
    const [url, init] = calls()[0]!;
    expect(url).toBe("/api/proposals/5/approve");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(headerOf(init, "X-CSRF-Token")).toBe(sessionOut.csrf_token);
  });

  it("GET requests carry no CSRF header and build query strings from defined values only", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(json({ items: [] }));
    await client.proposals({ status: "pending", date: undefined, limit: 5 });
    const [url, init] = calls()[0]!;
    expect(url).toBe("/api/proposals?status=pending&limit=5");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("same-origin");
    expect(headerOf(init, "X-CSRF-Token")).toBeNull();
  });

  it("sends JSON bodies with PUT and the path parts encoded", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(json({ key: "risk_pct" }));
    await client.putSetting("killswitch.daily loss", "0.5");
    const [url, init] = calls()[0]!;
    expect(url).toBe("/api/settings/killswitch.daily%20loss");
    expect(init.method).toBe("PUT");
    expect(headerOf(init, "Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({ value: "0.5" });
    expect(headerOf(init, "X-CSRF-Token")).toBe(sessionOut.csrf_token);
  });

  it("maps every other method to its route", async () => {
    const client = await loggedIn();
    fetchMock.mockImplementation(async () => json({ items: [] }));
    await client.logout();
    await client.changePassword({ current_password: "a", new_password: "b" });
    await client.resetKillSwitch("daily_loss_pct", { reason: "checked" });
    await client.pause();
    await client.runJob("nightly", { force: true });
    await client.putQuestradeToken("tok");
    await client.telegramTest();
    await client.deleteWatchlist("2026-10-06");
    await client.putJournal("2026-10-06", { notes: "ok" });
    await client.putStrategy("orb_sip", { enabled: true });
    await client.candidates("2026-10-06");
    await client.position(3);
    await client.trades({ run: "live", from: "2026-10-01" });
    await client.events({ since: 10 });
    const got = calls().map(([url, init]) => `${init.method} ${url}`);
    expect(got).toEqual([
      "POST /api/auth/logout",
      "PUT /api/auth/password",
      "POST /api/killswitch/daily_loss_pct/reset",
      "POST /api/killswitch/pause",
      "POST /api/jobs/nightly/run",
      "POST /api/credentials/questrade",
      "POST /api/system/telegram-test",
      "DELETE /api/watchlist/2026-10-06",
      "PUT /api/journal/2026-10-06",
      "PUT /api/strategies/orb_sip",
      "GET /api/candidates?date=2026-10-06",
      "GET /api/positions/3",
      "GET /api/trades?run=live&from=2026-10-01",
      "GET /api/events?since=10",
    ]);
    const token = calls()[5]![1];
    expect(JSON.parse(String(token.body))).toEqual({ refresh_token: "tok" });
  });

  it("builds same-origin URLs for the CSV export and the stream, with the base URL", () => {
    const client = createHttpClient({ baseUrl: "/base" });
    expect(client.exportTradesUrl({ run: "live", from: "2026-10-01" })).toBe("/base/api/export/trades.csv?run=live&from=2026-10-01");
    expect(client.streamUrl()).toBe("/base/api/stream");
    expect(createHttpClient().streamUrl()).toBe("/api/stream");
  });

  it("uploads the watchlist as multipart form data", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(json({ watchlist: {}, rejected: [], launched: null }));
    const file = new File(["ticker\nAAPL\n"], "list.csv", { type: "text/csv" });
    await client.uploadWatchlist(file, { date: "2026-10-06", runNightly: true });
    const [url, init] = calls()[0]!;
    expect(url).toBe("/api/watchlist");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const form = init.body as FormData;
    expect((form.get("file") as File).name).toBe("list.csv");
    expect(form.get("date")).toBe("2026-10-06");
    expect(form.get("run_nightly")).toBe("true");
    // The browser sets the multipart boundary itself.
    expect(headerOf(init, "Content-Type")).toBeNull();
    expect(headerOf(init, "X-CSRF-Token")).toBe(sessionOut.csrf_token);
  });

  it("watchlist() resolves null on a 404", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(errorBody(404, "not_found", "No watchlist"));
    await expect(client.watchlist("2026-10-06")).resolves.toBeNull();
    expect(calls()[0]![0]).toBe("/api/watchlist?date=2026-10-06");
  });

  it("a 422 body becomes an ApiError with fields and the request id", async () => {
    const client = await loggedIn();
    const fields = [{ loc: ["body", "value"], msg: "must be at most 5" }];
    fetchMock.mockResolvedValueOnce(errorBody(422, "validation", "Check the highlighted fields", fields));
    const err = await client.putSetting("risk_pct", "9").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const api = err as ApiError;
    expect(api.status).toBe(422);
    expect(api.code).toBe("validation");
    expect(api.message).toBe("Check the highlighted fields");
    expect(api.fields).toEqual(fields);
    expect(api.requestId).toBe("req-1");
  });

  it("a network failure becomes code network", async () => {
    const client = await loggedIn();
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const err = await client.dashboard().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
    expect((err as ApiError).code).toBe("network");
    expect((err as ApiError).message).toBe("Can't reach the server");
  });

  it("a non-JSON error body still becomes an ApiError without leaking the body", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(new Response("<html>Bad gateway secret</html>", { status: 502 }));
    const err = (await client.dashboard().catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
    expect(err.message).not.toContain("secret");
  });
});

describe("createHttpClient: 401 and CSRF (test 2)", () => {
  it("a 401 calls onUnauthorized and rejects", async () => {
    const onUnauthorized = vi.fn();
    const client = await loggedIn({ onUnauthorized });
    fetchMock.mockResolvedValueOnce(errorBody(401, "unauthorized", "Please log in"));
    const err = (await client.dashboard().catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(401);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("a 401 from login (wrong password) does not call onUnauthorized", async () => {
    const onUnauthorized = vi.fn();
    const client = createHttpClient({ onUnauthorized });
    fetchMock.mockResolvedValueOnce(errorBody(401, "unauthorized", "Wrong username or password"));
    const err = (await client.login({ username: "stephen", password: "x" }).catch((e: unknown) => e)) as ApiError;
    expect(err.message).toBe("Wrong username or password");
    expect(onUnauthorized).not.toHaveBeenCalled();
    const [url, init] = calls()[0]!;
    expect(url).toBe("/api/auth/login");
    expect(headerOf(init, "X-CSRF-Token")).toBeNull();
  });

  it("login stores the session's CSRF token for later writes; logout forgets it", async () => {
    const client = createHttpClient();
    fetchMock.mockResolvedValueOnce(json({ ...sessionOut, csrf_token: "from-login" }));
    await client.login({ username: "stephen", password: "pw" });
    fetchMock.mockResolvedValueOnce(json({ switches: [], history: [] }));
    await client.pause();
    expect(headerOf(calls()[1]![1], "X-CSRF-Token")).toBe("from-login");
    fetchMock.mockResolvedValueOnce(json({ ok: true, message: null }));
    await client.logout();
    fetchMock.mockResolvedValueOnce(json({ switches: [], history: [] }));
    await client.resume();
    expect(headerOf(calls()[3]![1], "X-CSRF-Token")).toBeNull();
  });

  it("a 403 csrf refreshes /auth/me and retries once with the new token", async () => {
    const client = await loggedIn();
    fetchMock
      .mockResolvedValueOnce(errorBody(403, "csrf", "Session check failed"))
      .mockResolvedValueOnce(json({ ...sessionOut, csrf_token: "fresh" }))
      .mockResolvedValueOnce(json({ proposal: {}, status: "approved", message: "Approved" }));
    await client.approve(7);
    const urls = calls().map(([url]) => url);
    expect(urls).toEqual(["/api/proposals/7/approve", "/api/auth/me", "/api/proposals/7/approve"]);
    // Two fetch calls for the request itself.
    expect(urls.filter((u) => u === "/api/proposals/7/approve")).toHaveLength(2);
    expect(headerOf(calls()[2]![1], "X-CSRF-Token")).toBe("fresh");
  });

  it("a second 403 csrf surfaces as an error", async () => {
    const client = await loggedIn();
    fetchMock
      .mockResolvedValueOnce(errorBody(403, "csrf", "Session check failed"))
      .mockResolvedValueOnce(json(sessionOut))
      .mockResolvedValueOnce(errorBody(403, "csrf", "Session check failed"));
    const err = (await client.approve(7).catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(403);
    expect(err.code).toBe("csrf");
    expect(calls()).toHaveLength(3);
  });

  it("a 403 that is not csrf is not retried", async () => {
    const client = await loggedIn();
    fetchMock.mockResolvedValueOnce(errorBody(403, "forbidden", "Origin not allowed"));
    const err = (await client.pause().catch((e: unknown) => e)) as ApiError;
    expect(err.code).toBe("forbidden");
    expect(calls()).toHaveLength(1);
  });
});
