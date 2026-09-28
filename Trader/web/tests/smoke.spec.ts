// The end-to-end smoke (P4-T19, SPEC §16). Two modes, chosen by SMOKE_MODE:
// - local: the throwaway stack from docker/smoke.sh, seeded by tests.e2e.seed_smoke. Log in, the dashboard
//   shows the pending AAA entry, tap Approve, the card reports "Approved" and leaves the pending list, open the
//   seeded position's chain at /trades?position=<id>, /system renders, log out.
// - live: trader-dev, READ-ONLY. Log in, the dashboard renders with its session phase, every page renders
//   without an error box (the Replay page included, and a finished replay's comparison when SMOKE_REPLAY_ID
//   names one), log out. It never clicks Approve, Reject, Save, Run, New replay or Cancel.
// Both modes check at 390 px that no page scrolls sideways.
// Credentials come only from the environment (never printed, never typed into a recorded trace in live mode):
// SMOKE_USER/SMOKE_PASSWORD, else TRADER_WEB_USER/TRADER_WEB_PASSWORD, else ADMIN_USERNAME/ADMIN_PASSWORD_INITIAL.
import { expect, test, type Page } from "@playwright/test";

const MODE = process.env.SMOKE_MODE === "live" ? "live" : "local";

interface Credentials {
  user: string;
  password: string;
}

function credentials(): Credentials {
  const env = process.env;
  const pairs: Array<[string | undefined, string | undefined]> = [
    [env.SMOKE_USER, env.SMOKE_PASSWORD],
    [env.TRADER_WEB_USER, env.TRADER_WEB_PASSWORD],
    [env.ADMIN_USERNAME, env.ADMIN_PASSWORD_INITIAL],
  ];
  for (const [user, password] of pairs) {
    if (user && password) return { user, password };
  }
  throw new Error("smoke: no web credentials in the environment");
}

/** The Friday of the current trading week in New York (the Reports page's `week` parameter), YYYY-MM-DD. */
function thisFriday(now: Date = new Date()): string {
  const etToday = now.toLocaleDateString("en-CA", { timeZone: "America/New_York" }); // YYYY-MM-DD
  const d = new Date(`${etToday}T00:00:00Z`);
  const day = d.getUTCDay(); // 0 Sunday .. 6 Saturday
  const offset = day === 6 ? 6 : 5 - day; // Saturday belongs to the week ending the next Friday
  d.setUTCDate(d.getUTCDate() + offset);
  return d.toISOString().slice(0, 10);
}

async function noSideScroll(page: Page): Promise<void> {
  const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  expect(fits, `${page.url()} scrolls sideways at 390 px`).toBe(true);
}

/** Wait for the page heading and for every "Loading" to go, then: no error box, no sideways scroll. */
async function rendered(page: Page, heading: string): Promise<void> {
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  await expect(page.getByRole("status", { name: "Loading" })).toHaveCount(0, { timeout: 20_000 });
  await expect(page.locator(".error-box"), `${page.url()} shows an error box`).toHaveCount(0);
  await noSideScroll(page);
}

async function login(page: Page): Promise<void> {
  const { user, password } = credentials();
  await page.goto("/login");
  await expect(page.getByRole("heading", { level: 1, name: "Login" })).toBeVisible();
  await noSideScroll(page);
  await page.getByLabel("Username").fill(user);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page).toHaveURL(/\/dashboard/);
}

async function logout(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { level: 1, name: "Login" })).toBeVisible();
  // the session is gone: a protected page sends us back to the login
  await page.goto("/system");
  await expect(page).toHaveURL(/\/login/);
}

test.describe("smoke", () => {
  test.skip(MODE !== "local", "local mode only (it approves a seeded proposal)");

  test("local: login, approve the seeded proposal, the position chain, system, logout", async ({ page }) => {
    await login(page);
    await rendered(page, "Dashboard");

    const card = page.getByRole("article", { name: /^Proposal \d+: ENTRY AAA$/ });
    await expect(card).toHaveCount(1);
    await card.getByRole("button", { name: "Approve" }).click();
    // The card reports the outcome, then leaves the pending list when the dashboard refetches; the
    // decision notice keeps the message.
    await expect(page.getByText(/AAA ENTRY: Approved|^Approved$/).first()).toBeVisible();
    await expect(card).toHaveCount(0);
    await expect(page.getByText("AAA ENTRY: Approved")).toBeVisible();
    await noSideScroll(page);

    // The seeded closed BBB trade is in the history; its position chain is then opened by a fresh page load
    // of the deep link, the way a Telegram link opens it.
    await page.goto("/trades");
    await rendered(page, "Trades");
    const link = page.getByRole("link", { name: "BBB", exact: true });
    await expect(link).toHaveCount(1);
    const href = await link.getAttribute("href");
    const match = /\/trades\?position=(\d+)$/.exec(href ?? "");
    expect(match, `the BBB row links to its position (${href})`).not.toBeNull();
    const positionId = match![1]!;
    await page.goto(`/trades?position=${positionId}`);
    await expect(page.getByText(`position #${positionId}`)).toBeVisible();
    for (const section of ["Signal", "Proposals", "Orders", "Fills", "Result"]) {
      await expect(page.getByRole("region", { name: section })).toBeVisible();
    }
    await expect(page.getByRole("status", { name: "Loading" })).toHaveCount(0, { timeout: 20_000 });
    await noSideScroll(page);

    await page.goto("/system");
    await rendered(page, "System");

    await logout(page);
  });
});

test.describe("smoke (live, read-only)", () => {
  test.skip(MODE !== "live", "live mode only");

  test("live: login, every page renders without an error, logout", async ({ page }) => {
    await login(page);
    await rendered(page, "Dashboard");
    await expect(page.getByRole("region", { name: "Session" })).toContainText(
      /Open|Pre-market|After close|Market closed today/,
    );

    const pages: Array<[string, string]> = [
      ["/candidates", "Candidates"],
      ["/trades", "Trades"],
      ["/performance", "Performance"],
      ["/journal", "Journal"],
      ["/settings", "Settings"],
      ["/system", "System"],
      [`/reports?week=${thisFriday()}`, "Reports"],
      ["/replay", "Replay"],
    ];
    for (const [path, heading] of pages) {
      await page.goto(path);
      await rendered(page, heading);
    }

    // P5-T18 LIVE 4: a finished replay's page shows its comparison with the live run (read only: the page's
    // "New replay" and "Cancel" buttons are never clicked).
    const replayId = process.env.SMOKE_REPLAY_ID;
    if (replayId) {
      await page.goto(`/replay?id=${encodeURIComponent(replayId)}`);
      await rendered(page, "Replay");
      await expect(page.getByRole("region", { name: `Replay ${replayId}` })).toBeVisible();
      await expect(page.getByText("Compared with live")).toBeVisible();
    }

    await logout(page);
  });
});
