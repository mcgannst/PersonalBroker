// Phone layout, touch targets (P4 Global Constraints: at least 44 px). Buttons carry an inline min-height
// (checked by the gauntlet's phone test); links, <summary> toggles and checkbox rows get theirs from shared
// rules in styles.css, so this test checks that the rules exist and that every such element uses them.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import * as fx from "../test/fixtures";
import { FakeApiClient } from "../test/fakeApi";
import { renderWithProviders } from "../test/render";
import CandidatesPage from "./Candidates";
import ReportsPage from "./Reports";
import SettingsPage from "./Settings";
import SystemPage from "./System";
import TradesPage from "./Trades";

async function settle(): Promise<void> {
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "Loading" })).toHaveLength(0));
}

function readWebFile(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return readFileSync(found, "utf8");
}

/** The declarations of the first rule whose selector list contains `selector`. */
function ruleFor(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = new RegExp(`(?:^|[},\\s])${escaped}\\s*(?:,[^{]*)?\\{([^}]*)\\}`, "m").exec(css);
  return m?.[1] ?? "";
}

/** Link classes that give a 44 px target: the shared one, button-styled links and the app frame's own. */
const TOUCH_LINK_CLASSES = ["link-touch", "btn", "nav-link", "shell-title"];

describe("44 px touch targets for links, <summary> and checkbox rows", () => {
  it("styles.css has the shared rules, each with min-height: var(--touch)", () => {
    const css = readWebFile("src/styles.css");
    expect(css).toMatch(/--touch:\s*44px/);
    expect(ruleFor(css, ".link-touch")).toMatch(/min-height:\s*var\(--touch\)/);
    expect(ruleFor(css, "summary")).toMatch(/min-height:\s*var\(--touch\)/);
    const checkRow = ruleFor(css, ".check-row");
    expect(checkRow).toMatch(/min-height:\s*var\(--touch\)/);
    expect(checkRow).toMatch(/flex-direction:\s*row/);
  });

  it("every link, summary and checkbox on the pages uses them", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    const api = new FakeApiClient({
      system: { ...fx.systemOut, token: { ...fx.tokenOut, ok: false, error: "expired" } },
      trades: { items: [fx.trade] },
    });
    const pages: [ReactElement, string][] = [
      [<SettingsPage />, "/settings"],
      [<SystemPage />, "/system"],
      [<TradesPage />, "/trades"],
      [<TradesPage />, "/trades?position=3"],
      [<ReportsPage />, "/reports?week=2026-10-09"],
      [<CandidatesPage />, "/candidates"],
    ];
    const seen = { links: 0, summaries: 0, checkboxes: 0 };
    for (const [ui, route] of pages) {
      const r = renderWithProviders(ui, { api, route });
      await settle();
      for (const a of Array.from(document.querySelectorAll("a"))) {
        seen.links += 1;
        const ok = TOUCH_LINK_CLASSES.some((c) => a.classList.contains(c));
        expect(ok, `${route}: link "${a.textContent}" has no touch class`).toBe(true);
      }
      seen.summaries += document.querySelectorAll("summary").length;
      for (const box of Array.from(document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))) {
        seen.checkboxes += 1;
        const row = box.closest("label");
        const labelled = row ?? (box.id ? document.querySelector(`label[for="${box.id}"]`) : null);
        expect(labelled?.classList.contains("check-row"), `${route}: checkbox ${box.id || box.name} is not in a check-row`).toBe(true);
      }
      r.unmount();
    }
    // The pages really have each kind (Settings: groups, kill-switch history, the None and Enabled boxes;
    // System: Force and Run nightly; Trades, Reports, Candidates and System: links).
    expect(seen.links).toBeGreaterThanOrEqual(6);
    expect(seen.summaries).toBeGreaterThanOrEqual(2);
    expect(seen.checkboxes).toBeGreaterThanOrEqual(4);
  });
});
