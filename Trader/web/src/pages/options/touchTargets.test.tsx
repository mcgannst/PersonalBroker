// OPTSIM-T15: phone layout and touch targets for the Options page, by the same rules as the other pages
// (pages/touchTargets.test.tsx): every button carries the 44 px inline min-height, every link has a touch
// class, every checkbox sits in a `check-row`, and nothing has a fixed width over a 390 px phone.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { MIN_TOUCH_PX } from "../../components/ui";
import OptionsPage, { OPTION_TABS } from "../Options";
import { renderOptions } from "./testRender";

const TOUCH_LINK_CLASSES = ["link-touch", "btn", "nav-link", "shell-title"];

function readWebFile(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return readFileSync(found, "utf8");
}

async function settle(): Promise<void> {
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "Loading" })).toHaveLength(0));
}

function check(route: string, seen: { buttons: number; checkboxes: number }): void {
  for (const b of Array.from(document.querySelectorAll("button"))) {
    seen.buttons += 1;
    expect(b.style.minHeight, `${route}: button "${b.textContent}" has no 44 px min-height`).toBe(`${MIN_TOUCH_PX}px`);
  }
  for (const a of Array.from(document.querySelectorAll("a"))) {
    expect(TOUCH_LINK_CLASSES.some((c) => a.classList.contains(c)), `${route}: link "${a.textContent}" has no touch class`).toBe(true);
  }
  for (const box of Array.from(document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))) {
    seen.checkboxes += 1;
    const row = box.closest("label") ?? (box.id ? document.querySelector(`label[for="${box.id}"]`) : null);
    expect(row?.classList.contains("check-row"), `${route}: checkbox ${box.id || box.name} is not in a check-row`).toBe(true);
  }
  for (const el of Array.from(document.querySelectorAll<HTMLElement>("[style]"))) {
    for (const prop of ["width", "minWidth"] as const) {
      const px = /^(\d+(?:\.\d+)?)px$/.exec(el.style[prop]);
      if (px) expect(Number(px[1]), `${route}: ${el.tagName} ${prop} ${el.style[prop]}`).toBeLessThanOrEqual(390);
    }
  }
}

describe("Options page: 44 px touch targets and the phone layout", () => {
  it("every tab's buttons, links and checkboxes are touch targets, and nothing is wider than the phone", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    const user = userEvent.setup();
    const seen = { buttons: 0, checkboxes: 0 };
    for (const tab of OPTION_TABS) {
      const route = `/options?tab=${tab.id}`;
      const r = renderOptions(<OptionsPage />, { route });
      await settle();
      if (tab.id === "trade") {
        // With a chain loaded and a leg on the ticket.
        await user.type(screen.getByLabelText("Underlying"), "F");
        await user.click(screen.getByRole("button", { name: "Load chain" }));
        await user.click(await screen.findByRole("button", { name: /^Sell F .* P 12\.00 at the bid/ }));
      }
      if (tab.id === "strategies") {
        for (const more of await screen.findAllByRole("button", { name: /^Show / })) await user.click(more);
      }
      if (tab.id === "orders") await user.click((await screen.findAllByRole("button", { name: "Reprice" }))[0]!);
      await settle();
      check(route, seen);
      r.unmount();
    }
    // The page really has each kind: the seven tabs on every render, the chain's prices, the actions.
    expect(seen.buttons).toBeGreaterThanOrEqual(7 * OPTION_TABS.length + 20);
    expect(seen.checkboxes).toBeGreaterThanOrEqual(5);
  });

  it("options.css sets no fixed pixel width, and lets the tab row scroll inside itself", () => {
    const css = readWebFile("src/pages/options/options.css");
    expect(css).not.toMatch(/(?:^|[\s;{])(?:min-)?width:\s*\d+px/);
    expect(css).toMatch(/\.opt-tabs\s*\{[^}]*overflow-x:\s*auto/);
  });
});
