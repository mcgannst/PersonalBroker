// OPTSIM-T15: the Options page's tabs and deep links, its route (behind login) and its place in the navigation.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes } from "../../App";
import { ApiError } from "../../api/client";
import { NAV_ITEMS } from "../../layout/Layout";
import { FakeApiClient } from "../../test/fakeApi";
import { FakeOptionsApiClient, noOptionsRun } from "../../test/optionsFakeApi";
import OptionsPage, { OPTION_TABS, tabFrom } from "../Options";
import { renderOptions } from "./testRender";

class FakeEventSource {
  readyState = 0;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  addEventListener(): void {}
  close(): void {}
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function selectedTab(): string {
  return screen.getByRole("tab", { selected: true }).textContent ?? "";
}

describe("Options page", () => {
  it("has the seven tabs and opens on Account", async () => {
    renderOptions(<OptionsPage />, { route: "/options" });
    expect(screen.getByRole("heading", { level: 1, name: "Options" })).toBeInTheDocument();
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Account", "Positions", "Trade", "Orders", "Strategies", "Activity", "Settings"]);
    expect(OPTION_TABS).toHaveLength(7);
    expect(selectedTab()).toBe("Account");
    expect(await screen.findByTestId("opt-account-value")).toBeInTheDocument();
  });

  it("switching tabs shows the tab and keeps it in the URL", async () => {
    const user = userEvent.setup();
    const r = renderOptions(<OptionsPage />, { route: "/options" });
    await user.click(screen.getByRole("tab", { name: "Orders" }));
    expect(selectedTab()).toBe("Orders");
    expect(r.location().search).toBe("?tab=orders");
    expect(await screen.findByRole("article", { name: "Order 31" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Account" }));
    expect(r.location().search).toBe("");
    expect(await screen.findByTestId("opt-account-value")).toBeInTheDocument();
  });

  it.each([
    ["/options?tab=positions", "Positions"],
    ["/options?tab=trade", "Trade"],
    ["/options?tab=strategies", "Strategies"],
    ["/options?tab=activity", "Activity"],
    ["/options?tab=settings", "Settings"],
    ["/options?tab=nope", "Account"],
  ])("deep link %s opens %s", (route, name) => {
    renderOptions(<OptionsPage />, { route });
    expect(selectedTab()).toBe(name);
  });

  it("tabFrom accepts only the known tabs", () => {
    expect(tabFrom("orders")).toBe("orders");
    expect(tabFrom(null)).toBe("account");
    expect(tabFrom("__proto__")).toBe("account");
  });

  it("?prompt= marks that prompt above the tabs, on any tab", async () => {
    renderOptions(<OptionsPage />, { route: "/options?tab=orders&prompt=13" });
    const prompt = await screen.findByRole("article", { name: "Approve SOFI?" });
    expect(prompt).toHaveClass("is-focused");
    expect(screen.getByRole("article", { name: "Would you buy F today with fresh cash?" })).not.toHaveClass("is-focused");
    expect(selectedTab()).toBe("Orders");
  });

  it("without an options run it says how to start one instead of an error", async () => {
    const opt = new FakeOptionsApiClient().fail("optAccount", noOptionsRun()).fail("optPrompts", noOptionsRun());
    renderOptions(<OptionsPage />, { route: "/options", opt });
    expect(await screen.findByText(/No options run yet/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("the /options route and the navigation", () => {
  it("Options comes right after Dashboard in the navigation", () => {
    const labels = NAV_ITEMS.map((i) => i.label);
    expect(labels.indexOf("Options")).toBe(labels.indexOf("Dashboard") + 1);
    expect(NAV_ITEMS.find((i) => i.label === "Options")?.to).toBe("/options");
  });

  it("renders the page when logged in, with Options in the side navigation", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const r = renderOptions(<AppRoutes />, { route: "/options?tab=orders" });
    expect(await screen.findByRole("heading", { level: 1, name: "Options" })).toBeInTheDocument();
    expect(`${r.location().pathname}${r.location().search}`).toBe("/options?tab=orders");
    const side = screen.getByRole("navigation", { name: /main/i });
    expect(within(side).getByRole("link", { name: "Options" })).toHaveAttribute("href", "/options");
  });

  it("sends a logged-out visitor to the login page, keeping the deep link", async () => {
    const api = new FakeApiClient().fail("me", new ApiError(401, "unauthorized", "Please log in"));
    const opt = new FakeOptionsApiClient();
    const r = renderOptions(<AppRoutes />, { api, opt, route: "/options?prompt=12" });
    await screen.findByRole("heading", { name: "Login" });
    expect(r.location().pathname).toBe("/login");
    expect(r.location().search).toBe("?next=%2Foptions%3Fprompt%3D12");
    await waitFor(() => expect(opt.calls).toHaveLength(0));
  });
});
