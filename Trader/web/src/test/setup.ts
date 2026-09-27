// Vitest setup (vite.config.ts `setupFiles`): jest-dom matchers, DOM cleanup, and the time formatter in
// fixed-offset mode (UTC-6, "MT") so no test depends on the host's time-zone data.
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

import { setDisplayZone } from "../lib/format";

export const TEST_OFFSET_MINUTES = -360;

beforeEach(() => {
  setDisplayZone({ fixedOffsetMinutes: TEST_OFFSET_MINUTES, label: "MT" });
});

afterEach(() => {
  cleanup();
});
