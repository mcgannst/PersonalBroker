import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../../test/render";
import SettingsPage from "../Settings";

describe("Settings page", () => {
  it("renders every section in order, each with its anchor", async () => {
    const { container } = renderWithProviders(<SettingsPage />, { route: "/settings" });
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    await screen.findByRole("article", { name: "risk_pct" });
    const ids = Array.from(container.querySelectorAll("section[id]")).map((s) => s.id);
    expect(ids).toEqual(["approval", "killswitches", "strategies", "settings", "questrade", "telegram", "security"]);
  });

  it("scrolls to the section named in the hash", async () => {
    const scroll = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    try {
      renderWithProviders(<SettingsPage />, { route: "/settings#killswitches" });
      await screen.findByRole("article", { name: "risk_pct" });
      expect(scroll).toHaveBeenCalled();
      expect(scroll.mock.contexts[0]).toHaveProperty("id", "killswitches");
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});
