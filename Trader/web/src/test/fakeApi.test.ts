import { describe, expect, expectTypeOf, it } from "vitest";

import { API_METHODS, ApiError, queryString, type ApiClient, type ApiMethod } from "../api/client";
import type { SettingOut } from "../api/types";
import { FakeApiClient } from "./fakeApi";
import * as fx from "./fixtures";
import * as lfx from "./liveFixtures";

describe("FakeApiClient (acceptance test 5)", () => {
  it("satisfies ApiClient and API_METHODS lists every method", () => {
    // Type-level: tsc (npm run check) fails if the fake drifts from the interface or the list misses one.
    const client: ApiClient = new FakeApiClient();
    expectTypeOf<Exclude<keyof ApiClient, ApiMethod>>().toEqualTypeOf<never>();
    for (const method of API_METHODS) expect(typeof client[method]).toBe("function");
    expect(new Set(API_METHODS).size).toBe(API_METHODS.length);
  });

  it("returns the fixtures and records calls", async () => {
    const api = new FakeApiClient();
    const dashboard = await api.dashboard();
    expect(dashboard.pending[0]?.ticker).toBe("AAA");
    await api.approve(12);
    await api.trades({ offset: 50 });
    expect(api.calls).toEqual([
      ["dashboard", []],
      ["approve", [12]],
      ["trades", [{ offset: 50 }]],
    ]);
    expect(api.callsTo("approve")).toEqual([[12]]);
  });

  it("hands out copies, so a test cannot corrupt the fixtures", async () => {
    const api = new FakeApiClient();
    const first = await api.dashboard();
    first.pending.length = 0;
    expect((await api.dashboard()).pending).toHaveLength(1);
    expect(fx.dashboardOut.pending).toHaveLength(1);
  });

  it("fail(method, error) makes that method reject until succeed()", async () => {
    const api = new FakeApiClient();
    const error = new ApiError(409, "conflict", "Already paused.");
    api.fail("approve", error);
    await expect(api.approve(12)).rejects.toBe(error);
    await expect(api.reject(12)).resolves.toMatchObject({ message: "Rejected" });
    api.succeed("approve");
    await expect(api.approve(12)).resolves.toMatchObject({ message: "Approved" });
    expect(api.callsTo("approve")).toHaveLength(2);
  });

  it("set, respond and constructor overrides change the answers", async () => {
    const api = new FakeApiClient({ metrics: fx.emptyMetrics });
    expect((await api.metrics({})).trades).toBe(0);
    api.set("approve", fx.decisionBlocked);
    expect((await api.approve(12)).blocked).toBe("kill switch manual_pause is tripped");
    api.respond("putSetting", (key, value) => ({ ...(fx.settingsItems[0] as SettingOut), key, value }));
    expect(await api.putSetting("approval_mode", "auto")).toMatchObject({ key: "approval_mode", value: "auto" });
  });

  it("looks proposals and positions up by id, with 404 for unknown ones", async () => {
    const api = new FakeApiClient();
    expect((await api.proposal(11)).decided_via).toBe("web");
    expect((await api.position(3)).trade?.id).toBe(fx.trade.id);
    expect((await api.position(4)).trade).toBeNull();
    await expect(api.proposal(999)).rejects.toMatchObject({ status: 404, code: "not_found" });
  });

  it("answers live and control with the live fixtures by default (DB-T1)", async () => {
    const api = new FakeApiClient();
    const live = await api.live({ range: "today" });
    expect(live).toEqual(lfx.liveOut);
    expect(live.positions).toHaveLength(1);
    expect(await api.control()).toEqual(lfx.controlOut);
    expect(api.calls).toEqual([
      ["live", [{ range: "today" }]],
      ["control", []],
    ]);
    api.set("live", lfx.liveEmptyDay);
    expect((await api.live({})).positions).toEqual([]);
  });

  it("builds same-origin URLs", () => {
    const api = new FakeApiClient();
    expect(api.streamUrl()).toBe("/api/stream");
    expect(api.exportTradesUrl({ run: "live", from: "2026-10-05", to: undefined })).toBe(
      "/api/export/trades.csv?run=live&from=2026-10-05",
    );
  });
});

describe("ApiError and queryString", () => {
  it("ApiError carries status, code, fields and request id", () => {
    const e = new ApiError(422, "validation", "Invalid input", [{ loc: ["body", "reason"], msg: "too short" }], "req-1");
    expect(e).toBeInstanceOf(Error);
    expect(e.message).toBe("Invalid input");
    expect(e.status).toBe(422);
    expect(e.fields?.[0]?.msg).toBe("too short");
    expect(e.requestId).toBe("req-1");
    const plain = new ApiError(0, "network", "Can't reach the server");
    expect(plain.fields).toBeUndefined();
    expect(plain.requestId).toBeUndefined();
  });

  it("queryString drops empty values and encodes the rest", () => {
    expect(queryString({})).toBe("");
    expect(queryString({ a: undefined, b: null, c: "" })).toBe("");
    expect(queryString({ job: "token-refresh", limit: 5, force: false })).toBe("?job=token-refresh&limit=5&force=false");
    expect(queryString({ q: "a b&c" })).toBe("?q=a+b%26c");
  });
});
