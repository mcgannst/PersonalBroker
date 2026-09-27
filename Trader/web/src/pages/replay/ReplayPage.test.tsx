// P5-T8 acceptance test 8: at 390 px no element is wider than the phone, tables scroll inside their card, and
// every button, link and checkbox is a 44 px target (the checks of the P4 phone test).
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import ReplayPage from "../Replay";

const TOUCH_LINK_CLASSES = ["link-touch", "btn"];

async function settle(): Promise<void> {
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "Loading" })).toHaveLength(0));
}

function checkPhoneLayout(route: string): number {
  for (const table of Array.from(document.querySelectorAll("table"))) {
    expect(table.parentElement?.classList.contains("table-scroll"), `${route}: a table outside .table-scroll`).toBe(true);
  }
  for (const el of Array.from(document.querySelectorAll<HTMLElement>("[style]"))) {
    for (const prop of ["width", "minWidth"] as const) {
      const px = /^(\d+(?:\.\d+)?)px$/.exec(el.style[prop]);
      if (px) expect(Number(px[1]), `${route}: ${el.tagName} ${prop} ${el.style[prop]}`).toBeLessThanOrEqual(390);
    }
  }
  for (const svg of Array.from(document.querySelectorAll("svg[width]"))) {
    expect(Number(svg.getAttribute("width")), `${route}: chart wider than the phone`).toBeLessThanOrEqual(390);
  }
  const buttons = Array.from(document.querySelectorAll<HTMLButtonElement>("button"));
  for (const button of buttons) {
    const name = (button.getAttribute("aria-label") ?? button.textContent ?? "").trim();
    expect(name, `${route}: a button without a name`).not.toBe("");
    expect(parseFloat(button.style.minHeight || "0"), `${route}: button "${name}" under 44 px`).toBeGreaterThanOrEqual(44);
  }
  for (const control of Array.from(document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>("input, select, textarea"))) {
    const labelled = (control.labels?.length ?? 0) > 0 || control.hasAttribute("aria-label") || control.hasAttribute("aria-labelledby");
    expect(labelled, `${route}: an unlabelled <${control.tagName.toLowerCase()} id=${control.id}>`).toBe(true);
  }
  for (const box of Array.from(document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))) {
    const row = box.closest("label") ?? (box.id ? document.querySelector(`label[for="${box.id}"]`) : null);
    expect(row?.classList.contains("check-row"), `${route}: checkbox ${box.id || box.name} is not in a check-row`).toBe(true);
  }
  for (const a of Array.from(document.querySelectorAll("a"))) {
    expect(TOUCH_LINK_CLASSES.some((c) => a.classList.contains(c)), `${route}: link "${a.textContent}" has no touch class`).toBe(true);
  }
  return buttons.length;
}

describe("the Replay page on a 390 px phone (acceptance test 8)", () => {
  it("list, form, running and completed detail: nothing wider than the phone, every button at least 44 px", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));
    const api = new FakeApiClient();

    const list = renderWithProviders(<ReplayPage />, { api, route: "/replay" });
    await screen.findByRole("table", { name: "Replays" });
    await userEvent.click(await screen.findByRole("button", { name: "New replay" }));
    await screen.findByRole("form", { name: "New replay" });
    await settle();
    expect(checkPhoneLayout("/replay (form)")).toBeGreaterThanOrEqual(2);
    list.unmount();

    for (const route of ["/replay?id=13", "/replay?id=11"]) {
      const r = renderWithProviders(<ReplayPage />, { api, route });
      await screen.findByRole("region", { name: `Replay ${route.slice(-2)}` });
      await settle();
      if (route.endsWith("13")) await userEvent.click(screen.getByRole("button", { name: "Cancel replay" }));
      else await screen.findByRole("figure", { name: "Equity" });
      checkPhoneLayout(route);
      r.unmount();
    }
  });

  it("the detail has a link back to the list", async () => {
    const r = renderWithProviders(<ReplayPage />, { route: "/replay?id=11" });
    await userEvent.click(await screen.findByRole("link", { name: "← Back to replays" }));
    await waitFor(() => expect(r.location().search).toBe(""));
    expect(await screen.findByRole("table", { name: "Replays" })).toBeInTheDocument();
  });
});
