// P6-T12: the decision log client methods of the real HTTP client (against a mocked `fetch`) and the keys.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { decisionDayOut, decisionDaysOut } from "../test/fixtures";
import { API_METHODS, ApiError } from "./client";
import { createHttpClient } from "./http";
import { qk } from "./queryKeys";

type FetchArgs = [string, RequestInit];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the HTTP client's decision log methods", () => {
  it("decisionDays and decisionDay GET the documented paths with only the given filters", async () => {
    const client = createHttpClient();
    fetchMock.mockResolvedValueOnce(json(decisionDaysOut));
    fetchMock.mockResolvedValueOnce(json(decisionDayOut));
    fetchMock.mockResolvedValueOnce(json(decisionDayOut));
    expect(await client.decisionDays({ limit: 60 })).toEqual(decisionDaysOut);
    expect(await client.decisionDay({ date: "2026-10-06", limit: 200, offset: 0 })).toEqual(decisionDayOut);
    await client.decisionDay({ date: "2026-10-06", run_id: 42, stage: "scan", outcome: "rejected", ticker: "AMD" });
    const urls = (fetchMock.mock.calls as FetchArgs[]).map(([url, init]) => [init.method, url]);
    expect(urls).toEqual([
      ["GET", "/api/decisions/days?limit=60"],
      ["GET", "/api/decisions?date=2026-10-06&limit=200&offset=0"],
      ["GET", "/api/decisions?date=2026-10-06&run_id=42&stage=scan&outcome=rejected&ticker=AMD"],
    ]);
  });

  it("decisionDay resolves null on a 404 and rejects other errors", async () => {
    const client = createHttpClient();
    fetchMock.mockResolvedValueOnce(json({ error: { code: "not_found", message: "No decisions" } }, 404));
    await expect(client.decisionDay({ date: "2026-10-03" })).resolves.toBeNull();
    fetchMock.mockResolvedValueOnce(json({ error: { code: "validation", message: "Bad stage" } }, 422));
    await expect(client.decisionDay({ date: "2026-10-03" })).rejects.toBeInstanceOf(ApiError);
  });

  it("decisionsCsvUrl is a same-origin link, not a fetch", () => {
    const client = createHttpClient({ baseUrl: "/base" });
    expect(client.decisionsCsvUrl({ date: "2026-10-06" })).toBe("/base/api/export/decisions.csv?date=2026-10-06");
    expect(client.decisionsCsvUrl({ date: "2026-10-06", run_id: 7 })).toBe(
      "/base/api/export/decisions.csv?date=2026-10-06&run_id=7",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("the methods are listed and keyed", () => {
    for (const m of ["decisionDays", "decisionDay", "decisionsCsvUrl"] as const) expect(API_METHODS).toContain(m);
    expect(qk.decisionDays({ limit: 60 })).toEqual(["decisionDays", { limit: 60 }]);
    expect(qk.decisionDay({ date: "2026-10-06" })).toEqual(["decisionDay", { date: "2026-10-06" }]);
  });
});
