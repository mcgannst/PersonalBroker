// The live fixtures (DB-T1) are what DB-T7/T8/T9/T11 test against: check they hold what the plan promises.
import { describe, expect, it } from "vitest";

import * as lfx from "./liveFixtures";

describe("live fixtures", () => {
  it("liveOut is a session day with one live position and every part present", () => {
    const live = lfx.liveOut;
    expect(live.session.is_session).toBe(true);
    expect(live.positions).toHaveLength(1);
    expect(live.positions?.[0]?.mark_state).toBe("live");
    expect(live.periods?.map((p) => p.period)).toEqual(["today", "week", "run"]);
    expect(live.books?.ok).toBe(true);
    expect(live.equity?.points).toHaveLength(30);
    expect(live.activity).toHaveLength(12);
    expect(new Set(live.activity?.map((a) => a.chip))).toEqual(new Set(["trades", "proposals", "alerts", "scan"]));
    const ts = live.activity?.map((a) => a.ts) ?? [];
    expect([...ts].sort().reverse()).toEqual(ts); // newest first
    expect(live.rejections?.rules).toHaveLength(2);
    expect(live.timeline?.length).toBeGreaterThan(0);
    expect(live.pending).toEqual([]);
    expect(live.part_errors).toEqual([]);
  });

  it("positionsN builds 0, 1 and 20 distinct positions, stale or near the stop on request", () => {
    expect(lfx.positionsN(0)).toEqual([]);
    expect(lfx.positionsN(1)).toHaveLength(1);
    const twenty = lfx.positionsN(20);
    expect(new Set(twenty.map((p) => p.id)).size).toBe(20);
    expect(new Set(twenty.map((p) => p.ticker)).size).toBe(20);
    expect(twenty.every((p) => p.mark_state === "live")).toBe(true);
    const stale = lfx.positionsN(3, { staleMarks: true });
    expect(stale.map((p) => p.mark_state)).toEqual(["stale", "stale", "missing"]);
    expect(stale[2]?.mark).toBeNull();
    expect(lfx.positionsN(4, { nearStop: true }).every((p) => p.near_stop)).toBe(true);
  });

  it("liveWith copies, liveEmptyDay is empty, liveAllPartsFailed has an error per null part", () => {
    const changed = lfx.liveWith({ positions: lfx.positionsN(20) });
    expect(changed.positions).toHaveLength(20);
    expect(lfx.liveOut.positions).toHaveLength(1);
    expect(lfx.liveEmptyDay.session.is_session).toBe(false);
    expect(lfx.liveEmptyDay.session_day).toBe("2026-10-09");
    expect(lfx.liveEmptyDay.positions).toEqual([]);
    expect(lfx.liveEmptyDay.activity).toEqual([]);
    expect(lfx.liveEmptyDay.rejections?.rules).toEqual([]);
    const failed = lfx.liveAllPartsFailed;
    for (const part of lfx.LIVE_PARTS) expect(failed[part], part).toBeNull();
    expect(failed.part_errors.map((e) => e.part)).toEqual([...lfx.LIVE_PARTS]);
  });

  it("the control fixtures: complete, a 95 s old heartbeat, no soak yet", () => {
    expect(lfx.controlOut.part_errors).toEqual([]);
    expect(lfx.controlOut.health?.opening_bars?.complete).toBe(true);
    expect(lfx.controlStaleWorker.health?.worker.age_seconds).toBe(95);
    expect(lfx.controlStaleWorker.health?.worker_stale).toBe(true);
    expect(lfx.controlNoSoak.soak?.day_one).toBeNull();
    expect(lfx.controlWith({ engine: null }).engine).toBeNull();
  });

  it("withXssText puts the payload in free text only", () => {
    const { live, control } = lfx.withXssText();
    expect(live.activity?.every((a) => a.text.endsWith(lfx.XSS))).toBe(true);
    expect(live.positions?.[0]?.ticker).toContain(lfx.XSS);
    expect(live.rejections?.rules[0]?.tickers[0]).toContain(lfx.XSS);
    expect(live.timeline?.[0]?.label).toContain(lfx.XSS);
    expect(live.activity?.[0]?.kind).toBe("exit"); // literals untouched
    expect(live.equity?.points[0]?.source).toBe("snapshot");
    expect(control.errors?.[0]?.message).toContain(lfx.XSS);
    expect(control.errors?.[0]?.source).toContain(lfx.XSS);
    expect(control.schedule?.[0]?.summary).toContain(lfx.XSS);
    expect(control.strategies?.[0]?.key).toBe("orb_sip");
    expect(lfx.liveOut.activity?.[0]?.text).not.toContain(lfx.XSS);
  });
});
