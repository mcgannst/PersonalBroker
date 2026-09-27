// P4-T12 tests 3-5 and 7-9: routes and deep links, login, layout, the time-zone check (FakeApiClient, no
// network). The page modules are replaced by markers so these tests do not depend on the pages' own work.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppRoutes } from "./App";
import { ApiError } from "./api/client";
import { displayZoneInfo, fmtTime, setDisplayZone, zoneOffsetMinutes } from "./lib/format";
import { notifyUnauthorized } from "./layout/AuthContext";
import { safeNext } from "./layout/safeNext";
import { serverSkewMs } from "./layout/serverTime";
import { FakeApiClient } from "./test/fakeApi";
import { metaOut } from "./test/fixtures";
import { renderWithProviders } from "./test/render";

vi.mock("./lib/format", async (importOriginal) => {
  const mod = await importOriginal<typeof import("./lib/format")>();
  return { ...mod, zoneOffsetMinutes: vi.fn(mod.zoneOffsetMinutes) };
});

function pageMock(name: string) {
  return { default: () => <h1 data-testid="page">{name} page</h1> };
}

vi.mock("./pages/Dashboard", () => pageMock("Dashboard"));
vi.mock("./pages/Candidates", () => pageMock("Candidates"));
vi.mock("./pages/Trades", () => pageMock("Trades"));
vi.mock("./pages/Performance", () => pageMock("Performance"));
vi.mock("./pages/Journal", () => pageMock("Journal"));
vi.mock("./pages/Reports", () => pageMock("Reports"));
vi.mock("./pages/Settings", () => pageMock("Settings"));
vi.mock("./pages/System", () => pageMock("System"));

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readyState = 0;
  closed = false;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(): void {}
  close(): void {
    this.closed = true;
    this.readyState = 2;
  }
}

const unauthorized = () => new ApiError(401, "unauthorized", "Please log in");

function loggedOutApi(): FakeApiClient {
  return new FakeApiClient().fail("me", unauthorized());
}

async function page(): Promise<string> {
  return (await screen.findByTestId("page")).textContent ?? "";
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.mocked(zoneOffsetMinutes).mockReturnValue(metaOut.tz_offset_minutes);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("safeNext (the login redirect target)", () => {
  it.each([
    ["/\\evil.com"],
    ["//evil.com"],
    ["/%5Cevil"],
    ["/%5cevil"],
    ["/%2F%2Fevil.com"],
    ["https://evil.com"],
    ["javascript:alert(1)"],
    ["/ok\u0000"],
    ["/ok\ttab"],
    ["/ok\\x"],
    [""],
    [null],
    ["dashboard"],
  ])("rejects %j", (raw) => {
    expect(safeNext(raw)).toBe("/dashboard");
  });

  it.each([["/trades?position=3"], ["/dashboard?proposal=12"], ["/journal?date=2026-10-06"], ["/system"]])("accepts %j", (raw) => {
    expect(safeNext(raw)).toBe(raw);
  });

  it("never sends a logged-in visitor back to the login page", () => {
    expect(safeNext("/login?next=%2Fsystem")).toBe("/dashboard");
  });
});

describe("routes and deep links (tests 3-4)", () => {
  it("test 3: a logged-out deep link goes to login with next, and logging in lands on it", async () => {
    const api = loggedOutApi();
    const user = userEvent.setup();
    const r = renderWithProviders(<AppRoutes />, { api, route: "/dashboard?proposal=12" });
    await screen.findByRole("heading", { name: "Login" });
    expect(r.location().pathname).toBe("/login");
    expect(r.location().search).toBe("?next=%2Fdashboard%3Fproposal%3D12");

    await user.type(screen.getByLabelText(/username/i), "stephen");
    await user.type(screen.getByLabelText(/^password/i), "correct horse");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(await page()).toBe("Dashboard page");
    expect(r.location().pathname).toBe("/dashboard");
    expect(r.location().search).toBe("?proposal=12");
    expect(api.callsTo("login")).toEqual([[{ username: "stephen", password: "correct horse" }]]);
  });

  it.each([["https://evil.example"], ["//evil.example"], ["/\\evil.example"]])("test 3: a next of %j lands on /dashboard", async (next) => {
    const api = loggedOutApi();
    const user = userEvent.setup();
    const r = renderWithProviders(<AppRoutes />, { api, route: `/login?next=${encodeURIComponent(next)}` });
    await user.type(await screen.findByLabelText(/username/i), "stephen");
    await user.type(screen.getByLabelText(/^password/i), "pw");
    await user.click(screen.getByRole("button", { name: /log in/i }));
    expect(await page()).toBe("Dashboard page");
    expect(r.location().pathname).toBe("/dashboard");
    expect(r.location().search).toBe("");
  });

  it.each([
    ["/trades?position=3", "Trades page"],
    ["/journal?date=2026-10-06", "Journal page"],
    ["/reports?week=2026-10-09", "Reports page"],
    ["/system", "System page"],
    ["/dashboard?proposal=12", "Dashboard page"],
    ["/candidates", "Candidates page"],
    ["/performance", "Performance page"],
    ["/settings", "Settings page"],
  ])("test 4: %s renders its page when logged in", async (route, name) => {
    const r = renderWithProviders(<AppRoutes />, { route });
    expect(await page()).toBe(name);
    expect(`${r.location().pathname}${r.location().search}`).toBe(route);
  });

  it("/ goes to the dashboard", async () => {
    const r = renderWithProviders(<AppRoutes />, { route: "/" });
    expect(await page()).toBe("Dashboard page");
    expect(r.location().pathname).toBe("/dashboard");
  });

  it("an unknown path shows Not found with a link to the dashboard", async () => {
    renderWithProviders(<AppRoutes />, { route: "/nope" });
    expect(await screen.findByRole("heading", { name: /not found/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /dashboard/i })).toHaveAttribute("href", "/dashboard");
  });

  it("a 401 during the session sends the visitor to login, keeping where they were", async () => {
    const r = renderWithProviders(<AppRoutes />, { route: "/trades?position=3" });
    await page();
    act(() => notifyUnauthorized());
    await screen.findByRole("heading", { name: "Login" });
    expect(r.location().pathname).toBe("/login");
    expect(r.location().search).toBe("?next=%2Ftrades%3Fposition%3D3");
  });

  it("visiting /login while logged in goes to next", async () => {
    const r = renderWithProviders(<AppRoutes />, { route: "/login?next=%2Fsystem" });
    expect(await page()).toBe("System page");
    expect(r.location().pathname).toBe("/system");
  });

  it("an unreachable server shows the error with Retry", async () => {
    const api = new FakeApiClient().fail("me", new ApiError(0, "network", "Can't reach the server"));
    const user = userEvent.setup();
    renderWithProviders(<AppRoutes />, { api, route: "/dashboard" });
    expect(await screen.findByText("Can't reach the server")).toBeInTheDocument();
    api.succeed("me");
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(await page()).toBe("Dashboard page");
  });
});

describe("login page (test 5)", () => {
  it("shows the server's 429 message and clears the password", async () => {
    const api = loggedOutApi().fail("login", new ApiError(429, "rate_limited", "Too many attempts. Try again in 60 s."));
    const user = userEvent.setup();
    renderWithProviders(<AppRoutes />, { api, route: "/login" });
    await user.type(await screen.findByLabelText(/username/i), "stephen");
    await user.type(screen.getByLabelText(/^password/i), "secret-pw");
    await user.click(screen.getByRole("button", { name: /log in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts. Try again in 60 s.");
    expect(screen.getByLabelText(/^password/i)).toHaveValue("");
    expect(screen.queryByDisplayValue("secret-pw")).toBeNull();
  });

  it("sends the code only when it is filled", async () => {
    const api = loggedOutApi().fail("login", new ApiError(401, "unauthorized", "Wrong username or password"));
    const user = userEvent.setup();
    renderWithProviders(<AppRoutes />, { api, route: "/login" });
    await user.type(await screen.findByLabelText(/username/i), "stephen");
    await user.type(screen.getByLabelText(/^password/i), "pw1");
    await user.click(screen.getByRole("button", { name: /log in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong username or password");

    await user.type(screen.getByLabelText(/^password/i), "pw2");
    await user.type(screen.getByLabelText(/code/i), "123456");
    await user.click(screen.getByRole("button", { name: /log in/i }));
    await waitFor(() => expect(api.callsTo("login")).toHaveLength(2));
    const [first, second] = api.callsTo("login") as [[Record<string, unknown>], [Record<string, unknown>]];
    expect(first[0]).toEqual({ username: "stephen", password: "pw1" });
    expect("totp" in first[0]).toBe(false);
    expect(second[0]).toEqual({ username: "stephen", password: "pw2", totp: "123456" });
  });
});

describe("layout (tests 7 and 9)", () => {
  it("test 9: the header shows DEV for app_env dev", async () => {
    renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    const header = await screen.findByRole("banner");
    expect(await within(header).findByText("DEV")).toBeInTheDocument();
    expect(within(header).getByText("Trader")).toBeInTheDocument();
  });

  it("test 9: the header shows no DEV badge for prod", async () => {
    const api = new FakeApiClient({ meta: { ...metaOut, app_env: "prod" } });
    renderWithProviders(<AppRoutes />, { api, route: "/dashboard" });
    const header = await screen.findByRole("banner");
    await waitFor(() => expect(api.callsTo("meta")).toHaveLength(1));
    await page();
    expect(within(header).queryByText("DEV")).toBeNull();
  });

  it("test 9 and 7: Logout calls logout(), returns to /login and closes the stream", async () => {
    const user = userEvent.setup();
    const r = renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    await page();
    expect(FakeEventSource.instances).toHaveLength(1);
    const es = FakeEventSource.instances[0]!;
    expect(es.closed).toBe(false);
    await user.click(screen.getByRole("button", { name: /log out/i }));
    await screen.findByRole("heading", { name: "Login" });
    expect(r.api.callsTo("logout")).toHaveLength(1);
    expect(r.location().pathname).toBe("/login");
    expect(es.closed).toBe(true);
  });

  it("shows the live dot as reconnecting until the stream says hello", async () => {
    renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    await page();
    expect(screen.getByRole("status", { name: /reconnecting/i })).toBeInTheDocument();
  });

  it("navigation: every page is at most two taps away on a phone", async () => {
    const user = userEvent.setup();
    const r = renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    await page();
    const tabs = screen.getByRole("navigation", { name: /tabs/i });
    for (const name of ["Dashboard", "Candidates", "Trades", "Journal"]) {
      expect(within(tabs).getByRole("link", { name })).toBeInTheDocument();
    }
    await user.click(within(tabs).getByRole("button", { name: /more/i }));
    const more = screen.getByRole("menu");
    for (const name of ["Performance", "Settings", "System", "Reports"]) {
      expect(within(more).getByRole("menuitem", { name })).toBeInTheDocument();
    }
    await user.click(within(more).getByRole("menuitem", { name: "System" }));
    expect(await page()).toBe("System page");
    expect(r.location().pathname).toBe("/system");
    expect(screen.queryByRole("menu")).toBeNull();

    const side = screen.getByRole("navigation", { name: /main/i });
    for (const name of ["Dashboard", "Candidates", "Trades", "Performance", "Journal", "Reports", "Settings", "System"]) {
      expect(within(side).getByRole("link", { name })).toBeInTheDocument();
    }
  });
});

describe("time display (test 8)", () => {
  it("switches to the server's fixed offset when the browser's zone data disagrees", async () => {
    vi.mocked(zoneOffsetMinutes).mockReturnValue(-420);
    setDisplayZone({ zone: "UTC" });
    expect(fmtTime("2026-10-06T13:35:05Z")).toBe("13:35 MT");
    renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    await page();
    await waitFor(() => expect(displayZoneInfo().browserOffsetMinutes).toBe(-420));
    expect(displayZoneInfo()).toEqual({ mode: "fixed", browserOffsetMinutes: -420, serverOffsetMinutes: -360 });
    expect(vi.mocked(zoneOffsetMinutes)).toHaveBeenCalledWith(metaOut.tz_display, metaOut.server_time);
    expect(fmtTime("2026-10-06T13:35:05Z")).toBe("07:35 MT");
  });

  it("uses the named zone when the offsets agree", async () => {
    vi.mocked(zoneOffsetMinutes).mockReturnValue(-360);
    renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    await page();
    await waitFor(() => expect(displayZoneInfo().mode).toBe("zone"));
    expect(displayZoneInfo()).toEqual({ mode: "zone", browserOffsetMinutes: -360, serverOffsetMinutes: -360 });
  });

  it("falls back to the fixed offset when the browser does not know the zone", async () => {
    vi.mocked(zoneOffsetMinutes).mockImplementation(() => {
      throw new RangeError("Invalid time zone");
    });
    setDisplayZone({ zone: "UTC" });
    renderWithProviders(<AppRoutes />, { route: "/dashboard" });
    await page();
    await waitFor(() => expect(vi.mocked(zoneOffsetMinutes)).toHaveBeenCalled());
    // format.ts's check needs two numbers, so an unknown zone is recorded as fixed mode with no check.
    await waitFor(() => expect(displayZoneInfo().mode).toBe("fixed"));
    expect(fmtTime("2026-10-06T13:35:05Z")).toBe("07:35 MT");
  });

  it("keeps the server clock skew for countdowns", async () => {
    const serverTime = new Date(Date.now() + 30_000).toISOString();
    const api = new FakeApiClient({ meta: { ...metaOut, server_time: serverTime } });
    renderWithProviders(<AppRoutes />, { api, route: "/dashboard" });
    await page();
    await waitFor(() => expect(Math.abs(serverSkewMs() - 30_000)).toBeLessThan(2_000));
  });
});
