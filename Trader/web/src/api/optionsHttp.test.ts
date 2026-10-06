// OPTSIM-T15: the real options client against a mocked `fetch` (no network): every method requests the path
// and verb of the task plan's §3.9, writes carry the CSRF header, and failures become `ApiError`s.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./client";
import { OPTIONS_API_METHODS, type OptionsApiClient, type OptionsApiMethod } from "./optionsClient";
import { createOptionsHttpClient } from "./optionsHttp";
import type { OptOrderIn } from "./types";

type FetchArgs = [string, RequestInit];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function errorBody(status: number, code: string, message: string): Response {
  return json({ error: { code, message, fields: null, request_id: "req-1" } }, status);
}

let fetchMock: ReturnType<typeof vi.fn>;

function calls(): FetchArgs[] {
  return fetchMock.mock.calls as FetchArgs[];
}

function headerOf(init: RequestInit, name: string): string | null {
  return new Headers(init.headers).get(name);
}

beforeEach(() => {
  fetchMock = vi.fn(async () => json({ items: [] }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const ORDER: OptOrderIn = {
  underlying: "F",
  intent: "open",
  structure_id: null,
  legs: [{ instrument: "option", contract_id: 501, side: "sell", effect: "open", ratio: 1 }],
  qty: 1,
  order_type: "limit",
  net_limit: "0.45",
  tif: "day",
  walk: false,
};

/** Every method, the call a page makes, and the request §3.9 says it is. `body` undefined: no body is sent. */
const ROUTES: { [M in OptionsApiMethod]: [(c: OptionsApiClient) => Promise<unknown>, "GET" | "POST" | "PUT", string, unknown?] } = {
  optAccount: [(c) => c.optAccount(), "GET", "/api/options/account"],
  optPositions: [(c) => c.optPositions({ state: "closed", limit: 50 }), "GET", "/api/options/positions?state=closed&limit=50"],
  optChain: [(c) => c.optChain({ underlying: "F" }), "GET", "/api/options/chain?underlying=F"],
  optChainQuotes: [(c) => c.optChainQuotes({ underlying: "BRK.B", expiry: "2026-11-20" }), "GET", "/api/options/chain/quotes?underlying=BRK.B&expiry=2026-11-20"],
  optPreview: [(c) => c.optPreview(ORDER), "POST", "/api/options/orders/preview", ORDER],
  optSubmit: [(c) => c.optSubmit(ORDER), "POST", "/api/options/orders", ORDER],
  optOrders: [(c) => c.optOrders({ status: "history", limit: 50 }), "GET", "/api/options/orders?status=history&limit=50"],
  optCancel: [(c) => c.optCancel(31), "POST", "/api/options/orders/31/cancel"],
  optReprice: [(c) => c.optReprice(31, { net_limit: "-0.30" }), "POST", "/api/options/orders/31/reprice", { net_limit: "-0.30" }],
  optActivity: [(c) => c.optActivity({ limit: 100, before: "2026-10-06T13:00:00Z" }), "GET", "/api/options/activity?limit=100&before=2026-10-06T13%3A00%3A00Z"],
  optPrompts: [(c) => c.optPrompts({ status: "pending" }), "GET", "/api/options/prompts?status=pending"],
  optAnswerPrompt: [(c) => c.optAnswerPrompt(12, { choice: "y", text: null }), "POST", "/api/options/prompts/12/answer", { choice: "y", text: null }],
  optStrategies: [(c) => c.optStrategies(), "GET", "/api/options/strategies"],
  optPutStrategy: [(c) => c.optPutStrategy("toy_call", { enabled: false }), "PUT", "/api/options/strategies/toy_call", { enabled: false }],
  optPanel: [(c) => c.optPanel("toy_call"), "GET", "/api/options/strategies/toy_call/panel"],
  optPanelAction: [
    (c) => c.optPanelAction("toy_call", { action: "note", row_id: "41", value: "hi" }),
    "POST",
    "/api/options/strategies/toy_call/actions",
    { action: "note", row_id: "41", value: "hi" },
  ],
  optSettings: [(c) => c.optSettings(), "GET", "/api/options/settings"],
  optPutSetting: [(c) => c.optPutSetting("options.max_position_pct", "0.40"), "PUT", "/api/options/settings/options.max_position_pct", { value: "0.40" }],
};

describe("createOptionsHttpClient", () => {
  it("has exactly the 18 methods of the route table", () => {
    const client = createOptionsHttpClient();
    expect(Object.keys(client).sort()).toEqual([...OPTIONS_API_METHODS].sort());
    expect(Object.keys(ROUTES).sort()).toEqual([...OPTIONS_API_METHODS].sort());
    expect(OPTIONS_API_METHODS).toHaveLength(18);
  });

  it.each(OPTIONS_API_METHODS.map((m) => [m] as const))("%s requests its route", async (method) => {
    const [call, verb, path, body] = ROUTES[method];
    const client = createOptionsHttpClient({ csrfToken: () => "csrf-1" });
    await call(client);
    expect(calls()).toHaveLength(1);
    const [url, init] = calls()[0]!;
    expect([init.method, url]).toEqual([verb, path]);
    expect(init.credentials).toBe("same-origin");
    // Every write carries the CSRF header; a read never does.
    expect(headerOf(init, "X-CSRF-Token")).toBe(verb === "GET" ? null : "csrf-1");
    if (body === undefined) {
      expect(init.body).toBeUndefined();
    } else {
      expect(headerOf(init, "Content-Type")).toBe("application/json");
      expect(JSON.parse(String(init.body))).toEqual(body);
    }
  });

  it("encodes path parts and refuses a dot segment before any request", async () => {
    const client = createOptionsHttpClient();
    await client.optPanel("a b/c");
    expect(calls()[0]![0]).toBe("/api/options/strategies/a%20b%2Fc/panel");
    await expect(client.optPutSetting("..", 1)).rejects.toMatchObject({ code: "bad_request" });
    await expect(client.optPanel("")).rejects.toMatchObject({ code: "bad_request" });
    expect(calls()).toHaveLength(1);
  });

  it("turns the server's error envelope into an ApiError (409 no_options_run), and hides a body it does not understand", async () => {
    const client = createOptionsHttpClient();
    fetchMock.mockResolvedValueOnce(errorBody(409, "no_options_run", "There is no active options run."));
    const err = await client.optAccount().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "no_options_run", message: "There is no active options run.", requestId: "req-1" });

    fetchMock.mockResolvedValueOnce(new Response("<html>secret proxy page</html>", { status: 502 }));
    await expect(client.optAccount()).rejects.toMatchObject({ status: 502, code: "http_502", message: "The server had a problem (HTTP 502). Try again." });

    fetchMock.mockRejectedValueOnce(new TypeError("failed to fetch"));
    await expect(client.optAccount()).rejects.toMatchObject({ status: 0, code: "network", message: "Can't reach the server" });
  });

  it("a 401 calls onUnauthorized", async () => {
    const onUnauthorized = vi.fn();
    const client = createOptionsHttpClient({ onUnauthorized });
    fetchMock.mockResolvedValueOnce(errorBody(401, "unauthorized", "Please log in"));
    await expect(client.optAccount()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("a 403 csrf answer refreshes the token once and retries the write once", async () => {
    let token = "old";
    const refreshCsrf = vi.fn(async () => {
      token = "new";
    });
    const client = createOptionsHttpClient({ csrfToken: () => token, refreshCsrf });
    fetchMock.mockResolvedValueOnce(errorBody(403, "csrf", "Bad CSRF token"));
    await client.optCancel(31);
    expect(refreshCsrf).toHaveBeenCalledTimes(1);
    expect(calls().map(([url, init]) => [url, headerOf(init, "X-CSRF-Token")])).toEqual([
      ["/api/options/orders/31/cancel", "old"],
      ["/api/options/orders/31/cancel", "new"],
    ]);

    fetchMock.mockClear();
    fetchMock.mockImplementation(async () => errorBody(403, "csrf", "Bad CSRF token"));
    await expect(client.optCancel(31)).rejects.toMatchObject({ status: 403, code: "csrf" });
    expect(calls()).toHaveLength(2);
  });
});
