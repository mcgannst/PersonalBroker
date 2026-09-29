import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../../test/render";
import SettingsPage from "../Settings";

describe("Settings page", () => {
  it("renders every section in order, each with its anchor", async () => {
    const { container } = renderWithProviders(<SettingsPage />, { route: "/settings" });
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    await screen.findByRole("article", { name: "risk_pct" });
    const ids = Array.from(container.querySelectorAll("section[id]")).map((s) => s.id);
    expect(ids).toEqual(["engine", "strategies", "settings", "questrade", "telegram", "security"]);
  });

  it("DB-T11: no approval-mode or kill-switch card; one card links to /control; the Telegram test stays", async () => {
    const { api } = renderWithProviders(<SettingsPage />, { route: "/settings" });
    await screen.findByRole("article", { name: "risk_pct" });
    expect(screen.queryByRole("heading", { name: "Approval mode" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Kill switches" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Auto" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Reset / })).toBeNull();
    expect(api.callsTo("killswitches")).toEqual([]);
    const moved = document.getElementById("engine")!;
    expect(within(moved).getByRole("heading", { name: "Engine controls moved" })).toBeInTheDocument();
    expect(within(moved).getByRole("link", { name: "Control page" })).toHaveAttribute("href", "/control");
    expect(within(document.getElementById("telegram")!).getByRole("button", { name: "Send a test message" })).toBeInTheDocument();
  });

  it("scrolls to the section named in the hash", async () => {
    const scroll = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    try {
      renderWithProviders(<SettingsPage />, { route: "/settings#telegram" });
      await screen.findByRole("article", { name: "risk_pct" });
      expect(scroll).toHaveBeenCalled();
      expect(scroll.mock.contexts[0]).toHaveProperty("id", "telegram");
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});
