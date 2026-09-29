import { describe, expect, it } from "vitest";

import { prefixesFor, qk, TOPIC_KEYS } from "./queryKeys";
import { TOPICS } from "./types";

describe("query keys", () => {
  it("every key starts with its resource name", () => {
    expect(qk.dashboard()).toEqual(["dashboard"]);
    expect(qk.proposal(5)).toEqual(["proposal", 5]);
    expect(qk.trades({ offset: 50 })).toEqual(["trades", { offset: 50 }]);
    expect(qk.candidates()).toEqual(["candidates", null]);
    expect(qk.watchlist("2026-10-07")).toEqual(["watchlist", "2026-10-07"]);
    expect(qk.me()[0]).toBe("me");
    expect(qk.meta()[0]).toBe("meta");
  });

  it("TOPIC_KEYS covers every topic and names only real resources", () => {
    const resources = new Set(Object.keys(qk));
    expect(Object.keys(TOPIC_KEYS).sort()).toEqual([...TOPICS].sort());
    for (const prefixes of Object.values(TOPIC_KEYS)) {
      for (const prefix of prefixes) expect(resources.has(prefix)).toBe(true);
    }
  });

  it("proposals invalidate the dashboard and proposal queries only", () => {
    expect(TOPIC_KEYS.proposals).toEqual(["dashboard", "proposals", "proposal", "position"]);
    expect(prefixesFor(["proposals", "killswitch"])).toEqual(["dashboard", "proposals", "proposal", "position", "killswitches"]);
  });

  it("the live dashboard and Control keys sit under the dashboard and system prefixes (DB-T1)", () => {
    expect(qk.live()).toEqual(["dashboard", "live", {}]);
    expect(qk.live({ range: "run", expand: "7" })).toEqual(["dashboard", "live", { range: "run", expand: "7" }]);
    expect(qk.control()).toEqual(["system", "control"]);
    expect(TOPIC_KEYS.marks).toEqual(["dashboard"]);
    expect(TOPIC_KEYS.activity).toEqual(["dashboard"]);
    expect(prefixesFor(["marks", "activity"])).toEqual(["dashboard"]);
  });

  it("a new trade refreshes the journal (its days show trade counts and realized P&L)", () => {
    expect(TOPIC_KEYS.trades).toContain("journal");
  });
});
