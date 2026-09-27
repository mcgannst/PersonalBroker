import { afterEach, describe, expect, it, vi } from "vitest";

import {
  displayZoneInfo,
  fmtDate,
  fmtDateTime,
  fmtDuration,
  fmtMoney,
  fmtPct,
  fmtPrice,
  fmtR,
  fmtTime,
  secondsUntil,
  setDisplayZone,
  zoneOffsetMinutes,
} from "./format";

// src/test/setup.ts puts the formatter in fixed-offset mode (-360 min, "MT") before every test.

describe("times (acceptance test 1)", () => {
  it("formats in the fixed -360 mode without the host's zone data", () => {
    expect(fmtTime("2026-10-06T13:35:05Z")).toBe("07:35 MT");
    expect(fmtDateTime("2026-10-06T13:35:05Z")).toBe("2026-10-06 07:35 MT");
  });

  it("formats with an explicit IANA zone that still changes its clocks", () => {
    setDisplayZone({ zone: "America/Denver" });
    // 2026-11-02 is after the US fall-back (2026-11-01): Denver is UTC-7.
    expect(fmtTime("2026-11-02T14:35:05Z")).toBe("07:35 MT");
    // Summer: UTC-6.
    expect(fmtTime("2026-10-06T13:35:05Z")).toBe("07:35 MT");
  });

  it("crosses midnight correctly in both modes", () => {
    expect(fmtDateTime("2026-10-07T03:10:00Z")).toBe("2026-10-06 21:10 MT");
    setDisplayZone({ zone: "America/Denver" });
    expect(fmtDateTime("2026-11-03T03:10:00Z")).toBe("2026-11-02 20:10 MT");
  });

  it("uses the fixed mode's label and offset", () => {
    setDisplayZone({ fixedOffsetMinutes: -420, label: "MST" });
    expect(fmtTime("2026-10-06T13:35:05Z")).toBe("06:35 MST");
  });

  it("shows n/a for a missing or unreadable time", () => {
    expect(fmtTime(null)).toBe("n/a");
    expect(fmtDateTime(undefined)).toBe("n/a");
    expect(fmtTime("not a time")).toBe("n/a");
  });

  it("formats a session date as given", () => {
    expect(fmtDate("2026-10-06")).toBe("2026-10-06");
    expect(fmtDate(null)).toBe("n/a");
  });

  it("zoneOffsetMinutes reads the offset from Intl", () => {
    expect(zoneOffsetMinutes("America/Denver", "2026-11-02T14:35:05Z")).toBe(-420);
    expect(zoneOffsetMinutes("America/Denver", "2026-07-02T14:35:05Z")).toBe(-360);
    expect(zoneOffsetMinutes("UTC", "2026-07-02T14:35:05Z")).toBe(0);
    expect(zoneOffsetMinutes("Asia/Kolkata", "2026-07-02T14:35:05Z")).toBe(330);
  });

  it("displayZoneInfo reports the mode and the offsets the shell compared", () => {
    setDisplayZone({ fixedOffsetMinutes: -360, label: "MT" }, { browserOffsetMinutes: -420, serverOffsetMinutes: -360 });
    expect(displayZoneInfo()).toEqual({ mode: "fixed", browserOffsetMinutes: -420, serverOffsetMinutes: -360 });
    setDisplayZone({ zone: "America/Edmonton" }, { browserOffsetMinutes: -360, serverOffsetMinutes: -360 });
    expect(displayZoneInfo()).toEqual({ mode: "zone", browserOffsetMinutes: -360, serverOffsetMinutes: -360 });
    setDisplayZone({ zone: "America/Edmonton" });
    expect(displayZoneInfo()).toEqual({ mode: "zone", browserOffsetMinutes: null, serverOffsetMinutes: null });
  });
});

describe("numbers (acceptance test 2)", () => {
  it("fmtMoney", () => {
    expect(fmtMoney("-12.3")).toBe("-$12.30");
    expect(fmtMoney(null)).toBe("n/a");
    expect(fmtMoney("1234.56")).toBe("$1,234.56");
    expect(fmtMoney("1234567.004")).toBe("$1,234,567.00");
    expect(fmtMoney("0.005")).toBe("$0.01");
    expect(fmtMoney("-0.004")).toBe("$0.00");
    expect(fmtMoney("720")).toBe("$720.00");
    expect(fmtMoney("1E+3")).toBe("$1,000.00");
    expect(fmtMoney("garbage")).toBe("n/a");
  });

  it("fmtPct", () => {
    expect(fmtPct("0.0123")).toBe("+1.23%");
    expect(fmtPct("-0.05")).toBe("-5.00%");
    expect(fmtPct("0")).toBe("0.00%");
    expect(fmtPct(0.5)).toBe("+50.00%");
    expect(fmtPct(null)).toBe("n/a");
  });

  it("fmtR", () => {
    expect(fmtR("2.1700")).toBe("+2.17R");
    expect(fmtR("-1")).toBe("-1.00R");
    expect(fmtR("0.000")).toBe("0.00R");
    expect(fmtR(null)).toBe("n/a");
  });

  it("fmtPrice keeps 4 dp trimmed to at least 2", () => {
    expect(fmtPrice("21.5600")).toBe("21.56");
    expect(fmtPrice("21.5608")).toBe("21.5608");
    expect(fmtPrice("21.5")).toBe("21.50");
    expect(fmtPrice("21.56085")).toBe("21.5609");
    expect(fmtPrice("21")).toBe("21.00");
    expect(fmtPrice(null)).toBe("n/a");
  });

  it("fmtDuration", () => {
    expect(fmtDuration(200)).toBe("3m 20s");
    expect(fmtDuration(0)).toBe("0s");
    expect(fmtDuration(45.9)).toBe("45s");
    expect(fmtDuration(3600)).toBe("1h 0m");
    expect(fmtDuration(3 * 86400 + 2 * 3600)).toBe("3d 2h");
    expect(fmtDuration(-5)).toBe("0s");
    expect(fmtDuration(null)).toBe("n/a");
  });
});

describe("secondsUntil (acceptance test 3)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("counts by the server's clock", () => {
    vi.useFakeTimers();
    const serverNow = Date.parse("2026-10-06T13:40:00Z");
    // The browser's clock is 30 s fast.
    vi.setSystemTime(serverNow + 30_000);
    const skewMs = serverNow - Date.now(); // server_time - Date.now() = -30 000
    expect(secondsUntil("2026-10-06T13:42:00Z", skewMs)).toBe(120);
  });

  it("never goes below zero", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse("2026-10-06T13:45:00Z"));
    expect(secondsUntil("2026-10-06T13:42:00Z", 0)).toBe(0);
  });
});
