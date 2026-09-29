// Phone layout, touch targets (P4 Global Constraints: at least 44 px). Buttons carry an inline min-height
// (checked by the gauntlet's phone test); links, <summary> toggles and checkbox rows get theirs from shared
// rules in styles.css (with --touch from theme/tokens.css, DB-T11), so this test checks that the rules exist
// and that every such element uses them. DB-T11 test 9: the Dashboard and Control pages are in the list, and no
// page has a fixed width over a 390 px phone.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import * as fx from "../test/fixtures";
import * as lfx from "../test/liveFixtures";
import { FakeApiClient } from "../test/fakeApi";
import { renderWithProviders } from "../test/render";
import CandidatesPage from "./Candidates";
import ControlPage from "./Control";
import DashboardPage from "./Dashboard";
import ReportsPage from "./Reports";
import SettingsPage from "./Settings";
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
  it("styles.css has the shared rules, each with min-height: var(--touch); tokens.css sets --touch: 44px", () => {
    const css = readWebFile("src/styles.css");
    expect(readWebFile("src/theme/tokens.css")).toMatch(/--touch:\s*44px/);
    // DB-T11: the variables live in tokens.css only (main.tsx imports it before styles.css).
    expect(css).not.toMatch(/--touch:/);
    expect(readWebFile("src/main.tsx")).toMatch(/import "\.\/theme\/tokens\.css";\s*import "\.\/styles\.css";/);
    expect(ruleFor(css, ".link-touch")).toMatch(/min-height:\s*var\(--touch\)/);
    expect(ruleFor(css, "summary")).toMatch(/min-height:\s*var\(--touch\)/);
    const checkRow = ruleFor(css, ".check-row");
    expect(checkRow).toMatch(/min-height:\s*var\(--touch\)/);
    expect(checkRow).toMatch(/flex-direction:\s*row/);
  });

  it("every link, summary and checkbox on the pages uses them", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    const api = new FakeApiClient({
      control: lfx.controlWith({ health: { ...lfx.healthPanel, token: { ...fx.tokenOut, ok: false, error: "expired" } } }),
      live: lfx.liveWith({ positions: lfx.positionsN(20, { staleMarks: true }), pending: [fx.pendingProposal], worker_stale: true }),
      trades: { items: [fx.trade] },
    });
    const pages: [ReactElement, string][] = [
      [<DashboardPage />, "/dashboard"],
      [<DashboardPage />, "/dashboard?proposal=11&expand=100,101"],
      [<ControlPage />, "/control"],
      [<SettingsPage />, "/settings"],
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
      // DB-T11 test 9: nothing wider than a 390 px phone.
      for (const el of Array.from(document.querySelectorAll<HTMLElement>("[style]"))) {
        for (const prop of ["width", "minWidth"] as const) {
          const px = /^(\d+(?:\.\d+)?)px$/.exec(el.style[prop]);
          if (px) expect(Number(px[1]), `${route}: ${el.tagName} ${prop} ${el.style[prop]}`).toBeLessThanOrEqual(390);
        }
      }
      for (const svg of Array.from(document.querySelectorAll("svg[width]"))) {
        expect(Number(svg.getAttribute("width")), `${route}: an svg wider than the phone`).toBeLessThanOrEqual(390);
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
    // The pages really have each kind (Settings: groups, the None and Enabled boxes; Control: kill-switch
    // history, Force and Run nightly; Dashboard, Trades, Reports, Candidates and Control: links).
    expect(seen.links).toBeGreaterThanOrEqual(6);
    expect(seen.summaries).toBeGreaterThanOrEqual(2);
    expect(seen.checkboxes).toBeGreaterThanOrEqual(4);
  });
});
