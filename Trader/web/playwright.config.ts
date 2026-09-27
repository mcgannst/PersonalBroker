// Playwright for the end-to-end smoke (P4-T19, SPEC §16): Chromium only, a phone viewport (390×844), no
// retries. `SMOKE_BASE_URL` is the app under test; `SMOKE_MODE` is `local` (the throwaway smoke stack from
// docker/smoke.sh) or `live` (trader-dev, read-only). Traces, videos and screenshots are kept on failure in
// local mode only: a trace records what was typed, the password included, so live runs record nothing.
import { defineConfig, devices } from "@playwright/test";

const live = process.env.SMOKE_MODE === "live";
const baseURL = process.env.SMOKE_BASE_URL ?? "http://127.0.0.1:18000";

export default defineConfig({
  testDir: "tests",
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    viewport: { width: 390, height: 844 },
    trace: live ? "off" : "retain-on-failure",
    video: live ? "off" : "retain-on-failure",
    screenshot: live ? "off" : "only-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
