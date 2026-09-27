// Vitest setup (vite.config.ts `setupFiles`): jest-dom matchers, DOM cleanup, and the time formatter in
// fixed-offset mode (UTC-6, "MT") so no test depends on the host's time-zone data.
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

import { setDisplayZone } from "../lib/format";

export const TEST_OFFSET_MINUTES = -360;

// Some page tests render 1000-row tables in jsdom; they take about a second alone but several when the
// whole suite runs in parallel on a busy machine, so the default 5 s per test is too tight for the gate.
vi.setConfig({ testTimeout: 20_000 });

beforeEach(() => {
  setDisplayZone({ fixedOffsetMinutes: TEST_OFFSET_MINUTES, label: "MT" });
});

afterEach(() => {
  cleanup();
});
